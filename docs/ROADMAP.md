> **Status after remediation (2026-08-13):** all eleven P0 and six of seven P1
> findings from `docs/AUDIT_2026-08-13.md` are closed. The per-stage downgrades
> below were correct when written; `docs/FINAL_REPORT.md` carries the current
> state and `docs/REMEDIATION.md` the evidence for each. The one stage still
> genuinely open at P1 is **27 (scalability)** — the deployment scales to one
> machine until PostgreSQL lands (P1-5).

> **Status after the 19 August 2026 pass:** `docs/FINAL_GAP_AUDIT.md` is the
> current source of truth and wins over anything below it disagrees with.
> Closed in that pass: real S3 object storage (implemented, executed,
> signature externally verified); the retention/orphan-sweep bug; two
> usage-metering gaps (`generated_assets`, `seats`); a credential that would
> have been logged in the clear; provider-side resilience (router timeout,
> circuit-breaker cooldown, graceful SIGTERM drain); and, contrary to Stage
> 30's row below, **a deployment artifact now exists** — a real Docker image
> was built and a real two-worker deployment was run and measured, including a
> measured graceful shutdown. Still blocked, and not expected to close from
> inside this environment: vendor AI provider credentials, measured provider
> costs, and human quality evaluation. Still open by scope, not by blocker:
> `storage_gb` metering, backup/restore, alerting, and a PostgreSQL production
> deployment (the schema and RLS are implemented and tested against a live
> server; nothing in the running deployment talks to it). See "Near-term" and
> the P2 list below.

# Roadmap

Thirty-one stages (0 through 30). The whole roadmap has been attempted; the
table below states honestly how far each one actually got.

The order is not arbitrary. Each stage produces something the next one consumes,
and each is buildable and testable without the ones after it. The rule we hold to
is: **do not start a stage until the one before it is genuinely done.**

## Status

> **Revised 2026-08-13 after a static architecture audit.** The table below was
> previously scored on implementation. It is now scored on *whether the
> composition root uses the implementation*. Several stages moved backwards.
> Full detail and the remediation plan: **`docs/REMEDIATION.md`**.
> The audit itself: **`docs/AUDIT_2026-08-13.md`**.

**The golden path works on one node.** A recording goes in; a playable MP4 with
synchronised captions comes out. `make demo` proves it on a fresh checkout. It
**cannot currently run as two replicas** without losing rate limiting, quota
enforcement, job identity and artifact retrieval.

### Closed stages

~~0 contracts~~ · ~~1 capture~~ · ~~4 scene engine~~ · ~~7 generation router~~ ·
~~11 audio and captions~~ · ~~16 evaluation~~ · ~~17 cost optimisation~~

Seven of thirty-one. Every other stage remains open.

