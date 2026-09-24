"""How many shots a project may buy, decided before it buys any.

A per-shot ceiling answers "may this shot cost that much". It cannot answer
"what will this video cost me", which is the question a user actually has, and
the difference only shows at scale: thirteen shots let through one at a time is
a bill you read afterwards; six hundred and fifty is a bill you would have
wanted to see first.
"""

from __future__ import annotations

import unittest

from vtv.contracts.generation import VisualFidelity
from vtv.pipeline.allowance import (
    Shot,
    UnlimitedAllowance,
    plan,
    price_of_one_image,
)


def shots(count: int, seconds: float = 5.0, generable: bool = True) -> list[Shot]:
    return [
        Shot(unit_id=f"vun_{index:04d}", seconds=seconds, could_generate=generable)
        for index in range(count)
    ]


class TheBudgetDecidesHowMany(unittest.TestCase):
    def test_a_generous_budget_permits_every_shot(self) -> None:
        allowance = plan(shots(13), budget_usd=10.0, price_each_usd=0.016)
        self.assertEqual(len(allowance.permitted), 13)
        self.assertTrue(allowance.is_unconstrained)

    def test_a_tight_budget_permits_what_it_can_pay_for(self) -> None:
        # $1 at 25c each is four, exactly as a user would work it out.
        allowance = plan(shots(13), budget_usd=1.0, price_each_usd=0.25)
        self.assertEqual(len(allowance.permitted), 4)
        self.assertEqual(allowance.withheld, 13 - len(allowance.permitted))
        self.assertLessEqual(allowance.projected_usd, 1.0)

    def test_an_hour_of_video_is_planned_rather_than_discovered(self) -> None:
        """650 shots is where a per-shot limit stops being a budget."""
        allowance = plan(shots(650), budget_usd=10.0, price_each_usd=0.016)
        self.assertLessEqual(allowance.projected_usd, 10.0)
        self.assertGreater(len(allowance.permitted), 500)

    def test_a_zero_budget_buys_nothing_rather_than_failing(self) -> None:
        allowance = plan(shots(5), budget_usd=0.0, price_each_usd=0.016)
        self.assertEqual(len(allowance.permitted), 0)
        self.assertEqual(allowance.withheld, 5)
        self.assertEqual(allowance.projected_usd, 0.0)

    def test_a_budget_of_exactly_one_image_buys_one_image(self) -> None:
        """The arithmetic a user does in their head must be the arithmetic.

        An earlier version discounted the budget by ten per cent against price
        drift and this bought *zero*. The conservatism belongs in the price —
        which is already the dearest tier the provider could charge — not
        stacked on top of it.
        """
        allowance = plan(shots(5), budget_usd=0.25, price_each_usd=0.25)
        self.assertEqual(len(allowance.permitted), 1)

    def test_the_arithmetic_is_decimal_not_binary(self) -> None:
        """`2.0 // 0.016` is 124 in binary floating point. The user's own
        arithmetic says 125 and the user is right."""
        allowance = plan(shots(200), budget_usd=2.0, price_each_usd=0.016)
        self.assertEqual(len(allowance.permitted), 125)
        self.assertAlmostEqual(allowance.projected_usd, 2.0)

    def test_the_plan_never_projects_over_the_budget(self) -> None:
        for count, budget, price in [(13, 1.0, 0.25), (650, 10.0, 0.016), (7, 0.05, 0.016)]:
            allowance = plan(shots(count), budget_usd=budget, price_each_usd=price)
            self.assertLessEqual(allowance.projected_usd, budget + 1e-9)

    def test_free_generation_permits_everything(self) -> None:
        allowance = plan(shots(50), budget_usd=0.0, price_each_usd=0.0)
        self.assertEqual(len(allowance.permitted), 50)


class TheMoneyGoesToTheLongestShots(unittest.TestCase):
    """Screen time is the only signal here that tracks how much a shot matters.

    Deliberately not a quality judgement: a ranking that guessed which
    sentences "deserve" a picture would be a second visual director disagreeing
    with the first, wrong in ways nobody could predict. Length is dumb,
    explicable, and the user changes it by editing pacing.
    """

    def test_longer_shots_are_chosen_first(self) -> None:
        mixed = [
            Shot(unit_id="short", seconds=1.5),
            Shot(unit_id="long", seconds=9.0),
            Shot(unit_id="middle", seconds=4.0),
        ]
        allowance = plan(mixed, budget_usd=0.5, price_each_usd=0.25)
        self.assertIn("long", allowance.permitted)
        self.assertNotIn("short", allowance.permitted)

    def test_the_same_project_plans_the_same_way_twice(self) -> None:
        """Two identical renders must not cost differently."""
        tied = [Shot(unit_id=f"vun_{i}", seconds=4.0) for i in range(6)]
        first = plan(tied, budget_usd=0.5, price_each_usd=0.25)
        second = plan(list(reversed(tied)), budget_usd=0.5, price_each_usd=0.25)
        self.assertEqual(first.permitted, second.permitted)

    def test_a_shot_that_could_never_generate_takes_no_slot(self) -> None:
        """It costs nothing, so holding money back for it would withhold a
        picture from a shot that could have used one."""
        mixed = [
            Shot(unit_id="cannot", seconds=99.0, could_generate=False),
            Shot(unit_id="can", seconds=1.0),
        ]
        allowance = plan(mixed, budget_usd=0.25, price_each_usd=0.25)
        self.assertEqual(allowance.permitted, frozenset({"can"}))
        self.assertEqual(allowance.withheld, 0)


