# AI provider policy

## The rule

Business logic calls `generate_image(...)`. It never calls a vendor.

```python
# never, anywhere outside an adapter
result = some_vendor.images.create(...)

# always
result = await router.generate(request)
```

Enforced by `tests/test_architecture_boundaries.py`, which parses every file
under `src/vtv` and fails the build if a vendor SDK, HTTP client or database
driver is imported.

## Why this is not over-engineering

Provider abstraction is often premature. Here it is not, for four concrete
reasons:

1. **Prices move fast, in both directions.** Being able to change provider is
   worth real margin, repeatedly.
2. **Providers go down.** A pipeline with thirty generation calls per project
   fails constantly without fallback.
3. **Quality moves fast.** Today's best image model will not be next year's, and
   we should be able to take the improvement in an afternoon.
4. **Different shots want different providers.** Fast and cheap for a background;
   slow and excellent for the opening. That is a routing decision, and it is only
   expressible if routing exists.

## Ports

| Port | Purpose |
| --- | --- |
| `SpeechToTextProvider` | Voice to timestamped transcript |
| `TextGenerationProvider` | Structured reasoning over contracts |
| `ImageGenerationProvider` | Still images |
| `VideoGenerationProvider` | Moving footage |
| `AssetSearchProvider` | Licensed and open media search |
| `StorageProvider` | Object storage |
| `Renderer` | Timeline to MP4 |
| `JobQueue` | Background work |

Defined in `src/vtv/ports/`. All are `runtime_checkable` protocols, so an adapter
needs no inheritance and the router can verify at wiring time that an object
actually satisfies the interface it claims.

## Adapter responsibilities

An adapter **must**:

- translate our provider-independent params into its vendor's API;
- translate the response back into our contract;
- report cost and latency honestly, **including on failure** — a failed call that
  reports zero cost makes cost optimisation impossible;
- raise `VTVError` subclasses, never leak a vendor exception type upward;
- validate structured output against the requested schema before returning it;
- store binary outputs via the storage port and return `ObjectRef`s, never raw
  bytes or a vendor URL that will expire.

An adapter **must not**:

- retry — the router owns retry policy;
- cache — the router owns caching;
- substitute a different model than it advertises;
- decide a request is unimportant enough to silently degrade.

That division exists so that retry, caching and fallback behaviour are written
once and behave identically across every provider, rather than being reimplemented
slightly differently in each adapter.

## Capability-driven routing

Every provider declares itself through `ProviderCapabilities`: unit cost, typical
latency, maximum resolution and duration, seed support, style-reference support,
structured-output support, languages, and its **data policy**.

The router selects by comparing declared capability against what the request
needs:

```
1. filter   providers that can do this kind of work at all
2. filter   providers whose data policy permits this content
3. filter   providers within the request's budget
4. filter   providers currently healthy
5. rank     by fitness, then cost, then latency
6. dispatch to the best; on failure, the next
```

Cost optimisation is then a computation over data we already hold, rather than a
hard-coded preference that rots the first time prices change.

## Data policy is a routing input

`DataPolicy` defaults are pessimistic: `retains_input=True`,
`trains_on_input=True`, `dpa_in_place=False`. A provider we have not vetted is
treated as if it keeps and trains on everything we send it.

```python
policy.is_acceptable_for_user_voice  # not trains_on_input and dpa_in_place
```

Raw user speech may only be sent to a provider that passes that check. The
exclusion is a **field comparison**, not a promise in a policy document — which
means it is enforced at dispatch and testable in CI (`docs/SECURITY.md`).

## Caching

`GenerationRequest.cache_key()` hashes exactly the inputs that determine the
output — kind and params — and deliberately excludes the request id, timestamps,
the scene it belongs to, the budget and the provider hint.

Consequences:

- two scenes needing the same image pay once;
- re-rendering a project after an unrelated edit costs nothing for unchanged
  shots;
- an evaluation run over the same corpus is nearly free after the first pass.

Cached results record the original provider and model but cost zero, and the
contract refuses a cached result that claims a cost.

## Retry and fallback

```
attempt provider A
   ↓ transient failure (5xx, timeout)
retry A with backoff, up to max_attempts
   ↓ still failing
attempt provider B
   ↓ no healthy provider, or budget exhausted
descend the visual fallback ladder
   ↓ ladder exhausted
controlled placeholder — the project still renders
```

Only `PROVIDER` and `TIMEOUT` categories are retried. A validation failure, a
policy refusal or a content-filter refusal will fail identically on retry, so
retrying them just spends money and time (`docs/ERROR_MODEL.md`).

## Budgets

Every request carries a `Budget`. A provider that cannot satisfy it declines
rather than overspending. Project-level budgets are checked against
`VisualPlan.worst_case_cost_usd` — the sum of every fallback ladder — because
checking only the primaries lets a project quietly cost several times its
ceiling.

## Cost posture

The target mix for a typical explainer:

```
programmatic     ~60-70%    effectively free
licensed media   ~20-30%    free to cheap
generated image  ~5-15%     cents
generated video  0-5%       tens of cents to dollars
```

`VisualPlan.strategy_mix()` makes this observable in one line, and it is the
cheapest early warning that the Director has started reaching for expensive
options by reflex. The worked example holds generation to one shot in five, and
`tests/test_example_transistor.py` fails if that ratio drifts above a quarter.

## Structured output only

`TextGenerationProvider` has no "chat" method. Every language-model call in this
system asks for a document that must validate against a named contract from
`schemas/`. There is nowhere in the architecture that consumes free-form model
prose (Rule 2).

The adapter validates before returning, and raises `ValidationFailed` rather than
handing back something malformed. A model that invents a field fails at the
boundary instead of corrupting four stages downstream.

## Provider identity is data

`provider` and `model` are recorded on every result, for reproducibility, billing
attribution and evaluation. They are **data**, never something business logic
branches on. A conditional on provider name anywhere outside the router is a bug.
