"""Stages 8 to 12 — animation, composition, captions, rendering and the API.

The most important test in this file is
`FailureDegradesTheShotNotTheProject`: it is Rule 8 executed rather than
asserted about.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.media import ffmpeg
from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.animation import primitives
from vtv.animation.canvas import format_number, nice_ticks
from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.contracts.base import ObjectRef, TimeSpan
from vtv.contracts.errors import ErrorCode, Status, VTVError
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.scene import Scene, ScenePurpose, VisualGoal
from vtv.contracts.style import AspectRatio, StyleProfile
from vtv.contracts.timeline import (
    NarrationTrack,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    Timeline,
)
from vtv.contracts.transcript import Transcript, TranscriptSegment, TranscriptWord
from vtv.contracts.visual_language import (
    ChartKind,
    ChartSeries,
    ChartSpec,
    ComparisonSide,
    ComparisonSpec,
    DataPoint,
    MapMarker,
    MapSpec,
    NetworkEdge,
    NetworkNode,
    NetworkSpec,
    TimelineEvent,
    TimelineSpec,
    TypographySpec,
)
from vtv.contracts.visual_plan import (
    ImageGenerationRequirements,
    LicensedMediaRequirements,
    ProgrammaticRequirements,
    SceneVisualPlan,
    VisualDirective,
    VisualPlan,
    VisualStrategy,
)
from vtv.observability.events import EventSink
from vtv.pipeline.captions import CaptionBuilder, to_srt, to_vtt, wrap_caption
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.text_entry import transcript_from_text

SIZE = RenderSize(480, 270)


def run(coro):
    return asyncio.run(coro)


def every_spec() -> dict[str, object]:
    return {
        "typography": TypographySpec(headline="A headline that has to wrap nicely"),
        "chart": ChartSpec(
            kind=ChartKind.COLUMN,
            title="Growth",
            y_label="people",
            series=[
                ChartSeries(
                    name="population",
                    points=[DataPoint(label="1800", value=1e9), DataPoint(label="2024", value=8e9)],
                )
            ],
        ),
        "timeline": TimelineSpec(
            events=[
                TimelineEvent(label="Invented", when="1947", sort_value=1947),
                TimelineEvent(label="Displaced", when="1967", sort_value=1967),
            ]
        ),
        "network": NetworkSpec(
            nodes=[NetworkNode(key="a", label="A"), NetworkNode(key="b", label="B")],
            edges=[NetworkEdge(source="a", target="b", label="replaced")],
        ),
        "comparison": ComparisonSpec(
            left=ComparisonSide(title="Before", points=["Large", "Fragile"]),
            right=ComparisonSide(title="After", points=["Tiny", "Solid"]),
        ),
        "map": MapSpec(
            markers=[MapMarker(label="Geneva", latitude=46.2, longitude=6.14)],
            scope="country",
        ),
    }


class TheAnimationEngineDrawsEveryPrimitive(unittest.TestCase):
    def test_every_declared_primitive_has_a_renderer(self) -> None:
        from vtv.contracts.visual_language import VisualPrimitive

        supported = set(primitives.supported_primitives())
        declared = {member.value for member in VisualPrimitive}
        self.assertEqual(
            declared - supported,
            set(),
            "a primitive the Director can choose but the engine cannot draw",
        )

    def test_each_primitive_renders_at_several_progress_points(self) -> None:
        engine = AnimationEngine(StyleProfile())
        for name, spec in every_spec().items():
            for progress in (0.0, 0.35, 1.0):
                image = engine.still(spec, size=SIZE, progress=progress)
                self.assertEqual(image.size, (SIZE.width, SIZE.height), name)

    def test_layouts_work_in_portrait_as_well_as_landscape(self) -> None:
        engine = AnimationEngine(StyleProfile(aspect_ratio=AspectRatio.PORTRAIT_9_16))
        portrait = RenderSize.for_aspect(AspectRatio.PORTRAIT_9_16, scale=0.25)
        for spec in every_spec().values():
            image = engine.still(spec, size=portrait, progress=1.0)
            self.assertEqual(image.size, (portrait.width, portrait.height))

    def test_rendering_is_deterministic(self) -> None:
        engine = AnimationEngine(StyleProfile())
        spec = every_spec()["chart"]
        first = engine.still(spec, size=SIZE, progress=0.6).tobytes()
        second = engine.still(spec, size=SIZE, progress=0.6).tobytes()
        self.assertEqual(first, second)

    def test_an_unknown_spec_is_refused_rather_than_drawn_blank(self) -> None:
        engine = AnimationEngine(StyleProfile())
        with self.assertRaises(ValueError):
            engine.still(object(), size=SIZE)

    def test_axis_ticks_are_readable_numbers(self) -> None:
        self.assertEqual(nice_ticks(0, 10, 4), [0.0, 2.5, 5.0, 7.5, 10.0])
        self.assertEqual(format_number(8_000_000_000), "8B")
        self.assertEqual(format_number(1500), "1.5K")


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class TheEngineProducesRealVideo(unittest.TestCase):
    def test_a_clip_encodes_to_a_playable_file(self) -> None:
        with TemporaryDirectory(prefix="vtv-test-clip-") as directory:
            output = Path(directory) / "clip.mp4"
            AnimationEngine(StyleProfile()).render_clip(
                every_spec()["timeline"], output=output, size=SIZE, duration=2.0, fps=12
            )
            self.assertTrue(output.exists())
            probe = json.loads(
                subprocess.run(
                    [
                        ffmpeg.FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_streams", str(output),
                    ],
                    capture_output=True, text=True, check=True,
                ).stdout
            )
        stream = probe["streams"][0]
        self.assertEqual(stream["codec_name"], "h264")
        self.assertEqual((stream["width"], stream["height"]), (SIZE.width, SIZE.height))
        self.assertAlmostEqual(float(stream["duration"]), 2.0, delta=0.2)

    def test_a_zero_length_clip_is_refused(self) -> None:
        with (
            TemporaryDirectory(prefix="vtv-test-clip-") as directory,
            self.assertRaises(VTVError),
        ):
                AnimationEngine(StyleProfile()).render_clip(
                    every_spec()["typography"],
                    output=Path(directory) / "x.mp4",
                    size=SIZE,
                    duration=0.0,
                )


class Captions(unittest.TestCase):
    def setUp(self) -> None:
        self.transcript = transcript_from_text(
            "The transistor was invented in 1947 at Bell Labs. "
            "It was smaller and far more efficient than the vacuum tubes that came before it. "
            "Within twenty years transistors had replaced vacuum tubes almost everywhere.",
            project_id="prj_" + "a" * 24,
        )
        self.duration = self.transcript.span.end if self.transcript.span else 0.0
        self.cues = CaptionBuilder().build(self.transcript, limit=self.duration)

    def test_cues_are_ordered_and_never_overlap(self) -> None:
        for previous, following in zip(self.cues, self.cues[1:], strict=False):
            self.assertLessEqual(previous.span.end, following.span.start + 1e-6)

    def test_cues_stay_inside_the_recording(self) -> None:
        for cue in self.cues:
            self.assertLessEqual(cue.span.end, self.duration + 1e-6)

    def test_every_cue_is_on_screen_long_enough_to_read(self) -> None:
        for cue in self.cues:
            self.assertGreaterEqual(cue.span.duration, 0.15)

    def test_lines_are_wrapped_for_legibility(self) -> None:
        lines = wrap_caption("a " * 80)
        self.assertLessEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(len(line), 42)

    def test_word_timings_are_used_when_available(self) -> None:
        words = [
            TranscriptWord(text=f"word{i}", span=TimeSpan.of(i * 0.5, i * 0.5 + 0.45))
            for i in range(40)
        ]
        segment = TranscriptSegment(
            span=TimeSpan.of(0, 20), text=" ".join(w.text for w in words), words=words
        )
        transcript = Transcript(
            project_id="prj_" + "a" * 24,
            recording_id="rec_" + "a" * 24,
            segments=[segment],
        )
        cues = CaptionBuilder().build(transcript, limit=20)
        self.assertGreater(len(cues), 1)
        self.assertTrue(any(cue.word_spans for cue in cues))

    def test_srt_and_vtt_are_well_formed(self) -> None:
        srt = to_srt(self.cues)
        self.assertIn(" --> ", srt)
        self.assertIn(",", srt.split(" --> ")[0])  # SubRip uses a comma
        vtt = to_vtt(self.cues)
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn(".", vtt.split(" --> ")[1][:12])  # WebVTT uses a dot


def scene(index: int, start: float, end: float) -> Scene:
    return Scene(
        index=index,
        span=TimeSpan.of(start, end),
        narration="some narration for this scene",
        purpose=ScenePurpose.EXPLANATION,
        visual_goal=VisualGoal.EMPHASISE_STATEMENT,
        visual_brief="show the words",
    )


class FailureDegradesTheShotNotTheProject(unittest.TestCase):
    """Rule 8, executed."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-test-compose-")
        self.storage = LocalStorageProvider(Path(self._dir.name))
        self.events = EventSink()
        self.events.record = True
        self.transcript = transcript_from_text(
            "One idea here. Another idea there. A third to finish on.",
            project_id="prj_" + "a" * 24,
        )
        self.narration = NarrationTrack(
            audio=ObjectRef(bucket="b", key="a.wav", content_type="audio/wav"),
            duration_seconds=12.0,
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _compose(self, plan: VisualPlan, scenes: list[Scene]):
        from vtv.contracts.scene import SceneGraph

        graph = SceneGraph(
            project_id="prj_" + "a" * 24,
            understanding_id="sem_" + "a" * 24,
            transcript_id=self.transcript.transcript_id,
            scenes=scenes,
        )
        composer = SceneComposer(storage=self.storage, events=self.events)
        return run(
            composer.compose(
                scene_graph=graph,
                visual_plan=plan,
                transcript=self.transcript,
                narration=self.narration,
            )
        )

    def test_an_unavailable_generator_degrades_to_a_drawn_shot(self) -> None:
        scenes = [scene(0, 0, 6), scene(1, 6, 12)]
        plan = VisualPlan(
            project_id="prj_" + "a" * 24,
            scene_graph_id="sgr_" + "a" * 24,
            scene_plans=[
                SceneVisualPlan(
                    scene_id=scenes[0].scene_id,
                    primary=VisualDirective(
                        strategy=VisualStrategy.GENERATED_IMAGE,
                        requirements=ImageGenerationRequirements(prompt="an abstract idea"),
                        rationale="no photographic referent exists for this idea",
                    ),
                    fallbacks=[
                        VisualDirective(
                            strategy=VisualStrategy.PROGRAMMATIC,
                            requirements=ProgrammaticRequirements(
                                spec=TypographySpec(headline="Fallback")
                            ),
                            rationale="type always renders, whatever else fails",
                        )
                    ],
                ),
                SceneVisualPlan(
                    scene_id=scenes[1].scene_id,
                    primary=VisualDirective(
                        strategy=VisualStrategy.PROGRAMMATIC,
                        requirements=ProgrammaticRequirements(
                            spec=TypographySpec(headline="Second")
                        ),
                        rationale="a statement, so the words are the content",
                    ),
                ),
            ],
        )
        # No router and no asset resolver: generation is simply unavailable.
        result = self._compose(plan, scenes)
        self.assertEqual(result.timeline.placeholder_count, 0)
        first = result.timeline.clips[0]
        self.assertIsInstance(first.source, ProgrammaticClipSource)
        self.assertTrue(first.degradation, "the descent must be recorded")
        ok, problems = result.timeline.is_renderable()
        self.assertTrue(ok, problems)

    def test_an_exhausted_ladder_produces_a_placeholder_not_a_hole(self) -> None:
        scenes = [scene(0, 0, 12)]
        plan = VisualPlan(
            project_id="prj_" + "a" * 24,
            scene_graph_id="sgr_" + "a" * 24,
            scene_plans=[
                SceneVisualPlan(
                    scene_id=scenes[0].scene_id,
                    primary=VisualDirective(
                        strategy=VisualStrategy.LICENSED_MEDIA,
                        requirements=LicensedMediaRequirements(query="anything at all"),
                        rationale="a real subject deserves a real photograph",
                    ),
                )
            ],
        )
        result = self._compose(plan, scenes)
        self.assertEqual(result.timeline.placeholder_count, 1)
        # The video still renders and the narration is still heard.
        ok, problems = result.timeline.is_renderable()
        self.assertTrue(ok, problems)
        self.assertEqual(result.timeline.coverage_gaps(), [])

    def test_the_timeline_always_covers_the_whole_narration(self) -> None:
        scenes = [scene(0, 0, 4), scene(1, 6, 9)]  # a deliberate gap
        plan = VisualPlan(
            project_id="prj_" + "a" * 24,
            scene_graph_id="sgr_" + "a" * 24,
            scene_plans=[
                SceneVisualPlan(
                    scene_id=item.scene_id,
                    primary=VisualDirective(
                        strategy=VisualStrategy.PROGRAMMATIC,
                        requirements=ProgrammaticRequirements(
                            spec=TypographySpec(headline=f"Scene {index}")
                        ),
                        rationale="a statement, so the words are the content",
                    ),
                )
                for index, item in enumerate(scenes)
            ],
        )
        result = self._compose(plan, scenes)
        self.assertEqual(result.timeline.coverage_gaps(), [])
        self.assertAlmostEqual(result.timeline.clips[-1].span.end, 12.0)


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class RenderingProducesAPlayableVideo(unittest.TestCase):
    def test_a_timeline_becomes_an_mp4_with_audio(self) -> None:
        with TemporaryDirectory(prefix="vtv-test-render-") as directory:
            root = Path(directory)
            storage = LocalStorageProvider(root / "storage")
            audio_path = root / "narration.wav"
            ffmpeg.synthesise_tone_audio(
                audio_path, duration=6.0, segments=[(0.3, 2.5), (3.0, 5.7)]
            )
            audio = run(
                storage.put(
                    key="projects/p/narration.wav",
                    data=audio_path.read_bytes(),
                    content_type="audio/wav",
                )
            )
            transcript = transcript_from_text(
                "A first idea. A second idea.", project_id="prj_" + "a" * 24
            )
            timeline = Timeline(
                project_id="prj_" + "a" * 24,
                scene_graph_id="sgr_" + "a" * 24,
                narration=NarrationTrack(audio=audio, duration_seconds=6.0),
                clips=[
                    __import__("vtv.contracts.timeline", fromlist=["VisualClip"]).VisualClip(
                        scene_id="scn_" + "a" * 24,
                        span=TimeSpan.of(0, 3),
                        source=ProgrammaticClipSource(
                            spec=TypographySpec(headline="First")
                        ),
                    ),
                    __import__("vtv.contracts.timeline", fromlist=["VisualClip"]).VisualClip(
                        scene_id="scn_" + "b" * 24,
                        span=TimeSpan.of(3, 6),
                        source=PlaceholderClipSource(message="Visual unavailable"),
                    ),
                ],
                captions=CaptionBuilder().build(transcript, limit=6.0),
                status=Status.READY,
            )
            renderer = FfmpegRenderer(
                storage=storage, events=EventSink(), workdir=root / "work"
            )
            job = run(
                renderer.render(
                    timeline=timeline,
                    settings=RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24),
                )
            )
            self.assertIs(job.status, Status.READY)
            assert job.output is not None
            path = storage.path_for(job.output)
            probe = json.loads(
                subprocess.run(
                    [
                        ffmpeg.FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path),
                    ],
                    capture_output=True, text=True, check=True,
                ).stdout
            )
        kinds = {stream["codec_type"] for stream in probe["streams"]}
        self.assertEqual(kinds, {"video", "audio"})
        self.assertAlmostEqual(float(probe["format"]["duration"]), 6.0, delta=0.3)
        # A placeholder shot does not stop the render.
        self.assertEqual(job.progress, 1.0)

    def test_a_timeline_with_gaps_is_refused_before_encoding(self) -> None:
        with TemporaryDirectory(prefix="vtv-test-render-") as directory:
            storage = LocalStorageProvider(Path(directory))
            timeline = Timeline(
                project_id="prj_" + "a" * 24,
                scene_graph_id="sgr_" + "a" * 24,
                narration=NarrationTrack(
                    audio=ObjectRef(bucket="b", key="a.wav", content_type="audio/wav"),
                    duration_seconds=10.0,
                ),
                clips=[],
                captions=[],
            )
            renderer = FfmpegRenderer(storage=storage, events=EventSink())
            with self.assertRaises(VTVError) as caught:
                run(renderer.render(timeline=timeline, settings=RenderSettings()))
        self.assertIs(caught.exception.info.code, ErrorCode.RENDER_FAILED)


if __name__ == "__main__":
    unittest.main()
