"""Stage 2 — Speech intelligence.

Audio to a timestamped `Transcript`. The provider does the recognition; this
module owns everything that must be true regardless of which provider ran:
segments in order, no overlaps, timings inside the recording, and a record of
who produced it.

The transcript is the source representation. Nothing downstream may edit it —
they produce derived documents that point back into it (Rule 12).
"""

from __future__ import annotations

from dataclasses import dataclass

from vtv.contracts.base import TimeSpan
from vtv.contracts.errors import ErrorCode, Status, ValidationFailed, VTVError
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    SpeechToTextParams,
)
from vtv.contracts.recording import Recording
from vtv.contracts.transcript import Transcript, TranscriptSegment, TranscriptWord
from vtv.observability.events import EventName, EventSink, Timer

#: Segments longer than this are hard to work with downstream: they blur scene
#: boundaries and make caption cues unreadable. Providers occasionally emit them
#: for uninterrupted speech, so they are split on internal word timings.
MAX_SEGMENT_SECONDS = 18.0


@dataclass
class TranscriptionService:
    """Runs speech-to-text and validates the result into a `Transcript`."""

    provider: object  # SpeechToTextProvider
    events: EventSink

    async def transcribe(
        self,
        recording: Recording,
        *,
        vocabulary: list[str] | None = None,
    ) -> Transcript:
        if recording.status is not Status.READY or recording.properties is None:
            raise VTVError(
                "cannot transcribe a recording that has not been probed",
                code=ErrorCode.TRANSCRIPTION_FAILED,
            )

        duration = recording.properties.duration_seconds
        self.events.emit(
            EventName.TRANSCRIPTION_STARTED,
            project_id=recording.project_id,
            data={"recording_id": recording.recording_id, "duration_seconds": duration},
        )
        timer = Timer()

        request = GenerationRequest(
            project_id=recording.project_id,
            kind=GenerationKind.SPEECH_TO_TEXT,
            params=SpeechToTextParams(
                audio=recording.audio,
                language_hint=recording.declared_language,
                word_timestamps=True,
                vocabulary=vocabulary or [],
            ),
        )
        result = await self.provider.transcribe(request)  # type: ignore[attr-defined]

        if result.status is not Status.READY or not result.structured_output:
            raise VTVError(
                "transcription produced no output",
                code=ErrorCode.TRANSCRIPTION_FAILED,
                user_message="We could not understand that recording.",
            )

        segments = self._segments_from(result.structured_output, duration)
        transcript = Transcript(
            project_id=recording.project_id,
            recording_id=recording.recording_id,
            language=str(result.structured_output.get("language") or "en"),
            segments=segments,
            provider=result.provider,
            model=result.model,
            status=Status.READY,
        )

        self.events.emit(
            EventName.TRANSCRIPTION_COMPLETED,
            project_id=recording.project_id,
            duration_ms=timer.elapsed_ms,
            cost_usd=result.cost_usd,
            data={
                "transcript_id": transcript.transcript_id,
                "provider": result.provider,
                "segments": len(segments),
                "words": sum(len(s.words) for s in segments),
                # Surfaced deliberately: a development transcriber must be
                # visible in the event stream, not just in a code comment.
                "is_development_provider": result.provider == "scripted-dev",
            },
        )
        return transcript

    # -- normalisation ----------------------------------------------------

    def _segments_from(
        self, payload: dict[str, object], duration: float
    ) -> list[TranscriptSegment]:
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValidationFailed("transcription returned no segments")

        parsed: list[TranscriptSegment] = []
        for raw in raw_segments:
            segment = (
                raw
                if isinstance(raw, TranscriptSegment)
                else TranscriptSegment.model_validate(raw)
            )
            parsed.append(segment)

        clamped = self._clamp(parsed, duration)
        split = [piece for segment in clamped for piece in self._split_long(segment)]
        return split

    @staticmethod
    def _clamp(
        segments: list[TranscriptSegment], duration: float
    ) -> list[TranscriptSegment]:
        """Force ordering, disjointness and containment within the recording.

        Providers occasionally return a segment ending a few milliseconds after
        the file does, or two segments that touch. Both are harmless mistakes
        and both would fail our contract, so they are corrected here rather than
        by relaxing the contract.
        """
        cleaned: list[TranscriptSegment] = []
        cursor = 0.0
        for segment in sorted(segments, key=lambda s: s.span.start):
            start = max(segment.span.start, cursor)
            end = min(segment.span.end, duration)
            if end - start < 0.05:
                continue
            words = [
                word
                for word in segment.words
                if word.span.start >= start - 1e-6 and word.span.end <= end + 1e-6
            ]
            cleaned.append(
                segment.model_copy(
                    update={"span": TimeSpan.of(start, end), "words": words}
                )
            )
            cursor = end
        if not cleaned:
            raise ValidationFailed("no transcript segment survived normalisation")
        return cleaned

    @staticmethod
    def _split_long(segment: TranscriptSegment) -> list[TranscriptSegment]:
        """Break an over-long segment on its own word timings.

        Only word-level timings can tell us where a real pause is, so a segment
        without them is left alone: an arbitrary split would invent a boundary
        the speaker did not make.
        """
        if segment.span.duration <= MAX_SEGMENT_SECONDS or len(segment.words) < 4:
            return [segment]

        pieces: list[TranscriptSegment] = []
        current: list[TranscriptWord] = []
        start = segment.span.start
        for word in segment.words:
            current.append(word)
            if word.span.end - start >= MAX_SEGMENT_SECONDS * 0.75:
                pieces.append(
                    TranscriptSegment(
                        span=TimeSpan.of(start, word.span.end),
                        text=" ".join(w.text for w in current),
                        confidence=segment.confidence,
                        speaker=segment.speaker,
                        words=list(current),
                    )
                )
                start = word.span.end
                current = []
        if current:
            pieces.append(
                TranscriptSegment(
                    span=TimeSpan.of(start, segment.span.end),
                    text=" ".join(w.text for w in current),
                    confidence=segment.confidence,
                    speaker=segment.speaker,
                    words=list(current),
                )
            )
        return pieces or [segment]


__all__ = ["MAX_SEGMENT_SECONDS", "TranscriptionService"]