| Stage | Name | State | What is not real |
| --- | --- | --- | --- |
| 0 | Product and architecture contract | **complete** | |
| 1 | Voice capture | **complete** | |
| 2 | Speech intelligence | **partial** | No transcription without a credential; the dev aligner needs a supplied script |
| 3 | Semantic understanding | **complete (rules)** | The model-backed engine is written but unexercised |
| 4 | Scene engine | **complete** | |
| 5 | Visual Director | **complete (rules)** | The model-backed director is written but unexercised |
| 6 | Asset intelligence | **partial** | Adapters written; needs network to run |
| 7 | Generation router | **complete** | No real generation provider is reachable here |
| 8 | Animation engine | **partial** | Six primitives of a planned seventeen |
| 9 | Scene composition | **complete** | Composite strategy not executable yet |
| 10 | Timeline and rendering | **ORANGE** ↓ | Non-deterministic, not resumable; runs in the API event loop (P0-4, P3-1) |
| 11 | Audio and captions | **complete** | No word-level highlighting yet |
| 12 | Storyboard UI | **complete** | Single-file HTML, no build step |
| 13 | Visual editing | **partial** | "Try another approach" only |
| 14 | Project persistence | **ORANGE** ↓ | No migrations; nullable tenant column; deletion leaves storage (P0-3, P0-11, P1-7) |
| 15 | Production infrastructure | **RED** ↓ | Unauthenticated destructive sweep; in-process queue; results in memory (P0-2, P0-5, P0-8) |
| 16 | Evaluation system | **complete** | Deterministic metrics only |
| 17 | Cost optimisation | **complete** | Cache, budgets, ledger |
| 18 | Creator beta | **partial** | Project history; no accounts |
| 19 | Business and education | **partial** | Text entry works; no document ingest |
| 20 | Platform and API | **partial** | `/v1/visualize` returns a plan, not a render |
| 21 | Universal input layer | **YELLOW — PARTIAL** ↓ | Inspection bypassed off the HTTP path; no parser timeouts; keys not tenant-scoped (P1-2, P1-3) |
| 22 | Real-time voice | **partial** | Streaming transport not built; narration synthesis port exists and degrades to silence |
| 23 | Visual consistency engine | **ORANGE — PARTIAL** ↓ | Bible built and reviewed but **never applied or persisted** (P1-1) |
| 24 | Factuality and grounding | **RED — BLOCKED** ↓ | **`LlmVisualDirector` bypasses the gate**, and is primary in production (P0-1) |
| 25 | Security and privacy hardening | **RED — BLOCKED** ↓ | Unauthenticated sweep; tenancy fails open; `paths.py` unused; limiter process-local (P0-2, P0-3, P0-6) |
| 26 | Billing and usage | **ORANGE — PARTIAL** | Reservations process-local; `expire_reservations()` never called; no processor (P0-6, P0-7) |
| 27 | Scalability | **RED — BLOCKED** ↓ | Durable queue **unused**; renders block the event loop; cannot run two replicas (P0-4, P0-5, P0-10) |
| 28 | Reliability and disaster recovery | **RED — BLOCKED** ↓ | Every recovery guarantee lives in code with no production callers (P0-5) |
| 29 | Enterprise foundation | **ORANGE — PARTIAL** | Isolation fails open; no encryption at rest; deletion incomplete; no SSO (P0-3, P1-7, P2-1, P2-2) |
| 30 | Production readiness | **RED — OPEN**, deployment artifact closed 2026-08-19 | Audit delivered: **31/100**, at the time it was scored. Since then a real Docker image was built (`Dockerfile.localbase`, the production `Dockerfile` after `FROM`) and a real two-worker deployment was run and measured — see `docs/FINAL_GAP_AUDIT.md` §8. P0-11 (no deployment artifact) is closed; vendor credentials, measured costs and human quality evaluation are not, and are tracked below rather than under this stage's original P0 list. |

"Partial" always names what is missing. Nothing here is marked complete because
a mock returned a 200 — and after the audit, nothing is marked complete because
a test passed either. **A stage closes only when the composition root uses it.**
Arrows (↓) mark stages the audit downgraded.

### What is genuinely blocked by this environment

No network, no PyPI, no npm, no GPU. That makes six things unexecutable rather
than unwritten: real transcription, real speech synthesis, real image and video
generation, real asset search, a payment processor and a malware scanner. Each
has a written adapter behind a port, each is marked `STATUS:` in its module
docstring, and in every case the *absence* is what the system reports rather
than a fabricated success.

### Near-term — what is actually next

Taken from `docs/FINAL_GAP_AUDIT.md` §14 and §15, in the order that document
gives for what would hurt first if a real customer landed on this system
today. None of these is blocked by this environment the way the six items
above are — each is buildable or measurable here, or with ordinary vendor
access, and none needs a capability this sandbox structurally lacks.

- **Measured provider costs.** Every cost figure in the system is currently
  adapter-declared, not measured against a bill. The tier model in
  `docs/BILLING.md` is arithmetic on assumptions until real invoices exist.
- **Human quality evaluation.** No human has ever watched this system's output
  against a rubric. `make evaluate` scores structure, not whether a viewer
  finds the video good, accurate or worth paying for — the largest unknown in
  the product.
- **`storage_gb` metering.** The one usage dimension still reported as
  `enforced: false`. It needs a storage accounting table keyed by object,
  written on every put and delete, because bytes retained is a level and the
  usage meter currently only sums events.
- **Backup / restore.** No procedure exists. Losing the storage volume loses
  every project.
- **Alerting.** Metrics are exposed (`/metrics`, structured logs) but nothing
  consumes them and no alert rule exists.
- **PostgreSQL production deployment.** The schema and `FORCE ROW LEVEL
  SECURITY` policies are implemented and tested against a live server (43
  tests); the running deployment is still SQLite on a shared volume, and
  `wiring.repository_path` refuses any non-SQLite URL rather than silently
  substituting a file.

### P2 — not implemented, by decision

These are explicitly out of scope for now, not accidentally missing. None of
them is claimed anywhere in the product or the docs. Per
`docs/FINAL_GAP_AUDIT.md` §3:

SSO, SCIM, audit export, DSAR (data subject access requests), encryption at
rest, data residency, an admin console, price/version history, org-level
controls, worker orchestration, and disaster recovery are all **NOT
IMPLEMENTED, by decision.**

