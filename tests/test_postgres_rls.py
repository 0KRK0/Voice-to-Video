"""P1-5 — the PostgreSQL schema, executed.

`adapters/repository/postgres_schema.py` carried the words **DESIGNED AND
STRUCTURALLY TESTED. NOT EXECUTED.** for as long as this repository has had it,
and the structural tests beside it read the DDL as *text*: every tenant table
has a `NOT NULL organisation_id`, RLS is enabled, `FORCE` is present, a policy
exists. All true, and none of it proves the policies isolate anything. A policy
can be present, forced, syntactically perfect, and let every tenant read every
row — `USING (true)` satisfies every one of those text assertions.

This module runs the DDL against a real server and then attacks it, as the
application role, the way a compromised request handler would:

* read another tenant's row by primary key
* insert a row attributed to another tenant
* update and delete another tenant's rows
* query with no tenant set at all
* rely on a tenant set in a previous transaction on the same connection
* turn RLS off, drop the policy, write a permissive one
* create a table outside the policy set and use it as an escape hatch

Each is a specific claim the module docstring makes. Each is asserted here
against a live server, so the claim is a result rather than a design intention.

## Running it

Skipped unless a server is reachable. Point it at one:

    VTV_TEST_POSTGRES_HOST=/tmp/pgsock VTV_TEST_POSTGRES_PORT=5433 \\
      VTV_TEST_POSTGRES_SUPERUSER=postgres python -m unittest tests.test_postgres_rls

Skipped rather than failed when absent, and the skip message says exactly what
is missing — a test that fails on every machine without a database is a test
people delete.

## Why `psql` and not a driver

There is no `psycopg` in this environment and no package index to fetch one
from. Driving `psql` is not a workaround here so much as the more honest test:
it applies the DDL exactly as an operator would, and the thing under test is the
DDL, not a Python binding to it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
import uuid

from vtv.adapters.repository.postgres_schema import (
    TENANT_TABLES,
    rls_ddl,
    roles_ddl,
    tables_ddl,
)

HOST = os.environ.get("VTV_TEST_POSTGRES_HOST", "")
PORT = os.environ.get("VTV_TEST_POSTGRES_PORT", "5432")
SUPERUSER = os.environ.get("VTV_TEST_POSTGRES_SUPERUSER", "postgres")

#: Debian and Ubuntu keep the binaries out of `PATH`. Look there too, so a
#: developer does not have to know that to run this.
_SEARCH = ["", "/usr/lib/postgresql/16/bin/", "/usr/lib/postgresql/15/bin/"]


def _binary(name: str) -> str | None:
    for prefix in _SEARCH:
        found = shutil.which(prefix + name) if not prefix else None
        candidate = found or (prefix + name if prefix else None)
        if found:
            return found
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


PSQL = _binary("psql")


def _reachable() -> bool:
    if not HOST or PSQL is None:
        return False
    ready = _binary("pg_isready")
    if ready is None:
        return False
    return (
        subprocess.run(
            [ready, "-h", HOST, "-p", PORT],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


REACHABLE = _reachable()
WHY = (
    "no PostgreSQL reachable — set VTV_TEST_POSTGRES_HOST (and _PORT, "
    "_SUPERUSER) to run the row-level-security suite"
)


class Result:
    """One `psql` invocation: the exit code and both streams."""

    def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.code = completed.returncode
        self.out = completed.stdout.strip()
        self.err = completed.stderr.strip()

    @property
    def failed(self) -> bool:
        return self.code != 0

    def __repr__(self) -> str:  # pragma: no cover - only read on failure
        return f"Result(code={self.code}, out={self.out!r}, err={self.err!r})"


@unittest.skipUnless(REACHABLE, WHY)
class RowLevelSecurityIsolatesTenants(unittest.TestCase):
    """The schema, applied and then attacked."""

    database = ""
    owner = ""
    app = ""

    @classmethod
    def setUpClass(cls) -> None:
        # A unique database and unique roles per run, so this can run beside
        # anything else on a shared server and clean up after itself.
        suffix = uuid.uuid4().hex[:10]
        cls.database = f"vtv_rls_{suffix}"
        cls.owner = f"vtv_owner_{suffix}"
        cls.app = f"vtv_app_{suffix}"

        cls._admin("postgres", f'CREATE DATABASE "{cls.database}"')

        # The roles, from the module's own DDL. Only the owner name is
        # substituted: the application role is already a parameter, and
        # replacing it a second time turns `vtv_app_ab12` into
        # `vtv_app_ab12_ab12` — which then fails to log in, with sixteen tests
        # blaming row-level security for a fixture bug.
        roles = roles_ddl(application_role=cls.app).replace("vtv_owner", cls.owner)
        cls._admin(cls.database, roles)
        cls._admin(
            cls.database,
            f'GRANT CREATE, USAGE ON SCHEMA public TO "{cls.owner}"',
        )

        # The documented deployment: the *migration* role owns the schema, and
        # the application role owns nothing. `SET ROLE` is what makes the
        # objects come out owned by the owner rather than by whoever ran the
        # migration — and ownership is what `FORCE ROW LEVEL SECURITY` and the
        # "cannot ALTER the table" guarantees both hang off.
        schema = (
            f'SET ROLE "{cls.owner}";\n'
            + tables_ddl()
            + "\n"
            + rls_ddl(application_role=cls.app)
        )
        cls._admin(cls.database, schema)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._admin("postgres", f'DROP DATABASE IF EXISTS "{cls.database}"')
        cls._admin("postgres", f'DROP ROLE IF EXISTS "{cls.app}"')
        cls._admin("postgres", f'DROP ROLE IF EXISTS "{cls.owner}"')

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _run(user: str, database: str, sql: str, stop_on_error: bool) -> Result:
        assert PSQL is not None
        command = [
            PSQL,
            "-h",
            HOST,
            "-p",
            PORT,
            "-U",
            user,
            "-d",
            database,
            "-t",
            "-A",
            "-q",
        ]
        if stop_on_error:
            command += ["-v", "ON_ERROR_STOP=1"]
        command += ["-c", sql] if "\n" not in sql.strip() else ["-f", "-"]

        return Result(
            subprocess.run(
                command,
                input=None if command[-1] != "-" else sql,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        )

    @classmethod
    def _admin(cls, database: str, sql: str) -> Result:
        result = cls._run(SUPERUSER, database, sql, stop_on_error=True)
        if result.failed:  # pragma: no cover - a broken fixture, not a finding
            raise RuntimeError(f"setup failed: {result!r}")
        return result

    def app_sql(self, sql: str, *, stop_on_error: bool = True) -> Result:
        """Run SQL as the application role — the role a request handler uses."""
        return self._run(self.app, self.database, sql, stop_on_error=stop_on_error)

    def as_tenant(self, organisation_id: str, sql: str, **kwargs: bool) -> Result:
        """One transaction, scoped to a tenant exactly as the API would scope it."""
        return self.app_sql(
            "BEGIN;\n"
            f"SELECT set_config('vtv.organisation_id', '{organisation_id}', true);\n"
            f"{sql}\n"
            "COMMIT;",
            **kwargs,
        )

    def seed(self) -> tuple[str, str]:
        """One project for each of two tenants. Returns their ids."""
        a = f"org_a{uuid.uuid4().hex[:18]}"
        b = f"org_b{uuid.uuid4().hex[:18]}"
        for org, project in ((a, "prj_a"), (b, "prj_b")):
            result = self.as_tenant(
                org,
                "INSERT INTO projects "
                "(project_id, organisation_id, persistence, status, payload) "
                f"VALUES ('{project}_{org[-6:]}', '{org}', 'project', 'ready', "
                "'{}'::jsonb);",
            )
            self.assertFalse(result.failed, result)
        return a, b

    # -- the schema itself ------------------------------------------------

    def test_every_tenant_table_exists_with_rls_enabled_and_forced(self) -> None:
        """The structural claims, checked against `pg_class` rather than text.

        `FORCE` is the load-bearing one. Without it the table owner bypasses
        every policy, and the owner is usually the role that ran the migration
        — so a deployment that reuses that role for the application has
        row-level security that is present, documented, and does nothing.
        """
        result = self.app_sql(
            "SELECT relname || ':' || relrowsecurity || ':' || relforcerowsecurity "
            "FROM pg_class WHERE relkind = 'r' "
            "AND relnamespace = 'public'::regnamespace ORDER BY relname"
        )
        # Concatenating a boolean casts it, so these read `true`/`false` rather
        # than the `t`/`f` a bare column would give.
        self.assertFalse(result.failed, result)
        state = dict(
            (line.split(":", 1)[0], line.split(":", 1)[1])
            for line in result.out.splitlines()
            if line
        )
        for table in TENANT_TABLES:
            with self.subTest(table=table):
                self.assertIn(table, state, f"{table} was never created")
                self.assertEqual(
                    state[table],
                    "true:true",
                    f"{table} is not both enabled and FORCEd",
                )

    def test_the_application_role_does_not_own_the_tables(self) -> None:
        """Ownership is what every other guarantee here hangs off."""
        result = self.app_sql(
            "SELECT count(*) FROM pg_class WHERE relkind = 'r' "
            "AND relnamespace = 'public'::regnamespace "
            f"AND pg_get_userbyid(relowner) = '{self.app}'"
        )
        self.assertEqual(result.out, "0", result)

    def test_the_application_role_cannot_bypass_rls_or_create_roles(self) -> None:
        result = self.app_sql(
            "SELECT rolbypassrls::text || ':' || rolsuper::text || ':' "
            f"|| rolcreaterole::text FROM pg_roles WHERE rolname = '{self.app}'"
        )
        self.assertEqual(result.out, "false:false:false", result)

    def test_organisation_id_is_not_null_on_every_tenant_table(self) -> None:
        """A nullable tenant is a row no policy matches and every scope misses.

        Invisible rather than protected — which is P0-3, at the layer that can
        actually enforce it.
        """
        result = self.app_sql(
            "SELECT table_name || ':' || is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'organisation_id' "
            "ORDER BY table_name"
        )
        self.assertFalse(result.failed, result)
        rows = [line for line in result.out.splitlines() if line]
        self.assertEqual(len(rows), len(TENANT_TABLES))
        for row in rows:
            with self.subTest(row=row):
                self.assertTrue(row.endswith(":NO"), f"{row} allows a null tenant")

    # -- the attacks ------------------------------------------------------

    def test_a_tenant_sees_only_its_own_rows(self) -> None:
        a, _b = self.seed()
        result = self.as_tenant(a, "SELECT count(*) FROM projects;")
        self.assertIn("1", result.out.splitlines(), result)

    def test_another_tenants_row_is_invisible_even_by_primary_key(self) -> None:
        """The direct-object attack: guess the id, ask for it by name."""
        a, b = self.seed()
        theirs = f"prj_b_{b[-6:]}"
        result = self.as_tenant(
            a, f"SELECT count(*) FROM projects WHERE project_id = '{theirs}';"
        )
        self.assertIn("0", result.out.splitlines(), result)

    def test_a_row_cannot_be_inserted_on_another_tenants_behalf(self) -> None:
        """`WITH CHECK`, not just `USING`.

        `USING` alone governs what is *visible*. A policy with only `USING`
        lets a tenant insert a row attributed to somebody else — which they
        then cannot see, and which turns up in the other tenant's data.
        """
        a, b = self.seed()
        result = self.as_tenant(
            a,
            "INSERT INTO projects "
            "(project_id, organisation_id, persistence, status, payload) "
            f"VALUES ('prj_smuggled', '{b}', 'project', 'ready', '{{}}'::jsonb);",
            stop_on_error=True,
        )
        self.assertTrue(result.failed, "the insert should have been refused")
        self.assertIn("row-level security", result.err.lower())

        # And nothing landed.
        check = self.as_tenant(
            b, "SELECT count(*) FROM projects WHERE project_id = 'prj_smuggled';"
        )
        self.assertIn("0", check.out.splitlines(), check)

    def test_another_tenants_row_cannot_be_updated(self) -> None:
        a, b = self.seed()
        theirs = f"prj_b_{b[-6:]}"
        self.as_tenant(
            a, f"UPDATE projects SET status = 'deleted' WHERE project_id = '{theirs}';"
        )
        # Silently affects nothing, which is the correct behaviour: the row is
        # not merely protected, it is not there.
        survived = self.as_tenant(
            b, f"SELECT status FROM projects WHERE project_id = '{theirs}';"
        )
        self.assertIn("ready", survived.out, survived)

    def test_another_tenants_row_cannot_be_deleted(self) -> None:
        a, b = self.seed()
        theirs = f"prj_b_{b[-6:]}"
        self.as_tenant(a, f"DELETE FROM projects WHERE project_id = '{theirs}';")
        survived = self.as_tenant(
            b, f"SELECT count(*) FROM projects WHERE project_id = '{theirs}';"
        )
        self.assertIn("1", survived.out.splitlines(), survived)

    def test_a_query_with_no_tenant_set_sees_nothing(self) -> None:
        """Fail closed.

        A handler that forgets to scope its transaction gets an empty result,
        not the whole table. This is the case that makes RLS worth its cost:
        the application's own scoping is a claim about all present and future
        code, and this is what holds when that claim is false.
        """
        self.seed()
        result = self.app_sql("SELECT count(*) FROM projects;")
        self.assertEqual(result.out, "0", result)

    def test_a_tenant_does_not_leak_into_the_next_transaction(self) -> None:
        """`SET LOCAL`, not `SET`.

        A connection handed back to the pool must carry nothing forward. A
        plain `SET` leaves the previous request's tenant on the connection,
        which is a cross-tenant read waiting for a pool to reuse it.
        """
        a, _b = self.seed()
        result = self.app_sql(
            "BEGIN;\n"
            f"SELECT set_config('vtv.organisation_id', '{a}', true);\n"
            "COMMIT;\n"
            "SELECT count(*) FROM projects;"
        )
        self.assertEqual(result.out.splitlines()[-1], "0", result)

    def test_the_application_role_cannot_disable_row_level_security(self) -> None:
        for statement in (
            "ALTER TABLE projects NO FORCE ROW LEVEL SECURITY",
            "ALTER TABLE projects DISABLE ROW LEVEL SECURITY",
            "DROP POLICY projects_tenant_isolation ON projects",
        ):
            with self.subTest(statement=statement):
                result = self.app_sql(statement, stop_on_error=True)
                self.assertTrue(result.failed, f"{statement} was allowed")
                self.assertIn("must be owner", result.err.lower())

    def test_the_application_role_cannot_write_a_permissive_policy(self) -> None:
        result = self.app_sql(
            f'CREATE POLICY sneaky ON projects FOR ALL TO "{self.app}" USING (true)',
            stop_on_error=True,
        )
        self.assertTrue(result.failed, "a permissive policy was allowed")

    def test_the_application_role_cannot_create_a_table_to_escape_into(self) -> None:
        """No DDL. An application that cannot create a table cannot create one
        outside the policy set and copy rows into it."""
        result = self.app_sql("CREATE TABLE escape_hatch (x int)", stop_on_error=True)
        self.assertTrue(result.failed, "the application role could create a table")
        self.assertIn("permission denied", result.err.lower())

    def test_the_idempotency_constraints_are_unique_indexes_not_conventions(self) -> None:
        """Two enqueues of the same work must collide in the database.

        A lookup-then-insert races; a unique index does not. The same applies
        to usage records, where the race is a double charge.
        """
        result = self.app_sql(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname IN ('jobs_idempotent', 'usage_idempotent') "
            "ORDER BY indexname"
        )
        self.assertEqual(
            sorted(line for line in result.out.splitlines() if line),
            ["jobs_idempotent", "usage_idempotent"],
            result,
        )

    def test_the_application_role_can_insert_into_serial_keyed_tables(self) -> None:
        """The defect this whole module exists to have caught.

        Four tables have a `BIGSERIAL` key, and `GRANT ... ON <table>` says
        nothing about the sequence behind it. Before the sequence grant, every
        insert into `documents`, `usage_records` or `audit_log` failed with
        *permission denied for sequence documents_sequence_seq* — while the DDL
        remained, as text, exactly correct: NOT NULL tenant column, RLS
        enabled, FORCE set, a right policy on every table.

        `USAGE`, not `ALL`: `nextval` is needed, `setval` is not, and `setval`
        would let a caller rewind a sequence into rows that already exist.
        """
        a, _b = self.seed()
        project = f"prj_a_{a[-6:]}"
        for table, columns, values in (
            (
                "documents",
                "(project_id, organisation_id, kind, document_id, payload)",
                f"('{project}', '{a}', 'script', 'script', '{{}}'::jsonb)",
            ),
            (
                "usage_records",
                "(organisation_id, kind, quantity, period)",
                f"('{a}', 'render_seconds', 1.0, '2026-08')",
            ),
            (
                "audit_log",
                "(organisation_id, action, principal)",
                f"('{a}', 'project.created', 'key:test')",
            ),
        ):
            with self.subTest(table=table):
                result = self.as_tenant(
                    a, f"INSERT INTO {table} {columns} VALUES {values};"
                )
                self.assertFalse(result.failed, result)

        granted = self.app_sql(
            "SELECT count(*) FROM information_schema.role_usage_grants "
            f"WHERE grantee = '{self.app}' AND object_type = 'SEQUENCE'"
        )
        self.assertNotEqual(granted.out, "0", "no sequence grant was issued")

    def test_the_application_role_cannot_rewind_a_sequence(self) -> None:
        """`USAGE` grants `nextval` and `currval`, never `setval`."""
        result = self.app_sql(
            "SELECT setval('documents_sequence_seq', 1)", stop_on_error=True
        )
        self.assertTrue(result.failed, "the application role could rewind a sequence")

    def test_documents_cascade_from_their_project(self) -> None:
        """Deleting a project must not leave its documents behind.

        Orphaned documents are a retention problem — bytes that no deletion
        path reaches, belonging to a tenant who asked to be forgotten.
        """
        a, _b = self.seed()
        project = f"prj_a_{a[-6:]}"
        self.as_tenant(
            a,
            "INSERT INTO documents "
            "(project_id, organisation_id, kind, document_id, payload) "
            f"VALUES ('{project}', '{a}', 'script', 'script', '{{}}'::jsonb);",
        )
        before = self.as_tenant(a, "SELECT count(*) FROM documents;")
        self.assertIn("1", before.out.splitlines(), before)

        self.as_tenant(a, f"DELETE FROM projects WHERE project_id = '{project}';")
        after = self.as_tenant(a, "SELECT count(*) FROM documents;")
        self.assertIn("0", after.out.splitlines(), after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
