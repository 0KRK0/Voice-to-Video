"""Speech-to-text for development and testing.

STATUS: **DEVELOPMENT PROVIDER — NOT A TRANSCRIBER.**

This adapter does not perform speech recognition and does not claim to. It is
reachable only when a *script* — the text of what was said — is supplied
alongside the audio, and its job is to align that text to the real recording.

What is real here:

* the speech and silence boundaries come from ffmpeg analysing the actual audio;
* the timings are therefore the speaker's actual pauses;
* word timings are interpolated within each real region.

What is not real: the words themselves come from the supplied script.

Why it exists. Without any reachable speech-to-text provider, the choice is
either to stop the entire project at Stage 2, or to find an honest way to
exercise Stages 3 through 20 against genuine audio timing. This is that way. It
is registered under the name ``scripted-dev`` so that every transcript it
produces is identifiable in the database as having come from it, and the golden
path clearly reports when it was used.

Replace with `HttpSpeechToTextProvider` the moment a credential exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.media import ffmpeg
from vtv.contracts.base import ObjectRef, TimeSpan
from vtv.contracts.errors import ErrorCode, Status, VTVError
from vtv.contracts.generation import (
    GenerationRequest,
    GenerationResult,
    SpeechToTextParams,
)
from vtv.contracts.transcript import TranscriptSegment, TranscriptWord
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth

#: Sentence-ish splitter. Deliberately conservative: it splits on terminal
#: punctuation followed by whitespace and a capital, which keeps "1947." and
#: "Dr. Shockley" intact.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")

ScriptLookup = Callable[[ObjectRef], str | None]


def split_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE.split(text.strip()) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


class ScriptedSpeechToTextProvider:
    """Aligns a supplied script to the real speech regions of real audio."""

    #: Marks every transcript this produced, so a development transcript can
    #: never be mistaken for a real one downstream or in the database.
    NAME = "scripted-dev"

    def __init__(self, storage: object, script_lookup: ScriptLookup) -> None:
        self.storage = storage
        self.script_lookup = script_lookup

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.NAME,
            model="alignment-only",
            unit_cost_usd=0.0,
            unit="minute",
            typical_latency_seconds=1.0,
            supports_word_timestamps=True,
            languages=["en"],
            # A local provider sends nothing anywhere, so it is trivially
            # acceptable for voice — the one honest use of these flags here.
            data_policy=DataPolicy(
                retains_input=False,
                trains_on_input=False,
                retention_days=0,
                region="local",
                dpa_in_place=True,
            ),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def transcribe(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, SpeechToTextParams):
            raise VTVError("speech-to-text request carried the wrong params")

        script = self.script_lookup(params.audio)
        if not script:
            raise VTVError(
                "no script supplied for the scripted development transcriber; "
                "configure a real SpeechToTextProvider to transcribe audio",
                code=ErrorCode.TRANSCRIPTION_FAILED,
                user_message="Transcription is not configured.",
            )

        payload = await self.storage.get(params.audio)  # type: ignore[attr-defined]
        with TemporaryDirectory(prefix="vtv-stt-") as directory:
            scratch = Path(directory) / "audio.bin"
            scratch.write_bytes(payload)
            probed = ffmpeg.probe_audio(scratch)
            regions = ffmpeg.detect_speech_regions(
                scratch, duration=probed.duration_seconds
            )

        segments = self._align(script, regions, probed.duration_seconds)
        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.NAME,
            model="alignment-only",
            structured_output={
                "language": params.language_hint or "en",
                "segments": [s.model_dump(mode="json") for s in segments],
            },
            cost_usd=0.0,
            latency_ms=0,
        )

    # -- alignment --------------------------------------------------------

    def _align(
        self,
        script: str,
        regions: list[ffmpeg.SpeechRegion],
        duration: float,
    ) -> list[TranscriptSegment]:
        """Distribute sentences across speech regions by speaking length.

        Sentences are assigned to regions in proportion to their character
        count, which is a decent proxy for how long they take to say. Where a
        region holds several sentences they share it proportionally; where a
        long sentence spans regions it takes the whole of each.
        """
        sentences = split_sentences(script)
        if not sentences:
            raise VTVError("script is empty", code=ErrorCode.TRANSCRIPTION_FAILED)

        usable = [r for r in regions if r.duration > 0.15] or [
            ffmpeg.SpeechRegion(0.0, duration)
        ]
        total_speech = sum(region.duration for region in usable)
        total_chars = sum(len(sentence) for sentence in sentences)

        # Walk the sentences, consuming speech time proportionally.
        segments: list[TranscriptSegment] = []
        region_index = 0
        cursor = usable[0].start
        for sentence in sentences:
            share = (len(sentence) / total_chars) * total_speech
            start = cursor
            remaining = share
            end = start
            while remaining > 1e-6 and region_index < len(usable):
                region = usable[region_index]
                available = region.end - max(cursor, region.start)
                if available <= 1e-6:
                    region_index += 1
                    if region_index < len(usable):
                        cursor = usable[region_index].start
                    continue
                consumed = min(available, remaining)
                end = max(cursor, region.start) + consumed
                cursor = end
                remaining -= consumed
                if remaining > 1e-6:
                    region_index += 1
                    if region_index < len(usable):
                        cursor = usable[region_index].start
            if end - start < 0.2:
                end = min(duration, start + 0.2)
            segments.append(
                TranscriptSegment(
                    span=TimeSpan.of(round(start, 3), round(min(end, duration), 3)),
                    text=sentence,
                    confidence=None,
                    words=self._words(sentence, start, min(end, duration)),
                )
            )

        return _make_disjoint(segments, duration)

    @staticmethod
    def _words(sentence: str, start: float, end: float) -> list[TranscriptWord]:
        tokens = [t for t in sentence.split() if t]
        if not tokens or end - start < 0.05:
            return []
        weights = [len(token) + 1 for token in tokens]
        total = sum(weights)
        words: list[TranscriptWord] = []
        cursor = start
        for token, weight in zip(tokens, weights, strict=True):
            width = (weight / total) * (end - start)
            stop = min(end, cursor + width)
            if stop - cursor < 0.01:
                stop = min(end, cursor + 0.01)
            if stop <= cursor:
                break
            words.append(
                TranscriptWord(
                    text=token, span=TimeSpan.of(round(cursor, 3), round(stop, 3))
                )
            )
            cursor = stop
        return words


def _make_disjoint(
    segments: list[TranscriptSegment], duration: float
) -> list[TranscriptSegment]:
    """Nudge segments so they are strictly ordered and non-overlapping.

    Float arithmetic over proportional shares produces overlaps of a few
    microseconds. The `Transcript` contract rejects those, correctly — so they
    are removed here rather than by loosening the contract.
    """
    cleaned: list[TranscriptSegment] = []
    cursor = 0.0
    for segment in segments:
        start = max(segment.span.start, cursor)
        end = max(start + 0.05, segment.span.end)
        end = min(end, duration)
        if end <= start:
            continue
        words = [w for w in segment.words if w.span.start >= start and w.span.end <= end]
        cleaned.append(
            segment.model_copy(
                update={"span": TimeSpan.of(start, end), "words": words}
            )
        )
        cursor = end
    return cleaned


__all__ = ["ScriptedSpeechToTextProvider", "split_sentences"]
