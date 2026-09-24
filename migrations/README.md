# Schema migrations

Migrations live in code, in `src/vtv/migrate.py`, not as loose `.sql` files in
this directory. This file explains the policy; the directory exists because the
Dockerfile copies it and because a migration that genuinely needs a large data
file (a backfill from an export, say) has somewhere to put it.

```bash
python -m vtv.migrate status     # what is applied, what is outstanding
python -m vtv.migrate upgrade    # apply everything outstanding
```

`deploy/docker-compose.yml` runs `upgrade` as a separate service that must
complete successfully before the API or any worker starts.

## Forward only

There are no down-migrations, and there will not be.

A down-migration is a promise to reverse a change users have already observed,
and it is usually a lie. Dropping a column does not restore the data that was in
it; reversing a backfill does not recover what the backfill overwrote. Systems
that carry down-migrations tend to have never run one in anger, which means the
rollback path is the least-tested code in the deployment at the exact moment it
matters most.

Recovery from a bad migration is: restore from backup, then write a new forward
migration. That procedure is testable, and it is the one an operator will
actually be able to follow at 3am.

## Idempotent

Every migration is safe to run twice, and safe to run concurrently by two
replicas starting at the same time.

Two mechanisms, deliberately overlapping. The ledger (`schema_migrations`)
records what has been applied, and the apply step takes `BEGIN IMMEDIATE` and
re-checks the ledger inside the transaction, so a race produces one winner and
one no-op rather than two applications. Independently, each migration's *body*
is written to tolerate having already happened — `ADD COLUMN` is guarded by a
`PRAGMA table_info` check, indexes use `IF NOT EXISTS`, backfills are `WHERE
column IS NULL`. Belt and braces, because the failure mode of getting this wrong
is a half-migrated production database.

## Ordered and checksummed

The ledger stores a SHA-256 of each migration's source. Editing a migration
after it has been applied somewhere makes `pending()` raise rather than silently
skip it, because "the file says X and the database has Y" is the failure that
costs a weekend.

**So: never edit an applied migration.** Add a new one.

## One transaction per migration

Not one transaction for the whole run. If migration 4 of 6 fails, migrations 1–3
stay applied and recorded, and a retry resumes at 4. A single wrapping
transaction would roll all of them back, which sounds safer and is not: it turns
every retry into a full re-run, and on a large backfill that is the difference
between a two-minute recovery and an outage.

## Writing one

Add a function and an entry in `MIGRATIONS`, in `src/vtv/migrate.py`:

```python
def _0004_short_imperative_name(connection: sqlite3.Connection) -> None:
    """Why this was needed — not what the SQL says."""
    ...

MIGRATIONS = (
    ...,
    Migration(
        version=4,
        name="short_imperative_name",
        target="repository",     # or "queue" / "shared"
        apply=_0004_short_imperative_name,
        reason="the defect or requirement this closes",
    ),
)
```

Rules:

* **Never rename or renumber an existing migration.** The ledger keys on version.
* **Additive first.** Add a column, backfill it, deploy code that reads it, and
  only then — in a later release — stop writing the old one. A migration that
  drops a column the currently-running code still selects is an outage during
  the rolling deploy, not after it.
* **Backfill, do not delete.** `_0001` quarantines rows it cannot attribute to a
  reserved tenant rather than removing them. Losing a customer's project to a
  migration is worse than parking it somewhere only an operator can reach.
* **Assume it runs twice.**

## Targets

Three SQLite databases, each with its own ledger:

| Target | File | Holds |
|---|---|---|
| `repository` | `var/vtv.db` | projects, documents |
| `queue` | `var/queue.db` | durable jobs |
| `shared` | `var/shared.db` | rate-limit buckets |

Paths come from `vtv.wiring`, so the API, the worker and the migrator cannot
disagree about where a database is — which they would eventually do if each
parsed `VTV_DATABASE_URL` itself.

## PostgreSQL

Not yet. P1-5 in `docs/REMEDIATION.md` tracks it, and the reason it is P1 rather
than P0 is that the correctness properties above (atomic claim, `BEGIN
IMMEDIATE` for read-modify-write, single-statement token spend) are already
written the way Postgres wants them. The `Migration` interface takes a DB-API
connection and does not assume SQLite anywhere except in the three migration
bodies' `PRAGMA table_info` guards, which become `information_schema` queries.
