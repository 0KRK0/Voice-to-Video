"""Shared vocabulary for every provider port.

A *port* is an interface the core defines and the outside world implements. The
core depends on the port; the adapter depends on the port; neither depends on the
other. That is the whole trick, and it is what keeps a vendor's SDK from ending
up in the middle of the Visual Director.

Every provider must describe itself through :class:`ProviderCapabilities`. This
is not documentation — it is the input to the routing decision. The generation
router picks a provider by comparing declared capability, price, latency and
**data policy** against what a request requires. Which means two things worth
saying plainly:

* Cost optimisation (Section 28) becomes a computation over data we already hold,
  rather than a hard-coded preference that rots the moment prices change.
* A provider that retains or trains on user input can be *structurally* excluded
  from handling voice, because the exclusion is a field comparison rather than a
  promise in a policy document (Section 27).
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import Field

from vtv.contracts.base import UsdAmount, VTVModel


class DataPolicy(VTVModel):
    """What a provider does with what we send it.

    Defaults are pessimistic. A provider whose policy we have not verified is
    treated as if it retains and trains on input, which excludes it from the
    voice path until someone establishes otherwise.
    """

    retains_input: bool = True
    trains_on_input: bool = True
    retention_days: int | None = None
    #: Where processing happens, for data-residency commitments.
    region: str | None = Field(default=None, max_length=32)
    #: Whether a signed agreement covering personal data is in place.
    dpa_in_place: bool = False

    @property
    def is_acceptable_for_user_voice(self) -> bool:
        """Whether raw user speech may be sent to this provider."""
        return not self.trains_on_input and self.dpa_in_place


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ProviderHealth(VTVModel):
    """A provider's current state, as observed by us rather than as advertised.

    The router reads this before dispatching. Circuit-breaking on our own
    measurements is what turns a provider outage into a slightly cheaper-looking
    video instead of a queue of failed projects.
    """

    status: HealthStatus = HealthStatus.HEALTHY
    consecutive_failures: int = Field(default=0, ge=0)
    observed_latency_p50_ms: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=200)


class ProviderCapabilities(VTVModel):
    """Everything the router needs to decide whether to use a provider."""

    #: Stable slug recorded on every result, e.g. ``"acme-image-v2"``. Appears in
    #: data and metrics; never branched on by business logic.
    name: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)

    #: Typical price for one unit of work — one image, one second of video, one
    #: minute of audio, one thousand output tokens.
    unit_cost_usd: UsdAmount = 0.0
    unit: str = Field(default="request", max_length=32)
    typical_latency_seconds: float = Field(default=5.0, ge=0.0)

    max_output_width: int | None = Field(default=None, ge=1)
    max_output_height: int | None = Field(default=None, ge=1)
    max_duration_seconds: float | None = Field(default=None, gt=0)
    supports_seed: bool = False
    supports_style_reference: bool = False
    supports_structured_output: bool = False
    supports_word_timestamps: bool = False
    languages: list[str] = Field(default_factory=list, max_length=200)

    #: Most requests this provider will accept at once, or `None` for no limit.
    #:
    #: Declared by the adapter because only the adapter knows its vendor's quota.
    #: The router holds one semaphore per provider and waits rather than firing,
    #: which is the difference between queueing for a slot and being told 429.
    #:
    #: One real render sourced four visuals concurrently, sent four image
    #: requests at once, and was rate-limited. The retries were rate-limited
    #: too, the shot fell to typography, and — before the breaker learned to
    #: ignore throttling — so did the four shots after it. Every one of those
    #: requests would have succeeded a second apart.
    max_concurrency: int | None = Field(default=None, ge=1)

    data_policy: DataPolicy = Field(default_factory=DataPolicy)

    def can_afford(self, units: float, max_cost_usd: float | None) -> bool:
        if max_cost_usd is None:
            return True
        return self.unit_cost_usd * units <= max_cost_usd


@runtime_checkable
class Provider(Protocol):
    """The minimum every provider adapter implements."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Static description of what this adapter can do and what it costs."""
        ...

    async def health(self) -> ProviderHealth:
        """Current observed health. Cheap; must not call the vendor on the hot
        path — adapters report from their own recent-call statistics."""
        ...


__all__ = [
    "DataPolicy",
    "HealthStatus",
    "Provider",
    "ProviderCapabilities",
    "ProviderHealth",
]
