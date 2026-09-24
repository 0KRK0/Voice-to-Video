"""The CPU render backend: PIL composition, x264 encoding, one segment at a time.

## What this is, and what it deliberately is not

This is Phase A of giving the renderer more than one place to run, and its
entire job is to **change nothing**. It wraps `segments.encode_segment` — the
function that has been drawing and encoding segments all along — in the
`RenderBackend` port, so that a second implementation can be added beside it
without the renderer above growing a conditional.

The frames it produces are the frames the previous version produced, and that
is checked rather than asserted: `test_render_segments` compares a pooled render
against an inline one and a resumed render against a whole one, frame for frame
by `framemd5`, and those comparisons run through this class now.

## Why it holds no state

It runs inside a process pool, so it is pickled to a worker. A backend holding
a storage handle, an event sink or an open file would either fail to pickle or —
worse — pickle a copy and behave differently in the worker than in the parent.
Everything a segment needs is already on disk before any backend is asked to
run; see `RenderContext`.

## Why its failures do not fall back

`SegmentOutcome.elsewhere` says whether a *different* backend is worth trying.
For this one the answer is almost always no, and saying so is the honest
default: if PIL and x264 cannot compose a frame, the reason is the timeline, not
the hardware, and every other backend will fail the same way. Retrying would
turn one clear error into several slow ones.

The exception is deliberate and narrow: running out of memory. A machine that
cannot hold one segment's stills might still manage on a backend that streams
them, so that case is marked as worth trying elsewhere — and today, with no
other backend, it simply reports honestly and stops.
"""

from __future__ import annotations

from dataclasses import dataclass

from vtv.contracts.execution import ExecutionTarget
from vtv.contracts.render import MAX_RENDER_WORKERS
from vtv.ports.rendering import RenderCapabilities, SegmentOutcome


@dataclass(frozen=True)
class CpuRenderBackend:
    """Implements `RenderBackend` on the processor. Stateless and picklable."""

    #: Whose machine this is running on. The work is identical either way; the
    #: distinction exists because it decides who pays for the electricity, and
    #: that is the whole basis of the desktop tier.
    where: ExecutionTarget = ExecutionTarget.CLOUD_CPU
    #: Measured composition throughput, when anyone has measured it.
    measured_fps: float | None = None
    #: A hardware encoder this machine could use. Recorded, not used: encoding
    #: is about 23% of render time and hardware encoders cap the whole render
    #: at a 1.3x speedup, while fighting the worker pool that carries the other
    #: 77%. See `adapters/media/compute.py`.
    hardware_encoder: str = ""

    @property
    def target(self) -> ExecutionTarget:
        return self.where

    def capabilities(self) -> RenderCapabilities:
        return RenderCapabilities(
            target=self.where,
            # No pixel ceiling. PIL will compose whatever fits in memory, and
            # what fits in memory is not a number this can know in advance —
            # claiming one would refuse renders that would have worked.
            max_pixels=None,
            hardware_encoder=self.hardware_encoder,
            frames_per_second=self.measured_fps,
            # Every primitive in the visual language has a CPU implementation.
            # That is what makes this the floor: a backend that cannot draw
            # something falls back to one that can, and the chain has to end
            # somewhere that never refuses.
            unsupported=frozenset(),
        )

    def render_segment(
        self, scratch: str, index: int, start_frame: int, end_frame: int
    ) -> SegmentOutcome:
        from vtv.adapters.render.segments import encode_segment

        try:
            reason = encode_segment(scratch, index, start_frame, end_frame)
        except MemoryError:
            return SegmentOutcome.failed(
                f"segment {index}: out of memory composing frames",
                # The one case where different hardware might genuinely help.
                elsewhere=True,
            )
        if reason:
            return SegmentOutcome.failed(reason)
        return SegmentOutcome.done()


def workers_for(cores: int) -> int:
    """How many of these to run at once. One place, so the API and the renderer
    cannot disagree about what a machine will do."""
    return max(1, min(MAX_RENDER_WORKERS, cores))




@dataclass(frozen=True)
class GpuRenderBackend:
    """`RenderBackend` on the graphics card. Composition only; ffmpeg still encodes.

    **Registered only when `gpu_probe.probe()` reports a device that
    initialised and drew a calibration frame matching the CPU reference.** See
    `wiring.execution_registry`. Hardware being present is not a capability.

    ## Why encoding stays on the processor

    Measured: composition is about 77% of render time and encoding about 23%.
    A hardware encoder therefore caps the whole render at a 1.3x speedup, and
    it fights the worker pool that carries the other 77% — consumer NVIDIA
    drivers limit concurrent encode sessions to a handful, so eight parallel
    segments would serialise on the card. Encoding moves later, if measurement
    says it helps, and not to have a "GPU" checkbox.

    ## Why nearly every failure says `elsewhere`

    The opposite of the CPU backend, and for the same reason: what goes wrong
    on a graphics card is almost always about the card. A driver reset, video
    memory exhausted, a context lost when the machine slept, a shader that will
    not compile on this vendor — none of those say anything about the timeline,
    and the processor will finish the segment. Because segments are
    checkpointed, that costs one segment rather than the render.

    The exception is the timeline itself being unrenderable, which arrives as
    the same message from either backend and is passed through unchanged.
    """

    where: ExecutionTarget = ExecutionTarget.LOCAL_GPU
    measured_fps: float | None = None
    hardware_encoder: str = ""

    @property
    def target(self) -> ExecutionTarget:
        return self.where

    def capabilities(self) -> RenderCapabilities:
        from vtv.adapters.render.gpu_probe import probe

        found = probe()
        return RenderCapabilities(
            target=self.where,
            # A device's largest texture bounds what can be composed without
            # tiling. 4K needs 3840; every current card allows at least 16384,
            # but a report that guessed would be a report that lies on the one
            # machine where it matters.
            max_pixels=(found.max_texture**2 if found.max_texture else None),
            hardware_encoder=self.hardware_encoder,
            frames_per_second=self.measured_fps,
            unsupported=frozenset(),
        )

    def render_segment(
        self, scratch: str, index: int, start_frame: int, end_frame: int
    ) -> SegmentOutcome:
        from vtv.adapters.render.segments import encode_segment

        try:
            reason = encode_segment(scratch, index, start_frame, end_frame, "gpu")
        except MemoryError:
            return SegmentOutcome.failed(
                f"segment {index}: out of memory", elsewhere=True
            )
        except Exception as exc:
            # Driver resets and lost contexts surface as anything at all.
            # Catching broadly is right here precisely because the answer is
            # always the same: this card cannot do it, the processor can.
            return SegmentOutcome.failed(
                f"segment {index}: the graphics device failed "
                f"({type(exc).__name__}: {exc})",
                elsewhere=True,
            )
        if reason:
            return SegmentOutcome.failed(reason, elsewhere=True)
        return SegmentOutcome.done()


__all__ = ["CpuRenderBackend", "GpuRenderBackend", "workers_for"]
