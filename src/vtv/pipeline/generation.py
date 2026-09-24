"""Stage 7 — The generation router.

Business logic hands over a `GenerationRequest` and gets a `GenerationResult`.
It never learns which provider ran unless it asks the result (Rule 3).

The router owns the four behaviours that must be identical across every
provider, and that adapters are therefore forbidden from implementing
themselves:

* **caching** — content-addressed on the request, so the same image is paid for
  once no matter how many scenes or renders want it;
* **selection** — by declared capability, data policy, budget and observed
  health, not by a hard-coded preference;
* **retry** — only for categories where a retry can plausibly succeed;
* **fallback** — the next healthy provider, before giving up.

When no provider can serve a request the router raises. That is the correct
outcome: the caller descends its visual fallback ladder and the scene degrades to
something cheaper. Substituting a fake success here would hide a real failure,
which is exactly the failure mode this design exists to prevent.

**The router is also where money is authorised.** Two rules live here and
nowhere else, because this is the last point every paid call passes through:

* a request whose cost ceiling is unknown is refused rather than dispatched;
* before a provider is called, an injected :class:`SpendAuthoriser` is asked
  whether the tenant may still spend.

The second used to have no enforcement anywhere. `QuotaKind.PROVIDER_SPEND_USD`
is described in `billing/plans.py` as "the circuit breaker that stops a runaway
loop becoming a runaway bill", and it was only ever *recorded*, once, when a job
settled — which is after every call the job made. It was a meter, not a breaker.
Putting the check at the job boundary instead would have kept that property: a
single job that loops is authorised once and then spends without limit, and that
is precisely the scenario the comment describes. So the check is here, per call,
where the money actually moves.

A third rule joined them for the same reason: before an image or video call is
dispatched, an injected :class:`AssetAuthoriser` is asked whether the tenant may
still buy a generated asset. `QuotaKind.GENERATED_ASSETS` was in the same state
`PROVIDER_SPEND_USD` used to be in — counted by `jobs._record_generated_assets`
only after a render job settled, which is after every asset that job bought, so
a Free-tier tenant whose allowance is zero could still have every scene
generated. The router is the only point that can refuse one asset without
failing the whole video — everywhere else, refusing means the job cannot start
at all — so this is where the check belongs, exactly as for spend.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from vtv.contracts.errors import (
    BudgetExceeded,
    ErrorCategory,
    ErrorCode,
    PolicyViolation,
    ProviderError,
    Status,
    TimeoutExceeded,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    GenerationResult,
    SpeechToTextParams,
)
from vtv.observability.events import EventName, EventSink, Timer
from vtv.observability.trace import CallRecord, NullTrace
from vtv.pipeline.costs import CostEntry, CostLedger
from vtv.ports.base import HealthStatus, ProviderCapabilities

#: Method name each provider kind is expected to expose.
_METHOD_BY_KIND: dict[GenerationKind, str] = {
    GenerationKind.IMAGE: "generate_image",
    GenerationKind.VIDEO: "generate_video",
    GenerationKind.TEXT: "generate",
    GenerationKind.SPEECH_TO_TEXT: "transcribe",
    GenerationKind.SPEECH: "synthesize",
}

#: After this many consecutive failures a provider is skipped until it recovers.
CIRCUIT_THRESHOLD = 3

#: Errors that mean "you are asking too fast", not "this provider is broken".
#:
#: These deliberately do **not** count toward the circuit breaker, and the
#: distinction is not academic. One real render generated four images at once,
#: was rate-limited three times on the fourth, tripped the breaker, and then the
#: next four shots found no image provider at all — `provider_unavailable` —
#: and fell to typography. A momentary throttle became a third of the video set
#: as text, and the provider was working perfectly the whole time.
#:
#: A breaker exists to stop hammering something that is down. A 429 is the
#: opposite signal: the provider is up, healthy, and telling us its rate. The
#: right response is to wait longer — see `_backoff` — not to declare it dead.
THROTTLING_CODES = frozenset({ErrorCode.RATE_LIMITED})

#: How long a tripped breaker stays open before the next selection gives the
#: provider one more try (a "half-open" probe). Without a cooldown, a provider
#: that trips the breaker never gets another chance: `candidates()` excludes it
#: forever, so `consecutive_failures` can never fall back to zero, because the
#: only place that resets it is a success this provider is no longer allowed to
#: attempt. Long enough that a provider mid-incident is not hammered every
#: request; short enough that "the vendor fixed it five minutes ago" is
#: rediscovered without anyone redeploying.
CIRCUIT_COOLDOWN_SECONDS = 30.0

#: How long to wait after being told we are going too fast.
#:
#: Deliberately far longer than the ordinary retry backoff, which starts at a
#: quarter of a second. One real render retried a rate limit at 370ms, 655ms and
#: 363ms — three requests inside a second and a half, to an endpoint that had
#: just said "too many requests". None of them could have succeeded; all three
#: counted as failures; the shot fell to typography.
#:
#: Doubling from four seconds gives a per-minute quota time to actually roll
#: over, which is the thing being waited for.
THROTTLE_BACKOFF_SECONDS = 4.0
MAX_BACKOFF_SECONDS = 30.0


def _retry_after(error: VTVError) -> float:
    """What the vendor asked us to wait, in seconds.

    Taken from the error's context where an adapter captured a `Retry-After`
    header. Vendors know their own quota windows and we do not, so their number
    wins over any schedule of ours — clamped, because a header is attacker- or
    bug-influenced data and a render must not stall for an hour on one.
    """
    raw = (error.info.context or {}).get("retry_after")
    try:
        return min(MAX_BACKOFF_SECONDS, max(0.0, float(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _backoff(error: VTVError, attempt: int) -> float:
    """How long to wait before the next attempt.

    Two schedules, because two different things are being waited for. An
    ordinary failure is waiting for a transient fault to pass, and a quarter of
    a second doubling is right for that. A rate limit is waiting for a quota
    window to roll over, and a quarter of a second is not waiting at all.
    """
    if error.info.code in THROTTLING_CODES:
        asked = _retry_after(error)
        if asked > 0:
            return asked
        return min(
            MAX_BACKOFF_SECONDS, THROTTLE_BACKOFF_SECONDS * (2 ** (attempt - 1))
        )
    return min(2.0, 0.25 * (2 ** (attempt - 1)))


#: Bound on one provider call when the request states no latency budget of its
#: own. Every HTTP adapter already passes its own timeout to httpx
#: (`adapters/*/http_*.py` all build their client with `timeout=self.timeout_seconds`),
#: but that bounds only the HTTP request *inside* the call. It does nothing for
#: a call that never reaches httpx at all — a provider stub that deadlocks, an
#: SDK that retries internally with no ceiling of its own, a semaphore that
#: never releases. This is the router-level backstop: every call the router
#: makes has a deadline, regardless of what the adapter underneath it does. Set
#: above the slowest configured adapter timeout (300s, speech-to-text) so it
#: never fires before a legitimately slow call would have succeeded anyway.
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 300.0


@runtime_checkable
class SpendAuthoriser(Protocol):
    """Whether a tenant may still spend, and what it has just spent.

    Deliberately not the billing meter itself. The router must not know what a
    plan is, what a quota is or how a period is defined — it knows only that
    something outside it can veto a spend and wants to be told about the ones it
    permits. `vtv.wiring.MeteredSpendAuthoriser` is the implementation that
    connects this to `QuotaKind.PROVIDER_SPEND_USD`.
    """

    def authorise(self, *, organisation_id: str, amount_usd: float) -> None:
        """Raise a :class:`VTVError` if this tenant may not spend ``amount_usd``."""
        ...

    def note_spend(self, *, organisation_id: str, amount_usd: float) -> None:
        """Record money that has just been spent, so the next call sees it."""
        ...


@runtime_checkable
class AssetAuthoriser(Protocol):
    """Whether a tenant may still buy one more generated image or video.

    Same shape as :class:`SpendAuthoriser`, deliberately: the router must not
    know what a plan is, what a quota is or how a period is defined — only that
    something outside it can veto the next generated asset and wants to be told
    about the ones it permits. `vtv.billing.usage.GeneratedAssetAllowance` is
    the implementation that connects this to `QuotaKind.GENERATED_ASSETS`, and
    `vtv.wiring.build` is what supplies it.
    """

    def authorise(self, *, organisation_id: str) -> None:
        """Raise a :class:`VTVError` if this tenant may not buy one more asset."""
        ...

    def note_generated(self, *, organisation_id: str) -> None:
        """Record an asset that has just been bought, so the next check sees it."""
        ...


@dataclass
class _Registered:
    provider: object
    kind: GenerationKind
    consecutive_failures: int = 0
    #: Guards this provider's declared `max_concurrency`. Built on first use
    #: rather than at registration, because a semaphore binds to the event loop
    #: that created it and the router is registered before the loop exists.
    _gate: asyncio.Semaphore | None = None
    #: Monotonic time the breaker tripped, or `None` if it never has (or has
    #: since seen a success). Distinguishes "still inside its cooldown" from
    #: "due for a probe" — both have `consecutive_failures >= CIRCUIT_THRESHOLD`.
    opened_at: float | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        capabilities: ProviderCapabilities = self.provider.capabilities  # type: ignore[attr-defined]
        return capabilities

    def gate(self) -> asyncio.Semaphore | None:
        """The semaphore for this provider's declared concurrency, if any."""
        limit = self.capabilities.max_concurrency
        if limit is None:
            return None
        if self._gate is None:
            self._gate = asyncio.Semaphore(limit)
        return self._gate

    def is_open(self, now: float) -> bool:
        """Whether dispatch must currently skip this provider.

        Called from exactly one place — `candidates()`, the router's only
        selection point — which is what makes this a breaker rather than a
        convention: there is no second call site that could forget to check it.
        A provider past the failure threshold stays skipped until its cooldown
        elapses, at which point it is let back into the candidate list for one
        probe; a failed probe pushes `opened_at` forward again (see `generate`),
        so a provider that is still down keeps being retried on a schedule
        rather than either hammered every call or locked out forever.
        """
        if self.consecutive_failures < CIRCUIT_THRESHOLD:
            return False
        if self.opened_at is None:
            return True
        return now - self.opened_at < CIRCUIT_COOLDOWN_SECONDS


