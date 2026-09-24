"""The comparison that will decide whether a GPU painter is trustworthy.

## Why the harness is tested before the thing it tests

Because a comparison that cannot fail proves nothing, and one that fails on
correct work is worse than none. Before this is pointed at a graphics card it
is pointed at two deliberately-wrong painters — one that shifts a plate by two
pixels, one that omits an overlay entirely — and it has to catch both. Then it
is pointed at two instances of the CPU painter, and it has to pass.

That ordering matters. "The GPU painter passed the equivalence check" is only
information if the check has been shown to discriminate.

## What first contact with real hardware found

A GTX 1650 ran the twenty-three scenes and eight failed. The *pattern* was the
diagnosis: every failing scene contained something vertically asymmetric — a
plate near the top, a line of text, a message card — and every passing one was
symmetric. That is what a mirrored framebuffer readback looks like from
outside, and the arithmetic confirmed it exactly (a plate at y 11..50 differing
across y 11..169: itself and its mirror).

Three more followed from the numbers: a black clear colour where the reference
starts from the theme background, a picture placed at its box rather than where
`paste_fitted` actually puts it, and overlays alpha-composited on the card where
the reference replaces pixels and flattens onto the theme background.

The classifier below exists so the *next* round is data rather than pattern
recognition, and it is tested the same way the harness is: by injecting each
defect it claims to name.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from vtv.animation.canvas import Canvas
from vtv.animation.equivalence import (
    MAX_OUTLIERS,
    TOLERANCE,
    check,
    classify,
    compare,
    measure,
    scenes,
    summarise,
)
from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.animation.painter import CpuPainter
from vtv.animation.theme import Theme
from vtv.contracts.display import DisplayList, Label, Plate
from vtv.contracts.style import StyleProfile

WIDTH, HEIGHT = 480, 270


def theme() -> Theme:
    return Theme.from_style(StyleProfile(), width=WIDTH, height=HEIGHT)


def still(_key: str) -> Image.Image:
    """A picture with structure in it.

    Flat colour would make a wrong transform invisible — a crop of grey is grey
    wherever it was taken from. Bands and a corner block mean a shifted or
    mirrored box changes the pixels.
    """
    image = Image.new("RGB", (600, 400), (40, 60, 110))
    draw = Canvas(theme()).draw.__class__(image)
    for index in range(0, 600, 40):
        draw.rectangle([index, 0, index + 20, 400], fill=(200, 120, 40))
    draw.rectangle([0, 0, 120, 90], fill=(240, 240, 250))
    return image


def cpu_painter() -> CpuPainter:
    return CpuPainter(
        theme=theme(),
        engine=AnimationEngine(StyleProfile()),
        size=RenderSize(WIDTH, HEIGHT),
        stills=still,
    )


@dataclass
class Sabotaged:
    """A painter that is wrong in one specific, plausible way.

    Both failures modelled here are real ones this project has shipped: a plate
    in slightly the wrong place, and an overlay that goes missing during a
    transition. If the harness cannot see them it cannot see anything.
    """

    inner: CpuPainter
    shift: float = 0.0
    drop_overlays: bool = False
    name: str = "sabotaged"

    def paint(self, frame: DisplayList) -> Image.Image:
        layers = tuple(self._move(layer) for layer in frame.layers)
        overlays = () if self.drop_overlays else tuple(
            self._move(layer) for layer in frame.overlays
        )
        return self.inner.paint(
            DisplayList(
                width=frame.width,
                height=frame.height,
                layers=layers,
                overlays=overlays,
                beneath=frame.beneath,
                transition=frame.transition,
                progress=frame.progress,
            )
        )

    def _move(self, layer: object) -> object:
        if not self.shift:
            return layer
        if isinstance(layer, Plate):
            x0, y0, x1, y1 = layer.box
            return Plate(
                box=(x0 + self.shift, y0, x1 + self.shift, y1),
                radius=layer.radius,
                fill=layer.fill,
                outline=layer.outline,
                outline_width=layer.outline_width,
            )
        if isinstance(layer, Label):
            x, y = layer.position
            return Label(
                text=layer.text,
                role=layer.role,
                position=(x + self.shift, y),
                colour=layer.colour,
                anchor=layer.anchor,
            )
        return layer

    def release(self) -> None:
        self.inner.release()


class TheHarnessCatchesRealMistakes(unittest.TestCase):
    """Tested before it is trusted. A comparison that cannot fail is decoration."""

    def test_a_two_pixel_shift_is_caught(self) -> None:
        """The failure a mean-difference check would launder: everything is
        almost right, on every frame, forever."""
        differences = check(
            cpu_painter(),
            Sabotaged(inner=cpu_painter(), shift=2.0),
            width=WIDTH,
            height=HEIGHT,
        )
        self.assertTrue(
            [d for d in differences if not d.ok],
            f"a two-pixel shift went unnoticed: {summarise(differences)}",
        )

    def test_a_dropped_overlay_is_caught(self) -> None:
        differences = check(
            cpu_painter(),
            Sabotaged(inner=cpu_painter(), drop_overlays=True),
            width=WIDTH,
            height=HEIGHT,
        )
        failed = {d.scene for d in differences if not d.ok}
        self.assertIn("overlay-over-picture", failed)
        self.assertIn("overlay-during-blend", failed)

    def test_a_painter_that_raises_is_a_failure_not_a_crash(self) -> None:
        """A port under development throws. The harness has to survive that and
        say which scene did it, or the first hour of the port is spent finding
        out which scene did it."""

        class Broken:
            name = "broken"

            def paint(self, frame: DisplayList) -> Image.Image:
                raise RuntimeError("shader compilation failed")

            def release(self) -> None:
                return

        differences = check(
            cpu_painter(), Broken(), width=WIDTH, height=HEIGHT
        )
        self.assertTrue(all(not d.ok for d in differences))
        self.assertIn("shader compilation failed", differences[0].scene)

    def test_different_sizes_are_a_failure_not_an_exception(self) -> None:
        one = Image.new("RGB", (10, 10))
        two = Image.new("RGB", (20, 20))
        self.assertFalse(compare(one, two, scene="mismatched").ok)


class TheDiagnosisNamesTheRightCause(unittest.TestCase):
    """The classifier is tested the same way the harness was: by injecting each
    defect it claims to recognise and checking it says so.

    Its whole value is turning "eight scenes differ" into "the framebuffer is
    read bottom-up" without a round trip. A classifier that guessed would cost
    more than no classifier, because it would send somebody to fix the wrong
    thing — and every one of these defects has actually occurred.
    """

    def frame(self) -> Image.Image:
        """Deliberately asymmetric, or a flip is invisible."""
        image = Image.new("RGB", (320, 180), (11, 13, 16))
        Canvas(theme()).draw.__class__(image).rectangle(
            [19, 11, 134, 50], fill=(90, 120, 200)
        )
        return image

    def test_a_mirrored_frame_is_named_as_one(self) -> None:
        """The defect a real GTX 1650 hit on first contact: OpenGL reads the
        framebuffer bottom-up and PIL expects top-down."""
        base = self.frame()
        found = classify(base, base.transpose(Image.Transpose.FLIP_TOP_BOTTOM))
        self.assertTrue(any("vertical flip" in item for item in found), found)

    def test_a_one_pixel_shift_is_named_as_one(self) -> None:
        from PIL import ImageChops

        base = self.frame()
        found = classify(base, ImageChops.offset(base, 1, 0))
        self.assertTrue(any("translation" in item for item in found), found)

    def test_a_wrong_clear_colour_is_named_as_one(self) -> None:
        """A constant difference everywhere is a background, not geometry."""
        from PIL import ImageChops

        base = self.frame()
        shifted = ImageChops.add(base, Image.new("RGB", (320, 180), (9, 9, 9)))
        found = classify(base, shifted)
        self.assertTrue(any("uniform offset" in item for item in found), found)

    def test_identical_frames_are_not_given_a_cause(self) -> None:
        """It must not invent one. A classifier that always has an answer is a
        classifier nobody can act on."""
        base = self.frame()
        self.assertEqual(classify(base, base), ["unclassified: see the diff image"])

    def test_the_measurements_are_the_ones_a_report_needs(self) -> None:
        base = self.frame()
        keys = set(measure(base, base))
        self.assertEqual(
            keys,
            {
                "dimensions",
                "max_per_channel",
                "pixels_outside_tolerance",
                "mean_absolute_difference",
                "bounding_box",
            },
        )


class TheHarnessPassesCorrectWork(unittest.TestCase):
    """The other half. A check that rejects a correct implementation for being
    a different correct implementation is a check nobody will keep."""

    def test_the_reference_painter_matches_itself(self) -> None:
        differences = check(
            cpu_painter(), cpu_painter(), width=WIDTH, height=HEIGHT
        )
        self.assertTrue(
            all(d.ok for d in differences), summarise(differences)
        )
        self.assertEqual(max(d.worst for d in differences), 0)

    def test_every_layer_type_and_transition_is_covered(self) -> None:
        """The suite is the claim. A comparison over one plain frame would pass
        a painter that handles nothing else."""
        names = set(scenes(WIDTH, HEIGHT))
        for required in (
            "background", "message", "picture", "picture-zoomed",
            "picture-panned", "plate-filled", "plate-outlined", "text",
            "vignette", "overlay-over-picture", "overlay-during-blend",
        ):
            self.assertIn(required, names)
        for kind in ("blend", "wipe", "push"):
            for point in (0, 50, 100):
                self.assertIn(f"{kind}-{point}", names)

    def test_the_tolerance_is_tight_and_admits_no_outliers(self) -> None:
        """Documented as a test because both halves matter and the second is
        the one that stops a subtle error passing."""
        self.assertLessEqual(TOLERANCE, 2)
        self.assertEqual(MAX_OUTLIERS, 0)


class TheGpuIsNotClaimedWithoutEvidence(unittest.TestCase):
    """Hardware being present is not a capability, and neither is code existing."""

    def setUp(self) -> None:
        from vtv.adapters.render import gpu_probe

        gpu_probe.forget()

    def test_this_environment_reports_no_gpu_and_says_why(self) -> None:
        from vtv.adapters.render.gpu_probe import probe

        found = probe()
        if found.available:
            self.skipTest("this machine has a usable GPU; see the equivalence run")
        self.assertFalse(found.available)
        self.assertTrue(found.reason, "unavailable with no reason is unactionable")

    def test_the_probe_never_raises(self) -> None:
        """It runs before every render that might use a card. A probe that
        throws takes down the render it exists to protect."""
        from vtv.adapters.render.gpu_probe import forget, probe

        forget()
        probe()
        forget()
        probe()

    def test_disabling_it_is_honoured_before_anything_is_loaded(self) -> None:
        import os

        from vtv.adapters.render.gpu_probe import DISABLE, forget, probe

        forget()
        os.environ[DISABLE] = "1"
        try:
            found = probe()
            self.assertFalse(found.available)
            self.assertIn(DISABLE, found.reason)
        finally:
            del os.environ[DISABLE]
            forget()

    def test_no_gpu_backend_is_registered_without_a_working_device(self) -> None:
        """The Phase A rule, applied to the thing Phase B2 adds: a target with
        no working backend is never offered."""
        from vtv.config import Settings
        from vtv.contracts.execution import ExecutionTarget
        from vtv.wiring import execution_registry

        registry = execution_registry(Settings(asset_search_endpoint=""))
        for target in registry.ready():
            if target.processor.value == "gpu":
                from vtv.adapters.render.gpu_probe import probe

                self.assertTrue(
                    probe().available,
                    "a GPU target was registered without a proven device",
                )
        del ExecutionTarget


@unittest.skipUnless(
    __import__(
        "vtv.adapters.render.gpu_probe", fromlist=["probe"]
    ).probe().available,
    "no usable GPU on this machine",
)
class TheGpuPainterMatchesTheReference(unittest.TestCase):
    """The acceptance test for Phase B2, run wherever there is a card.

    Skipped in the container this was written in, which has no graphics device
    and no way to obtain a graphics library. That is not a soft pass: until
    this runs somewhere with a GPU, the GPU painter is implemented and
    unverified, and the product does not offer it.
    """

    def test_every_scene_matches_within_tolerance(self) -> None:
        from vtv.adapters.render.gpu_painter import GpuPainter

        with TemporaryDirectory():
            gpu = GpuPainter(
                theme=theme(),
                engine=AnimationEngine(StyleProfile()),
                size=RenderSize(WIDTH, HEIGHT),
                stills=still,
            )
            try:
                differences = check(
                    cpu_painter(), gpu, width=WIDTH, height=HEIGHT
                )
            finally:
                gpu.release()
        self.assertTrue(all(d.ok for d in differences), summarise(differences))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheResamplingKernelMatchesTheReference(unittest.TestCase):
    """The shader's arithmetic, checked without a graphics card.

    ## Why this test exists

    The second hardware run came back with every vector scene at worst 0 and
    every scene containing a photograph at worst 50 to 98 — the signature of two
    different resamplers, confirmed by measuring PIL's own LANCZOS against its
    own BILINEAR on the same downscale (worst 40, mean 8.1, the same order).

    A card's built-in filter samples the four texels nearest each destination
    pixel however far apart the source pixels are, so a reduction aliases: fine
    detail crawls as a Ken Burns move drifts across it. That is a defect in its
    own right, not merely a mismatch, which is why the answer was a Lanczos
    shader rather than a looser tolerance.

    ## What is verified here and what is not

    The **algorithm** — the kernel, the mapping from destination pixel to source
    coordinate, the widening of the support when reducing, the edge clamp and
    the renormalisation. Those are the parts that are easy to get wrong and they
    are checked against PIL to a difference of one.

    What is *not* verified is that the GLSL compiles and runs. Nothing in this
    container can do that. But the arithmetic below is the same arithmetic the
    shader performs, transcribed, so a hardware failure now points at the
    plumbing rather than at the mathematics.
    """

    def lanczos3(self, x):  # type: ignore[no-untyped-def]
        import numpy

        x = numpy.abs(x)
        out = numpy.zeros_like(x)
        inside = (x > 1e-8) & (x < 3.0)
        scaled = numpy.pi * x[inside]
        out[inside] = (
            3.0 * numpy.sin(scaled) * numpy.sin(scaled / 3.0) / (scaled * scaled)
        )
        out[x <= 1e-8] = 1.0
        return out

    def shader_resize(  # type: ignore[no-untyped-def]
        self, source: Image.Image, width: int, height: int, *, clip_between: bool = True
    ):
        """A transcription of `_RESAMPLE_FRAGMENT` and its two passes, in numpy.

        The intermediate is rounded and clipped to eight bits between the
        passes, because that is what PIL's `clip8` does and what the shader's
        RGBA8 staging buffer does. It is not an approximation of the real thing
        — it is the behaviour under test, and a model that kept float precision
        here would pass while the shader failed.
        """
        import numpy

        pixels = numpy.asarray(source.convert("RGB"), dtype=numpy.float64)
        source_h, source_w = pixels.shape[:2]
        filter_x = max(1.0, source_w / width)
        filter_y = max(1.0, source_h / height)

        def axis(count: int, source_count: int, filter_scale: float):  # type: ignore[no-untyped-def]
            support = 3.0 * filter_scale
            centres = (numpy.arange(count) + 0.5) * (source_count / count)
            low = numpy.floor(centres - support + 0.5)
            taps = int(numpy.ceil(2 * support)) + 2
            index = low[:, None] + numpy.arange(taps)[None, :]
            weight = self.lanczos3((index - centres[:, None] + 0.5) / filter_scale)
            # PIL truncates the kernel at the edge and renormalises what is
            # left; the shader's running total does the same.
            weight[(index < 0) | (index > source_count - 1)] = 0.0
            index = numpy.clip(index, 0, source_count - 1).astype(int)
            total = weight.sum(axis=1, keepdims=True)
            return index, numpy.divide(
                weight, total, out=numpy.zeros_like(weight), where=total != 0
            )

        xi, xw = axis(width, source_w, filter_x)
        yi, yw = axis(height, source_h, filter_y)
        rows = numpy.stack(
            [(pixels[:, xi[i], :] * xw[i][None, :, None]).sum(axis=1) for i in range(width)],
            axis=1,
        )
        # The 8-bit intermediate. Lanczos overshoots at a sharp edge and this
        # throws the overshoot away before the vertical pass can see it; keeping
        # float here is the bug that survived a whole hardware round.
        if clip_between:
            rows = numpy.clip(numpy.round(rows), 0, 255)
        out = numpy.stack(
            [(rows[yi[j], :, :] * yw[j][:, None, None]).sum(axis=0) for j in range(height)],
            axis=0,
        )
        return Image.fromarray(
            numpy.clip(numpy.round(out), 0, 255).astype(numpy.uint8)
        )

    def detailed(self) -> Image.Image:
        """Fine detail *against the ends of the range*, which is the hard part.

        The first version of this still used stripes of 210 over 40 and it let a
        real bug through a whole round on hardware. Lanczos overshoots either
        side of a sharp edge; at 210 over 40 the overshoot stays inside 0..255,
        nothing is ever clipped, and a resampler that never clips agrees with
        one that does. The card then failed by 19 to 23 on a still that had
        white lines in it.

        So: pure white on near-black, and a saturated block. A test still is a
        test double, and a test double more forgiving than the real thing is how
        this project has lost time before.
        """
        image = Image.new("RGB", (400, 260), (8, 10, 14))
        draw = Canvas(theme()).draw.__class__(image)
        for x in range(0, 400, 7):
            draw.line([(x, 0), (x, 260)], fill=(255, 255, 255), width=2)
        for y in range(0, 260, 23):
            draw.line([(0, y), (400, y)], fill=(0, 0, 0), width=1)
        draw.rectangle([40, 30, 150, 120], fill=(255, 255, 255))
        draw.ellipse([60, 40, 220, 170], outline=(0, 0, 0), width=3)
        return image

    def test_it_matches_pil_on_reduction_and_enlargement(self) -> None:
        source = self.detailed()
        for target in ((320, 208), (480, 320), (160, 104), (400, 260)):
            with self.subTest(target=target):
                reference = source.resize(target, Image.Resampling.LANCZOS)
                shader = self.shader_resize(source, *target)
                verdict = compare(reference, shader, scene=str(target))
                self.assertTrue(verdict.ok, str(verdict))

    def test_the_cards_default_filter_would_not_have_passed(self) -> None:
        """The measurement that made a Lanczos shader the answer rather than a
        looser number. If bilinear were close enough, none of this would be
        worth its complexity."""
        source = self.detailed()
        reference = source.resize((320, 208), Image.Resampling.LANCZOS)
        bilinear = source.resize((320, 208), Image.Resampling.BILINEAR)
        verdict = compare(reference, bilinear, scene="bilinear")
        self.assertFalse(verdict.ok)
        self.assertGreater(verdict.worst, 10)

    def test_one_pass_in_float_would_not_have_passed(self) -> None:
        """Why the resample is two passes through an 8-bit buffer.

        The first shader did the whole separable kernel in one 2D accumulation
        in float, which is the obvious way to write it and is wrong. PIL resizes
        horizontally into an 8-bit image and then vertically out of it, and
        Lanczos overshoots at a sharp edge — so PIL clips the overshoot before
        the second pass and a single float pass does not.

        It survived a full hardware round because the still it was verified
        against had no saturated pixels to ring past. This test is the same
        comparison with the intermediate left in float, and it must fail: if it
        ever passes, either the still has gone soft again or PIL has changed,
        and both are things to find out about here rather than on a customer's
        video.
        """
        import numpy

        source = self.detailed()
        target = (320, 208)
        reference = source.resize(target, Image.Resampling.LANCZOS)

        two_pass = numpy.asarray(self.shader_resize(source, *target), dtype=int)
        one_pass = numpy.asarray(
            self.shader_resize(source, *target, clip_between=False), dtype=int
        )
        self.assertGreater(
            numpy.abs(two_pass - one_pass).max(),
            TOLERANCE,
            "the still no longer has content that rings out of range, so it can "
            "no longer tell a clipped intermediate from a float one",
        )

        verdict = compare(
            reference,
            Image.fromarray(one_pass.astype(numpy.uint8)),
            scene="float intermediate",
        )
        self.assertFalse(verdict.ok, str(verdict))

    def test_a_resampling_difference_is_named_as_one(self) -> None:
        """The classifier said "unclassified" for fifteen scenes on the second
        hardware run, because erosion finds differences that live on edges and
        this one lives everywhere, in fine detail only."""
        source = self.detailed()
        found = classify(
            source.resize((320, 208), Image.Resampling.LANCZOS),
            source.resize((320, 208), Image.Resampling.BILINEAR),
        )
        self.assertTrue(
            any("resampling kernel" in item for item in found), found
        )
