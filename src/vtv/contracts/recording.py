"""The captured voice input.

A ``Recording`` is metadata *about* audio, never the audio itself. The bytes live
in object storage behind an :class:`~vtv.contracts.base.ObjectRef`, so the same
model describes a recording whether it was captured in a browser, uploaded, or
streamed in from a future mobile client.
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
    new_id,
)
from vtv.contracts.errors import ErrorInfo, Status


class AudioFormat(str, Enum):
    """Container/codec of the captured audio.

    ``WEBM_OPUS`` is what browser ``MediaRecorder`` produces on Chromium and is
    therefore the format the primary capture path must always support.
    """

    WEBM_OPUS = "webm/opus"
    OGG_OPUS = "ogg/opus"
    MP4_AAC = "mp4/aac"
    WAV_PCM = "wav/pcm"
    MP3 = "mp3"
    FLAC = "flac"


class CaptureSource(str, Enum):
    """How the audio entered the system.

    The product is voice-first: ``MICROPHONE`` is the primary path and ``UPLOAD``
    is the secondary one (see ``docs/PRODUCT.md``). Keeping the distinction in
    the data lets us measure whether that stays true.
    """

    MICROPHONE = "microphone"
    UPLOAD = "upload"
    API = "api"
    #: The narration was synthesised from written input — a document, a deck, a
    #: paragraph. Kept distinct so that no metric, evaluation or billing report
    #: ever counts a synthesised track as a user recording.
    DOCUMENT = "document"


#: Hard ceiling on a single recording. Long enough for a full lecture segment,
#: short enough that transcription, planning and rendering stay affordable and
#: predictable. Enforced at capture time so the user learns immediately.
MAX_RECORDING_SECONDS: float = 30 * 60.0

#: Below this, there is nothing meaningful to visualise.
MIN_RECORDING_SECONDS: float = 1.0


class AudioProperties(Timestamped):
    """Measured properties of the audio, filled in after probing the file.

    These are *measured*, not *claimed*: the client may report a duration, but
    the value stored here comes from server-side probing. Client-reported values
    are hints only and are never trusted for billing or timeline construction.
    """

    duration_seconds: Duration
    sample_rate_hz: int = Field(ge=8000, le=192000)
    channels: int = Field(ge=1, le=8)
    bit_rate_bps: int | None = Field(default=None, ge=0)
    peak_dbfs: float | None = Field(default=None, le=0.0)
    #: Fraction of the recording that is below the speech threshold. A very high
    #: value usually means a muted microphone, which we want to catch before
    #: spending money on transcription.
    silence_ratio: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_duration_bounds(self) -> AudioProperties:
        if self.duration_seconds > MAX_RECORDING_SECONDS:
            raise ValueError(
                f"recording exceeds the {MAX_RECORDING_SECONDS:.0f}s maximum"
            )
        if self.duration_seconds < MIN_RECORDING_SECONDS:
            raise ValueError(
                f"recording is shorter than the {MIN_RECORDING_SECONDS:.0f}s minimum"
            )
        return self


class Recording(RootDocument, Timestamped):
    """A single captured utterance, and the root of one pipeline run."""

    document_name = "recording"

    recording_id: Id = Field(default_factory=lambda: new_id(IdPrefix.RECORDING))
    #: The tenant this belongs to. Carried on every project-scoped document so
    #: that a service deep in the pipeline can name the storage namespace it is
    #: allowed to write to without an ambient lookup — `LocalStorageProvider`
    #: refuses any key outside `orgs/<organisation_id>/`.
    organisation_id: Id
    project_id: Id

    source: CaptureSource = CaptureSource.MICROPHONE
    format: AudioFormat
    audio: ObjectRef
    properties: AudioProperties | None = None

    #: BCP-47 tag the speaker declared or the client detected, if any. The
    #: transcription provider may override this with what it actually heard.
    declared_language: str | None = Field(default=None, max_length=16)

    status: Status = Status.PENDING
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _check_status_consistency(self) -> Recording:
        if self.status is Status.READY and self.properties is None:
            raise ValueError("a READY recording must have measured audio properties")
        if self.status is Status.FAILED and self.error is None:
            raise ValueError("a FAILED recording must carry an ErrorInfo")
        if self.error is not None and self.status not in {
            Status.FAILED,
            Status.RETRYING,
        }:
            raise ValueError("only FAILED or RETRYING recordings may carry an error")
        return self

    @property
    def duration_seconds(self) -> float | None:
        return self.properties.duration_seconds if self.properties else None


__all__ = [
    "MAX_RECORDING_SECONDS",
    "MIN_RECORDING_SECONDS",
    "AudioFormat",
    "AudioProperties",
    "CaptureSource",
    "Recording",
]
