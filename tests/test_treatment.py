"""The Visual Director: what kind of visual explains this idea.

Every case here is either one of the examples the product was specified against
or one of the failures that actually shipped. Nothing is invented to make a
rule look good.
"""

from __future__ import annotations

import unittest

from vtv.contracts.scene import VisualGoal
from vtv.contracts.semantics import SemanticIntent
from vtv.contracts.visual_language import VisualPrimitive
from vtv.contracts.visual_plan import VisualStrategy
from vtv.pipeline.treatment import (
    MIN_READABLE_SECONDS,
    OFFERABLE,
    Brief,
    Capabilities,
    Census,
    Treatment,
    decide,
    ladder_for,
)

EVERYTHING = Capabilities(
    can_search_media=True, can_generate_image=True, can_generate_video=True
)


def brief(**kw: object) -> Brief:
    base: dict[str, object] = {
        "unit_id": "vun_0001",
        "narration": "A line of narration.",
        "seconds": 5.0,
    }
    return Brief(**{**base, **kw})  # type: ignore[arg-type]


class TheKindIsChosenBeforeTheAsset(unittest.TestCase):
    """The specification, case by case.

    Each of these is a sentence from the product spec: "historical fact → a
    real photograph", "numerical comparison → a chart", and so on.
    """

    def test_a_sentence_that_charts_is_charted(self) -> None:
        """The clearest case there is, and the one the old fixed ladder got
        most expensively wrong: it searched the commons, failed, and paid to
        generate an impression of growth."""
        decision = decide(brief(drawn=VisualPrimitive.CHART), EVERYTHING)
        self.assertIs(decision.treatment, Treatment.CHART)
        self.assertIs(decision.treatment.as_strategy, VisualStrategy.PROGRAMMATIC)

    def test_chronological_development_becomes_a_timeline(self) -> None:
        decision = decide(brief(drawn=VisualPrimitive.TIMELINE), EVERYTHING)
        self.assertIs(decision.treatment, Treatment.TIMELINE)

    def test_a_process_becomes_a_diagram(self) -> None:
        decision = decide(
            brief(intent=SemanticIntent.PROCESS, goal=VisualGoal.SHOW_PROCESS),
            EVERYTHING,
        )
        self.assertIs(decision.treatment, Treatment.DIAGRAM)

    def test_a_comparison_is_held_side_by_side(self) -> None:
        decision = decide(brief(intent=SemanticIntent.COMPARISON), EVERYTHING)
        self.assertIs(decision.treatment, Treatment.COMPARISON)

    def test_a_spatial_idea_becomes_a_map(self) -> None:
        decision = decide(brief(goal=VisualGoal.SHOW_PLACE), EVERYTHING)
        self.assertIs(decision.treatment, Treatment.MAP)

    def test_a_real_named_subject_is_photographed_never_invented(self) -> None:
        """The rule that stops a stranger's face appearing under a claim they
        never made. It is a photograph of them or it is the words."""
        decision = decide(brief(names_real_subject=True), EVERYTHING)
        self.assertIs(decision.treatment, Treatment.FOUND_MEDIA)
        self.assertNotIn(Treatment.GENERATED_IMAGE, decision.ladder)

    def test_the_users_own_media_wins_over_everything(self) -> None:
        """They chose it. We did not, and no rule below reconsiders it."""
        caps = Capabilities(
            can_search_media=True, can_generate_image=True, has_user_media=True
        )
        decision = decide(brief(drawn=VisualPrimitive.CHART), caps)
        self.assertIs(decision.treatment, Treatment.USER_MEDIA)