class GenerationCache:
    """Content-addressed result cache.

    In-memory here; the same interface fronts Redis in production. Only
    successful results are cached — caching a failure would turn one bad minute
    into a permanently broken shot.
    """

    def __init__(self) -> None:
        self._entries: dict[str, GenerationResult] = {}

    def get(self, key: str) -> GenerationResult | None:
        return self._entries.get(key)

    def put(self, key: str, result: GenerationResult) -> None:
        if result.status is Status.READY:
            self._entries[key] = result

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class GenerationRouter:
    """Selects a provider, runs it, and accounts for what it cost."""

    events: EventSink
    cache: GenerationCache = field(default_factory=GenerationCache)
    ledger: CostLedger = field(default_factory=CostLedger)
    #: Veto on tenant spend. Optional on the type, mandatory in any deployment
    #: that has a tenant: `vtv.wiring.build` always supplies one, and
    #: `tests/test_billing.py` asserts that it does, so "the assembly forgot"
    #: fails the build rather than showing up on an invoice. It stays optional
    #: here because the router is also used with no tenancy at all — the
    #: evaluation harness and most unit tests construct one directly — and a
    #: required argument would have been satisfied in those places by a
    #: permissive stub, which is a worse outcome than an explicit None.
    spend_authoriser: SpendAuthoriser | None = None
    #: Veto on one more generated image or video. Same optionality and the same
    #: reason as `spend_authoriser`: mandatory in `vtv.wiring.build`, absent in
    #: the evaluation harness and most unit tests, which have no tenant to check
    #: an allowance against.
    asset_authoriser: AssetAuthoriser | None = None
    #: Injected so a test can move the circuit breaker's cooldown without a real
    #: sleep. Defaults to wall-clock monotonic time, which is what every
    #: deployment uses; monotonic rather than `time.time` because a breaker
    #: timing itself against a clock that can jump backwards would stay open
    #: past its cooldown, or reopen early, across a leap-second or NTP step.
    clock: Callable[[], float] = time.monotonic
    #: The local provider trace, or a null object. Off unless a deployment asks
    #: for it, refused in production, and the only place in the system where a
    #: prompt is written to disk — see `observability/trace.py` for why that is
    #: a separate facility from the event stream rather than a field on it.
    #:
    #: It is attached *here* for the same reason spend authorisation is: this is
    #: the one object every paid call passes through, so a trace attached
    #: anywhere else would be a trace with holes in it.
    trace: object = field(default_factory=NullTrace)
    _providers: list[_Registered] = field(default_factory=list)

    def register(self, provider: object, kind: GenerationKind) -> None:
        self._providers.append(_Registered(provider=provider, kind=kind))

    def providers_for(self, kind: GenerationKind) -> list[object]:
        return [r.provider for r in self._providers if r.kind is kind]

    # -- selection --------------------------------------------------------

    @staticmethod
    async def _dispatch(
        registered: _Registered,
        method: object,
        request: GenerationRequest,
        deadline: float,
    ) -> GenerationResult:
        """One provider call, inside that provider's concurrency limit.

        Waiting for a slot is not counted against `deadline`: the timeout bounds
        *the provider's* answer, and a request that spent nine seconds queueing
        behind two others has not been slow, it has been polite. Timing the
        queue would turn a working rate limit into a timeout.

        `wait_for` both bounds the call and cancels it, so a provider that never
        returns cannot go on holding its slot after the router has moved on.
        """
        gate = registered.gate()
        if gate is None:
            return await asyncio.wait_for(
                method(request), timeout=deadline  # type: ignore[operator]
            )
        # The coroutine is created *inside* the gate. Building it first and
        # awaiting it after would leave an un-awaited coroutine behind if the
        # acquisition were cancelled — a RuntimeWarning and a leaked object.
        async with gate:
            return await asyncio.wait_for(
                method(request), timeout=deadline  # type: ignore[operator]
            )

    def candidates(self, request: GenerationRequest) -> list[_Registered]:
        """Providers that can serve this request, best first.

        Ordering is cost then latency, so changing provider prices changes
        routing without a code change. Cost is what *this* request would cost —
        see :meth:`_price_of` — rather than the provider's declared average,
        because image fidelity moves the price of one shot by sixteen times.
        """
        now = self.clock()
        eligible: list[tuple[_Registered, float]] = []
        for registered in self._providers:
            if registered.kind is not request.kind:
                continue
            if registered.is_open(now):
                continue
            capabilities = registered.capabilities

            # Raw user speech only goes to a provider whose data policy has been
            # verified. This is the enforcement point for docs/SECURITY.md.
            if isinstance(request.params, SpeechToTextParams) and not (
                capabilities.data_policy.is_acceptable_for_user_voice
            ):
                continue

            price = self._price_of(registered, request)
            if request.budget.max_cost_usd is not None and (
                price > request.budget.max_cost_usd
            ):
                continue
            if (
                request.budget.max_latency_seconds is not None
                and capabilities.typical_latency_seconds
                > request.budget.max_latency_seconds
            ):
                continue
            eligible.append((registered, price))

        preferred = request.provider_hint
        return [
            registered
            for registered, _ in sorted(
                eligible,
                key=lambda pair: (
                    0 if preferred and pair[0].capabilities.name == preferred else 1,
                    pair[1],
                    pair[0].capabilities.typical_latency_seconds,
                ),
            )
        ]

    @staticmethod
    def _price_of(registered: _Registered, request: GenerationRequest) -> float:
        """What this provider would charge for *this* request.

        A provider whose price varies with what is asked for — image fidelity is
        a sixteen-fold spread — implements :class:`~vtv.ports.ai.PricesRequests`
        and is asked. Everything else keeps its declared capability.

        `except Exception` rather than `except VTVError` because this runs
        inside candidate selection: a pricing method that raised would remove
        every provider from consideration and read to the user as "generation is
        not configured". Falling back to the declared cost is the same answer
        the router gave before this method existed.
        """
        provider = registered.provider
        pricer = getattr(provider, "price_for", None)
        if pricer is None:
            return registered.capabilities.unit_cost_usd
        try:
            return float(pricer(request))
        except Exception:
            return registered.capabilities.unit_cost_usd

    # -- authorisation ----------------------------------------------------

    def _authorised_ceiling(self, request: GenerationRequest) -> float:
        """The most this call may cost, having checked that it may happen.

        Raises rather than returning a verdict, so there is no way to call it
        and ignore the answer. Both rules are here rather than in `generate`
        itself only to keep that method readable; there is exactly one caller.
        """
        ceiling = request.budget.max_cost_usd
        if ceiling is None:
            # Fail closed. `GenerationRequest` fills a backstop ceiling during
            # validation, so this is reached only by a request that skipped
            # validation — `model_copy(update={"budget": Budget()})`,
            # `model_construct`, or a stand-in object in a test. Those are
            # exactly the paths a future call site is most likely to take, and
            # an unknown ceiling is also unanswerable by the authoriser below:
            # "may this tenant spend an unknown amount" has no safe yes.
            raise BudgetExceeded(
                f"a {request.kind.value} request reached the router with no cost "
                "ceiling; attach Budget(max_cost_usd=...) — for a shot, "
                "SceneVisualPlan.budget is the per-scene ceiling the Director "
                "already computed",
                user_message=(
                    "We could not create this part of your video, so we used a "
                    "simpler visual instead."
                ),
            )

        authoriser = self.spend_authoriser
        if authoriser is None:
            return ceiling
        try:
            authoriser.authorise(
                organisation_id=request.organisation_id, amount_usd=ceiling
            )
        except VTVError:
            raise
        except Exception as error:
            # An authoriser that cannot answer — its store is unreachable, it
            # raised on a value it did not expect — must not read as consent.
            # We cannot know what this tenant has spent, so we do not spend.
            raise PolicyViolation(
                f"the spend authoriser failed to answer for "
                f"{request.organisation_id}: {error!r}",
                code=ErrorCode.QUOTA_EXCEEDED,
                user_message=(
                    "We could not check your account's spending limit, so we "
                    "did not start this work. Please try again shortly."
                ),
            ) from error
        return ceiling

    def _note_spend(self, request: GenerationRequest, cost_usd: float) -> None:
        """Tell the authoriser what a call actually cost.

        Called for every charge, including one that broke its own budget: the
        work happened and the invoice will show it, so the next authorisation
        has to see it. Without this the per-call check is answered from the
        usage meter alone, and the meter is only written when a job settles —
        so a job that loops a thousand times would pass a thousand identical
        checks. This is what makes the breaker work *during* a run.
        """
        if self.spend_authoriser is None or cost_usd <= 0:
            return
        self.spend_authoriser.note_spend(
            organisation_id=request.organisation_id, amount_usd=cost_usd
        )

    def _authorise_generated_asset(self, request: GenerationRequest) -> None:
        """Refuse one more generated asset before dispatch, not after.

        Only image and video requests are gated — `QuotaKind.GENERATED_ASSETS`
        is defined as "images and video bought from a generation provider", and
        gating text, speech or transcription here would refuse work this quota
        was never meant to bound. A missing authoriser is not a refusal: most
        callers of this router (the evaluation harness, most unit tests) have no
        tenant and no plan to check against, exactly as for `spend_authoriser`.
        """
        if self.asset_authoriser is None:
            return
        if request.kind not in (GenerationKind.IMAGE, GenerationKind.VIDEO):
            return
        self.asset_authoriser.authorise(organisation_id=request.organisation_id)

    def _note_generated_asset(self, request: GenerationRequest, cost_usd: float) -> None:
        """Tell the authoriser an asset was just bought, so the next check sees it.

        Called from the same two places as `_note_spend`, for the same reason:
        `jobs._record_generated_assets` counts a ledger entry as a generated
        asset whenever it cost money, whether or not the call ultimately raised
        `BudgetExceeded` for overspending its ceiling — the work happened either
        way. Keeping the two call sites identical is what keeps the in-flight
        tally and the settled meter counting the same events.
        """
        if self.asset_authoriser is None or cost_usd <= 0:
            return
        if request.kind not in (GenerationKind.IMAGE, GenerationKind.VIDEO):
            return
        self.asset_authoriser.note_generated(organisation_id=request.organisation_id)

    # -- execution --------------------------------------------------------

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        key = request.cache_key()
        cached = self.cache.get(key)
        if cached is not None:
            result = cached.model_copy(
                update={"from_cache": True, "cost_usd": 0.0, "request_id": request.request_id}
            )
            self.ledger.record(
                CostEntry(
                    kind=request.kind,
                    provider=cached.provider or "cache",
                    model=cached.model,
                    scene_id=request.scene_id,
                    usd=0.0,
                    latency_ms=0,
                    from_cache=True,
                )
            )
            self.events.emit(
                EventName.GENERATION_COMPLETED,
                project_id=request.project_id,
                scene_id=request.scene_id,
                cost_usd=0.0,
                data={"kind": request.kind.value, "from_cache": True},
            )
            self.trace.record(  # type: ignore[attr-defined]
                CallRecord(
                    kind=request.kind.value,
                    provider=cached.provider or "cache",
                    model=cached.model,
                    attempt=1,
                    outcome="cached",
                    cost_usd=0.0,
                    latency_ms=0,
                    from_cache=True,
                    organisation_id=request.organisation_id,
                    project_id=request.project_id,
                    request=request.trace_summary(),
                    response={"outputs": len(cached.outputs)},
                )
            )
            return result

        # Deliberately after the cache lookup: a cache hit costs nothing, and
        # refusing free work would degrade a shot for a tenant who is at their
        # limit but not actually asking to spend anything.
        # Refusals *before* dispatch are traced too. Without this the trace is
        # silent about the calls that never happened, and "budget_exceeded"
        # appears in the event log with nothing explaining whether it was the
        # shot's ceiling, the tenant's plan, or an authoriser that could not
        # answer — three different problems with three different fixes.
        try:
            self._authorised_ceiling(request)
            self._authorise_generated_asset(request)
        except VTVError as refusal:
            self._trace_blocked(request, refusal)
            raise

        candidates = self.candidates(request)
        if not candidates:
            unavailable = ProviderError(
                f"no provider available for {request.kind.value} within budget "
                "and data-policy constraints",
                code=ErrorCode.PROVIDER_UNAVAILABLE,
            )
            self._trace_blocked(request, unavailable)
            raise unavailable

        last_error: VTVError | None = None
        for registered in candidates:
            method = getattr(registered.provider, _METHOD_BY_KIND[request.kind], None)
            if method is None:
                continue
            name = registered.capabilities.name

            for attempt in range(1, request.max_attempts + 1):
                self.events.emit(
                    EventName.GENERATION_STARTED,
                    project_id=request.project_id,
                    scene_id=request.scene_id,
                    data={
                        "kind": request.kind.value,
                        "provider": name,
                        "attempt": attempt,
                    },
                )
                timer = Timer()
                deadline = (
                    request.budget.max_latency_seconds
                    or DEFAULT_PROVIDER_TIMEOUT_SECONDS
                )
                try:
                    # `wait_for` both bounds the call and cancels it on timeout,
                    # so a provider that never returns cannot go on consuming a
                    # connection, a semaphore slot or an event-loop task after
                    # the router has already moved on to the next attempt.
                    try:
                        outcome: GenerationResult = await self._dispatch(
                            registered, method, request, deadline
                        )
                    except TimeoutError as timeout_error:
                        raise TimeoutExceeded(
                            f"{name} did not answer a {request.kind.value} "
                            f"request within {deadline:.1f}s",
                            context={
                                "provider": name,
                                "attempt": attempt,
                                "deadline_seconds": deadline,
                            },
                        ) from timeout_error
                except VTVError as error:
                    # Throttling is not a fault. See `THROTTLING_CODES`.
                    if error.info.code not in THROTTLING_CODES:
                        registered.consecutive_failures += 1
                    if registered.consecutive_failures >= CIRCUIT_THRESHOLD:
                        # Refreshed on every failure past the threshold, not just
                        # the first: a failed half-open probe must push the
                        # cooldown forward again, or a provider that is still
                        # down would be re-probed on every subsequent call
                        # instead of backing off.
                        registered.opened_at = self.clock()
                    last_error = error
                    self.ledger.record(
                        CostEntry(
                            kind=request.kind,
                            provider=name,
                            scene_id=request.scene_id,
                            usd=0.0,
                            latency_ms=timer.elapsed_ms,
                            succeeded=False,
                        )
                    )
                    self.events.emit(
                        EventName.GENERATION_FAILED,
                        project_id=request.project_id,
                        scene_id=request.scene_id,
                        duration_ms=timer.elapsed_ms,
                        data={
                            "provider": name,
                            "attempt": attempt,
                            "code": error.info.code.value,
                            "retryable": error.info.retryable,
                        },
                    )
                    self.trace.record(  # type: ignore[attr-defined]
                        CallRecord(
                            kind=request.kind.value,
                            provider=name,
                            model=registered.capabilities.model,
                            attempt=attempt,
                            outcome=(
                                "refused"
                                if not error.info.retryable
                                else "failed"
                            ),
                            cost_usd=0.0,
                            latency_ms=timer.elapsed_ms,
                            organisation_id=request.organisation_id,
                            project_id=request.project_id,
                            request=request.trace_summary(),
                            response={"code": error.info.code.value},
                            # The vendor's own sentence. This is the field that
                            # tells a rejected parameter apart from a content
                            # refusal, and both arrive as a 400.
                            error=error.info.message,
                        )
                    )
                    if not error.info.retryable or attempt >= request.max_attempts:
                        break
                    # Exponential backoff, capped. Short in tests because the
                    # multiplier is small; the shape is what matters.
                    await asyncio.sleep(_backoff(error, attempt))
                    continue

                registered.consecutive_failures = 0
                registered.opened_at = None
                if (
                    request.budget.max_cost_usd is not None
                    and outcome.cost_usd > request.budget.max_cost_usd
                ):
                    # An adapter that overspends its budget still gets paid — the
                    # work happened — but the result is refused so the ladder
                    # descends rather than the overspend being normalised.
                    self.ledger.record(
                        CostEntry(
                            kind=request.kind,
                            provider=name,
                            model=outcome.model,
                            scene_id=request.scene_id,
                            usd=outcome.cost_usd,
                            latency_ms=outcome.latency_ms,
                            succeeded=False,
                        )
                    )
                    self._note_spend(request, outcome.cost_usd)
                    self._note_generated_asset(request, outcome.cost_usd)
                    raise BudgetExceeded(
                        f"{name} charged {outcome.cost_usd} against a budget of "
                        f"{request.budget.max_cost_usd}"
                    )

                self.cache.put(key, outcome)
                self._note_spend(request, outcome.cost_usd)
                self._note_generated_asset(request, outcome.cost_usd)
                self.ledger.record(
                    CostEntry(
                        kind=request.kind,
                        provider=outcome.provider or name,
                        model=outcome.model,
                        scene_id=request.scene_id,
                        usd=outcome.cost_usd,
                        latency_ms=outcome.latency_ms or timer.elapsed_ms,
                    )
                )
                self.events.emit(
                    EventName.GENERATION_COMPLETED,
                    project_id=request.project_id,
                    scene_id=request.scene_id,
                    duration_ms=timer.elapsed_ms,
                    cost_usd=outcome.cost_usd,
                    data={
                        "kind": request.kind.value,
                        "provider": outcome.provider or name,
                        "model": outcome.model,
                        "from_cache": False,
                    },
                )
                self.trace.record(  # type: ignore[attr-defined]
                    CallRecord(
                        kind=request.kind.value,
                        provider=outcome.provider or name,
                        model=outcome.model,
                        attempt=attempt,
                        outcome="ok",
                        cost_usd=outcome.cost_usd,
                        latency_ms=outcome.latency_ms or timer.elapsed_ms,
                        organisation_id=request.organisation_id,
                        project_id=request.project_id,
                        request=request.trace_summary(),
                        response={
                            "outputs": len(outcome.outputs),
                            "structured": sorted(outcome.structured_output or {}),
                        },
                    )
                )
                return outcome

        raise last_error or ProviderError(
            f"every provider for {request.kind.value} failed"
        )

    def _trace_blocked(self, request: GenerationRequest, error: VTVError) -> None:
        """Record a call that was refused before any provider was asked.

        `provider` is the refusal's origin rather than a vendor, because there
        was no vendor: `router` for "nothing could serve this", `quota` for a
        plan limit, `budget` for this shot's ceiling.
        """
        origin = {
            ErrorCode.QUOTA_EXCEEDED: "quota",
            ErrorCode.BUDGET_EXCEEDED: "budget",
        }.get(error.info.code, "router")
        self.trace.record(  # type: ignore[attr-defined]
            CallRecord(
                kind=request.kind.value,
                provider=origin,
                model=None,
                attempt=1,
                outcome="blocked",
                cost_usd=0.0,
                latency_ms=0,
                organisation_id=request.organisation_id,
                project_id=request.project_id,
                request=request.trace_summary(),
                response={"code": error.info.code.value, "ceiling": request.budget.max_cost_usd},
                error=error.info.message,
            )
        )

    # The router exposes each provider's own method name and forwards to
    # `generate`. That makes it substitutable anywhere a single provider is
    # expected, so every call — including transcription — passes through the
    # cache, the retry policy and the cost ledger rather than around them.
    async def transcribe(self, request: GenerationRequest) -> GenerationResult:
        return await self.generate(request)

    async def generate_image(self, request: GenerationRequest) -> GenerationResult:
        return await self.generate(request)

    async def generate_video(self, request: GenerationRequest) -> GenerationResult:
        return await self.generate(request)

    async def health(self) -> dict[str, str]:
        now = self.clock()
        report: dict[str, str] = {}
        for registered in self._providers:
            name = registered.capabilities.name
            if registered.is_open(now):
                report[name] = HealthStatus.UNAVAILABLE.value
                continue
            try:
                state = await registered.provider.health()  # type: ignore[attr-defined]
                report[name] = state.status.value
            except Exception:
                report[name] = HealthStatus.UNAVAILABLE.value
        return report


def is_retryable(error: VTVError) -> bool:
    return error.info.category in {ErrorCategory.PROVIDER, ErrorCategory.TIMEOUT}


__all__ = [
    "CIRCUIT_COOLDOWN_SECONDS",
    "CIRCUIT_THRESHOLD",
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "AssetAuthoriser",
    "GenerationCache",
    "GenerationRouter",
    "SpendAuthoriser",
    "is_retryable",
]
