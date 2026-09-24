# Production readiness

Stage 30. This is the audit, written to be read by someone deciding whether to
put real customers on this system. It is deliberately unflattering where being
unflattering is accurate.

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
captions comes out. Verified in this environment on every change:

| | |
| --- | --- |
| Tests | 500, all passing |
| Lint | `ruff check .` clean |
| Types | `mypy --strict` clean across 110 modules |
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
None of them fakes a success.

| Capability | Module | Blocked by |
| --- | --- | --- |
| Speech to text | `adapters/speech/http_stt.py` | No network, no credential |
| Speech synthesis | `adapters/speech/http_tts.py` | No network, no credential |
| Text generation | `adapters/text/http_llm.py` | No network, no credential |
| Image / video generation | `adapters/images/http_image.py` | No network, no credential |
| Asset search | `adapters/assets/openverse.py` | No network |
| Remote media fetch | `adapters/assets/http_fetcher.py` | No network |
| PostgreSQL schema | `adapters/repository/sqlite.py`, `queue/durable.py` | No server |

With no provider registered the router raises `PROVIDER_UNAVAILABLE`, the scene
degrades, the video still renders and the event stream records it. That is the
behaviour we want in production too, and it is exercised on every run here.

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