class MoneyIsNotSpentOnGrammar(unittest.TestCase):
    """"And that brings us to the next part" spent a commons search and an
    image generation on a sentence that means nothing on its own."""

    def test_a_transition_is_set_as_text(self) -> None:
        decision = decide(brief(intent=SemanticIntent.TRANSITION), EVERYTHING)
        self.assertIs(decision.treatment, Treatment.TYPOGRAPHY)
        self.assertTrue(decision.treatment.is_free)

    def test_filler_is_set_as_text(self) -> None:
        decision = decide(brief(intent=SemanticIntent.FILLER), EVERYTHING)
        self.assertIs(decision.treatment, Treatment.TYPOGRAPHY)

    def test_a_shot_too_short_to_read_is_the_words(self) -> None:
        """A chart held for a second and a half is decoration. Nobody reads an
        axis in that time."""
        decision = decide(
            brief(seconds=MIN_READABLE_SECONDS - 0.1, drawn=VisualPrimitive.CHART),
            EVERYTHING,
        )
        self.assertIs(decision.treatment, Treatment.TYPOGRAPHY)

    def test_an_abstract_line_with_nothing_to_search_for_is_not_searched(self) -> None:
        """A real render made 725 commons searches on an essay about ideas and
        the selector vetoed most of them. Two round trips to be told so."""
        decision = decide(brief(searchable=False), EVERYTHING)
        self.assertIsNot(decision.treatment, Treatment.FOUND_MEDIA)

    def test_a_withheld_budget_removes_every_paid_rung(self) -> None:
        """The planner decided this shot may not be bought, before any of this.
        A decision cannot overturn a plan made about somebody's money."""
        decision = decide(brief(may_spend=False, searchable=False), EVERYTHING)
        self.assertTrue(all(t.is_free for t in decision.ladder), decision.ladder)


class ItAsksWhatIsAvailableRatherThanAssuming(unittest.TestCase):
    """Nothing here names a vendor. A deployment with no image provider is not
    a broken deployment; it is one that makes videos out of charts, diagrams,
    found photographs and type."""

    def test_a_treatment_with_no_provider_is_never_offered(self) -> None:
        decision = decide(brief(searchable=False), Capabilities(can_search_media=True))
        self.assertNotIn(Treatment.GENERATED_IMAGE, decision.ladder)

    def test_with_nothing_configured_it_still_decides(self) -> None:
        """Drawing needs no provider, no network and no money."""
        decision = decide(brief(drawn=VisualPrimitive.CHART), Capabilities())
        self.assertIs(decision.treatment, Treatment.CHART)

    def test_the_ladder_always_ends_in_something_that_cannot_fail(self) -> None:
        for caps in (EVERYTHING, Capabilities(), Capabilities(can_search_media=True)):
            for drawn in (None, VisualPrimitive.CHART):
                with self.subTest(caps=caps, drawn=drawn):
                    decision = decide(brief(drawn=drawn), caps)
                    self.assertIn(Treatment.TYPOGRAPHY, decision.ladder)

    def test_the_registry_is_read_not_a_config_file(self) -> None:
        from vtv.contracts.generation import GenerationKind

        class Router:
            def providers_for(self, kind):  # type: ignore[no-untyped-def]
                return [object()] if kind is GenerationKind.IMAGE else []

        caps = Capabilities.from_router(Router())
        self.assertTrue(caps.can_generate_image)
        self.assertFalse(caps.can_generate_video)

    def test_a_router_that_raises_is_treated_as_offering_nothing(self) -> None:
        """A planning stage must never fail a render."""

        class Broken:
            def providers_for(self, kind):  # type: ignore[no-untyped-def]
                raise RuntimeError("no registry")

        caps = Capabilities.from_router(Broken())
        self.assertFalse(caps.can_generate_image)
        self.assertTrue(caps.can_draw)


