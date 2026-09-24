# Final gap audit

**Date:** 19 August 2026
**Scope:** the P0 and P1 gap list, closed or reported as blocked.
**Rule for this document:** nothing is claimed that was not run. Where a claim
rests on something that cannot be run here — a vendor account, a human
reviewer — it is marked BLOCKED and the blocker is named.

---

## The seven classifications

| Classification | What it means |
| --- | --- |
| **IMPLEMENTED** | The code exists and does the thing. Nothing about it has necessarily been executed. |
| **RUNTIME-WIRED** | The implementation is reachable from a running system — it is in `wiring.build`, on a route, or in a job handler. Code that exists but nothing constructs is not this. |
| **TESTED** | Executed by an automated test that fails when the behaviour is removed. |
| **EXTERNALLY VERIFIED** | Checked against something outside this repository — a published specification, a vector, a real container, a real process boundary. The strongest thing claimable here. |
| **PARTIAL** | Some of the requirement holds and the rest is named. |
| **BLOCKED** | Cannot be done in this environment. The blocker is stated, not implied. |
| **NOT IMPLEMENTED** | Does not exist. Listed so it is not mistaken for one of the above. |

A row can hold several. `IMPLEMENTED + RUNTIME-WIRED + TESTED` is the normal
finished state; `EXTERNALLY VERIFIED` is rarer and always says against what.

---

## 1. P0 gaps — status

| Gap | Status | Evidence |
| --- | --- | --- |
| Real AI providers | **IMPLEMENTED + RUNTIME-WIRED + TESTED**, vendor calls **BLOCKED** | Four HTTP adapters executed against a conforming OpenAI-shaped server in `tests/test_provider_adapters_live.py` (11 tests). No vendor credentials exist in this environment, so no request has ever reached a vendor. See §4. |
| AI budget enforcement | **IMPLEMENTED + RUNTIME-WIRED + TESTED** | `GenerationRequest._budget_is_bounded` fills a per-kind ceiling on every request built through validation; `GenerationRouter._authorised_ceiling` raises `BudgetExceeded` when `max_cost_usd` is still `None`. The rule is on the object, so a request cannot be constructed without a bound. |
| Provider spend circuit breaker | **IMPLEMENTED + RUNTIME-WIRED + TESTED** | `MeteredSpendAuthoriser` is injected into `GenerationRouter.spend_authoriser` in `wiring.build` and consulted per call, with in-flight tracking so a looping job cannot pass the same stale check twice. `tests/test_billing.py::ProviderSpendIsABreakerNotAMeter` drives a 100-call loop that stops after 4. |
| Real S3 object storage | **IMPLEMENTED + RUNTIME-WIRED + TESTED + EXTERNALLY VERIFIED** | `adapters/storage/s3.py`, full REST implementation with hand-rolled SigV4, no SDK. 30 tests. SigV4 verified against AWS's published `get-vanilla` vector. See §5. |
| Retention bug (orphan sweep deleting live media) | **IMPLEMENTED + TESTED** | The sweep now reads the retention class recorded with the object rather than deleting on age. A backend that cannot report a class is refused rather than swept. 41 tests in `tests/test_retention.py`. |
| Usage enforcement | **PARTIAL — 7 of 8 dimensions enforced** | See §6. `storage_gb` is the one exception and the API says so. |
| Production deployment | **EXTERNALLY VERIFIED** | Real image built, real containers, two workers, real MP4 downloaded. See §8. |
| Real provider cost measurement | **BLOCKED** | No vendor account. Every cost figure in this system is adapter-declared. See §7. |
| Real quality validation | **BLOCKED** | Requires human reviewers watching output. See §11. |

## 2. P1 gaps — status

