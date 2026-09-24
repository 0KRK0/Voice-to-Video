"""Turning a timeline into a file.

The renderer port is the narrowest interface in the system, on purpose. It takes
a fully-resolved timeline and settings, and it returns a finished job. It is
given no access to AI providers, no ability to search for assets, and no say in
what anything should look like (Rule 13).

Remotion is the first implementation. This port exists so that it is a choice
rather than a commitment: a second implementation — a headless compositor, a
GPU-backed encoder, a different framework entirely — can be introduced without a
single change above this line.

## Two ports, at two scales

`Renderer` is a whole video: give it a timeline, get a finished job. That is the
interface the product uses and it has not changed.

`RenderBackend` is **one segment of frames**, and it is the new one. It exists
because the interesting question is no longer "which renderer" but "which
*hardware*, for this range of frames, right now" — and the answer can differ
between segment 3 and segment 4 of the same video, when a graphics card runs
out of memory partway through.

Keeping them separate is what stops a GPU appearing in the product's vocabulary.
The Visual Director, the timeline, grounding and the asset resolver all speak to
`Renderer` and none of them can tell whether a frame was composed on a processor
or a graphics card. That is the whole point of the split, and it is the property
to protect when a GPU backend is written.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from vtv.contracts.display import DisplayList
from vtv.contracts.execution import ExecutionTarget
from vtv.contracts.render import RenderJob, RenderSettings
from vtv.contracts.timeline import Timeline


@runtime_checkable
class Renderer(Protocol):
    """Compose a timeline into a video file."""

    async def render(
        self,
        *,
        timeline: Timeline,
        settings: RenderSettings,
    ) -> RenderJob:
        """Render to completion and return the finished job.

        The implementation is responsible for resolving every object reference
        in the timeline through the storage port, and for failing loudly if one
        cannot be read — a render that silently substitutes a black frame for a
        missing asset is worse than one that fails.
        """
        ...

    async def render_progress(self, render_job_id: str) -> AsyncIterator[RenderJob]:
        """Stream progress updates for a running render.

        Rendering takes long enough that the user must be shown real progress,
        and honest progress requires the renderer to report it rather than the
        UI simulating it.
        """
        ...

    async def cancel(self, render_job_id: str) -> None:
        """Stop a running render. Idempotent."""
        ...


@dataclass(frozen=True)
class RenderCapabilities:
    """What one backend can actually do, and how fast it does it.

    ## Why `frames_per_second` is measured and may be `None`

    Choosing a backend by hardware name — "an RTX 4070 is present, therefore
    use it" — is the mistake this field exists to avoid. A card can be present
    and driverless, present and already saturated by something else, present
    and slower than eight CPU cores on a typography-heavy video where almost
    nothing is being sampled. The only honest comparison is throughput on this
    machine, on this kind of frame.

    `None` means nobody has measured yet, and it is deliberately not `0.0`:
    "unmeasured" and "measured as useless" are different facts, and a selector
    that reads a missing measurement as zero would refuse a backend it has
    simply never tried.
    """

    target: ExecutionTarget
    #: Largest frame this backend will compose. `None` for no limit.
    max_pixels: int | None = None
    #: ffmpeg encoder name if this backend encodes in hardware, else "".
    #: Recorded because it is real — it is just not the 77% of the work.
    hardware_encoder: str = ""
    #: Measured composition throughput, frames per second. See above.
    frames_per_second: float | None = None
    #: Primitive kinds this backend cannot draw. A backend that cannot draw
    #: something the timeline contains must say so *before* a render starts,
    #: not by producing a frame with a hole in it.
    unsupported: frozenset[str] = frozenset()

    def can_draw(self, kinds: Iterable[str]) -> bool:
        return not any(kind in self.unsupported for kind in kinds)

    def as_json(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "max_pixels": self.max_pixels,
            "hardware_encoder": self.hardware_encoder,
            "frames_per_second": self.frames_per_second,
            "unsupported": sorted(self.unsupported),
        }


@dataclass(frozen=True)
class SegmentOutcome:
    """What happened to one segment, and whether anywhere else could do better.

    ## The distinction that makes fallback safe

    `elsewhere` separates **"this backend cannot do this"** from **"this cannot
    be done"**, and getting it wrong is expensive in both directions.

    A GPU running out of video memory is the first kind: the CPU has the same
    timeline and plenty of RAM, and falling back finishes the render. A
    timeline referencing an asset that does not exist is the second kind: every
    backend will fail identically, and retrying each one turns a clear error
    into three slow ones and a confusing report.

    So the backend that failed decides, because it is the only thing that knows
    why. Nothing above it is allowed to guess from the message text.
    """

    ok: bool
    #: Empty when `ok`. Otherwise one line, safe to show a user.
    reason: str = ""
    #: Whether a different backend is worth trying for this segment.
    elsewhere: bool = False

    @classmethod
    def done(cls) -> SegmentOutcome:
        return cls(ok=True)

    @classmethod
    def failed(cls, reason: str, *, elsewhere: bool = False) -> SegmentOutcome:
        return cls(ok=False, reason=reason, elsewhere=elsewhere)


@runtime_checkable
class RenderBackend(Protocol):
    """Composes and encodes one range of frames. The unit of execution.

    ## Why the unit is a segment and not a video

    Because a segment is already the unit of everything else: of checkpointing
    (a segment on disk is finished by definition), of parallelism (segments are
    drawn in a pool), and now of hardware fallback. A GPU that dies in the
    fortieth minute of a sixty-minute render costs one segment, and the segments
    already on disk stay on disk — which is what makes shipping a GPU backend
    a reasonable risk rather than a bet on somebody's driver.

    ## What an implementation may not do

    Reach for storage, credentials or the network. Everything a segment needs
    was written to the scratch directory by the parent process before any
    backend was asked to run — see `RenderContext`. A backend runs in a pool
    worker, so an implementation that could fetch would be one that can fail
    differently in a worker than in the parent, and that failure arrives as a
    corrupt segment in the middle of a two-hundred-segment video.

    Implementations must be picklable, for the same reason.
    """

    @property
    def target(self) -> ExecutionTarget:
        """Where this backend runs. One backend, one target."""
        ...

    def capabilities(self) -> RenderCapabilities:
        """What this backend can do on this machine, right now."""
        ...

    def render_segment(
        self, scratch: str, index: int, start_frame: int, end_frame: int
    ) -> SegmentOutcome:
        """Draw and encode `[start_frame, end_frame)` into the scratch directory.

        Returns an outcome rather than raising. Whatever an implementation
        raises has to survive pickling back from a pool worker, and a failure
        to *transport* the failure surfaces as an opaque pool error that hides
        the real cause — on the one path that only runs when something has
        already gone wrong.
        """
        ...


@runtime_checkable
class Painter(Protocol):
    """Turns a `DisplayList` into one frame. The seam a GPU plugs into.

    ## Why this is a port and not a function

    `paint` was a function taking a theme, an engine, a size, a way to fetch
    stills and a memo. That is five arguments threaded through every call
    because the function is stateless — and a GPU painter is not. It holds a
    device, a command queue, compiled pipelines and a texture cache, and every
    one of those is expensive to create and must outlive a single frame.

    So a painter is constructed once per segment and asked for frames. The CPU
    implementation ignores that it could have been a function; the GPU one
    depends on it.

    ## The invariant

    **Every implementation consumes the same `DisplayList`.** There is one
    scene representation and two ways to draw it, never two scene
    representations. The moment a backend needs its own description, the
    composer has to know which backend it is describing for, and the whole
    boundary is gone.

    `tests/test_painter_equivalence.py` is what holds that: it takes any two
    painters and compares them across every layer type, every transition,
    every alpha case and every camera motion.
    """

    @property
    def name(self) -> str:
        """Short identifier for logs and the render record. e.g. "cpu"."""
        ...

    def paint(self, frame: DisplayList) -> object:
        """Draw one frame. Returns an RGB `PIL.Image`, which an encoder takes.

        Typed as `object` rather than as the image class on purpose: this layer
        may import only the standard library, pydantic and `vtv` itself, and
        `test_architecture_boundaries` enforces that. A port naming a vendor
        type is a port that has picked an implementation — the whole point of
        this file is that a second painter can be written against it, and a
        painter that had to produce PIL images specifically would be one that
        cannot hold a GPU texture.
        """
        ...

    def release(self) -> None:
        """Drop per-segment resources. Idempotent, and never raises.

        Called between segments so a long render's working set stays bounded.
        A painter holding a GPU device releases textures here, not the device.
        """
        ...


__all__ = [
    "Painter",
    "RenderBackend",
    "RenderCapabilities",
    "Renderer",
    "SegmentOutcome",
]
