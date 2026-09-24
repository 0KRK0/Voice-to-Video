# Architecture

## The organising idea

Every stage of this system consumes one validated document and produces another.
Nothing shares mutable state; nothing reaches sideways into another stage's
internals. The pipeline is a sequence of pure-ish transformations over documents
defined in `src/vtv/contracts/`.

```
Recording  →  Transcript  →  Understanding  →  SceneGraph
                                                    ↓
RenderJob  ←   Timeline    ←     Asset      ←   VisualPlan
```

This buys four things that matter more than they sound:

- **Every stage is independently testable**, because its input is data you can
  write by hand and its output is data you can assert on.
- **Every stage is independently replaceable.** A better scene engine is a drop-in
  if it produces a valid `SceneGraph`.
- **Everything is resumable.** A failed render does not re-run understanding.
- **Everything is auditable.** From a pixel you can walk back to the spoken word.

## Layers

```
                          Client (Next.js)
                                 |
                          API / Gateway
                                 |
        +------------------------+------------------------+
        |                        |                        |
   Voice service          Story service            Project service
        |                        |                        |
  transcription           scene engine              project state
                                 |
                          Visual Director
                                 |
        +------------------------+------------------------+
        |                        |                        |
   Asset engine          Animation engine        Generation router
        |                        |                        |
        +------------------------+------------------------+
                                 |
                          Scene composer
                                 |
                          Timeline engine
                                 |
                             Renderer
                                 |
                               VIDEO
```

**This diagram describes modules, not deployments.** We start as a modular
monolith plus a render worker. Splitting into services before we know where the
real seams and the real load are would buy distributed-systems problems in
exchange for nothing.

The seams are drawn where extraction will one day be easy: each box above talks
to its neighbours through contracts, not through function calls into internals.
When rendering needs its own fleet — and it will, first — it lifts out cleanly
because it already only receives a `Timeline`.

## Dependency rule

```
contracts  ←  ports  ←  adapters  ←  services  ←  apps
```

Dependencies point one way only. Specifically:

- `vtv.contracts` depends on the standard library and pydantic. Nothing else.
- `vtv.ports` depends on `vtv.contracts`. No vendor SDKs, no HTTP clients.
- Adapters depend on ports and on their vendor. Services never import them
  directly; they receive them by injection.
- Nothing depends on an app.

This is enforced by `tests/test_architecture_boundaries.py`, which parses the
source and fails the build on a violation. An architecture rule that is only
written down is a rule that will be broken within a quarter.

## Repository layout

Every directory below exists because something is in it:

```
voice-to-video/
├── docs/                  the engineering constitution
├── schemas/               exported JSON Schema (generated, committed)
├── apps/web/              capture and storyboard UI (one file, no build step)
├── src/vtv/
│   ├── contracts/         every document in the pipeline
│   ├── ports/             every interface to the outside world
│   ├── pipeline/          the stage implementations
│   ├── animation/         the visual primitives, drawn
│   ├── security/          Stage 25: authz, keys, limits, SSRF, uploads, audit
│   ├── billing/           Stage 26: plans, quotas, usage metering
│   ├── adapters/          vendor implementations of ports
│   │   ├── storage/       local filesystem, S3 (skeleton)
│   │   ├── media/         ffmpeg and ffprobe
│   │   ├── speech/        HTTP transcription and synthesis, dev aligner, silence
│   │   ├── text/          HTTP structured generation
│   │   ├── images/        HTTP image and video generation
│   │   ├── assets/        Openverse, Wikimedia, local library, fetcher
│   │   ├── ingest/        Stage 21: six document parsers
│   │   ├── render/        the ffmpeg renderer
│   │   ├── repository/    SQLite persistence
│   │   ├── queue/         in-process and durable job queues
│   │   └── testing.py     stubs, for tests only
│   ├── evaluation/        the scored corpus and its metrics
│   ├── observability/     structured events
│   ├── examples/          worked end-to-end fixtures
│   ├── api/               HTTP surface (Starlette)
│   ├── wiring.py          the only module that knows which adapter is which
│   └── demo.py            the golden path, runnable
├── tests/
├── pyproject.toml
└── Makefile
```

`security/` and `billing/` sit beside `pipeline/` rather than inside it because
they are cross-cutting: every stage is subject to them and none of them is a
stage. They import contracts and nothing else from the core, so the dependency
rule still holds and the boundary tests still enforce it.

No `services/` directory. The seams are enforced by the import-boundary tests,
which work regardless of directory layout; extraction into separate services is a
deployment decision to take when load demands it, not a folder to create in
advance.

### Two deviations from this document, stated plainly

**Starlette, not FastAPI.** FastAPI is a layer over Starlette and remains the
right choice once request/response models multiply; it was not installable in the
environment this was built in. Swapping up is additive.

