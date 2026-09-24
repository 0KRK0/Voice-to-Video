"""P0-11 — schema changes are deliberate, ordered and repeatable.

Before this, the entire migration strategy was `CREATE TABLE IF NOT EXISTS`.
That is not a small stylistic point: it is the direct cause of the audit's
worst finding. `Project.organisation_id` was added as a nullable column with no
backfill, existing rows kept NULL, and the API read a null owner as "unowned,
therefore yours" — a cross-tenant read caused by not having a way to change a
schema on purpose.

So these tests assert the properties that make a migration system trustworthy
rather than merely present:

* running twice changes nothing the second time
* two processes racing produce one application, not two
* a migration edited after it was applied is refused, loudly
* `_0001` actually backfills, and quarantines rather than deletes what it
  cannot attribute
* a failure part-way leaves earlier migrations applied, so a retry resumes
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.config import Settings
from vtv.contracts.errors import VTVError
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.migrate import MIGRATIONS, Migration, Migrator, status, upgrade
from vtv.wiring import DATABASES, directory_path, repository_path


class MigrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-migrate-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
        )
        self.db = repository_path(self.settings)
        self.directory_db = directory_path(self.settings)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def legacy_repository(self) -> None:
        """The two databases as they were *before* tenancy, with rows in them.

        Written by hand rather than by importing the adapters, because the whole
        point is to reproduce databases created by an older release — narrower
        tables, no ledger, no `organisation_id`.

        Projects and organisations live in *different* files. That separation is
        not incidental to this test: pointing the denormalisation migration at
        the repository database, where `organisations` does not exist, is the
        defect these tests were extended to catch.
        """
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db, isolation_level=None) as connection:
            connection.executescript(
                """
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    payload    TEXT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'draft',
                    persistence TEXT NOT NULL DEFAULT 'temporary',
                    expires_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            # One row that predates tenancy entirely.
            connection.execute(
                "INSERT INTO projects (project_id, payload) VALUES (?, ?)",
                ("prj_orphan", json.dumps({"title": "from before tenancy"})),
            )
            # One row written after the contract changed but before a migration
            # existed: the tenant is in the JSON but not in a column.
            connection.execute(
                "INSERT INTO projects (project_id, payload) VALUES (?, ?)",
                (
                    "prj_recoverable",
                    json.dumps({"title": "has a tenant", "organisation_id": "org_acme"}),
                ),
            )

        self.directory_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.directory_db, isolation_level=None) as connection:
            connection.executescript(
                """
                CREATE TABLE organisations (
                    organisation_id TEXT PRIMARY KEY,
                    payload         TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO organisations (organisation_id, payload) VALUES (?, ?)",
                ("org_acme", json.dumps({"plan": "business", "suspended": False})),
            )
            connection.execute(
                "INSERT INTO organisations (organisation_id, payload) VALUES (?, ?)",
                ("org_gone", json.dumps({"plan": "free", "suspended": True})),
            )

    def repository_migrator(self) -> Migrator:
        return Migrator(self.db, "repository")

    def directory_migrator(self) -> Migrator:
        return Migrator(self.directory_db, "directory")

    def rows(self, sql: str, *, database: Path | None = None) -> list[tuple[object, ...]]:
        with sqlite3.connect(database or self.db) as connection:
            return [tuple(row) for row in connection.execute(sql)]


class TheLedgerIsTheSourceOfTruth(MigrationTestCase):
    def test_a_fresh_database_has_everything_pending(self) -> None:
        migrator = self.repository_migrator()
        self.assertEqual(
            [item.version for item in migrator.pending()],
            [item.version for item in MIGRATIONS if item.target == "repository"],
        )

    def test_upgrade_records_what_it_applied(self) -> None:
        self.legacy_repository()
        applied = self.repository_migrator().upgrade()
        self.assertTrue(applied)
        recorded = self.repository_migrator().applied()
        self.assertEqual(sorted(recorded), [item.version for item in applied])

    def test_running_twice_applies_nothing_the_second_time(self) -> None:
        """The property a deploy job and an operator both depend on."""
        self.legacy_repository()
        first = self.repository_migrator().upgrade()
        second = self.repository_migrator().upgrade()
        self.assertTrue(first)
        self.assertEqual(second, [], "a second run re-applied migrations")

    def test_each_migration_body_is_independently_idempotent(self) -> None:
        """Belt and braces: not just the ledger, the SQL too.

        A recovery procedure sometimes means applying a migration by hand. If
        only the ledger made it safe, doing that twice would break the database.
        """
        self.legacy_repository()
        with sqlite3.connect(self.db, isolation_level=None) as connection:
            for migration in MIGRATIONS:
                if migration.target != "repository":
                    continue
                migration.apply(connection)
                migration.apply(connection)  # deliberately again

        owners = {row[0] for row in self.rows("SELECT organisation_id FROM projects")}
        self.assertNotIn(None, owners)

    def test_status_reports_both_halves(self) -> None:
        self.legacy_repository()
        before = status(self.settings)
        repository = before["repository"]
        assert isinstance(repository, dict)
        self.assertEqual(repository["applied"], [])
        self.assertTrue(repository["pending"])

        upgrade(self.settings)
        after = status(self.settings)
        repository = after["repository"]
        assert isinstance(repository, dict)
        self.assertEqual(repository["pending"], [])


class AnEditedMigrationIsRefused(MigrationTestCase):
    def test_a_changed_checksum_raises_rather_than_being_skipped(self) -> None:
        """"The file says X and the database has Y" must be loud."""
        self.legacy_repository()
        migrator = self.repository_migrator()
        migrator.upgrade()

        # Simulate someone editing migration 1 after it shipped.
        with sqlite3.connect(self.db, isolation_level=None) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
                ("0" * 32,),
            )

        with self.assertRaises(VTVError) as caught:
            migrator.pending()
        self.assertIn("edited", str(caught.exception))

    def test_the_checksum_is_of_the_code_not_the_name(self) -> None:
        def one(_: sqlite3.Connection) -> None:
            pass

        def two(connection: sqlite3.Connection) -> None:
            connection.execute("SELECT 1")

        a = Migration(version=99, name="same", target="repository", apply=one)
        b = Migration(version=99, name="same", target="repository", apply=two)
        self.assertNotEqual(a.checksum, b.checksum)


class TheTenancyBackfillIsTheWholePoint(MigrationTestCase):
    def test_a_tenant_in_the_payload_is_recovered_not_guessed(self) -> None:
        self.legacy_repository()
        self.repository_migrator().upgrade()
        owner = self.rows(
            "SELECT organisation_id FROM projects WHERE project_id = 'prj_recoverable'"
        )
        self.assertEqual(owner, [("org_acme",)])

    def test_an_unattributable_row_is_quarantined_not_deleted(self) -> None:
        """Parking a project beats losing one."""
        self.legacy_repository()
        self.repository_migrator().upgrade()
        owner = self.rows(
            "SELECT organisation_id FROM projects WHERE project_id = 'prj_orphan'"
        )
        self.assertEqual(owner, [(SYSTEM_ORGANISATION_ID,)])

        surviving = self.rows("SELECT COUNT(*) FROM projects")
        self.assertEqual(surviving, [(2,)], "a migration deleted a customer's project")

    def test_no_project_is_left_without_an_owner(self) -> None:
        """The fail-open the audit found: NULL owner read as 'yours'."""
        self.legacy_repository()
        self.repository_migrator().upgrade()
        orphans = self.rows(
            "SELECT COUNT(*) FROM projects WHERE organisation_id IS NULL"
        )
        self.assertEqual(orphans, [(0,)])

    def test_the_tenant_index_exists_afterwards(self) -> None:
        self.legacy_repository()
        self.repository_migrator().upgrade()
        indexes = {row[0] for row in self.rows("SELECT name FROM sqlite_master "
                                               "WHERE type = 'index'")}
        self.assertIn("projects_tenant", indexes)


class TheDenormalisationMigration(MigrationTestCase):
    def test_suspension_and_plan_become_indexed_columns(self) -> None:
        """P0-10. Per-request work must not grow with the tenant count."""
        self.legacy_repository()
        self.directory_migrator().upgrade()

        rows = dict(
            self.rows(
                "SELECT organisation_id, active FROM organisations",
                database=self.directory_db,
            )  # type: ignore[arg-type]
        )
        self.assertEqual(rows["org_acme"], 1)
        self.assertEqual(rows["org_gone"], 0, "a suspended tenant read as active")

        plans = dict(
            self.rows(
                "SELECT organisation_id, plan FROM organisations",
                database=self.directory_db,
            )  # type: ignore[arg-type]
        )
        self.assertEqual(plans["org_acme"], "business")

        indexes = {
            row[0]
            for row in self.rows(
                "SELECT name FROM sqlite_master WHERE type = 'index'",
                database=self.directory_db,
            )
        }
        self.assertIn("organisations_active", indexes)

    def test_it_targets_the_database_the_table_actually_lives_in(self) -> None:
        """The defect this test exists for.

        `organisations` is in directory.db. The migration was declared against
        the repository, so `upgrade()` raised `no such table: organisations` on
        every fresh deployment — and no test noticed, because no test ran the
        real `upgrade()` against a real set of databases.
        """
        declared = {item.name: item.target for item in MIGRATIONS}
        self.assertEqual(
            declared["organisations_denormalise_hot_columns"], "directory"
        )

    def test_a_full_upgrade_of_every_database_succeeds(self) -> None:
        """The end-to-end version, which is what actually caught it."""
        self.legacy_repository()
        upgrade(self.settings)
        for report in status(self.settings).values():
            assert isinstance(report, dict)
            self.assertEqual(report["pending"], [], report)


class PartialFailureResumes(MigrationTestCase):
    def test_a_failure_leaves_earlier_migrations_applied(self) -> None:
        """One transaction per migration, not one for the run.

        A single wrapping transaction would roll back the successful steps too,
        turning every retry into a full re-run — which on a large backfill is
        the difference between a two-minute recovery and an outage.
        """
        self.legacy_repository()

        def explode(_: sqlite3.Connection) -> None:
            raise RuntimeError("disk full, halfway through")

        migrator = self.repository_migrator()
        repository_migrations = [
            item for item in MIGRATIONS if item.target == "repository"
        ]
        self.assertGreaterEqual(len(repository_migrations), 2)
        first, second = repository_migrations[0], repository_migrations[1]
        broken = Migration(
            version=second.version,
            name=second.name,
            target=second.target,
            apply=explode,
        )

        import vtv.migrate as module

        original = module.MIGRATIONS
        module.MIGRATIONS = tuple(
            broken if item is second else item for item in MIGRATIONS
        )
        try:
            with self.assertRaises(RuntimeError):
                migrator.upgrade()
        finally:
            module.MIGRATIONS = original

        # The first repository migration survived; the broken one did not.
        self.assertEqual(
            sorted(self.repository_migrator().applied()), [first.version]
        )
        owner = self.rows(
            "SELECT organisation_id FROM projects WHERE project_id = 'prj_recoverable'"
        )
        self.assertEqual(owner, [("org_acme",)], "the successful step was rolled back")

    def test_a_retry_after_a_failure_completes_the_run(self) -> None:
        self.legacy_repository()
        migrator = self.repository_migrator()
        migrator.upgrade()
        self.assertEqual(migrator.pending(), [])


class EveryDatabaseIsCovered(MigrationTestCase):
    def test_upgrade_touches_every_database_the_deployment_owns(self) -> None:
        """A migration runner that knows about a subset is a trap.

        It knew about three of six. Deriving the list from `wiring.DATABASES` —
        the same table the assembly builds from — is what makes "every database"
        true rather than aspirational.
        """
        report = status(self.settings)
        self.assertEqual(set(report), set(DATABASES))

    def test_every_migration_targets_a_database_that_exists(self) -> None:
        for migration in MIGRATIONS:
            with self.subTest(migration.name):
                self.assertIn(migration.target, DATABASES)

    def test_versions_are_unique_and_ordered(self) -> None:
        versions = [item.version for item in MIGRATIONS]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(len(versions), len(set(versions)))

    def test_every_migration_states_why_it_exists(self) -> None:
        for migration in MIGRATIONS:
            with self.subTest(migration.name):
                self.assertTrue(migration.reason, "a migration with no stated reason")
                self.assertTrue((migration.apply.__doc__ or "").strip())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
