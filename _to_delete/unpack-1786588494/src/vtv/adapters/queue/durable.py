"""Stages 27–28: a job queue that survives the process running it.

STATUS: **REAL IMPLEMENTATION — RUNS.** Exercised by ``tests/test_durable_queue.py``
against real SQLite files, including two independent queue instances competing for
the same rows and eight OS threads racing on the same claim statement. What is
*not* exercised here is a genuine crash: the recovery tests reproduce the database
state a killed worker leaves behind (a row in ``running`` with a stale heartbeat)
rather than actually killing a process. That is a real gap and it is named here
rather than papered over.

The in-process sibling in ``inprocess.py`` keeps job state in dictionaries, so a
restart loses every running job — the one property that makes it unfit for
production. This adapter moves the state into SQLite and treats the database as
the only source of truth: a job exists because a row exists, it is running because
that row says so, and a process that dies takes nothing with it. The interface is
deliberately identical (``register``, ``enqueue``, ``status``, ``cancel``, the same
four events) so that choosing between them is a wiring decision.

Three properties are worth stating precisely, because queues are usually wrong in
exactly these three places.

**Delivery is at-least-once, never at-most-once.** A claim is a single atomic
``UPDATE ... WHERE state = 'pending'``, so two workers cannot both take the same
row. But a worker that finishes the work and dies before writing the outcome will
hand that row to somebody else once its heartbeat goes stale. Handlers must
therefore be idempotent. The alternative — exactly-once — is not available to a
system that also has to touch object storage and provider APIs, and pretending
otherwise would just move the duplicate somewhere less visible.

**Idempotency is a unique index, not a dictionary.** ``idempotency_key`` is
enforced by SQLite, which is what makes "the same double-clicked button after a
deploy" resolve to the same job rather than a second paid-for render.

**Backoff is deterministic.** ``deterministic_jitter`` derives its spread from a
BLAKE2b digest of the job id and attempt number, so two workers retrying two jobs
still spread out, but the delay for a given job is reproducible in a test and in a
post-mortem. Nothing here calls :mod:`random`.

The queue depends on ports and contracts only; it must never import
``vtv.pipeline``, since an adapter that knows what work it is carrying stops being
swappable.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from vtv.contracts.base import Id, IdPrefix, new_id
from vtv.contracts.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    NotFound,
    Status,
    TimeoutExceeded,
    ValidationFailed,
    VTVError,
)
from vtv.observability.events import EventName, EventSink
from vtv.ports.jobs import JobHandle, JobPriority

#: Same shape as the in-process queue's handler, so one handler can be registered
#: with either adapter without a wrapper.
Handler = Callable[[dict[str, Any]], Awaitable[Any]]

#: ``(job_id, attempt, delay) -> delay``. Injected so a test can pin it.
JitterFn = Callable[[str, int, float], float]

#: ``UPDATE ... RETURNING`` claims a row and reads it back in one statement.
#: Without it the claim would need a second SELECT and a per-claim token to tell
#: our row from one we claimed earlier.
MIN_SQLITE_VERSION = (3, 35, 0)

_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(Id)


class JobState(str, Enum):
    """Storage-level state, which is finer-grained than :class:`Status`.

    The port speaks in :class:`Status`, whose vocabulary has no "dead letter" and
    no "cancelled". Rather than bend those meanings in the public handle, the
    database keeps the states an operator needs to act on and
    :func:`_handle_status` projects them onto the port's smaller vocabulary.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


#: Lower sorts first. Kept as a stored column rather than computed at query time
#: so the claim query can use an index instead of a CASE expression.
PRIORITY_RANK: dict[JobPriority, int] = {
    JobPriority.INTERACTIVE: 0,
    JobPriority.STANDARD: 1,
    JobPriority.BATCH: 2,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    payload           TEXT NOT NULL,
    priority          TEXT NOT NULL,
    priority_rank     INTEGER NOT NULL,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL,
    enqueued_at       REAL NOT NULL,
    available_at      REAL NOT NULL,
    updated_at        REAL NOT NULL,
    claimed_by        TEXT,
    heartbeat_at      REAL,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    idempotency_key   TEXT,
    project_id        TEXT,
    last_error        TEXT
);

