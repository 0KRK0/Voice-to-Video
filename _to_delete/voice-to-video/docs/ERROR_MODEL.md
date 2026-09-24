# Error model

## Two commitments

**Nothing is ever merely absent.** Every artefact has an explicit status. A
missing visual with a reason attached can be worked around; a missing visual with
no reason attached is a bug that first appears in the finished MP4.

**Failure is local.** One scene failing to acquire a visual must not fail the
project (Rule 8). The video still renders, the narration is still heard, and the
storyboard shows exactly which shot needs attention.

## Statuses

```
PENDING     created, not started
PROCESSING  in flight
READY       usable by the next stage
FAILED      terminal, with an ErrorInfo saying why
RETRYING    failed, will be attempted again
EXPIRED     was ready, swept by retention policy
DELETED     removed on request
```

`Status.is_terminal` and `Status.is_usable` are properties on the enum, so
callers ask a question rather than maintaining their own list of which states
mean what.

Contracts enforce coherence between status and content: a `READY` recording must
have measured audio properties; a `READY` generation result must carry outputs
*and* name its provider; a `FAILED` anything must carry an `ErrorInfo`. You
cannot construct a document that claims success without evidence of it.

## Categories drive behaviour

| Category | Meaning | Retry? |
| --- | --- | --- |
| `VALIDATION` | Input or model output failed its schema | no |
| `PROVIDER` | Upstream misbehaved: 5xx, timeout, malformed | **yes** |
| `PROVIDER_REFUSED` | Provider up but declined: quota, filter | no |
| `POLICY` | Our own rules said no: licence, budget | no |
| `NOT_FOUND` | Referenced thing is gone or expired | no |
| `TIMEOUT` | Exceeded its latency budget | **yes** |
| `INTERNAL` | A bug on our side | no |

Only `PROVIDER` and `TIMEOUT` are retried. The rest will fail identically on a
second attempt, so retrying them spends money and user patience for nothing.

`ErrorInfo.of()` derives `retryable` from the category, so the decision is made
in one place rather than at each raise site.

## Two audiences, two messages

```python
ErrorInfo(
    code=ErrorCode.PROVIDER_UNAVAILABLE,
    category=ErrorCategory.PROVIDER,
    message="HTTP 503 from vendor-x pool eu-west-1, attempt 2",   # engineers
    user_message="A service we depend on is having trouble.",     # the user
    context={"attempt": 2, "scene_id": "scn_..."},                # structured
)
```

`message` goes to logs and traces. `user_message` reaches the person who spoke
and must never leak provider names, prompts, stack traces or internal
identifiers. `context` carries non-sensitive structured facts and never user
content (`docs/SECURITY.md`).

## Codes are a public contract

`ErrorCode` values appear in API responses and metrics. They may be **added to**
but must not be renamed, because dashboards, alerts and eventually customers
depend on them.

## The degradation ladder

When a visual cannot be produced as planned, the system descends rather than
gives up:

```
generated_video
      ↓  budget exceeded
generated_image
      ↓  provider refused
licensed_media
      ↓  nothing suitably licensed
programmatic typography     ← cannot fail
```

Each step taken is recorded as a `DegradationStep` on the clip:

```python
DegradationStep(
    from_strategy="generated_video",
    to_strategy="generated_image",
    reason=DegradationReason.BUDGET_EXCEEDED,
)
```

This makes finished videos auditable. Given an MP4 we can say which shots are the
system's first choice and which are compromises — which is the raw material for
the evaluation system, and for telling the user *"this shot used a simpler visual
because generation timed out"* instead of leaving them to wonder.

Reasons: `PROVIDER_FAILED`, `BUDGET_EXCEEDED`, `LATENCY_EXCEEDED`,
`LICENSE_UNACCEPTABLE`, `NO_SUITABLE_ASSET`, `QUALITY_REJECTED`,
`SAFETY_REFUSED`.

## The bottom of the ladder

`PlaceholderClipSource` is the controlled failure state. When every rung is
exhausted, the timeline gets a placeholder rather than a hole:

- the video renders,
- the narration is heard,
- the storyboard flags the shot,
- `Timeline.placeholder_count` makes the failure rate measurable.

A placeholder is a bad outcome. A crashed project is a much worse one, and a
silently dropped scene — where the narration plays over a black frame with no
record of why — is the worst of the three because nobody finds out.

## Exceptions

`VTVError` is the base; every subclass carries a serialisable `ErrorInfo`.

```
VTVError
├── ValidationFailed     schema violation
├── ProviderError        upstream failure (retryable)
├── ProviderRefused      upstream declined
├── PolicyViolation      licence, content, or rule
├── BudgetExceeded       cost ceiling
├── NotFound             missing or expired
├── TimeoutExceeded      latency ceiling (retryable)
└── RenderFailed         rendering
```

Boundaries — API handlers, job workers — catch `VTVError` and turn `.info` into a
response or a status update. **Anything that is not a `VTVError` reaching a
boundary is by definition a bug** and is reported as `INTERNAL_ERROR` with a
generic user message, never a stack trace.

## Stage failure

Failures at different stages have different blast radii, and the pipeline treats
them accordingly:

| Stage | If it fails |
| --- | --- |
| Capture | Fatal. No audio, no product. Tell the user immediately. |
| Transcription | Fatal. Retry across providers first. |
| Understanding | Fatal, but degradable: a simpler segmentation still yields a video. |
| Scene planning | As above. |
| Visual direction | Per scene. A scene with no plan gets a typography default. |
| Asset resolution | Per scene. Descend the ladder. |
| Composition | Fatal — but it is our own deterministic code, so a failure here is a bug to fix, not a condition to handle. |
| Rendering | Retryable. The timeline is saved; re-render costs nothing but compute. |

The pattern: **fatal early, local late**. Failures before meaning is established
stop the project because there is nothing to proceed with. Failures after it
affect one shot, because by then the system knows what it is trying to say and
has other ways to say it.

## What is tested

`tests/test_generation_and_errors.py` covers retryability by category, the
separation of engineer and user messages, status coherence on results, and the
refusal to charge for a cached result. `tests/test_timeline.py` asserts that a
timeline containing a placeholder is still renderable — the executable form of
Rule 8.
