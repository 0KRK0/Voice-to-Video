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
    ValidationFailed,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    SpeechParams,
)
from vtv.contracts.scale import (
    MAX_SCRIPT_CHARS,
    MAX_SEGMENT_CHARS,
    MAX_SPEECH_SEGMENTS,
    describe,
)
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.generation import GenerationRouter

#: A synthesiser whose output differs from the estimate by less than this is
#: close enough that re-timing would move captions for no reason.
RETIME_TOLERANCE = 0.05

#: What a whole narration can carry. Derived — see `contracts/scale.py`.
#:
#: This used to be a flat 20 000 characters, described as "what one synthesis
#: request can carry", and it was **obsolete**: since the caption-drift fix the
#: adapter synthesises one sentence per request, so no single request is
#: anywhere near a vendor limit. The cap was enforcing a constraint that no
#: longer existed, and because it was the smallest number in the system it was
#: the one every long script hit — a thirty-minute narration was refused with
#: "Something in that request did not look right".
MAX_SPEECH_CHARS = MAX_SCRIPT_CHARS


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
        self,
        transcript: Transcript,
        *,
        organisation_id: str | None = None,
        project_id: str | None = None,
    ) -> SynthesisedNarration:
        texts = [segment.text for segment in transcript.segments if segment.text.strip()]
        if not texts:
            raise ProviderError("there is nothing to narrate")

        joined = "\n".join(texts)
        # Refuse rather than truncate. The previous version sliced to the
        # contract's 20 000-character limit and to 2 000 segments, which meant a
        # long script came back as a video that simply stopped talking part way
        # through, with no error, no event and nothing in the response to say so
        # — a silent content cut, which is the one thing the product promises
        # never to do.
        if len(joined) > MAX_SPEECH_CHARS or len(texts) > MAX_SPEECH_SEGMENTS:
            raise ValidationFailed(
                f"this narration is {len(joined):,} characters over "
                f"{len(texts):,} segments; the ceiling is "
                f"{MAX_SPEECH_CHARS:,} characters and "
                f"{MAX_SPEECH_SEGMENTS:,} segments",
                user_message=(
                    f"This script is {describe(len(joined))} of narration, and "
                    f"a single project can hold {describe(MAX_SPEECH_CHARS)}. "
                    "Split it into parts."
                ),
            )

        # The one real vendor limit, and it is per *sentence* rather than per
        # script: each of these is its own request. A line over the limit is a
        # fixable problem and naming it is the difference between "split this
        # sentence" and "your script is too long" — so it is reported with the
        # line, not as a fact about the whole project.
        for index, piece in enumerate(texts):
            if len(piece) > MAX_SEGMENT_CHARS:
                raise ValidationFailed(
                    f"segment {index} is {len(piece):,} characters; a single "
                    f"line may be at most {MAX_SEGMENT_CHARS:,}",
                    user_message=(
                        f"One line of this script is too long to narrate in "
                        f"one piece ({len(piece):,} characters, the limit is "
                        f"{MAX_SEGMENT_CHARS:,}). It starts: "
                        f"“{piece[:60]}…”. Break it into shorter sentences."
                    ),
                )

        params = SpeechParams(
            text=joined,
            language=transcript.language,
            voice=self.voice,
            speaking_rate=self.speaking_rate,
            segment_texts=texts,
        )
        request = GenerationRequest(
            organisation_id=organisation_id or transcript.organisation_id,
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

        retimed = _measured(transcript, details.get("segments")) or _retime(
            transcript, duration
        )

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


def transcript_from_script(
    script: object,
    *,
    organisation_id: str,
    project_id: str,
) -> Transcript:
    """Build the transcript a `Script` implies, laid contiguously from zero.

    The product lane (paste a script, direct the visuals) never passes through
    transcription: the words are the input, not the output. But the narration
    service — and everything downstream of it — speaks `Transcript`, so this is
    the adapter between the two, and it is a **function of the script alone**.

    Contiguous and zero-based on purpose. `TimelineBuilder` lays the narration
    track end to end from the same per-block durations, so a transcript built
    here and a timeline built there describe the same clock. If this ever laid
    segments out differently the two would disagree by a silent, growing offset,
    which is the defect class `flatten` exists to refuse.

    Muted and empty blocks are skipped, exactly as the builder skips them: a
    block the user muted is not narrated, so it occupies no time in the voice.
    """
    from vtv.contracts.base import IdPrefix, new_id

    segments: list[TranscriptSegment] = []
    cursor = 0.0
    for block in getattr(script, "blocks", []):
        if not block.is_narrated:
            continue
        length = max(0.05, float(block.duration_seconds))
        start = round(cursor, 3)
        end = round(cursor + length, 3)
        segments.append(
            TranscriptSegment(span=TimeSpan.of(start, end), text=block.text)
        )
        cursor = end

    return Transcript(
        recording_id=new_id(IdPrefix.RECORDING),
        organisation_id=organisation_id,
        project_id=project_id,
        language=getattr(script, "language", "en") or "en",
        segments=segments,
        provider="script",
        model="written-input",
        status=Status.READY,
    )


def _measured(transcript: Transcript, reported: object) -> Transcript | None:
    """Rebuild a transcript from spans the provider *measured*, if it gave any.

    ## Why this outranks `_retime`

    `_retime` below scales every estimated timing by one factor. That is the
    best transform available when all you know is the total, and it is still
    wrong in a way users see: the estimate is derived from character counts, and
    real speech does not take time in proportion to characters. "1,024" is five
    characters and most of a second; "the" is three characters and almost
    nothing. So a uniform scale lands the *end* of the audio correctly and puts
    every sentence in between slightly off, each one's error pushing into its
    neighbours. Captions and visuals then sit progressively wrong through the
    body of the video — the exact complaint that produced this function.

    A provider that synthesises sentence by sentence knows each sentence's real
    length, because it measured the file. When it reports them, they are facts
    and nothing here should be scaling anything.

    Returns `None` rather than raising when the report is missing, malformed, or
    a different length from the transcript. A provider that split or merged
    segments is not lying — it is answering a different question — and attaching
    one sentence's timing to another sentence's text would be worse than falling
    back to the scale.
    """
    if not isinstance(reported, list) or len(reported) != len(transcript.segments):
        return None

    segments: list[TranscriptSegment] = []
    cursor = 0.0
    for segment, item in zip(transcript.segments, reported, strict=True):
        if not isinstance(item, dict):
            return None
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            return None
        if end <= start or start + 1e-6 < cursor:
            # Overlapping or reversed spans are not a timing we can use, and
            # silently repairing them would hide a provider defect behind a
            # video that is subtly wrong.
            return None
        segments.append(
            segment.model_copy(
                update={
                    "span": TimeSpan.of(round(start, 3), round(end, 3)),
                    # Word timings were never measured here, only sentence
                    # ones. Inventing them from a sentence span would be
                    # fiction at word granularity.
                    "words": [],
                }
            )
        )
        cursor = end

    return transcript.model_copy(
        update={
            "segments": segments,
            "model": f"{transcript.model or 'unknown'}+measured",
        }
    )


def _retime(transcript: Transcript, duration: float) -> Transcript:
    """Scale a transcript's estimated timings onto the audio that exists.

    The fallback for a provider that reports only a total. See `_measured`
    above for why a uniform scale cannot be right in the middle of a video, and
    why it is nonetheless the correct thing to do when the total is all anyone
    knows: it preserves the relative length of every segment and the order of
    everything, and it guarantees the last caption ends when the audio does.
    When a provider returns real word timings the transcript is rebuilt from
    those instead, and this function is not used.
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


__all__ = [
    "RETIME_TOLERANCE",
    "NarrationService",
    "SynthesisedNarration",
    "transcript_from_script",
]
