"""Stages 1 and 2 — capture and speech intelligence."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.media import ffmpeg
from vtv.adapters.speech.scripted import ScriptedSpeechToTextProvider, split_sentences
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import TimeSpan
from vtv.contracts.errors import ErrorCode, Status, VTVError
from vtv.contracts.generation import GenerationKind, GenerationRequest, SpeechToTextParams
from vtv.contracts.recording import AudioFormat
from vtv.contracts.transcript import TranscriptSegment, TranscriptWord
from vtv.observability.events import EventSink
from vtv.pipeline.capture import CaptureService, sniff_format
from vtv.pipeline.transcription import TranscriptionService

SCRIPT = (
    "The transistor was invented in 1947 at Bell Labs. "
    "It was smaller and far more efficient than the vacuum tubes that came before it. "
    "Within twenty years transistors had replaced vacuum tubes almost everywhere."
)


def run(coro):
    return asyncio.run(coro)


class FormatSniffing(unittest.TestCase):
    """The declared content type is a hint. The bytes are the answer."""

    def test_recognises_real_containers(self) -> None:
        cases = {
            b"\x1a\x45\xdf\xa3rest": AudioFormat.WEBM_OPUS,
            b"OggS....": AudioFormat.OGG_OPUS,
            b"RIFF....WAVE": AudioFormat.WAV_PCM,
            b"fLaC....": AudioFormat.FLAC,
            b"....ftypM4A ": AudioFormat.MP4_AAC,
        }
        for data, expected in cases.items():
            self.assertIs(sniff_format(data), expected)

    def test_rejects_anything_it_does_not_recognise(self) -> None:
        # A .webm filename on a zip file must not become a recording.
        with self.assertRaises(VTVError) as caught:
            sniff_format(b"PK\x03\x04not audio at all")
        self.assertIs(caught.exception.info.code, ErrorCode.AUDIO_UNREADABLE)


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class CaptureMeasuresRatherThanTrusts(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-test-capture-")
        self.root = Path(self._dir.name)
        self.storage = LocalStorageProvider(self.root / "storage")
        self.events = EventSink()
        self.events.record = True
        self.service = CaptureService(storage=self.storage, events=self.events)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _audio(self, name: str, duration: float, segments: list[tuple[float, float]]) -> bytes:
        path = self.root / name
        ffmpeg.synthesise_tone_audio(path, duration=duration, segments=segments)
        return path.read_bytes()

    def test_duration_comes_from_probing_not_from_the_client(self) -> None:
        data = self._audio("a.wav", 8.0, [(0.5, 3.0), (4.0, 7.5)])
        recording = run(self.service.capture(project_id="prj_" + "a" * 24, data=data))
        self.assertIs(recording.status, Status.READY)
        assert recording.properties is not None
        self.assertAlmostEqual(recording.properties.duration_seconds, 8.0, places=1)
        self.assertEqual(recording.properties.channels, 1)

    def test_an_empty_upload_is_refused(self) -> None:
        with self.assertRaises(VTVError):
            run(self.service.capture(project_id="prj_" + "a" * 24, data=b""))

    def test_an_oversized_upload_is_refused_before_it_is_stored(self) -> None:
        service = CaptureService(storage=self.storage, events=self.events, max_bytes=32)
        with self.assertRaises(VTVError) as caught:
            run(service.capture(project_id="prj_" + "a" * 24, data=b"x" * 64))
        self.assertIs(caught.exception.info.code, ErrorCode.AUDIO_TOO_LONG)

    def test_an_over_long_recording_fails_with_a_reason(self) -> None:
        service = CaptureService(storage=self.storage, events=self.events, max_seconds=4.0)
        data = self._audio("long.wav", 9.0, [(0.5, 8.5)])
        recording = run(service.capture(project_id="prj_" + "a" * 24, data=data))
        self.assertIs(recording.status, Status.FAILED)
        assert recording.error is not None
        self.assertIs(recording.error.code, ErrorCode.AUDIO_TOO_LONG)
        # The user gets a sentence, not a stack trace.
        self.assertIsNotNone(recording.error.user_message)

    def test_a_silent_recording_is_caught_before_transcription(self) -> None:
        # A muted microphone is the most common capture failure, and the one
        # most worth catching before spending money on a transcription bill.
        path = self.root / "silent.wav"
        ffmpeg.run(
            [
                ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", "6", str(path),
            ]
        )
        recording = run(
            self.service.capture(project_id="prj_" + "a" * 24, data=path.read_bytes())
        )
        self.assertIs(recording.status, Status.FAILED)
        assert recording.error is not None
        self.assertIs(recording.error.code, ErrorCode.AUDIO_SILENT)

    def test_object_keys_are_generated_never_taken_from_the_client(self) -> None:
        data = self._audio("b.wav", 6.0, [(0.5, 5.5)])
        recording = run(self.service.capture(project_id="prj_" + "b" * 24, data=data))
        self.assertIn(recording.recording_id, recording.audio.key)
        self.assertTrue(recording.audio.key.startswith("projects/prj_"))


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class SilenceDetectionFindsRealBoundaries(unittest.TestCase):
    def test_detected_regions_match_the_synthesised_speech(self) -> None:
        with TemporaryDirectory(prefix="vtv-test-silence-") as directory:
            path = Path(directory) / "a.wav"
            ffmpeg.synthesise_tone_audio(
                path, duration=12.0, segments=[(0.4, 3.6), (4.4, 7.9), (8.8, 11.6)]
            )
            regions = ffmpeg.detect_speech_regions(path, duration=12.0)
        self.assertEqual(len(regions), 3)
        self.assertAlmostEqual(regions[1].start, 4.4, delta=0.35)
        self.assertAlmostEqual(regions[1].end, 7.9, delta=0.35)


class SentenceSplitting(unittest.TestCase):
    def test_splits_on_terminal_punctuation(self) -> None:
        self.assertEqual(len(split_sentences(SCRIPT)), 3)

    def test_keeps_decimals_and_abbreviations_intact(self) -> None:
        # A split after "1947." or "Dr." would put half a clause in its own cue.
        self.assertEqual(len(split_sentences("It cost 3.5 million dollars in total.")), 1)


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class ScriptedTranscription(unittest.TestCase):
    """The development transcriber: real timings, supplied words."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-test-stt-")
        self.root = Path(self._dir.name)
        self.storage = LocalStorageProvider(self.root / "storage")
        self.events = EventSink()
        path = self.root / "a.wav"
        ffmpeg.synthesise_tone_audio(
            path, duration=20.0, segments=[(0.3, 6.0), (6.8, 13.0), (13.8, 19.5)]
        )
        self.recording = run(
            CaptureService(storage=self.storage, events=self.events).capture(
                project_id="prj_" + "c" * 24, data=path.read_bytes()
            )
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _provider(self, script: str | None) -> ScriptedSpeechToTextProvider:
        return ScriptedSpeechToTextProvider(self.storage, lambda ref: script)

    def test_it_refuses_to_invent_words(self) -> None:
        # The single most important property of this provider: with no script
        # it fails loudly rather than producing plausible nonsense.
        provider = self._provider(None)
        request = GenerationRequest(
            kind=GenerationKind.SPEECH_TO_TEXT,
            params=SpeechToTextParams(audio=self.recording.audio),
        )
        with self.assertRaises(VTVError) as caught:
            run(provider.transcribe(request))
        self.assertIs(caught.exception.info.code, ErrorCode.TRANSCRIPTION_FAILED)

    def test_it_marks_itself_as_a_development_provider(self) -> None:
        transcript = run(
            TranscriptionService(
                provider=self._provider(SCRIPT), events=self.events
            ).transcribe(self.recording)
        )
        self.assertEqual(transcript.provider, "scripted-dev")

    def test_timings_stay_inside_the_recording_and_never_overlap(self) -> None:
        transcript = run(
            TranscriptionService(
                provider=self._provider(SCRIPT), events=self.events
            ).transcribe(self.recording)
        )
        self.assertGreaterEqual(len(transcript.segments), 3)
        assert transcript.span is not None
        self.assertLessEqual(transcript.span.end, 20.05)
        for previous, following in zip(
            transcript.segments, transcript.segments[1:], strict=False
        ):
            self.assertLessEqual(previous.span.end, following.span.start + 1e-6)

    def test_word_timings_are_produced_and_contained(self) -> None:
        transcript = run(
            TranscriptionService(
                provider=self._provider(SCRIPT), events=self.events
            ).transcribe(self.recording)
        )
        with_words = [s for s in transcript.segments if s.words]
        self.assertTrue(with_words)
        for segment in with_words:
            for word in segment.words:
                self.assertTrue(segment.span.contains(word.span))


class TranscriptNormalisation(unittest.TestCase):
    """Providers return nearly-valid data. This is where it becomes valid."""

    def setUp(self) -> None:
        self.service = TranscriptionService(provider=object(), events=EventSink())

    def test_overlapping_segments_are_separated(self) -> None:
        segments = [
            TranscriptSegment(span=TimeSpan.of(0, 5), text="one"),
            TranscriptSegment(span=TimeSpan.of(4.5, 9), text="two"),
        ]
        cleaned = self.service._clamp(segments, 10.0)
        self.assertLessEqual(cleaned[0].span.end, cleaned[1].span.start + 1e-9)

    def test_segments_past_the_end_are_trimmed(self) -> None:
        segments = [TranscriptSegment(span=TimeSpan.of(0, 12), text="over")]
        cleaned = self.service._clamp(segments, 10.0)
        self.assertAlmostEqual(cleaned[0].span.end, 10.0)

    def test_a_long_segment_splits_on_its_own_word_timings(self) -> None:
        words = [
            TranscriptWord(text=f"w{i}", span=TimeSpan.of(i * 1.0, i * 1.0 + 0.9))
            for i in range(30)
        ]
        segment = TranscriptSegment(
            span=TimeSpan.of(0, 30), text=" ".join(w.text for w in words), words=words
        )
        pieces = self.service._split_long(segment)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(p.span.duration <= 20 for p in pieces))

    def test_a_long_segment_without_word_timings_is_left_alone(self) -> None:
        # Splitting it would invent a boundary the speaker did not make.
        segment = TranscriptSegment(span=TimeSpan.of(0, 40), text="x " * 200)
        self.assertEqual(len(self.service._split_long(segment)), 1)


if __name__ == "__main__":
    unittest.main()
