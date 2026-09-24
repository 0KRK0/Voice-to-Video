# Remediation roadmap

**Source of truth: `docs/AUDIT_2026-08-13.md` (static CTO audit).**
This document supersedes the stage-completion language in `BUILD_REPORT.md`
wherever the two disagree. The audit wins.

## The completion rule

A stage is **COMPLETE** only when all seven hold:

1. the implementation exists
2. the composition root (`wiring.py`, `api/app.py`) actually uses it
3. failure semantics are correct
4. security boundaries are enforced *on the real path*, not in a library
5. multi-tenant behaviour is correct
6. production deployment assumptions are satisfied *at that stage's boundary*
7. no known P0 blocker lives inside the stage

Test count is **not** a completion criterion. Several stages have comprehensive
passing tests over code that no request ever reaches.

Statuses: **GREEN** production-ready · **YELLOW** implemented, operationally
incomplete · **ORANGE** functional, architectural debt · **RED** unsafe /
prototype · **GRAY** not implemented.

---

## Stage board after audit

### Closed — genuinely complete

| Stage | Name | Why it closes |
| --- | --- | --- |
| ~~0~~ | ~~Product and architecture contract~~ | **GREEN.** Contracts are used everywhere, schema drift is enforced by test, boundary governance is executable. |
| ~~1~~ | ~~Voice capture~~ | **GREEN.** Wired, magic-byte sniffing, real probing, failure states honest. |
| ~~4~~ | ~~Scene engine~~ | **GREEN.** Pure, deterministic, wired, no external dependency. |
| ~~7~~ | ~~Generation router~~ | **GREEN.** Caching, budgets, circuit breaking, fallback ladder — all on the real path. |
| ~~11~~ | ~~Audio and captions~~ | **GREEN** at its own boundary. Captions derive from the transcript, so they cannot introduce an unsupported claim. (Serving them is P0-8, a Stage 15 defect.) |
| ~~16~~ | ~~Evaluation system~~ | **GREEN** as a framework. Corpus coverage gaps are tracked as P3-2, not as stage incompleteness. |
| ~~17~~ | ~~Cost optimisation~~ | **GREEN.** Content-addressed cache, budgets and ledger are real and used. (Per-tenant aggregation is P2-3, a billing concern.) |

### Open — partial, blocked, or downgraded by the audit

| Stage | Name | Status | Blocking tasks |
| --- | --- | --- | --- |
| 2 | Speech intelligence | YELLOW | external credential |
| 3 | Semantic understanding | YELLOW | LLM engine unexercised |
| 5 | Visual Director | YELLOW | LLM director unexercised **and ungrounded** → P0-1 |
| 6 | Asset intelligence | YELLOW | network |
| 8 | Animation engine | YELLOW | 6 of 17 primitives |
| 9 | Scene composition | YELLOW | becomes the grounding chokepoint → P0-1 |
| 10 | Timeline and rendering | **ORANGE** *(downgraded from complete)* | non-deterministic, not resumable → P3-1; runs in event loop → P0-4 |
| 12 | Storyboard UI | YELLOW | reads process memory → P0-8 |
| 13 | Visual editing | YELLOW | in-place mutation race; "try another approach" only |
| 14 | Project persistence | **ORANGE** *(downgraded from complete)* | no migrations, nullable tenant, incomplete deletion → P0-3, P0-11, P1-7 |
| 15 | Production infrastructure | **RED** | unauthenticated sweep, in-process queue, in-memory results → P0-2, P0-5, P0-8 |
| 18 | Creator beta | YELLOW | no accounts → P0-9 |
| 19 | Business and education | YELLOW | document ingest now real; brand kits absent |
| 20 | Platform and API | YELLOW | `/v1/visualize` plans only; no pagination/versioning → P3-3 |
| **21** | **Universal input layer** | **YELLOW — PARTIAL** | inspection bypassed off the HTTP path, no parser timeouts, keys not tenant-scoped → P1-2, P1-3 |
| **22** | **Real-time voice** | **YELLOW — PARTIAL** | no streaming transport; mute run still reports success → P1-4 |
| **23** | **Visual consistency engine** | **ORANGE — PARTIAL** | bible built and reviewed but **never applied or persisted** → P1-1 |
| **24** | **Factuality and grounding** | **RED — BLOCKED** | **LLM director bypasses the gate entirely** → P0-1 |
| **25** | **Security and privacy hardening** | **RED — BLOCKED** | unauthenticated destructive endpoint; tenant fail-open; `paths.py` unused; limiter process-local → P0-2, P0-3, P0-6, P1-2 |
| **26** | **Billing and usage** | **ORANGE — PARTIAL** | reservations process-local; expiry never runs → P0-6, P0-7 |
| **27** | **Scalability** | **RED — BLOCKED** | durable queue unused; renders in the event loop; cannot run two replicas → P0-4, P0-5, P0-10 |
| **28** | **Reliability and DR** | **RED — BLOCKED** | all recovery guarantees live in unused code → P0-5 |
| **29** | **Enterprise foundation** | **ORANGE — PARTIAL** | isolation fails open; no encryption at rest; deletion incomplete; SSO absent → P0-3, P1-7, P2-1, P2-2 |
| **30** | **Production readiness** | **RED — OPEN** | audit delivered (31/100); no deployment artifact of any kind → P0-11 |

