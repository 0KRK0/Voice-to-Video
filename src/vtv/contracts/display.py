"""What is on screen at one instant, described rather than drawn.

## Why this exists

The renderer knew what a frame looked like only by drawing it. `_draw_still`,
`_draw_caption`, `_draw_attribution`, `_draw_badge` and the vignette were five
imperative PIL routines inside the composer, and the *only* representation of a
frame was the finished pixels.

That is fine with one compositor and impossible with two. A second
implementation — a graphics card, a different machine, eventually a browser —
cannot consume "call these five methods in this order"; it needs to be told what
to put where. And if it reimplements the five routines instead, the two drift:
somebody adjusts the caption plate's corner radius on the processor path and
the graphics path keeps the old one, and now a render looks different depending
on which hardware drew it.

So the composer's job splits in two:

    RenderContext.describe(t)  →  DisplayList     what is on screen
    Painter.paint(list)        →  Image           how it gets there

`describe` is pure, cheap, and produces no pixels. Every backend consumes the
same description. That is the whole of Phase B's foundation, and it is why the
first task in building a GPU compositor is not GPU code.

## Why `Drawn` is still opaque

A chart, a timeline, a diagram — the programmatic visuals — are already
declarative: `ProgrammaticClipSource` carries a spec, and `AnimationEngine`
turns it into a frame. Decomposing *those* into layers is a much larger job and
buying nothing yet, so a `Drawn` layer says "ask the engine for this spec at
this time" and any backend may implement it by doing exactly that. The boundary
moves inward later, one primitive at a time, without this contract changing.

## Why a list and not a tree

Because the renderer composites strictly back to front and nothing here nests.
A scene graph would be a more general answer to a question nobody is asking,
and generality that is never exercised is the kind that turns out to be wrong
when it finally is.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: An RGBA colour, as PIL takes it.
RGBA = tuple[int, int, int, int]

#: A rectangle in pixels: left, top, right, bottom. Floats because the layout
#: arithmetic is fractional and rounding belongs at the point of drawing, not
#: scattered through whoever computed the box.
Box = tuple[float, float, float, float]


class Fit(str, Enum):
    """How a picture fills the box it is given."""

    #: Crop to fill. What every photograph in a video wants.
    COVER = "cover"
    #: Fit inside, letterboxed. For material that must not be cropped.
    CONTAIN = "contain"


class Anchor(str, Enum):
    """Which point of the text sits at the given position."""

    TOP_LEFT = "top_left"
    LEFT_MIDDLE = "lm"


@dataclass(frozen=True)
class Background:
    """Fill the frame. Always first when present."""

    colour: RGBA


@dataclass(frozen=True)
class Picture:
    """A still, placed in a box.

    The box carries the camera move. Ken Burns, a pan and a zoom differ only in
    which rectangle of the frame the picture is asked to fill at time `t`, and
    computing that in `describe` rather than in the painter means every backend
    performs the same camera move rather than each implementing its own easing.
    """

    #: Key into the render's asset manifest, not a path — a backend may hold
    #: the picture as a texture rather than a file.
    key: str
    box: Box
    fit: Fit = Fit.COVER


@dataclass(frozen=True)
class Drawn:
    """A programmatic visual: chart, timeline, diagram, typography.

    Opaque on purpose — see the module docstring. `seconds` is the time within
    the clip, not within the video, because the animation is a function of its
    own progress.
    """

    #: The `VisualSpec` to draw. Held as an object rather than serialised,
    #: because it already is a contract and re-encoding it here would be a
    #: second representation to keep in step.
    spec: object
    seconds: float
    duration: float


@dataclass(frozen=True)
class Plate:
    """A rounded rectangle. The backing behind captions, chips and badges."""

    box: Box
    radius: float
    fill: RGBA | None = None
    outline: RGBA | None = None
    outline_width: int = 0


@dataclass(frozen=True)
class Label:
    """One run of text at one position."""

    text: str
    #: A theme role — "title", "body", "micro" — never a font file. The theme
    #: owns the type scale, and a layer naming a face would be a layer that can
    #: disagree with it.
    role: str
    position: tuple[float, float]
    colour: RGBA
    anchor: Anchor = Anchor.TOP_LEFT


@dataclass(frozen=True)
class Vignette:
    """Darken the edges. Strength 0 draws nothing."""

    strength: float


@dataclass(frozen=True)
class Message:
    """A centred card saying why there is no picture.

    A layer rather than something the composer assembles from a plate and a
    label, because the wrapping depends on measured text and that measurement
    belongs to whoever has the fonts. What it *says* is decided upstream; how
    wide the box ends up is a painting detail.
    """

    text: str


Layer = Background | Picture | Drawn | Plate | Label | Vignette | Message


class Mix(str, Enum):
    """How this frame is mixed with the one beneath it.

    Not called `Transition`, and that is not a style choice: `timeline.py`
    already exports a `Transition` — the editor's choice of dissolve or wipe
    and how long it lasts — and `contracts/__init__` re-exports both. The two
    imports collided there silently, the timeline's won because it came second,
    and `vtv.contracts.Transition` was the wrong class for anyone who imported
    it expecting this one.

    They are genuinely different things. A `Transition` is an editorial
    decision with a duration; a `Mix` is one of four pixel operations. Naming
    them apart is cheaper than a shadow nobody sees.
    """

    NONE = "none"
    BLEND = "blend"
    WIPE = "wipe"
    PUSH = "push"


@dataclass(frozen=True)
class DisplayList:
    """Everything on screen at one instant, back to front.

    `beneath` and `transition` describe a frame that is a mix of two pictures.
    Keeping that here rather than in the composer means a backend receives the
    whole frame as data — including the fact that it is a dissolve — instead of
    receiving two frames and a convention about what to do with them.
    """

    width: int
    height: int
    #: The picture. What a transition mixes.
    layers: tuple[Layer, ...] = ()
    #: What is drawn *on top of* the finished picture — captions, the credit
    #: chip, the illustrative badge.
    #:
    #: ## Why these are a separate list
    #:
    #: Because a transition must not touch them. The first version of this
    #: contract had one list, and the painter mixed all of it with the outgoing
    #: frame — so for the 0.6 seconds of a dissolve the caption plate faded up
    #: with the picture behind it, and the credit chip along with it. That is
    #: wrong: an overlay is not part of the shot, it is on the glass in front
    #: of it, and it is fully opaque from the first frame it appears.
    #:
    #: Nothing in the existing suite could see it — every render there is
    #: typography with captions off. It was caught by comparing frames against
    #: the previous implementation, which is the only check that would have.
    overlays: tuple[Layer, ...] = ()
    #: The outgoing picture, when this frame is part of a transition.
    beneath: DisplayList | None = None
    transition: Mix = Mix.NONE
    #: How far through the transition, already eased. `describe` applies the
    #: timing curve so that every backend uses the same one.
    progress: float = 0.0
    #: A name for a sub-picture that does not change while it is on screen, so
    #: a backend may paint it once and keep it.
    #:
    #: The outgoing side of a transition is always the same picture — the frame
    #: a millisecond before the previous clip ended — and it was being redrawn
    #: for every frame of every dissolve: twelve times for a 0.4-second
    #: transition at 30fps, three hundred and sixty full frames for a video
    #: with thirty of them. The composer knows it is stable; a painter cannot
    #: tell, because two display lists that describe the same picture are not
    #: cheap to compare. So the composer says so, here.
    #:
    #: Empty means "do not cache this", which is the right default: caching
    #: something that changes is a frozen frame in the finished video.
    key: str = ""

    @property
    def is_plain(self) -> bool:
        """Whether this is a single opaque picture with nothing over it.

        The fast path, and worth naming: a typography shot with captions off is
        one `Drawn` layer, and painting it must not cost a lift into RGBA and a
        flatten back for overlays that are not there. That round trip was about
        10ms of a 42ms frame at 1080p.
        """
        return (
            self.beneath is None
            and not self.overlays
            and len(self.layers) == 1
            and isinstance(self.layers[0], Drawn | Picture | Message)
        )

    @property
    def needs_resampling(self) -> bool:
        """Whether anything here is work a graphics card is actually better at.

        One question, one answer, on the frame itself — because two things ask
        it and they must never disagree. The painter asks in order to decide
        whether to use the device or hand the frame to the reference; the
        renderer asks in order to decide whether to build a device for this
        segment *at all*. A router that sent a segment to hardware the painter
        then declined to use would pay for a graphics context and get nothing,
        and the two rules drifting apart is the kind of thing nobody notices
        because the output stays correct.

        ## Why only a picture counts

        Because resampling a photograph is the only thing here that a card does
        better. Everything else in a display list is either produced by the
        reference already or is cheaper in memory than across a bus:

        * a `Drawn` layer is rasterised by the animation engine **on the
          processor**, so sending it to the card means uploading a finished
          picture, drawing it once, and reading the whole frame back — three
          transfers to deliver something that was already done;
        * `Plate`, `Label`, `Vignette` and `Message` are vector rasterisation,
          which the reference does and a shader would only reimplement;
        * a transition is a blend of two finished frames, which is one pass over
          memory that is already in RAM.

        Counting `Drawn` and `Background` as card work is what made a
        typography-heavy video **2.2x slower** on a GTX 1650 than on the same
        machine's processor — 0.45x at 1080p, 0.36x at 4K, measured. The card
        was doing the round trip and none of the drawing.

        A transition counts only when one of the two frames being mixed has a
        picture in it: a dissolve between two typography shots is entirely
        processor work, and asking for a device to hold its coat costs a context
        and buys nothing.
        """
        if self.beneath is not None and self.transition is not Mix.NONE:
            if self.beneath.needs_resampling:
                return True
        return any(isinstance(layer, Picture) for layer in self.layers)

    def with_beneath(
        self, under: DisplayList, kind: Mix, progress: float
    ) -> DisplayList:
        return DisplayList(
            width=self.width,
            height=self.height,
            layers=self.layers,
            overlays=self.overlays,
            beneath=under,
            transition=kind,
            progress=progress,
            key=self.key,
        )


__all__ = [
    "RGBA",
    "Anchor",
    "Background",
    "Box",
    "DisplayList",
    "Drawn",
    "Fit",
    "Label",
    "Layer",
    "Message",
    "Mix",
    "Picture",
    "Plate",
    "Vignette",
]
