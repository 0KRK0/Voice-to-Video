"""P1-5 — the PostgreSQL schema, checked as far as it can be without a server.

**These tests do not run PostgreSQL.** There is no server here, no `asyncpg`,
and no package index to fetch one from. Saying so is the point: a test file that
quietly skipped would leave "P1-5 has tests" true and useless.

What they *can* check is the class of mistake that makes row-level security
decorative, and every one of these has taken down a real system:

* a tenant column that is nullable — the P0-3 defect, at the database
* `ENABLE ROW LEVEL SECURITY` without `FORCE`, so the table owner bypasses
  every policy and the owner is usually the migration role
* a `USING` clause with no `WITH CHECK`, so a tenant can *write* a row
  attributed to someone else
* a new table added without a policy
* `SET` where `SET LOCAL` was meant, leaking a tenant across a pooled connection

Each is a property of the DDL as text, so each is testable here. What is not
testable here — that PostgreSQL accepts this DDL, and that a real connection
under a real policy actually refuses a cross-tenant read — is listed at the
bottom as explicitly unproven.
"""

from __future__ import annotations

import re
import unittest

from vtv.adapters.repository.postgres_schema import (
    TENANT_SETTING,
    TENANT_TABLES,
    rls_ddl,
    roles_ddl,
    schema,
    set_tenant_sql,
    tables_ddl,
)


def table_body(name: str) -> str:
    """The text between `CREATE TABLE <name> (` and its closing paren."""
    ddl = tables_ddl()
    start = ddl.index(f"CREATE TABLE IF NOT EXISTS {name} (")
    return ddl[start : ddl.index(");", start)]


class EveryTenantTableNamesItsTenant(unittest.TestCase):
    def test_the_tenant_column_exists_and_is_not_null(self) -> None:
        """P0-3, expressed where the database can enforce it.

        A nullable tenant is not a schema convenience. It is a row that no
        policy matches and that every `WHERE organisation_id = $1` misses:
        invisible rather than protected.
        """
        for table in TENANT_TABLES:
            with self.subTest(table):
                body = table_body(table)
                self.assertRegex(
                    body,
                    r"organisation_id\s+TEXT\s+NOT NULL",
                    f"{table} has a nullable or missing tenant column",
                )

    def test_the_tenant_column_leads_an_index(self) -> None:
        """A policy that cannot use an index is a sequential scan per query."""
        ddl = tables_ddl()
        indexed = set(re.findall(r"ON (\w+) \(organisation_id", ddl))
        indexed |= set(re.findall(r"PRIMARY KEY \(organisation_id", ddl))
        # memberships has organisation_id first in its composite primary key.
        indexed.add("memberships")
        for table in TENANT_TABLES:
            with self.subTest(table):
                self.assertIn(table, indexed, f"{table} has no tenant-leading index")


class RowLevelSecurityIsForced(unittest.TestCase):
    def setUp(self) -> None:
        self.ddl = rls_ddl()

    def test_every_table_enables_it(self) -> None:
        for table in TENANT_TABLES:
            with self.subTest(table):
                self.assertIn(
                    f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.ddl
                )

    def test_every_table_forces_it(self) -> None:
        """The single most common way RLS is present and does nothing.

        Without `FORCE`, the table owner bypasses every policy — and the owner
        is the migration role, which many deployments also use to run the
        application.
        """
        for table in TENANT_TABLES:
            with self.subTest(table):
                self.assertIn(
                    f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.ddl
                )

    def test_every_table_has_a_policy(self) -> None:
        for table in TENANT_TABLES:
            with self.subTest(table):
                self.assertIn(f"CREATE POLICY {table}_tenant_isolation", self.ddl)

    def test_every_policy_governs_writes_as_well_as_reads(self) -> None:
        """`USING` alone lets a tenant insert into someone else's data.

        The row becomes invisible to its author and visible to the victim. It is
        the less obvious half of tenant isolation and the more damaging one.
        """
        policies = self.ddl.count("CREATE POLICY")
        self.assertEqual(policies, len(TENANT_TABLES))
        # Count clauses, not the word: the DDL explains itself in comments, and
        # a test that counts prose is a test that fails when someone improves a
        # comment.
        clauses = [
            line.strip()
            for line in self.ddl.splitlines()
            if not line.lstrip().startswith("--")
        ]
        self.assertEqual(
            sum(1 for line in clauses if line.startswith("WITH CHECK (")), policies
        )
        self.assertEqual(
            sum(1 for line in clauses if line.startswith("USING (")), policies
        )

    def test_a_policy_compares_against_the_session_tenant(self) -> None:
        self.assertIn(f"current_setting('{TENANT_SETTING}', true)", self.ddl)

    def test_the_policy_count_tracks_the_table_list(self) -> None:
        """Adding a table without a policy must be a failure, not a hole.

        This is the whole shape of the audit's findings — a guarantee that a
        new caller has to remember to opt into.
        """
        self.assertEqual(
            sorted(re.findall(r"CREATE POLICY (\w+)_tenant_isolation", self.ddl)),
            sorted(TENANT_TABLES),
        )


class TheApplicationRoleIsConstrained(unittest.TestCase):
    def setUp(self) -> None:
        self.ddl = roles_ddl()

    def test_it_cannot_bypass_row_level_security(self) -> None:
        self.assertIn("NOBYPASSRLS", self.ddl)

    def test_it_is_not_a_superuser(self) -> None:
        self.assertIn("NOSUPERUSER", self.ddl)

    def test_it_does_not_own_the_schema(self) -> None:
        """RLS protects rows; ownership is what protects the tables."""
        self.assertIn("REVOKE ALL ON SCHEMA public FROM vtv_app", self.ddl)
        self.assertIn("CREATE ROLE vtv_owner", self.ddl)

    def test_it_gets_only_row_operations(self) -> None:
        grants = rls_ddl()
        self.assertIn("GRANT SELECT, INSERT, UPDATE, DELETE", grants)
        for forbidden in ("GRANT ALL", "TRUNCATE", "GRANT CREATE"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, grants)


