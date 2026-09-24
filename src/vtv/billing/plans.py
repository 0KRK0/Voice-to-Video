"""What each plan includes.

A table, not a class hierarchy and not a set of `if tier ==` branches. Pricing
changes far more often than code should, and every plan-shaped conditional
scattered through business logic is a place a pricing change can go wrong
silently.

Nothing in the pipeline reads a tier name. It reads a `Plan` and asks whether a
number is under a limit. That is what lets a bespoke enterprise contract be a
row rather than a code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from vtv.contracts.tenancy import PlanTier

#: Unlimited. `None` would work but reads ambiguously at call sites — "no limit"
#: and "limit not configured" are different things and must not share a value.
UNLIMITED = float("inf")


class QuotaKind(str, Enum):
    """What is being counted.

    Each is a thing the customer can observe for themselves, which is what makes
    an invoice defensible.
    """

    #: The headline unit: finished video the customer can play.
    RENDERED_MINUTES = "rendered_minutes"
    #: Speech sent for transcription, whether or not a render followed.
    TRANSCRIBED_MINUTES = "transcribed_minutes"
    #: Documents ingested. Cheap, but not free, and worth bounding.
    DOCUMENTS = "documents"
    #: Images and video bought from a generation provider.
    GENERATED_ASSETS = "generated_assets"
    #: Hard ceiling on what we will spend on this tenant's behalf in a period.
    #: The circuit breaker that stops a runaway loop becoming a runaway bill.
    PROVIDER_SPEND_USD = "provider_spend_usd"
    #: Bytes retained. Storage is the cost that accrues after the work stops.
    STORAGE_GB = "storage_gb"
    SEATS = "seats"
    API_KEYS = "api_keys"

    @property
    def is_enforced(self) -> bool:
        """Does anything in this build actually count this *and* refuse on it?

        On the member rather than in a table each caller has to remember to
        consult, because the failure this exists to stop is a reporting one: a
        quota was declared here with five per-tier limits, surfaced by
        `/v1/usage` beside ``used: 0``, and metered by nothing. The billing page
        was inventing a number, and the only way a reader could tell was to grep
        src/ for a call site.

        Fails closed. A member added tomorrow and wired to nothing answers
        ``False`` — it has to be named in :data:`_ENFORCED` to claim otherwise,
        and naming it there without a chokepoint is caught by
        `tests/test_billing.py::EveryQuotaDeclaresWhetherItIsEnforced`.
        """
        return self in _ENFORCED

    @property
    def enforcement_note(self) -> str | None:
        """Why an unenforced quota is unenforced, and what would fix it.

        ``None`` for the enforced ones. A refusal to claim enforcement is still
        a refusal, so it names the remedy rather than leaving whoever reads
        ``enforced: false`` with nowhere to go.
        """
        if self in _ENFORCED:
            return None
        return _UNENFORCED.get(
            self,
            "This quota has no enforcement status declared in billing/plans.py, "
            "so it is reported as unenforced until one is.",
        )


#: The quotas this build counts *and* refuses on. Membership is earned by a
#: chokepoint in src/, never by having a number in a plan below — the whole
#: point of this set is that the two can disagree and that the disagreement is
#: what gets reported to the customer.
#:
#:  * RENDERED_MINUTES — reserved by `api/app.py` before the job is enqueued and
#:    settled against the measured duration in `jobs.py`.
#:  * TRANSCRIBED_MINUTES — the same pair, against the recording's measured
#:    duration.
#:  * DOCUMENTS — checked and recorded by `api/app.py` on upload.
#:  * PROVIDER_SPEND_USD — authorised per provider call by
#:    `wiring.MeteredSpendAuthoriser` through `pipeline/generation.py`.
#:  * GENERATED_ASSETS — authorised per image/video call by
#:    `billing.usage.GeneratedAssetAllowance` through
#:    `pipeline/generation.py::GenerationRouter.asset_authoriser`, wired in
#:    `wiring.build`. Same shape as the spend breaker, for the same reason: the
#:    router is the only point that can refuse one asset without failing the
#:    whole video.
#:  * SEATS — checked by `security/directory.py::Directory.add_member` through
#:    the injected `SeatAuthoriser`, `wiring.PlanSeatAuthoriser` in any real
#:    deployment.
#:  * API_KEYS — checked against the live key count in `api/app.py`.
_ENFORCED: frozenset[QuotaKind] = frozenset(
    {
        QuotaKind.RENDERED_MINUTES,
        QuotaKind.TRANSCRIBED_MINUTES,
        QuotaKind.DOCUMENTS,
        QuotaKind.GENERATED_ASSETS,
        QuotaKind.PROVIDER_SPEND_USD,
        QuotaKind.SEATS,
        QuotaKind.API_KEYS,
    }
)

#: Why each of the rest is not enforced, in words a customer reading their
#: billing page can act on. Every :class:`QuotaKind` must be in exactly one of
#: this mapping and :data:`_ENFORCED`; a test asserts it, so adding a quota
#: forces a decision about whether anything counts it.
_UNENFORCED: dict[QuotaKind, str] = {
    QuotaKind.STORAGE_GB: (
        "Not measured. Bytes retained is a level, not a monthly sum of events, "
        "and the usage meter only sums events within a period — it has no way "
        "to subtract a deletion. Metering this needs a storage accounting "
        "table keyed by object, written on every put and delete."
    ),
}


@dataclass(frozen=True)
class Plan:
    """One commercial tier, as data."""

    tier: PlanTier
    display_name: str
    monthly_price_usd: float
    quotas: dict[QuotaKind, float]

    #: Beyond quota: refuse, or bill for the excess. Refusing is the default
    #: because a surprise invoice destroys more trust than a blocked render.
    allow_overage: bool = False
    overage_price_per_minute_usd: float = 0.0

    #: Features that genuinely gate on the plan. Named individually rather than
    #: derived from the tier, so "give this one customer SSO" is a field change.
    custom_branding: bool = False
    sso: bool = False
    audit_export: bool = False
    data_residency_choice: bool = False
    priority_rendering: bool = False
    #: Retention ceiling. A plan that keeps content forever is a plan whose
    #: storage cost grows without bound.
    max_retention_days: int = 30

    def limit(self, kind: QuotaKind) -> float:
        """The allowance for one quota. Absent means unlimited."""
        return self.quotas.get(kind, UNLIMITED)

    def allows(self, kind: QuotaKind, used: float, adding: float = 0.0) -> bool:
        return used + adding <= self.limit(kind)

    def remaining(self, kind: QuotaKind, used: float) -> float:
        return max(0.0, self.limit(kind) - used)


#: The published plans. Enterprise is deliberately generous rather than
#: unlimited: a contract without a ceiling is a contract that cannot be
#: capacity-planned, and the spend limit is what stops a compromised enterprise
#: key from costing six figures overnight.
PLANS: dict[PlanTier, Plan] = {
    PlanTier.FREE: Plan(
        tier=PlanTier.FREE,
        display_name="Free",
        monthly_price_usd=0.0,
        quotas={
            QuotaKind.RENDERED_MINUTES: 10.0,
            QuotaKind.TRANSCRIBED_MINUTES: 20.0,
            QuotaKind.DOCUMENTS: 20.0,
            QuotaKind.GENERATED_ASSETS: 0.0,
            QuotaKind.PROVIDER_SPEND_USD: 2.0,
            QuotaKind.STORAGE_GB: 1.0,
            QuotaKind.SEATS: 1.0,
            QuotaKind.API_KEYS: 1.0,
        },
        max_retention_days=7,
    ),
    PlanTier.STARTER: Plan(
        tier=PlanTier.STARTER,
        display_name="Starter",
        monthly_price_usd=29.0,
        quotas={
            QuotaKind.RENDERED_MINUTES: 120.0,
            QuotaKind.TRANSCRIBED_MINUTES: 240.0,
            QuotaKind.DOCUMENTS: 300.0,
            QuotaKind.GENERATED_ASSETS: 200.0,
            QuotaKind.PROVIDER_SPEND_USD: 40.0,
            QuotaKind.STORAGE_GB: 25.0,
            QuotaKind.SEATS: 3.0,
            QuotaKind.API_KEYS: 5.0,
        },
        max_retention_days=90,
    ),
    PlanTier.PROFESSIONAL: Plan(
        tier=PlanTier.PROFESSIONAL,
        display_name="Professional",
        monthly_price_usd=99.0,
        quotas={
            QuotaKind.RENDERED_MINUTES: 600.0,
            QuotaKind.TRANSCRIBED_MINUTES: 1200.0,
            QuotaKind.DOCUMENTS: 2000.0,
            QuotaKind.GENERATED_ASSETS: 1500.0,
            QuotaKind.PROVIDER_SPEND_USD: 250.0,
            QuotaKind.STORAGE_GB: 200.0,
            QuotaKind.SEATS: 10.0,
            QuotaKind.API_KEYS: 20.0,
        },
        allow_overage=True,
        overage_price_per_minute_usd=0.20,
        custom_branding=True,
        max_retention_days=365,
    ),
    PlanTier.BUSINESS: Plan(
        tier=PlanTier.BUSINESS,
        display_name="Business",
        monthly_price_usd=499.0,
        quotas={
            QuotaKind.RENDERED_MINUTES: 3000.0,
            QuotaKind.TRANSCRIBED_MINUTES: 6000.0,
            QuotaKind.DOCUMENTS: 20000.0,
            QuotaKind.GENERATED_ASSETS: 10000.0,
            QuotaKind.PROVIDER_SPEND_USD: 1500.0,
            QuotaKind.STORAGE_GB: 1000.0,
            QuotaKind.SEATS: 50.0,
            QuotaKind.API_KEYS: 100.0,
        },
        allow_overage=True,
        overage_price_per_minute_usd=0.15,
        custom_branding=True,
        sso=True,
        audit_export=True,
        priority_rendering=True,
        max_retention_days=1095,
    ),
    PlanTier.ENTERPRISE: Plan(
        tier=PlanTier.ENTERPRISE,
        display_name="Enterprise",
        monthly_price_usd=0.0,  # negotiated
        quotas={
            QuotaKind.RENDERED_MINUTES: 50000.0,
            QuotaKind.TRANSCRIBED_MINUTES: 100000.0,
            QuotaKind.DOCUMENTS: 500000.0,
            QuotaKind.GENERATED_ASSETS: 200000.0,
            QuotaKind.PROVIDER_SPEND_USD: 25000.0,
            QuotaKind.STORAGE_GB: 20000.0,
            QuotaKind.SEATS: 1000.0,
            QuotaKind.API_KEYS: 500.0,
        },
        allow_overage=True,
        overage_price_per_minute_usd=0.10,
        custom_branding=True,
        sso=True,
        audit_export=True,
        data_residency_choice=True,
        priority_rendering=True,
        max_retention_days=3650,
    ),
}


def plan_for(tier: PlanTier) -> Plan:
    """The plan for a tier. Unknown tiers get the most restrictive one.

    Failing closed: a tier this build does not recognise is more likely a
    downgrade, a data error or a rollback than a customer entitled to more.
    """
    return PLANS.get(tier, PLANS[PlanTier.FREE])


__all__ = ["PLANS", "UNLIMITED", "Plan", "QuotaKind", "plan_for"]
