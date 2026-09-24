"""Stage 26 — usage, quotas and plans.

Kept separate from `vtv.security.limits` because they answer different
questions. A rate limit protects the *service* from a burst and resets
continuously. A quota protects the *business* from unpriced consumption and
resets on a billing boundary. Conflating them produces a system that either
throttles a paying customer for being fast or lets a free account render for a
month.

Three commitments this package makes.

**Meter the unit the customer is charged for.** Rendered video minutes, not API
calls. A customer disputing an invoice must be able to reconcile it against
something they can observe, which an internal request count is not.

**Record cost as it is incurred, not as it is estimated.** The `CostLedger`
already records what each provider call actually cost; usage records join to
it, so gross margin per project is a query rather than a guess.

**Refuse before spending, not after.** A quota check that runs after the render
has already been paid for is an accounting entry, not a control.
"""

from vtv.billing.plans import (
    PLANS,
    Plan,
    QuotaKind,
    plan_for,
)
from vtv.billing.usage import (
    QuotaVerdict,
    UsageMeter,
    UsagePeriod,
    UsageRecord,
    UsageSummary,
)

__all__ = [
    "PLANS",
    "Plan",
    "QuotaKind",
    "QuotaVerdict",
    "UsageMeter",
    "UsagePeriod",
    "UsageRecord",
    "UsageSummary",
    "plan_for",
]
