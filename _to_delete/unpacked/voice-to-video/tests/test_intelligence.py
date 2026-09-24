"""Stages 3, 4 and 5 — understanding, scenes and visual direction."""

from __future__ import annotations

import asyncio
import itertools
import unittest

from vtv.contracts.scene import MIN_SCENE_SECONDS, ScenePurpose, VisualGoal
from vtv.contracts.semantics import EntityType, SemanticIntent
from vtv.contracts.style import StyleProfile
from vtv.contracts.visual_plan import VisualStrategy
from vtv.observability.events import EventSink
from vtv.pipeline import nlp
from vtv.pipeline.director import RuleBasedVisualDirector
from vtv.pipeline.scenes import SceneEngine, continuity_score, group_units
from vtv.pipeline.text_entry import transcript_from_text
from vtv.pipeline.understanding import HeuristicUnderstandingEngine, extract_json

TRANSISTOR = (
    "Let me tell you about the single most important invention of the twentieth century. "
    "The transistor was invented in 1947 at Bell Labs. "
    "It was smaller and far more efficient than the vacuum tubes that came before it. "
    "A vacuum tube was the size of a light bulb, but a transistor could be smaller than a grain of rice. "
    "Within twenty years transistors had replaced vacuum tubes almost everywhere. "
    "Everything you are holding right now is built out of them."
)


def analyse(script: str):
    transcript = transcript_from_text(script, project_id="prj_" + "a" * 24)
    understanding = asyncio.run(HeuristicUnderstandingEngine().understand(transcript))
    graph = SceneEngine(events=EventSink()).build(
        transcript=transcript,
        understanding=understanding,
        style=StyleProfile(),
        total_duration=transcript.span.end if transcript.span else 0.0,
    )
    plan = asyncio.run(
        RuleBasedVisualDirector(events=EventSink()).direct(
            scene_graph=graph, understanding=understanding
        )
    )
    return transcript, understanding, graph, plan


class DeterministicLanguageAnalysis(unittest.TestCase):
    def test_intent_is_recognised_from_discourse_cues(self) -> None:
        cases = {
            "A blockchain is a distributed ledger.": SemanticIntent.DEFINITION,
            "It was smaller than the tubes.": SemanticIntent.COMPARISON,
            "Because it was fragile, it failed.": SemanticIntent.CAUSATION,
            "First you heat the water, then you add coffee.": SemanticIntent.PROCESS,
            "Why does this matter?": SemanticIntent.QUESTION,
            "um, uh, yeah": SemanticIntent.FILLER,
        }
        for text, expected in cases.items():
            self.assertIs(nlp.classify_intent(text), expected, text)

    def test_a_signposted_sentence_with_content_is_not_demoted_to_filler(self) -> None:
        # "Let me tell you about X" opens with a transition cue but states the
        # thesis; treating it as filler throws away the opening shot.
        intent = nlp.classify_intent(
            "Let me tell you about the single most important invention of the century."
        )
        self.assertIsNot(intent, SemanticIntent.TRANSITION)
        self.assertIsNot(intent, SemanticIntent.FILLER)

    def test_spoken_numbers_become_values_a_chart_can_use(self) -> None:
        quantities = nlp.extract_quantities(
            "The population grew from one billion to eight billion people."
        )
        values = sorted(q.value for q in quantities)
        self.assertEqual(values, [1e9, 8e9])

    def test_years_are_dates_not_measurements(self) -> None:
        self.assertEqual(nlp.extract_quantities("It happened in 1947."), [])
        entities = nlp.extract_entities("It happened in 1947.")
        self.assertTrue(any(e.type is EntityType.DATE for e in entities))

    def test_collocations_are_one_subject(self) -> None:
        terms = nlp.salient_terms(TRANSISTOR)
        self.assertIn("vacuum tube", terms)
        self.assertNotIn("vacuum", terms)

    def test_comparatives_are_not_mistaken_for_subjects(self) -> None:
        self.assertNotIn("smaller", nlp.salient_terms(TRANSISTOR))

    def test_singular_and_plural_are_the_same_subject(self) -> None:
        self.assertEqual(
            nlp.normalise_term("transistors"), nlp.normalise_term("transistor")
        )


