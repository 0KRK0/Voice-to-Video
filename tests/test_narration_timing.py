"""Where each sentence starts in the voice track — measured, not apportioned.

## The defect these tests describe

The user's report was "voice and caption are off the mark, they don't sync
perfectly, at least there is a 5 sec gap when we go to the end, visual by visual
the gap is increasing".

The cause was structural. `HttpSpeechSynthesisProvider` sent the entire script
as one request and measured one total duration. `NarrationService` then scaled
the *estimated* per-sentence timings by a single factor to fit that total.

A uniform scale can only be right if every sentence's estimate is wrong by the
same proportion, and estimates here come from character counts. Speech does not
work that way: numerals, acronyms and long technical words take far more time
per character than ordinary prose. So each sentence's individual error is
absorbed by its neighbours, and the pictures and captions sit progressively
wrong through the body of the video.

The server below models exactly that mismatch — it returns audio whose length
depends on **words**, while the estimator predicts from **characters** — and the
tests assert that the timings the system ends up with come from the audio.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from vtv.adapters.media import ffmpeg
from vtv.adapters.speech.http_tts import HttpSpeechSynthesisProvider
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import Budget, TimeSpan
from vtv.contracts.errors import Status
from vtv.contracts.generation import GenerationKind, GenerationRequest, SpeechParams
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.observability.events import EventSink
from vtv.pipeline.generation import GenerationRouter
from vtv.pipeline.narration import NarrationService

ORG = "org_0000000000000000000000"
PROJECT = "prj_0000000000000000000001"

#: Sentences chosen so that characters and words disagree sharply. The first is
#: long in characters and short in words; the last is the reverse.
SENTENCES = [
    "Asynchronous instrumentation reconfigures interdependencies.",
    "It may be a bit of a big deal for a lot of us in the end.",
    "In 1024 BC, 3,072 of them went to 65,536.",
    "So we go on.",
    "Every one of them had to be told what to do and when to do it, again.",
]


def audio_for(text: str) -> bytes:
    """Speech whose length is a function of *words*, not characters.

    0.34 s a word plus 0.4 s of breath, which is roughly how a real synthesiser
    behaves and is deliberately not what a character-count estimator predicts.
    """
    seconds = 0.4 + 0.34 * max(1, len(text.split()))
    with tempfile.TemporaryDirectory(prefix="vtv-voice-") as scratch:
        target = Path(scratch) / "voice.mp3"
        subprocess.run(
            [
                ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", f"{seconds:.3f}", "-c:a", "libmp3lame", "-b:a", "64k",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
        return target.read_bytes()


class WordRateVoice(BaseHTTPRequestHandler):
    """An OpenAI-compatible speech endpoint that speaks at a word rate."""

    inputs: list[str] = []

    def log_message(self, *_args: object) -> None:
        """Quiet."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        text = str(payload.get("input", ""))
        type(self).inputs.append(text)
        body = audio_for(text)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def transcript_of(sentences: list[str]) -> Transcript:
    """The estimate the product lane starts from: characters over a reading rate.

    Exactly the shape `transcript_from_script` produces — contiguous, from zero,
    and wrong in the specific way this whole file is about.
    """
    segments: list[TranscriptSegment] = []
    cursor = 0.0
    for text in sentences:
        length = max(0.9, len(text) / 16.0)
        segments.append(
            TranscriptSegment(span=TimeSpan.of(cursor, cursor + length), text=text)
        )
        cursor += length
    return Transcript(
        recording_id="rec_0000000000000000000002",
        organisation_id=ORG,
        project_id=PROJECT,
        language="en",
        segments=segments,
        provider="script",
        model="written-input",
        status=Status.READY,
    )


