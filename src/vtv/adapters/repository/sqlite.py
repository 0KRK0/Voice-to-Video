"""SQLite persistence.

STATUS: **REAL IMPLEMENTATION — RUNS.**

PostgreSQL is the production target (`docs/ARCHITECTURE.md`) and the DDL in
`postgres_schema()` below is the same shape. SQLite is what this environment can
actually run, and for a single-node development install it is genuinely the right
answer: no server, no configuration, one file, real transactions.

Documents are stored as JSON with an insertion sequence rather than being
overwritten, so the plan an old project was rendered from survives an upgrade to
the Visual Director. That is the data-versioning requirement from the brief, and
it costs one integer column.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from vtv.contracts.project import PersistenceMode, Project

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id      TEXT PRIMARY KEY,
    organisation_id TEXT,
    owner_id        TEXT,
    persistence     TEXT NOT NULL,
    status          TEXT NOT NULL,
    expires_at      TEXT,
    updated_at      TEXT NOT NULL,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_owner ON projects (owner_id, updated_at DESC);
-- Every tenant-scoped read goes through this index. A project list that does
-- not filter on organisation_id is a cross-customer disclosure.
CREATE INDEX IF NOT EXISTS projects_tenant
    ON projects (organisation_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS projects_expiry ON projects (expires_at);

CREATE TABLE IF NOT EXISTS documents (
    sequence     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    document_id  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects (project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS documents_lookup
    ON documents (project_id, kind, sequence DESC);
"""


def postgres_schema() -> str:
    """The equivalent PostgreSQL DDL.

    Kept beside the SQLite schema so the two cannot drift apart unnoticed. The
    only real differences are JSONB and a proper timestamp type.
    """
    return """
CREATE TABLE IF NOT EXISTS projects (
    project_id      TEXT PRIMARY KEY,
    organisation_id TEXT,
    owner_id        TEXT,
    persistence     TEXT NOT NULL,
    status          TEXT NOT NULL,
    expires_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_owner ON projects (owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS projects_tenant
    ON projects (organisation_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS projects_expiry ON projects (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS documents (
    sequence     BIGSERIAL PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    document_id  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_lookup
    ON documents (project_id, kind, sequence DESC);
"""


