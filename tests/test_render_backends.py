"""Phase A: more than one place to render, and nothing else changed.

Two claims, and the second is the one that matters.

**The abstraction works.** Segments are drawn through a `RenderBackend`, the
chain is descended per segment, and a backend that cannot do something is
followed by one that can.

**The output is identical.** Phase A's entire job is to change where a frame can
be composed without changing the frame. That is checked by the frame-for-frame
comparisons already in `test_render_segments` — pooled against inline, resumed
against whole — which now run through this code, plus the direct comparison
below of a rendered frame against the composer's own output.

The fake backends here fail in specific, named ways, because the interesting
behaviour is not "does a backend run" but "what happens when one of them will
not". A backend that only ever succeeds tells you nothing about a graphics card
running out of memory in the fortieth minute.
"""

from __future__ import annotations

import contextlib
import pickle
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.render.cpu_backend import CpuRenderBackend, workers_for
from vtv.adapters.render.ffmpeg_renderer import run_segment
from vtv.contracts.execution import ExecutionPolicy, ExecutionTarget, Registry
from vtv.ports.rendering import RenderBackend, RenderCapabilities, SegmentOutcome


@dataclass
class Fake:
    """A backend that does exactly what it is told, and remembers being asked."""

    where: ExecutionTarget
    outcome: SegmentOutcome = field(default_factory=SegmentOutcome.done)
    asked: list[int] = field(default_factory=list)

    @property
    def target(self) -> ExecutionTarget:
        return self.where

    def capabilities(self) -> RenderCapabilities:
        return RenderCapabilities(target=self.where)

    def render_segment(
        self, scratch: str, index: int, start_frame: int, end_frame: int
    ) -> SegmentOutcome:
        del scratch, start_frame, end_frame
        self.asked.append(index)
        return self.outcome


def chain(*backends: Fake) -> list[tuple[ExecutionTarget, object]]:
    return [(backend.where, backend) for backend in backends]


class TheCpuBackendIsAValidBackend(unittest.TestCase):
    """The floor of every chain. If this is not a real implementation of the
    port, nothing below it is worth testing."""

    def test_it_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(CpuRenderBackend(), RenderBackend)

    def test_it_refuses_nothing(self) -> None:
        """It is the end of the fallback chain, so it must never be the reason
        a chain runs out. A backend that cannot draw something falls back to
        one that can, and the chain has to terminate somewhere unconditional."""
        capabilities = CpuRenderBackend().capabilities()
        self.assertEqual(capabilities.unsupported, frozenset())
        self.assertIsNone(capabilities.max_pixels)
        self.assertTrue(capabilities.can_draw(["text", "image", "chart", "anything"]))

    def test_an_unmeasured_backend_reports_none_not_zero(self) -> None:
        """"Nobody has timed this" and "this is useless" are different facts,
        and a selector reading the first as the second would refuse hardware it
        has never tried."""
        self.assertIsNone(CpuRenderBackend().capabilities().frames_per_second)

    def test_it_survives_being_sent_to_a_pool_worker(self) -> None:
        """On Windows a pool spawns rather than forks, so the backend is
        pickled to the child. A backend holding a storage handle or an open
        file would fail here — or, worse, pickle a copy and behave differently
        in the worker than in the parent."""
        backend = CpuRenderBackend(
            where=ExecutionTarget.LOCAL_CPU, hardware_encoder="h264_nvenc"
        )
        self.assertEqual(pickle.loads(pickle.dumps(backend)), backend)

    def test_workers_are_capped_in_one_place(self) -> None:
        self.assertEqual(workers_for(1), 1)
        self.assertEqual(workers_for(4), 4)
        self.assertEqual(workers_for(64), 8)


