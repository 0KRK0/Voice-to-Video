"""Timeline construction, fitting and pre-flight checks.

Everything here exists because of one requirement from Stage 10 of the brief:
the renderer must not depend on every generation producing exactly the requested
duration. The fit policy is how that requirement becomes impossible to forget.
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from vtv.contracts import (
    AssetClipSource,
    CaptionCue,
    FitPolicy,
    NarrationTrack,
    ObjectRef,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    Timeline,
    TimeSpan,
    Transition,
    TransitionKind,
    TypographySpec,
    VisualClip,
)

SCENE_ID = "scn_aaaaaaaaaaaaaaaaaaaaaaaa"
AUDIO = ObjectRef(bucket="b", key="narration.webm", content_type="audio/webm")
VIDEO = ObjectRef(bucket="b", key="clip.mp4", content_type="video/mp4")


def drawn(start: float, end: float, scene_id: str = SCENE_ID) -> VisualClip:
    return VisualClip(
        scene_id=scene_id,
        span=TimeSpan.of(start, end),
        source=ProgrammaticClipSource(spec=TypographySpec(headline="a headline")),
        fit=FitPolicy.STILL,
    )


def timeline(clips: list[VisualClip], duration: float = 30.0, **overrides: object):
    fields: dict[str, object] = {
        "project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaa",
        "scene_graph_id": "sgr_aaaaaaaaaaaaaaaaaaaaaaaa",
        "narration": NarrationTrack(audio=AUDIO, duration_seconds=duration),
        "clips": clips,
        "captions": [CaptionCue(span=TimeSpan.of(0, 5), text="hello")],
    }
    fields.update(overrides)
    return Timeline(**fields)  # type: ignore[arg-type]


class TheVoiceIsTheClock(unittest.TestCase):
    def test_duration_comes_from_the_narration_alone(self) -> None:
        built = timeline([drawn(0, 10)], duration=42.0)
        self.assertEqual(built.duration_seconds, 42.0)

    def test_a_clip_may_not_run_past_the_narration(self) -> None:
        with self.assertRaises(ValidationError):
            timeline([drawn(0, 35)], duration=30.0)

    def test_a_caption_may_not_run_past_the_narration(self) -> None:
        with self.assertRaises(ValidationError):
            timeline(
                [drawn(0, 10)],
                duration=12.0,
                captions=[CaptionCue(span=TimeSpan.of(10, 15), text="too late")],
            )

    def test_clips_may_not_overlap(self) -> None:
        with self.assertRaises(ValidationError):
            timeline([drawn(0, 10), drawn(8, 15)])


class FittingIsExplicit(unittest.TestCase):
    def test_media_shorter_than_its_slot_may_not_simply_be_trimmed(self) -> None:
        # A five-second generated clip in an eight-second slot leaves three
        # seconds of nothing. The contract refuses rather than letting the
        # renderer improvise.
        with self.assertRaises(ValidationError):
            VisualClip(
                scene_id=SCENE_ID,
                span=TimeSpan.of(0, 8),
                source=AssetClipSource(
                    asset_id="ast_aaaaaaaaaaaaaaaaaaaaaaaa", object=VIDEO
                ),
                fit=FitPolicy.TRIM,
                media_duration_seconds=5.0,
            )

    def test_holding_the_last_frame_is_a_valid_answer(self) -> None:
        clip = VisualClip(
            scene_id=SCENE_ID,
            span=TimeSpan.of(0, 8),
            source=AssetClipSource(
                asset_id="ast_aaaaaaaaaaaaaaaaaaaaaaaa", object=VIDEO
            ),
            fit=FitPolicy.HOLD_LAST,
            media_duration_seconds=5.0,
        )
        self.assertIs(clip.fit, FitPolicy.HOLD_LAST)

    def test_stills_must_declare_the_still_policy(self) -> None:
        with self.assertRaises(ValidationError):
            VisualClip(
                scene_id=SCENE_ID,
                span=TimeSpan.of(0, 8),
                source=ProgrammaticClipSource(
                    spec=TypographySpec(headline="drawn live")
                ),
                fit=FitPolicy.LOOP,
            )

    def test_timed_media_may_not_claim_the_still_policy(self) -> None:
        with self.assertRaises(ValidationError):
            VisualClip(
                scene_id=SCENE_ID,
                span=TimeSpan.of(0, 4),
                source=AssetClipSource(
                    asset_id="ast_aaaaaaaaaaaaaaaaaaaaaaaa", object=VIDEO
                ),
                fit=FitPolicy.STILL,
                media_duration_seconds=5.0,
            )


class TransitionsAreCoherent(unittest.TestCase):
    def test_a_cut_has_no_duration(self) -> None:
        with self.assertRaises(ValidationError):
            Transition(kind=TransitionKind.CUT, duration_seconds=0.5)

    def test_a_dissolve_must_have_one(self) -> None:
        with self.assertRaises(ValidationError):
            Transition(kind=TransitionKind.DISSOLVE, duration_seconds=0.0)


class PreFlightChecksCatchSilentFailures(unittest.TestCase):
    def test_gaps_in_coverage_are_found_before_rendering(self) -> None:
        built = timeline([drawn(0, 10), drawn(15, 25)], duration=30.0)
        gaps = built.coverage_gaps()
        self.assertEqual(
            [(g.start, g.end) for g in gaps], [(10.0, 15.0), (25.0, 30.0)]
        )
        ok, problems = built.is_renderable()
        self.assertFalse(ok)
        self.assertTrue(any("uncovered" in problem for problem in problems))

    def test_full_coverage_is_renderable(self) -> None:
        built = timeline([drawn(0, 15), drawn(15, 30)], duration=30.0)
        self.assertEqual(built.coverage_gaps(), [])
        ok, problems = built.is_renderable()
        self.assertTrue(ok, problems)

    def test_missing_captions_are_reported_when_captions_are_enabled(self) -> None:
        built = timeline([drawn(0, 30)], duration=30.0, captions=[])
        ok, problems = built.is_renderable()
        self.assertFalse(ok)
        self.assertTrue(any("caption" in problem for problem in problems))


class FailureIsLocalNotFatal(unittest.TestCase):
    def test_a_placeholder_keeps_the_project_renderable(self) -> None:
        # Rule 8: one failed visual must not destroy the video.
        broken = VisualClip(
            scene_id=SCENE_ID,
            span=TimeSpan.of(15, 30),
            source=PlaceholderClipSource(message="Visual unavailable"),
        )
        built = timeline([drawn(0, 15), broken], duration=30.0)
        ok, problems = built.is_renderable()
        self.assertTrue(ok, problems)
        self.assertEqual(built.placeholder_count, 1)


class AttributionsAreCollectedForCredits(unittest.TestCase):
    def test_credits_are_deduplicated_in_first_use_order(self) -> None:
        def credited(start: float, end: float, credit: str) -> VisualClip:
            return VisualClip(
                scene_id=SCENE_ID,
                span=TimeSpan.of(start, end),
                source=AssetClipSource(
                    asset_id="ast_aaaaaaaaaaaaaaaaaaaaaaaa",
                    object=ObjectRef(
                        bucket="b", key="p.jpg", content_type="image/jpeg"
                    ),
                    attribution=credit,
                ),
                fit=FitPolicy.STILL,
            )

        built = timeline(
            [
                credited(0, 10, "Photo A by X (CC-BY-4.0)"),
                credited(10, 20, "Photo B by Y (CC-BY-4.0)"),
                credited(20, 30, "Photo A by X (CC-BY-4.0)"),
            ],
            duration=30.0,
        )
        self.assertEqual(
            built.attributions(),
            ["Photo A by X (CC-BY-4.0)", "Photo B by Y (CC-BY-4.0)"],
        )


if __name__ == "__main__":
    unittest.main()