class Understanding(unittest.TestCase):
    def setUp(self) -> None:
        _, self.understanding, _, _ = analyse(TRANSISTOR)

    def test_the_topic_is_the_subject_not_a_date(self) -> None:
        self.assertEqual(self.understanding.topic.lower(), "transistor")

    def test_entities_and_relations_are_extracted(self) -> None:
        names = {e.search_name.lower() for e in self.understanding.entities}
        self.assertIn("transistor", names)
        self.assertIn("bell labs", names)
        self.assertTrue(self.understanding.relations)

    def test_relations_point_the_right_way(self) -> None:
        # "The transistor replaced vacuum tubes", not the other way round.
        pairs = {
            (
                self.understanding.entity_by_id(r.subject_id).search_name.lower(),  # type: ignore[union-attr]
                r.predicate,
            )
            for r in self.understanding.relations
        }
        self.assertIn(("transistor", "replaced"), pairs)

    def test_units_reference_only_entities_that_exist(self) -> None:
        known = {e.entity_id for e in self.understanding.entities}
        for unit in self.understanding.units:
            for entity_id in unit.entity_ids:
                self.assertIn(entity_id, known)

    def test_no_confidence_is_invented_for_a_rule(self) -> None:
        # A rule has no calibrated confidence; reporting one would mislead
        # every consumer that reads it.
        self.assertTrue(all(unit.confidence is None for unit in self.understanding.units))


class SceneGrouping(unittest.TestCase):
    """Rule 7, tested directly."""

    def setUp(self) -> None:
        _, self.understanding, self.graph, _ = analyse(TRANSISTOR)

    def test_there_are_fewer_scenes_than_units(self) -> None:
        visualisable = [u for u in self.understanding.units if u.is_visualisable]
        self.assertLess(len(self.graph.scenes), len(visualisable))

    def test_the_graph_covers_the_whole_recording(self) -> None:
        self.assertEqual(self.graph.gaps(), [])
        assert self.graph.span is not None
        self.assertAlmostEqual(self.graph.span.start, 0.0)

    def test_no_scene_is_too_short_to_land(self) -> None:
        for scene in self.graph.scenes:
            self.assertGreaterEqual(scene.duration, MIN_SCENE_SECONDS - 1e-6)

    def test_the_first_scene_opens_and_the_last_concludes(self) -> None:
        self.assertIs(self.graph.scenes[0].purpose, ScenePurpose.OPENING)
        self.assertIs(self.graph.scenes[-1].purpose, ScenePurpose.CONCLUSION)

    def test_shared_subjects_pull_units_together(self) -> None:
        units = self.understanding.units
        shared = [
            (a, b)
            for a, b in itertools.pairwise(units)
            if set(a.entity_ids) & set(b.entity_ids)
        ]
        for previous, current in shared:
            self.assertGreater(continuity_score(previous, current), 0.4)

    def test_filler_never_gets_its_own_scene(self) -> None:
        _, _understanding, graph, _ = analyse(
            "So, um, yeah. Okay so sleep consolidates memory during the deep phases. "
            "Right, um, so that is why rest matters. You know."
        )
        self.assertLessEqual(len(graph.scenes), 3)
        self.assertTrue(all(scene.narration.strip() for scene in graph.scenes))

    def test_grouping_is_deterministic(self) -> None:
        # Segmentation is the structural decision every later stage inherits.
        # If it varied run to run, no downstream test could be stable.
        first = [s.narration for s in analyse(TRANSISTOR)[2].scenes]
        second = [s.narration for s in analyse(TRANSISTOR)[2].scenes]
        self.assertEqual(first, second)

    def test_grouping_survives_an_empty_understanding(self) -> None:
        self.assertEqual(group_units([]), [])