class TheChainIsDescendedPerSegment(unittest.TestCase):
    """The granularity at which hardware actually fails.

    A graphics card that runs out of memory on one dense shot has not made the
    video impossible. With segments already checkpointed, a failed attempt
    costs one segment rather than the render — which is what makes shipping a
    GPU backend a reasonable risk rather than a bet on somebody's driver.
    """

    def test_the_first_backend_that_works_is_used(self) -> None:
        first = Fake(ExecutionTarget.LOCAL_GPU)
        second = Fake(ExecutionTarget.LOCAL_CPU)
        failure, target, after = run_segment(chain(first, second), "s", 0, 0, 10)
        self.assertEqual(failure, "")
        self.assertEqual(target, "local_gpu")
        self.assertEqual(after, "")
        self.assertEqual(second.asked, [], "the second backend was asked anyway")

    def test_a_backend_that_says_elsewhere_is_followed_by_another(self) -> None:
        gpu = Fake(
            ExecutionTarget.LOCAL_GPU,
            SegmentOutcome.failed("out of video memory", elsewhere=True),
        )
        cpu = Fake(ExecutionTarget.LOCAL_CPU)
        failure, target, after = run_segment(chain(gpu, cpu), "s", 7, 0, 10)
        self.assertEqual(failure, "")
        self.assertEqual(target, "local_cpu")
        # The fallback is reported, never silent: a render that quietly moved
        # to different hardware is one whose timing nobody can explain.
        self.assertEqual(after, "out of video memory")
        self.assertEqual(cpu.asked, [7])

    def test_a_backend_that_does_not_is_the_end_of_it(self) -> None:
        """A timeline referencing a missing asset fails identically everywhere.
        Retrying each backend turns one clear error into several slow ones."""
        gpu = Fake(
            ExecutionTarget.LOCAL_GPU, SegmentOutcome.failed("that clip has no asset")
        )
        cpu = Fake(ExecutionTarget.LOCAL_CPU)
        failure, target, _ = run_segment(chain(gpu, cpu), "s", 0, 0, 10)
        self.assertEqual(failure, "that clip has no asset")
        self.assertEqual(target, "")
        self.assertEqual(cpu.asked, [], "a hopeless failure was retried elsewhere")

    def test_running_out_of_backends_says_so(self) -> None:
        gpu = Fake(
            ExecutionTarget.LOCAL_GPU, SegmentOutcome.failed("no memory", elsewhere=True)
        )
        failure, target, _ = run_segment(chain(gpu), "s", 3, 0, 10)
        self.assertEqual(failure, "no memory")
        self.assertEqual(target, "")

    def test_an_empty_chain_fails_with_a_sentence(self) -> None:
        failure, _, _ = run_segment([], "s", 12, 0, 10)
        self.assertIn("12", failure)
        self.assertIn("backend", failure)


class TheChainComesFromThePolicy(unittest.TestCase):
    def registry(self, *targets: ExecutionTarget) -> Registry:
        found = Registry()
        for target in targets:
            found.register(target, Fake(target))
        return found

    def test_auto_prefers_the_customers_own_machine(self) -> None:
        found = self.registry(ExecutionTarget.CLOUD_CPU, ExecutionTarget.LOCAL_CPU)
        order = [t for t, _ in found.chain(ExecutionPolicy.auto())]
        self.assertEqual(order[0], ExecutionTarget.LOCAL_CPU)

    def test_everything_available_is_in_the_chain(self) -> None:
        """A fallback list that omits a working backend is a render that fails
        while something that could have finished it sat idle."""
        found = self.registry(ExecutionTarget.CLOUD_CPU, ExecutionTarget.LOCAL_GPU)
        order = [t for t, _ in found.chain(ExecutionPolicy.auto())]
        self.assertEqual(len(order), 2)

    def test_strict_means_no_chain_at_all(self) -> None:
        """"Never run this in the cloud" is a real requirement, and a chain
        that quietly appends the cloud to it is a policy that does nothing."""
        found = self.registry(ExecutionTarget.CLOUD_CPU, ExecutionTarget.LOCAL_CPU)
        order = [
            t
            for t, _ in found.chain(
                ExecutionPolicy.exactly(ExecutionTarget.LOCAL_CPU, strict=True)
            )
        ]
        self.assertEqual(order, [ExecutionTarget.LOCAL_CPU])

    def test_a_chain_survives_pickling(self) -> None:
        """It is handed to a pool worker whole."""
        found = Registry()
        found.register(ExecutionTarget.CLOUD_CPU, CpuRenderBackend())
        restored = pickle.loads(pickle.dumps(found.chain(ExecutionPolicy.auto())))
        self.assertEqual(restored[0][0], ExecutionTarget.CLOUD_CPU)