**Nothing in Stages 21–30 closes.** Every one of them has either a P0 inside it
or a capability that exists only as a library.

### Preserved history — implemented components

None of the work below is deleted or discounted. It is real, reviewed code that
is **not yet on the request path**. It becomes the implementation for the tasks
that wire it in.

| Component | File | Wiring task |
| --- | --- | --- |
| `DurableJobQueue` (1,035 lines, 27 tests) | `adapters/queue/durable.py` | P0-5 |
| `safe_join` / `safe_storage_key` / `tenant_key` / `safe_filename` | `security/paths.py` | P1-2 |
| `ConsistencyEngine` + `VisualBible` | `pipeline/consistency.py`, `contracts/consistency.py` | P1-1 |
| `check_spec` / `Evidence` | `pipeline/grounding.py` | P0-1 |
| `archive_is_safe` / `inspect_upload` | `security/uploads.py` | P1-3 |
| `expire_reservations` | `billing/usage.py` | P0-7 |
| `postgres_schema()` (repository and queue) | `adapters/repository/sqlite.py`, `adapters/queue/durable.py` | P1-5 |
| `HttpSpeechSynthesisProvider`, `HttpSpeechToTextProvider`, `HttpTextGenerationProvider`, `HttpImageGenerationProvider` | `adapters/*` | blocked on credentials, not on us |

---

## P0 — MUST FIX BEFORE ANY PAYING CUSTOMER

### Status — all eleven RESOLVED (2026-08-13)

Verified state: **608 tests passing**, `ruff check .` clean, `mypy --strict`
clean across 116 source files, schemas current, evaluation harness passing.

