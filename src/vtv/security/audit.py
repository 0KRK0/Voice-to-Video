"""The audit log, and the redaction that keeps secrets out of it.

An audit log has one property that matters: an actor who can alter the record of
what they did has not been audited. So this is append-only by construction —
there is no update path and no delete path, and the SQLite table enforces it
with triggers rather than trusting the application not to try.

Redaction is the other half. An audit entry that faithfully records an API key,
a password or a bearer token has moved the secret from a place with access
control to a place that is deliberately long-lived and widely readable. Every
value written goes through :func:`redact` first.

Retention is a real tension. Audit entries are exactly what a customer needs
during an incident and exactly what privacy law says should not be kept forever.
The resolution here: entries expire on a schedule like everything else, security
relevant ones live longer than routine ones, and the boundary is a documented
constant rather than a number somebody chose in a migration.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vtv.contracts.base import utc_now
from vtv.contracts.tenancy import AuditAction, AuditEvent, Principal

#: Routine entries — a project created, a render submitted.
ROUTINE_RETENTION_DAYS = 90

#: Security-relevant entries — denials, key lifecycle, failed logins. Kept
#: longer because an incident is usually discovered long after it began.
SECURITY_RETENTION_DAYS = 400

MAX_DETAIL_VALUE = 500
MAX_DETAIL_KEYS = 24

#: Anything whose *name* suggests a secret is redacted regardless of its value,
#: because a value that does not look like a secret today may tomorrow.
_SECRET_KEYS = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|authorization|auth|credential"
    r"|private[_-]?key|session|cookie|signature|otp|pin)",
    re.I,
)

#: Value-shaped detection, for secrets that arrive under an innocent name.
_SECRET_VALUES: tuple[re.Pattern[str], ...] = (
    re.compile(r"vtv_(live|test)_[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # A long unbroken high-entropy run. Deliberately conservative: over-
    # redacting a hash is harmless, under-redacting a key is not.
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)

REDACTED = "[redacted]"


def redact(value: str) -> str:
    """Remove anything that looks like a secret from a string."""
    cleaned = value
    for pattern in _SECRET_VALUES:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


def redact_mapping(detail: dict[str, Any]) -> dict[str, str]:
    """Bound and redact a detail mapping before it is stored.

    Keys that name a secret are replaced wholesale rather than pattern-matched,
    because the value under `password` is a secret whatever it looks like.
    """
    cleaned: dict[str, str] = {}
    for key, value in list(detail.items())[:MAX_DETAIL_KEYS]:
        name = str(key)[:64]
        if _SECRET_KEYS.search(name):
            cleaned[name] = REDACTED
            continue
        cleaned[name] = redact(str(value))[:MAX_DETAIL_VALUE]
    return cleaned


SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id  TEXT PRIMARY KEY,
    organisation_id TEXT,
    action          TEXT NOT NULL,
    actor           TEXT NOT NULL,
    actor_role      TEXT,
    target          TEXT,
    succeeded       INTEGER NOT NULL,
    security        INTEGER NOT NULL,
    detail          TEXT NOT NULL,
    request_id      TEXT,
    ip_address      TEXT,
    user_agent      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_by_org
    ON audit_events (organisation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_by_action
    ON audit_events (action, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_security
    ON audit_events (security, created_at DESC);

-- Append-only, enforced by the database rather than by convention. An
-- application bug, a careless migration and a compromised process all hit the
-- same wall.
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit entries cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_events
WHEN OLD.created_at > datetime('now', '-90 days') OR OLD.security = 1
BEGIN
    SELECT RAISE(ABORT, 'audit entries cannot be deleted before they expire');
END;
"""


