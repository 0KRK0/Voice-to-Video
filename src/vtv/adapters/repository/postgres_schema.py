"""P1-5 — the PostgreSQL schema, with tenant isolation the database enforces.

**STATUS: EXECUTED.** This DDL is applied to a real PostgreSQL server by
`tests/test_postgres_rls.py`, which then attacks it as the application role:
reading another tenant's row by primary key, inserting one attributed to
somebody else, updating and deleting across the boundary, querying with no
tenant set, relying on a tenant left over from a previous transaction, and
trying to disable the policies outright. Every claim below is a test result.

That test was worth writing. The structural tests that preceded it read this
file as *text* — NOT NULL tenant column, RLS enabled, FORCE set, a policy per
table — and all four were true while the application role could not insert a
single row into any table with a serial key, because a table grant says nothing
about the sequence behind it. Text was not evidence.

**Still not implemented: the repository itself.** There is no `asyncpg` in this
environment and no package index to fetch one from, so nothing in the running
application talks to PostgreSQL — `repository_path()` raises on any URL scheme
but `sqlite:///`, deliberately, rather than quietly writing to a local file.
What is proven here is the schema and its isolation properties, not a backend.
`docs/REMEDIATION.md` carries the remainder.

## Why row-level security, when the application already scopes every query

Because "already scopes every query" is a claim about all present and future
code, and the audit found six places where exactly that kind of claim was false.
The P0-3 defect was precisely this: `organisation_id` was nullable, one query
forgot the scope, and the API read a null owner as "unowned, therefore yours".

Application-level scoping and RLS fail in uncorrelated ways. A forgotten `WHERE`
clause is caught by RLS; a misconfigured RLS policy is caught by the
application's own scoping. Requiring both to be wrong simultaneously is the
entire value, and it costs one `SET LOCAL` per transaction.

Three properties make it real rather than decorative:

* **`FORCE ROW LEVEL SECURITY`.** Without it, the table's *owner* bypasses every
  policy — and the migration role is usually the owner, so a deployment that
  runs the application as its migration user has RLS that does nothing. This is
  the single most common way RLS is present and useless.
* **The application role is not the owner and is not superuser.** Stated in
  `deploy/README.md` and asserted in the role DDL below.
* **The tenant comes from a `SET LOCAL`,** which is transaction-scoped. A
  connection returned to the pool cannot leak the previous request's tenant,
  because `LOCAL` ends with the transaction rather than the session.

## The nullable-tenant lesson, applied at the database

`organisation_id` is `NOT NULL` on every tenant table. A nullable tenant column
is not a schema convenience; it is a row that no policy matches and every
`WHERE organisation_id = $1` misses — invisible rather than protected. The
migration in `vtv.migrate` backfills before this constraint can be applied,
which is the order those two have to happen in.
"""

from __future__ import annotations

#: Every table that holds one tenant's data. Kept as a list rather than
#: discovered, so that adding a table and forgetting its policy is a test
#: failure rather than a silent hole — the class of defect this whole
#: remediation exists to remove.
TENANT_TABLES: tuple[str, ...] = (
    "projects",
    "documents",
    "jobs",
    "usage_records",
    "usage_reservations",
    "audit_log",
    "api_keys",
    "memberships",
)

#: The session variable a request sets before touching anything. Namespaced so
#: it cannot collide with another extension's settings.
TENANT_SETTING = "vtv.organisation_id"


def roles_ddl(*, application_role: str = "vtv_app") -> str:
    """Roles, and the ownership split that makes RLS mean anything.

    The application role deliberately owns nothing. `FORCE ROW LEVEL SECURITY`
    covers the owner too, but relying on it alone means a single `ALTER TABLE
    … NO FORCE` — or a table created by the application at runtime — silently
    removes the boundary.
    """
    return f"""
-- The migration role owns the schema. It is not the role the application uses.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vtv_owner') THEN
        CREATE ROLE vtv_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{application_role}') THEN
        CREATE ROLE {application_role} LOGIN;
    END IF;
END
$$;

-- No DDL, no truncate, no bypass. An application that cannot drop a table
-- cannot be talked into dropping a table.
REVOKE ALL ON SCHEMA public FROM {application_role};
GRANT USAGE ON SCHEMA public TO {application_role};
ALTER ROLE {application_role} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
"""