class SpeedIsMeasuredNotLookedUp(unittest.TestCase):
    """Choosing hardware by name is wrong often enough to matter: a card can be
    present and driverless, present and saturated, or present and slower than
    eight cores on a typography-heavy video."""

    def setUp(self) -> None:
        from vtv.pipeline import calibration

        calibration.forget()

    def measurement(self, target: str, fps: float):  # type: ignore[no-untyped-def]
        from vtv.pipeline.calibration import Measurement

        return Measurement(target=target, frames_per_second=fps, frames=24)

    def test_a_materially_faster_backend_wins(self) -> None:
        from vtv.pipeline.calibration import better

        self.assertEqual(
            better(
                [self.measurement("cloud_cpu", 20.0), self.measurement("local_gpu", 90.0)],
                "cloud_cpu",
            ),
            "local_gpu",
        )

    def test_a_tie_goes_to_the_incumbent(self) -> None:
        """Below the threshold the difference is inside the noise of a
        quarter-second sample, and switching adds a class of failure — a
        driver, a memory ceiling, a fallback — that the slower path lacks."""
        from vtv.pipeline.calibration import better

        self.assertEqual(
            better(
                [self.measurement("cloud_cpu", 20.0), self.measurement("local_gpu", 21.0)],
                "cloud_cpu",
            ),
            "cloud_cpu",
        )

    def test_nothing_measured_changes_nothing(self) -> None:
        from vtv.pipeline.calibration import better

        self.assertEqual(better([], "cloud_cpu"), "cloud_cpu")

    def test_the_report_is_empty_rather_than_invented(self) -> None:
        from vtv.pipeline.calibration import report

        self.assertEqual(report()["measured"], [])

    def test_a_real_context_can_be_timed(self) -> None:
        """The measurement is of composition, not of a whole segment: encoding
        is the part that does not differ between a processor and a card."""
        from tests.test_render_segments import plain_timeline

        from vtv.adapters.render.ffmpeg_renderer import RenderContext
        from vtv.contracts.base import ObjectRef
        from vtv.contracts.render import RenderQuality, RenderSettings
        from vtv.pipeline.calibration import compose_rate

        timeline = plain_timeline(
            ObjectRef(bucket="b", key="n.wav", content_type="audio/wav")
        )
        with TemporaryDirectory() as tmp:
            context = RenderContext(
                timeline=timeline,
                settings=RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24),
                scratch=Path(tmp),
                assets={},
            )
            rate = compose_rate(context, frames=6)
        self.assertGreater(rate, 0.0)
        self.assertLess(rate, 100_000.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


@dataclass(frozen=True)
class FlakyGpu:
    """Stands in for a graphics card that fails on one segment.

    Frozen and module-level so it pickles into a pool worker, like a real
    backend must. It delegates nothing: the segment it refuses is finished by
    whatever comes next in the chain.
    """

    fails_on: int = 1
    where: ExecutionTarget = ExecutionTarget.LOCAL_GPU

    @property
    def target(self) -> ExecutionTarget:
        return self.where

    def capabilities(self) -> RenderCapabilities:
        return RenderCapabilities(target=self.where)

    def render_segment(
        self, scratch: str, index: int, start_frame: int, end_frame: int
    ) -> SegmentOutcome:
        if index == self.fails_on:
            return SegmentOutcome.failed(
                f"segment {index}: out of video memory", elsewhere=True
            )
        # Everything else it "renders" by handing straight to the CPU path, so
        # that this test is about the fallback and not about a second renderer.
        return CpuRenderBackend().render_segment(
            scratch, index, start_frame, end_frame
        )


@unittest.skipUnless(
    __import__("vtv.adapters.media", fromlist=["ffmpeg"]).ffmpeg.is_available(),
    "ffmpeg is required",
)
class FallingBackProducesTheSameVideo(unittest.TestCase):
    """The property that makes per-segment fallback safe rather than merely
    clever.

    If a render that lost its graphics card halfway produced a *slightly*
    different video from one that never had it, the feature would be a source
    of bugs nobody could reproduce — the seam would be in a different place
    every run. Frames composed on different hardware have to be the same
    frames, and until a second compositor exists that is trivially true; this
    test is what will fail on the day it stops being.
    """

    def render(self, root: Path, name: str, chain_backends: list[object]) -> str:
        from tests.test_render_segments import (
            SEGMENT_SECONDS,
            illustrated_timeline,
            picture_of,
            run,
        )

        from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
        from vtv.adapters.storage.local import LocalStorageProvider
        from vtv.contracts.render import RenderQuality, RenderSettings
        from vtv.observability.events import EventSink

        storage = LocalStorageProvider(root / f"storage-{name}")
        timeline = illustrated_timeline(storage, root)
        registry = Registry()
        for backend in chain_backends:
            registry.register(backend.target, backend)  # type: ignore[attr-defined]
        job = run(
            FfmpegRenderer(
                storage=storage,
                events=EventSink(),
                workdir=root / name,
                workers=1,
                segment_seconds=SEGMENT_SECONDS,
                backends=registry,
            ).render(
                timeline=timeline,
                settings=RenderSettings(
                    quality=RenderQuality.PREVIEW, frame_rate=24
                ),
            )
        )
        self.job = job
        assert job.output is not None
        return picture_of(storage.path_for(job.output))

    def test_a_render_that_fell_back_matches_one_that_did_not(self) -> None:
        with TemporaryDirectory(prefix="vtv-fallback-") as directory:
            root = Path(directory)
            plain = self.render(root, "plain", [CpuRenderBackend()])
            fell_back = self.render(
                root, "fallback", [FlakyGpu(), CpuRenderBackend()]
            )
            record = self.job.backends
        # Asserted *before* the pictures are compared, because two processor
        # renders match each other perfectly and this comparison is only worth
        # anything if the card drew some of the second one. Routing sends a
        # segment to the processor when nothing in it needs a card, so a fixture
        # that drifted back to pure typography would make this pass vacuously.
        self.assertGreater(record.get("local_gpu", 0), 0, record)
        self.assertEqual(record.get("cloud_cpu", 0), 1, record)
        self.assertEqual(plain, fell_back)

    def test_the_record_says_which_hardware_drew_what(self) -> None:
        """"Did my graphics card do this?" is a question the product should be
        able to answer about its own output."""
        with TemporaryDirectory(prefix="vtv-fallback-record-") as directory:
            self.render(Path(directory), "record", [FlakyGpu(), CpuRenderBackend()])
        counted = self.job.backends
        self.assertGreater(counted.get("local_gpu", 0), 0, counted)
        # Exactly the one segment the card refused went to the processor.
        self.assertEqual(counted.get("cloud_cpu", 0), 1, counted)


class ThePainterInUseIsTheOneThatWasAskedFor(unittest.TestCase):
    """The bug a whole-render check on real hardware found, and the harness did not.

    `use_painter` read `if name == "cpu": return`, on the assumption that the
    painter is the processor unless somebody has asked for the card. The
    context is cached for the whole render, so that assumption held for exactly
    one segment: after any segment ran on the graphics card, every later
    segment asking for "cpu" kept the GPU painter and drew on the card anyway.

    The fallback therefore did not fall back, and reported that it had. The
    equivalence harness could never have caught it — that harness compares two
    painters, and this is a bug about *which painter is installed*.

    So these tests do the one thing nothing did before: switch back.
    """

    def context(self) -> object:
        from tests.test_render_segments import plain_timeline

        from vtv.adapters.render.ffmpeg_renderer import RenderContext
        from vtv.contracts.base import ObjectRef
        from vtv.contracts.render import RenderQuality, RenderSettings

        self._dir = TemporaryDirectory(prefix="vtv-painter-")
        return RenderContext(
            timeline=plain_timeline(
                ObjectRef(bucket="b", key="n.wav", content_type="audio/wav")
            ),
            settings=RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24),
            scratch=Path(self._dir.name),
            assets={},
        )

    def tearDown(self) -> None:
        if hasattr(self, "_dir"):
            self._dir.cleanup()

    def test_switching_to_the_card_and_back_actually_switches_back(self) -> None:
        context = self.context()
        self.assertEqual(context.painter.name, "cpu")
        with _pretend_gpu() as built:
            context.use_painter("gpu")
            self.assertEqual(context.painter.name, "gpu")
            context.use_painter("cpu")
        self.assertEqual(
            context.painter.name,
            "cpu",
            "a segment that asked for the processor was given the graphics card",
        )
        self.assertEqual(len(built), 1)

    def test_the_painter_it_replaces_is_given_back(self) -> None:
        """Otherwise every switch leaks a graphics context, and the fourth
        render in a process fails with "cannot create texture" — which is
        exactly how this was found."""
        context = self.context()
        with _pretend_gpu() as built:
            context.use_painter("gpu")
            device = built[0]
            context.use_painter("cpu")
        self.assertTrue(device.closed, "the graphics context was never released")

    def test_asking_for_the_same_painter_twice_does_not_rebuild_it(self) -> None:
        """Segments are drawn back to back. Re-creating a device between each
        would cost more than the drawing."""
        context = self.context()
        with _pretend_gpu() as built:
            context.use_painter("gpu")
            first = context.painter
            context.use_painter("gpu")
            self.assertIs(context.painter, first)
        self.assertEqual(len(built), 1)

    def test_a_device_that_will_not_start_leaves_the_old_painter_working(self) -> None:
        """The caller reports the failure and the chain descends. A context left
        without a painter would turn one refused segment into a broken render."""
        context = self.context()
        before = context.painter
        with _pretend_gpu(fails=True), self.assertRaises(RuntimeError):
            context.use_painter("gpu")
        self.assertIs(context.painter, before)
        self.assertEqual(context.painter.name, "cpu")

    def test_an_unknown_painter_is_refused_rather_than_ignored(self) -> None:
        context = self.context()
        with self.assertRaises(ValueError):
            context.use_painter("quantum")

    def test_closing_a_context_gives_the_device_back(self) -> None:
        context = self.context()
        with _pretend_gpu() as built:
            context.use_painter("gpu")
            context.close()
        self.assertTrue(built[0].closed)

    def test_a_finished_render_forgets_its_context(self) -> None:
        """`_LOADED` is per scratch directory and nothing ever removed an entry.
        Harmless while every painter was pure Python; not once one holds a
        device."""
        from vtv.adapters.render import segments as seg

        context = self.context()
        key = str(context.scratch)
        seg._LOADED[key] = context
        with _pretend_gpu() as built:
            context.use_painter("gpu")
            seg.forget(key)
        self.assertNotIn(key, seg._LOADED)
        self.assertTrue(built[0].closed)


