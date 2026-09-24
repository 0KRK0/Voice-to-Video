"""Forward-only schema migrations.

`python -m vtv.migrate upgrade`

Before this, the entire migration strategy was `CREATE TABLE IF NOT EXISTS`.
That is why `Project.organisation_id` could be added as a nullable column with
no backfill, and why the API then read a null owner as "unowned, therefore
yours" — a cross-tenant read caused, at root, by not having a way to change a
schema deliberately.

**Forward only.** There are no down-migrations. A down-migration is a promise to
reverse a data change that has already been observed by users, and it is almost
always a lie: dropping a column does not restore the information that was in it.
Recovery from a bad migration is a restore plus a new forward migration, which
is the procedure `docs/RUNNING.md` documents and which is actually testable.

**Idempotent.** Every migration is safe to run twice. Two API replicas starting
at once, a retried deploy job and an operator running it by hand must all
converge, so applying is guarded by a ledger row *and* each step is written to
tolerate having already happened.

**Ordered and hashed.** The ledger records the checksum of what was applied. A
migration edited after the fact is refused rather than silently skipped, because
"the file says X and the database has Y" is the failure that takes a weekend.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vtv.adapters.queue.durable import SCHEMA as QUEUE_SCHEMA
from vtv.adapters.repository.sqlite import SCHEMA as REPOSITORY_SCHEMA
from vtv.billing.usage import SCHEMA as USAGE_SCHEMA
from vtv.config import Settings
from vtv.contracts.base import utc_now
from vtv.contracts.errors import ErrorCode, VTVError
from vtv.security.audit import SCHEMA as AUDIT_SCHEMA
from vtv.security.directory import SCHEMA as DIRECTORY_SCHEMA
from vtv.security.shared_state import SCHEMA as SHARED_SCHEMA
from vtv.wiring import DATABASES

#: The current shape of each database, as `CREATE TABLE IF NOT EXISTS`.
#:
#: Every adapter also runs its own schema in its constructor, which is what made
#: this system work at all before there were migrations. That is kept — an
#: adapter that cannot open its own store is a worse failure — but it means the
#: schema exists in two places, so they are the *same* string imported from the
#: adapter rather than a copy that drifts.
#:
#: The baseline runs before migrations, so `python -m vtv.migrate upgrade` on an
#: empty volume produces a usable database. Without it the first migration hits
#: `no such table` on every fresh deployment, which is exactly what happened.
BASELINES: dict[str, str] = {
    "repository": REPOSITORY_SCHEMA,
    "queue": QUEUE_SCHEMA,
    "shared": SHARED_SCHEMA,
    "directory": DIRECTORY_SCHEMA,
    "audit": AUDIT_SCHEMA,
    "usage": USAGE_SCHEMA,
}

LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    """One forward step."""

    version: int
    name: str
    #: Which database this applies to: "repository", "queue", "shared".
    target: str
    apply: Callable[[sqlite3.Connection], None]
    #: Free text, shown by `status`, explaining why the change was needed.
    reason: str = ""

    @property
    def checksum(self) -> str:
        """Identity of the *code*, so an edited migration is detectable."""
        import inspect

        try:
            body = inspect.getsource(self.apply)
        except (OSError, TypeError):  # pragma: no cover - defensive
            body = self.name
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    """Whether a table exists.

    Every migration body starts with this. The runner establishes the baseline
    schema first, so in the normal path the answer is always yes — but a
    migration is also the thing an operator applies by hand during a recovery,
    and `no such table` in the middle of that is a bad half-hour.
    """
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _0001_projects_have_a_tenant(connection: sqlite3.Connection) -> None:
    """Add `organisation_id` to projects and backfill it.

    The audit's P0-3. The column was introduced without a migration, so existing
    rows had NULL, and the API treated NULL as "no owner, so anyone may read
    it". Backfilling from the payload and then refusing NULLs closes it at the
    only layer that can be trusted.
    """
    if not _has_table(connection, "projects"):
        return
    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    if "organisation_id" not in columns:
        connection.execute("ALTER TABLE projects ADD COLUMN organisation_id TEXT")

    # The tenant is already inside the stored JSON for anything written after
    # the contract change; recover it rather than guessing.
    connection.execute(
        "UPDATE projects SET organisation_id = json_extract(payload, "
        "'$.organisation_id') WHERE organisation_id IS NULL"
    )

    # Anything still NULL predates tenancy entirely. It is quarantined to a
    # reserved tenant rather than deleted — losing a customer's project to a
    # migration is worse than parking it somewhere only an operator can reach.
    connection.execute(
        "UPDATE projects SET organisation_id = ? WHERE organisation_id IS NULL",
        ("prj_0000000000000000000000",),
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS projects_tenant "
        "ON projects (organisation_id, updated_at DESC)"
    )


def _0002_organisations_denormalise_hot_columns(
    connection: sqlite3.Connection,
) -> None:
    """Add indexed `active` and `plan` columns to organisations.

    The audit's P0-10. Every request called `suspended_ids()`, which read and
    JSON-parsed every organisation in the deployment; request cost therefore
    grew with customer count.
    """
    if not _has_table(connection, "organisations"):
        return
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(organisations)")
    }
    if "active" not in columns:
        connection.execute(
            "ALTER TABLE organisations ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
        )
    if "plan" not in columns:
        connection.execute(
            "ALTER TABLE organisations ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'"
        )
    connection.execute(
        "UPDATE organisations SET "
        "active = CASE WHEN json_extract(payload, '$.suspended') = 1 THEN 0 "
        "              WHEN json_extract(payload, '$.deleted_at') IS NOT NULL THEN 0 "
        "              ELSE 1 END, "
        "plan = COALESCE(json_extract(payload, '$.plan'), 'free')"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS organisations_active ON organisations (active)"
    )


def _0003_projects_record_their_outcome(connection: sqlite3.Connection) -> None:
    """Nothing structural — the outcome lives in the payload.

    Recorded as a migration anyway so the ledger reflects the release that
    introduced `Project.outcome`, and so an operator reading the ledger can see
    when a project's status stopped meaning "a file exists".
    """
    if not _has_table(connection, "projects"):
        return
    connection.execute(
        "CREATE INDEX IF NOT EXISTS projects_expiry_scan "
        "ON projects (persistence, expires_at)"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="projects_have_a_tenant",
        target="repository",
        apply=_0001_projects_have_a_tenant,
        reason="P0-3: a nullable tenant column read as 'unowned, therefore yours'",
    ),
    Migration(
        version=2,
        name="organisations_denormalise_hot_columns",
        # `organisations` lives in directory.db, not the project repository.
        target="directory",
        apply=_0002_organisations_denormalise_hot_columns,
        reason="P0-10: per-request work grew with the number of tenants",
    ),
    Migration(
        version=3,
        name="projects_record_their_outcome",
        target="repository",
        apply=_0003_projects_record_their_outcome,
        reason="P1-4: status alone said a file existed, not that it was delivered",
    ),
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class Migrator:
    """Applies pending migrations to one database file."""

    path: Path
    target: str

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.executescript(LEDGER)
        return connection

    def is_empty(self) -> bool:
        """Whether this database has nothing but the migration ledger."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
            ).fetchone()
        return int(rows[0]) == 0

    def baseline(self) -> None:
        """Create the current schema — **only on an empty database**.

        The "only" is the whole subtlety, and getting it wrong is subtle in a
        way that bites in production rather than in a test.

        `CREATE TABLE IF NOT EXISTS` is harmless against an existing table. The
        `CREATE INDEX` statements in the same script are not: the current schema
        indexes columns that a table created by an *older* release may not have,
        so running today's baseline over yesterday's database fails with
        `no such column` — during the migration job, before anything serves.

        So a database that already has tables is the migrations' responsibility
        alone, which is also the rule that makes migrations meaningful. Adding
        an index or a column to an existing deployment is a migration, never an
        edit to the baseline.
        """
        script = BASELINES.get(self.target)
        if not script or not self.is_empty():
            return
        with self._connect() as connection:
            connection.executescript(script)

    def applied(self) -> dict[int, str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version, checksum FROM schema_migrations"
            ).fetchall()
        return {int(row[0]): str(row[1]) for row in rows}

    def pending(self) -> list[Migration]:
        done = self.applied()
        mine = [item for item in MIGRATIONS if item.target == self.target]
        for item in mine:
            recorded = done.get(item.version)
            if recorded is not None and recorded != item.checksum:
                raise VTVError(
                    f"migration {item.version} ({item.name}) was edited after "
                    "it was applied; the database and the code disagree",
                    code=ErrorCode.SCHEMA_INVALID,
                )
        return [item for item in mine if item.version not in done]

    def upgrade(self) -> list[Migration]:
        """Apply everything outstanding. Safe to run concurrently."""
        self.baseline()
        applied: list[Migration] = []
        for migration in self.pending():
            with self._connect() as connection:
                # One transaction per migration: a failure leaves earlier steps
                # applied and recorded, so a retry resumes rather than restarts.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    already = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?",
                        (migration.version,),
                    ).fetchone()
                    if already is not None:
                        # Another replica won the race. Not an error.
                        connection.execute("COMMIT")
                        continue
                    migration.apply(connection)
                    connection.execute(
                        "INSERT INTO schema_migrations "
                        "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            utc_now().isoformat(),
                        ),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            applied.append(migration)
        return applied


