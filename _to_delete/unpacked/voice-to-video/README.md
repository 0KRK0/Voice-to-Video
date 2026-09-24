# Voice to Video

**Speak naturally. The system understands what you mean and turns your ideas into
a visual story.**

```
speech → understanding → story → visual intelligence → video
```

This is not a text-to-video wrapper. For every idea in a recording the system
asks a different question — *given this meaning, what is the best way to show
it?* — and answers it with a photograph, a chart, a timeline, animated type, a
generated image, or a combination. Choosing well among those is a reasoning
problem, and it is the product.

Read [`docs/VISION.md`](docs/VISION.md) first.

---

## Status: the golden path works

Speech goes in; a playable MP4 with synchronised captions comes out.

```
254 tests · ruff clean · mypy --strict clean · 11 exported JSON Schemas
10/10 evaluation cases passing · no network or credentials required
```

Per-stage status, including exactly what is *not* real, is in
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## Quick start

```bash
pip install -e ".[dev,server,render,providers]"
make check     # lint, types, schema drift, 254 tests, evaluation corpus
make demo      # the whole pipeline → ./var/demo/demo.mp4   (needs ffmpeg)
make serve     # API + web UI on http://localhost:8000
```

`make demo` prints:

```
  topic            transistor
  structure        6 segments -> 7 units -> 5 scenes
  visuals          {'programmatic': 5}
  captions         10 cues
  placeholders     0
  uncovered        0 gaps
  cost             $0.0000
  render time      32.2s

video: ./var/demo/demo.mp4  (0.7 MB)
```

Six spoken segments become seven ideas become **five** scenes, because two
sentences making one point are one visual idea. Every shot is drawn by our own
animation engine, so the video costs nothing to produce. That is the thesis of
the company, executable.

## What is real without credentials

Understanding, scene grouping, visual direction, the animation engine,
composition, timeline, captions, rendering, the API, the storyboard and
persistence are all real and run offline. Transcription, asset search and
generation need providers; with none configured the Visual Director's fallback
ladder descends to visuals the system draws itself, the video still renders, and
every descent is recorded. See [`docs/RUNNING.md`](docs/RUNNING.md).

**Nothing here fakes a provider.** With no image generator configured the router
raises and the scene degrades — it does not return a placeholder pretending to be
a generation.

## Layout

```
docs/                the engineering constitution — read before building
schemas/             exported JSON Schema (generated, committed, drift-checked)
apps/web/            capture + storyboard UI (one file, no build step)
src/vtv/contracts/   every document in the pipeline
src/vtv/ports/       every interface to the outside world
src/vtv/pipeline/    the stage implementations
src/vtv/animation/   the visual primitives, drawn
src/vtv/adapters/    vendor implementations — the only place vendor code lives
src/vtv/evaluation/  the scored corpus
tests/               254 tests
```

## The pipeline

```
Recording  →  Transcript  →  Understanding  →  SceneGraph
                                                    ↓
RenderJob  ←   Timeline    ←     Asset      ←   VisualPlan
```

Each stage consumes one validated document and produces another. Nothing shares
mutable state. Every stage is therefore independently testable, independently
replaceable, resumable after failure, and auditable from a pixel back to the
spoken word.

## Rules that are enforced, not just written down

| Rule | Enforced by |
| --- | --- |
| No vendor SDKs in the core | `tests/test_architecture_boundaries.py` parses every import |
| Contracts never reach up into ports | same |
| Unknown licence means unusable | `Permission.UNKNOWN` fails closed, `tests/test_asset_licensing.py` |
| Scraped media may never enter | `AssetSource.WEB_SCRAPE` raises on construction |
| One sentence is not one scene | `MIN_SCENE_SECONDS`, `tests/test_example_transistor.py` |
| Generation is the exception | `VisualPlan.strategy_mix()`, asserted at ≤25% |
| One failed visual never kills a project | `PlaceholderClipSource`, `tests/test_timeline.py` |
| No model-authored code is ever executed | animation specs are typed data, asserted field-by-field |
| Committed schemas match the code | `python -m vtv.schema_export --check` in CI |
| Vendor code lives only in `adapters/` and `api/` | import-boundary test, scoped to core packages |
| Every primitive the Director can pick can be drawn | `test_every_declared_primitive_has_a_renderer` |
| Charts and timelines only use stated values | `factual_safety` metric, target 1.0 |

## Documentation

| Document | What it settles |
| --- | --- |
| [VISION](docs/VISION.md) | What we are building and why it is defensible |
| [PRODUCT](docs/PRODUCT.md) | The experience and what "done" means |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | How the system is put together |
| [SCENE_MODEL](docs/SCENE_MODEL.md) | How speech becomes a story |
| [VISUAL_DIRECTOR](docs/VISUAL_DIRECTOR.md) | How meaning becomes an image |
| [ASSET_PROVENANCE](docs/ASSET_PROVENANCE.md) | Licensing, rights, attribution |
| [STORAGE_POLICY](docs/STORAGE_POLICY.md) | What we keep, and for how long |
| [AI_PROVIDER_POLICY](docs/AI_PROVIDER_POLICY.md) | Ports, routing, caching, budgets |
| [SECURITY](docs/SECURITY.md) | Privacy, consent, untrusted model output |
| [ERROR_MODEL](docs/ERROR_MODEL.md) | Statuses, degradation, controlled failure |
| [TESTING](docs/TESTING.md) | How a non-deterministic system is tested |
| [ROADMAP](docs/ROADMAP.md) | Twenty-one stages, and how far each got |
| [RUNNING](docs/RUNNING.md) | How to run it, and what needs credentials |
| [DECISIONS](docs/DECISIONS.md) | Choices made, and what was rejected |

## Development

```bash
make test            # pytest if available, unittest otherwise
make lint            # ruff
make types           # mypy --strict
make schemas         # regenerate exported JSON Schemas
make schemas-check   # fail on contract drift
make evaluate        # score the intelligence stages against the corpus
make demo            # the golden path, end to end
make serve           # API and web UI
make check           # everything CI runs
```

The **core** has exactly one runtime dependency: pydantic. Keeping it at one is a
design constraint, not an accident — every provider SDK, HTTP client and database
driver belongs to an adapter, behind a port, and a test fails if one appears
anywhere else. The optional extras (`server`, `render`, `providers`, `postgres`)
map one-to-one onto capabilities you can choose not to install.