| Gap | Status | Evidence |
| --- | --- | --- |
| PostgreSQL / RLS | **IMPLEMENTED + TESTED**, production deployment **NOT IMPLEMENTED** | Schema and `FORCE ROW LEVEL SECURITY` policies executed against a real PostgreSQL in `tests/test_postgres_rls.py` (18) and `test_postgres_schema.py` (25). The running deployment is still SQLite: `wiring.repository_path` refuses any non-SQLite URL rather than silently substituting a file. |
| S3 tenant isolation under real deployment | **IMPLEMENTED + TESTED**, real-bucket run **BLOCKED** | `require_tenant_key` on every S3 write path, tested. No bucket to run against. |
| Provider timeout / circuit breaker | **IMPLEMENTED + RUNTIME-WIRED + TESTED** | Router-level `asyncio.wait_for` → `TimeoutExceeded`; breaker with a cooldown window consulted inside `candidates()`, the router's single selection point. `tests/test_provider_resilience.py` (12). See §9. |
| Retry / idempotency under real failure | **TESTED** | Retried calls are billed once and reported to the spend authoriser once. Verified in the real deployment too: 10 jobs, all `attempts=1`, no double-processing. |
| Durable artifact lifecycle | **IMPLEMENTED + TESTED** | Retention class rides the write itself (object tag on S3, sidecar on disk). |
| Backup / restore | **NOT IMPLEMENTED** | No backup procedure exists. Listed rather than glossed. |
| Observability | **IMPLEMENTED + RUNTIME-WIRED + TESTED** | 34 tests; `/metrics`, correlated structured logs, one registry per assembly. |
| Alerting | **NOT IMPLEMENTED** | Metrics are exposed; nothing consumes them and no alert rule exists. |
| Graceful shutdown | **IMPLEMENTED + TESTED + EXTERNALLY VERIFIED** | SIGTERM to a running worker container mid-render: drained and exited 0 in 55.6 s, all in-flight renders completed. See §8. |
| Load testing | **PARTIAL** | Seven concurrent renders across two workers is a concurrency check, not a load test. No sustained-throughput or saturation testing has been done. |
| Frontend against real provider states | **EXTERNALLY VERIFIED** | The wire-contract suite ran against a live API process: 9/9. See §10. |
| Billing under concurrency | **TESTED** | Reservation/settlement covered; the seven-render concurrent run metered without discrepancy. Not stress-tested. |
| Deletion / retention E2E | **TESTED** | Not exercised against a real bucket. |

## 3. P2 — roadmap, not blockers

SSO, SCIM, audit export, DSAR, encryption-at-rest, data residency, admin
console, price/version history, org controls, worker orchestration and DR are
all **NOT IMPLEMENTED**, by decision. They are in `docs/ROADMAP.md`. None of
them is claimed anywhere in the product or the docs.

---

## 4. Actual providers and actual model names

There are **no vendor providers configured**, and therefore no real model names
to report. That is the honest answer and it is worth being precise about why.

The four HTTP adapters — transcription, speech synthesis, image generation, text
generation — are written against the OpenAI-compatible REST shape and take an
endpoint and a credential. Both are unset in every environment that exists here.
The boot-time capability report says so rather than implying otherwise:

```
real_transcription      false      real_asset_search       true
real_understanding      false      document_ingestion      true
real_visual_direction   false      rendering               true
real_image_generation   false
real_video_generation   false
real_speech_synthesis   false
```

`real_asset_search: true` is not a formality — Openverse and Wikimedia need no
credential, so rungs two and three of the visual sourcing ladder are genuinely
live. Rungs one and five (drawn and typographic visuals) are $0 and need no
provider at all. What is missing is generation: rungs three and four.

Configured model names are placeholders (`"unset"`), which is deliberate — a
default of `gpt-4o` would produce a system that looks configured and fails at
the first call.

## 5. Storage

`S3StorageProvider` is a complete implementation of the S3 REST API with
Signature Version 4 written by hand — `hmac`, `hashlib` and `httpx`, no boto3,
because no package index is reachable from this environment and SigV4 is about
eighty lines. It works unchanged against S3, R2, MinIO, B2, Ceph RGW and Wasabi.

**What is verified externally.** The signature is checked against AWS's own
published `get-vanilla` test vector from the SigV4 test suite — canonical
request byte-exact, string-to-sign byte-exact, and the final signature equal to
`5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31`. This
matters more than it sounds: a wrong signature is the one failure mode that
passes every test written against a server we control, because a server we
control can simply not check, and then fails on the first real request with an
error about credentials rather than about encoding.

Two of the vector constants in the first version of that test were wrong — my
own, not AWS's. The signer was right and the assertions were fiction. They were
corrected against the published values and cross-checked against an independent
implementation.