def tables_ddl() -> str:
    """The tables. Tenant columns are NOT NULL, without exception."""
    return """
CREATE TABLE IF NOT EXISTS projects (
    project_id      TEXT PRIMARY KEY,
    -- NOT NULL. A nullable tenant is a row no policy matches and every scoped
    -- query misses: invisible rather than protected. This is P0-3, at the
    -- layer that can actually enforce it.
    organisation_id TEXT NOT NULL,
    owner_id        TEXT,
    persistence     TEXT NOT NULL,
    status          TEXT NOT NULL,
    outcome         TEXT,
    expires_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_tenant
    ON projects (organisation_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS projects_owner
    ON projects (organisation_id, owner_id, updated_at DESC);
-- Partial: only rows that can expire are worth scanning for expiry.
CREATE INDEX IF NOT EXISTS projects_expiry
    ON projects (organisation_id, expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS documents (
    sequence        BIGSERIAL PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
    -- Denormalised from the project on purpose. An RLS policy that had to join
    -- to `projects` to find the tenant would run that join on every row of
    -- every query, and a policy nobody can afford is a policy that gets turned
    -- off.
    organisation_id TEXT NOT NULL,
    kind            TEXT NOT NULL,
    document_id     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_lookup
    ON documents (organisation_id, project_id, kind, sequence DESC);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    kind            TEXT NOT NULL,
    state           TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 5,
    attempt         INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    idempotency_key TEXT,
    project_id      TEXT,
    claimed_by      TEXT,
    heartbeat_at    DOUBLE PRECISION,
    available_at    DOUBLE PRECISION NOT NULL,
    created_at      DOUBLE PRECISION NOT NULL,
    payload         JSONB NOT NULL,
    error           JSONB
);
-- The claim query's index. `state, priority, available_at` in that order
-- because the claim filters on state first and orders by the rest.
CREATE INDEX IF NOT EXISTS jobs_claim
    ON jobs (state, priority DESC, available_at);
-- Idempotency is a *unique* constraint, not a lookup: two enqueues of the same
-- work must collide in the database rather than race in the application.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotent
    ON jobs (organisation_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS usage_records (
    usage_id        BIGSERIAL PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    kind            TEXT NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    period          TEXT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS usage_totals
    ON usage_records (organisation_id, kind, period);
-- The billing idempotency guarantee, as a constraint rather than a convention.
CREATE UNIQUE INDEX IF NOT EXISTS usage_idempotent
    ON usage_records (organisation_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS usage_reservations (
    reservation_id  TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    kind            TEXT NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reservations_open
    ON usage_reservations (organisation_id, kind);
CREATE INDEX IF NOT EXISTS reservations_expiry
    ON usage_reservations (expires_at);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id        BIGSERIAL PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    action          TEXT NOT NULL,
    principal       TEXT NOT NULL,
    target          TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address      INET,
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_by_tenant
    ON audit_log (organisation_id, at DESC);

CREATE TABLE IF NOT EXISTS memberships (
    organisation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organisation_id, user_id)
);

CREATE TABLE IF NOT EXISTS api_keys (
    api_key_id      TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,
    -- The hash, never the key. A database backup must not be a credential
    -- store.
    secret_hash     TEXT NOT NULL,
    scopes          JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (organisation_id);
"""


def rls_ddl(*, application_role: str = "vtv_app") -> str:
    """Enable, force, and write one policy per tenant table.

    The `FORCE` is the load-bearing word. Without it the table owner bypasses
    every policy, and the owner is usually the role that ran the migration — so
    a deployment that reuses that role for the application has row-level
    security that is present, documented, and does nothing.
    """
    blocks: list[str] = []
    for table in TENANT_TABLES:
        blocks.append(
            f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
-- Applies to the owner too. See the module docstring.
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};
CREATE POLICY {table}_tenant_isolation ON {table}
    FOR ALL
    TO {application_role}
    -- USING governs what is visible; WITH CHECK governs what may be written.
    -- Both, always: USING alone lets a tenant INSERT a row attributed to
    -- someone else, which they then cannot see — and which shows up in the
    -- other tenant's data.
    USING (organisation_id = current_setting('{TENANT_SETTING}', true))
    WITH CHECK (organisation_id = current_setting('{TENANT_SETTING}', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {application_role};
"""
        )

    # Sequences, which the table grant does not cover.
    #
    # Found by running this DDL against a real server for the first time: four
    # of these tables have a `BIGSERIAL` key, and `GRANT ... ON <table>` says
    # nothing about the sequence behind it. Every insert into `documents`,
    # `usage_records` or `audit_log` failed with *permission denied for
    # sequence documents_sequence_seq* — which is to say the application could
    # authenticate, pass every policy, and still not write a row.
    #
    # No structural test could have caught it. The DDL had a NOT NULL tenant
    # column, RLS enabled, FORCE set and a correct policy on every table; it
    # was, as text, exactly right. This is the reason the executed test exists.
    #
    # `USAGE`, not `ALL`: the application needs `nextval`, and nothing needs
    # `setval` — which would let a caller rewind a sequence and collide with
    # rows that already exist.
    blocks.append(
        f"""
DO $$
DECLARE
    seq TEXT;
BEGIN
    FOR seq IN
        SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'
    LOOP
        EXECUTE format('GRANT USAGE ON SEQUENCE %I TO {application_role}', seq);
    END LOOP;
END
$$;
"""
    )
    return "\n".join(blocks)


def set_tenant_sql() -> str:
    """What every transaction runs before it touches a row.

    `SET LOCAL`, not `SET`. `LOCAL` is scoped to the transaction, so a
    connection handed back to the pool carries nothing forward; a plain `SET`
    would leave the previous request's tenant on the connection, which is a
    cross-tenant read waiting for a pool to reuse a connection.

    Parameterised, because the tenant identifier reaches SQL. `set_config` takes
    a value rather than being interpolated into DDL, which is the only form of
    this that is not an injection point.
    """
    return f"SELECT set_config('{TENANT_SETTING}', $1, true)"


def schema() -> str:
    """The whole thing, in the order it must be applied."""
    return "\n".join([roles_ddl(), tables_ddl(), rls_ddl()])


__all__ = [
    "TENANT_SETTING",
    "TENANT_TABLES",
    "rls_ddl",
    "roles_ddl",
    "schema",
    "set_tenant_sql",
    "tables_ddl",
]
