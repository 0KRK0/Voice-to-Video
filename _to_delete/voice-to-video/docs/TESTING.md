# Testing strategy

## The problem

Most of this system's behaviour is non-deterministic, slow and expensive. A
language model decides how to segment speech; an image model produces a picture;
a render takes minutes and money. Testing that naively means a suite nobody runs.

The answer is the same one that shapes the architecture: **separate the decision
from the execution**, then test the decision deterministically and the execution
in isolation.

```
deterministic, fast, free          non-deterministic, slow, costly
-------------------------          -------------------------------
contracts and invariants           does the model segment well?
scene → visual decision            is the generated image good?
decision → request                 is the final video coherent?
timeline → coverage
schema round-trips
architecture boundaries
                                   ↓ evaluation (Stage 16), not unit tests
```

Nearly everything that can break at three in the morning is on the left.

## Layers

### 1. Contract tests — the majority

Every invariant in `src/vtv/contracts/` has a test proving it holds *and* a test
proving the invalid case is rejected. The second half matters more: a schema that
accepts everything validates nothing.

```python
def test_media_shorter_than_its_slot_may_not_simply_be_trimmed(self):
    with self.assertRaises(ValidationError):
        VisualClip(span=TimeSpan.of(0, 8), fit=FitPolicy.TRIM,
                   media_duration_seconds=5.0, ...)
```

### 2. Architecture tests

`tests/test_architecture_boundaries.py` parses the source and fails the build if:

- a vendor SDK, HTTP client or database driver is imported anywhere in the core;
- `vtv.contracts` or `vtv.ports` import anything beyond stdlib and pydantic;
- `vtv.contracts` reaches upward into `vtv.ports`;
- the examples depend on anything but contracts.

An architecture rule that is only written down is a rule that will be broken
within a quarter. These make the rules executable.

### 3. Schema drift tests

`tests/test_schema_export.py` regenerates every JSON Schema and compares it to
what is committed in `schemas/`. Since the frontend generates its types from
those files and language models are handed them as output constraints, drift
would mean two consumers quietly working from a contract that no longer exists.

### 4. The worked example

`src/vtv/examples/transistor.py` builds a complete, internally consistent project
graph by hand: recording through transcript, understanding, scenes, plan, assets
and timeline.

If twelve schemas cannot be assembled into one valid video *by hand*, they will
certainly not be assembled into one by a language model at three in the morning.

`tests/test_example_transistor.py` then asserts the product rules on it:

- **Rule 7** — fewer scenes than segments, and the contrast scene really does
  merge two sentences;
- **Rule 6** — generation is at most a quarter of shots, programmatic at least
  three of five;
- **Rule 8** — every expensive shot has a fallback ladder terminating in
  something that cannot fail;
- **provenance** — the licensed photograph's credit reaches the timeline;
- **economics** — the whole project costs cents;
- **serialisation** — every document survives a JSON round trip byte-identically.

These are executable product requirements. A change that makes it impossible to
express "two sentences, one visual idea" breaks a test.

### 5. Provider mock tests (Stage 2+)

Adapters are tested against recorded fixtures. Because ports are
`runtime_checkable` protocols, a fake is an ordinary object with the right
methods — no inheritance, no framework:

```python
class FakeStorage:
    async def put(self, **kwargs): ...
    ...

assert isinstance(FakeStorage(), StorageProvider)   # structural typing
```

The pipeline is tested end to end against fakes on every commit. Real providers
are exercised by a small, separately-scheduled integration suite.

### 6. Render tests (Stage 10+)

Render a known timeline; assert duration, frame count, resolution and audio
track; compare selected frames against golden images with a perceptual tolerance.
Deterministic because the timeline is fully resolved before the renderer sees it.

### 7. Evaluation (Stage 16)

Not unit tests. A scored corpus of recordings run through the whole pipeline,
measuring: speech understanding, scene segmentation, visual relevance, temporal
synchronisation, narrative coherence, factual accuracy, cost and latency.

Evaluation is how quality improves. Tests are how quality stops regressing. Both
are needed and they are not the same activity.

## Rules

**No test may call a live AI provider.** Unit and integration suites use fakes.
A test that needs a key belongs in the integration suite, which is separately
scheduled and allowed to be slow.

**Every test asserts the failure case.** For every "this is accepted", a "this is
rejected".

**Fixtures are code, not JSON blobs.** `transistor_project()` is type-checked and
fails to build if a contract changes incompatibly. A JSON fixture rots silently.

**Tests are `unittest.TestCase` classes.** pytest runs them natively, and so does
the standard library. The suite therefore works in any environment, including one
where installing pytest is not possible — which is not hypothetical, and cost
nothing to guarantee.

## Running

```bash
make test            # pytest if available, unittest otherwise
make lint            # ruff
make types           # mypy --strict
make schemas-check   # contract drift
make check           # everything CI runs
make example         # print the worked example end to end
```

## Coverage

Line coverage is a weak signal, and we do not target a number. What we do require:

- every model validator has a test that trips it;
- every enum with behaviour attached has a test of that behaviour;
- every public method on a contract has at least one test;
- every architecture rule has a test that fails when it is broken.

Current state: 143 tests, running in under half a second, with no network, no
credentials and no provider access.

That last property is the one worth protecting. A suite that is fast and free is
a suite people run before pushing.