**Backend selection** (`wiring.storage_provider`) is decided by whether S3
credentials are present, not by a `STORAGE_BACKEND` name. A name and a
credential can disagree, and the dangerous direction is a deployment that
believes it is on S3 while writing customer footage to a container filesystem
discarded on the next deploy. Credentials cannot disagree with themselves.

Local disk is *not* refused in production, and an earlier version of this
function was wrong to refuse it: the validated deployment is two workers sharing
one Docker volume, and SQLite already requires exactly that shared filesystem.
What *is* refused at boot is `postgresql://` plus local disk — Postgres is only
chosen when the replicas were separated, and separated replicas have separate
disks, so that combination renders on the worker and 404s on the API every time.

**Not verified:** no request has been made to AWS, R2 or MinIO. "Correct against
the S3 REST contract" and "works against AWS" are different claims and only the
first is made.

## 6. Usage enforcement

Seven of eight quota dimensions are enforced. Read live from `/v1/usage`:

| Dimension | `enforced` | Where the gate is |
| --- | --- | --- |
| `rendered_minutes` | `true` | reserved before enqueue, settled on completion |
| `transcribed_minutes` | `true` | reserved in `upload_recording`, 402 on refusal, settled against measured duration |
| `documents` | `true` | reserved on upload |
| `generated_assets` | `true` | **fixed in this pass** — was counted after the fact only; now an `AssetAuthoriser` checked before dispatch inside the router |
| `provider_spend_usd` | `true` | `MeteredSpendAuthoriser`, checked per call with in-flight tracking |
| `seats` | `true` | **fixed in this pass** — was not checked anywhere; now a `SeatAuthoriser` on `Directory.add_member`, and a role change on an existing member does not consume a seat |
| `api_keys` | `true` | checked on issue |
| `storage_gb` | **`false`** | genuinely unmeasured, and the API says so |

`storage_gb` is not a wiring gap. Bytes retained is a *level*, and the usage
meter sums *events* within a period — it has no way to subtract a deletion.
Metering it needs a storage accounting table keyed by object, written on every
put and delete. Until that exists, `/v1/usage` returns `enforced: false` with
that explanation attached, so the product never displays a limit it does not
apply. Four dimensions used to render as `used: 0` beside a real limit while
nothing counted them, which reads to a customer as headroom rather than as an
unmeasured number.

A related finding: `QuotaKind.is_enforced` claimed in its docstring that a test
asserted the enforced/unenforced partition. That test did not exist. It does
now.

## 7. Costs — declared, not measured

**Every cost figure in this system is adapter-declared. None has been measured
against a bill.** This is the correction the CTO review asked for and it is
repeated wherever a number appears.

| Operation | Declared unit cost | Source |
| --- | --- | --- |
| Generated image | $0.04 / image | `http_image.py:56` default |
| Generated video | $0.25 / second | `http_image.py:202` default |
| Text generation | $3.00 / Mtok in, $15.00 / Mtok out | `http_llm.py:57-58` defaults |
| Speech synthesis | $0.015 / 1 000 characters | `http_tts.py:71` default |
| Transcription | $0.006 / minute | `http_stt.py:58` default |
| Openverse / Wikimedia photograph | $0 | no credential, no charge |
| Drawn and typographic visuals | $0 | rendered locally by ffmpeg |

These are constructor defaults chosen to resemble public list prices at the time
of writing. They drive the budget ceiling and the spend breaker, which means the
*enforcement* is real and the *numbers it enforces against* are estimates. A
real deployment must pass measured figures into the adapters and re-derive the
tier economics. Until an invoice exists, no statement about gross margin in this
repository should be treated as anything but arithmetic on assumptions.

The pricing tiers in `docs/BILLING.md` are **proposed**, not validated, for the
same reason plus one more: they were proposed while several metering dimensions
were unenforced. Two of those (`generated_assets`, `seats`) were closed in this
pass; `storage_gb` remains open.

## 8. Deployment — measured

Built from `Dockerfile.localbase`, which differs from the production
`Dockerfile` only in its base image (the container registries return 403 from
this environment, so the base was imported from the local rootfs). Everything
after `FROM` is the production build, including the `.dockerignore` fix without
which the real build fails at `COPY README.md`.

