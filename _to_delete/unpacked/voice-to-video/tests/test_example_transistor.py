"""The worked example, asserted end to end.

This is the closest thing Stage 0 has to an integration test. It proves the
twelve contracts compose into one coherent project, and it pins the product
decisions that the rest of the system is supposed to preserve. If a future change
makes it impossible to express "two sentences, one visual idea", or lets an
unlicensed photograph into a timeline, this test is where it will be noticed.
"""

from __future__ import annotations

import json
import unittest

from vtv.contracts import (
    AssetClipSource,
    AssetSource,
    PipelineStage,
    ScenePurpose,
    Status,
    VisualStrategy,
)
from vtv.examples import transistor_project


class TheGraphIsInternallyConsistent(unittest.TestCase):
    def setUp(self) -> None:
        self.example = transistor_project()

    def test_every_document_references_the_same_project(self) -> None:
        project_id = self.example.project.project_id
        for document in (
            self.example.recording,
            self.example.transcript,
            self.example.understanding,
            self.example.scene_graph,
            self.example.visual_plan,
            self.example.timeline,
        ):
            self.assertEqual(document.project_id, project_id)

    def test_the_chain_of_references_is_unbroken(self) -> None:
        example = self.example
        self.assertEqual(
            example.transcript.recording_id, example.recording.recording_id
        )
        self.assertEqual(
            example.understanding.transcript_id, example.transcript.transcript_id
        )
        self.assertEqual(
            example.scene_graph.understanding_id,
            example.understanding.understanding_id,
        )
        self.assertEqual(
            example.visual_plan.scene_graph_id, example.scene_graph.scene_graph_id
        )
        self.assertEqual(
            example.timeline.scene_graph_id, example.scene_graph.scene_graph_id
        )

    def test_scene_semantic_units_exist_in_the_understanding(self) -> None:
        known = {unit.unit_id for unit in self.example.understanding.units}
        for scene in self.example.scene_graph.scenes:
            for unit_id in scene.semantic_unit_ids:
                self.assertIn(unit_id, known)

    def test_every_scene_has_exactly_one_plan_and_one_clip(self) -> None:
        example = self.example
        for scene in example.scene_graph.scenes:
            self.assertIsNotNone(example.visual_plan.plan_for(scene.scene_id))
            clips = [c for c in example.timeline.clips if c.scene_id == scene.scene_id]
            self.assertEqual(len(clips), 1, scene.scene_id)


class ProductRulesAreUpheld(unittest.TestCase):
    def setUp(self) -> None:
        self.example = transistor_project()

    def test_rule_seven_one_sentence_is_not_one_scene(self) -> None:
        segments = len(self.example.transcript.segments)
        scenes = len(self.example.scene_graph.scenes)
        self.assertLess(scenes, segments)
        # The contrast scene is the specific case: two spoken sentences merged
        # into a single visual idea.
        contrast = next(
            scene
            for scene in self.example.scene_graph.scenes
            if scene.purpose is ScenePurpose.CONTRAST
        )
        self.assertEqual(len(contrast.semantic_unit_ids), 2)

    def test_rule_six_generation_is_the_exception_not_the_default(self) -> None:
        mix = self.example.visual_plan.strategy_mix()
        generated = mix.get("generated_image", 0) + mix.get("generated_video", 0)
        total = sum(mix.values())
        self.assertLessEqual(
            generated / total,
            0.25,
            f"too much of this project reaches for generation: {mix}",
        )
        self.assertGreaterEqual(mix.get("programmatic", 0), 3)

    def test_the_project_costs_cents_not_dollars(self) -> None:
        self.assertLess(self.example.visual_plan.worst_case_cost_usd, 0.20)

    def test_rule_eight_every_expensive_shot_has_a_way_down(self) -> None:
        for plan in self.example.visual_plan.scene_plans:
            if plan.primary.strategy in {
                VisualStrategy.GENERATED_IMAGE,
                VisualStrategy.GENERATED_VIDEO,
                VisualStrategy.LICENSED_MEDIA,
            }:
                self.assertTrue(
                    plan.fallbacks,
                    f"scene {plan.scene_id} can fail with no way down",
                )
                # The last rung must be something that cannot fail.
                self.assertIs(
                    plan.fallbacks[-1].strategy, VisualStrategy.PROGRAMMATIC
                )

    def test_provenance_travels_all_the_way_to_the_screen(self) -> None:
        photo = next(
            asset
            for asset in self.example.assets
            if asset.source is AssetSource.WIKIMEDIA_COMMONS
        )
        self.assertTrue(photo.is_commercially_usable)
        credit = photo.attribution_line()
        self.assertIsNotNone(credit)
        self.assertIn(credit, self.example.timeline.attributions())

    def test_generated_assets_are_traceable_to_their_generation(self) -> None:
        generated = next(
            asset
            for asset in self.example.assets
            if asset.source is AssetSource.GENERATED
        )
        self.assertIsNotNone(generated.generation_id)


class TheTimelineIsRenderable(unittest.TestCase):
    def setUp(self) -> None:
        self.example = transistor_project()

    def test_no_gaps_and_no_placeholders(self) -> None:
        timeline = self.example.timeline
        self.assertEqual(timeline.coverage_gaps(), [])
        self.assertEqual(timeline.placeholder_count, 0)
        ok, problems = timeline.is_renderable()
        self.assertTrue(ok, problems)

    def test_the_video_is_exactly_as_long_as_the_voice(self) -> None:
        self.assertEqual(
            self.example.timeline.duration_seconds,
            self.example.recording.duration_seconds,
        )

    def test_clips_cover_the_same_spans_as_their_scenes(self) -> None:
        by_scene = {clip.scene_id: clip for clip in self.example.timeline.clips}
        for scene in self.example.scene_graph.scenes:
            clip = by_scene[scene.scene_id]
            self.assertAlmostEqual(clip.span.start, scene.span.start)
            self.assertAlmostEqual(clip.span.end, scene.span.end)

    def test_captions_cover_every_spoken_segment(self) -> None:
        self.assertEqual(
            len(self.example.timeline.captions),
            len(self.example.transcript.segments),
        )

    def test_asset_clips_point_at_assets_we_actually_hold(self) -> None:
        known = {asset.asset_id for asset in self.example.assets}
        for clip in self.example.timeline.clips:
            if isinstance(clip.source, AssetClipSource):
                self.assertIn(clip.source.asset_id, known)


class TheProjectIsComplete(unittest.TestCase):
    def test_all_stages_are_ready_and_the_project_is_regenerable(self) -> None:
        project = transistor_project().project
        self.assertEqual(project.progress, 1.0)
        self.assertIsNone(project.current_stage)
        self.assertIsNone(project.failed_stage)
        self.assertTrue(project.is_regenerable)
        self.assertIs(
            project.stage(PipelineStage.RENDERING).status, Status.READY
        )


class EverythingSurvivesSerialisation(unittest.TestCase):
    """Documents cross process boundaries as JSON. Nothing may be lost."""

    def test_round_trip_through_json_preserves_every_document(self) -> None:
        example = transistor_project()
        for document in (
            example.project,
            example.recording,
            example.transcript,
            example.understanding,
            example.scene_graph,
            example.visual_plan,
            example.timeline,
            *example.assets,
        ):
            payload = json.loads(document.model_dump_json())
            restored = type(document).model_validate(payload)
            self.assertEqual(
                restored.model_dump_json(),
                document.model_dump_json(),
                f"{type(document).__name__} did not survive a JSON round trip",
            )


if __name__ == "__main__":
    unittest.main()
