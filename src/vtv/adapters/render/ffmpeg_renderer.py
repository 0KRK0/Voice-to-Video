"""Stage 10 — Rendering a timeline to MP4 with ffmpeg.

STATUS: **REAL IMPLEMENTATION — RUNS AND PRODUCES A PLAYABLE FILE.**

Composition is a pure function of the timeline and a timestamp. For every frame
time the composer asks the timeline what should be on screen and draws it; no
frame depends on the frame before it, and nothing about the picture is decided
here (Rule 13 — everything drawn was decided upstream).

That purity buys three things it always bought:

* **transitions are free** — a dissolve is two frames blended, not a filter graph;
* **captions and attributions are ours** — drawn with the same type system as
  everything else, rather than handed to a subtitle burner whose fonts and
  metrics we do not control;
* **it cannot desynchronise** — audio is muxed once, and every frame's timestamp
  is derived from the narration clock, so drift has nowhere to enter.

and it now buys two more, because a pure function of a timestamp can be
evaluated out of order and in parallel:

* **a render survives a crash** — frames are encoded in segments, and a segment
  on disk is finished by definition, so a machine that dies at 97% resumes at
  97% instead of at zero;
* **cores are used** — segments are drawn in a process pool, because the slow
  part is PIL and Python and the GIL would serialise threads doing it.

`segments.py` holds why that decomposition is shaped the way it is. This module
holds the drawing.

The one thing that is *not* per-frame is asset loading. Stills and extracted
video frames are materialised to the scratch directory once, by the parent
process which is the only one holding a storage handle, and every worker opens
them from there. So the drawing side of this file — `RenderContext` — has no
storage, no network and no events: it takes a directory and a time and returns a
picture. That is what makes it safe to run in a pool worker, and it is enforced
by the fact that it is a separate class rather than by a comment asking nicely.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from vtv.adapters.media import ffmpeg
from vtv.adapters.render import segments as seg
from vtv.animation.canvas import Canvas
from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.animation.painter import CpuPainter, mix
from vtv.animation.theme import Theme, ease_in_out, ease_out_cubic, with_alpha
from vtv.contracts.base import ObjectRef, RetentionClass, TimeSpan
from vtv.contracts.display import (
    Anchor,
    Background,
    DisplayList,
    Drawn,
    Label,
    Layer,
    Message,
    Mix,
    Picture,
    Plate,
)
from vtv.contracts.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    RenderFailed,
    Status,
    VTVError,
)
from vtv.contracts.execution import (
    ExecutionPolicy,
    ExecutionTarget,
    Processor,
    Registry,
)
from vtv.contracts.render import MAX_RENDER_WORKERS, RenderJob, RenderSettings
from vtv.contracts.timeline import (
    AssetClipSource,
    CaptionCue,
    FitPolicy,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    Timeline,
    TransitionKind,
    VisualClip,
)
from vtv.contracts.visual_language import CameraMotion
from vtv.observability.events import EventName, EventSink, Timer
from vtv.pipeline.captions import caption_pages, to_srt, to_vtt
from vtv.security.paths import tenant_key

#: Ken Burns strength: how far the frame travels over a clip. Subtle on purpose
#: — anything more reads as a screensaver.
CAMERA_TRAVEL = 0.10

#: Re-exported so existing callers keep working; defined in the contracts
#: because the capability report needs it too and adapters may not import each
#: other. See `contracts/render.MAX_RENDER_WORKERS`.
MAX_WORKERS = MAX_RENDER_WORKERS

#: How much *finished video* must accumulate before the preview is republished.
#:
#: Thirty seconds keeps a four-hour render's preview to about 480 concatenations
#: instead of one per segment, and nobody watching a render notices a preview
#: that is half a minute behind. See `_publish_preview`.
PREVIEW_EVERY_SECONDS = 30.0


def run_segment(
    chain: list[tuple[ExecutionTarget, object]],
    scratch: str,
    index: int,
    start_frame: int,
    end_frame: int,
) -> tuple[str, str, str]:
    """Render one segment, descending the backend chain when told to.

    Returns `(failure, target_used, superseded_reason)`. Empty failure means it
    worked; `superseded_reason` is non-empty only when an earlier backend was
    tried and refused, so the caller can report a fallback rather than hide it.

    ## Why this is a module-level function

    It is handed to a process pool, and on Windows a pool spawns rather than
    forks — the child imports this module and unpickles the arguments. A
    closure cannot be pickled at all, so writing this as a nested function
    would work in every test on Linux and fail on the machine this product is
    being built on, at the first segment. The arguments are an enum, a frozen
    dataclass, a path and three integers, all of which travel.

    ## Why the descent is per segment

    That is the granularity at which hardware actually fails. A graphics card
    that runs out of memory on one dense shot has not made the video
    impossible, and with segments already checkpointed, a failed attempt costs
    one segment rather than the render. Only a backend that reports `elsewhere`
    is followed by another attempt: a timeline referencing a missing asset
    fails identically everywhere, and retrying it would turn one clear error
    into several slow ones and a confusing report.
    """
    superseded = ""
    for target, backend in _routed(chain, scratch, start_frame, end_frame):
        outcome = backend.render_segment(scratch, index, start_frame, end_frame)
        if outcome.ok:
            return "", target.value, superseded
        superseded = outcome.reason
        if not outcome.elsewhere:
            break
    return (
        superseded or f"segment {index}: no render backend could produce it",
        "",
        "",
    )


#: How many instants of a segment to look at before deciding where it runs.
#:
#: Not every frame, and not one. Every frame is exact and wasteful — twelve
#: seconds is 360 of them and the answer stops changing long before that. One is
#: cheap and wrong, because a segment is not one shot: at twelve seconds it
#: usually spans two or three, and sampling only the first would send a segment
#: that opens on a title and cuts to a photograph to the processor.
#:
#: Ninety-six samples over twelve seconds is one every eighth of a second, which
#: is finer than any shot this product will cut.
_ROUTING_SAMPLES = 96


def _routed(
    chain: list[tuple[ExecutionTarget, object]],
    scratch: str,
    start_frame: int,
    end_frame: int,
) -> list[tuple[ExecutionTarget, object]]:
    """Put the hardware this segment's content is suited to at the front.

    ## Why this is per segment and not per render

    Because the right answer changes inside one video, and by a lot. Measured on
    a GTX 1650: photographs composite **11.6x** faster on the card and
    typography **2.2x slower**, and a normal video alternates between them
    shot by shot. Choosing once for the whole render means either paying the
    typography penalty on every title card or giving up the photograph win on
    every shot — and on a mixed 48-second video, choosing once end to end came
    out at 1.48x when the photograph shots alone were worth eleven.

    A segment is the right unit because it is already the unit of everything
    else — checkpointing, parallelism, fallback — and because it is big enough
    that deciding costs nothing next to drawing it.

    ## What it will not do

    Reorder a chain of one. A caller that asked for exactly one target, strictly,
    gets exactly that target: "never run this in the cloud" is a real
    requirement and a router that quietly satisfied it differently would be the
    same class of lie as a fallback that does not fall back. Routing chooses
    between backends the policy has already allowed.

    It also never *removes* a backend. A segment routed to the processor keeps
    the card behind it in the chain, because being wrong about which is faster
    should cost time, not a failed render.
    """
    if len(chain) < 2:
        return chain
    try:
        wants = _wants_a_device(scratch, start_frame, end_frame)
    except Exception:
        # Deciding must never be able to fail a render. An unroutable segment
        # runs in the order the policy gave, which is what happened before this
        # existed.
        return chain
    if wants:
        return chain
    return sorted(chain, key=lambda pair: pair[0].processor is Processor.GPU)


def _wants_a_device(scratch: str, start_frame: int, end_frame: int) -> bool:
    """Whether any instant in this range is work a card is better at.

    Asks `describe`, which builds no pixels, and then asks the frame itself —
    the same property the painter uses to decide whether to use its device. One
    rule, so a segment cannot be routed to hardware the painter then declines.
    """
    held = seg.context(scratch)
    total = max(1, end_frame - start_frame)
    step = max(1, total // _ROUTING_SAMPLES)
    for frame_index in range(start_frame, end_frame, step):
        if held.describe(frame_index / held.fps).needs_resampling:
            return True
    # The last instant explicitly: a picture that appears in the final frames of
    # a segment is still a picture, and `range` with a stride can step over it.
    return held.describe((end_frame - 1) / held.fps).needs_resampling


def _camera_box(
    theme: Theme, clip: VisualClip, local: float
) -> tuple[float, float, float, float]:
    """Where a still sits at this instant. The camera move, as a rectangle.

    A static image held for eight seconds reads as a broken video, so every
    still moves. Ken Burns, a pan and a zoom differ only in which rectangle of
    the frame the picture is asked to fill — computing that here rather than in
    a painter means every backend performs the same move with the same easing,
    instead of each implementing its own.
    """
    fraction = local / max(0.001, clip.span.duration)
    motion = clip.camera_motion
    eased = ease_out_cubic(fraction) if motion is CameraMotion.KEN_BURNS else fraction
    travel = CAMERA_TRAVEL

    zoom = 1.0 + travel
    offset_x = offset_y = 0.0
    if motion in {CameraMotion.ZOOM_IN, CameraMotion.KEN_BURNS}:
        zoom = 1.0 + travel * eased
    elif motion is CameraMotion.ZOOM_OUT:
        zoom = 1.0 + travel * (1 - eased)
    elif motion in {CameraMotion.PAN_LEFT, CameraMotion.PAN_RIGHT}:
        offset_x = travel * (eased if motion is CameraMotion.PAN_RIGHT else -eased)
    elif motion in {CameraMotion.PAN_UP, CameraMotion.PAN_DOWN}:
        offset_y = travel * (eased if motion is CameraMotion.PAN_DOWN else -eased)
    elif motion is CameraMotion.NONE:
        zoom = 1.0

    if motion is CameraMotion.KEN_BURNS:
        offset_x = travel * 0.35 * eased

    target_w = int(theme.width * zoom)
    target_h = int(theme.height * zoom)
    left = int((theme.width - target_w) / 2 + offset_x * theme.width)
    top = int((theme.height - target_h) / 2 + offset_y * theme.height)
    return (left, top, left + target_w, top + target_h)


def _attribution_layer(theme: Theme, attribution: str) -> tuple[Layer, ...]:
    """A credit chip. A CC-BY image without its credit line is a licence breach."""
    font = theme.font("micro")
    text = attribution[:110]
    width = Canvas(theme).measure(text, font)[0]
    pad = theme.scale(0.01)
    x0 = theme.margin * 0.45
    y0 = theme.margin * 0.45
    return (
        Plate(
            box=(x0, y0, x0 + width + pad * 2, y0 + theme.scale(0.032)),
            radius=theme.scale(0.006),
            fill=with_alpha((0, 0, 0, 255), 0.45),
        ),
        Label(
            text=text,
            role="micro",
            position=(x0 + pad, y0 + theme.scale(0.016)),
            colour=with_alpha(theme.foreground, 0.85),
            anchor=Anchor.LEFT_MIDDLE,
        ),
    )


def _badge_layer(theme: Theme, label: str) -> tuple[Layer, ...]:
    """Generated imagery of a real subject is labelled on screen."""
    font = theme.font("micro")
    width = Canvas(theme).measure(label, font)[0]
    pad = theme.scale(0.012)
    x1 = theme.width - theme.margin * 0.45
    y0 = theme.margin * 0.45
    return (
        Plate(
            box=(x1 - width - pad * 2, y0, x1, y0 + theme.scale(0.032)),
            radius=theme.scale(0.016),
            fill=with_alpha(theme.secondary, 0.9),
        ),
        Label(
            text=label,
            role="micro",
            position=(x1 - width - pad, y0 + theme.scale(0.016)),
            colour=(10, 10, 12, 255),
            anchor=Anchor.LEFT_MIDDLE,
        ),
    )


def _caption_layer(theme: Theme, cue: CaptionCue, t: float) -> tuple[Layer, ...]:
    """The part of this cue that belongs on screen at `t`.

    A cue that does not fit the box is paged rather than cut. It used to return
    only its first two lines and the remainder was never drawn — the sentence
    stopped mid-clause and the viewer had no way to know anything was missing.
    Now the lines are grouped into screenfuls and the elapsed fraction of the
    cue's own span picks which one is showing, so a long line reads as two
    pages and the whole sentence arrives.

    The pages divide the span evenly. That is deliberately simple: dividing by
    character count would make a short second page flash past, and the cue
    boundaries themselves already came from real word timings upstream.
    """
    canvas = Canvas(theme)
    font = theme.font("body")
    pages = caption_pages(cue.text)
    if len(pages) == 1:
        lines = pages[0]
    else:
        elapsed = t - cue.span.start
        share = cue.span.duration / len(pages) if cue.span.duration > 0 else 0.0
        index = int(elapsed / share) if share > 0 else 0
        lines = pages[min(max(index, 0), len(pages) - 1)]
    widths = [canvas.measure(line, font)[0] for line in lines]
    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent) * 1.16)
    pad_x, pad_y = theme.scale(0.022), theme.scale(0.014)
    box_w = max(widths) + pad_x * 2
    box_h = line_height * len(lines) + pad_y * 2
    x0 = (theme.width - box_w) / 2
    y0 = theme.height - theme.margin * 0.55 - box_h

    layers: list[Layer] = [
        Plate(
            box=(x0, y0, x0 + box_w, y0 + box_h),
            radius=theme.scale(0.012),
            fill=with_alpha((0, 0, 0, 255), 0.55),
        )
    ]
    y = y0 + pad_y
    for line, width in zip(lines, widths, strict=True):
        layers.append(
            Label(
                text=line,
                role="body",
                position=((theme.width - width) / 2, y),
                colour=theme.foreground,
            )
        )
        y += line_height
    return tuple(layers)


def _default_registry() -> Registry:
    """This process, on its processor. The floor every renderer starts from.

    Deliberately not `wiring.execution_registry`: an adapter that reached into
    the composition root would invert the layering, and a renderer constructed
    directly in a test would then depend on how the whole system is assembled.
    A registry passed in wins; this is what "nobody passed one" means.
    """
    from vtv.adapters.render.cpu_backend import CpuRenderBackend

    registry = Registry()
    registry.register(ExecutionTarget.CLOUD_CPU, CpuRenderBackend())
    return registry


#: The composer's transition vocabulary, in the display list's terms.
#:
#: Two enums rather than one because they answer different questions:
#: `TransitionKind` is what an editor chose and is part of the timeline
#: contract; `Mix` is what a backend has to draw. A cut is not a
#: transition at all once you are painting, which is why it has no entry here.
_TRANSITIONS: dict[TransitionKind, Mix] = {
    TransitionKind.FADE: Mix.BLEND,
    TransitionKind.DISSOLVE: Mix.BLEND,
    TransitionKind.WIPE: Mix.WIPE,
    TransitionKind.PUSH: Mix.PUSH,
}


def _combine(
    under: Image.Image, over: Image.Image, kind: TransitionKind, progress: float
) -> Image.Image:
    """One frame of a transition, in the timeline's vocabulary.

    The pixels are `painter.mix`; this is the translation. Two names for one
    operation is worth it because the alternative was the painter importing an
    adapter, which inverts the layering — and because a `TransitionKind` the
    contract gains but this table does not is exactly the bug the coverage test
    below catches.
    """
    return mix(under, over, _TRANSITIONS.get(kind, Mix.BLEND), progress)


@dataclass(eq=False)
class _ResolvedClip:
    """A clip with its pixels *addressable*, not necessarily loaded.

    ## Why the still is a path and not an image

    Every clip's still used to be decoded into memory before the first frame was
    drawn, and held until the render finished. At 1536×1024 that is 4.7MB each;
    a forty-minute video has around seven hundred visual units, so the renderer
    asked for three gigabytes before it drew anything — on a machine that also
    had to run an encoder. It survived because much of that video was drawn
    typography, which is generated rather than loaded, and it would not have
    survived a video of photographs.

    Loading on demand and releasing between segments bounds the working set to
    whatever one segment touches — a handful of shots — regardless of how long
    the video is. The cost is re-decoding a clip that straddles two segments,
    which is one JPEG decode against three hundred and sixty composited frames.

    `eq=False` matters: the default dataclass equality would compare PIL images
    field by field, and `_previous` locates a clip by identity in a list. An
    accidental value comparison there would find the wrong neighbour and blend
    the wrong shot into a transition.
    """

    clip: VisualClip
    #: Where the still lives on disk, for image sources.
    still_path: Path | None = None
    #: Directory of extracted frames, for video sources.
    frames: list[Path] | None = None
    frame_rate: float = 30.0
    _still: Image.Image | None = field(default=None, repr=False)

    @property
    def has_still(self) -> bool:
        return self.still_path is not None

    @property
    def still(self) -> Image.Image | None:
        if self.still_path is None:
            return None
        if self._still is None:
            with Image.open(self.still_path) as opened:
                self._still = opened.convert("RGB").copy()
        return self._still

    def release(self) -> None:
        self._still = None


def _cpu_painter(context: RenderContext) -> object:
    return CpuPainter(
        theme=context.theme, engine=context.engine, size=context.size,
        stills=context._still_for,
    )


def _gpu_painter(context: RenderContext) -> object:
    from vtv.adapters.render.gpu_painter import GpuPainter

    return GpuPainter(
        theme=context.theme, engine=context.engine, size=context.size,
        stills=context._still_for,
    )


#: Every painter a backend may ask for, by the name it asks for it by. A table
#: rather than a chain of `if`s so that adding one cannot mean adding it to the
#: construction path and forgetting the *other* direction, which is exactly the
#: mistake `use_painter` documents.
_PAINTERS = {"cpu": _cpu_painter, "gpu": _gpu_painter}


def _retire(painter: object) -> None:
    """Hand a painter's resources back. Never raises.

    `close` where there is one — a GPU painter's device — and `release`
    otherwise. Teardown running at the end of a successful render must not be
    able to fail it.
    """
    handler = getattr(painter, "close", None) or getattr(painter, "release", None)
    if handler is not None:
        with contextlib.suppress(Exception):
            handler()


class RenderContext:
    """Draws any frame of one video, from a directory and nothing else.

    Constructed in the parent process, saved to `plan.json`, and reconstructed
    inside each pool worker. It holds no storage handle, opens no sockets and
    emits no events — everything it needs was written to the scratch directory
    before the first segment was dispatched.

    That is a deliberate boundary and not merely a convenience. A composer that
    could reach storage would be a composer that can fail differently in a
    worker than it does in the parent, and the failure would appear as a
    corrupt segment in the middle of a two-hundred-segment video.
    """

    def __init__(
        self,
        *,
        timeline: Timeline,
        settings: RenderSettings,
        scratch: Path,
        assets: dict[str, dict[str, object]],
    ) -> None:
        self.timeline = timeline
        self.settings = settings
        self.scratch = scratch
        self.assets = assets
        self.fps = settings.frame_rate

        width, height = settings.dimensions
        self.size = RenderSize(width, height)
        self.theme = Theme.from_style(timeline.style, width=width, height=height)
        self.engine = AnimationEngine(
            timeline.style, entity_colours=timeline.entity_colours
        )

        self.resolved = [self._rebuild(clip) for clip in timeline.clips]
        #: What turns a description into pixels. The processor by default; a
        #: backend that wants the graphics card swaps it with `use_painter`.
        #: Nothing above here knows which one is in place.
        self.painter: object = _cpu_painter(self)
        # Clip starts, for locating the active clip by bisection rather than by
        # scanning. A four-hour video has ~2 900 clips and ~430 000 frames; the
        # linear scan this replaces did up to 1.2 billion comparisons per
        # render, which does not show up in a ten-second test and dominates a
        # long one.
        self._starts = [item.clip.span.start for item in self.resolved]
        self._transition_frames: dict[str, Image.Image] = {}

    # -- persistence ------------------------------------------------------

    def save(self) -> None:
        (self.scratch / "plan.json").write_text(
            json.dumps(
                {
                    "timeline": self.timeline.model_dump(mode="json"),
                    "settings": self.settings.model_dump(mode="json"),
                    "assets": self.assets,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, scratch: Path) -> RenderContext:
        payload = json.loads((scratch / "plan.json").read_text(encoding="utf-8"))
        return cls(
            timeline=Timeline.model_validate(payload["timeline"]),
            settings=RenderSettings.model_validate(payload["settings"]),
            scratch=scratch,
            assets=payload["assets"],
        )

    def _rebuild(self, clip: VisualClip) -> _ResolvedClip:
        entry = self.assets.get(clip.clip_id)
        if not entry:
            return _ResolvedClip(clip=clip)
        still = entry.get("still")
        if isinstance(still, str):
            return _ResolvedClip(clip=clip, still_path=self.scratch / still)
        folder = entry.get("frames")
        if isinstance(folder, str):
            directory = self.scratch / folder
            return _ResolvedClip(
                clip=clip,
                frames=sorted(directory.glob("frame-*.png")),
                frame_rate=float(entry.get("frame_rate", self.fps) or self.fps),
            )
        return _ResolvedClip(clip=clip)

    def release(self) -> None:
        """Drop decoded pixels. Called between segments to bound memory."""
        for item in self.resolved:
            item.release()
        self._transition_frames.clear()
        with contextlib.suppress(Exception):
            self.painter.release()  # type: ignore[attr-defined]

    # -- frame composition ------------------------------------------------

    def compose(self, t: float) -> Image.Image:
        """The finished frame. Describe it, then paint it.

        Two steps rather than one, and the split is the point: `describe`
        produces a `DisplayList` — what is on screen, as data — and the painter
        turns that into pixels. A second compositor consumes the same
        description rather than reimplementing the drawing, which is what stops
        two backends drifting apart on the corner radius of a caption plate.
        """
        return self.painter.paint(self.describe(t))  # type: ignore[attr-defined]

    def use_painter(self, name: str) -> None:
        """Swap the painter, in **either** direction. Raises if it cannot start.

        Raising is correct here: a backend that asked for the graphics card and
        silently got the processor would report a GPU render that never
        happened, and the segment record would be a lie.

        ## The bug this shape exists to prevent

        The first version read `if name == "cpu": return`, on the assumption
        that the painter is the processor unless somebody has asked for the
        card. That holds for exactly one segment. This context is cached for
        the whole render — `segments._context` keys it by scratch directory —
        so once any segment has run on the graphics card, every later segment
        asking for "cpu" kept the GPU painter and drew on the card anyway.

        Which means **the fallback did not fall back**. A card that failed at
        segment three had segments four onward drawn on it regardless, recorded
        as processor work. That is the same lie the docstring above forbids,
        pointing the other way, and it was invisible in every test because no
        test had ever switched back.

        It was found by a whole-render check on real hardware, not by the
        equivalence harness: the harness compares painters, and this is a bug
        about *which painter is installed*. Hence the shape now — the name is
        matched against the painter actually in place, not against an
        assumption about history.
        """
        if name not in _PAINTERS:
            raise ValueError(f"unknown painter {name!r}")
        if getattr(self.painter, "name", "") == name:
            return

        # Built before anything is replaced, so a device that will not start
        # leaves a working painter in place. The caller reports the failure and
        # the chain descends; a context left without a painter would turn one
        # refused segment into a broken render.
        replacement = _PAINTERS[name](self)
        retiring, self.painter = self.painter, replacement
        _retire(retiring)

    def close(self) -> None:
        """Give everything back, including the painter's device.

        `release` deliberately keeps the device between segments; this is the
        end of the render. Without it every render leaves its graphics context
        alive for the life of the process, and the fourth one on a 4 GB card
        fails with "cannot create texture" — which is how this was found.
        """
        self.release()
        _retire(self.painter)

    def describe(self, t: float) -> DisplayList:
        """What is on screen at `t`. No pixels, no fonts, no measurement.

        Cheap enough to call for its own sake — the calibration harness times
        `compose`, but a backend deciding *whether it can draw a frame* only
        needs this.
        """
        timeline, settings = self.timeline, self.settings
        active = self._active(t)
        base: list[Layer] = []
        if active is None:
            base.append(Background(colour=self.theme.background))
        else:
            base.append(self._describe_clip(active, t))

        frame = DisplayList(
            width=self.size.width, height=self.size.height, layers=tuple(base)
        )

        # Every transition is the outgoing clip's final frame combined with the
        # incoming one. What differs is *how* they are combined, and until
        # recently they did not differ at all: `TransitionKind` offered cut,
        # fade, dissolve, wipe and push, and the renderer blended for all four
        # of the non-cut ones. A timeline could ask for a wipe, the API would
        # accept it, the editor would show it, and the video would dissolve — a
        # feature that exists everywhere except on screen.
        if active is not None:
            transition = active.clip.transition_in
            if transition.kind is not TransitionKind.CUT and transition.duration_seconds > 0:
                since = t - active.clip.span.start
                if 0 <= since < transition.duration_seconds:
                    previous = self._previous(active)
                    if previous is not None:
                        frame = frame.with_beneath(
                            DisplayList(
                                width=self.size.width,
                                height=self.size.height,
                                layers=(
                                    self._describe_clip(
                                        previous, previous.clip.span.end - 0.001
                                    ),
                                ),
                                # Always the same picture for the whole
                                # transition — the frame a millisecond before
                                # the outgoing clip ended — so it is painted
                                # once and kept. Without this a 0.4-second
                                # dissolve at 30fps redraws it twelve times.
                                key=previous.clip.clip_id,
                            ),
                            _TRANSITIONS.get(transition.kind, Mix.BLEND),
                            # Eased here, so every backend uses one timing
                            # curve rather than each choosing its own.
                            ease_in_out(since / transition.duration_seconds),
                        )

        # What, if anything, goes on top of the picture. Decided before any
        # canvas exists: a frame with nothing over it is returned as-is by the
        # painter, and that fast path is worth about 10ms of a 42ms frame at
        # 1080p on a typography shot with captions off.
        source = active.clip.source if active is not None else None
        overlays: list[Layer] = []
        if isinstance(source, AssetClipSource):
            if source.attribution and settings.include_attributions:
                overlays.extend(_attribution_layer(self.theme, source.attribution))
            if source.illustrative_label:
                # Generated imagery of real subjects is labelled on screen.
                overlays.extend(_badge_layer(self.theme, "Illustrative"))
        if settings.burn_in_captions and timeline.style.captions_enabled:
            cue = self._caption_at(timeline.captions, t)
            if cue is not None:
                overlays.extend(_caption_layer(self.theme, cue, t))
        if not overlays:
            return frame
        return DisplayList(
            width=frame.width,
            height=frame.height,
            layers=frame.layers,
            # Separate from the picture on purpose: a transition mixes the
            # shot, never what is drawn on the glass in front of it.
            overlays=tuple(overlays),
            beneath=frame.beneath,
            transition=frame.transition,
            progress=frame.progress,
        )

    def _describe_clip(self, resolved: _ResolvedClip, t: float) -> Layer:
        """One clip's contribution, as a layer rather than as pixels."""
        clip = resolved.clip
        local = max(0.0, min(clip.span.duration, t - clip.span.start))
        source = clip.source

        if isinstance(source, ProgrammaticClipSource):
            return Drawn(spec=source.spec, seconds=local, duration=clip.span.duration)
        if isinstance(source, PlaceholderClipSource):
            return Message(text=source.message)
        if resolved.has_still:
            return Picture(key=clip.clip_id, box=_camera_box(self.theme, clip, local))
        if resolved.frames:
            return Picture(
                key=f"{clip.clip_id}#{self._frame_index(resolved, local)}",
                box=(0, 0, self.theme.width, self.theme.height),
            )
        return Message(text="Visual unavailable")

    def _still_for(self, key: str) -> Image.Image | None:
        """The picture behind a `Picture` layer's key.

        The one place the display list touches the filesystem, and it is the
        painter that calls it — a layer holds a key so that a backend holding
        textures rather than files can satisfy it its own way.
        """
        clip_id, _, frame = key.partition("#")
        for item in self.resolved:
            if item.clip.clip_id != clip_id:
                continue
            if frame:
                paths = item.frames or []
                if not paths:
                    return None
                with Image.open(paths[min(int(frame), len(paths) - 1)]) as opened:
                    return opened.convert("RGB").copy()
            return item.still
        return None

    @staticmethod
    def _frame_index(resolved: _ResolvedClip, local: float) -> int:
        """Which extracted frame to show, honouring the clip's fit policy."""
        count = len(resolved.frames or [])
        if count == 0:
            return 0
        raw = int(local * resolved.frame_rate)
        fit = resolved.clip.fit
        if fit is FitPolicy.LOOP:
            return raw % count
        if fit is FitPolicy.SPEED_RAMP:
            fraction = local / max(0.001, resolved.clip.span.duration)
            return min(count - 1, int(fraction * count))
        # TRIM and HOLD_LAST both stop at the final frame; TRIM simply never
        # reaches it because the media is longer than the slot.
        return min(raw, count - 1)

    # -- overlays ---------------------------------------------------------

    @staticmethod
    def _caption_at(cues: list[CaptionCue], t: float) -> CaptionCue | None:
        for cue in cues:
            if cue.span.start <= t < cue.span.end:
                return cue
        return None

    # -- lookup -----------------------------------------------------------

    def _active(self, t: float) -> _ResolvedClip | None:
        if not self.resolved:
            return None
        index = bisect.bisect_right(self._starts, t) - 1
        if index < 0:
            return None
        candidate = self.resolved[index]
        if t < candidate.clip.span.end:
            return candidate
        # Past the end of the last clip the final picture holds, which is what
        # fills the deliberate visual time the pacing planner allocates after
        # the narration stops. Inside a gap between clips, nothing is active and
        # the background shows.
        return self.resolved[-1] if index == len(self.resolved) - 1 else None

    def _previous(self, current: _ResolvedClip) -> _ResolvedClip | None:
        for index, item in enumerate(self.resolved):
            if item is current:
                return self.resolved[index - 1] if index > 0 else None
        return None