## Stage 0 — Product and architecture contract ✅

Documentation, contracts, ports, error model, testing strategy, repository
structure. Everything downstream depends on these and nothing depends on
anything downstream.

Delivered: twelve documents, thirteen contract modules, eight provider ports,
eleven exported JSON Schemas, a complete worked example, 143 tests, and
executable architecture governance.

## Stage 1 — Voice capture ✅

Browser `MediaRecorder` to object storage, and a `Recording` in the database.

- microphone permission, recording state, live duration, stop;
- direct-to-storage upload via signed URL;
- server-side probing of real audio properties (never trust the client);
- `Recording` created, validated, persisted.

Done when: a person can press a button, speak, stop, and a valid `Recording` with
measured properties exists in storage.

Explicitly not in scope: transcription, any UI beyond the capture control,
accounts.

## Stage 2 — Speech intelligence

Audio to `Transcript`, with word-level timings where the provider supplies them.

The first real adapter, and therefore the first test of whether the port design
holds. Two providers from the start — one is not an abstraction, it is a wrapper.

Done when: a recording becomes a validated `Transcript` whose segment timings
align with the audio, through at least two interchangeable providers.

## Stage 3 — Semantic understanding

`Transcript` to `Understanding`: entities, relations, semantic units, intents.

Structured output validated against the exported schema. The first place a
language model's output enters the system, and therefore the first place the
"model output is untrusted input" rule earns its keep.

Done when: the transistor narration produces an `Understanding` a human would
agree with, and malformed model output is rejected rather than absorbed.

## Stage 4 — Scene engine

`Understanding` to `SceneGraph`. Where Rule 7 is either honoured or lost.

Done when: two sentences making one point become one scene, reliably, across a
corpus of a few dozen recordings.

## Stage 5 — Visual Director

`SceneGraph` to `VisualPlan`. The heart of the product.

Done when: the strategy mix on a corpus of explainers looks like the posture in
`docs/AI_PROVIDER_POLICY.md` — mostly drawn, some licensed, generation used where
it earns its place — and each decision's rationale survives human review.

## Stage 6 — Asset intelligence

Search, licence filtering, provenance capture, download, dedupe. Openverse and
Wikimedia Commons first.

Done when: assets arrive with complete provenance, unlicensed material never
enters the system, and empty results degrade gracefully.

## Stage 7 — Visual generation router

Capability-driven selection, caching, retry, fallback, budget enforcement.

Done when: a provider outage produces cheaper videos rather than failed projects,
and the cache hit rate on a repeated corpus exceeds 90%.

## Stage 8 — Animation engine

Remotion components for every `VisualPrimitive`, plus the primitives the
Director keeps wanting and not having.

The compounding asset. Every primitive added here permanently moves a class of
meaning from "generate approximately" to "draw exactly".

Done when: every primitive renders beautifully at every supported aspect ratio,
in every style profile.

## Stage 9 — Scene composition

Plan plus assets to resolved clips: fit policies, camera motion, transitions,
overlays, degradation recording.

## Stage 10 — Timeline and rendering

`Timeline` to MP4 through Remotion. Preview and standard quality, progress
streaming, cancellation.

**This is the first end-to-end milestone.** A person speaks and watches the
result. Everything before it is scaffolding.

## Stage 11 — Audio and captions

Narration track, caption cues from real word timings, burn-in and sidecar,
attribution card.

## Stage 12 — Storyboard UI

The story made visible and editable: narration, visual, source, licence,
rationale, and the actions to change any of it.

## Stage 13 — Visual editing

Regenerate, replace, change approach, adjust scene boundaries. Editing meaning,
not frames.

## Stage 14 — Project persistence

Temporary and saved modes, lifecycle enforcement, deletion, regeneration from
stored reasoning.

## Stage 15 — Production infrastructure

Auth, rate limiting, observability, tracing, alerting, deployment, scaling the
render fleet.

## Stage 16 — Evaluation system

The scored corpus. Understanding quality, segmentation quality, visual relevance,
synchronisation, coherence, factual accuracy, cost, latency.

This is when the product starts improving on a measured curve rather than on
opinion, and it is the point at which the visual-intelligence asset becomes
legible as an asset.

## Stage 17 — Cost optimisation

Cache tuning, provider arbitrage, importance-weighted budget allocation, learned
strategy selection informed by Stage 16's data.

## Stage 18 — Creator beta

Real users, real recordings, real disappointment. Where we learn which of these
assumptions were wrong.