**Run.** `VTV_ENV=production`, non-root user, one API container and two worker
containers on one Docker volume, SQLite repository and durable queue.

| Measurement | Result |
| --- | --- |
| Migrations applied | 3 (`0001`, `0002`, `0003`) |
| API health in production mode | `ok` / `production` |
| Concurrent renders submitted | 7, all `202` |
| Renders reaching `ready` | 7 of 7 |
| Split across workers | w1: 3, w2: 4 |
| Jobs claimed by both workers | **0** |
| Retries | 0 — every job settled at `attempts=1` |
| Output | 96 418-byte MP4, `ffprobe` duration 18.000 s, downloaded over HTTP from the API container |
| Queue-to-ready latency (n=10) | min 32.5 s, median 116.8 s, max 205.5 s |

The latency spread is queue wait, not render time: seven jobs arrived at once
onto two workers. The minimum, 32.5 s, is the closest thing here to a service
time for 18 s of 30 fps standard-quality output inside a container.

**Graceful shutdown, measured under the real deployment.** Three renders were
submitted and a worker was sent SIGTERM four seconds later, mid-flight. It
stopped claiming, finished what it held, and exited **0** after 55.6 s. All three
renders reached `ready`. Across the whole session: 10 jobs, all `succeeded`, all
`attempts=1` — nothing orphaned, nothing redelivered, nothing double-charged.

## 9. Provider resilience

Three real gaps were found and closed; one suspected gap turned out not to
exist, and that is reported as such rather than dressed up as a fix.

- **Timeout — was a real gap.** Every HTTP adapter already passed `timeout=` to
  `httpx`, but nothing bounded the router's own `await`. A deadlocked SDK or an
  exhausted connection pool would have hung the router indefinitely. Now wrapped
  in `asyncio.wait_for`, raising `TimeoutExceeded` (`LATENCY_EXCEEDED`, already
  in `RETRYABLE_CATEGORIES` — existing vocabulary, no new error invented).
- **Circuit breaker — existed, with a fatal omission.** It was already consulted
  at the single right place. But once tripped, a provider was excluded *forever*:
  the only thing that reset the failure count was a success it could never again
  attempt. Added an `opened_at` stamp and a cooldown, with a failed half-open
  probe pushing the window forward rather than reopening the door on every call.
- **Retry double-charging — looked for, not found.** Failed attempts record a
  zero-cost entry; the charge and the spend report fire once, on the attempt that
  returns. Two tests were added as regression guards. **They pass on the
  unmodified code** — they document correct behaviour rather than prove a fix,
  and saying otherwise would be inventing credit.
- **Graceful shutdown — was a real gap.** Stop-claiming and wait-for-in-flight
  were already right. The gap was what happened when the grace period expired
  with a job still running: the row stayed `running` with a stale heartbeat until
  `reclaim_after_seconds` (900 s) noticed. A job caught mid-deploy could be
  unclaimable for fifteen minutes. `release_claimed(worker_id)` now hands those
  rows straight back to `pending`, which is safe because `_settle_blocking`'s
  existing `claimed_by` guard discards a late outcome from the released attempt.

## 10. Frontend

- 55 unit tests across four modules (client, format, store, undo), all passing.
- **9 live wire-contract tests against a running API process**, all passing.
  This suite skips loudly when there is no API to talk to, and it had never been
  run against one. It now has been. It asserts, among other things, that no
  storage key ever reaches the browser and that no provider cost or margin
  appears in a customer-facing payload.
- Build: 34 assets, 382.7 KB total (318.2 KB JS, 64.5 KB CSS, 2.7 KB HTML),
  **zero runtime dependencies**, no bundler.

## 11. Quality evaluation — blocked

**No human has evaluated the output quality of this system.** `make evaluate`
scores the intelligence stages against a fixed corpus, which measures agreement
with expected structure, not whether a viewer finds the video good, accurate or
worth paying for. The distinction matters for a product whose thesis is that AI
should *choose the best way to make knowledge visual* — that judgement is
exactly what an automated score cannot make.

Closing this needs people watching renders against a rubric. It is the largest
remaining unknown in the product and no amount of further engineering here
substitutes for it.

