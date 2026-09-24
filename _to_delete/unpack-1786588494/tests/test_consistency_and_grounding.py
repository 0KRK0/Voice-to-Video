"""Stages 23 and 24 — visual consistency and factual grounding.

The grounding tests are the ones that matter most commercially. A chart is the
most authoritative object a video can contain, and a chart of numbers nobody
said launders a guess into evidence. So each test here is a specific way a
number could get on screen without having been stated, and the assertion is that
it does not.
"""

from __future__ import annotations

import asyncio
import unittest

from vtv.contracts.base import IdPrefix, new_id
from vtv.contracts.consistency import (
    BindingKind,
    BindingSource,
    EntityBinding,
    VisualBible,
)
from vtv.contracts.source import TableData
from vtv.contracts.style import StyleProfile
from vtv.contracts.visual_language import (
    ChartKind,
    ChartSeries,
    ChartSpec,
    ComparisonSide,
    ComparisonSpec,
    DataPoint,
    MapMarker,
    MapSpec,
    TimelineEvent,
    TimelineSpec,
    TypographySpec,
)
from vtv.observability.events import EventSink
from vtv.pipeline.consistency import MIN_SCENES, ConsistencyEngine
from vtv.pipeline.grounding import Evidence, GroundingVerdict, check_spec
from vtv.pipeline.scenes import SceneEngine
from vtv.pipeline.text_entry import transcript_from_text
from vtv.pipeline.understanding import (
    HeuristicUnderstandingEngine,
    UnderstandingService,
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def chart(*points: tuple[str, float], title: str | None = None) -> ChartSpec:
    return ChartSpec(
        kind=ChartKind.COLUMN,
        title=title,
        series=[
            ChartSeries(
                name="series",
                points=[DataPoint(label=label, value=value) for label, value in points],
            )
        ],
        preferred_duration=4.0,
    )


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

class ChartGrounding(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = Evidence.build(
            narration=(
                "Renewable energy reached 30 percent of generation in 2023. "
                "Ten years earlier it was 12 percent. The world population "
                "passed 8 billion people."
            )
        )

    def test_a_chart_of_stated_numbers_is_grounded(self) -> None:
        result = check_spec(chart(("2013", 12.0), ("2023", 30.0)), self.evidence)
        self.assertIs(result.verdict, GroundingVerdict.GROUNDED)
        self.assertTrue(result.is_acceptable)

    def test_a_chart_containing_one_invented_number_is_refused_whole(self) -> None:
        """Plotting only the supported points would change the shape of the claim."""
        result = check_spec(
            chart(("2013", 12.0), ("2023", 30.0), ("2030", 55.0)), self.evidence
        )
        self.assertIs(result.verdict, GroundingVerdict.UNGROUNDED)
        self.assertFalse(result.is_acceptable)
        self.assertTrue(any("55" in claim for claim in result.unsupported))

    def test_an_extrapolated_trend_is_refused(self) -> None:
        """The most tempting invention: a plausible next value."""
        result = check_spec(chart(("2023", 30.0), ("2033", 48.0)), self.evidence)
        self.assertFalse(result.is_acceptable)

    def test_rounding_is_accepted_but_a_different_number_is_not(self) -> None:
        """"About 8 billion" and 8,045,311,447 are the same claim. 9 billion is not."""
        evidence = Evidence.build(narration="The population passed 8 billion.")
        self.assertTrue(
            check_spec(
                chart(("a", 8_000_000_000.0), ("b", 8_045_311_447.0)), evidence
            ).is_acceptable
        )
        self.assertFalse(
            check_spec(
                chart(("a", 8_000_000_000.0), ("b", 9_000_000_000.0)), evidence
            ).is_acceptable
        )

    def test_small_numbers_are_compared_absolutely(self) -> None:
        """A relative tolerance on small values would admit different claims."""
        evidence = Evidence.build(narration="The rate was 0.02 per cent.")
        self.assertFalse(
            check_spec(chart(("a", 0.02), ("b", 0.5)), evidence).is_acceptable
        )

    def test_a_number_from_a_source_table_is_grounded(self) -> None:
        """A figure the document contained is as good as one the speaker said."""
        evidence = Evidence.build(
            narration="Revenue grew through the year.",
            tables=[
                TableData(
                    headers=["Quarter", "Revenue"],
                    rows=[["Q1", "4"], ["Q4", "11"]],
                )
            ],
        )
        result = check_spec(chart(("Q1", 4.0), ("Q4", 11.0)), evidence)
        self.assertTrue(result.is_acceptable)
        self.assertTrue(any("table" in origin for _, origin in result.supported))

    def test_an_invented_title_is_refused(self) -> None:
        """"Projected growth" over historical data is a claim nobody made."""
        result = check_spec(
            chart(("2013", 12.0), ("2023", 30.0), title="Projected growth to 2040"),
            self.evidence,
        )
        self.assertFalse(result.is_acceptable)

    def test_the_refusal_says_what_was_not_supported(self) -> None:
        result = check_spec(chart(("x", 1.0), ("y", 999.0)), self.evidence)
        self.assertIn("999", result.reason())

    def test_a_spoken_number_in_words_is_evidence(self) -> None:
        """Transcripts contain "eight billion", not "8000000000"."""
        evidence = Evidence.build(narration="The population reached eight billion.")
        self.assertIsNotNone(evidence.supports_number(8_000_000_000.0))


class TimelineGrounding(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = Evidence.build(
            narration=(
                "The transistor was invented in 1947 at Bell Labs. "
                "By 1965 it had replaced the vacuum tube almost everywhere."
            )
        )

    def timeline(self, *events: tuple[str, str, float]) -> TimelineSpec:
        return TimelineSpec(
            events=[
                TimelineEvent(label=label, when=when, sort_value=sort)
                for label, when, sort in events
            ],
            preferred_duration=6.0,
        )

    def test_stated_dates_are_grounded(self) -> None:
        result = check_spec(
            self.timeline(
                ("transistor invented", "1947", 1947.0),
                ("vacuum tube replaced", "1965", 1965.0),
            ),
            self.evidence,
        )
        self.assertTrue(result.is_acceptable)

    def test_an_unstated_date_is_refused(self) -> None:
        """A timeline with a year nobody said is the system inventing history."""
        result = check_spec(
            self.timeline(
                ("transistor invented", "1947", 1947.0),
                ("integrated circuit", "1958", 1958.0),
            ),
            self.evidence,
        )
        self.assertFalse(result.is_acceptable)

    def test_a_spoken_year_counts_as_stated(self) -> None:
        evidence = Evidence.build(
            narration="It was invented in nineteen forty seven at Bell Labs."
        )
        self.assertTrue(evidence.supports_year(1947))


class MapAndTextGrounding(unittest.TestCase):
    def test_a_place_nobody_named_is_refused(self) -> None:
        """A map decides where something happened. That is a strong claim."""
        evidence = Evidence.build(
            narration="Penicillin was discovered in London in 1928."
        )
        refused = check_spec(
            MapSpec(
                markers=[MapMarker(label="Reykjavik", latitude=64.1, longitude=-21.9)],
                preferred_duration=4.0,
            ),
            evidence,
        )
        self.assertFalse(refused.is_acceptable)

    def test_a_named_place_is_grounded(self) -> None:
        evidence = Evidence.build(
            narration="Penicillin was discovered in London in 1928."
        )
        allowed = check_spec(
            MapSpec(
                markers=[MapMarker(label="London", latitude=51.5, longitude=-0.1)],
                preferred_duration=4.0,
            ),
            evidence,
        )
        self.assertTrue(allowed.is_acceptable)

    def test_a_headline_figure_must_have_been_stated(self) -> None:
        evidence = Evidence.build(narration="Solar costs fell by 80 percent.")
        self.assertTrue(
            check_spec(
                TypographySpec(headline="Solar costs fell 80 percent",
                               preferred_duration=3.0),
                evidence,
            ).is_acceptable
        )
        self.assertFalse(
            check_spec(
                TypographySpec(headline="Solar costs fell 95 percent",
                               preferred_duration=3.0),
                evidence,
            ).is_acceptable
        )

    def test_a_comparison_side_nobody_mentioned_is_refused(self) -> None:
        evidence = Evidence.build(
            narration="Transistors were smaller and cheaper than vacuum tubes."
        )
        self.assertFalse(
            check_spec(
                ComparisonSpec(
                    left=ComparisonSide(title="Transistors"),
                    right=ComparisonSide(title="Quantum processors"),
                    preferred_duration=5.0,
                ),
                evidence,
            ).is_acceptable
        )

    def test_a_visual_with_no_claim_needs_no_grounding(self) -> None:
        """A diagram of relationships asserts structure, not figures.

        Refusing it for failing a numeric check would refuse most of the
        product's output, so primitives that make no measurable claim return
        NO_CLAIM rather than being held to evidence they cannot have.
        """
        from vtv.contracts.visual_language import (
            NetworkEdge,
            NetworkNode,
            NetworkSpec,
        )

        result = check_spec(
            NetworkSpec(
                nodes=[NetworkNode(key="a", label="Sun"),
                       NetworkNode(key="b", label="Plant")],
                edges=[NetworkEdge(source="a", target="b", label="feeds")],
                preferred_duration=4.0,
            ),
            Evidence.build(narration="Anything at all."),
        )
        self.assertIs(result.verdict, GroundingVerdict.NO_CLAIM)
        self.assertTrue(result.is_acceptable)


class GroundingInTheDirector(unittest.TestCase):
    """The gate is only worth anything if the Director actually consults it."""

    def plan(self, script: str):  # type: ignore[no-untyped-def]
        from vtv.pipeline.director import RuleBasedVisualDirector

        project_id = new_id(IdPrefix.PROJECT)
        transcript = transcript_from_text(script, project_id=project_id)
        understanding = run(
            UnderstandingService(
                engine=HeuristicUnderstandingEngine(), events=EventSink()
            ).understand(transcript)
        )
        span = transcript.span
        assert span is not None
        graph = SceneEngine(events=EventSink()).build(
            transcript=transcript,
            understanding=understanding,
            style=StyleProfile(),
            total_duration=span.end,
        )
        director = RuleBasedVisualDirector(events=EventSink())
        return run(
            director.direct(scene_graph=graph, understanding=understanding)
        ), graph

    def test_a_correctly_labelled_chart_survives_the_gate(self) -> None:
        """The false positive this gate produced on first write, now a test.

        The Director sees a scene graph, not a transcript. Building evidence
        without the narration refused a chart whose axis was correctly labelled
        "percent", because that word appeared in no extracted entity.
        """
        plan, _graph = self.plan(
            "Renewable energy reached 30 percent of generation in 2023. "
            "Ten years earlier it was 12 percent. "
            "The cost of solar fell by 80 percent over the same period."
        )
        primitives = {
            spec.primitive.value
            for scene_plan in plan.scene_plans
            if (spec := getattr(scene_plan.primary.requirements, "spec", None))
        }
        self.assertIn("chart", primitives)

    def test_every_scene_still_gets_a_visual(self) -> None:
        """Refusal must degrade, never leave a hole (Rule 8)."""
        plan, graph = self.plan(
            "Sleep matters more than people think. "
            "The brain consolidates memories during deep sleep. "
            "Losing an hour a night compounds over a week."
        )
        planned = {scene_plan.scene_id for scene_plan in plan.scene_plans}
        self.assertEqual(planned, {scene.scene_id for scene in graph.scenes})


# ---------------------------------------------------------------------------
# Visual consistency
# ---------------------------------------------------------------------------

class Bible(unittest.TestCase):
    def setUp(self) -> None:
        self.bible = VisualBible(project_id=new_id(IdPrefix.PROJECT))

    def test_a_binding_is_found_by_name_or_alias(self) -> None:
        self.bible.bind(
            EntityBinding(
                canonical_name="Transistor",
                aliases=["the transistor", "solid-state switch"],
                kind=BindingKind.COLOUR,
                colour="#1f77b4",
            )
        )
        for name in ("transistor", "TRANSISTOR", "solid-state switch"):
            with self.subTest(name=name):
                self.assertIsNotNone(
                    self.bible.binding_for(name, kind=BindingKind.COLOUR)
                )

    def test_a_locked_binding_is_never_overwritten(self) -> None:
        """A user who pressed "keep this" and watched it change has learned the
        control does not work, and nothing recovers from that."""
        self.bible.bind(
            EntityBinding(
                canonical_name="Transistor",
                kind=BindingKind.COLOUR,
                colour="#ff0000",
                source=BindingSource.USER,
            )
        )
        self.bible.bind(
            EntityBinding(
                canonical_name="Transistor",
                kind=BindingKind.COLOUR,
                colour="#00ff00",
                source=BindingSource.AUTOMATIC,
            )
        )
        binding = self.bible.binding_for("transistor", kind=BindingKind.COLOUR)
        assert binding is not None
        self.assertEqual(binding.colour, "#ff0000")

    def test_a_user_may_replace_their_own_lock(self) -> None:
        self.bible.bind(
            EntityBinding(
                canonical_name="X", kind=BindingKind.COLOUR, colour="#ff0000",
                source=BindingSource.USER,
            )
        )
        self.bible.bind(
            EntityBinding(
                canonical_name="X", kind=BindingKind.COLOUR, colour="#0000ff",
                source=BindingSource.USER,
            )
        )
        binding = self.bible.binding_for("X", kind=BindingKind.COLOUR)
        assert binding is not None
        self.assertEqual(binding.colour, "#0000ff")

    def test_a_binding_must_carry_its_value(self) -> None:
        """A colour binding with no colour is a silent no-op later."""
        with self.assertRaises(ValueError):
            EntityBinding(canonical_name="X", kind=BindingKind.COLOUR)
        with self.assertRaises(ValueError):
            EntityBinding(canonical_name="X", kind=BindingKind.LABEL)

    def test_reserving_a_colour_marks_it_used(self) -> None:
        self.bible.bind(
            EntityBinding(
                canonical_name="A", kind=BindingKind.COLOUR, colour="#1f77b4"
            )
        )
        self.assertFalse(self.bible.palette.is_free("#1f77b4"))
        self.assertEqual(self.bible.palette.colour_for("a"), "#1f77b4")

    def test_use_is_recorded_for_audit(self) -> None:
        self.bible.bind(
            EntityBinding(
                canonical_name="A", kind=BindingKind.COLOUR, colour="#1f77b4"
            )
        )
        scene_id = new_id(IdPrefix.SCENE)
        self.bible.note_use("A", scene_id, kind=BindingKind.COLOUR)
        self.bible.note_use("A", scene_id, kind=BindingKind.COLOUR)
        binding = self.bible.binding_for("A", kind=BindingKind.COLOUR)
        assert binding is not None
        self.assertEqual(binding.used_in_scenes, [scene_id])


class BuildingTheBible(unittest.TestCase):
    def build(self, script: str) -> tuple[VisualBible, object]:
        project_id = new_id(IdPrefix.PROJECT)
        transcript = transcript_from_text(script, project_id=project_id)
        understanding = run(
            UnderstandingService(
                engine=HeuristicUnderstandingEngine(), events=EventSink()
            ).understand(transcript)
        )
        span = transcript.span
        assert span is not None
        graph = SceneEngine(events=EventSink()).build(
            transcript=transcript,
            understanding=understanding,
            style=StyleProfile(),
            total_duration=span.end,
        )
        engine = ConsistencyEngine(events=EventSink())
        return engine.build(
            scene_graph=graph, understanding=understanding, style=StyleProfile()
        ), graph

    def test_a_recurring_entity_gets_one_colour_everywhere(self) -> None:
        bible, _graph = self.build(
            "The transistor changed everything. "
            "Bell Labs built the first transistor in 1947. "
            "By 1965 the transistor had replaced the vacuum tube. "
            "Today a transistor is measured in nanometres."
        )
        colours = [
            binding
            for binding in bible.bindings
            if binding.kind is BindingKind.COLOUR
        ]
        by_name: dict[str, set[str]] = {}
        for binding in colours:
            by_name.setdefault(binding.canonical_name.lower(), set()).add(
                binding.colour or ""
            )
        for name, values in by_name.items():
            with self.subTest(name=name):
                self.assertEqual(len(values), 1, "one entity, two colours")

    def test_two_entities_never_share_a_colour(self) -> None:
        bible, _graph = self.build(
            "The transistor and the vacuum tube did the same job. "
            "The vacuum tube was hot and fragile. "
            "The transistor was small and cool. "
            "The transistor replaced the vacuum tube almost entirely."
        )
        colours = [
            binding.colour
            for binding in bible.bindings
            if binding.kind is BindingKind.COLOUR and binding.colour
        ]
        self.assertEqual(len(colours), len(set(colours)))

    def test_a_one_scene_entity_is_not_bound(self) -> None:
        """Binding costs a palette slot; a thing mentioned once cannot clash."""
        bible, _graph = self.build(
            "Photosynthesis converts light into sugar. "
            "Chlorophyll absorbs the light. "
            "The process sustains almost all life on earth."
        )
        for binding in bible.bindings:
            with self.subTest(name=binding.canonical_name):
                self.assertGreaterEqual(len(binding.used_in_scenes), MIN_SCENES)

    def test_building_twice_produces_the_same_decisions(self) -> None:
        """Re-rendering after an edit must produce the video the user approved."""
        script = (
            "The transistor changed everything. "
            "Bell Labs built the first transistor in 1947. "
            "By 1965 the transistor had replaced the vacuum tube."
        )
        first, _ = self.build(script)
        second, _ = self.build(script)
        self.assertEqual(
            sorted(
                (binding.canonical_name, binding.colour)
                for binding in first.bindings
                if binding.kind is BindingKind.COLOUR
            ),
            sorted(
                (binding.canonical_name, binding.colour)
                for binding in second.bindings
                if binding.kind is BindingKind.COLOUR
            ),
        )

    def test_carrying_a_bible_forward_preserves_a_user_lock(self) -> None:
        script = (
            "The transistor changed everything. "
            "Bell Labs built the first transistor in 1947. "
            "By 1965 the transistor had replaced the vacuum tube."
        )
        bible, graph = self.build(script)
        bible.bind(
            EntityBinding(
                canonical_name="transistor",
                kind=BindingKind.COLOUR,
                colour="#abcdef",
                source=BindingSource.USER,
            )
        )
        project_id = new_id(IdPrefix.PROJECT)
        transcript = transcript_from_text(script, project_id=project_id)
        understanding = run(
            UnderstandingService(
                engine=HeuristicUnderstandingEngine(), events=EventSink()
            ).understand(transcript)
        )
        rebuilt = ConsistencyEngine(events=EventSink()).build(
            scene_graph=graph,  # type: ignore[arg-type]
            understanding=understanding,
            style=StyleProfile(),
            existing=bible,
        )
        binding = rebuilt.binding_for("transistor", kind=BindingKind.COLOUR)
        assert binding is not None
        self.assertEqual(binding.colour, "#abcdef")

    def test_review_reports_rather_than_corrects(self) -> None:
        bible, graph = self.build(
            "The transistor changed everything. "
            "Bell Labs built the first transistor in 1947. "
            "By 1965 the transistor had replaced the vacuum tube."
        )
        bible.bindings = [
            *bible.bindings,
            EntityBinding(
                canonical_name="ghost",
                kind=BindingKind.COLOUR,
                colour="#123456",
                used_in_scenes=[new_id(IdPrefix.SCENE), new_id(IdPrefix.SCENE)],
            ),
        ]
        issues = ConsistencyEngine(events=EventSink()).review(
            bible=bible, scene_graph=graph  # type: ignore[arg-type]
        )
        self.assertTrue(any(issue.entity_name == "ghost" for issue in issues))
        # The binding is still there: reporting, not correcting.
        self.assertIsNotNone(bible.binding_for("ghost", kind=BindingKind.COLOUR))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
