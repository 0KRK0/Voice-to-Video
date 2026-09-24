# Voice-to-Video — state of the system after remediation

**Date:** 2026-08-13
**Baseline:** `docs/AUDIT_2026-08-13.md` — independent static audit, 31/100
**Scope of this report:** everything that changed since that audit, what it
proves, and what it still does not.

---

## The one-paragraph version

The audit's finding was not that the code was bad. It was that the system had
**six guarantees implemented as helper functions that callers had to remember to
call** — grounding, tenant scoping, path safety, upload inspection, quota
accounting, durability — and in every case at least one caller had not
remembered. Correct libraries, comprehensive tests, and a request path that
reached none of them. Every P0 fix below is the same move: convert a guarantee
into a chokepoint the caller cannot route around, and then prove the routing
around is impossible rather than merely discouraged.

All eleven P0 findings are closed. Six of seven P1 findings are closed. The
seventh — PostgreSQL with row-level security — is written and structurally
tested but has never been executed, because there is no server, no driver and no
package index in this environment. That is the system's remaining scaling
ceiling, and it is stated in three places rather than buried.

---

## Verified state

Every number here was produced by running the thing, in this environment, on the
current tree.

| | |
| --- | --- |
| Tests | **740**, all passing |
| Lint | `ruff check .` clean |
| Types | `mypy --strict` clean across **122** source files |
| Schema drift | 18 exported JSON Schemas, checked against the code |
| Evaluation corpus | 10 cases, all passing |
| Source + tests | ~43,000 lines |

`make check` runs lint, types, schema-drift, the full suite and the evaluation
harness. It is the gate.

---

## P0 — all eleven closed

Each row names the **chokepoint**, because "we fixed it" and "it cannot happen
again" are different claims and only the second one is worth making.

| ID | The defect | The chokepoint that closes it |
| --- | --- | --- |
| P0-1 | An LLM could emit a fabricated chart and it rendered unchecked — `check_spec` guarded only the rule-based director, which cannot hallucinate | `PlanGate`, applied in the orchestrator. `Pipeline.plan_gate` has **no default**, so a pipeline cannot be constructed without one |
| P0-2 | `/internal/sweep` deleted every tenant's projects and objects, with no `guard()` at all | The route is `/v1/retention/sweep`, requires `PROJECT_DELETE`, is tenant-scoped and audited. A path prefix is not an access control |
| P0-3 | `Project.organisation_id` was nullable; the API read a null owner as "unowned, therefore yours" | Required contract field, and a repository **query parameter** — the scope is inside the `SELECT`, not in an `if` after it |
| P0-4 | Rendering ran in the API's event loop as an asyncio task | `vtv.worker` consumes; the API registers **zero** handlers, so it cannot execute a job even if asked |
| P0-5 | A 1,035-line durable queue with 27 tests that nothing used | `DurableJobQueue` on a shared file is the only queue wired |
| P0-6 | Rate-limit buckets and quota reservations were Python dicts — N replicas enforced N× the limit | `SharedStore`; the limiter holds no correctness-critical state. Token spend is one atomic `UPDATE` |
| P0-7 | Reservation expiry existed and never ran | `Worker._maintenance` runs it every 30s; reservations live in one table |
| P0-8 | Finished videos lived in `ApiState.results`, a process-local dict | Artifacts are read from persisted documents. `ApiState.results` is deleted |
| P0-9 | `verify_password` raised `NotImplementedError` — no human could log in | PBKDF2-HMAC-SHA256 at 600k iterations, with transparent Argon2id upgrade when the library is present |
| P0-10 | Every request JSON-parsed every organisation in the deployment | Denormalised indexed `active` / `plan` columns. Request cost no longer grows with customer count |
| P0-11 | No Dockerfile, no migrations, no lockfile of any kind | Multi-stage image, forward-only migrations with a checksum ledger, a compose topology that runs two API replicas and two workers |

### Defects found *by* the P0 work

These were found by tests written to prove a fix, not by inspection. They are
the argument for writing the tests that way.