| ID | Status | The chokepoint that now enforces it | Proof |
| --- | --- | --- | --- |
| **P0-1** | RESOLVED | `PlanGate`, applied in the orchestrator; `Pipeline.plan_gate` has no default so it cannot be omitted | `tests/test_plan_gate.py` |
| **P0-2** | RESOLVED | route is `/v1/retention/sweep` with a capability guard and an audit record; a path prefix is not an access control | `tests/test_api_security.py::RetentionSweepIsNotInternalByNaming` |
| **P0-3** | RESOLVED | `organisation_id` is a required contract field and a repository *query parameter*, not an `if` after the query | `tests/test_api_security.py::TenancyFailsClosed` |
| **P0-4** | RESOLVED | `vtv.worker` consumes; the API registers **zero** handlers, so it cannot execute a job | `tests/test_worker_runtime.py::TheApiDoesNotRunWork` |
| **P0-5** | RESOLVED | `DurableJobQueue` on a shared file is the only queue wired | `tests/test_worker_runtime.py::FailureAndRecovery` |
| **P0-6** | RESOLVED | `SharedStore`; the limiter holds no correctness-critical state | `tests/test_security.py::test_two_limiters_over_one_store_enforce_one_limit` |
| **P0-7** | RESOLVED | `Worker._maintenance` expires reservations every 30s; reservations live in one table | `tests/test_worker_runtime.py` |
| **P0-8** | RESOLVED | artifacts are read from persisted documents; `ApiState.results` is deleted | `tests/test_worker_runtime.py::test_a_replica_that_never_saw_the_upload_serves_the_video` |
| **P0-9** | RESOLVED | PBKDF2-HMAC-SHA256 at 600k iterations with transparent Argon2id upgrade | `tests/test_passwords.py` |
| **P0-10** | RESOLVED | denormalised indexed `active` / `plan` columns; no per-request O(tenants) scan | `tests/test_migrations.py::TheDenormalisationMigration` |
| **P0-11** | RESOLVED | Dockerfile, compose, forward-only migrations with a checksum ledger, health/readiness split | `tests/test_deployment.py`, `tests/test_migrations.py` |

### Defects discovered *during* remediation, and fixed

Each of these was found by a test written to prove a P0 fix, not by inspection.
They are recorded because they are the argument for writing the tests that way.

| Found by | Defect | Fix |
| --- | --- | --- |
| `tests/test_api_security.py` running `upgrade()` for real | Migration 2 was declared against the `repository` database; `organisations` lives in `directory.db`. **`migrate upgrade` failed on every fresh deployment.** | migration retargeted; `migrators()` now derives from `wiring.DATABASES` |
| the same | The migrator knew about **three of six** databases. `directory.db`, `audit.db` and `usage.db` had no migration path at all. | one `DATABASES` table in `wiring.py`, used by both the assembly and the migrator |
| `tests/test_migrations.py` baseline work | Running the *current* schema over an *older* database fails on `CREATE INDEX` for columns that release did not have. | `baseline()` runs only on an empty database; existing databases are the migrations' responsibility alone |
| `tests/test_deployment.py` | The compose file started PostgreSQL and Redis, set `VTV_DATABASE_URL: postgresql://…`, and `repository_path()` **silently fell back to a local SQLite file** — so each replica wrote to its own private database while the file looked distributed. | services removed; `repository_path()` raises on any scheme it does not implement |
| the same | The Dockerfile installed from `requirements.lock` with `--require-hashes`. That file was never generated and could not be — **the image could not have built.** | `requirements.txt` with exact pins; the hash gap is stated in `requirements.lock.md` rather than faked |
| the same | `LocalStorageProvider` generated a **per-process** URL signing key. Behind a load balancer a media URL signed by one replica is rejected by the next. | `VTV_SIGNING_KEY`; production refuses to start without it |
| the same | `.env.example` documented four variables the code has never read (`VTV_REDIS_URL`, `VTV_STORAGE_ACCESS_KEY`, `VTV_STORAGE_SECRET_KEY`, `VTV_ASSET_SEARCH_API_KEY`); three real settings could not be set from the environment at all. | example rewritten; a test asserts every documented key is one `Settings.from_env` reads |
| the same | CORS declared `GET, POST` while `/v1/api-keys/{id}` serves `DELETE` — a preflight failure visible only in a browser. | `DELETE` added |

### What P0 does **not** yet establish

Stated so the table above is not read as more than it is:

- **NOT PROVEN FROM STATIC INSPECTION or from these tests:** that the container
  image builds, that ffmpeg and the Noto fonts are sufficient at runtime, or
  that two replicas come up healthy against one volume. Docker is unavailable in
  this environment; `deploy/README.md` names these four checks explicitly.
