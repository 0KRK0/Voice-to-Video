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
from vtv.security.paths import tenant_key

#: Words per minute used to give written text a plausible spoken duration.
#: Matches :data:`vtv.pipeline.ingestion.SPEAKING_WPM` so a document's synthetic
#: transcript and its synthetic audio agree on how long the words take.
SPEAKING_WPM = 145.0

#: Pause between segments, mirroring the ingestion timing model.
SEGMENT_PAUSE = 0.3

#: Never emit an audio file longer than this from a single request.
MAX_SECONDS = 3600.0


def segment_durations(text: str, *, segments: list[str], rate: float = 1.0) -> list[float]:
    """How long each segment occupies, laid contiguously and summing to the total.

    The inter-segment pause is charged to the segment *before* it rather than
    left as a gap. Two reasons, and they point the same way: the narration track
    is one continuous file, so the pause is genuinely inside it; and the timeline
    lays narration end to end, so a reported gap would describe a shape the rest
    of the system refuses to build.
    """
    parts = [part for part in segments if part.strip()] or [text]
    speed = max(0.5, rate)
    lengths = [
        max(0.9, (max(1, len(part.split())) / SPEAKING_WPM) * 60.0) for part in parts
    ]
    out = [
        (length + (SEGMENT_PAUSE if index < len(parts) - 1 else 0.0)) / speed
        for index, length in enumerate(lengths)
    ]
    # `estimate_duration` clamps the total; the pieces must still sum to it or
    # the last caption would end somewhere the audio does not.
    total = sum(out)
    clamped = min(MAX_SECONDS, max(1.0, total))
    if total > 0 and abs(clamped - total) > 1e-9:
        factor = clamped / total
        out = [length * factor for length in out]
    return [round(length, 3) for length in out]


def estimate_duration(text: str, *, segments: list[str], rate: float = 1.0) -> float:
    """How long these words would take to say.

    Uses the segment list when present so the pauses land in the same places the
    transcript put them; falls back to a word count for a single block.
    """
    return round(sum(segment_durations(text, segments=segments, rate=rate)), 3)


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

        pieces = [text for text in params.segment_texts if text.strip()] or [params.text]
        lengths = segment_durations(
            params.text,
            segments=list(params.segment_texts),
            rate=params.speaking_rate,
        )
        duration = round(sum(lengths), 3)

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
            key=tenant_key(
                request.organisation_id,
                "projects",
                request.project_id or "anonymous",
                "narration",
                f"{request.cache_key()[:32]}.wav",
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
                "segments": _spans(pieces, lengths, probed.duration_seconds or duration),
                "has_speech": False,
                "note": (
                    "No speech synthesiser is configured. This track is silence "
                    "of the correct length so the video can be assembled; it "
                    "carries no voice."
                ),
            },
        )


def _spans(
    pieces: list[str], lengths: list[float], measured_total: float
) -> list[dict[str, object]]:
    """Where each segment sits in the file that was just written.

    Scaled onto the measured total, because ffmpeg's `anullsrc` output is
    quantised to whole samples and the requested duration is therefore an
    approximation of the file that exists. Reporting spans that run past the end
    of the audio would be worse than reporting none.
    """
    total = sum(lengths)
    if total <= 0 or len(pieces) != len(lengths):
        return []
    factor = (measured_total / total) if measured_total > 0 else 1.0
    spans: list[dict[str, object]] = []
    cursor = 0.0
    for index, (text, length) in enumerate(zip(pieces, lengths, strict=True)):
        start = round(cursor, 3)
        cursor = (
            round(measured_total, 3)
            if index == len(pieces) - 1
            else round(cursor + length * factor, 3)
        )
        spans.append({"start": start, "end": cursor, "text": text})
    return spans


__all__ = [
    "MAX_SECONDS",
    "SEGMENT_PAUSE",
    "SPEAKING_WPM",
    "SilentNarrationProvider",
    "estimate_duration",
    "segment_durations",
]