-- The index, not a pre-flight SELECT, is what makes idempotency hold: two
-- processes enqueueing the same key concurrently both survive, one by inserting
-- and one by catching the constraint.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency
    ON jobs (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS jobs_claim
    ON jobs (state, priority_rank, available_at);
CREATE INDEX IF NOT EXISTS jobs_recovery
    ON jobs (state, heartbeat_at);
"""


def postgres_schema() -> str:
    """The equivalent PostgreSQL DDL.

    Kept beside the SQLite schema, as in ``adapters/repository/sqlite.py``, so the
    two cannot drift apart unnoticed. On PostgreSQL the claim becomes
    ``SELECT ... FOR UPDATE SKIP LOCKED``, which lets many workers claim in
    parallel; SQLite serialises writers instead, which is correct but caps
    throughput at one claim at a time.
    """
    return """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    payload           JSONB NOT NULL,
    priority          TEXT NOT NULL,
    priority_rank     SMALLINT NOT NULL,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL,
    enqueued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    available_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by        TEXT,
    heartbeat_at      TIMESTAMPTZ,
    cancel_requested  BOOLEAN NOT NULL DEFAULT false,
    idempotency_key   TEXT,
    project_id        TEXT,
    last_error        JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency
    ON jobs (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_claim
    ON jobs (state, priority_rank, available_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS jobs_recovery
    ON jobs (state, heartbeat_at) WHERE state = 'running';
"""


def backoff_seconds(
    attempt: int, *, base: float, factor: float, cap: float
) -> float:
    """Exponential delay before attempt ``attempt + 1``, before jitter.

    ``attempt`` is 1-based: the delay after the first failure is ``base``. The cap
    exists because an unbounded exponential eventually schedules a retry past the
    point where anyone still cares about the result.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be 1-based, got {attempt}")
    return min(cap, base * (factor ** (attempt - 1)))


def deterministic_jitter(job_id: str, attempt: int, delay: float) -> float:
    """Spread retries over ``[delay/2, delay]`` without touching :mod:`random`.

    Two jobs failing against the same rate-limited provider must not retry in
    lockstep, which is what jitter is for. Seeding from a digest of the job id and
    attempt gets that spread while keeping every delay reproducible — the same job
    computes the same delay in a test, in a re-run and in a post-mortem, and
    across processes, which ``hash()`` would not give us.
    """
    digest = hashlib.blake2b(f"{job_id}:{attempt}".encode(), digest_size=8).digest()
    fraction = int.from_bytes(digest, "big") / float(1 << 64)
    return delay * (0.5 + 0.5 * fraction)


def no_jitter(job_id: str, attempt: int, delay: float) -> float:
    """Identity jitter, for tests that assert on exact schedule arithmetic."""
    return delay


def _handle_status(state: JobState, attempts: int) -> Status:
    """Project the storage state onto the port's :class:`Status` vocabulary.

    ``CANCELLED`` reports as ``FAILED`` with a cancellation ``ErrorInfo`` attached,
    matching what the in-process queue does to a cancelled task; the distinction
    that matters to an operator is preserved in the row and surfaced by
    :meth:`DurableJobQueue.stats`.
    """
    if state is JobState.RUNNING:
        return Status.PROCESSING
    if state is JobState.SUCCEEDED:
        return Status.READY
    if state in (JobState.DEAD_LETTER, JobState.CANCELLED):
        return Status.FAILED
    return Status.RETRYING if attempts > 0 else Status.PENDING


def _project_id_of(payload: dict[str, Any]) -> str | None:
    """Extract a project id for event correlation, or nothing.

    A payload whose ``project_id`` does not match the contract's id shape is
    telemetry we drop rather than an enqueue we reject: refusing the job would
    turn a cosmetic problem in an unrelated field into lost work.
    """
    value = payload.get("project_id")
    if not isinstance(value, str):
        return None
    try:
        return _ID_ADAPTER.validate_python(value)
    except ValidationError:
        return None


@dataclass(frozen=True)
class ClaimedJob:
    """One row, taken by one worker, on its way to a handler."""

    job_id: str
    kind: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    project_id: str | None


class DurableJobQueue:
    """A SQLite-backed queue whose rows outlive the process.

    Satisfies :class:`vtv.ports.jobs.JobQueue`. Unlike the in-process sibling,
    ``enqueue`` does not require a handler to be registered here: in a deployment
    the API process enqueues and a worker process runs, and demanding that both
    know every handler would defeat the point. A worker only claims kinds it can
    actually run, so a job whose kind nobody registered waits visibly in
    ``stats()`` instead of being failed by whichever process happened to see it.
    """

    def __init__(
        self,
        path: Path | str,
        events: EventSink,
        *,
        worker_id: str | None = None,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
        backoff_factor: float = 2.0,
        max_backoff_seconds: float = 300.0,
        jitter: JitterFn = deterministic_jitter,
        max_payload_bytes: int = 64 * 1024,
        reclaim_after_seconds: float = 60.0,
        heartbeat_interval_seconds: float = 5.0,
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], float] = time.time,
        recover_on_start: bool = True,
    ) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise VTVError(
                f"DurableJobQueue needs SQLite >= "
                f"{'.'.join(str(part) for part in MIN_SQLITE_VERSION)}, "
                f"found {sqlite3.sqlite_version}",
                code=ErrorCode.STORAGE_UNAVAILABLE,
            )
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events = events
        self.handlers: dict[str, Handler] = {}
        # Identifies this process in ``claimed_by``, so an operator looking at a
        # stuck row can tell which worker was holding it.
        self.worker_id = worker_id or f"worker-{new_id(IdPrefix.RENDER_JOB)[4:12]}"
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.backoff_factor = backoff_factor
        self.max_backoff_seconds = max_backoff_seconds
        self.jitter = jitter
        self.max_payload_bytes = max_payload_bytes
        self.reclaim_after_seconds = reclaim_after_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.busy_timeout_ms = busy_timeout_ms
        self.clock = clock

        with self._connect() as connection:
            connection.executescript(SCHEMA)
        if recover_on_start:
            # Blocking I/O in a constructor is deliberate: this runs once, at
            # startup, before the process serves anything, and a queue that came
            # up without recovering would silently strand the work a crash left
            # behind. Periodic reaping goes through the async `recover()`.
            self._recover_blocking(self.clock())

    # -- connections ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # WAL keeps a reader (the API asking for status) from blocking a writer
        # (a worker claiming), and the busy timeout absorbs the write-lock
        # contention two workers necessarily create. There is no asyncio.Lock
        # here, unlike the repository adapter: every mutation below is a single
        # atomic statement, so the database — not this process — is the arbiter,
        # which is the only thing that works across processes.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    # -- registration -----------------------------------------------------

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    # -- enqueue ----------------------------------------------------------

    def _encode_payload(self, payload: dict[str, Any]) -> str:
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValidationFailed(
                f"job payload is not JSON-serialisable: {error}"
            ) from error
        size = len(encoded.encode("utf-8"))
        if size > self.max_payload_bytes:
            # A queue row is a reference to work, not a place to keep bytes.
            # Storing an unbounded blob here would make every claim query drag it
            # off disk and would put user content into a table that is copied into
            # logs and backups far more casually than object storage is.
            raise ValidationFailed(
                f"job payload is {size} bytes, over the "
                f"{self.max_payload_bytes} byte limit; store the data and enqueue "
                f"a reference to it",
                context={"payload_bytes": size, "limit_bytes": self.max_payload_bytes},
            )
        return encoded

    async def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        priority: JobPriority = JobPriority.STANDARD,
        idempotency_key: str | None = None,
        delay_seconds: float = 0.0,
    ) -> JobHandle:
        if not kind:
            raise ValidationFailed("job kind must not be empty")
        encoded = self._encode_payload(payload)
        # Mirrors the in-process queue's id scheme so job ids look the same
        # whichever adapter is wired in.
        job_id = new_id(IdPrefix.RENDER_JOB if kind == "render" else IdPrefix.PROJECT)
        now = self.clock()
        row, created = await asyncio.to_thread(
            self._enqueue_blocking,
            job_id,
            kind,
            encoded,
            priority,
            idempotency_key,
            _project_id_of(payload),
            now,
            now + max(delay_seconds, 0.0),
        )
        handle = _row_to_handle(row)
        if created:
            self.events.emit(
                EventName.JOB_ENQUEUED,
                project_id=handle.project_id,
                data={"job_id": handle.job_id, "kind": kind, "priority": priority.value},
            )
        return handle

    def _enqueue_blocking(
        self,
        job_id: str,
        kind: str,
        payload: str,
        priority: JobPriority,
        idempotency_key: str | None,
        project_id: str | None,
        now: float,
        available_at: float,
    ) -> tuple[sqlite3.Row, bool]:
        columns = (
            "job_id, kind, payload, priority, priority_rank, state, attempts, "
            "max_attempts, enqueued_at, available_at, updated_at, idempotency_key, "
            "project_id"
        )
        values = (
            job_id,
            kind,
            payload,
            priority.value,
            PRIORITY_RANK[priority],
            JobState.PENDING.value,
            0,
            self.max_attempts,
            now,
            available_at,
            now,
            idempotency_key,
            project_id,
        )
        with self._connect() as connection:
            try:
                connection.execute(
                    f"INSERT INTO jobs ({columns}) "
                    f"VALUES ({', '.join('?' * len(values))})",
                    values,
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is None:
                    raise
                return existing, False
            inserted = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if inserted is None:  # pragma: no cover - the insert above just succeeded
            raise VTVError(
                f"job {job_id} vanished immediately after insert",
                code=ErrorCode.STORAGE_UNAVAILABLE,
            )
        return inserted, True

    # -- observation ------------------------------------------------------

    async def status(self, job_id: str) -> JobHandle:
        row = await asyncio.to_thread(self._row_blocking, job_id)
        if row is None:
            raise NotFound(f"unknown job {job_id}")
        return _row_to_handle(row)

    def _row_blocking(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row

    async def stats(self) -> dict[str, int]:
        """Counts per state, including zeros, for the ops and health surface."""
        return await asyncio.to_thread(self._stats_blocking)

    def _stats_blocking(self) -> dict[str, int]:
        counts = dict.fromkeys((state.value for state in JobState), 0)
        with self._connect() as connection:
            for row in connection.execute(
                "SELECT state, COUNT(*) AS total FROM jobs GROUP BY state"
            ).fetchall():
                counts[str(row["state"])] = int(row["total"])
        return counts

    async def outstanding(self, kinds: Sequence[str] | None = None) -> int:
        """Jobs still pending or running, optionally restricted to some kinds."""
        return await asyncio.to_thread(self._outstanding_blocking, kinds)

    def _outstanding_blocking(self, kinds: Sequence[str] | None) -> int:
        if kinds is not None and not kinds:
            return 0
        sql = "SELECT COUNT(*) AS total FROM jobs WHERE state IN (?, ?)"
        params: list[Any] = [JobState.PENDING.value, JobState.RUNNING.value]
        if kinds is not None:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            params.extend(kinds)
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["total"])

    async def is_cancelled(self, job_id: str) -> bool:
        """Whether cancellation has been requested, for a cooperative handler."""
        row = await asyncio.to_thread(self._row_blocking, job_id)
        if row is None:
            raise NotFound(f"unknown job {job_id}")
        return bool(row["cancel_requested"])

    # -- cancellation -----------------------------------------------------

    async def cancel(self, job_id: str) -> None:
        """Cancel pending work. Running work is asked to stop; it may not.

        A running handler is interrupted at its next ``await`` once the heartbeat
        notices the flag. One that never awaits — a long ``ffmpeg`` call, say —
        cannot be interrupted at all, which is why the port promises only that the
        job is *asked* to stop.
        """
        cancelled_info = ErrorInfo.of(
            ErrorCode.INTERNAL_ERROR,
            ErrorCategory.INTERNAL,
            "job cancelled before it started",
            user_message="That job was cancelled.",
        )
        outcome = await asyncio.to_thread(
            self._cancel_blocking,
            job_id,
            self.clock(),
            cancelled_info.model_dump_json(),
        )
        if outcome == "unknown":
            raise NotFound(f"unknown job {job_id}")

    def _cancel_blocking(self, job_id: str, now: float, error_json: str) -> str:
        with self._connect() as connection:
            pending = connection.execute(
                "UPDATE jobs SET state = ?, cancel_requested = 1, last_error = ?, "
                "updated_at = ? WHERE job_id = ? AND state = ?",
                (
                    JobState.CANCELLED.value,
                    error_json,
                    now,
                    job_id,
                    JobState.PENDING.value,
                ),
            )
            if pending.rowcount == 1:
                return "cancelled"
            running = connection.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? "
                "WHERE job_id = ? AND state = ?",
                (now, job_id, JobState.RUNNING.value),
            )
            if running.rowcount == 1:
                return "flagged"
            exists = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        # Cancelling finished work is a no-op rather than an error: the caller's
        # intent (this job must not run) is already satisfied.
        return "terminal" if exists else "unknown"

    # -- recovery ---------------------------------------------------------

    async def recover(self) -> tuple[int, int]:
        """Re-queue work abandoned by dead workers. Returns (requeued, dead-lettered).

        Safe to call periodically as well as at startup: a worker that is alive
        keeps its ``heartbeat_at`` fresh, so only rows that have gone quiet for
        ``reclaim_after_seconds`` are taken away from their claimant.
        """
        return await asyncio.to_thread(self._recover_blocking, self.clock())

    def _recover_blocking(self, now: float) -> tuple[int, int]:
        cutoff = now - self.reclaim_after_seconds
        lost = ErrorInfo.of(
            ErrorCode.INTERNAL_ERROR,
            ErrorCategory.INTERNAL,
            "worker stopped responding while the job was running and the job has "
            "no attempts left",
            user_message="Something went wrong on our side.",
        )
        with self._connect() as connection:
            # A crashed attempt has already been counted — `attempts` is
            # incremented when the row is claimed, not when it completes — so a
            # job that has burned through its budget by crashing must not be
            # re-queued for ever. It is dead-lettered here with the reason
            # recorded, which is the difference between a poison job that stops
            # and one that takes a worker down every minute until someone notices.
            dead = connection.execute(
                "UPDATE jobs SET state = ?, last_error = ?, claimed_by = NULL, "
                "heartbeat_at = NULL, updated_at = ? "
                "WHERE state = ? AND attempts >= max_attempts "
                "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                (
                    JobState.DEAD_LETTER.value,
                    lost.model_dump_json(),
                    now,
                    JobState.RUNNING.value,
                    cutoff,
                ),
            )
            requeued = connection.execute(
                "UPDATE jobs SET state = ?, claimed_by = NULL, heartbeat_at = NULL, "
                "available_at = ?, updated_at = ? "
                "WHERE state = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                (
                    JobState.PENDING.value,
                    now,
                    now,
                    JobState.RUNNING.value,
                    cutoff,
                ),
            )
        return int(requeued.rowcount), int(dead.rowcount)

    # -- claiming and running ---------------------------------------------

    def _claim_blocking(
        self, worker_id: str, kinds: Sequence[str], now: float
    ) -> ClaimedJob | None:
        if not kinds:
            return None
        placeholders = ", ".join("?" * len(kinds))
        with self._connect() as connection:
            # One statement, guarded twice: the subquery picks the best candidate
            # and the outer `state = 'pending'` makes the write itself conditional.
            # Because SQLite serialises writers, a worker that loses the race
            # updates nothing and gets an empty result rather than a second copy
            # of somebody else's job.
            row = connection.execute(
                f"""
                UPDATE jobs
                   SET state = ?, claimed_by = ?, heartbeat_at = ?,
                       attempts = attempts + 1, updated_at = ?
                 WHERE job_id = (
                        SELECT job_id FROM jobs
                         WHERE state = ? AND cancel_requested = 0
                           AND available_at <= ? AND kind IN ({placeholders})
                         ORDER BY priority_rank ASC, available_at ASC, rowid ASC
                         LIMIT 1)
                   AND state = ?
                RETURNING job_id, kind, payload, attempts, max_attempts, project_id
                """,
                (
                    JobState.RUNNING.value,
                    worker_id,
                    now,
                    now,
                    JobState.PENDING.value,
                    now,
                    *kinds,
                    JobState.PENDING.value,
                ),
            ).fetchone()
        if row is None:
            return None
        project_id = row["project_id"]
        return ClaimedJob(
            job_id=str(row["job_id"]),
            kind=str(row["kind"]),
            payload=dict(json.loads(str(row["payload"]))),
            attempt=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            project_id=str(project_id) if project_id is not None else None,
        )

    async def run_once(self, *, worker_id: str | None = None) -> str | None:
        """Claim and run at most one job. Returns its id, or ``None`` if idle."""
        who = worker_id or self.worker_id
        job = await asyncio.to_thread(
            self._claim_blocking, who, tuple(self.handlers), self.clock()
        )
        if job is None:
            return None
        await self._execute(job, who)
        return job.job_id

    async def _execute(self, job: ClaimedJob, worker_id: str) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:  # pragma: no cover - only if register() raced the claim
            await asyncio.to_thread(self._release_blocking, job.job_id, self.clock())
            return

        self.events.emit(
            EventName.JOB_STARTED,
            project_id=job.project_id,
            data={"job_id": job.job_id, "kind": job.kind, "attempt": job.attempt},
        )

        cancelled = asyncio.Event()
        running: asyncio.Task[Any] = asyncio.create_task(
            _coroutine(handler, job.payload), name=f"vtv-job-{job.job_id}"
        )
        beat = asyncio.create_task(
            self._heartbeat(job.job_id, worker_id, running, cancelled),
            name=f"vtv-beat-{job.job_id}",
        )
        try:
            await running
        except asyncio.CancelledError:
            if not cancelled.is_set():
                # The worker itself is going down, not the job. Leave the row in
                # `running` with a heartbeat that will go stale: recovery is the
                # one code path that decides what happens to interrupted work.
                raise
            await self._settle(
                job,
                worker_id,
                JobState.CANCELLED,
                ErrorInfo.of(
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    "job cancelled while running",
                    user_message="That job was cancelled.",
                    attempt=job.attempt,
                ),
            )
            return
        except VTVError as error:
            error.info.attempt = job.attempt
            await self._fail(job, worker_id, error.info)
            return
        except Exception as error:
            # An exception that is not a VTVError reaching here is by definition a
            # bug (docs/ERROR_MODEL.md), so it is reported as one rather than
            # being dressed up as a provider problem.
            await self._fail(
                job,
                worker_id,
                ErrorInfo.of(
                    ErrorCode.INTERNAL_ERROR,
                    ErrorCategory.INTERNAL,
                    f"{type(error).__name__}: {error}"[:2000],
                    user_message="Something went wrong on our side.",
                    attempt=job.attempt,
                ),
            )
            return
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

        await self._settle(job, worker_id, JobState.SUCCEEDED, None)
        self.events.emit(
            EventName.JOB_COMPLETED,
            project_id=job.project_id,
            data={"job_id": job.job_id, "kind": job.kind, "attempt": job.attempt},
        )

    async def _heartbeat(
        self,
        job_id: str,
        worker_id: str,
        running: asyncio.Task[Any],
        cancelled: asyncio.Event,
    ) -> None:
        """Keep the claim alive and act on a cancellation request.

        The heartbeat is what distinguishes a slow worker from a dead one. Its
        interval also sets how quickly a cancellation reaches a running job, which
        is the trade the caller makes when tuning it: shorter means more writes.
        """
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            requested = await asyncio.to_thread(
                self._heartbeat_blocking, job_id, worker_id, self.clock()
            )
            if requested:
                cancelled.set()
                running.cancel()
                return

    def _heartbeat_blocking(self, job_id: str, worker_id: str, now: float) -> bool:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE job_id = ? AND claimed_by = ? "
                "AND state = ?",
                (now, job_id, worker_id, JobState.RUNNING.value),
            )
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return bool(row["cancel_requested"]) if row is not None else False

    async def _fail(
        self, job: ClaimedJob, worker_id: str, info: ErrorInfo
    ) -> None:
        exhausted = job.attempt >= job.max_attempts
        available_at: float | None = None
        if not exhausted:
            delay = self.jitter(
                job.job_id,
                job.attempt,
                backoff_seconds(
                    job.attempt,
                    base=self.base_backoff_seconds,
                    factor=self.backoff_factor,
                    cap=self.max_backoff_seconds,
                ),
            )
            available_at = self.clock() + delay
        await self._settle(
            job,
            worker_id,
            JobState.DEAD_LETTER if exhausted else JobState.PENDING,
            info,
            available_at=available_at,
        )
        self.events.emit(
            EventName.JOB_FAILED,
            project_id=job.project_id,
            data={
                "job_id": job.job_id,
                "kind": job.kind,
                "code": info.code.value,
                "attempt": job.attempt,
                "dead_letter": exhausted,
            },
        )

    async def _settle(
        self,
        job: ClaimedJob,
        worker_id: str,
        state: JobState,
        info: ErrorInfo | None,
        *,
        available_at: float | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._settle_blocking,
            job.job_id,
            worker_id,
            state,
            info.model_dump_json() if info is not None else None,
            available_at if available_at is not None else self.clock(),
            self.clock(),
        )

    def _settle_blocking(
        self,
        job_id: str,
        worker_id: str,
        state: JobState,
        error_json: str | None,
        available_at: float,
        now: float,
    ) -> bool:
        with self._connect() as connection:
            # `claimed_by = ?` is the guard that makes at-least-once safe in the
            # other direction: if recovery already gave this row to another worker
            # while we were running, our outcome is stale and is discarded rather
            # than overwriting theirs.
            cursor = connection.execute(
                "UPDATE jobs SET state = ?, last_error = ?, available_at = ?, "
                "claimed_by = NULL, heartbeat_at = NULL, updated_at = ? "
                "WHERE job_id = ? AND claimed_by = ? AND state = ?",
                (
                    state.value,
                    error_json,
                    available_at,
                    now,
                    job_id,
                    worker_id,
                    JobState.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def _release_blocking(self, job_id: str, now: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET state = ?, claimed_by = NULL, heartbeat_at = NULL, "
                "attempts = MAX(attempts - 1, 0), updated_at = ? "
                "WHERE job_id = ? AND state = ?",
                (JobState.PENDING.value, now, job_id, JobState.RUNNING.value),
            )

    # -- draining ---------------------------------------------------------

    async def drain(
        self,
        *,
        timeout: float = 30.0,
        workers: int = 1,
        poll_interval: float = 0.005,
    ) -> int:
        """Run jobs until nothing this process can handle is left. Returns the count.

        Used by shutdown and by tests. Only kinds registered here are waited for,
        so a queue holding work for a different worker does not make this hang.
        Delayed retries are waited for too — "drained" means the backlog is
        genuinely finished, not merely quiet for a moment.
        """
        stop = asyncio.Event()
        kinds = tuple(self.handlers)

        async def loop(index: int) -> int:
            executed = 0
            while not stop.is_set():
                job_id = await self.run_once(worker_id=f"{self.worker_id}#{index}")
                if job_id is None:
                    await asyncio.sleep(poll_interval)
                else:
                    executed += 1
            return executed

        tasks = [asyncio.create_task(loop(index)) for index in range(max(workers, 1))]
        deadline = time.monotonic() + timeout
        try:
            while await self.outstanding(kinds) > 0:
                if time.monotonic() >= deadline:
                    raise TimeoutExceeded(
                        f"queue did not drain within {timeout}s; "
                        f"{await self.outstanding(kinds)} jobs outstanding"
                    )
                await asyncio.sleep(poll_interval)
        except BaseException:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        stop.set()
        return sum(await asyncio.gather(*tasks))

    # -- dead letters -----------------------------------------------------

    async def dead_letters(self, *, limit: int = 50) -> list[JobHandle]:
        """Jobs that exhausted their attempts, newest first, with their error."""
        rows = await asyncio.to_thread(self._dead_letters_blocking, limit)
        return [_row_to_handle(row) for row in rows]

    def _dead_letters_blocking(self, limit: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM jobs WHERE state = ? ORDER BY updated_at DESC "
                    "LIMIT ?",
                    (JobState.DEAD_LETTER.value, limit),
                ).fetchall()
            )

    async def replay(self, job_id: str) -> JobHandle:
        """Put a dead letter back on the queue as a fresh job.

        Attempts reset to zero and the recorded error is cleared: the operator has
        already read it from :meth:`dead_letters`, and leaving it attached would
        make a job that has not run yet report as failed. Replaying is an explicit
        human decision, which is why nothing does it automatically.
        """
        row = await asyncio.to_thread(self._replay_blocking, job_id, self.clock())
        handle = _row_to_handle(row)
        self.events.emit(
            EventName.JOB_ENQUEUED,
            project_id=handle.project_id,
            data={"job_id": handle.job_id, "kind": handle.kind, "replay": True},
        )
        return handle

    def _replay_blocking(self, job_id: str, now: float) -> sqlite3.Row:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET state = ?, attempts = 0, last_error = NULL, "
                "available_at = ?, claimed_by = NULL, heartbeat_at = NULL, "
                "cancel_requested = 0, updated_at = ? "
                "WHERE job_id = ? AND state = ?",
                (
                    JobState.PENDING.value,
                    now,
                    now,
                    job_id,
                    JobState.DEAD_LETTER.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"unknown job {job_id}")
            if cursor.rowcount != 1:
                raise ValidationFailed(
                    f"job {job_id} is {row['state']}, and only a dead letter can be "
                    f"replayed"
                )
        found: sqlite3.Row = row
        return found


async def _coroutine(handler: Handler, payload: dict[str, Any]) -> Any:
    """Adapt a handler's awaitable into a coroutine.

    ``asyncio.create_task`` accepts coroutines, not arbitrary awaitables, and the
    port declares the looser type so that a handler may be any awaitable.
    """
    return await handler(payload)


def _row_to_handle(row: sqlite3.Row) -> JobHandle:
    raw_error = row["last_error"]
    project_id = row["project_id"]
    return JobHandle(
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        status=_handle_status(JobState(str(row["state"])), int(row["attempts"])),
        # The port counts attempts from one; the table counts attempts taken,
        # which is zero for a job nobody has claimed yet.
        attempt=max(int(row["attempts"]), 1),
        project_id=str(project_id) if project_id is not None else None,
        error=ErrorInfo.model_validate_json(str(raw_error)) if raw_error else None,
    )


__all__ = [
    "PRIORITY_RANK",
    "SCHEMA",
    "ClaimedJob",
    "DurableJobQueue",
    "Handler",
    "JitterFn",
    "JobState",
    "backoff_seconds",
    "deterministic_jitter",
    "no_jitter",
    "postgres_schema",
]