class VisualDirection(unittest.TestCase):
    def test_stated_numbers_become_a_chart_not_a_generated_image(self) -> None:
        _, _, _, plan = analyse(
            "The world population grew from one billion people to eight billion people. "
            "Almost all of that came in the last two centuries."
        )
        primitives = [
            getattr(getattr(p.primary.requirements, "spec", None), "primitive", None)
            for p in plan.scene_plans
        ]
        self.assertIn("chart", [getattr(p, "value", None) for p in primitives])
        self.assertEqual(plan.expected_cost_usd, 0.0)

    def test_a_comparison_becomes_a_two_sided_shot(self) -> None:
        _, _, _, plan = analyse(TRANSISTOR)
        specs = [
            getattr(p.primary.requirements, "spec", None) for p in plan.scene_plans
        ]
        comparisons = [s for s in specs if getattr(s, "primitive", None) and s.primitive.value == "comparison"]
        self.assertTrue(comparisons)
        # Both sides must be named. "this vs that" is not a visual.
        for spec in comparisons:
            self.assertTrue(spec.left.title.strip())
            self.assertTrue(spec.right.title.strip())
            self.assertNotEqual(spec.left.title, spec.right.title)

    def test_a_plain_statement_becomes_type(self) -> None:
        _, _, _, plan = analyse("This changed everything about how we build things.")
        spec = plan.scene_plans[0].primary.requirements.spec  # type: ignore[union-attr]
        self.assertEqual(spec.primitive.value, "typography")

    def test_every_scene_gets_exactly_one_plan(self) -> None:
        _, _, graph, plan = analyse(TRANSISTOR)
        self.assertEqual(len(plan.scene_plans), len(graph.scenes))
        for scene in graph.scenes:
            self.assertIsNotNone(plan.plan_for(scene.scene_id))

    def test_every_decision_explains_itself(self) -> None:
        _, _, _, plan = analyse(TRANSISTOR)
        for scene_plan in plan.scene_plans:
            self.assertGreaterEqual(len(scene_plan.primary.rationale.split()), 6)

    def test_fallible_strategies_carry_a_ladder_ending_in_type(self) -> None:
        _, _, _, plan = analyse(
            "Benjamin Franklin flew a kite in a thunderstorm in Philadelphia."
        )
        for scene_plan in plan.scene_plans:
            if scene_plan.primary.strategy in {
                VisualStrategy.LICENSED_MEDIA,
                VisualStrategy.GENERATED_IMAGE,
                VisualStrategy.GENERATED_VIDEO,
            }:
                self.assertTrue(scene_plan.fallbacks)
                self.assertIs(
                    scene_plan.fallbacks[-1].strategy, VisualStrategy.PROGRAMMATIC
                )

    def test_a_budget_demotes_expensive_shots_rather_than_overspending(self) -> None:
        transcript = transcript_from_text(
            "Imagine a futuristic city floating above the clouds of Mars. "
            "It is a place nobody has ever photographed.",
            project_id="prj_" + "a" * 24,
        )
        understanding = asyncio.run(
            HeuristicUnderstandingEngine().understand(transcript)
        )
        graph = SceneEngine(events=EventSink()).build(
            transcript=transcript,
            understanding=understanding,
            total_duration=transcript.span.end if transcript.span else 0.0,
        )
        from vtv.contracts.base import Budget

        tight = RuleBasedVisualDirector(
            events=EventSink(), budget=Budget(max_cost_usd=0.001)
        )
        plan = asyncio.run(tight.direct(scene_graph=graph, understanding=understanding))
        self.assertLessEqual(plan.worst_case_cost_usd, 0.001 + 1e-9)

    def test_timeline_events_quote_the_speaker_verbatim(self) -> None:
        _, _, _, plan = analyse(TRANSISTOR)
        for scene_plan in plan.scene_plans:
            spec = getattr(scene_plan.primary.requirements, "spec", None)
            if getattr(spec, "primitive", None) and spec.primitive.value == "timeline":
                for event in spec.events:
                    # Never a precision the speaker did not claim.
                    self.assertIn(event.when, TRANSISTOR)


class ModelOutputIsUntrusted(unittest.TestCase):
    def test_json_is_recovered_from_a_fenced_response(self) -> None:
        payload = extract_json('```json\n{"topic": "x", "units": []}\n```')
        self.assertEqual(payload["topic"], "x")

    def test_json_is_recovered_from_surrounding_prose(self) -> None:
        payload = extract_json('Sure! Here you go:\n{"a": {"b": 1}}\nHope that helps.')
        self.assertEqual(payload["a"]["b"], 1)

    def test_a_response_with_no_json_is_rejected(self) -> None:
        from vtv.contracts.errors import ValidationFailed

        with self.assertRaises(ValidationFailed):
            extract_json("I am afraid I cannot do that.")

    def test_an_unterminated_object_is_rejected(self) -> None:
        from vtv.contracts.errors import ValidationFailed

        with self.assertRaises(ValidationFailed):
            extract_json('{"a": {"b": 1}')


class VisualGoalsAreCommunicative(unittest.TestCase):
    def test_a_process_asks_to_be_shown_as_a_process(self) -> None:
        _, _, graph, _ = analyse(
            "First the water is heated. Then the coffee is added and left to steep. "
            "Finally the grounds are pressed down."
        )
        self.assertIn(
            VisualGoal.SHOW_PROCESS, {scene.visual_goal for scene in graph.scenes}
        )


if __name__ == "__main__":
    unittest.main()
