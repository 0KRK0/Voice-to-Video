"""Deciding how many shots a project may buy, before it buys any of them.

## The problem a per-shot ceiling cannot solve

Until now the budget was divided evenly: each visual could spend a quarter of
the project's ceiling, and the ladder decided per shot whether to spend it. That
is a *limit*, not a *plan*. It answers "may this shot cost this much" and cannot
answer the question a user actually has, which is "how much will this video
cost me".

The difference shows up at scale. Thirteen shots at sixteen cents is two
dollars twenty at the old price and about fifteen cents now; six hundred and
fifty shots — an hour of video — is neither. A per-shot rule lets every shot
through and presents the total afterwards.

## What this does instead

Given a budget and what a generated image actually costs, it works out **how
many shots may reach a paid rung**, and hands back the set that may. Everything
else is capped to the rungs that cost nothing: drawing, the commons, typography.

    650 shots, $10 budget, $0.016 an image  ->  625 may generate
    650 shots, $2  budget, $0.016 an image  ->  125 may generate
     13 shots, $1  budget, $0.25  an image  ->    4 may generate

## Which shots get the money

The longest ones. Screen time is the only signal available here that correlates
with how much a shot matters — a visual held for eight seconds is looked at,
one held for two is glimpsed — and it is a signal the user controls directly by
editing pacing.

This is deliberately not a quality judgement. A ranking that tried to guess
which sentences "deserve" a picture would be a second visual director
disagreeing with the first, and it would be wrong in ways nobody could predict.
Length is dumb, explicable, and the user can change it.

## What it will not do

It will not stop the ladder spending. The router's per-call budget check is
still the enforcement — this is a *plan*, made before any call, and a plan that
also enforced would be two answers to one question. What it does is stop the
expensive rung from being offered at all for shots the budget cannot cover,
which is why the video comes in under budget instead of being truncated at it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

#: No safety factor, deliberately.
#:
#: The first version held back ten per cent against price drift, and the visible
#: effect was that a budget of exactly one image's price bought **zero** images
#: — which is not caution, it is a surprise.
#:
#: The conservatism is already in the price. `plan` is given the provider's
#: *declared* cost, which `HttpImageGenerationProvider.declared_unit_cost`
#: defines as the dearest it could charge across every size it might request.
#: Discounting a worst-case number by a further ten per cent is conservatism
#: about conservatism, and the two are indistinguishable to a user reading a
#: budget that bought less than the arithmetic said it would.
HEADROOM = 1.0


def price_of_one_image(providers: Iterable[object], fidelity: object) -> float:
    """What one generated image costs at a fidelity, before any request exists.

    The planner and the studio both need this, and they must agree: the studio
    tells the user "$2 buys about 125 pictures" and the planner then decides
    which 125. Two implementations of that arithmetic would be two answers to
    the same question, and the user would only find out which was right after
    the render.

    Providers that understand fidelity expose `price_at`; the rest report their
    declared cost, which is what the planner used before fidelity was a choice.

    `max` across providers rather than `min`, deliberately. The router picks the
    cheapest that can serve a request, but when the cheapest is failing or
    circuit-broken the call lands on a dearer one — and a plan built on the
    cheapest would then overspend without anything having gone wrong.
    """
    prices: list[float] = []
    for provider in providers:
        prices.append(_price_from(provider, fidelity))
    return max(prices, default=0.0)


def _price_from(provider: object, fidelity: object) -> float:
    """One provider's price for one image, by the best answer it can give.

    Three fallbacks, in order, and the order is the point:

    1. `price_at(fidelity)` — the published table for the tier being planned.
    2. `declared_unit_cost` — the provider's own worst case across sizes.
    3. `capabilities.unit_cost_usd` — the number every provider must declare,
       because it is the one the router has always refused on.

    The third rung is not decoration. Before it existed the fallback was `0.0`,
    which meant a provider that happened not to implement the two optional
    properties — `StubImageGenerationProvider` is exactly such a provider, and
    so is any adapter somebody adds next — was planned as **free**, and every
    budget it was involved in permitted every shot. A price of zero is a real
    answer only when a deployment genuinely cannot generate, and that case is
    "no providers at all", which `max(..., default=0.0)` already covers.
    """
    at_tier = getattr(provider, "price_at", None)
    if at_tier is not None:
        try:
            return float(at_tier(fidelity))
        except Exception:
            # A provider that cannot price a tier is not a reason to price the
            # project at zero, which is what an escaping exception here would
            # eventually mean.
            pass
    declared = getattr(provider, "declared_unit_cost", None)
    if declared is not None:
        return float(declared)
    try:
        return float(provider.capabilities.unit_cost_usd)  # type: ignore[attr-defined]
    except Exception:
        # Nothing left to ask. Zero here would mean "free", so it has to be
        # something that cannot be silently overspent instead — and the only
        # honest such number is the dearest thing we know how to buy.
        return _DEAREST_KNOWN_IMAGE_USD


#: What to assume for a provider that will not say what it costs.
#:
#: The dearest published price in the product — gpt-image-1 at `high`, 16:9.
#: Erring high makes the plan buy fewer pictures than it could have; erring low
#: makes it buy more than the user agreed to pay for. Only one of those two
#: mistakes arrives as an invoice.
_DEAREST_KNOWN_IMAGE_USD = 0.25


@dataclass(frozen=True)
class Shot:
    """One visual the planner is deciding about."""

    unit_id: str
    #: Seconds on screen. The ranking signal — see the module docstring for why
    #: this and not a quality judgement.
    seconds: float
    #: False when this shot could not generate anyway: material we will not
    #: illustrate, or a named subject whose likeness we will not invent. It
    #: costs nothing, so it must not consume an allowance slot.
    could_generate: bool = True


@dataclass(frozen=True)
class Allowance:
    """How many shots may be bought, and which ones."""

    #: Unit ids permitted to reach a paid rung.
    permitted: frozenset[str]
    #: Shots that could have generated and were held back by the budget.
    withheld: int
    #: What the plan expects to spend if every permitted shot generates.
    projected_usd: float
    budget_usd: float
    price_each_usd: float
    shots: int = 0

    def may_generate(self, unit_id: str) -> bool:
        return unit_id in self.permitted

    @property
    def is_unconstrained(self) -> bool:
        """Whether the budget was large enough that nothing was held back."""
        return self.withheld == 0

    @property
    def headline(self) -> str:
        """One line for the user, in money rather than mechanism."""
        if self.price_each_usd <= 0:
            return "Generated visuals cost nothing on this configuration."
        if self.is_unconstrained:
            return (
                f"About ${self.projected_usd:.2f} for {len(self.permitted)} "
                f"generated visual(s), within a ${self.budget_usd:.2f} budget."
            )
        return (
            f"${self.budget_usd:.2f} covers {len(self.permitted)} generated "
            f"visual(s) at ${self.price_each_usd:.3f} each. The other "
            f"{self.withheld} are drawn, found in the commons, or set as text."
        )

    def as_json(self) -> dict[str, object]:
        return {
            "budget_usd": round(self.budget_usd, 4),
            "price_each_usd": round(self.price_each_usd, 4),
            "shots": self.shots,
            "permitted": len(self.permitted),
            "withheld": self.withheld,
            "projected_usd": round(self.projected_usd, 4),
            "headline": self.headline,
        }


def _how_many(budget_usd: float, price_each_usd: float) -> int:
    """How many whole units a budget buys — in decimal, not binary.

    `2.0 // 0.016` is **124**, not 125, because neither 2.0 nor 0.016 is exactly
    representable in binary and their quotient lands at 124.99999999999999. The
    user's arithmetic says 125 pictures for two dollars and they would be right;
    the float said 124 and silently withheld one.

    A tolerance would paper over it in this case and fail in the next. Both
    numbers here are decimal quantities by nature — a price published in cents,
    a budget typed in dollars — so `Decimal` computes what they mean rather than
    what their binary approximations happen to work out to. `str()` is the
    conversion that preserves the literal a user typed; `Decimal(0.016)` would
    reintroduce the same error it is here to avoid.
    """
    return int(Decimal(str(budget_usd)) // Decimal(str(price_each_usd)))


def plan(
    shots: list[Shot], *, budget_usd: float, price_each_usd: float
) -> Allowance:
    """Which shots may reach a paid rung, given the money available.

    `price_each_usd` is the provider's own declared price for one generated
    image — the dearest it could charge, which is what
    `HttpImageGenerationProvider.declared_unit_cost` reports. Planning against
    an average would put the plan over budget exactly when the expensive case
    happened, which is the case worth planning for.

    A price of zero means generation costs nothing here (a stub, a self-hosted
    endpoint declared free) and every shot is permitted.
    """
    eligible = [shot for shot in shots if shot.could_generate]
    if price_each_usd <= 0:
        permitted = frozenset(shot.unit_id for shot in eligible)
        return Allowance(
            permitted=permitted,
            withheld=0,
            projected_usd=0.0,
            budget_usd=budget_usd,
            price_each_usd=0.0,
            shots=len(shots),
        )

    affordable = _how_many(max(0.0, budget_usd) * HEADROOM, price_each_usd)
    # Longest first. Ties broken by unit id so the plan is the same every time
    # it is computed for the same project — a planner that reshuffled on each
    # render would make two identical renders cost differently.
    ranked = sorted(eligible, key=lambda shot: (-shot.seconds, shot.unit_id))
    chosen = ranked[:affordable]
    return Allowance(
        permitted=frozenset(shot.unit_id for shot in chosen),
        withheld=len(eligible) - len(chosen),
        projected_usd=round(len(chosen) * price_each_usd, 4),
        budget_usd=budget_usd,
        price_each_usd=price_each_usd,
        shots=len(shots),
    )


@dataclass
class UnlimitedAllowance:
    """The allowance when no budget applies. Permits everything.

    A null object rather than `None`, for the same reason `NullTrace` is one:
    the call site is a decision about somebody's money and an
    `if allowance is not None` there is one more thing a future edit can get
    wrong in the expensive direction.
    """

    permitted: frozenset[str] = field(default_factory=frozenset)
    withheld: int = 0
    projected_usd: float = 0.0

    def may_generate(self, unit_id: str) -> bool:
        del unit_id
        return True

    @property
    def is_unconstrained(self) -> bool:
        return True

    @property
    def headline(self) -> str:
        return "No budget set for this project; the deployment ceiling applies."

    def as_json(self) -> dict[str, object]:
        return {"headline": self.headline, "unconstrained": True}


__all__ = [
    "HEADROOM",
    "Allowance",
    "Shot",
    "UnlimitedAllowance",
    "plan",
    "price_of_one_image",
]