- The deployment scales to **one machine**. SQLite on a shared volume is
  genuinely correct across processes on one host and genuinely wrong across a
  network filesystem. P1-5 removes that ceiling.
- Dependency **hashes** are not pinned (no reachable package index). Versions
  are.

---

### P0-1 · Make grounding mandatory at the composition boundary

- **Problem.** A language model can emit a `ChartSpec` with fabricated values and it renders into a customer-facing video unchecked.
- **Root cause.** `check_spec` is called only from `RuleBasedVisualDirector._ground` (`director.py:223, 546`). `LlmVisualDirector.direct()` validates the payload into a `VisualPlan` and returns. `wiring.py` makes the LLM director *primary* when a text credential exists, with rules as fallback. The gate guards the path that cannot hallucinate and skips the one that can.
- **Files.** `pipeline/director.py`, `pipeline/grounding.py`, `pipeline/composition.py`, `pipeline/orchestrator.py`, `wiring.py`.
- **Expected architecture.** A `PlanGate` applied to every `VisualPlan` before composition, regardless of which director produced it. Directors propose; the gate disposes. Refusal remains a `DegradationStep`.
- **Acceptance.** No code path reaches composition with an ungrounded programmatic spec. A test constructs an `LlmVisualDirector` whose provider returns a fabricated chart and asserts it is refused and degraded.
- **Dependencies.** None. Start here.
- **Stage.** 24 (also 5, 9). **Migration.** No.
- **Security impact.** Removes the system's largest false-claim surface.
- **Scalability impact.** None.

### P0-2 · Authenticate and tenant-scope `/internal/sweep`

- **Problem.** Unauthenticated POST deletes projects and storage objects across every tenant.
- **Root cause.** `app.py:1180` — the route has no `guard()` call. Its own docstring says it should not be an endpoint in production; nothing enforces that.
- **Files.** `api/app.py:1180`, `adapters/repository/sqlite.py` (`expired_projects`), `adapters/storage/local.py` (`sweep_expired`).
- **Expected architecture.** Retention runs as a scheduled worker job under a system principal, tenant by tenant. If an endpoint is kept, it requires an internal credential and a tenant parameter.
- **Acceptance.** Unauthenticated POST returns 401 or 404. Sweeping cannot delete outside a named tenant. The action is audited.
- **Dependencies.** None.
- **Stage.** 25, 29. **Migration.** No.
- **Security impact.** Closes unauthenticated cross-tenant data destruction.
- **Scalability impact.** Positive — moves an unbounded scan out of the request path.

### P0-3 · Make tenant ownership fail closed at the data boundary

- **Problem.** Any project whose `organisation_id` is null is readable and editable by every authenticated tenant.
- **Root cause.** `app.py:365` — `if owner is not None and not principal.owns(owner)`. `Project.organisation_id` is `Id | None`, and every project created outside `create_project` (direct `Pipeline.run`, demo, `visualise_text`, evaluation, pre-column rows) has it null. The passing isolation tests miss this because they all create through `create_project`.
- **Files.** `contracts/project.py`, `adapters/repository/sqlite.py`, `ports/repository.py`, `api/app.py:354`.
- **Expected architecture.** Non-nullable column; scoping pushed into `repository.get_project(id, organisation_id=...)` so a caller cannot forget; Postgres RLS as the eventual backstop (P1-5).
- **Acceptance.** A project written directly through the repository with a foreign or null tenant is unreachable over HTTP. A test asserts it.
- **Dependencies.** None. Blocks P1-2, P1-5.
- **Stage.** 25, 29. **Migration.** **Yes** — backfill then `NOT NULL`.
- **Security impact.** Closes cross-tenant read/write.
- **Scalability impact.** Enables an index-backed tenant-scoped query.

### P0-4 · Split `vtv-api` from `vtv-worker`; move rendering off the event loop

