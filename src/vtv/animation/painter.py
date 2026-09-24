"""Turning a `DisplayList` into pixels, with PIL.

This is the CPU half of the split described in `contracts/display.py`. The
composer says what is on screen; this says how it gets there. A second backend
replaces this file and nothing else.

## What this had to preserve exactly

Every routine here was previously a method on the composer, and the frames it
produces are checked against the frames those methods produced — pooled against
inline, resumed against whole, fallen-back against clean, all frame-for-frame by
`framemd5`. A refactor of a renderer that changes one pixel is not a refactor,
and the difference would be a caption plate two pixels wider that nobody spots
in review and everybody sees in a video.

Two behaviours in particular are load-bearing and easy to lose:

* **A frame with nothing over it is returned untouched.** No lift into RGBA, no
  flatten back. That round trip is about 10ms of a 42ms frame at 1080p, spent on
  a typography shot with captions off to draw precisely nothing.
* **Layer order is paint order.** Back to front, no sorting, no cleverness. The
  composer already emitted them in the order they must appear.
"""

from __future__ import annotations

from collections.abc import Callable

from PIL import Image, ImageDraw, ImageFilter

from vtv.animation.canvas import Canvas
from vtv.animation.theme import Theme, with_alpha
from vtv.contracts.display import (
    Anchor,
    Background,
    DisplayList,
    Drawn,
    Fit,
    Label,
    Message,
    Picture,
    Plate,
    Mix,
    Vignette,
)

#: Given a `Picture` layer's key, the image behind it — or `None`.
Stills = Callable[[str], "Image.Image | None"]


class CpuPainter:
    """`Painter` on the processor, with PIL. The reference implementation.

    Every other painter is checked against this one. That is not a courtesy to
    the CPU path — it is the only way a second implementation can be trusted,
    because "looks right" is not a test and a two-pixel difference in a caption
    plate is invisible in review and obvious in a video.

    Constructed once per segment and asked for frames. The state it holds is a
    memo of stable sub-pictures; a GPU painter holds a device, a queue and
    compiled pipelines, which is why this is a class at all.
    """

    name = "cpu"

    def __init__(
        self,
        *,
        theme: Theme,
        engine: object,
        size: object,
        stills: Stills,
    ) -> None:
        self.theme = theme
        self.engine = engine
        self.size = size
        self.stills = stills
        self._memo: dict[str, Image.Image] = {}

    def paint(self, frame: DisplayList) -> Image.Image:
        return paint(
            frame,
            theme=self.theme,
            engine=self.engine,
            size=self.size,
            stills=self.stills,
            memo=self._memo,
        )

    def release(self) -> None:
        self._memo.clear()


def paint(
    frame: DisplayList,
    *,
    theme: Theme,
    engine: object,
    size: object,
    stills: Stills,
    memo: dict[str, Image.Image] | None = None,
) -> Image.Image:
    """Draw a display list. Returns RGB, which is what an encoder takes.

    `memo` holds sub-pictures the composer has named as stable — see
    `DisplayList.key`. It is the caller's dictionary, cleared between segments,
    so a four-hour render does not accumulate one full-size frame per clip.
    """
    if frame.key and memo is not None and frame.key in memo:
        return memo[frame.key]

    if frame.beneath is not None and frame.transition is not Mix.NONE:
        # The transition mixes the *pictures*. Overlays are drawn on the
        # result, at full opacity — a caption is on the glass in front of the
        # shot, not part of it, and mixing it in makes it fade up over the
        # length of the dissolve.
        under = paint(
            frame.beneath, theme=theme, engine=engine, size=size, stills=stills,
            memo=memo,
        )
        over = paint(
            DisplayList(width=frame.width, height=frame.height, layers=frame.layers),
            theme=theme,
            engine=engine,
            size=size,
            stills=stills,
            memo=memo,
        )
        mixed = mix(under, over, frame.transition, frame.progress)
        if not frame.overlays:
            return mixed
        return _draw_over(mixed, frame.overlays, theme)

    # The fast path. A single opaque picture is the answer; anything else has
    # to be composited and therefore has to become a canvas.
    if frame.is_plain:
        painted = _base(
            frame.layers[0], theme=theme, engine=engine, size=size, stills=stills
        )
        if frame.key and memo is not None:
            memo[frame.key] = painted
        return painted

    canvas = Canvas(theme)
    started = False
    for layer in (*frame.layers, *frame.overlays):
        if isinstance(layer, Background):
            canvas.image = Image.new("RGBA", canvas.image.size, layer.colour)
            canvas.draw = canvas.draw.__class__(canvas.image, "RGBA")
            started = True
        elif isinstance(layer, Drawn | Picture | Message):
            base = _base(layer, theme=theme, engine=engine, size=size, stills=stills)
            canvas.image = base.convert("RGBA")
            canvas.draw = canvas.draw.__class__(canvas.image, "RGBA")
            started = True
        elif isinstance(layer, Plate):
            canvas.rounded_rect(
                layer.box,
                layer.radius,
                fill=layer.fill,
                outline=layer.outline,
                width=layer.outline_width,
            )
        elif isinstance(layer, Label):
            canvas.text(
                layer.position,
                layer.text,
                theme.font(layer.role, layer.text),
                layer.colour,
                anchor="lm" if layer.anchor is Anchor.LEFT_MIDDLE else "la",
            )
        elif isinstance(layer, Vignette):
            canvas.vignette(layer.strength)
    del started
    painted = canvas.to_rgb()
    if frame.key and memo is not None:
        memo[frame.key] = painted
    return painted


