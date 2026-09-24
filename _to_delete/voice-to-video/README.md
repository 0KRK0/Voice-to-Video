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

## Status: Stage 0 complete

The foundation: contracts, ports, error model, testing strategy, and the
documentation that governs everything built on top.

```
143 tests · ruff clean · mypy --strict clean · 11 exported JSON Schemas
zero network access required · zero provider credentials required
```

Next: **Stage 1 — Voice capture** ([`docs/ROADMAP.md`](docs/ROADMAP.md)).

## Quick start

```bash
pip install -e ".[dev]"
make check      # lint, types, schema drift, tests
make example    # the worked example, end to end
```

`make example` prints:

```
segments 6 -> units 6 -> scenes 5
strategy mix {'programmatic': 3, 'licensed_media': 1, 'generated_image': 1}
expected cost  $0.040
renderable (True, [])
```

Six spoken segments become five scenes — because two sentences making one point
become one visual idea. Three of five shots are drawn by our own engine, one is a
licensed historical photograph, one is generated. The whole project costs four
cents. That single line is the thesis of the company, executable.

## Layout

```
docs/                the engineering constitution — read before building
schemas/             exported JSON Schema (generated, committed, drift-checked)
src/vtv/contracts/   every document in the pipeline
src/vtv/ports/       every interface to the outside world
src/vtv/examples/    worked end-to-end fixtures
tests/               contract, architecture and example tests
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
| [ROADMAP](docs/ROADMAP.md) | Twenty-one stages, in order |
| [DECISIONS](docs/DECISIONS.md) | Choices made, and what was rejected |

## Development

```bash
make test            # pytest if available, unittest otherwise
make lint            # ruff
make types           # mypy --strict
make schemas         # regenerate exported JSON Schemas
make schemas-check   # fail on contract drift
make check           # everything CI runs
```

The core has exactly one runtime dependency: pydantic. Keeping it at one is a
design constraint, not an accident — every provider SDK, HTTP client and database
driver belongs to an adapter, behind a port.
