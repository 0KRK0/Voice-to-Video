"""Opacity, and the two fast paths that depend on getting it right.

## The bug these were written to catch, and did

`vignette` and `to_rgb` each have a cheap path that is only valid on a frame
with no transparency. The first version of this decided that with a flag set at
construction, reasoning that nothing in `Canvas` can make an opaque frame
transparent.

That reasoning was wrong, and `AfterDrawing` below failed on the first run.
`ImageDraw` in `"RGBA"` mode does **not** blend a fill's alpha into the frame —
it writes it through. A caption box drawn at 55% black leaves those pixels at
alpha 140, and the fast flatten would have rendered the box as **solid black**.
Nobody reviewing a diff spots that; everybody watching the video does.

So the flag became a measurement. It costs 1.3ms a frame and saves 19ms, which
is not a compromise — it is most of the speed and all of the correctness.

Every test here checks a picture, never a duration. An optimisation that
changes what is on screen is not an optimisation.
"""

from __future__ import annotations

import unittest

from PIL import Image, ImageChops

from vtv.animation.canvas import Canvas
from vtv.animation.theme import Theme
from vtv.contracts.style import StyleProfile


def theme(width: int = 480, height: int = 270) -> Theme:
    return Theme.from_style(StyleProfile(), width=width, height=height)


def alpha_extrema(canvas: Canvas) -> tuple[int, int]:
    return canvas.image.getchannel("A").getextrema()


def really_opaque(canvas: Canvas) -> bool:
    return alpha_extrema(canvas) == (255, 255)


def photograph(size: tuple[int, int] = (200, 120)) -> Image.Image:
    """Something with its own alpha, to paste."""
    image = Image.new("RGBA", size, (30, 120, 200, 128))
    image.paste((250, 40, 40, 255), (10, 10, 80, 60))
    return image


class TheFastPathIsChosenOnlyWhenItIsValid(unittest.TestCase):
    """Whatever `is_opaque` says, flattening must produce the same picture.

    Subclasses each perform one of the operations `Canvas` offers and then
    check that both flatten paths agree. Whether the fast one was taken is not
    the assertion — the assertion is that taking it did not change anything,
    which is the only property that matters and the one that survives someone
    later changing how the decision is made.
    """

    def operate(self, canvas: Canvas) -> None:
        raise NotImplementedError

    def check(self, *, transparent: bool) -> None:
        canvas = Canvas(theme(), transparent=transparent)
        self.operate(canvas)
        fast = canvas.to_rgb()
        # The general path, forced: composite through the alpha channel onto
        # the theme background, which is what the fast path is standing in for.
        general = Image.new("RGB", canvas.image.size, canvas.theme.background[:3])
        general.paste(canvas.image, mask=canvas.image.split()[3])
        diff = ImageChops.difference(fast, general)
        self.assertEqual(
            max(band[1] for band in diff.getextrema()),
            0,
            f"is_opaque={canvas.is_opaque}, alpha extrema {alpha_extrema(canvas)}",
        )

    def test_on_an_opaque_canvas(self) -> None:
        if type(self) is not TheFastPathIsChosenOnlyWhenItIsValid:
            self.check(transparent=False)

    def test_on_a_transparent_canvas(self) -> None:
        if type(self) is not TheFastPathIsChosenOnlyWhenItIsValid:
            self.check(transparent=True)


class AfterDoingNothing(TheFastPathIsChosenOnlyWhenItIsValid):
    def operate(self, canvas: Canvas) -> None:
        return


class AfterDrawing(TheFastPathIsChosenOnlyWhenItIsValid):
    def operate(self, canvas: Canvas) -> None:
        canvas.draw.rectangle([10, 10, 200, 150], fill=(200, 80, 40, 255))
        # A semi-transparent fill: the case that disproved the flag. PIL
        # writes this alpha straight into the frame rather than blending it.
        canvas.draw.ellipse([50, 40, 160, 120], fill=(20, 200, 120, 180))


class AfterAVignette(TheFastPathIsChosenOnlyWhenItIsValid):
    def operate(self, canvas: Canvas) -> None:
        canvas.draw.rectangle([10, 10, 200, 150], fill=(200, 80, 40, 255))
        canvas.vignette(0.35)


class AfterAFade(TheFastPathIsChosenOnlyWhenItIsValid):
    def operate(self, canvas: Canvas) -> None:
        canvas.fade(0.4)


class AfterPastingAPictureThatHasItsOwnAlpha(TheFastPathIsChosenOnlyWhenItIsValid):
    """A source with its own alpha, composited onto the frame."""

    def operate(self, canvas: Canvas) -> None:
        canvas.paste_fitted(photograph(), (0, 0, 300, 200), cover=False)