- **Problem.** One 25–30s render stalls every request on the process, including health checks and authentication.
- **Root cause.** `InProcessJobQueue.enqueue` creates an asyncio task; `run_pipeline` awaits `Pipeline.run`; the renderer's frame loop (`ffmpeg_renderer.py:216-228`) is synchronous CPU work writing to a `Popen` pipe. No executor, no separate process.
- **Files.** `api/app.py`, `wiring.py`, `adapters/render/ffmpeg_renderer.py`, new `src/vtv/worker.py`.
- **Expected architecture.** Two entrypoints. The API may only enqueue and read; the worker owns the pipeline. The queue is the only boundary.
- **Acceptance.** The API process does not import the renderer. Concurrent renders leave API p99 unchanged.
- **Dependencies.** None. Blocks P0-5, P0-8, P1-3.
- **Stage.** 27. **Migration.** No.
- **Security impact.** Privilege separation — untrusted document parsing stops sharing a process with authentication.
- **Scalability impact.** The single largest unlock. Nothing else scales until this lands.

### P0-5 · Wire `DurableJobQueue`; retire `InProcessJobQueue` from production

- **Problem.** Delivery semantics today are **at-most-once with no durability**. Any restart loses in-flight and queued work silently.
- **Root cause.** `app.py:170` instantiates `InProcessJobQueue`. `DurableJobQueue` has zero production callers. Idempotency lives in `self._by_key`, a dict.
- **Files.** `wiring.py`, `api/app.py:170`, `adapters/queue/inprocess.py`, `adapters/queue/durable.py`.
- **Expected architecture.** Durable queue selected by settings; in-process retained for tests only and unreachable from `Settings`. Target semantics stated honestly as **at-least-once delivery with idempotent effects**, not exactly-once.
- **Acceptance.** A job enqueued, then the API killed and restarted, still runs to completion. Re-running a settled job does not double-charge.
- **Dependencies.** P0-4.
- **Stage.** 27, 28. **Migration.** Queue schema (already drafted).
- **Security impact.** Low.
- **Scalability impact.** Enables multiple workers.

### P0-6 · Shared store for rate limits and quota reservations

- **Problem.** N replicas enforce N× every rate limit, including the credential-stuffing limit, and quota reservations are invisible between processes — a tenant can exceed a paid plan by running requests against different pods.
- **Root cause.** `security/limits.py` `_buckets` is a dict. `billing/usage.py:347` `_reserved()` reads `self._reservations` (`usage.py:239`) and never queries the `usage_reservations` table it writes.
- **Files.** `security/limits.py`, `billing/usage.py`.
- **Expected architecture.** Redis (or Postgres) behind the existing `RateLimiter` and `UsageMeter` interfaces. No signature change.
- **Acceptance.** Two processes sharing a store enforce one limit and one quota. A test runs two meter instances against one backing store.
- **Dependencies.** None. Blocks P0-7.
- **Stage.** 25, 26. **Migration.** New store.
- **Security impact.** Restores the login limit under replication.
- **Scalability impact.** Prerequisite for replication.

### P0-7 · Run reservation expiry; reconcile the two reservation stores

- **Problem.** A crashed render permanently consumes quota; after a restart the memory store and the table disagree about what is held.
- **Root cause.** `expire_reservations()` exists and has no caller. `_reservations` is authoritative for reads, the table for writes.
- **Files.** `billing/usage.py`, `wiring.py`, scheduled job.
- **Expected architecture.** The durable table is the single source of truth; expiry runs on a schedule.
- **Acceptance.** An abandoned reservation is released within its TTL with no operator action. Restart does not change the reserved total.
- **Dependencies.** P0-6.
- **Stage.** 26. **Migration.** No.
- **Security impact.** None. **Scalability impact.** Correctness under replication.

### P0-8 · Serve artifacts from storage and the repository, not memory