def migrators(settings: Settings) -> list[Migrator]:
    """Every database this deployment owns.

    Derived from `wiring.DATABASES` rather than listed here. A hand-written list
    is how three of six databases got migrated and the other three did not — and
    the one that mattered, `directory.db`, held the `organisations` table that
    migration 2 was pointed at.
    """
    return [
        Migrator(locate(settings), name) for name, locate in DATABASES.items()
    ]


def upgrade(settings: Settings | None = None) -> list[Migration]:
    settings = settings or Settings.from_env()
    applied: list[Migration] = []
    for migrator in migrators(settings):
        applied.extend(migrator.upgrade())
    return applied


def status(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or Settings.from_env()
    report: dict[str, object] = {}
    for migrator in migrators(settings):
        report[migrator.target] = {
            "path": str(migrator.path),
            "applied": sorted(migrator.applied()),
            "pending": [item.version for item in migrator.pending()],
        }
    return report


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entrypoint
    argv = list(argv if argv is not None else sys.argv[1:])
    command = argv[0] if argv else "upgrade"
    settings = Settings.from_env()

    if command == "status":
        for target, detail in status(settings).items():
            print(f"{target}: {detail}")
        return 0
    if command != "upgrade":
        print(f"unknown command {command!r}; expected 'upgrade' or 'status'")
        return 2

    applied = upgrade(settings)
    if not applied:
        print("schema is up to date")
    for migration in applied:
        print(f"applied {migration.version:04d} {migration.name} — {migration.reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "LEDGER",
    "MIGRATIONS",
    "Migration",
    "Migrator",
    "main",
    "migrators",
    "status",
    "upgrade",
]