def _draw_over(base: Image.Image, overlays: tuple, theme: Theme) -> Image.Image:
    """Draw overlays onto a finished picture, at full opacity."""
    canvas = Canvas(theme)
    canvas.image = base.convert("RGBA")
    canvas.draw = canvas.draw.__class__(canvas.image, "RGBA")
    for layer in overlays:
        if isinstance(layer, Plate):
            canvas.rounded_rect(
                layer.box,
                layer.radius,
                fill=layer.fill,
                outline=layer.outline,
                width=layer.outline_width,
            )
        elif isinstance(layer, Label):
            canvas.text(
                layer.position,
                layer.text,
                theme.font(layer.role, layer.text),
                layer.colour,
                anchor="lm" if layer.anchor is Anchor.LEFT_MIDDLE else "la",
            )
        elif isinstance(layer, Vignette):
            canvas.vignette(layer.strength)
    return canvas.to_rgb()


def _base(
    layer: object, *, theme: Theme, engine: object, size: object, stills: Stills
) -> Image.Image:
    """The picture a full-frame layer stands for."""
    if isinstance(layer, Drawn):
        return engine.frame_at(  # type: ignore[attr-defined]
            layer.spec, size=size, t=layer.seconds, duration=layer.duration
        )
    if isinstance(layer, Picture):
        still = stills(layer.key)
        if still is None:
            return _message(theme, "Visual unavailable")
        canvas = Canvas(theme)
        canvas.paste_fitted(still, layer.box, cover=layer.fit is Fit.COVER)
        return canvas.to_rgb()
    if isinstance(layer, Message):
        return _message(theme, layer.text)
    return Image.new("RGB", (theme.width, theme.height), theme.background[:3])


def _message(theme: Theme, text: str) -> Image.Image:
    """A centred card saying why there is no picture.

    Assembled here rather than by the composer because the box is sized from
    measured text, and measuring needs the fonts. What it *says* was decided
    upstream; how wide it ends up is a painting detail.
    """
    canvas = Canvas(theme)
    font = theme.font("title")
    block = canvas.wrap(text, font, int(theme.safe_width * 0.7))
    canvas.rounded_rect(
        (
            theme.width / 2 - block.width / 2 - theme.scale(0.05),
            theme.height / 2 - block.height / 2 - theme.scale(0.04),
            theme.width / 2 + block.width / 2 + theme.scale(0.05),
            theme.height / 2 + block.height / 2 + theme.scale(0.04),
        ),
        theme.scale(0.02),
        outline=with_alpha(theme.muted, 0.6),
        width=2,
    )
    canvas.text_block(
        block,
        font,
        (
            int(theme.width / 2 - block.width / 2),
            int(theme.height / 2 - block.height / 2),
        ),
        theme.muted,
        align="center",
    )
    return canvas.to_rgb()


def mix(
    under: Image.Image, over: Image.Image, kind: Mix, progress: float
) -> Image.Image:
    """One frame of a transition: the outgoing picture, the incoming one, and
    how far through we are.

    ``progress`` is already eased, so each of these is a straight geometric or
    photometric mix — the timing curve is one decision made once, not four
    slightly different ones.

    * **fade / dissolve** — a cross-blend. Two names for the same operation in
      this renderer, and deliberately so: a true fade goes out through black and
      back, which on a cut between two lit shots reads as a mistake rather than
      a choice. Editors who want black between shots get it by putting a gap in
      the timeline, which the renderer already fills with the background.
    * **wipe** — a vertical edge travelling left to right, with a soft margin so
      it does not alias into a staircase on diagonal content.
    * **push** — the incoming frame slides in from the right and shoves the
      outgoing one off to the left. Both move, which is what separates a push
      from a slide-over, and it is the transition that reads as "next" rather
      than "meanwhile".

    Anything unrecognised cross-blends. A new `TransitionKind` added to the
    contract will look like a dissolve until it is implemented here, which is
    the mistake this function was written to stop repeating — so the enum and
    this table are checked against each other by a test rather than by hope.
    """
    if kind is Mix.WIPE:
        width, height = under.size
        edge = int(width * progress)
        # A hard edge on a 1080p frame crawls; a soft one reads as an edge.
        feather = max(2, width // 96)
        mask = Image.new("L", (width, height), 0)
        if edge > 0:
            mask.paste(255, (0, 0, min(edge, width), height))
        if 0 < edge < width:
            ImageDraw.Draw(mask).rectangle(
                [max(0, edge - feather), 0, min(width, edge + feather), height],
                fill=128,
            )
            mask = mask.filter(ImageFilter.GaussianBlur(feather / 2))
        return Image.composite(over, under, mask)

    if kind is Mix.PUSH:
        width, height = under.size
        offset = int(width * progress)
        frame = Image.new(under.mode, (width, height))
        frame.paste(under, (-offset, 0))
        frame.paste(over, (width - offset, 0))
        return frame

    return Image.blend(under, over, progress)


__all__ = ["mix", "paint"]