- **Problem.** A restart makes every completed video unreachable; a second replica 404s at random; memory grows without bound.
- **Root cause.** `ApiState.results` (`app.py:97`) is the only source for `/video`, `/storyboard`, `/captions.vtt`, `/thumbnail.png`.
- **Files.** `api/app.py`, `adapters/repository/sqlite.py`.
- **Expected architecture.** Read persisted documents and object storage. `_persist` already writes most of what is needed.
- **Acceptance.** Video and storyboard retrievable after restart and from a replica that never ran the job.
- **Dependencies.** P0-4. Blocks P1-1.
- **Stage.** 14, 15. **Migration.** No.
- **Security impact.** None. **Scalability impact.** Prerequisite for replication.

### P0-9 · Provide a working human authentication path

- **Problem.** No end user can log in. Only API keys authenticate.
- **Root cause.** `Directory.verify_password` raises `NotImplementedError`; no `argon2-cffi` or `bcrypt` in any `pyproject.toml` extra; no OIDC integration.
- **Files.** `security/directory.py`, `pyproject.toml`.
- **Expected architecture.** Declare and implement Argon2id, or ship OIDC against `Directory.principal_for_user`. Keep the refusal if the KDF is genuinely absent — the refusal is correct; the missing dependency is not.
- **Acceptance.** A human authenticates by a supported mechanism.
- **Dependencies.** None.
- **Stage.** 25, 29. **Migration.** Possibly a credential column.
- **Security impact.** Must not be closed by weakening the refusal.
- **Scalability impact.** None.

### P0-10 · Remove per-request O(tenants) work and blocking SQLite from handlers

- **Problem.** Request cost grows with tenant count; every handler blocks the event loop on synchronous SQLite.
- **Root cause.** `guard()` (`app.py:345`) calls `suspended_ids()`, which reads and JSON-parses every organisation, per request. Plus `tier_of()` and `authenticate()`. `Directory._connect()` opens a new connection and re-issues PRAGMAs each call. ~25 blocking call sites across `api/app.py`.
- **Files.** `api/app.py:296-350`, `security/directory.py`, `security/audit.py`, `billing/usage.py`.
- **Expected architecture.** Suspension cached with invalidation; indexed lookup by id; connection pooling; all DB access via `asyncio.to_thread` or an async driver.
- **Acceptance.** Request cost independent of tenant count; no synchronous DB call in a handler.
- **Dependencies.** None.
- **Stage.** 15, 27. **Migration.** Index only.
- **Security impact.** Also fixes `authenticate()` doing DB work *before* the rate-limit check (`app.py:309-311`), an amplification vector.
- **Scalability impact.** High.

### P0-11 · Deployment artifacts: Dockerfile, migrations, lockfile

- **Problem.** The system cannot be deployed reproducibly, schema changes have no versioning, and a compromised dependency has nothing standing in its way.
- **Root cause.** No Dockerfile, compose or manifest anywhere. `CREATE TABLE IF NOT EXISTS` is the entire migration strategy — which is how the nullable `organisation_id` of P0-3 arose. No lockfile, no hashes; several extras are unpinned above their floor.
- **Files.** new `Dockerfile`, new `migrations/`, `pyproject.toml`, new lock artifact.
- **Expected architecture.** Container image, versioned migrations applied at deploy, hash-pinned dependency set.
- **Acceptance.** Builds and runs from a container against a versioned schema with a reproducible dependency set.
- **Dependencies.** None. Blocks P1-5.
- **Stage.** 30. **Migration.** Introduces the framework.
- **Security impact.** Closes the supply-chain gap.
- **Scalability impact.** Precondition for any orchestration.

---

## P1 — REQUIRED BEFORE SERIOUS SCALE

