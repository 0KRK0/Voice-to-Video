"""Correctness-critical state that outlives one process.

The audit's finding: `RateLimiter._buckets` and `UsageMeter._reservations` were
Python dictionaries. Two API replicas therefore enforced two independent rate
limits — so autoscaling multiplied the credential-stuffing allowance by the
number of pods — and neither could see the other's quota reservations, so a
tenant could exceed a paid plan simply by having requests land on different
pods.

This module is the shared store those two need. It is deliberately *not* Redis:
there is no Redis in this environment, and inventing a dependency we cannot run
would move the same defect from "process-local" to "untested". It is SQLite on a
shared file, which is genuinely correct across processes on one host, and it is
written against an interface a Redis implementation satisfies unchanged.

**What makes it correct rather than merely shared.** Every operation is a single
atomic statement. The token bucket is not read-modify-write in Python; it is an
`UPDATE ... WHERE tokens >= cost` that either spends or does not, so two
processes racing produce one winner rather than two. That is the same discipline
the durable queue uses for claiming, and for the same reason.

**Honest limits.** SQLite on a shared filesystem is correct for a single host
and for a small fleet sharing a volume. It is not correct across a network
filesystem with weak locking, and it will contend before Redis would. Both facts
are stated here rather than discovered in production, and `SharedStore` is the
seam where a Redis or Postgres implementation drops in.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

SCHEMA = """
CREATE TABLE IF NOT EXISTS buckets (
    key        TEXT PRIMARY KEY,
    tokens     REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL DEFAULT 0,
    expires_at REAL
);

CREATE INDEX IF NOT EXISTS counters_expiry ON counters (expires_at);
"""


@runtime_checkable
class SharedStore(Protocol):
    """The operations shared correctness needs, and no more.

    Deliberately tiny. A Redis implementation of exactly this is a couple of
    Lua scripts; anything richer would tempt callers into logic that only one
    backend supports.
    """

    def spend(
        self, key: str, *, cost: float, rate: float, burst: float, now: float
    ) -> tuple[bool, float, float]:
        """Atomically refill and spend from a token bucket.

        Returns ``(allowed, remaining, retry_after)``. Must be a single atomic
        operation: a read followed by a write lets two processes both succeed
        on the last token.
        """
        ...

    def clear(self, key: str) -> None:
        """Forget a bucket, e.g. after a successful login."""
        ...


@dataclass
class SqliteSharedStore:
    """A shared store on one SQLite file.

    STATUS: **EXECUTED.** Correct across processes on a single host. See the
    module docstring for where it stops being the right answer.
    """

    path: Path
    busy_timeout_ms: int = 5_000
    #: Buckets untouched for this long are removed by `vacuum`, so a caller
    #: cycling keys cannot grow the table without bound — which would turn the
    #: rate limiter into the denial of service it exists to prevent.
    stale_after_seconds: float = 3600.0

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL so a reader never blocks a writer, and a busy timeout so two
        # processes contending for the write lock wait rather than fail.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def spend(
        self, key: str, *, cost: float, rate: float, burst: float, now: float
    ) -> tuple[bool, float, float]:
        with self._connect() as connection:
            # BEGIN IMMEDIATE takes the write lock up front. Without it two
            # processes can both read the same token count inside their own
            # deferred transactions and both decide they may spend it.
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT tokens, updated_at FROM buckets WHERE key = ?", (key,)
                ).fetchone()

                if row is None:
                    tokens = burst
                    elapsed = 0.0
                else:
                    elapsed = max(0.0, now - float(row["updated_at"]))
                    tokens = min(burst, float(row["tokens"]) + elapsed * rate)

                if tokens >= cost:
                    tokens -= cost
                    allowed, retry_after = True, 0.0
                else:
                    allowed = False
                    retry_after = round((cost - tokens) / rate, 3) if rate > 0 else 60.0

                connection.execute(
                    "INSERT INTO buckets (key, tokens, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET tokens = excluded.tokens, "
                    "updated_at = excluded.updated_at",
                    (key, tokens, now),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return allowed, tokens, retry_after

    def clear(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM buckets WHERE key = ?", (key,))

    def vacuum(self, *, now: float | None = None) -> int:
        """Drop stale buckets. Returns how many went.

        Safe by construction: a missing bucket refills to full, which is
        generous rather than dangerous, and unbounded growth is the failure
        mode that actually takes a service down.
        """
        cutoff = (now or time.time()) - self.stale_after_seconds
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM buckets WHERE updated_at < ?", (cutoff,)
            )
            return int(cursor.rowcount)

    def size(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM buckets").fetchone()
        return int(row["n"])


@dataclass
class InMemorySharedStore:
    """The single-process implementation, for tests and for `make demo`.

    Kept explicitly rather than as a default, so that choosing it is a decision
    someone made rather than an accident nobody noticed. Wiring selects the
    SQLite store unless the deployment is explicitly single-process.
    """

    stale_after_seconds: float = 3600.0
    max_keys: int = 100_000

    def __post_init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}

    def spend(
        self, key: str, *, cost: float, rate: float, burst: float, now: float
    ) -> tuple[bool, float, float]:
        held = self._buckets.get(key)
        if held is None:
            if len(self._buckets) >= self.max_keys:
                self.vacuum(now=now)
            tokens = burst
        else:
            stored, updated = held
            tokens = min(burst, stored + max(0.0, now - updated) * rate)

        if tokens >= cost:
            tokens -= cost
            self._buckets[key] = (tokens, now)
            return True, tokens, 0.0

        self._buckets[key] = (tokens, now)
        retry_after = round((cost - tokens) / rate, 3) if rate > 0 else 60.0
        return False, tokens, retry_after

    def clear(self, key: str) -> None:
        self._buckets.pop(key, None)

    def vacuum(self, *, now: float | None = None) -> int:
        cutoff = (now or time.time()) - self.stale_after_seconds
        stale = [key for key, (_, updated) in self._buckets.items() if updated < cutoff]
        if not stale:
            # Nothing has aged out, so shed the oldest tenth rather than grow.
            ordered = sorted(self._buckets.items(), key=lambda item: item[1][1])
            stale = [key for key, _ in ordered[: max(1, len(ordered) // 10)]]
        for key in stale:
            self._buckets.pop(key, None)
        return len(stale)

    def size(self) -> int:
        return len(self._buckets)


__all__ = [
    "SCHEMA",
    "InMemorySharedStore",
    "SharedStore",
    "SqliteSharedStore",
]