| Found by | Defect | Fix |
| --- | --- | --- |
| running `upgrade()` for real | Migration 2 targeted the `repository` database; `organisations` lives in `directory.db`. **`migrate upgrade` failed on every fresh deployment** | Retargeted; `migrators()` now derives from `wiring.DATABASES` |
| the same | The migrator knew about **three of six** databases | One table in `wiring.py`, used by both the assembly and the migrator |
| baseline work | Running today's schema over yesterday's database fails on `CREATE INDEX` | `baseline()` runs only on an empty database |
| `tests/test_deployment.py` | Compose started PostgreSQL and Redis; the code implements neither, and `repository_path()` **silently fell back to a private SQLite file per replica** | Services removed; `repository_path()` raises on any scheme it does not implement |
| the same | The Dockerfile installed from `requirements.lock`, which was never generated — **the image could not have built** | `requirements.txt` with exact pins; the hash gap is stated in `requirements.lock.md` rather than faked |
| the same | `LocalStorageProvider` generated a **per-process** URL signing key — behind a load balancer, downloads fail about half the time | `VTV_SIGNING_KEY`; production refuses to start without it |
| the same | `.env.example` documented four variables nothing has ever read; three real settings could not be set at all | Rewritten; a test asserts every documented key is one `Settings.from_env` reads |
| the same | CORS declared `GET, POST` while `/v1/api-keys/{id}` serves `DELETE` | `DELETE` added |

---

## P1 — six of seven closed

| ID | Status | What changed |
| --- | --- | --- |
| **P1-1** | RESOLVED | The Visual Bible had **no effect on any pixel**. Composition now reuses a bound entity's stored object instead of searching again, the colour lock travels on the `Timeline`, and the worker reloads the approved Bible before every render — so a binding a user *locked* survives |
| **P1-2** | RESOLVED | `tenant_key()` existed with **zero callers**. The storage adapter now refuses any key outside `orgs/<organisation>/`, and `organisation_id` is required on every project-scoped document |
| **P1-3** | RESOLVED | Bomb and mismatch checks lived only in `api/app.py`. They now run in `IngestionService`, and the parse runs in a child process with a wall-clock kill and `RLIMIT_AS`/`RLIMIT_CPU` |
| **P1-4** | RESOLVED | `RunOutcome`, `degradation_notes`, `narration_has_speech`. A mute video reports `degraded`, not `ready` |
| **P1-5** | **PARTIAL — BLOCKED** | Schema, roles and RLS policies written and verified structurally by 23 tests. **Never executed.** No server, no `asyncpg`, no package index |
| **P1-6** | RESOLVED (spans open) | Prometheus `/metrics` with bounded label cardinality; a `trace_id` that crosses the queue in the job payload; `/health/live` and `/health/ready` checked against the route table. OpenTelemetry spans are not implemented |
| **P1-7** | RESOLVED | Deleting a project removed the rows and left the bytes. Storage is now deleted **first**, deliberately: interrupted, that leaves a visible broken row the next sweep completes, rather than an invisible permanent leak. `Plan.max_retention_days` is finally read |

### Two design decisions worth naming

**Deletion order.** Deletion spans two stores and cannot be atomic, so the
question is which wreckage is recoverable. Database-first leaves objects nothing
knows about — invisible, unbilled, removable only by a full-bucket scan. That is
a permanent leak of customer data. Storage-first leaves a row pointing at
nothing — visibly broken, found again by the next sweep, completable. The second
is strictly better, so the code is ordered to produce it.

**Sandboxing by subprocess, not by timeout.** You cannot interrupt a CPU-bound C
extension from Python. `signal.alarm` does not fire until the interpreter
regains control; a watchdog thread can *observe* an overrun but not stop it, and
abandoning the thread leaks it along with everything it allocated. A "timeout"
implemented that way is a log line, not a control. A child process can be
killed.

---

## Scores

Against the audit's twelve dimensions, same scale, same intent. Where the audit
was harsh and remains right, the number has not moved.

| Dimension | Audit | Now | Why |
| --- | --- | --- | --- |
| Architecture | 72 | **86** | Chokepoints replaced conventions in six places; contracts→ports→adapters still enforced by AST test |
| Security | 34 | **78** | Destructive endpoint authorised, tenancy fails closed, real password hashing, tenant-namespaced storage, sandboxed parsing. **Not**: encryption at rest, SSO, a scanner |
| Reliability | 22 | **80** | Durable queue on the real path, at-least-once with idempotent effects, reclaim after worker death, graceful drain |
| Scalability | 15 | **55** | Two replicas genuinely work; no process-local correctness state. Ceiling is one machine until P1-5 |
| Observability | 38 | **74** | Metrics, correlation across the queue, liveness/readiness split. No distributed tracing spans |
| Data integrity | 45 | **82** | Forward-only migrations with a checksum ledger, backfill-not-delete, six databases covered |
| Multi-tenancy | 40 | **80** | Required tenant on every document, scoped queries, namespaced storage. Database-level RLS written, not executed |
| Billing | 41 | **70** | Reserve→settle with idempotency keys, expiry actually runs, plan retention enforced. No price versioning or invoices |
| Product correctness | 44 | **83** | Grounding gate is mandatory; the Visual Bible reaches the pixels; degradation is a first-class outcome |
| Deployment | 8 | **68** | Image, compose, migrations, health probes — all checked against the code by test. **Never run in Docker here** |
| Operations | 17 | **65** | Runnable migrations, readiness gating, drain semantics, metrics. No runbook rehearsal, no DR test |
| **Overall production readiness** | **31** | **74** | |

