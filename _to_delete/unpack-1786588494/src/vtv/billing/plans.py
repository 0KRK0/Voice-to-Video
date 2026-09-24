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
