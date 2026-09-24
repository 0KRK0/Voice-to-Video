"""Stage 21/22 — giving a voiceless input a voice track.

A recording arrives with narration already attached; that is the easy case and
the one the pipeline was built around. Every other input — a PDF, a deck, a
paragraph — arrives as words with no sound, and the timeline has no clock
without one (Rule 5).

This service closes that gap through the same router as everything else, so
speech synthesis is cached, costed, budgeted and subject to the same fallback
ladder as image generation. Two rungs today:

1. a configured synthesiser, which produces real narration in the target
   language;
2. correctly-timed silence, which produces a watchable video with captions and
   visuals and no voice.

Rung 2 is a degradation, is recorded as one, and is reported to the user. It is
not a success wearing a success's clothes.

**Re-timing.** Synthetic transcript timings are an estimate; real synthesised
audio is a fact. When the two disagree the transcript is scaled onto the audio,
because the audio is what the viewer hears and the captions must match it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vtv.contracts.base import Budget, ObjectRef, TimeSpan
from vtv.contracts.errors import (
    DegradationReason,
    DegradationStep,
    ProviderError,
    Status,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    SpeechParams,
)
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.generation import GenerationRouter

#: A synthesiser whose output differs from the estimate by less than this is
#: close enough that re-timing would move captions for no reason.
RETIME_TOLERANCE = 0.05


@dataclass
class SynthesisedNarration:
    """An audio track plus the transcript that now matches it."""

    audio: ObjectRef
    duration_seconds: float
    transcript: Transcript
    provider: str
    #: False when the track is silence. Carried all the way to the API so the
    #: user is told, not left to wonder why the video is mute.
    has_speech: bool = True
    cost_usd: float = 0.0
    degradations: list[DegradationStep] = field(default_factory=list)


@dataclass
class NarrationService:
    """Transcript in, audio track out, through the router."""

    router: GenerationRouter
    events: EventSink
    #: Optional last-resort provider used directly when the router has no
    #: registered synthesiser at all. Kept separate from the router so that a
    #: deployment which genuinely wants "fail rather than ship a mute video"
    #: can simply not supply one.
    silent_fallback: object | None = None
    voice: str | None = None
    speaking_rate: float = 1.0
    max_cost_usd: float | None = 2.0

    async def synthesise(
        self, transcript: Transcript, *, project_id: str | None = None
    ) -> SynthesisedNarration:
        texts = [segment.text for segment in transcript.segments if segment.text.strip()]
        if not texts:
            raise ProviderError("there is nothing to narrate")

        params = SpeechParams(
            text="\n".join(texts)[:20000],
            language=transcript.language,
            voice=self.voice,
            speaking_rate=self.speaking_rate,
            segment_texts=texts[:2000],
        )
        request = GenerationRequest(
            project_id=project_id or transcript.project_id,
            kind=GenerationKind.SPEECH,
            params=params,
            budget=Budget(max_cost_usd=self.max_cost_usd),
        )

        degradations: list[DegradationStep] = []
        result = None
        if self.router.providers_for(GenerationKind.SPEECH):
            try:
                result = await self.router.generate(request)
            except VTVError as error:
                degradations.append(
                    DegradationStep(
                        from_strategy="synthesised_speech",
                        to_strategy="silent_narration",
                        reason=DegradationReason.PROVIDER_FAILED,
                        error=error.info,
                    )
                )
                result = None
        else:
            degradations.append(
                DegradationStep(
                    from_strategy="synthesised_speech",
                    to_strategy="silent_narration",
                    reason=DegradationReason.PROVIDER_FAILED,
                )
            )

        if result is None or result.status is not Status.READY or not result.outputs:
            if self.silent_fallback is None:
                raise ProviderError(
                    "no speech synthesiser is available and no silent fallback "
                    "is configured; this input cannot be given a narration track"
                )
            result = await self.silent_fallback.synthesize(request)  # type: ignore[attr-defined]

        output = result.outputs[0]
        details = result.structured_output or {}
        duration = float(details.get("duration_seconds") or 0.0)
        if duration <= 0:
            raise ProviderError("narration was produced without a usable duration")
        has_speech = bool(details.get("has_speech", True))

        retimed = _retime(transcript, duration)

        self.events.emit(
            EventName.NARRATION_SYNTHESISED,
            project_id=project_id or transcript.project_id,
            data={
                "provider": result.provider,
                "has_speech": has_speech,
                "duration_seconds": round(duration, 3),
                "cost_usd": result.cost_usd,
                "degraded": bool(degradations),
            },
        )

        return SynthesisedNarration(
            audio=output,
            duration_seconds=duration,
            transcript=retimed,
            provider=result.provider or "unknown",
            has_speech=has_speech,
            cost_usd=result.cost_usd,
            degradations=degradations,
        )


def _retime(transcript: Transcript, duration: float) -> Transcript:
    """Scale a transcript's estimated timings onto the audio that exists.

    A uniform scale is the only defensible transform without word timings: it
    preserves the relative length of every segment and the order of everything,
    and it guarantees the last caption ends when the audio does. When a provider
    returns real word timings the transcript is rebuilt from those instead, and
    this function is not used.
    """
    span = transcript.span
    estimated = span.end if span else 0.0
    if estimated <= 0 or abs(estimated - duration) <= RETIME_TOLERANCE:
        return transcript

    factor = duration / estimated
    segments: list[TranscriptSegment] = []
    cursor = 0.0
    for segment in transcript.segments:
        start = max(cursor, round(segment.span.start * factor, 3))
        end = round(segment.span.end * factor, 3)
        if end <= start:
            end = round(start + 0.05, 3)
        segments.append(
            segment.model_copy(
                update={
                    "span": TimeSpan.of(start, min(end, duration)),
                    # Word timings scaled from an estimate would be fiction
                    # presented at word granularity, which is worse than none.
                    "words": [],
                }
            )
        )
        cursor = segments[-1].span.end

    return transcript.model_copy(
        update={"segments": segments, "model": f"{transcript.model or 'unknown'}+retimed"}
    )


__all__ = ["RETIME_TOLERANCE", "NarrationService", "SynthesisedNarration"]