**74, not 90.** The three things holding it there are named below, and none of
them is a matter of writing more code in this environment.

---

## What is NOT proven, stated plainly

1. **The container has never been built.** Docker is unavailable here.
   `tests/test_deployment.py` proves the artifacts agree with the code — the
   entrypoints resolve, the healthcheck targets routes the API serves, the
   install step reads a file that exists, the compose file starts nothing the
   code never contacts, the grace periods are ordered correctly. It does not
   prove the image builds, that ffmpeg and the Noto fonts suffice at runtime, or
   that two replicas come up healthy against one volume. Those four are listed
   in `deploy/README.md` as the first things to check on a machine with a daemon.

2. **PostgreSQL has never been run.** 23 tests read the DDL as data and assert
   the properties that make RLS real rather than decorative — `FORCE`, a
   `WITH CHECK` on every policy, a non-owning application role with
   `NOBYPASSRLS`, a transaction-scoped `SET LOCAL`. What remains is a driver, a
   pool, and one integration test proving the *database* refuses a cross-tenant
   read.

3. **No AI provider has ever been called.** Every HTTP provider adapter is real
   and wired; none has a credential. With none configured the system degrades
   visibly — `/health` reports which capabilities are absent, and a document
   render returns `outcome: degraded` with `narration.has_speech: false` rather
   than claiming success. This is an EXTERNAL DEPENDENCY, not a design gap.

4. **Dependency hashes are not pinned.** Versions are, exactly, to what the
   suite ran against. `--require-hashes` needs a package index this environment
   cannot reach; `requirements.lock.md` records the two-line change.

5. **The scaling ceiling is one machine.** SQLite on a shared volume is
   genuinely correct across processes on one host — proven by
   `tests/test_worker_runtime.py`, which drives separate API and worker objects
   over shared files. It is *not* correct across a network filesystem with weak
   locking. Adding a second node would not fail loudly.

---

## What remains, in priority order

| | Task | Blocked on |
| --- | --- | --- |
| **1** | P1-5: PostgreSQL driver + pool + integration test | a server and a package index |
| **2** | Build and run the container; verify the four unproven deployment claims | a Docker daemon |
| **3** | P2-1: encryption at rest, KMS, residency at provider selection | a KMS |
| **4** | P2-2: SSO, SCIM, DSAR tooling | an IdP for SSO; DSAR is implementable now |
| **5** | P2-3: price versioning, immutable invoices, per-tenant cost attribution | nothing |
| **6** | P2-4: admin console with its own audited cross-tenant path | nothing |
| **7** | P3-1: render determinism, checkpointing, resumability | nothing |
| **8** | P3-2: evaluation corpus over the document path and grounding false positives | nothing |
| **9** | P3-3: pagination, API versioning policy, response contract tests | nothing |
| **10** | Stage 8: the remaining 11 visual primitives | nothing |
| **11** | Stage 13: natural-language semantic editing | nothing |
| **12** | Real-time streaming voice transport | a streaming STT provider |

Items 5–11 are ordinary engineering with no external dependency. Items 1–4 and
12 need something this environment does not have, and saying so is not an
excuse — it is the difference between a roadmap and a wish.

---

## The rule that produced all of it

> A component exists. A composition root uses it. A real runtime path reaches
> it. Its state persists. Its security boundary is enforced where a caller
> cannot route around it. Its failure semantics are honest. Its deployment model
> agrees with all of the above.

A library, a class, a test and a schema existing is not completion. That is the
sentence the audit was written to establish and the one every fix above was
measured against.

---

## Provenance

- Baseline audit: `docs/AUDIT_2026-08-13.md` (read-only, static)
- Task board: `docs/REMEDIATION.md`
- Deployment truth: `deploy/README.md`, `migrations/README.md`,
  `requirements.lock.md`
- Superseded in part: `BUILD_REPORT.md`, `docs/PRODUCTION_READINESS.md`
