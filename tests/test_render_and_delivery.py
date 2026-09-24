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

from PIL import Image

from vtv.adapters.media import ffmpeg
from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.animation import fonts, primitives
from vtv.animation.canvas import format_number, nice_ticks
from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.contracts.base import ObjectRef, TimeSpan
from vtv.contracts.errors import ErrorCode, Status, VTVError
from vtv.contracts.language import Script
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.scene import Scene, ScenePurpose, VisualGoal
from vtv.contracts.style import AspectRatio, StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.timeline import (
    CaptionCue,
    NarrationTrack,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    Timeline,
    TransitionKind,
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
from vtv.pipeline.captions import (
    CaptionBuilder,
    caption_pages,
    to_srt,
    to_vtt,
    wrap_caption,
)
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
            "Within twenty years transistors had replaced vacuum tubes almost everywhere.", organisation_id=SYSTEM_ORGANISATION_ID,
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
        for line in lines:
            self.assertLessEqual(len(line), 42)

    def test_wrapping_never_drops_a_word(self) -> None:
        """The bug this test previously asserted was correct.

        `wrap_caption` ended `return lines[:MAX_LINES]`, and the assertion here
        was that it returned at most two lines — which is exactly what a
        truncation looks like from the outside. A sentence too long for two
        lines was drawn as its first two and the rest disappeared: the video
        read "…how software is built, and" and moved on, while the narration
        spoke the whole thing. The `.srt` and `.vtt` files were cut in the same
        place, so the download a deaf viewer depends on was missing text.

        Fitting is the renderer's job. Losing words is nobody's.
        """
        text = (
            "Computer Science is the study of how computers work, how software "
            "is built, and how we can use technology to solve real-world "
            "problems."
        )
        lines = wrap_caption(text)
        self.assertGreater(len(lines), 2, "this text needs more than one screen")
        self.assertEqual(
            " ".join(" ".join(lines).split()),
            " ".join(text.split()),
            "wrapping changed the words",
        )

    def test_a_long_cue_becomes_several_pages(self) -> None:
        """Two lines at a time, and every page holds real text."""
        text = "word " * 60
        pages = caption_pages(text)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(page), 2)
            self.assertTrue(all(line.strip() for line in page))
        flat = " ".join(" ".join(page) for page in pages)
        self.assertEqual(len(flat.split()), 60, "a page boundary ate a word")

    def test_short_text_is_still_one_page(self) -> None:
        self.assertEqual(len(caption_pages("Short enough.")), 1)

    def test_the_subtitle_files_carry_the_whole_sentence(self) -> None:
        """The `.vtt` a browser loads, and the `.srt` a platform ingests."""
        text = (
            "Computer Science is the study of how computers work, how software "
            "is built, and how we can use technology to solve real-world "
            "problems."
        )
        cue = CaptionCue(span=TimeSpan.of(0.0, 6.0), text=text)
        for rendered in (to_vtt([cue]), to_srt([cue])):
            with self.subTest(rendered.splitlines()[0]):
                self.assertIn("real-world problems.", rendered)

    def test_word_timings_are_used_when_available(self) -> None:
        words = [
            TranscriptWord(text=f"word{i}", span=TimeSpan.of(i * 0.5, i * 0.5 + 0.45))
            for i in range(40)
        ]
        segment = TranscriptSegment(
            span=TimeSpan.of(0, 20), text=" ".join(w.text for w in words), words=words
        )
        transcript = Transcript(
            organisation_id=SYSTEM_ORGANISATION_ID,
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
            "One idea here. Another idea there. A third to finish on.", organisation_id=SYSTEM_ORGANISATION_ID,
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
            organisation_id=SYSTEM_ORGANISATION_ID,
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
            organisation_id=SYSTEM_ORGANISATION_ID,
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
            organisation_id=SYSTEM_ORGANISATION_ID,
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
            organisation_id=SYSTEM_ORGANISATION_ID,
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
                    key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/p/narration.wav",
                    data=audio_path.read_bytes(),
                    content_type="audio/wav",
                )
            )
            transcript = transcript_from_text(
                "A first idea. A second idea.", organisation_id=SYSTEM_ORGANISATION_ID, project_id="prj_" + "a" * 24
            )
            timeline = Timeline(
                organisation_id=SYSTEM_ORGANISATION_ID,
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
                organisation_id=SYSTEM_ORGANISATION_ID,
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


class EveryFontLoadIsUsableOnEveryPlatform(unittest.TestCase):
    """The bug that made every render fail on Windows.

    `load()` is annotated `-> ImageFont.FreeTypeFont`, and two of its three exit
    paths returned `ImageFont.load_default_imagefont()` — a *bitmap* font, a
    different class, behind a `# type: ignore[return-value]` that silenced the
    type checker's entirely correct objection. Callers then asked it for
    `getmetrics()`, which bitmap fonts do not have:

        AttributeError: 'ImageFont' object has no attribute 'getmetrics'

    On Linux the branch never ran, because every candidate path in the module is
    a Debian-family path and one always existed. On a Windows laptop none do, so
    it ran on the first render and eleven jobs dead-lettered in a row.

    The lesson is the one this repository keeps relearning: the rule has to be
    on the object. A signature that promises a type, a body that returns another,
    and a comment telling the checker to be quiet is three places agreeing to be
    wrong together.
    """

    def test_a_loaded_font_can_always_answer_getmetrics(self) -> None:
        for script in (Script.LATIN, Script.DEVANAGARI, Script.HAN):
            with self.subTest(script.value):
                font = fonts.load(script, 48)
                self.assertTrue(
                    hasattr(font, "getmetrics"),
                    "a font without metrics crashes every caller that measures text",
                )
                ascent, _descent = font.getmetrics()
                self.assertGreater(ascent, 0)

    def test_a_machine_with_no_recognised_font_still_renders(self) -> None:
        """Windows, before this fix. Nothing resolved, so the fallback ran."""
        original = fonts.resolve_for_script
        fonts.load.cache_clear()
        try:
            fonts.resolve_for_script = lambda *a, **k: None  # type: ignore[assignment]
            font = fonts.load.__wrapped__(Script.LATIN, 64)
            self.assertTrue(hasattr(font, "getmetrics"))
            self.assertEqual(len(font.getmetrics()), 2)
        finally:
            fonts.resolve_for_script = original  # type: ignore[assignment]
            fonts.load.cache_clear()

    def test_the_desktop_candidates_are_absolute_and_unexpanded(self) -> None:
        """`Path.exists` does not expand `%WINDIR%` or `~`.

        A path with a variable in it silently never matches, which is the exact
        shape of the bug being fixed — a fallback that runs always instead of
        never.
        """
        for path, _quality in fonts._DESKTOP_LATIN:
            with self.subTest(path):
                self.assertNotIn("%", path)
                self.assertNotIn("~", path)
                self.assertNotIn("$", path)


class EveryTransitionActuallyLooksDifferent(unittest.TestCase):
    """`TransitionKind` offered five values and the renderer drew two.

    A timeline could ask for a wipe, the API would accept it, the editor would
    show it, and the video would dissolve — a feature present at every layer
    except the one the user watches. That is the worst shape a bug can have:
    nothing fails, and the only way to notice is to know what a wipe looks like.
    """

    def frames(self, kind: TransitionKind, progress: float) -> tuple[object, object]:
        from vtv.adapters.render.ffmpeg_renderer import _combine

        out = _combine(
            Image.new("RGB", (320, 180), (220, 40, 40)),
            Image.new("RGB", (320, 180), (40, 80, 220)),
            kind,
            progress,
        )
        return out.getpixel((4, 90)), out.getpixel((316, 90))

    def test_a_wipe_travels_across_rather_than_blending(self) -> None:
        """Halfway through, one side is fully the new picture and the other is
        fully the old one. A dissolve is purple on both sides."""
        left, right = self.frames(TransitionKind.WIPE, 0.5)
        self.assertEqual(left, (40, 80, 220))
        self.assertEqual(right, (220, 40, 40))

    def test_a_push_moves_both_pictures(self) -> None:
        """What separates a push from a slide-over: the outgoing frame is shoved
        off rather than covered."""
        left, right = self.frames(TransitionKind.PUSH, 0.5)
        self.assertEqual(left, (220, 40, 40))
        self.assertEqual(right, (40, 80, 220))

    def test_a_dissolve_blends(self) -> None:
        left, right = self.frames(TransitionKind.DISSOLVE, 0.5)
        self.assertEqual(left, right)
        self.assertEqual(left, (130, 60, 130))

    def test_every_transition_starts_and_ends_on_the_right_picture(self) -> None:
        """Whatever happens in between, a transition must begin as the outgoing
        frame and end as the incoming one — otherwise it flashes at a seam."""
        for kind in TransitionKind:
            if kind is TransitionKind.CUT:
                continue
            with self.subTest(kind=kind.value):
                self.assertEqual(self.frames(kind, 0.0), ((220, 40, 40), (220, 40, 40)))
                self.assertEqual(self.frames(kind, 1.0), ((40, 80, 220), (40, 80, 220)))

    def test_no_two_kinds_are_secretly_the_same_shape(self) -> None:
        """The guard against the original defect returning.

        Adding a value to `TransitionKind` and forgetting `_combine` makes the
        new transition a dissolve, which is exactly what happened to wipe and
        push. `fade` and `dissolve` are the one deliberate pair — see the
        docstring on `_combine` for why a fade here does not go through black.
        """
        shapes: dict[tuple[object, object], list[str]] = {}
        for kind in TransitionKind:
            if kind is TransitionKind.CUT:
                continue
            shapes.setdefault(self.frames(kind, 0.5), []).append(kind.value)
        collisions = [names for names in shapes.values() if len(names) > 1]
        self.assertEqual(
            collisions,
            [["fade", "dissolve"]],
            "a transition renders identically to another one — implement it in "
            "_combine, or document the pairing here as fade/dissolve is",
        )