**ffmpeg, not Remotion.** The `Renderer` port exists precisely so this is a
choice. `FfmpegRenderer` composes every frame itself and pipes raw pixels into
one ffmpeg process that muxes the narration — which gives us transitions,
captions and attributions drawn with the same type system as every other visual,
and makes audio drift structurally impossible. Remotion needs a Node toolchain
that was not available; adding that adapter is now a quality decision rather than
a gap.

## Technology, and why

| Choice | Reason |
| --- | --- |
| Python 3.11+, pydantic v2 | The AI and media ecosystem is Python. Pydantic gives runtime validation at every boundary, which is exactly what a system consuming model output needs. |
| FastAPI (Stage 1) | Async, pydantic-native, so the contracts *are* the API schema. |
| PostgreSQL | Project state is relational and we will want transactions, joins and JSONB in the same query. |
| Redis + a job abstraction | Every meaningful operation is too slow for a request. The queue sits behind `JobQueue` so it is replaceable. |
| S3-compatible object storage | Behind `StorageProvider`. Media never touches application-local disk. |
| Remotion | Programmatic video in React: composition as code, which is what a system that *generates* compositions needs. Behind the `Renderer` port so it is a choice, not a commitment. |
| TypeScript + Next.js | Browser `MediaRecorder` is the capture path; types generated from `schemas/`. |

## Where intelligence lives, and where it does not

The single most important boundary in the system:

```
   INTELLIGENCE                    |            EXECUTION
   understanding, scenes,          |    assets, animation, generation,
   visual direction                |    composition, timeline, rendering
                                   |
   reasons about meaning           |    does exactly what it is told
   uses language models            |    deterministic
   outputs decisions               |    outputs pixels
```

Nothing on the right makes a creative decision. Nothing on the left touches a
pixel. The `Timeline` is the handover: once it exists, every decision has been
made and the renderer's only job is to execute it precisely
(`src/vtv/contracts/timeline.py`).

That separation is why the renderer can be rewritten, parallelised or moved to
different hardware without anyone thinking about scene planning, and why scene
planning can be evaluated without rendering anything.

## Failure model

Local by default. A generation failure degrades one shot down its fallback ladder
and, in the worst case, produces a controlled placeholder. The video renders,
the narration is heard, and the storyboard shows exactly which shot needs
attention. See `docs/ERROR_MODEL.md`.

## Observability (Stage 15, designed for now)

Every document carries the identifiers needed to correlate it: `project_id` on
everything, `scene_id` on generation requests, provider and model recorded on
every result. The metrics that matter are already computable from the contracts:

- cost per project, per stage, per strategy (`GenerationResult.cost_usd`)
- cache hit rate (`GenerationResult.from_cache`)
- degradation rate (`VisualClip.degradation`)
- strategy mix (`VisualPlan.strategy_mix()`)
- placeholder rate (`Timeline.placeholder_count`)

None of that required a metrics library to design. It required putting the right
fields on the right documents, which is cheap now and expensive later.

## Things we deliberately did not do

- **Microservices.** Module boundaries first; extraction when load demands it.
- **An event bus.** Sequential stages with a job queue is honest about what this
  pipeline is. Events can come later if fan-out appears.
- **An ORM in the contracts.** Persistence models are a separate concern; the
  contracts describe the domain, not a table layout.
- **A plugin system.** Ports are enough. A plugin architecture with one
  implementation of each port is ceremony.


## Stages 21 to 30 in the layer diagram

Nothing added after Stage 20 required a new layer, which is the strongest
evidence the original shape was right.

**Stage 21 (universal input)** added `contracts/source.py`, `ports/ingestion.py`
and six adapters. A `SourceDocument` becomes a `Transcript`, and stages 3 onward
are unchanged — the payoff for making the pipeline a chain of documents rather
than a set of coupled services.

**Stage 22 (narration)** added `GenerationKind.SPEECH` and a port. Speech
synthesis routes through the same router as image generation, so it is cached,
costed, budgeted and subject to the same fallback ladder. No new machinery.

**Stage 23 (consistency)** added `contracts/consistency.py` and a stage between
direction and composition. The `VisualBible` is a document like every other, so
it persists, versions and re-renders with the project.

**Stage 24 (grounding)** added `pipeline/grounding.py`, consulted by the Visual
Director inside the existing ladder. A refusal is a `DegradationStep`, which is
the mechanism Stage 8 already used for provider failures.

**Stage 25 (security)** added a package and one call at the top of each route.
The `Principal` is resolved once at the edge and passed down, which is the same
"decide once, pass it in" discipline as `wiring.py`.

**Stage 26 (billing)** added a package that reads the `CostLedger` the pipeline
already maintained. Metering needed no new instrumentation because cost was
already recorded on every provider call.

**Stages 27–28 (durability)** added a second implementation of the existing
`JobQueue` port. The API changed by one line in `wiring.py`.

**Stage 29 (enterprise)** added `contracts/tenancy.py` and one column on
`projects`. Multi-tenancy is a query parameter on the repository rather than a
convention callers must remember.
