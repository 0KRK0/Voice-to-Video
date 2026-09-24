"""Rendering: turning a timeline into an MP4.

The renderer is the least intelligent and most exacting part of the system. It
receives a fully-resolved :class:`~vtv.contracts.timeline.Timeline` and produces
a file. It makes no creative decisions, calls no AI provider, and has no opinion
about meaning. Keeping it that dull is what allows it to be swapped, parallelised
or moved onto different infrastructure without touching anything upstream.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Duration,
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Timestamped,
    UsdAmount,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import ErrorInfo, Status
from vtv.contracts.style import AspectRatio


class VideoCodec(str, Enum):
    H264 = "h264"
    H265 = "h265"
    VP9 = "vp9"


class RenderQuality(str, Enum):
    """Preset trading render time and file size against fidelity.

    ``PREVIEW`` exists so the storyboard can show real motion in seconds rather
    than minutes; it is the same pipeline at lower resolution, never a different
    code path, so what the user previews is what they will get.
    """

    PREVIEW = "preview"
    STANDARD = "standard"
    HIGH = "high"

    @property
    def scale(self) -> float:
        return {
            RenderQuality.PREVIEW: 0.5,
            RenderQuality.STANDARD: 1.0,
            RenderQuality.HIGH: 1.0,
        }[self]

    @property
    def crf(self) -> int:
        return {
            RenderQuality.PREVIEW: 30,
            RenderQuality.STANDARD: 23,
            RenderQuality.HIGH: 18,
        }[self]


class RenderSettings(VTVModel):
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    quality: RenderQuality = RenderQuality.STANDARD
    frame_rate: int = Field(default=30, ge=24, le=60)
    codec: VideoCodec = VideoCodec.H264
    audio_bitrate_kbps: int = Field(default=128, ge=64, le=320)
    #: Burn captions into the frames. When false they are still exported as a
    #: sidecar track, so accessibility never depends on this flag.
    burn_in_captions: bool = True
    #: Render the credits card assembled from asset attributions.
    include_attributions: bool = True

    @property
    def dimensions(self) -> tuple[int, int]:
        width, height = self.aspect_ratio.dimensions_1080
        scale = self.quality.scale
        # Even dimensions are required by every mainstream H.264 encoder.
        return (int(width * scale) // 2 * 2, int(height * scale) // 2 * 2)


class RenderJob(RootDocument, Timestamped):
    """One attempt to render one timeline."""

    document_name = "render_job"

    render_job_id: Id = Field(default_factory=lambda: new_id(IdPrefix.RENDER_JOB))
    project_id: Id
    timeline_id: Id

    settings: RenderSettings = Field(default_factory=RenderSettings)
    status: Status = Status.PENDING
    progress: float = Field(default=0.0, ge=0.0, le=1.0)

    output: ObjectRef | None = None
    #: Sidecar caption file, always produced regardless of burn-in.
    captions_output: ObjectRef | None = None
    duration_seconds: Duration | None = None

    started_at_seconds: float | None = Field(default=None, ge=0)
    render_seconds: float | None = Field(default=None, ge=0)
    #: Compute cost of the render itself, separate from generation cost, so that
    #: unit economics can be reasoned about per stage.
    cost_usd: UsdAmount = 0.0

    attempt: int = Field(default=1, ge=1)
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _job_is_coherent(self) -> RenderJob:
        if self.status is Status.READY:
            if self.output is None:
                raise ValueError("a READY render job must have an output")
            if self.progress != 1.0:
                raise ValueError("a READY render job must report full progress")
        if self.status is Status.FAILED and self.error is None:
            raise ValueError("a FAILED render job must carry an ErrorInfo")
        return self


__all__ = [
    "RenderJob",
    "RenderQuality",
    "RenderSettings",
    "VideoCodec",
]