@dataclass
class AuditLog:
    """Append-only audit storage on SQLite.

    Writes are synchronous and deliberately so. An audit entry that is queued
    and lost on the crash it was recording is worse than no audit log, because
    it produces confident, incomplete evidence.
    """

    path: Path
    #: Also mirror entries to the event stream, so an operator watching a live
    #: deployment sees denials as they happen rather than on query.
    events: object = None
    _memory: list[AuditEvent] = field(default_factory=list)
    #: When true, entries are kept in memory only. For tests and for a
    #: deployment that has not yet chosen a store — and it says so rather than
    #: pretending to persist.
    in_memory: bool = False

    def __post_init__(self) -> None:
        if self.in_memory:
            return
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def write(
        self,
        *,
        action: AuditAction,
        principal: Principal | None = None,
        organisation_id: str | None = None,
        target: str | None = None,
        succeeded: bool = True,
        detail: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditEvent:
        """Record one action. Returns the entry that was stored."""
        actor = (
            f"{principal.kind.value}:{principal.subject}"
            if principal is not None
            else "system:unknown"
        )
        event = AuditEvent(
            organisation_id=organisation_id
            or (principal.organisation_id if principal else None),
            action=action,
            actor=actor[:160],
            actor_role=principal.role if principal else None,
            target=(target or None) and str(target)[:200],
            succeeded=succeeded,
            detail=redact_mapping(detail or {}),
            request_id=principal.request_id if principal else None,
            ip_address=(ip_address or None) and str(ip_address)[:64],
            user_agent=(user_agent or None) and redact(str(user_agent))[:256],
        )

        if self.in_memory:
            self._memory.append(event)
        else:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO audit_events (audit_event_id, organisation_id, "
                    "action, actor, actor_role, target, succeeded, security, "
                    "detail, request_id, ip_address, user_agent, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.audit_event_id,
                        event.organisation_id,
                        event.action.value,
                        event.actor,
                        event.actor_role.value if event.actor_role else None,
                        event.target,
                        int(event.succeeded),
                        int(event.is_security_relevant),
                        _dump(event.detail),
                        event.request_id,
                        event.ip_address,
                        event.user_agent,
                        event.created_at.isoformat(),
                    ),
                )

        if self.events is not None:
            from vtv.observability.events import EventName

            self.events.emit(  # type: ignore[attr-defined]
                EventName.AUDIT_RECORDED,
                project_id=None,
                data={
                    "action": event.action.value,
                    "actor": event.actor,
                    "succeeded": event.succeeded,
                    "security": event.is_security_relevant,
                    "target": event.target,
                },
            )
        return event

    def recent(
        self,
        *,
        organisation_id: str | None = None,
        actions: Sequence[AuditAction] | None = None,
        security_only: bool = False,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Read back entries, newest first.

        ``organisation_id`` is not optional in practice: reading another
        tenant's audit log is exactly the disclosure the log exists to detect.
        The API route requires `AUDIT_READ` *and* passes the caller's own
        organisation.
        """
        limit = max(1, min(limit, 1000))
        if self.in_memory:
            remembered = [
                event
                for event in reversed(self._memory)
                if (organisation_id is None or event.organisation_id == organisation_id)
                and (actions is None or event.action in set(actions))
                and (not security_only or event.is_security_relevant)
            ]
            return remembered[:limit]

        clauses: list[str] = []
        params: list[Any] = []
        if organisation_id is not None:
            clauses.append("organisation_id = ?")
            params.append(organisation_id)
        if actions:
            clauses.append(
                f"action IN ({','.join('?' for _ in actions)})"
            )
            params.extend(action.value for action in actions)
        if security_only:
            clauses.append("security = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as connection:
            found = connection.execute(
                f"SELECT * FROM audit_events {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_row_to_event(row) for row in found]

    def purge_expired(self) -> int:
        """Delete entries past their retention. Returns how many went.

        The only deletion path, and it cannot reach anything still inside its
        window — the table's trigger refuses, so a bug here fails loudly instead
        of quietly destroying evidence.
        """
        if self.in_memory:
            before = len(self._memory)
            cutoff = utc_now().timestamp() - ROUTINE_RETENTION_DAYS * 86400
            security_cutoff = utc_now().timestamp() - SECURITY_RETENTION_DAYS * 86400
            self._memory = [
                event
                for event in self._memory
                if event.created_at.timestamp()
                > (security_cutoff if event.is_security_relevant else cutoff)
            ]
            return before - len(self._memory)

        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM audit_events WHERE "
                "(security = 0 AND created_at < datetime('now', ?)) OR "
                "(security = 1 AND created_at < datetime('now', ?))",
                (f"-{ROUTINE_RETENTION_DAYS} days", f"-{SECURITY_RETENTION_DAYS} days"),
            )
            return int(cursor.rowcount)


def _dump(detail: dict[str, str]) -> str:
    import json

    return json.dumps(detail, sort_keys=True, ensure_ascii=False)


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    import json

    from vtv.contracts.tenancy import Role

    role = row["actor_role"]
    return AuditEvent(
        audit_event_id=str(row["audit_event_id"]),
        organisation_id=row["organisation_id"],
        action=AuditAction(row["action"]),
        actor=str(row["actor"]),
        actor_role=Role(role) if role else None,
        target=row["target"],
        succeeded=bool(row["succeeded"]),
        detail=json.loads(str(row["detail"])),
        request_id=row["request_id"],
        ip_address=row["ip_address"],
        user_agent=row["user_agent"],
    )


__all__ = [
    "MAX_DETAIL_KEYS",
    "MAX_DETAIL_VALUE",
    "REDACTED",
    "ROUTINE_RETENTION_DAYS",
    "SCHEMA",
    "SECURITY_RETENTION_DAYS",
    "AuditLog",
    "redact",
    "redact_mapping",
]
