# Production readiness

Stage 30. This is the audit, written to be read by someone deciding whether to
put real customers on this system. It is deliberately unflattering where being
unflattering is accurate.

> **SUPERSEDED IN PART — 2026-08-13.** The independent static audit
> (`docs/AUDIT_2026-08-13.md`) scored this system 31/100 and found that several
> claims below described code no request ever reached. The remediation is
> tracked in `docs/REMEDIATION.md`, the current state as of that pass is in
> `docs/FINAL_REPORT.md`, and where this document and those two disagree, they
> win. All eleven P0 findings and six of seven P1 findings from that audit were
> closed; the seventh (PostgreSQL with row-level security) was written,
> structurally tested and **not executed** as of that pass.
>
> **SUPERSEDED AGAIN, MORE CURRENTLY — 19 August 2026.**
> `docs/FINAL_GAP_AUDIT.md` is the current source of truth and, where anything
> here disagrees with it, it wins. As of that document: PostgreSQL/RLS is now
> executed against a live server (43 tests) but the *production deployment* is
> still SQLite; real S3 object storage is fully implemented, executed and
> externally verified (SigV4 checked against AWS's own test vector), though no
> request has reached a real bucket; a Docker image was actually built and a
> real two-worker deployment actually validated (§below); the
> `generated_assets` and `seats` usage-metering gaps are closed, `storage_gb`
> remains unenforced; provider-side resilience (router timeout, circuit-breaker
> cooldown, graceful SIGTERM drain) is implemented, wired and tested. Vendor AI
> providers, measured provider costs, and human quality evaluation remain
> **BLOCKED** — no vendor credentials exist in any environment this system has
> run in. See "Production blockers" and "Known limitations" below, both taken
> directly from `docs/FINAL_GAP_AUDIT.md` §15 and §14.

Three columns of truth throughout:

- **PROVEN** — executed in this environment, with a test that fails if it breaks.
- **WRITTEN** — real implementation, cannot be executed here, and the module
  says so in a `STATUS:` line.
- **MISSING** — not built. Named rather than omitted.

---

## What runs, right now, on a fresh checkout

```
make demo
```

A synthetic recording goes in; a playable H.264 + AAC MP4 with synchronised
captions comes out. Verified in this environment on every change (figures
below are as of the pass that produced this section; the current totals are
**1 080 backend / 55 frontend unit / 9 live wire-contract**, per
`docs/FINAL_GAP_AUDIT.md` §13):

| | |
| --- | --- |
| Tests | **740**, all passing |
| Lint | `ruff check .` clean |
| Types | `mypy --strict` clean across **122** modules |
| Schema drift | 18 exported JSON Schemas, checked against the code |
| Evaluation corpus | 10 cases, all passing |
| Voice path | 3 scenes, 5 caption cues, 25.8s MP4, $0.00 |
| Document path | PDF → 4 scenes, 8 caption cues, 25.7s MP4, $0.00 |

The document path renders **mute**, and says so before the job is queued. That
is the honest state, not a defect being hidden.

---

## Proven

### Pipeline
- Capture → transcribe → understand → scene → direct → compose → render, end to
  end, producing a real file.
- Document input takes the identical path from stage 3 onward.
- Six document formats parsed from real files written by the real libraries.
- Fallback ladders: a failed provider degrades the scene, never the project.
- Content-addressed generation cache; per-project and per-scene budgets.

### Security
- Tenant isolation enforced at the query, tested through the real HTTP app on
  every project subresource.
- Capability checks in one function, with a table a reviewer can read.
- API key secrets never stored; constant-time verification; public prefixes.
- SSRF: scheme allowlist, every resolved address checked, redirects re-checked,
  IPv4-mapped IPv6 handled.
- Path traversal: resolved-path containment, symlink-safe, storage keys refused
  rather than sanitised.
- Uploads: identified by magic bytes, executables refused, zip bombs refused by
  declared ratio before extraction, zip-slip refused, active SVG refused.
- Append-only audit log enforced by database triggers.
- Redaction of secrets by key name and by value shape.

### Billing
- Reserve-then-settle so concurrency cannot overshoot a quota.
- Idempotency enforced by a unique index; a retried job bills once.
- Cost recorded beside every billable quantity, so margin is a subtraction.

### Jobs
- Durability across process restart, proven by discarding the queue object.
- Two workers, one database, 40 jobs, each executed exactly once.
- Retries with capped backoff, dead letters preserving the error, replay.

### Grounding and consistency
- Ungrounded charts, timelines, maps, headlines and comparisons refused.
- Extrapolated trends refused; rounding accepted; different numbers refused.
- Locked visual bindings never overwritten; two builds produce one answer.

---

## Written but not executable here

Each has a real implementation behind a port and a `STATUS:` line in its module.
None of them fakes a success. Since this table was written, four of the seven
adapters below have been executed against a conforming server we stand up
ourselves (`tests/test_provider_adapters_live.py`, 11 tests) — that proves each
adapter is correct against the OpenAI-shaped contract it was written for, not
that any vendor works, because no vendor credential exists here to call one
with.

| Capability | Module | Blocked by |
| --- | --- | --- |
| Speech to text | `adapters/speech/http_stt.py` | No network, no credential |
| Speech synthesis | `adapters/speech/http_tts.py` | No network, no credential |
| Text generation | `adapters/text/http_llm.py` | No network, no credential |
| Image / video generation | `adapters/images/http_image.py` | No network, no credential |
| Asset search | `adapters/assets/openverse.py` | No network (Openverse and Wikimedia need no credential, and are live — see `docs/FINAL_GAP_AUDIT.md` §4) |
| Remote media fetch | `adapters/assets/http_fetcher.py` | No network |
| PostgreSQL schema | `adapters/repository/sqlite.py`, `queue/durable.py` | **Executed** against a live PostgreSQL 16 server since this table was written — 43 tests (18 RLS, 25 schema), adversarial. The *production deployment* is still SQLite: `wiring.repository_path` refuses any non-SQLite URL rather than silently substituting a file. |

**Object storage is no longer in this table.** `S3StorageProvider` is a
complete, executed implementation (hand-rolled SigV4, no boto3, 30 tests, the
signature externally verified against AWS's published test vector). What
remains unrun is a request against a real bucket — AWS, R2 or MinIO — which
this environment has no credentials or network access to attempt. See
`docs/FINAL_GAP_AUDIT.md` §5.

With no provider registered the router raises `PROVIDER_UNAVAILABLE`, the scene
degrades, the video still renders and the event stream records it. That is the
behaviour we want in production too, and it is exercised on every run here.

---

## Docker / deployment

A real image was built and a real two-worker deployment was actually run and
measured, not just asserted as consistent with the source. From
`docs/FINAL_GAP_AUDIT.md` §8.

**The base-image caveat, stated honestly.** The image was built from
`Dockerfile.localbase`, which differs from the production `Dockerfile` only in
its base image — the container registries return 403 from this environment,
so the base was imported from the local rootfs instead of pulled. Everything
after `FROM` is the production build, including the `.dockerignore` fix
without which the real build fails at `COPY README.md`. This proves the
production Dockerfile builds and runs correctly once a base image is
reachable; it does not prove the registry pull itself works, because that
step was substituted rather than executed.

**Run.** `VTV_ENV=production`, non-root user, one API container and two
worker containers on one Docker volume, SQLite repository and durable queue.

| Measurement | Result |
| --- | --- |
| Migrations applied | 3 (`0001`, `0002`, `0003`) |
| API health in production mode | `ok` / `production` |
| Concurrent renders submitted | 7, all `202` |
| Renders reaching `ready` | 7 of 7 |
| Split across workers | w1: 3, w2: 4 |
| Jobs claimed by both workers | 0 |
| Retries | 0 — every job settled at `attempts=1` |
| Output | 96 418-byte MP4, `ffprobe` duration 18.000 s, downloaded over HTTP from the API container |
| Queue-to-ready latency (n=10) | min 32.5 s, median 116.8 s, max 205.5 s |

**Graceful shutdown, measured under the real deployment.** Three renders were
submitted and a worker was sent SIGTERM four seconds later, mid-flight. It
stopped claiming, finished what it held, and exited **0** after **55.6 s**.
All three renders reached `ready`. Across the whole session: 10 jobs, all
`succeeded`, all `attempts=1` — nothing orphaned, nothing redelivered,
nothing double-charged.

What this does not prove: sustained throughput or saturation under load
(`docs/FINAL_GAP_AUDIT.md` §2 rates load testing **PARTIAL** — seven
concurrent renders across two workers is a concurrency check, not a load
test), and it does not prove PostgreSQL deployment, which remains
**NOT IMPLEMENTED** in production regardless of this build.

---

## Production blockers

Taken directly from `docs/FINAL_GAP_AUDIT.md` §15, ordered by what would hurt
first. None of these is a code defect — every one is a thing that has not yet
been *demonstrated*.

1. **No vendor credentials.** The product's core claim — choosing between
   generated and sourced visuals — runs at two of five ladder rungs today.
2. **No human quality evaluation.** Nothing here establishes that the output
   is good enough to sell.
3. **No measured costs.** Pricing cannot be committed to on declared numbers.
4. **No backup or restore.** Losing the volume loses every project.
5. **No alerting.** Failures are visible only to someone already looking.
6. **`storage_gb` unmetered.** A storage-heavy tenant is unbilled and
   unbounded.
7. **PostgreSQL not deployed.** Fine at one host; the ceiling on scaling out.

## Known limitations

Taken directly from `docs/FINAL_GAP_AUDIT.md` §14.

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

---

## Missing, and named

| | Why it matters | What to do |
| --- | --- | --- |
| **Password hashing** | `Directory.verify_password` raises | Install `argon2-cffi`, implement against it. Deliberately refuses so no deployment ships a weak KDF by accident. |
| **Malware scanning** | Uploads are structurally validated, not scanned | Wire a scanner into `UploadPolicy.scanner`. With `require_malware_scan=True` and no scanner, uploads are **refused** — never passed as clean. |
| **Shared rate limiter** | In-process; N processes allow N× the limit | Redis or equivalent behind the same `RateLimiter` interface. |
| **Streaming voice** | Stage 22 is a transport problem | WebSocket or gRPC ingest feeding partial transcripts. |
| **Remotion renderer** | `FfmpegRenderer` is behind the `Renderer` port | npm is unavailable here; swapping is a wiring change (D16). |
| **Payment processor** | Invoice lines exist; nothing is charged | Idempotent invoice writer plus reconciliation against the processor's record of truth. |
| **SSO** | `User.sso_subject` exists; no IdP integration | OIDC against the same `Directory.principal_for_user`. |
| **Word-level caption highlighting** | Needs word timings | Arrives free with a real STT provider. |
| **Cross-region DR** | Single-region assumptions throughout | Storage replication and a documented RPO/RTO. |
| **Eleven more animation primitives** | Six of a planned seventeen | Each is a design problem; `VisualPrimitive` is where they get added (D19). |

---

## Before taking a real customer

Ordered by what breaks first, not by effort.

1. **Configure at least one real provider per capability.** Everything degrades
   correctly without them, but the product is a slideshow of drawn visuals.
2. **Replace the password path or commit to SSO only.** Today it raises; do not
   "fix" that with a home-made hash.
3. **Move the rate limiter to a shared store** before running more than one API
   process.
4. **Move the queue and repository to PostgreSQL.** Both DDLs are written and
   kept beside their SQLite equivalents so they cannot silently drift.
5. **Wire a malware scanner** before accepting uploads from the public internet.
6. **Set `VTV_ENV=production`.** It is the switch that turns off the
   development principal. An unauthenticated request must return 401, and there
   is a test that asserts it does.
7. **Put a proxy in front that sets `X-Forwarded-For`,** or stop trusting it —
   a spoofable rate-limit key is no rate limit.
8. **Decide the retention story per plan** and run the sweep on a schedule
   rather than through `/internal/sweep`.
9. **Have a native reader validate RTL output.** Arabic and Hebrew letter
   shaping works; word-level bidirectional ordering is *unverified* and the
   font report says so rather than claiming otherwise.

---

## Honest risk register

| Risk | Severity | Status |
| --- | --- | --- |
| DNS rebinding between validation and fetch | Medium | Documented in `security/net.py`; `resolved_addresses()` exposes what to pin. Not solved — needs socket-level control. |
| In-process rate limiter under multiple workers | High at scale | Documented in the module's `STATUS:` line. |
| Complex-script shaping without libraqm | Medium | Detected and reported per script; degraded scripts are named, not hidden. |
| RTL bidi ordering unverified | Medium | Reported as `quality="fallback"` with an explicit caveat. |
| Grounding false negatives (a wrong number the speaker actually said) | By design | We guarantee fidelity to the source, not truth. Stated to customers. |
| Cost of a runaway generation loop | Low | Per-plan `PROVIDER_SPEND_USD` ceiling on every tier including Enterprise. |
| Audit log growth | Low | Retention split 90/400 days, purge is the only deletion path and cannot reach live entries. |

---

## What this system will not do

Worth stating, because each is a deliberate refusal rather than an omission.

- It will not generate AI video when a drawn visual or a real asset is better
  (Rule 6).
- It will not put a number on screen that the source did not contain (Stage 24).
- It will not treat an unknown licence as permissive (tri-state `Permission`).
- It will not send user voice to a provider whose data policy is unverified.
- It will not execute user-provided or model-generated code. Animation specs are
  typed data, validated before they reach the engine.
- It will not report a capability it does not have.
