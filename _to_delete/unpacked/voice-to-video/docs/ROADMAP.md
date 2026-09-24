# Roadmap

Twenty-one stages. The whole roadmap has been attempted; the table below
states honestly how far each one actually got.

The order is not arbitrary. Each stage produces something the next one consumes,
and each is buildable and testable without the ones after it. The rule we hold to
is: **do not start a stage until the one before it is genuinely done.**

## Status

**The golden path works.** A recording goes in; a playable MP4 with synchronised
captions comes out. `make demo` proves it on a fresh checkout.

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
| 10 | Timeline and rendering | **complete** | ffmpeg, not Remotion |
| 11 | Audio and captions | **complete** | No word-level highlighting yet |
| 12 | Storyboard UI | **complete** | Single-file HTML, no build step |
| 13 | Visual editing | **partial** | "Try another approach" only |
| 14 | Project persistence | **complete** | SQLite, not PostgreSQL |
| 15 | Production infrastructure | **partial** | No auth, no durable queue |
| 16 | Evaluation system | **complete** | Deterministic metrics only |
| 17 | Cost optimisation | **complete** | Cache, budgets, ledger |
| 18 | Creator beta | **partial** | Project history; no accounts |
| 19 | Business and education | **partial** | Text entry works; no document ingest |
| 20 | Platform and API | **partial** | `/v1/visualize` returns a plan, not a render |

"Partial" always names what is missing. Nothing here is marked complete because
a mock returned a 200.

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