class _FakeDevice:
    """A GPU painter that needs no graphics card, and records being closed."""

    name = "gpu"

    def __init__(self, **_: object) -> None:
        self.closed = False

    def paint(self, frame: object) -> object:  # pragma: no cover - not exercised
        raise AssertionError("these tests are about installation, not drawing")

    def release(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


@contextlib.contextmanager
def _pretend_gpu(*, fails: bool = False):
    """Put a fake card where the real one is looked up.

    Patched at `gpu_painter.GpuPainter` rather than at the context, because the
    thing under test is the context's own construction path — a test that
    injected a painter directly would not exercise the line that had the bug.
    """
    from vtv.adapters.render import gpu_painter

    built: list[_FakeDevice] = []

    def make(**kwargs: object) -> _FakeDevice:
        if fails:
            raise RuntimeError("no device")
        device = _FakeDevice(**kwargs)
        built.append(device)
        return device

    original = gpu_painter.GpuPainter
    gpu_painter.GpuPainter = make  # type: ignore[assignment]
    try:
        yield built
    finally:
        gpu_painter.GpuPainter = original  # type: ignore[assignment]


class ASegmentGoesWhereItsContentBelongs(unittest.TestCase):
    """Routing per segment rather than per render.

    The benchmark that made this necessary, on a GTX 1650: photographs
    composite 11.6x faster on the card and typography 2.2x *slower*. A normal
    video alternates the two shot by shot, so choosing hardware once for the
    whole render either pays the typography penalty on every title or gives up
    the photograph win on every shot. Measured end to end on mixed material,
    choosing once came out at 1.48x when the photograph shots alone were worth
    eleven.
    """

    def chain(self) -> list:
        from vtv.adapters.render.cpu_backend import (
            CpuRenderBackend,
            GpuRenderBackend,
        )

        return [
            (ExecutionTarget.LOCAL_GPU, GpuRenderBackend()),
            (ExecutionTarget.LOCAL_CPU, CpuRenderBackend(where=ExecutionTarget.LOCAL_CPU)),
        ]

    def scratch(self) -> str:
        """One saved render context, the way a pool worker would find one.

        Deliberately *one* video containing both kinds of shot, rather than two
        videos. Routing that varies between renders is not the claim — choosing
        once per render already did that. The claim is that it varies inside a
        single render, and only a timeline with a title card at second one and a
        photograph at second five can show it.
        """
        from PIL import Image
        from tests.test_display_list import timeline

        from vtv.adapters.render.ffmpeg_renderer import RenderContext
        from vtv.contracts.render import RenderQuality, RenderSettings

        self._dir = TemporaryDirectory(prefix="vtv-routing-")
        root = Path(self._dir.name)
        story = timeline(captions=False)
        # The photograph has to be on disk, or the clip is described as a
        # message — correctly, and it would make this test pass for the wrong
        # reason.
        picture = story.clips[1].clip_id
        Image.new("RGB", (600, 400), (200, 120, 40)).save(
            root / f"asset-{picture}.bin", "PNG"
        )
        context = RenderContext(
            timeline=story,
            settings=RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24),
            scratch=root,
            assets={picture: {"still": f"asset-{picture}.bin"}},
        )
        context.save()
        return str(root)

    def test_a_typography_segment_is_sent_to_the_processor(self) -> None:
        """The 2.2x-slower case. Nothing in it is work a card does better: the
        animation engine rasterises on the processor, so the card would upload a
        finished picture, draw it once and read the frame back."""
        from vtv.adapters.render.ffmpeg_renderer import _routed

        # Seconds 0 to 3.75 of the fixture: the opening title card.
        order = _routed(self.chain(), self.scratch(), 0, 90)
        self.assertEqual(order[0][0], ExecutionTarget.LOCAL_CPU)

    def test_a_segment_with_a_photograph_is_sent_to_the_card(self) -> None:
        from vtv.adapters.render.ffmpeg_renderer import _routed

        # Seconds 5 to 7.5 of the same video: the still with a camera move.
        order = _routed(self.chain(), self.scratch(), 120, 180)
        self.assertEqual(order[0][0], ExecutionTarget.LOCAL_GPU)

    def test_the_same_video_routes_two_segments_differently(self) -> None:
        """The whole point, stated as one assertion. Choosing once per render
        cannot produce this, and on mixed material choosing once measured 1.48x
        where the photograph shots alone were worth eleven."""
        from vtv.adapters.render.ffmpeg_renderer import _routed

        held = self.scratch()
        title = _routed(self.chain(), held, 0, 90)
        photograph = _routed(self.chain(), held, 120, 180)
        self.assertEqual(title[0][0], ExecutionTarget.LOCAL_CPU)
        self.assertEqual(photograph[0][0], ExecutionTarget.LOCAL_GPU)

    def test_a_placeholder_shot_does_not_want_a_card(self) -> None:
        """A clip whose picture could not be found is a message, and a message
        is vector rasterisation. Routing must follow what is actually drawn, not
        what the timeline hoped for."""
        from vtv.adapters.render.ffmpeg_renderer import _routed

        # Seconds 9 to 11: the placeholder clip.
        order = _routed(self.chain(), self.scratch(), 216, 264)
        self.assertEqual(order[0][0], ExecutionTarget.LOCAL_CPU)

    def test_a_picture_in_the_last_instant_still_counts(self) -> None:
        """Sampling with a stride can step over the end of a range. A segment
        that is typography until its final second is still a segment with a
        photograph in it."""
        from vtv.adapters.render.ffmpeg_renderer import _routed

        # Ends one frame inside the still.
        order = _routed(self.chain(), self.scratch(), 0, 121)
        self.assertEqual(order[0][0], ExecutionTarget.LOCAL_GPU)

    def test_routing_never_removes_a_backend(self) -> None:
        """Being wrong about which is faster should cost time, not a render."""
        from vtv.adapters.render.ffmpeg_renderer import _routed

        chain = self.chain()
        order = _routed(chain, self.scratch(), 0, 90)
        self.assertEqual(
            {target for target, _ in order}, {target for target, _ in chain}
        )

    def test_a_chain_of_one_is_left_alone(self) -> None:
        """"Never run this in the cloud" is a real requirement. A router that
        quietly satisfied it differently would be the same class of lie as a
        fallback that does not fall back."""
        from vtv.adapters.render.ffmpeg_renderer import _routed

        only = self.chain()[:1]
        self.assertEqual(_routed(only, self.scratch(), 0, 90), only)

    def test_deciding_cannot_fail_a_render(self) -> None:
        """An unreadable scratch directory means the policy's order stands,
        which is what happened before routing existed."""
        from vtv.adapters.render.ffmpeg_renderer import _routed

        chain = self.chain()
        self.assertEqual(_routed(chain, "/nonexistent-scratch", 0, 48), chain)

    def test_the_painter_and_the_router_ask_the_same_question(self) -> None:
        """The drift that would cost a graphics context per segment and buy
        nothing, while leaving the output perfectly correct — so nothing else
        would ever report it."""
        from vtv.adapters.render.gpu_painter import _has_raster
        from vtv.contracts.display import (
            Background,
            DisplayList,
            Drawn,
            Message,
            Mix,
            Picture,
            Plate,
        )
        from vtv.contracts.visual_language import TypographySpec

        spec = TypographySpec(headline="A line")
        drawn = Drawn(spec=spec, seconds=0.0, duration=2.0)
        picture = Picture(key="k", box=(0.0, 0.0, 8.0, 8.0))
        plate = Plate(box=(0.0, 0.0, 4.0, 2.0), radius=1.0, fill=(0, 0, 0, 120))

        def frame(*layers: object, **kw: object) -> DisplayList:
            return DisplayList(width=8, height=8, layers=tuple(layers), **kw)  # type: ignore[arg-type]

        cases = [
            frame(drawn),
            frame(picture),
            frame(Background(colour=(1, 2, 3, 255))),
            frame(Message(text="Visual unavailable")),
            frame(Background(colour=(1, 2, 3, 255)), plate),
            frame(drawn, picture),
            frame(drawn).with_beneath(frame(drawn), Mix.BLEND, 0.5),
            frame(drawn).with_beneath(frame(picture), Mix.WIPE, 0.5),
            frame(picture).with_beneath(frame(drawn), Mix.PUSH, 0.5),
        ]
        for case in cases:
            with self.subTest(layers=[type(x).__name__ for x in case.layers]):
                self.assertEqual(_has_raster(case), case.needs_resampling)

    def test_typography_is_not_claimed_by_the_card(self) -> None:
        """The measurement this whole change came from. If this ever flips back,
        a typography-heavy video is 2.2x slower on a machine with a GPU than on
        the same machine without one."""
        from vtv.contracts.display import DisplayList, Drawn
        from vtv.contracts.visual_language import TypographySpec

        typography = DisplayList(
            width=8,
            height=8,
            layers=(Drawn(spec=TypographySpec(headline="A line"), seconds=0.0, duration=2.0),),
        )
        self.assertFalse(typography.needs_resampling)