class TheReadersSuggestionIsWeighedNotObeyed(unittest.TestCase):
    """The reader sees the line *with the lines around it*, which is the
    context the decision needs. It is still validated on three counts."""

    def test_a_usable_suggestion_is_taken(self) -> None:
        decision = decide(brief(), EVERYTHING, suggested="typography")
        self.assertIs(decision.treatment, Treatment.TYPOGRAPHY)
        self.assertEqual(decision.decided_by, "director")

    def test_a_treatment_that_does_not_exist_is_discarded(self) -> None:
        """A model that answers "infographic" has made a sentence, not a
        decision."""
        decision = decide(brief(), EVERYTHING, suggested="infographic")
        self.assertEqual(decision.decided_by, "rules")

    def test_a_treatment_this_deployment_cannot_produce_is_discarded(self) -> None:
        decision = decide(
            brief(), Capabilities(can_search_media=True), suggested="generated_video"
        )
        self.assertEqual(decision.decided_by, "rules")

    def test_a_paid_suggestion_cannot_overturn_the_budget(self) -> None:
        decision = decide(
            brief(may_spend=False), EVERYTHING, suggested="generated_image"
        )
        self.assertEqual(decision.decided_by, "rules")
        self.assertTrue(all(t.is_free for t in decision.ladder))

    def test_it_cannot_claim_the_users_own_media(self) -> None:
        """That decision was taken before this and is never revisited."""
        decision = decide(brief(), EVERYTHING, suggested="user_media")
        self.assertIsNot(decision.treatment, Treatment.USER_MEDIA)

    def test_a_structural_signal_outranks_a_suggestion(self) -> None:
        """The Draughtsman found numbers it could plot. That is evidence, and
        evidence beats an opinion about the same sentence."""
        decision = decide(
            brief(drawn=VisualPrimitive.CHART), EVERYTHING, suggested="generated_image"
        )
        self.assertIs(decision.treatment, Treatment.CHART)

    def test_every_offerable_treatment_is_a_real_one(self) -> None:
        """The instruction lists these to the model by name; a name that is not
        a `Treatment` would be silently discarded on every shot."""
        for treatment in OFFERABLE:
            self.assertIsInstance(treatment, Treatment)


class TheLadderDescendsFromTheDecision(unittest.TestCase):
    """A chart whose numbers will not parse must still become something. The
    chain still exists; it just starts from a considered place."""

    def test_a_chart_falls_to_a_comparison_before_a_photograph(self) -> None:
        order = ladder_for(Treatment.CHART, EVERYTHING, brief=brief())
        self.assertEqual(order[0], Treatment.CHART)
        self.assertLess(
            order.index(Treatment.COMPARISON), order.index(Treatment.FOUND_MEDIA)
        )

    def test_nothing_appears_twice(self) -> None:
        order = ladder_for(Treatment.FOUND_MEDIA, EVERYTHING, brief=brief())
        self.assertEqual(len(order), len(set(order)))

    def test_a_decision_maps_onto_the_existing_contracts(self) -> None:
        """Executable by the composer that already exists, not a parallel
        pipeline."""
        for treatment in Treatment:
            with self.subTest(treatment=treatment):
                self.assertIsInstance(treatment.as_strategy, VisualStrategy)


class ItCanBeMeasured(unittest.TestCase):
    """"The agent decides well" is not a claim anybody should accept without a
    number."""

    def census(self) -> Census:
        return Census.of(
            [
                decide(brief(unit_id=f"v{i}", drawn=VisualPrimitive.CHART), EVERYTHING)
                for i in range(3)
            ]
            + [
                decide(brief(unit_id=f"t{i}", intent=SemanticIntent.TRANSITION), EVERYTHING)
                for i in range(5)
            ]
            + [decide(brief(unit_id="g", searchable=False), EVERYTHING)]
        )

    def test_it_counts_what_was_chosen(self) -> None:
        census = self.census()
        self.assertEqual(census.total, 9)
        self.assertEqual(census.by_treatment["chart"], 3)
        self.assertEqual(census.by_treatment["typography"], 5)

    def test_it_projects_the_cost_before_a_provider_is_called(self) -> None:
        census = self.census()
        self.assertEqual(census.paid, 1)
        self.assertAlmostEqual(census.projected_usd(0.016), 0.016)

    def test_it_says_what_fraction_was_free(self) -> None:
        census = self.census()
        self.assertAlmostEqual(census.free_fraction, 8 / 9)

    def test_the_headline_is_readable(self) -> None:
        headline = self.census().headline()
        self.assertIn("9 shots", headline)
        self.assertIn("drawn", headline)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