class AfterEverythingInSequence(TheFastPathIsChosenOnlyWhenItIsValid):
    def operate(self, canvas: Canvas) -> None:
        canvas.draw.rectangle([5, 5, 120, 90], fill=(90, 90, 200, 255))
        canvas.paste_fitted(photograph(), (20, 20, 240, 160), cover=True)
        canvas.fade(0.15)
        canvas.vignette(0.28)


class TheFastPathsDrawTheSamePicture(unittest.TestCase):
    """Both optimisations are equivalences, and both are checked as such."""

    def drawn(self, *, opaque: bool) -> Canvas:
        canvas = Canvas(theme())
        canvas.draw.rectangle([40, 30, 300, 200], fill=(200, 80, 40, 255))
        canvas.draw.ellipse([120, 60, 260, 170], fill=(30, 180, 220, 255))
        if not opaque:
            # One almost-invisible pixel, to take the frame off the fast path
            # without changing what anybody can see. This is how the two paths
            # are compared on one picture.
            canvas.image.putpixel((0, 0), (*canvas.theme.background[:3], 254))
        return canvas

    def difference(self, one: Image.Image, two: Image.Image) -> int:
        diff = ImageChops.difference(one.convert("RGB"), two.convert("RGB"))
        return max(band[1] for band in diff.getextrema())

    def test_the_one_pass_vignette_matches_the_three_pass_one(self) -> None:
        """`composite(frame, blend(frame, black, s), mask)` is algebraically
        `frame · (1 - s·(1-mask))`, which is what compositing black at alpha
        `s·(1-mask)` produces. Algebra is an argument; this is the check."""
        for strength in (0.1, 0.22, 0.35, 0.6, 1.0):
            with self.subTest(strength=strength):
                fast = self.drawn(opaque=True)
                fast.vignette(strength)
                slow = self.drawn(opaque=False)
                slow.vignette(strength)
                self.assertEqual(self.difference(fast.image, slow.image), 0)

    def test_flattening_an_opaque_frame_matches_the_masked_paste(self) -> None:
        fast = self.drawn(opaque=True)
        slow = self.drawn(opaque=False)
        self.assertEqual(self.difference(fast.to_rgb(), slow.to_rgb()), 0)

    def test_a_transparent_frame_still_lands_on_the_background(self) -> None:
        """The case the fast path must not be allowed to take. Dropping the
        alpha channel here would show whatever colour sat under a transparent
        pixel instead of the background the design assumes."""
        canvas = Canvas(theme(), transparent=True)
        canvas.draw.rectangle([10, 10, 100, 80], fill=(255, 0, 0, 255))
        flat = canvas.to_rgb()
        self.assertEqual(flat.getpixel((5, 5)), canvas.theme.background[:3])
        self.assertEqual(flat.getpixel((50, 40)), (255, 0, 0))

    def test_a_semi_transparent_draw_takes_the_frame_off_the_fast_path(self) -> None:
        """The regression itself, stated as what it is.

        A caption plate is drawn at 55% black. PIL writes alpha 140 into those
        pixels, so the frame stops being opaque and `to_rgb` must composite
        rather than drop the channel. The fast path would emit solid black.

        Worth recording what the composite actually blends with, because it is
        not what a reader would assume: `to_rgb` pastes onto the **theme
        background**, not onto the picture that was underneath, because the
        draw overwrote those pixels. A caption plate is therefore 55% black
        over the theme colour rather than over the photograph behind it. That
        is long-standing behaviour and it looks correct — the plate is meant to
        be dark — but it is a fact about the renderer rather than an accident
        of this test, so it is asserted here rather than discovered later.
        """
        canvas = Canvas(theme())
        canvas.draw.rectangle([0, 0, 100, 100], fill=(255, 255, 255, 255))
        canvas.draw.rectangle([10, 10, 60, 60], fill=(0, 0, 0, 128))
        self.assertFalse(canvas.is_opaque)
        plate = canvas.to_rgb().getpixel((30, 30))
        expected = tuple(
            round(channel * (1 - 128 / 255)) for channel in canvas.theme.background[:3]
        )
        self.assertEqual(plate, expected)
        # And the white beneath, where nothing was drawn over it, is untouched.
        self.assertEqual(canvas.to_rgb().getpixel((80, 80)), (255, 255, 255))

    def test_a_vignette_of_zero_strength_changes_nothing(self) -> None:
        canvas = self.drawn(opaque=True)
        before = canvas.image.copy()
        canvas.vignette(0.0)
        self.assertEqual(self.difference(before, canvas.image), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