class SqliteProjectRepository:
    """Projects and their documents, in one file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # WAL lets a reader (the API) and a writer (a render worker) coexist,
        # which is the only concurrency this design needs before Postgres.
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    async def ping(self) -> None:
        """Prove the database is reachable and has the schema we expect.

        Used by readiness. Cheap on purpose — a probe that scans a table adds
        load precisely when the system is already struggling — but not so cheap
        that it passes against an empty file: a missing `projects` table means
        migrations have not run, which is a reason not to take traffic.
        """
        await asyncio.to_thread(self._ping_blocking)

    def _ping_blocking(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone()

    # -- projects ---------------------------------------------------------

    async def save_project(self, project: Project) -> None:
        payload = project.model_dump_json()
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO projects
                        (project_id, organisation_id, owner_id, persistence,
                         status, expires_at, updated_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (project_id) DO UPDATE SET
                        organisation_id = excluded.organisation_id,
                        owner_id = excluded.owner_id,
                        persistence = excluded.persistence,
                        status = excluded.status,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at,
                        payload = excluded.payload
                    """,
                    (
                        project.project_id,
                        project.organisation_id,
                        project.owner_id,
                        project.persistence.value,
                        project.status.value,
                        project.expires_at.isoformat() if project.expires_at else None,
                        datetime.now().isoformat(),
                        payload,
                    ),
                )

    async def get_project(
        self, project_id: str, *, organisation_id: str | None = None
    ) -> Project | None:
        """Fetch one project, optionally scoped to a tenant.

        Passing ``organisation_id`` pushes the isolation check into the query
        rather than leaving it to the caller. A caller that forgets an `if` is
        a data leak; a query that cannot match is not.
        """
        async with self._lock:
            with self._connect() as connection:
                if organisation_id is None:
                    row = connection.execute(
                        "SELECT payload FROM projects WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT payload FROM projects WHERE project_id = ? "
                        "AND organisation_id = ?",
                        (project_id, organisation_id),
                    ).fetchone()
        return Project.model_validate_json(row["payload"]) if row else None

    async def list_projects(
        self,
        *,
        owner_id: str | None = None,
        organisation_id: str | None = None,
        limit: int = 50,
    ) -> list[Project]:
        if organisation_id is not None:
            async with self._lock:
                with self._connect() as connection:
                    rows = connection.execute(
                        "SELECT payload FROM projects WHERE organisation_id = ? "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (organisation_id, limit),
                    ).fetchall()
            return [Project.model_validate_json(row["payload"]) for row in rows]
        async with self._lock:
            with self._connect() as connection:
                if owner_id is None:
                    rows = connection.execute(
                        "SELECT payload FROM projects ORDER BY updated_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT payload FROM projects WHERE owner_id = ? "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (owner_id, limit),
                    ).fetchall()
        return [Project.model_validate_json(row["payload"]) for row in rows]

    async def delete_project(
        self, project_id: str, *, organisation_id: str | None = None
    ) -> None:
        """Delete a project and its documents, optionally scoped to a tenant.

        The scope is part of the DELETE rather than a check before it, so a
        caller holding the wrong tenant's id removes nothing instead of
        removing something.
        """
        async with self._lock:
            with self._connect() as connection:
                if organisation_id is None:
                    connection.execute(
                        "DELETE FROM documents WHERE project_id = ?", (project_id,)
                    )
                    connection.execute(
                        "DELETE FROM projects WHERE project_id = ?", (project_id,)
                    )
                    return
                owned = connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ? "
                    "AND organisation_id = ?",
                    (project_id, organisation_id),
                ).fetchone()
                if owned is None:
                    return
                connection.execute(
                    "DELETE FROM documents WHERE project_id = ?", (project_id,)
                )
                connection.execute(
                    "DELETE FROM projects WHERE project_id = ? "
                    "AND organisation_id = ?",
                    (project_id, organisation_id),
                )

    # -- documents --------------------------------------------------------

    async def put_document(
        self, *, project_id: str, kind: str, document_id: str, payload: dict[str, Any]
    ) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO documents "
                    "(project_id, kind, document_id, created_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        project_id,
                        kind,
                        document_id,
                        datetime.now().isoformat(),
                        json.dumps(payload, separators=(",", ":")),
                    ),
                )

    async def get_document(
        self, *, project_id: str, kind: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM documents WHERE project_id = ? AND kind = ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (project_id, kind),
                ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    async def document_history(
        self, *, project_id: str, kind: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Every version of a document, newest first.

        This is what makes "which Visual Director produced this?" answerable six
        months later.
        """
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT payload FROM documents WHERE project_id = ? AND kind = ? "
                    "ORDER BY sequence DESC LIMIT ?",
                    (project_id, kind, limit),
                ).fetchall()
        return [dict(json.loads(row["payload"])) for row in rows]

    # -- retention --------------------------------------------------------

    async def expired_projects(
        self, *, now: datetime, organisation_id: str | None = None
    ) -> list[str]:
        """Projects past their retention, optionally within one tenant.

        The unscoped form exists for the scheduled system-principal sweep in
        the worker. It is deliberately not reachable from an HTTP handler.
        """
        async with self._lock:
            with self._connect() as connection:
                if organisation_id is None:
                    rows = connection.execute(
                        "SELECT project_id FROM projects WHERE persistence = ? "
                        "AND expires_at IS NOT NULL AND expires_at < ?",
                        (PersistenceMode.TEMPORARY.value, now.isoformat()),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT project_id FROM projects WHERE persistence = ? "
                        "AND expires_at IS NOT NULL AND expires_at < ? "
                        "AND organisation_id = ?",
                        (
                            PersistenceMode.TEMPORARY.value,
                            now.isoformat(),
                            organisation_id,
                        ),
                    ).fetchall()
        return [row["project_id"] for row in rows]


__all__ = ["SCHEMA", "SqliteProjectRepository", "postgres_schema"]