class VoiceTimingIsMeasured(unittest.TestCase):
    server: HTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), WordRateVoice)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        WordRateVoice.inputs = []
        self._dir = tempfile.TemporaryDirectory(prefix="vtv-store-")
        self.storage = LocalStorageProvider(
            root=Path(self._dir.name), signing_key="test-key"
        )
        host, port = self.server.server_address[:2]
        self.provider = HttpSpeechSynthesisProvider(
            storage=self.storage,
            endpoint=f"http://{host}:{port}/v1/audio/speech",
            api_key="test-key",
            model="tts-1",
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def run_async(self, coro):  # type: ignore[no-untyped-def]
        return asyncio.run(coro)

    def true_lengths(self) -> list[float]:
        return [0.4 + 0.34 * max(1, len(s.split())) for s in SENTENCES]

    # -- the adapter ------------------------------------------------------

    def test_each_sentence_is_synthesised_and_measured_separately(self) -> None:
        result = self.run_async(
            self.provider.synthesize(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.SPEECH,
                    params=SpeechParams(
                        text="\n".join(SENTENCES),
                        language="en",
                        segment_texts=SENTENCES,
                    ),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        self.assertEqual(sorted(WordRateVoice.inputs), sorted(SENTENCES))

        spans = result.structured_output["segments"]
        self.assertEqual(len(spans), len(SENTENCES))
        for span, expected in zip(spans, self.true_lengths(), strict=True):
            self.assertAlmostEqual(span["end"] - span["start"], expected, delta=0.06)

    def test_the_joined_file_has_no_seam_between_sentences(self) -> None:
        """The spans must describe the file that exists, to the last frame.

        If the join inserted padding — which a container-level MP3 concat does —
        the reported spans would drift from the audio by more with every
        sentence, which is the same defect in a new place.
        """
        result = self.run_async(
            self.provider.synthesize(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.SPEECH,
                    params=SpeechParams(
                        text="\n".join(SENTENCES),
                        language="en",
                        segment_texts=SENTENCES,
                    ),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        spans = result.structured_output["segments"]
        total = float(result.structured_output["duration_seconds"])
        self.assertAlmostEqual(spans[-1]["end"], total, delta=0.005)

        data = self.run_async(self.storage.get(result.outputs[0]))
        with tempfile.TemporaryDirectory(prefix="vtv-check-") as scratch:
            path = Path(scratch) / "narration.wav"
            path.write_bytes(data)
            self.assertAlmostEqual(
                ffmpeg.probe_audio(path).duration_seconds, total, delta=0.005
            )

    # -- the service ------------------------------------------------------

    def test_the_transcript_ends_up_on_the_audio_not_on_a_scaled_estimate(self) -> None:
        router = GenerationRouter(events=EventSink())
        router.register(self.provider, GenerationKind.SPEECH)
        service = NarrationService(router=router, events=EventSink())

        spoken = self.run_async(
            service.synthesise(
                transcript_of(SENTENCES), organisation_id=ORG, project_id=PROJECT
            )
        )

        cursor = 0.0
        for segment, length in zip(
            spoken.transcript.segments, self.true_lengths(), strict=True
        ):
            self.assertAlmostEqual(segment.span.start, cursor, delta=0.06)
            cursor += length
        self.assertAlmostEqual(spoken.duration_seconds, cursor, delta=0.06)

    def test_the_uniform_scale_this_replaces_was_off_by_much_more(self) -> None:
        """Not a tautology — it measures the improvement rather than asserting it.

        The same estimate is put through the old transform and the new one, and
        the worst per-sentence start error of each is compared. The old one is
        allowed to be right at the very end of the video; that is exactly its
        property, and exactly why the drift was invisible in the total and
        obvious in the middle.
        """
        from vtv.pipeline.narration import _retime

        estimate = transcript_of(SENTENCES)
        lengths = self.true_lengths()
        total = sum(lengths)

        truth = []
        cursor = 0.0
        for length in lengths:
            truth.append(cursor)
            cursor += length

        scaled = _retime(estimate, total)
        old_error = max(
            abs(segment.span.start - start)
            for segment, start in zip(scaled.segments, truth, strict=True)
        )

        router = GenerationRouter(events=EventSink())
        router.register(self.provider, GenerationKind.SPEECH)
        service = NarrationService(router=router, events=EventSink())
        spoken = self.run_async(
            service.synthesise(estimate, organisation_id=ORG, project_id=PROJECT)
        )
        new_error = max(
            abs(segment.span.start - start)
            for segment, start in zip(spoken.transcript.segments, truth, strict=True)
        )

        self.assertGreater(old_error, 0.5)
        self.assertLess(new_error, 0.1)


class AProviderThatReportsNothingStillWorks(unittest.TestCase):
    """The scale is still the right answer when the total is all anyone knows."""

    def test_a_result_without_segments_falls_back_to_scaling(self) -> None:
        from vtv.pipeline.narration import _measured

        estimate = transcript_of(SENTENCES)
        self.assertIsNone(_measured(estimate, None))
        self.assertIsNone(_measured(estimate, []))
        # A provider that split or merged sentences is answering a different
        # question; attaching its spans positionally would misattribute text.
        self.assertIsNone(_measured(estimate, [{"start": 0.0, "end": 1.0}]))

    def test_overlapping_spans_are_refused_rather_than_repaired(self) -> None:
        from vtv.pipeline.narration import _measured

        estimate = transcript_of(SENTENCES[:2])
        overlapping = [
            {"start": 0.0, "end": 2.0, "text": SENTENCES[0]},
            {"start": 1.0, "end": 3.0, "text": SENTENCES[1]},
        ]
        self.assertIsNone(_measured(estimate, overlapping))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
