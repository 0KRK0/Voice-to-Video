"""Silent narration — the honest fallback when no voice exists.

STATUS: **EXECUTED.** This produces a real audio file of the correct duration
using ffmpeg. It contains no speech, because there is no speech synthesiser
available in this environment and inventing one is not possible.

Why this exists at all rather than simply refusing to render document input:

The narration track is the pipeline's clock (Rule 5). Every scene boundary,
every caption cue and every animation timing is expressed against it. A document
has no voice, so without *something* occupying the time axis the entire timeline
collapses. Producing correctly-timed silence keeps one code path for both voice
and documents, so the day a real synthesiser is configured, nothing downstream
changes — the router simply prefers it.

What this must never do is pretend. The provider name is ``silent-narration``,
the capability flag ``real_speech_synthesis`` stays false, and the pipeline
records a :class:`~vtv.contracts.errors.DegradationStep` on every project that
uses it. A user watching the result sees captions and visuals and hears nothing,
which is a visible, self-explaining limitation rather than a hidden one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from vtv.adapters.media import ffmpeg
from vtv.contracts.base import RetentionClass
from vtv.contracts.errors import ProviderError, Status
from vtv.contracts.generation import (
    GenerationRequest,
    GenerationResult,
    SpeechParams,
)
from vtv.ports.base import (
    DataPolicy,
    HealthStatus,
    ProviderCapabilities,
    ProviderHealth,
)

#: Words per minute used to give written text a plausible spoken duration.
#: Matches :data:`vtv.pipeline.ingestion.SPEAKING_WPM` so a document's synthetic
#: transcript and its synthetic audio agree on how long the words take.
SPEAKING_WPM = 145.0

#: Pause between segments, mirroring the ingestion timing model.
SEGMENT_PAUSE = 0.3

#: Never emit an audio file longer than this from a single request.
MAX_SECONDS = 3600.0


def estimate_duration(text: str, *, segments: list[str], rate: float = 1.0) -> float:
    """How long these words would take to say.

    Uses the segment list when present so the pauses land in the same places the
    transcript put them; falls back to a word count for a single block.
    """
    parts = [part for part in segments if part.strip()] or [text]
    spoken = sum(
        max(0.9, (max(1, len(part.split())) / SPEAKING_WPM) * 60.0) for part in parts
    )
    gaps = SEGMENT_PAUSE * max(0, len(parts) - 1)
    return min(MAX_SECONDS, max(1.0, (spoken + gaps) / max(0.5, rate)))


class SilentNarrationProvider:
    """Correctly-timed silence, so document input can still become a video."""

    name = "silent-narration"

    def __init__(self, *, storage: object, sample_rate: int = 44_100) -> None:
        self.storage = storage
        self.sample_rate = sample_rate

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model="silence-1.0",
            unit_cost_usd=0.0,
            unit="request",
            typical_latency_seconds=0.5,
            # Every language, because silence is language-independent — and
            # saying so is honest, not a boast: it produces no speech in any of
            # them. The router's language filter must not prefer this over a
            # real synthesiser, which is why cost and quality both rank last.
            languages=[],
            data_policy=DataPolicy(
                # Nothing leaves the process. This is the one provider that can
                # be trusted with anything, because it reads only a length.
                retains_input=False,
                trains_on_input=False,
                dpa_in_place=True,
            ),
        )

    async def health(self) -> ProviderHealth:
        ready = ffmpeg.is_available()
        return ProviderHealth(
            status=HealthStatus.HEALTHY if ready else HealthStatus.UNAVAILABLE,
            detail=None if ready else "ffmpeg is not installed",
        )

    async def synthesize(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, SpeechParams):
            raise ProviderError("speech request carried the wrong params")
        if not ffmpeg.is_available():
            raise ProviderError("ffmpeg is required to produce a narration track")

        duration = estimate_duration(
            params.text,
            segments=list(params.segment_texts),
            rate=params.speaking_rate,
        )

        with tempfile.TemporaryDirectory(prefix="vtv-silence-") as scratch:
            target = Path(scratch) / "narration.wav"
            ffmpeg.run(
                [
                    ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi",
                    "-i", f"anullsrc=r={self.sample_rate}:cl=mono",
                    "-t", f"{duration:.3f}",
                    "-c:a", "pcm_s16le",
                    str(target),
                ]
            )
            data = target.read_bytes()
            # Trust the file, not the arithmetic: the timeline is built from
            # this number and a drift of even a few frames desynchronises
            # captions from visuals for the whole video.
            probed = ffmpeg.probe_audio(target)

        ref = await self.storage.put(  # type: ignore[attr-defined]
            key=(
                f"projects/{request.project_id or 'anonymous'}"
                f"/narration/{request.cache_key()[:32]}.wav"
            ),
            data=data,
            content_type="audio/wav",
            retention=RetentionClass.PROJECT,
        )

        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model="silence-1.0",
            outputs=[ref],
            cost_usd=0.0,
            latency_ms=0,
            structured_output={
                "duration_seconds": round(probed.duration_seconds or duration, 3),
                "has_speech": False,
                "note": (
                    "No speech synthesiser is configured. This track is silence "
                    "of the correct length so the video can be assembled; it "
                    "carries no voice."
                ),
            },
        )


__all__ = [
    "MAX_SECONDS",
    "SEGMENT_PAUSE",
    "SPEAKING_WPM",
    "SilentNarrationProvider",
    "estimate_duration",
]
