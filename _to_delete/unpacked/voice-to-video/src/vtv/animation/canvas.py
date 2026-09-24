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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from vtv.animation.theme import RGBA, Theme, with_alpha


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

    # -- text -------------------------------------------------------------

    def measure(self, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
        left, top, right, bottom = self.draw.textbbox((0, 0), text, font=font)
        return int(right - left), int(bottom - top)

    def wrap(
        self, text: str, font: ImageFont.FreeTypeFont, max_width: int
    ) -> TextBlock:
        """Greedy word wrap, measured against the actual font metrics.

        Character-count wrapping is the classic bug here: it looks fine in
        testing and then breaks on the first headline full of wide letters.
        """
        words = text.split()
        lines: list[str] = []
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
        mask = Image.new("L", self.image.size, 0)
        ImageDraw.Draw(mask).ellipse(
            [
                -self.theme.width * 0.25,
                -self.theme.height * 0.35,
                self.theme.width * 1.25,
                self.theme.height * 1.35,
            ],
            fill=255,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(self.theme.width * 0.08))
        overlay = Image.new(
            "RGBA", self.image.size, with_alpha((0, 0, 0, 255), strength)
        )
        self.image = Image.composite(
            self.image, Image.alpha_composite(self.image, overlay), mask
        )
        self.draw = ImageDraw.Draw(self.image, "RGBA")

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
