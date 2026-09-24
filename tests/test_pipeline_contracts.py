"""Transcript, understanding and scene contracts.

The invariants tested here are the ones that let each stage trust the stage
before it. Without them, every consumer would have to defensively re-check
ordering and reference integrity, and one of them would forget.
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from vtv.contracts import (
    Continuity,
    Entity,
    EntityType,
    Relation,
    Scene,
    SceneGraph,
    ScenePurpose,
    SemanticIntent,
    SemanticUnit,
    Shot,
    TimeSpan,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    Understanding,
    VisualGoal,
)
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID


def segment(start: float, end: float, text: str = "hello there") -> TranscriptSegment:
    return TranscriptSegment(span=TimeSpan.of(start, end), text=text)


class TranscriptInvariants(unittest.TestCase):
    def test_segments_must_not_overlap(self) -> None:
        with self.assertRaises(ValidationError):
            Transcript(
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
                recording_id="rec_aaaaaaaaaaaaaaaaaaaaaaaa",
                segments=[segment(0, 5), segment(4, 9)],
            )

    def test_segments_must_be_in_ascending_order(self) -> None:
        with self.assertRaises(ValidationError):
            Transcript(
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
                recording_id="rec_aaaaaaaaaaaaaaaaaaaaaaaa",
                segments=[segment(10, 15), segment(0, 5)],
            )

    def test_word_timings_must_lie_inside_their_segment(self) -> None:
        with self.assertRaises(ValidationError):
            TranscriptSegment(
                span=TimeSpan.of(0, 5),
                text="a b",
                words=[TranscriptWord(text="b", span=TimeSpan.of(4.5, 6.0))],
            )

    def test_text_is_derived_not_stored(self) -> None:
        transcript = Transcript(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
            recording_id="rec_aaaaaaaaaaaaaaaaaaaaaaaa",
            segments=[segment(0, 5, "one two"), segment(5, 9, "three")],
        )
        self.assertEqual(transcript.text, "one two three")
        assert transcript.span is not None
        self.assertEqual((transcript.span.start, transcript.span.end), (0.0, 9.0))
        self.assertEqual(transcript.text_in(TimeSpan.of(5.5, 8.0)), "three")


class UnderstandingInvariants(unittest.TestCase):
    def setUp(self) -> None:
        self.a = Entity(name="transistor", type=EntityType.TECHNOLOGY)
        self.b = Entity(name="Bell Labs", type=EntityType.ORGANIZATION)
        self.relation = Relation(
            subject_id=self.a.entity_id,
            predicate="invented_at",
            object_id=self.b.entity_id,
        )

    def understanding(self, **overrides: object) -> Understanding:
        fields: dict[str, object] = {
            "project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaa",
            "transcript_id": "tsc_aaaaaaaaaaaaaaaaaaaaaaaa",
            "entities": [self.a, self.b],
            "relations": [self.relation],
            "units": [],
        }
        fields.update(overrides)
        return Understanding(**fields)  # type: ignore[arg-type]

    def test_dangling_entity_reference_is_rejected(self) -> None:
        unit = SemanticUnit(
            span=TimeSpan.of(0, 5),
            text="x",
            proposition="x",
            intent=SemanticIntent.CLAIM,
            entity_ids=["ent_zzzzzzzzzzzzzzzzzzzzzzzz"],
        )
        with self.assertRaises(ValidationError):
            self.understanding(units=[unit])

    def test_relation_endpoints_must_exist(self) -> None:
        orphan = Relation(
            subject_id=self.a.entity_id,
            predicate="relates_to",
            object_id="ent_zzzzzzzzzzzzzzzzzzzzzzzz",
        )
        with self.assertRaises(ValidationError):
            self.understanding(relations=[orphan])

    def test_relations_may_not_be_self_loops(self) -> None:
        with self.assertRaises(ValidationError):
            Relation(
                subject_id=self.a.entity_id,
                predicate="is",
                object_id=self.a.entity_id,
            )

    def test_predicates_are_normalised_to_snake_case(self) -> None:
        relation = Relation(
            subject_id=self.a.entity_id,
            predicate="Invented At",
            object_id=self.b.entity_id,
        )
        self.assertEqual(relation.predicate, "invented_at")

    def test_units_must_be_in_time_order(self) -> None:
        late = SemanticUnit(
            span=TimeSpan.of(10, 15), text="x", proposition="x",
            intent=SemanticIntent.CLAIM,
        )
        early = SemanticUnit(
            span=TimeSpan.of(0, 5), text="y", proposition="y",
            intent=SemanticIntent.CLAIM,
        )
        with self.assertRaises(ValidationError):
            self.understanding(units=[late, early])

    def test_filler_is_not_visualisable(self) -> None:
        filler = SemanticUnit(
            span=TimeSpan.of(0, 5), text="um, so", proposition="filler",
            intent=SemanticIntent.FILLER,
        )
        self.assertFalse(filler.is_visualisable)


def scene(index: int, start: float, end: float, **overrides: object) -> Scene:
    fields: dict[str, object] = {
        "index": index,
        "span": TimeSpan.of(start, end),
        "narration": "some narration",
        "purpose": ScenePurpose.EXPLANATION,
        "visual_goal": VisualGoal.EMPHASISE_STATEMENT,
        "visual_brief": "show the words",
    }
    fields.update(overrides)
    return Scene(**fields)  # type: ignore[arg-type]


class SceneInvariants(unittest.TestCase):
    def test_a_scene_too_short_to_land_is_rejected(self) -> None:
        # Rule 7's corollary: splitting on punctuation produces sub-second
        # scenes, and this is where that mistake is caught.
        with self.assertRaises(ValidationError):
            scene(0, 0.0, 0.8)

    def test_shots_must_lie_within_their_scene(self) -> None:
        with self.assertRaises(ValidationError):
            scene(
                0, 0.0, 10.0,
                shots=[Shot(span=TimeSpan.of(9.0, 12.0), beat="drift")],
            )

    def test_shots_must_not_overlap(self) -> None:
        with self.assertRaises(ValidationError):
            scene(
                0, 0.0, 10.0,
                shots=[
                    Shot(span=TimeSpan.of(0.0, 6.0), beat="one"),
                    Shot(span=TimeSpan.of(5.0, 9.0), beat="two"),
                ],
            )


def scene_graph(scenes: list[Scene]) -> SceneGraph:
    return SceneGraph(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
        understanding_id="sem_aaaaaaaaaaaaaaaaaaaaaaaa",
        transcript_id="tsc_aaaaaaaaaaaaaaaaaaaaaaaa",
        scenes=scenes,
    )


class SceneGraphInvariants(unittest.TestCase):
    def test_indices_must_match_position(self) -> None:
        with self.assertRaises(ValidationError):
            scene_graph([scene(0, 0, 5), scene(5, 5, 10)])

    def test_scenes_must_not_overlap(self) -> None:
        with self.assertRaises(ValidationError):
            scene_graph([scene(0, 0, 10), scene(1, 8, 15)])

    def test_gaps_are_reported_not_silently_accepted(self) -> None:
        graph = scene_graph([scene(0, 0, 5), scene(1, 9, 14)])
        gaps = graph.gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0].start, gaps[0].end), (5.0, 9.0))

    def test_a_contiguous_graph_has_no_gaps(self) -> None:
        graph = scene_graph([scene(0, 0, 5), scene(1, 5, 14)])
        self.assertEqual(graph.gaps(), [])

    def test_continuity_carries_across_scenes(self) -> None:
        entity_id = "ent_aaaaaaaaaaaaaaaaaaaaaaaa"
        graph = scene_graph(
            [
                scene(0, 0, 5, entity_ids=[entity_id]),
                scene(
                    1, 5, 14,
                    continuity=Continuity(
                        carried_entity_ids=[entity_id],
                        continues_previous_visual=True,
                    ),
                ),
            ]
        )
        self.assertTrue(graph.scenes[1].continuity.continues_previous_visual)


if __name__ == "__main__":
    unittest.main()