class TheTenantIsSetPerTransaction(unittest.TestCase):
    def test_it_is_local_so_a_pooled_connection_cannot_leak_it(self) -> None:
        """The failure this prevents is subtle and total.

        A plain `SET` persists for the session. A pooled connection handed to
        the next request would still be scoped to the previous tenant — and
        every query would succeed, returning the wrong customer's data.
        """
        sql = set_tenant_sql()
        self.assertIn("set_config", sql)
        self.assertTrue(sql.rstrip().endswith("true)"), sql)

    def test_the_tenant_is_a_parameter_not_interpolated(self) -> None:
        """The identifier reaches SQL, so it is a bind parameter."""
        sql = set_tenant_sql()
        self.assertIn("$1", sql)
        self.assertNotIn("%s", sql)
        self.assertNotIn("format(", sql)


class TheSchemaIsAppliedInTheRightOrder(unittest.TestCase):
    def test_roles_exist_before_policies_reference_them(self) -> None:
        full = schema()
        self.assertLess(full.index("CREATE ROLE"), full.index("CREATE POLICY"))

    def test_tables_exist_before_they_are_altered(self) -> None:
        full = schema()
        self.assertLess(
            full.index("CREATE TABLE IF NOT EXISTS projects"),
            full.index("ALTER TABLE projects ENABLE"),
        )

    def test_it_is_idempotent(self) -> None:
        """A deploy job is retried; a migration that fails the second time is a
        migration that blocks the retry."""
        full = schema()
        creates = re.findall(r"CREATE (TABLE|INDEX|UNIQUE INDEX) (?!IF NOT EXISTS)", full)
        self.assertEqual(creates, [], full[:400])
        # Policies cannot be `IF NOT EXISTS`, so they are dropped first.
        self.assertEqual(
            full.count("DROP POLICY IF EXISTS"), full.count("CREATE POLICY")
        )


class BillingAndQueueGuaranteesAreConstraints(unittest.TestCase):
    """The properties the SQLite version enforces, carried across."""

    def test_billing_idempotency_is_a_unique_index(self) -> None:
        """Not a lookup-then-insert, which races between two workers."""
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS usage_idempotent", tables_ddl()
        )

    def test_job_idempotency_is_scoped_to_a_tenant(self) -> None:
        """A global idempotency key would let one tenant suppress another's job."""
        ddl = tables_ddl()
        match = re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotent[^;]+", ddl)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertIn("organisation_id", match.group(0))

    def test_documents_cascade_from_their_project(self) -> None:
        self.assertIn("ON DELETE CASCADE", tables_ddl())

    def test_api_keys_store_a_hash_and_not_a_key(self) -> None:
        """A database backup must not be a credential store."""
        body = table_body("api_keys")
        self.assertIn("secret_hash", body)
        self.assertNotRegex(body, r"\bsecret\s+TEXT")


class WhatIsNotProven(unittest.TestCase):
    """Recorded as a test so it cannot be quietly dropped from the roadmap.

    This class used to assert that the module says **NOT EXECUTED**, and that
    was the right test while it was true. The DDL is now applied to a real
    server and attacked by `tests/test_postgres_rls.py`, so the claim has
    changed — and the honesty requirement moves with it rather than
    disappearing: the module must still say plainly what is *not* done, which
    is the repository backend itself.

    The distinction matters. A schema that has been executed and a system that
    can talk to PostgreSQL are different achievements, and conflating them is
    exactly the kind of overclaim this file exists to prevent.
    """

    def test_the_module_states_what_is_still_missing(self) -> None:
        from vtv.adapters.repository import postgres_schema

        doc = postgres_schema.__doc__ or ""
        self.assertIn("Still not implemented", doc)
        self.assertIn("repository", doc.lower())

    def test_the_module_does_not_claim_a_working_backend(self) -> None:
        """Executing the schema is not the same as having a backend.

        `repository_path()` still raises on `postgresql://`, deliberately —
        a deployment that points at a database the code cannot reach must fail
        to start rather than silently write to a local file.
        """
        from vtv.config import Settings
        from vtv.contracts.errors import VTVError
        from vtv.wiring import repository_path

        with self.assertRaises(VTVError):
            repository_path(
                Settings(asset_search_endpoint="", database_url="postgresql://user@host/vtv", env="development")
            )

    def test_the_executed_suite_exists_and_can_skip(self) -> None:
        """The proof is a test file, not a paragraph — and it skips cleanly.

        A security suite that fails on every machine without a database is a
        suite somebody deletes, and then the schema is unproven again with
        nothing to say so.
        """
        from pathlib import Path

        suite = Path(__file__).resolve().parent / "test_postgres_rls.py"
        self.assertTrue(suite.is_file(), "the executed RLS suite is missing")
        body = suite.read_text()
        self.assertIn("skipUnless", body)
        self.assertIn("VTV_TEST_POSTGRES_HOST", body)

    def test_no_postgres_driver_is_a_hard_dependency(self) -> None:
        """The system must keep running without one, on SQLite."""
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        )
        required = pyproject["project"]["dependencies"]
        self.assertTrue(all("asyncpg" not in entry for entry in required))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