| ID | Task | Status | The chokepoint / what changed | Proof |
| --- | --- | --- | --- | --- |
| **P1-1** | Apply *and persist* the Visual Bible | **RESOLVED** | Composition consults it; the colour lock travels on the `Timeline`; the worker reloads the approved Bible before every render | `tests/test_visual_bible_applied.py` |
| **P1-2** | Tenant-prefixed storage keys through the port | **RESOLVED** | `LocalStorageProvider.put`/`put_file` refuse any key outside `orgs/<org>/`; `organisation_id` is required on every project-scoped document | `tests/test_tenant_storage.py` |
| **P1-3** | Sandbox parsing; inspect inside `IngestionService` | **RESOLVED** | `inspect_upload` runs unconditionally in the service; parsing happens in a child process with a wall-clock kill and `RLIMIT_AS`/`RLIMIT_CPU` | `tests/test_ingestion_sandbox.py` |
| **P1-4** | Degradation is part of the success predicate | **RESOLVED** | `RunOutcome`, `degradation_notes`, `narration_has_speech` on the project | `tests/test_worker_runtime.py::DegradationIsVisible` |
| **P1-5** | PostgreSQL with tenant row-level security | **PARTIAL — BLOCKED** | Schema, roles and RLS policies written and structurally verified; `repository_path()` *raises* on any unimplemented scheme rather than silently using SQLite. **Never executed** — no server, no driver, no package index | `tests/test_postgres_schema.py`, `tests/test_deployment.py` |
| **P1-6** | Metrics, tracing, correlation, real probes | **RESOLVED for metrics, correlation and probes; distributed tracing spans remain OPEN** | Prometheus `/metrics` with bounded label cardinality; a `trace_id` that crosses the queue in the job payload; `/health/live` and `/health/ready` checked against the route table | `tests/test_observability.py` |
| **P1-7** | Complete deletion; per-plan retention | **RESOLVED** | `vtv.retention` — storage deleted *before* records so an interruption is recoverable; `Plan.max_retention_days` is finally read | `tests/test_retention.py` |

### What P1 fixed, in one line each

* **P1-1.** The Visual Bible had no effect on any pixel. It does now, and it
  survives a re-render — including bindings a user *locked*, which previously
  were discarded silently. A control that does not survive a re-render is not a
  control.
* **P1-2.** `tenant_key()` existed and had zero callers. Every writer built its
  own key with an f-string, so per-tenant deletion, retention and residency were
  inexpressible. The storage adapter now refuses a key it cannot attribute.
* **P1-3.** Bomb and mismatch checks lived only in `api/app.py`, so a document
  arriving from the queue was parsed unexamined. They now run in the ingestion
  service, and the parse itself runs in a child process — because you cannot
  interrupt a CPU-bound C extension from Python, so a "timeout" implemented
  in-process is a log line rather than a control.
* **P1-6.** An operator could not follow one render across the API, the queue
  and the worker: the three wrote logs with no field in common. A `trace_id`
  now crosses the queue in the job payload, and aggregate metrics exist that a
  dashboard can page on — with label cardinality bounded by construction, since
  a metric labelled by project id is one series per project.
* **P1-7.** Deleting a project removed the rows and left the bytes. Storage is
  now deleted first, on purpose: interrupted, that leaves a visible broken row
  the next sweep completes, rather than an invisible permanent leak.

### The one thing P1 did not close

**P1-5 is the scaling ceiling, and it is blocked on this environment, not on
design.** The queue, the rate limiter and the repository are SQLite files on a
shared volume. That is genuinely correct across processes on one host — every
correctness-critical operation is a single atomic statement under `BEGIN
IMMEDIATE`, proven by `tests/test_worker_runtime.py` driving real separate API
and worker objects over shared files. It is **not** correct across a network
filesystem with weak locking, and it will contend under write load long before
a real database would.

