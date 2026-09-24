"""Drawing helpers shared by every visual primitive.

PIL rather than matplotlib, deliberately. Charts here are bars, lines and labels
drawn on a canvas we control completely — which means every primitive shares one
type scale, one palette and one set of motion curves, and a chart sits next to a
timeline without looking like it came from a different application. It is also
several times faster per frame, and frame rate matters when a five-minute video
is thirty scenes deep.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from vtv.animation.theme import RGBA, Theme, with_alpha
from vtv.contracts.language import detect_script


@lru_cache(maxsize=8)
def _black(size: tuple[int, int]) -> Image.Image:
    """A solid opaque black plane, one per frame size.

    Allocating and filling eight megabytes to blend against, on every frame,
    cost more than the blend itself. Read-only by every caller.
    """
    return Image.new("RGBA", size, (0, 0, 0, 255))


@lru_cache(maxsize=8)
def _vignette_mask(
    width: int, height: int, size: tuple[int, int]
) -> Image.Image:
    """The vignette's falloff mask, computed once per frame size.

    ## Why this is cached and the rest of the drawing is not

    It was the single slowest thing in the renderer, by a wide margin, and it
    was recomputed for every frame of every video. The mask is a filled ellipse
    blurred by `width * 0.08` — a **154-pixel Gaussian** at 1920 wide — and at
    thirty frames a second that is thirty of them for every second of finished
    video. A three-minute render did it five and a half thousand times.

    It depends on nothing but the frame size. `strength` is applied afterwards,
    to the overlay, so two clips at different strengths still share one mask.
    The frames are byte-for-byte what they were; only the arithmetic is skipped.

    `maxsize=8` because a process renders one or two frame sizes and the cached
    value is a single 8-bit plane — about two megabytes at 1080p. Unbounded
    would be a slow leak in a long-lived worker; too small would defeat the
    point for a worker alternating between landscape and portrait jobs.
    """
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse(
        [-width * 0.25, -height * 0.35, width * 1.25, height * 1.35],
        fill=255,
    )
    return mask.filter(ImageFilter.GaussianBlur(width * 0.08))


@lru_cache(maxsize=8)
def _vignette_overlay(
    width: int, height: int, size: tuple[int, int], strength: float
) -> Image.Image:
    """Black, transparent in the middle, at the vignette's own falloff.

    The whole effect as one image, so applying it is a single composite. See
    `Canvas.vignette` for why this is equivalent to the three-pass form.

    `strength` is part of the key and is rounded by the caller, because a
    strength that varies in the sixth decimal place across a render would make
    every frame a cache miss and every miss a 154-pixel Gaussian.
    """
    mask = _vignette_mask(width, height, size)
    overlay = Image.new("RGBA", size, (0, 0, 0, 255))
    # `mask` is 255 in the middle, where nothing should darken. The alpha we
    # want is the inverse of it, scaled by the strength.
    overlay.putalpha(mask.point(lambda level: int((255 - level) * strength)))
    return overlay


@dataclass
class TextBlock:
    lines: list[str]
    width: int
    height: int
    line_height: int


class Canvas:
    """An RGBA frame plus the drawing operations the primitives need."""

    def __init__(self, theme: Theme, *, transparent: bool = False) -> None:
        self.theme = theme
        self.image = Image.new(
            "RGBA",
            (theme.width, theme.height),
            (0, 0, 0, 0) if transparent else theme.background,
        )
        self.draw = ImageDraw.Draw(self.image, "RGBA")
        #: What the canvas was *constructed* as. Not the same question as
        #: whether it is opaque now — see `is_opaque`.
        self.transparent = transparent

    @property
    def is_opaque(self) -> bool:
        """Whether every pixel is fully opaque, measured rather than assumed.

        ## Why this is measured every time

        It was a flag, set once at construction, on the argument that nothing
        in this class can make an opaque canvas transparent. The argument was
        wrong, and `test_canvas_opacity` caught it before it shipped:
        `ImageDraw` in `"RGBA"` mode does not blend the fill's alpha into the
        frame, it **writes it through**. Drawing a caption box at 55% black
        leaves those pixels at alpha 140, so the frame is no longer opaque and
        the fast flatten would render the box as solid black.

        Measuring the alpha channel costs about 1.3ms at 1080p. The passes it
        lets `vignette` and `to_rgb` skip cost about 19ms. Paying the 1.3ms is
        not a compromise between speed and correctness; it is most of the speed
        and all of the correctness.
        """
        return self.image.getchannel("A").getextrema() == (255, 255)

    # -- text -------------------------------------------------------------

    def measure(self, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
        left, top, right, bottom = self.draw.textbbox((0, 0), text, font=font)
        return int(right - left), int(bottom - top)

    def font(self, role: str, text: str | None = None) -> ImageFont.FreeTypeFont:
        """The themed face for a role, chosen for the script of ``text``."""
        return self.theme.font(role, text)

    def wrap(
        self, text: str, font: ImageFont.FreeTypeFont, max_width: int
    ) -> TextBlock:
        """Greedy word wrap, measured against the actual font metrics.

        Character-count wrapping is the classic bug here: it looks fine in
        testing and then breaks on the first headline full of wide letters.
        """
        script = detect_script(text)
        if script.wraps_on_characters:
            # Chinese, Japanese and Thai do not separate words with spaces.
            # Wrapping on whitespace produces one unbroken line that overflows
            # the frame, so these scripts wrap between characters instead.
            lines = []
            current = ""
            for character in text:
                candidate = current + character
                if self.measure(candidate, font)[0] <= max_width or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = character
            if current:
                lines.append(current)
        else:
            words = text.split()
            lines = []
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if self.measure(candidate, font)[0] <= max_width or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
        if not lines:
            lines = [""]
        ascent, descent = font.getmetrics()
        line_height = int((ascent + descent) * 1.18)
        widest = max(self.measure(line, font)[0] for line in lines)
        return TextBlock(
            lines=lines,
            width=widest,
            height=line_height * len(lines),
            line_height=line_height,
        )

    def text(
        self,
        position: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        colour: RGBA,
        *,
        anchor: str = "la",
    ) -> None:
        self.draw.text(position, text, font=font, fill=colour, anchor=anchor)

    def text_block(
        self,
        block: TextBlock,
        font: ImageFont.FreeTypeFont,
        origin: tuple[float, float],
        colour: RGBA,
        *,
        align: str = "left",
        highlight: dict[str, RGBA] | None = None,
        visible_words: int | None = None,
    ) -> None:
        """Draw wrapped text, optionally revealing word by word.

        Word-by-word reveal is done by drawing whole words rather than clipping,
        so a partially-revealed line never shows half a glyph.
        """
        x, y = origin
        drawn = 0
        # Right-to-left text is right-aligned within its block unless the
        # caller asked otherwise; left-aligning Arabic is as wrong as
        # right-aligning English.
        if align == "left" and self.theme.is_rtl:
            align = "right"
        for line in block.lines:
            if align == "center":
                line_x = x + (block.width - self.measure(line, font)[0]) // 2
            elif align == "right":
                line_x = x + block.width - self.measure(line, font)[0]
            else:
                line_x = x

            if highlight or visible_words is not None:
                cursor = line_x
                space = self.measure(" ", font)[0]
                for word in line.split():
                    if visible_words is not None and drawn >= visible_words:
                        return
                    word_colour = colour
                    if highlight:
                        stripped = word.strip(".,:;!?\"'").lower()
                        for needle, accent in highlight.items():
                            if needle and needle in stripped:
                                word_colour = accent
                                break
                    self.draw.text((cursor, y), word, font=font, fill=word_colour)
                    cursor += self.measure(word, font)[0] + space
                    drawn += 1
            else:
                self.draw.text((line_x, y), line, font=font, fill=colour)
            y += block.line_height

    # -- shapes -----------------------------------------------------------

    def rounded_rect(
        self,
        box: tuple[float, float, float, float],
        radius: float,
        *,
        fill: RGBA | None = None,
        outline: RGBA | None = None,
        width: int = 2,
    ) -> None:
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            return
        radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
        self.draw.rounded_rectangle(
            [x0, y0, x1, y1], radius=radius, fill=fill, outline=outline, width=width
        )

    def circle(
        self,
        centre: tuple[float, float],
        radius: float,
        *,
        fill: RGBA | None = None,
        outline: RGBA | None = None,
        width: int = 2,
    ) -> None:
        cx, cy = centre
        self.draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            fill=fill,
            outline=outline,
            width=width,
        )

    def line(
        self,
        points: list[tuple[float, float]],
        colour: RGBA,
        width: int = 3,
        *,
        joint: str | None = "curve",
    ) -> None:
        if len(points) < 2:
            return
        self.draw.line(points, fill=colour, width=width, joint=joint)

    def arrow(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        colour: RGBA,
        width: int = 3,
        head: float = 14.0,
    ) -> None:
        self.draw.line([start, end], fill=colour, width=width)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        for offset in (2.6, -2.6):
            self.draw.line(
                [
                    end,
                    (
                        end[0] + head * math.cos(angle + offset),
                        end[1] + head * math.sin(angle + offset),
                    ),
                ],
                fill=colour,
                width=width,
            )

    def dashed_line(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        colour: RGBA,
        width: int = 2,
        dash: float = 12.0,
    ) -> None:
        length = math.dist(start, end)
        if length <= 0:
            return
        steps = max(1, int(length / (dash * 2)))
        for step in range(steps):
            t0 = (step * 2 * dash) / length
            t1 = min(1.0, ((step * 2 + 1) * dash) / length)
            self.draw.line(
                [
                    (start[0] + (end[0] - start[0]) * t0, start[1] + (end[1] - start[1]) * t0),
                    (start[0] + (end[0] - start[0]) * t1, start[1] + (end[1] - start[1]) * t1),
                ],
                fill=colour,
                width=width,
            )

    # -- effects ----------------------------------------------------------

    def vignette(self, strength: float = 0.35) -> None:
        """A soft darkening at the edges. Holds the eye in the middle of the
        frame, which matters when the subject is type rather than a photograph."""
        if strength <= 0:
            return
        if self.is_opaque:
            # One pass instead of three, and pixel-for-pixel the same picture.
            #
            # ## The arithmetic
            #
            # The general form below is `composite(frame, darkened, mask)`,
            # where `darkened = blend(frame, black, s) = (1-s)·frame`. So
            #
            #     out = mask·frame + (1-mask)·(1-s)·frame
            #         = frame · (1 - s·(1-mask))
            #
            # which is exactly what compositing solid black at alpha
            # `s·(1-mask)` over the frame produces. The three full-frame passes
            # — a blend, a copy and a masked paste, 19ms of a 27ms frame at
            # 1080p — collapse into one `alpha_composite`, and the overlay
            # depends only on the size and the strength, so it is cached and
            # most frames pay nothing at all for it.
            #
            # Restricted to opaque frames because `alpha_composite` onto a
            # transparent one would darken towards black where the design
            # intends to show through. That case keeps the general path.
            self.image.alpha_composite(
                _vignette_overlay(
                    self.theme.width, self.theme.height, self.image.size,
                    round(strength, 3),
                )
            )
            self.draw = ImageDraw.Draw(self.image, "RGBA")
            return
        mask = _vignette_mask(self.theme.width, self.theme.height, self.image.size)
        self.image = Image.composite(self.image, self._darkened(strength), mask)
        self.draw = ImageDraw.Draw(self.image, "RGBA")

    def _darkened(self, strength: float) -> Image.Image:
        """The frame with a flat black wash over it, for the vignette to mask.

        Two ways to compute the same picture, and which is correct depends on
        the frame:

        * `Image.blend(frame, black, strength)` is a straight lerp. On a frame
          with no transparency it is *pixel-for-pixel identical* to compositing
          solid black at that alpha, and it is about three times faster —
          19 milliseconds a frame at 1080p, which over a 76-second video at
          30fps is forty-three seconds of render time.
        * `Image.alpha_composite` is the general answer, and the only correct
          one where the frame has transparency: a lerp would drag the alpha
          channel towards opaque as well, visibly filling in whatever the
          transparent canvas was meant to show through.

        Which one applies is answered by `is_opaque`, which measures. It is
        only reached on the transparent path now — an opaque frame takes the
        single-pass overlay in `vignette` and never gets here.
        """
        if self.is_opaque:
            return Image.blend(self.image, _black(self.image.size), strength)
        overlay = Image.new(
            "RGBA", self.image.size, with_alpha((0, 0, 0, 255), strength)
        )
        return Image.alpha_composite(self.image, overlay)

    def fade(self, amount: float) -> None:
        """Fade the whole frame towards the background colour."""
        if amount <= 0:
            return
        overlay = Image.new(
            "RGBA", self.image.size, with_alpha(self.theme.background, min(1.0, amount))
        )
        self.image = Image.alpha_composite(self.image, overlay)
        self.draw = ImageDraw.Draw(self.image, "RGBA")

    def paste_fitted(
        self, source: Image.Image, box: tuple[float, float, float, float], *, cover: bool = True
    ) -> None:
        """Place an image into a box, preserving aspect ratio.

        ``cover`` crops to fill; otherwise the image is contained and centred.
        Stretching is never an option — a squashed photograph is the fastest way
        to make automatically generated video look automatically generated.
        """
        x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
        target_w, target_h = max(1, x1 - x0), max(1, y1 - y0)
        source = source.convert("RGBA")
        scale = (
            max(target_w / source.width, target_h / source.height)
            if cover
            else min(target_w / source.width, target_h / source.height)
        )
        resized = source.resize(
            (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
            Image.Resampling.LANCZOS,
        )
        offset_x = x0 + (target_w - resized.width) // 2
        offset_y = y0 + (target_h - resized.height) // 2
        self.image.alpha_composite(resized, (max(x0, offset_x), max(y0, offset_y)))
        self.draw = ImageDraw.Draw(self.image, "RGBA")

    def to_rgb(self) -> Image.Image:
        """Flatten to RGB — the only form an encoder will take.

        The opaque path is a channel drop. The transparent one has to composite
        onto the theme background through the frame's own alpha, because
        dropping the channel there would reveal whatever colour happened to sit
        under a transparent pixel rather than the background the design
        assumes.
        """
        if self.is_opaque:
            return self.image.convert("RGB")
        flat = Image.new("RGB", self.image.size, self.theme.background[:3])
        flat.paste(self.image, mask=self.image.split()[3])
        return flat


def nice_ticks(minimum: float, maximum: float, count: int = 4) -> list[float]:
    """Human-readable axis ticks.

    Axes labelled 0, 2.5, 5, 7.5, 10 read instantly; axes labelled
    0, 2.34, 4.68 do not, and a chart nobody can read is worse than no chart.
    """
    if maximum <= minimum:
        return [minimum]
    span = maximum - minimum
    raw = span / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for multiple in (1, 2, 2.5, 5, 10):
        step = magnitude * multiple
        if span / step <= count + 0.5:
            break
    start = math.floor(minimum / step) * step
    ticks: list[float] = []
    value = start
    while value <= maximum + step * 0.5 and len(ticks) < 24:
        if value >= minimum - step * 0.001:
            ticks.append(round(value, 10))
        value += step
    return ticks or [minimum, maximum]


def format_number(value: float) -> str:
    """Compact, readable numbers. 8000000000 becomes 8B."""
    magnitude = abs(value)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= threshold:
            scaled = value / threshold
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    if magnitude >= 100 or float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


__all__ = ["Canvas", "TextBlock", "format_number", "nice_ticks"]