## Stage 19 — Business and education

Longer content, terminology and glossaries, brand style profiles, batch
processing, team workspaces.

## Stage 20 — Platform and API

```
POST /v1/visualize
```

The engine, exposed. Nothing in the current architecture prevents this, and
nothing before Stage 20 should be built specifically to enable it.

## Rules of engagement

1. Do not start a stage until its predecessor is done.
2. Do not build placeholder services to make a diagram look complete.
3. Every stage ships with tests and passes `make check`.
4. Every stage reports: what changed, what was tested, what risks remain.
5. Architectural changes are proposed and discussed before they are made.
6. Working foundations are not rewritten without a concrete reason.


## Stage 21 — Universal input layer ✅

Voice was the wedge; the engine takes meaning from anywhere. Six parsers —
markdown/text, HTML, CSV/JSON/XLSX, DOCX, PPTX, PDF — normalise into a
`SourceDocument` of typed blocks, which becomes a `Transcript`. Stages 3 to 30
never learn which kind of input they are serving.

Structure survives parsing, which is the whole point: a heading stays a heading
so the scene engine sees the author's own topic boundary; a table stays a table
so the Visual Director can chart the author's own figures. Provenance survives
too — every block knows its page or slide, which is what Stage 24 needs and what
an enterprise customer asks for.

Format is decided by magic bytes, never by the extension or the declared type.

## Stage 22 — Real-time voice ◐

**Built.** A `SpeechSynthesisProvider` port, an HTTP adapter for it, and a
narration service that runs through the same router — so synthesis is cached,
costed, budgeted and subject to the fallback ladder like everything else. When
synthesised audio disagrees with the transcript's estimated timings, the
transcript is scaled onto the audio, because the audio is what the viewer hears.

**Not built.** Streaming transport. Live partial transcripts and progressive
scene planning are a transport problem before they are a pipeline problem, and
no streaming provider is reachable from here.

## Stage 23 — Visual consistency engine ✅

The `VisualBible` records every decision that must hold across scenes: one
colour and one label per recurring entity, reserved from a fixed palette in a
deterministic order. Re-rendering a project reproduces the video the user
approved rather than one that resembles it.

A binding marked `USER` or `BRAND` is never revised. That single rule is what
makes iterative editing safe.

## Stage 24 — Factuality and grounding ✅

Every number, date and named place a programmatic visual draws must trace to the
transcript, the extracted quantities or a source table. A chart with one
invented value is refused whole — plotting the supported subset would silently
change the shape of the claim. Refusal is a recorded degradation, so the video
still renders and the event stream says why the chart is missing.

Fetched photographs and generated images are deliberately not checked: they make
no precise quantitative claim.

This is fidelity to the source, not truth about the world, and we say so.

## Stage 25 — Security and privacy hardening ✅

`src/vtv/security/` is one directory a reviewer can read end to end:
authentication, authorisation, API keys, rate limits, request bounds, SSRF
defence, path-traversal defence, upload inspection, redaction and an append-only
audit log.

Everything fails closed. The two capabilities this environment cannot provide —
malware scanning and password hashing — *refuse* rather than wave input through.

## Stage 26 — Billing and usage ◐

Plans as a table, quotas metered per calendar month, reserve-then-settle so
concurrency cannot overshoot, idempotency enforced by a unique index so a
retried job bills once. Every usage record carries what the providers actually
charged us, so gross margin per customer is a query rather than a guess.

No payment processor is reachable, so invoice *lines* are produced and no
invoice is ever sent.

## Stage 27 — Scalability ◐

A durable SQLite-backed job queue: atomic claims, priority ordering, delayed
retries, back-pressure through a bounded worker pool. Two workers on one
database provably never run the same job twice.

The rate limiter is in-process and says so: behind a load balancer each process
enforces the limit independently.

## Stage 28 — Reliability and disaster recovery ◐

Crash recovery through heartbeats and reclaim, exponential backoff with
deterministic jitter, a dead-letter state that preserves the final error, and
`replay()`. Idempotency survives a restart because it is a unique index rather
than an in-memory dictionary.

## Stage 29 — Enterprise foundation ◐

Organisations, users, memberships, roles, capabilities, API keys, audit export,
data residency and retention — all as contracts the repositories enforce. SSO is
a field on `User` and a documented integration point, not a working connection
to an identity provider.

## Stage 30 — Production readiness

`docs/PRODUCTION_READINESS.md` is the audit: what is proven, what is written but
unexecutable here, and what a deployment must add before taking real customers.