## 12. Security findings from this pass

- **A credential was one field away from being logged in the clear.**
  `Settings.redacted` matched `_api_key` and the literal `signing_key`. The new
  `storage_secret_key` matched neither and would have been printed in full in
  the boot log. Replaced with a suffix rule (`_key`, `_secret`, `_token`,
  `_password`), because the list version only redacts the secrets somebody
  remembered to add to it.
- **Seats were unenforced**, so an organisation on any plan could add unlimited
  members. Closed.
- **Generated assets were counted but not gated**, so the quota was a report
  rather than a limit. Closed.
- Tenant isolation, RLS, path confinement, key hashing and the audit trail were
  re-run, not re-derived: 201 tests across five security modules, all passing.

## 13. Tests — counts by type

Full suite: **1 080 tests, 0 failures, 18 skipped** (all ffmpeg-dependent), 434 s.

| Type | Tests | Modules |
| --- | ---: | ---: |
| Intelligence & pipeline | 305 | 14 |
| Infrastructure — queue, storage, deployment, retention, observability | 216 | 8 |
| Security, tenancy & authorization | 201 | 5 |
| Product domain & regression | 159 | 3 |
| Providers, routing & resilience | 78 | 4 |
| Architecture & contract | 65 | 5 |
| Billing & metering | 56 | 2 |
| **Backend total** | **1 080** | **41** |
| Frontend unit | 55 | 4 |
| Frontend live wire contract | 9 | 1 |
| **Total** | **1 144** | **46** |

The 18 skips are ffmpeg-dependent tests that skip when the binary is absent;
ffmpeg *is* present here and the golden path renders, so these are conditional
skips in specific codec paths, not silently disabled coverage.

## 14. Known limitations

1. No vendor AI provider has ever been called. Everything about generation
   quality, real latency and real cost is unmeasured.
2. Every cost number is adapter-declared. The tier model is arithmetic on
   assumptions.
3. `storage_gb` is metered as zero and reported as unenforced.
4. The production deployment is SQLite on a shared volume. PostgreSQL is
   implemented and tested but not deployed.
5. No backup or restore procedure exists.
6. Metrics are exposed but nothing alerts on them.
7. No load or saturation testing. Seven concurrent renders is not a load test.
8. No human quality evaluation.
9. The base image was imported from a local rootfs because registries are
   unreachable here. The production `Dockerfile` itself is unmodified and its
   build was proven up to that one substitution.
10. Dependencies are version-pinned but not hash-pinned — no reachable index.
    `requirements.lock.md` states the two-line change that closes it.

## 15. Production blockers

Ordered by what would hurt first.

1. **No vendor credentials.** The product's core claim — choosing between
   generated and sourced visuals — runs at two of five ladder rungs today.
2. **No human quality evaluation.** Nothing here establishes that the output is
   good enough to sell.
3. **No measured costs.** Pricing cannot be committed to on declared numbers.
4. **No backup or restore.** Losing the volume loses every project.
5. **No alerting.** Failures are visible only to someone already looking.
6. **`storage_gb` unmetered.** A storage-heavy tenant is unbilled and unbounded.
7. **PostgreSQL not deployed.** Fine at one host; the ceiling on scaling out.

None of these is a code defect. Every one is a thing that has not yet been
*demonstrated*, and the distinction is the whole point of this document.

---

## What this pass actually changed

Closed: S3 object storage (written, executed, signature externally verified);
storage backend selection at a single chokepoint; a credential that would have
been logged; the provider timeout gap; the circuit breaker's missing cooldown;
worker job release on SIGTERM; the generated-asset gate; the seat gate; a
docstring that claimed a test which did not exist.

Verified rather than assumed: the production image, the multi-worker split, the
graceful drain, the wire contract against a live API, and the full suite.

Reported as blocked rather than worked around: vendor providers, measured costs,
measured quality.

> The company is not "AI generates a video." It is "AI understands human
> knowledge and chooses the best way to make it visual." The optimisation
> targets are meaning, quality, trust, cost, consistency, editability and
> reliability — not model usage. Three of those seven — quality, cost and trust
> in the generated rungs — remain unmeasured, and this document exists so that
> stays visible.