class ItSaysWhatItDidInMoney(unittest.TestCase):
    def test_a_constrained_plan_names_the_trade(self) -> None:
        allowance = plan(shots(13), budget_usd=1.0, price_each_usd=0.25)
        headline = allowance.headline
        self.assertIn("$1.00", headline)
        self.assertIn("drawn, found in the commons, or set as text", headline)

    def test_an_unconstrained_plan_gives_the_projection(self) -> None:
        allowance = plan(shots(13), budget_usd=10.0, price_each_usd=0.016)
        self.assertIn("About $0.21", allowance.headline)

    def test_no_budget_says_so_rather_than_inventing_one(self) -> None:
        """The deployment ceiling is an operator's backstop, not a statement
        about one video, and substituting it would report a budget nobody set."""
        allowance = UnlimitedAllowance()
        self.assertTrue(allowance.is_unconstrained)
        self.assertTrue(allowance.may_generate("anything"))
        self.assertIn("No budget set", allowance.headline)


class OneAnswerAboutWhatAPictureCosts(unittest.TestCase):
    """The studio and the planner must agree, or the user finds out afterwards.

    The studio says "$2 buys about 125 pictures" while the slider is moving; the
    planner then decides which 125. Two implementations of that arithmetic would
    be two answers to the same question.
    """

    def provider(self) -> object:
        from vtv.adapters.images.http_image import HttpImageGenerationProvider

        return HttpImageGenerationProvider(
            storage=None,  # type: ignore[arg-type]
            endpoint="https://x/v1",
            api_key="k",
            model="gpt-image-1",
            quality="low",
        )

    def test_each_tier_is_priced_from_the_published_table(self) -> None:
        one = [self.provider()]
        self.assertAlmostEqual(price_of_one_image(one, VisualFidelity.DRAFT), 0.016)
        self.assertAlmostEqual(price_of_one_image(one, VisualFidelity.STANDARD), 0.063)
        self.assertAlmostEqual(price_of_one_image(one, VisualFidelity.FINE), 0.25)

    def test_the_same_budget_buys_sixteen_times_more_at_draft(self) -> None:
        """The whole reason the two settings are shown together."""
        one = [self.provider()]
        shots_ = shots(200)
        draft = plan(
            shots_,
            budget_usd=2.0,
            price_each_usd=price_of_one_image(one, VisualFidelity.DRAFT),
        )
        fine = plan(
            shots_,
            budget_usd=2.0,
            price_each_usd=price_of_one_image(one, VisualFidelity.FINE),
        )
        self.assertEqual(len(draft.permitted), 125)
        self.assertEqual(len(fine.permitted), 8)

    def test_the_dearest_provider_sets_the_plan_not_the_cheapest(self) -> None:
        """The router prefers the cheapest, but falls back to a dearer one when
        it is failing — and a plan built on the cheapest would then overspend
        with nothing having gone wrong."""

        class Dearer:
            declared_unit_cost = 0.40

        self.assertAlmostEqual(
            price_of_one_image([self.provider(), Dearer()], VisualFidelity.DRAFT), 0.40
        )

    def test_a_provider_that_cannot_price_does_not_zero_the_plan(self) -> None:
        """An escaping exception here would eventually mean 'free', which is the
        one answer that is never safe."""

        class Broken:
            declared_unit_cost = 0.05

            def price_at(self, fidelity: object) -> float:
                raise RuntimeError("no table for this model")

        self.assertAlmostEqual(
            price_of_one_image([Broken()], VisualFidelity.FINE), 0.05
        )

    def test_a_provider_that_declares_only_a_capability_is_not_free(self) -> None:
        """The fallback used to be `0.0`, so a provider that implemented
        neither optional pricing property was planned as **free** and every
        budget it touched permitted every shot. `StubImageGenerationProvider`
        is exactly such a provider, and so is any adapter added next.
        """

        class Capable:
            class capabilities:  # noqa: N801 - mimics the real attribute shape
                unit_cost_usd = 0.02

        self.assertAlmostEqual(
            price_of_one_image([Capable()], VisualFidelity.DRAFT), 0.02
        )

    def test_a_provider_that_will_not_say_is_priced_at_the_dearest(self) -> None:
        """Erring high buys fewer pictures than it could have. Erring low buys
        more than the user agreed to pay for. Only one of those is an invoice.
        """

        class Silent:
            pass

        self.assertAlmostEqual(
            price_of_one_image([Silent()], VisualFidelity.DRAFT), 0.25
        )

    def test_no_providers_at_all_is_free_rather_than_a_crash(self) -> None:
        """A deployment with no image provider generates nothing, so nothing is
        what it costs — and every shot is permitted to reach the free rungs."""
        self.assertEqual(price_of_one_image([], VisualFidelity.FINE), 0.0)
        self.assertEqual(len(plan(shots(9), budget_usd=0.0, price_each_usd=0.0).permitted), 9)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
