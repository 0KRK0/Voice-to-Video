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
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from vtv.contracts.errors import (
    BudgetExceeded,
    ErrorCategory,
    ErrorCode,
    ProviderError,
    Status,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    GenerationResult,
    SpeechToTextParams,
)
from vtv.observability.events import EventName, EventSink, Timer
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


@dataclass
class _Registered:
    provider: object
    kind: GenerationKind
    consecutive_failures: int = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        capabilities: ProviderCapabilities = self.provider.capabilities  # type: ignore[attr-defined]
        return capabilities

    @property
    def open_circuit(self) -> bool:
        return self.consecutive_failures >= CIRCUIT_THRESHOLD


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
    _providers: list[_Registered] = field(default_factory=list)

    def register(self, provider: object, kind: GenerationKind) -> None:
        self._providers.append(_Registered(provider=provider, kind=kind))

    def providers_for(self, kind: GenerationKind) -> list[object]:
        return [r.provider for r in self._providers if r.kind is kind]

    # -- selection --------------------------------------------------------

    def candidates(self, request: GenerationRequest) -> list[_Registered]:
        """Providers that can serve this request, best first.

        Ordering is cost then latency. Both come from declared capabilities, so
        changing provider prices changes routing without a code change.
        """
        eligible: list[_Registered] = []
        for registered in self._providers:
            if registered.kind is not request.kind:
                continue
            if registered.open_circuit:
                continue
            capabilities = registered.capabilities

            # Raw user speech only goes to a provider whose data policy has been
            # verified. This is the enforcement point for docs/SECURITY.md.
            if isinstance(request.params, SpeechToTextParams) and not (
                capabilities.data_policy.is_acceptable_for_user_voice
            ):
                continue

            if not capabilities.can_afford(1.0, request.budget.max_cost_usd):
                continue
            if (
                request.budget.max_latency_seconds is not None
                and capabilities.typical_latency_seconds
                > request.budget.max_latency_seconds
            ):
                continue
            eligible.append(registered)

        preferred = request.provider_hint
        return sorted(
            eligible,
            key=lambda r: (
                0 if preferred and r.capabilities.name == preferred else 1,
                r.capabilities.unit_cost_usd,
                r.capabilities.typical_latency_seconds,
            ),
        )

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
            return result

        candidates = self.candidates(request)
        if not candidates:
            raise ProviderError(
                f"no provider available for {request.kind.value} within budget "
                "and data-policy constraints",
                code=ErrorCode.PROVIDER_UNAVAILABLE,
            )

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
                try:
                    outcome: GenerationResult = await method(request)
                except VTVError as error:
                    registered.consecutive_failures += 1
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
                    if not error.info.retryable or attempt >= request.max_attempts:
                        break
                    # Exponential backoff, capped. Short in tests because the
                    # multiplier is small; the shape is what matters.
                    await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
                    continue

                registered.consecutive_failures = 0
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
                    raise BudgetExceeded(
                        f"{name} charged {outcome.cost_usd} against a budget of "
                        f"{request.budget.max_cost_usd}"
                    )

                self.cache.put(key, outcome)
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
                return outcome

        raise last_error or ProviderError(
            f"every provider for {request.kind.value} failed"
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
        report: dict[str, str] = {}
        for registered in self._providers:
            name = registered.capabilities.name
            if registered.open_circuit:
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
    "CIRCUIT_THRESHOLD",
    "GenerationCache",
    "GenerationRouter",
    "is_retryable",
]