The PostgreSQL schema exists, with row-level security, `FORCE ROW LEVEL
SECURITY`, `WITH CHECK` on every policy, a non-owning application role with
`NOBYPASSRLS`, and a transaction-scoped `SET LOCAL` for the tenant. Twenty-three
tests read that DDL as data and assert the properties that make RLS real rather
than decorative. **None of them run PostgreSQL**, because there is no server, no
`asyncpg`, and no reachable package index. What remains is a driver, a
connection pool, and one integration test proving a cross-tenant read is refused
by the database rather than by us.

### Contract changes P1 required

Both are the P0-3 lesson applied further out — *optional ownership is not
ownership*:

* `organisation_id` is now **required** on `Recording`, `Transcript`,
  `Understanding`, `SceneGraph`, `VisualPlan`, `VisualBible`, `SourceDocument`,
  `Timeline`, `RenderJob` and `GenerationRequest`.
* `Project` no longer forbids an expiry on a **saved** project. That invariant
  read "saved" as "forever", which is precisely what made `max_retention_days`
  unenforceable and therefore unread.

## P2 — REQUIRED FOR ENTERPRISE

| ID | Task | Stage | Migration |
| --- | --- | --- | --- |
| **P2-1** | Encryption at rest, KMS, residency enforcement at provider selection | 29 | key management |
| **P2-2** | SSO, SCIM, audit export, DSAR tooling | 29 | possibly |
| **P2-3** | Price versioning, immutable invoices, per-tenant cost attribution | 26 | **yes** |
| **P2-4** | Admin/support console with its own audited cross-tenant access path | 29, 30 | no |

---

## P3 — OPTIMISATION / HARDENING

| ID | Task | Stage |
| --- | --- | --- |
| **P3-1** | Render determinism, checkpointing, resumability | 10 |
| **P3-2** | Evaluation corpus: document path + grounding precision metric | 16, 24 |
| **P3-3** | Pagination, versioning policy, response contract tests | 20, 25 |
| — | Remotion adapter, GPU encode, remaining 11 primitives, word-level captions, CDN, cross-region DR | 8, 10, 11, 28 |

---

## Critical path

```
P0-4  split API / worker
  ├──▶ P0-5  wire durable queue
  └──▶ P0-8  artifacts from storage ──▶ P1-1  apply Visual Bible

P0-3  tenancy fails closed ──┬──▶ P1-2  tenant storage keys ──▶ P1-7, P2-1
                             └──▶ P1-5  Postgres + RLS
P0-11 deployment artifacts ──┘

P0-6  shared limits/quota ──▶ P0-7  reservation expiry ──▶ P2-3

P0-1  grounding gate      (independent — start immediately)
P0-2  sweep endpoint      (independent — hours of work)
P0-9  human auth          (independent)
P0-10 blocking/O(n) work  (independent)
```

**Longest chain: P0-4 → P0-8 → P1-1.** Everything else parallelises around it.
**Highest value per hour: P0-2 and P0-1** — one is a few hours and closes an
unauthenticated destruction path; the other is a day and removes the largest
false-claim surface in the product.

---

## What "production-ready" requires

The system may be described as production-ready when **all eleven P0 tasks are
closed and these five statements are demonstrably true**:

1. **No capability exists only as a library.** Every guarantee named in `docs/` is reachable from a request. Verified by a test that fails if a director, a route or a service bypasses a gate.
2. **Two replicas behave as one system.** Rate limits, quotas, job identity and artifact retrieval are identical whichever process serves the request.
3. **A restart loses nothing.** Queued work resumes; completed work remains retrievable.
4. **Tenant isolation is architectural.** Enforced by the repository signature and the storage key type, not by remembering to write an `if`.
5. **Success means success.** `ready` implies narrated, grounded and rendered. Degradation is visible in the status a client actually checks.

Until then the honest description is: **a strong single-node prototype with an
excellent contracts layer, an under-built composition root, and a documented
remediation path.** That is a defensible thing to say to an investor. Claiming
Stages 23, 24, 27 or 28 are complete is not.