class FfmpegRenderer:
    """Composes and encodes a timeline. Implements the `Renderer` port."""

    def __init__(
        self,
        *,
        storage: object,
        events: EventSink,
        workdir: Path | None = None,
        workers: int | None = None,
        segment_seconds: float = seg.TARGET_SECONDS,
        preview_every_seconds: float = PREVIEW_EVERY_SECONDS,
        backends: Registry | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self.storage = storage
        self.events = events
        self.workdir = workdir
        #: How much video one checkpoint covers. See `segments.TARGET_SECONDS`
        #: for the trade; smaller means less lost to a crash and more ffmpeg
        #: invocations.
        self.segment_seconds = segment_seconds
        #: How much finished video accumulates before the preview is
        #: republished. See `_publish_preview`.
        self.preview_every_seconds = preview_every_seconds
        #: Which backends may draw segments, and where each of them runs.
        #:
        #: Defaults to this process's own CPU backend, so a renderer built
        #: without one behaves exactly as it did before backends existed. A
        #: deployment with more — a desktop engine with a graphics card — passes
        #: a fuller registry from `wiring.execution_registry`, and nothing in
        #: this class changes.
        self.backends = backends if backends is not None else _default_registry()
        #: Where the caller would like this to run. `None` means AUTO.
        self.policy = policy or ExecutionPolicy.auto()
        #: How many segments to draw at once. `None` means "as many as this
        #: machine has cores, up to `MAX_WORKERS`"; `1` means draw inline with
        #: no pool at all, which is what tests and tiny previews want because
        #: starting a worker costs more than the video does.
        self.workers = workers
        self._progress: dict[str, RenderJob] = {}

    # -- Renderer port ----------------------------------------------------

    async def render(
        self, *, timeline: Timeline, settings: RenderSettings
    ) -> RenderJob:
        job = RenderJob(
            organisation_id=timeline.organisation_id,
            project_id=timeline.project_id,
            timeline_id=timeline.timeline_id,
            settings=settings,
            status=Status.PROCESSING,
        )
        self._progress[job.render_job_id] = job

        renderable, problems = timeline.is_renderable()
        if not renderable:
            # Coverage gaps and missing captions are caught here rather than
            # discovered in the finished file.
            #
            # `VALIDATION`, not the class's default `INTERNAL`, and the
            # distinction is the difference between one error and an endless
            # one. `TERMINAL_CATEGORIES` is what a device consults to decide
            # whether a failure was about *this machine* or about *this job*;
            # an internal category means "my fault, give it to somebody else",
            # so a timeline with a two-second hole in its narration was handed
            # back and re-drawn 493 times on a real machine before anyone
            # looked. A timeline that does not cover its own audio is wrong in
            # exactly the same way on every computer in the world.
            raise RenderFailed(
                "timeline is not renderable: " + "; ".join(problems),
                category=ErrorCategory.VALIDATION,
                user_message=(
                    "This timeline does not cover its narration, so it cannot "
                    "be rendered yet."
                ),
            )

        timer = Timer()
        root = self._root()
        seg.sweep(root)
        # Named by *what is being rendered*, never by this attempt's id — that
        # is the whole of how a restarted render finds the segments the dead one
        # left behind. See `segments.fingerprint`.
        scratch = root / f"{timeline.timeline_id}-{seg.fingerprint(timeline, settings)}"
        scratch.mkdir(parents=True, exist_ok=True)

        try:
            output = await self._encode(timeline, settings, scratch, job)
            captions_ref = await self._write_captions(timeline, scratch)
        except VTVError as exc:
            # The error is recorded *before* the status, because `RenderJob`
            # refuses to be FAILED without one — and until now this line set the
            # status alone. Every real render failure therefore died inside
            # pydantic with "a FAILED render job must carry an ErrorInfo",
            # replacing the actual cause ("encode failed (1): …") with a
            # validation error about our own model. The failure path was the one
            # path no test ran.
            job.error = exc.info
            job.status = Status.FAILED
            self.events.emit(
                EventName.RENDER_FAILED,
                project_id=timeline.project_id,
                data={"render_job_id": job.render_job_id},
            )
            # The *files* are deliberately kept — the segments already encoded
            # are the reason a retry is cheap, and deleting them here would make
            # every failure cost a full re-render, which is precisely the
            # behaviour this design replaced. The in-memory context is not: it
            # may be holding a graphics device, and a failed render that keeps
            # one is a failed render that makes the retry fail too.
            seg.forget(scratch)
            raise

        seg.forget(scratch)
        shutil.rmtree(scratch, ignore_errors=True)

        job.output = output
        job.captions_output = captions_ref
        job.duration_seconds = timeline.duration_seconds
        job.render_seconds = timer.elapsed_ms / 1000.0
        job.progress = 1.0
        job.status = Status.READY

        self.events.emit(
            EventName.RENDER_COMPLETED,
            project_id=timeline.project_id,
            duration_ms=timer.elapsed_ms,
            data={
                "render_job_id": job.render_job_id,
                "bytes": output.size_bytes,
                "realtime_ratio": round(
                    (timer.elapsed_ms / 1000.0) / max(0.001, timeline.duration_seconds), 2
                ),
            },
        )
        return job

    def chain(self) -> list[tuple[ExecutionTarget, object]]:
        """The backends to try for each segment, best first.

        Resolved once per render rather than per segment: which hardware exists
        does not change while a video is being encoded, and re-deriving it two
        thousand times would be two thousand chances to derive it differently.
        What *is* decided per segment is how far down this list a particular
        range of frames has to go — see `run_segment`.
        """
        return self.backends.chain(self.policy)

    def _root(self) -> Path:
        """Where segment directories live between attempts.

        A configured `workdir` is used as given. Without one the renderer used
        to call `mkdtemp` and delete the result in a `finally`, which meant a
        crashed render's segments were either already gone or in a directory
        the next attempt could not name. A stable path under the system temp
        directory is what makes resumption possible at all; `sweep` stops it
        growing without bound.
        """
        root = Path(self.workdir) if self.workdir else Path(tempfile.gettempdir()) / "vtv-renders"
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def render_progress(self, render_job_id: str) -> AsyncIterator[RenderJob]:
        async def iterator() -> AsyncIterator[RenderJob]:
            job = self._progress.get(render_job_id)
            if job is not None:
                yield job

        return iterator()

    async def cancel(self, render_job_id: str) -> None:
        job = self._progress.get(render_job_id)
        if job is not None and not job.status.is_terminal:
            job.error = ErrorInfo.of(
                ErrorCode.RENDER_FAILED,
                ErrorCategory.USER,
                "render cancelled by request",
                user_message="You cancelled this render.",
            )
            job.status = Status.FAILED

    # -- encoding ---------------------------------------------------------

    async def _encode(
        self,
        timeline: Timeline,
        settings: RenderSettings,
        scratch: Path,
        job: RenderJob,
    ) -> ObjectRef:
        fps = settings.frame_rate
        total_frames = max(1, round(timeline.duration_seconds * fps))

        assets = await self._materialise(timeline, scratch, fps)
        context = RenderContext(
            timeline=timeline, settings=settings, scratch=scratch, assets=assets
        )
        context.save()

        plan = seg.plan(
            [clip.span.start for clip in timeline.clips],
            fps=fps,
            total_frames=total_frames,
            target_seconds=self.segment_seconds,
        )
        outstanding = [item for item in plan if not item.done(scratch)]
        resumed = len(plan) - len(outstanding)

        self.events.emit(
            EventName.RENDER_STARTED,
            project_id=timeline.project_id,
            data={
                "render_job_id": job.render_job_id,
                "quality": settings.quality.value,
                "clips": len(timeline.clips),
                "duration_seconds": round(timeline.duration_seconds, 3),
                "segments": len(plan),
                # Reported, not inferred. A user whose render died overnight can
                # see from the event stream that it picked up where it stopped,
                # and a user whose first attempt this is sees zero.
                "resumed_segments": resumed,
                "workers": self._worker_count(len(outstanding)),
            },
        )

        job.progress = round(resumed / len(plan), 3) if plan else 0.0
        await self._encode_segments(timeline, context, scratch, outstanding, plan, job)

        output_path = await asyncio.to_thread(
            self._assemble, timeline, settings, scratch, plan
        )

        stored: ObjectRef = await self.storage.put_file(  # type: ignore[attr-defined]
            key=tenant_key(
                timeline.organisation_id,
                "projects",
                timeline.project_id,
                "renders",
                f"{job.render_job_id}.mp4",
            ),
            source=output_path,
            content_type="video/mp4",
            retention=RetentionClass.EPHEMERAL,
        )
        return stored

    def _worker_count(self, outstanding: int) -> int:
        if self.workers is not None:
            return max(1, self.workers)
        if outstanding <= 1:
            # One segment cannot be parallelised, and a pool would cost more to
            # start than the segment costs to draw.
            return 1
        return max(1, min(MAX_WORKERS, os.cpu_count() or 1, outstanding))

    async def _encode_segments(
        self,
        timeline: Timeline,
        context: RenderContext,
        scratch: Path,
        outstanding: list[seg.Segment],
        plan: list[seg.Segment],
        job: RenderJob,
    ) -> None:
        """Draw and encode every segment that is not already on disk.

        Inline and pooled are the same call in two arrangements, never two
        implementations. A separate sequential path would be the one the tests
        exercise and the pooled one the one users get, which is exactly the
        shape of failure — a test double more permissive than the real thing —
        that this project has paid for before.
        """
        if not outstanding:
            return

        done = len(plan) - len(outstanding)
        finished = {item.index for item in plan if item.done(scratch)}
        last_preview = 0.0

        async def advance(index: int) -> None:
            nonlocal done, last_preview
            done += 1
            finished.add(index)
            job.progress = round(done / len(plan), 3)
            watchable = await self._publish_preview(
                timeline, scratch, plan, finished, job, since=last_preview
            )
            if watchable is not None:
                last_preview = watchable
            self.events.emit(
                EventName.RENDER_PROGRESS,
                project_id=timeline.project_id,
                data={
                    "render_job_id": job.render_job_id,
                    "progress": job.progress,
                    "segments_done": done,
                    "segments_total": len(plan),
                    # Seconds of finished video the user can actually watch
                    # right now, which is not the same as the percentage: with
                    # workers finishing out of order, progress can be 60% while
                    # the watchable prefix is 20%.
                    "preview_seconds": round(job.preview_seconds, 2),
                },
            )

        chain = self.chain()
        used: dict[str, int] = dict(job.backends)

        def record(item: seg.Segment, target: str, after: str) -> None:
            used[target] = used.get(target, 0) + 1
            job.backends = dict(used)
            if not after:
                return
            # Reported, never silent. A render that quietly moved to different
            # hardware is one whose timing and cost the user cannot explain.
            self.events.emit(
                EventName.RENDER_PROGRESS,
                project_id=timeline.project_id,
                data={
                    "render_job_id": job.render_job_id,
                    "segment": item.index,
                    "fell_back_to": target,
                    "after": after[:200],
                },
            )

        workers = self._worker_count(len(outstanding))
        if workers == 1:
            for item in outstanding:
                failure, target, after = await asyncio.to_thread(
                    run_segment, chain, str(scratch), item.index,
                    item.start_frame, item.end_frame,
                )
                if failure:
                    raise RenderFailed(failure)
                record(item, target, after)
                await advance(item.index)
            return

        # The parent's own context is not shared with the workers — each loads
        # its own from `plan.json` — so release its pixels before forking rather
        # than copying them into every child.
        context.release()
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=workers) as pool:
            async def run(item: seg.Segment) -> tuple[seg.Segment, tuple[str, str, str]]:
                return item, await loop.run_in_executor(
                    pool, run_segment, chain, str(scratch), item.index,
                    item.start_frame, item.end_frame,
                )

            futures = [run(item) for item in outstanding]
            failures: list[str] = []
            for future in asyncio.as_completed(futures):
                try:
                    item, (failure, target, after) = await future
                except Exception as exc:  # pragma: no cover - pool death
                    failures.append(f"render worker died: {type(exc).__name__}: {exc}")
                    continue
                if failure:
                    failures.append(failure)
                else:
                    record(item, target, after)
                    await advance(item.index)
            if failures:
                # Every segment that *did* finish is on disk and will be skipped
                # by the retry, so reporting the first failure loses nothing.
                raise RenderFailed(failures[0])

    async def _publish_preview(
        self,
        timeline: Timeline,
        scratch: Path,
        plan: list[seg.Segment],
        finished: set[int],
        job: RenderJob,
        *,
        since: float,
    ) -> float | None:
        """Publish a playable video of the part that is definitely done.

        ## Only the contiguous prefix

        Segments finish out of order — that is what a pool is for — so at any
        moment the finished set looks like `{0, 1, 2, 5, 6, 9}`. Only the run
        starting at zero can be concatenated into something a browser will
        play; the rest are islands. So the preview stops at the first gap, and
        that is the honest thing to show: a video of the part that exists,
        rather than a video with holes in it or a progress bar the user cannot
        check.

        It also means the watchable length is not the percentage. A render can
        be 60% complete with a 20% preview, and the event carries both because
        conflating them is how a user comes to believe the render is stuck.

        ## Throttled by time, not by segments

        A four-hour video is about twelve hundred segments. Re-concatenating
        after each one is twelve hundred ffmpeg invocations, and the last of
        them reads twelve hundred files — quadratic work to keep a preview
        fresher than anybody can watch. Once every `PREVIEW_EVERY_SECONDS` of
        *finished video* is often enough for a person and cheap enough to be
        free.

        ## Failure here is never the render's problem

        A preview is a courtesy. If concatenation or upload fails, the render
        continues and the user simply does not get an early look — losing a
        four-hour encode because a convenience feature could not write a file
        would be an indefensible trade.
        """
        prefix: list[seg.Segment] = []
        for item in plan:
            if item.index not in finished:
                break
            prefix.append(item)
        if not prefix or len(prefix) == len(plan):
            # Nothing yet, or everything — and "everything" is the real output,
            # which `_assemble` is about to produce properly, with audio.
            return None

        seconds = prefix[-1].end_frame / max(1, self.settings_fps(job))
        if seconds - since < self.preview_every_seconds:
            return None

        try:
            path = await asyncio.to_thread(self._concat, scratch, prefix, "preview.mp4")
            job.preview = await self.storage.put_file(  # type: ignore[attr-defined]
                key=tenant_key(
                    timeline.organisation_id,
                    "projects",
                    timeline.project_id,
                    "renders",
                    f"{job.render_job_id}-preview.mp4",
                ),
                source=path,
                content_type="video/mp4",
                retention=RetentionClass.EPHEMERAL,
            )
            job.preview_seconds = seconds
        except Exception:
            return None
        return seconds

    @staticmethod
    def settings_fps(job: RenderJob) -> int:
        return job.settings.frame_rate

    def _concat(
        self, scratch: Path, items: list[seg.Segment], name: str
    ) -> Path:
        """Join segments into one file by stream copy. No audio, no re-encode."""
        listing = scratch / f"{name}.txt"
        listing.write_text(
            "".join(f"file '{item.path(scratch).as_posix()}'\n" for item in items),
            encoding="utf-8",
        )
        output = scratch / name
        result = subprocess.run(
            [
                ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", "-movflags", "+faststart", str(output),
            ],
            capture_output=True,
        )
        if result.returncode != 0 or not output.exists():
            detail = result.stderr.decode("utf-8", "replace").strip()[-400:]
            raise RenderFailed(f"concat failed ({result.returncode}): {detail}")
        return output

    def _assemble(
        self,
        timeline: Timeline,
        settings: RenderSettings,
        scratch: Path,
        plan: list[seg.Segment],
    ) -> Path:
        """Join the segments and mux the narration. One stream copy, no re-encode.

        Concat's demuxer requires every input to share codec parameters, which
        they do by construction: one encoder, one set of flags, one frame size,
        applied by `segments.encode_segment` and nowhere else.
        """
        missing = [item.name for item in plan if not item.done(scratch)]
        if missing:
            # Reached only if a segment vanished between encoding and assembly.
            # Better a named error than a video that is silently short.
            raise RenderFailed(f"segments missing at assembly: {', '.join(missing[:5])}")

        listing = scratch / "segments.txt"
        listing.write_text(
            "".join(f"file '{item.path(scratch).as_posix()}'\n" for item in plan),
            encoding="utf-8",
        )

        audio_path = scratch / "narration.bin"
        output_path = scratch / "render.mp4"
        args = [
            ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
        ]
        if audio_path.exists():
            args += ["-i", str(audio_path)]
        args += ["-c:v", "copy", "-movflags", "+faststart"]
        if audio_path.exists():
            # `apad` makes the audio stream effectively infinite and `-shortest`
            # then cuts the output at the end of the *video*. Without the pad,
            # `-shortest` cuts at the end of the audio — which silently threw
            # away every second of deliberate visual time the pacing planner
            # allocated past the end of the voice, so a video the user asked to
            # be five minutes long came back at the length of their script.
            # `adelay` puts the voice where the timeline says it starts.
            delay_ms = round(timeline.narration_start_seconds * 1000)
            chain = "apad" if delay_ms <= 0 else f"adelay={delay_ms}:all=1,apad"
            args += [
                "-af", chain,
                "-c:a", "aac",
                "-b:a", f"{settings.audio_bitrate_kbps}k",
                "-shortest",
            ]
        args.append(str(output_path))

        result = subprocess.run(args, capture_output=True)
        if result.returncode != 0 or not output_path.exists():
            detail = result.stderr.decode("utf-8", "replace").strip()[-400:]
            raise RenderFailed(f"assembly failed ({result.returncode}): {detail}")
        return output_path

    # -- resolution -------------------------------------------------------

    async def _materialise(
        self, timeline: Timeline, scratch: Path, fps: int
    ) -> dict[str, dict[str, object]]:
        """Put every clip's pixels on disk, and return where they went.

        This is the only part of rendering that touches storage, and it is done
        once in the parent — pool workers have no credentials, no network and no
        business fetching a customer's assets. Everything downstream of here
        reads files.

        Already-materialised assets are left alone, so a resumed render does not
        re-download what the dead attempt already fetched.
        """
        manifest: dict[str, dict[str, object]] = {}
        for clip in timeline.clips:
            source = clip.source
            if not isinstance(source, AssetClipSource):
                continue
            content_type = source.object.content_type

            if content_type.startswith("image/"):
                name = f"asset-{clip.clip_id}.bin"
                target = scratch / name
                if not target.exists():
                    # Written as fetched. Re-encoding to PNG here would cost a
                    # decode and an encode per asset to produce a larger file
                    # that PIL opens no faster.
                    target.write_bytes(
                        await self.storage.get(source.object)  # type: ignore[attr-defined]
                    )
                manifest[clip.clip_id] = {"still": name}
                continue

            if content_type.startswith("video/"):
                folder = f"frames-{clip.clip_id}"
                directory = scratch / folder
                if not any(directory.glob("frame-*.png")):
                    directory.mkdir(parents=True, exist_ok=True)
                    source_file = directory / "source.mp4"
                    source_file.write_bytes(
                        await self.storage.get(source.object)  # type: ignore[attr-defined]
                    )
                    ffmpeg.run(
                        [
                            ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(source_file),
                            "-vf", f"fps={fps}",
                            str(directory / "frame-%05d.png"),
                        ]
                    )
                    if not any(directory.glob("frame-*.png")):
                        raise RenderFailed(
                            f"no frames extracted from clip {clip.clip_id}"
                        )
                manifest[clip.clip_id] = {"frames": folder, "frame_rate": float(fps)}
                continue

            raise RenderFailed(f"unsupported asset content type {content_type}")

        await self._narration_file(timeline, scratch)
        return manifest

    async def _narration_file(self, timeline: Timeline, scratch: Path) -> Path | None:
        path = scratch / "narration.bin"
        if path.exists():
            return path
        try:
            data = await self.storage.get(timeline.narration.audio)  # type: ignore[attr-defined]
        except VTVError:
            # A render with no audio is still a render. The user gets silent
            # visuals and a clear event rather than nothing at all.
            self.events.emit(
                EventName.RENDER_PROGRESS,
                project_id=timeline.project_id,
                data={"warning": "narration audio unavailable; rendering silent"},
            )
            return None
        path.write_bytes(data)
        return path

    async def _write_captions(self, timeline: Timeline, scratch: Path) -> ObjectRef | None:
        """Always write a sidecar, whatever the burn-in setting.

        Accessibility must not depend on a render flag.
        """
        if not timeline.captions:
            return None
        srt = scratch / "captions.srt"
        srt.write_text(to_srt(timeline.captions), encoding="utf-8")
        vtt = scratch / "captions.vtt"
        vtt.write_text(to_vtt(timeline.captions), encoding="utf-8")
        captions: ObjectRef = await self.storage.put_file(  # type: ignore[attr-defined]
            key=tenant_key(
                timeline.organisation_id,
                "projects",
                timeline.project_id,
                "renders",
                f"{timeline.timeline_id}.vtt",
            ),
            source=vtt,
            content_type="text/vtt",
            retention=RetentionClass.PROJECT,
        )
        return captions


def clip_span_seconds(clip: VisualClip) -> TimeSpan:
    return clip.span


def ensure_ffmpeg() -> None:
    if not ffmpeg.is_available():
        raise VTVError(
            "ffmpeg and ffprobe are required for rendering",
            code=ErrorCode.RENDER_FAILED,
        )


__all__ = [
    "CAMERA_TRAVEL",
    "MAX_WORKERS",
    "FfmpegRenderer",
    "RenderContext",
    "ensure_ffmpeg",
]
