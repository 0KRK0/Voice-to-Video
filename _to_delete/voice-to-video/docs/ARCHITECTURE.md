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

Stage 0 creates exactly what Stage 0 needs:

```
voice-to-video/
├── docs/                 the engineering constitution
├── schemas/              exported JSON Schema (generated, committed)
├── src/vtv/
│   ├── contracts/        every document in the pipeline
│   ├── ports/            every interface to the outside world
│   ├── examples/         worked end-to-end fixtures
│   └── schema_export.py  contract → JSON Schema
├── tests/
├── pyproject.toml
└── Makefile
```

The brief sketches a larger tree with `services/`, `packages/`, `apps/` and
`infrastructure/`. Those directories arrive when something goes in them. Creating
empty scaffolding to make a diagram look complete produces a repository that
lies about what exists — the most expensive kind of documentation.

The expected shape as it grows:

```
src/vtv/
├── contracts/     (exists)
├── ports/         (exists)
├── adapters/      Stage 2+  vendor implementations of ports
├── pipeline/      Stage 3+  the stage functions themselves
└── api/           Stage 1+  HTTP surface
apps/web/          Stage 1+  Next.js capture and storyboard UI
apps/renderer/     Stage 10+ Remotion composition and render worker
```

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
