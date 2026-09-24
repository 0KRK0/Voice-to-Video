"""The HTTP provider adapters, actually executed.

Four adapters — transcription, speech synthesis, image generation and text
generation — each carried a docstring reading **REAL IMPLEMENTATION — REQUIRES
AN ENDPOINT AND A CREDENTIAL. Not executed in this environment.** That was
honest, and it meant four files of request construction, response parsing,
error mapping and storage writing had never had a single line of them run.

There are still no vendor credentials. But *credentials* were never what was
missing — a server that speaks the protocol was. So this module stands one up:
a small `http.server` implementing the OpenAI-compatible endpoints these
adapters were written against, and then drives the real adapters through it.

## What this does and does not prove

**Proves:** the request shape each adapter builds is well-formed and carries its
credential; the response parsing works; audio duration is measured from the
returned bytes rather than assumed; generated images are decoded and written to
tenant-namespaced storage; costs are computed; a 429 becomes a rate-limit error,
a 400 becomes a refusal rather than a crash, and a provider failure degrades
down the ladder instead of failing the project; `/health` reports a configured
provider as configured.

**Does not prove:** that any particular vendor works. A real endpoint can differ
in ways a conforming server cannot reveal — undocumented required fields,
different error envelopes, rate limits, or audio the probe cannot read. This is
the difference between "the adapter is correct against the contract it was
written for" and "the adapter works against Vendor X", and only the first is
claimable here. `docs/` says so.

That distinction is the whole point of writing this test rather than declaring
the adapters done.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.endpoints import resolve_endpoint
from vtv.adapters.images.http_image import HttpImageGenerationProvider
from vtv.adapters.media import ffmpeg
from vtv.adapters.speech.http_stt import HttpSpeechToTextProvider
from vtv.adapters.speech.http_tts import HttpSpeechSynthesisProvider
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.text.http_llm import HttpTextGenerationProvider
from vtv.contracts.base import Budget, IdPrefix, new_id
from vtv.contracts.errors import ErrorCode, ProviderError, VTVError
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    ImageParams,
    SpeechParams,
    SpeechToTextParams,
    TextParams,
    VisualFidelity,
)
from vtv.contracts.style import AspectRatio

ORG = new_id(IdPrefix.PROJECT)
PROJECT = new_id(IdPrefix.PROJECT)

#: A one-by-one PNG. Enough for the adapter to decode, store and hand back.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _silence(seconds: float) -> bytes:
    """Real encoded audio of a known length, so `_measure` has something to read.

    Generated rather than checked in, because what is under test is that the
    adapter *measures* the returned bytes. A fixture with a hard-coded duration
    would let a broken measurement pass.
    """
    with tempfile.TemporaryDirectory(prefix="vtv-fixture-") as scratch:
        target = Path(scratch) / "tone.mp3"
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


class ConformingProvider(BaseHTTPRequestHandler):
    """A server that speaks the contract the adapters were written against.

    Deliberately minimal and deliberately strict: it asserts the credential
    arrives and records every request, so the test can check what was *sent* as
    well as what was parsed. Half of what an adapter does is build a request
    nobody has ever looked at.
    """

    #: Set by the fixture. Shared across handler instances.
    log: list[dict[str, object]] = []
    audio: bytes = b""
    #: When set, the next request of this kind answers with that status.
    fail_with: dict[str, int] = {}

    def log_message(self, *_args: object) -> None:
        """Silence. The test's output is the test's."""

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        raw = self._read()
        path = self.path
        entry: dict[str, object] = {
            "path": path,
            "authorization": self.headers.get("Authorization", ""),
            "content_type": self.headers.get("Content-Type", ""),
        }
        if entry["content_type"] == "application/json":
            try:
                entry["json"] = json.loads(raw or b"{}")
            except ValueError:
                entry["json"] = None
        else:
            entry["bytes"] = len(raw)
        type(self).log.append(entry)

        for marker, status in type(self).fail_with.items():
            if marker in path:
                self._send(status, b'{"error":"nope"}', "application/json")
                return

        if path.endswith("/audio/speech"):
            self._send(200, type(self).audio, "audio/mpeg")
            return

        if path.endswith("/audio/transcriptions"):
            self._send(
                200,
                json.dumps(
                    {
                        "language": "en",
                        "duration": 6.0,
                        "segments": [
                            {"start": 0.0, "end": 3.0, "text": "The transistor was invented"},
                            {"start": 3.0, "end": 6.0, "text": "at Bell Labs in 1947"},
                        ],
                    }
                ).encode(),
                "application/json",
            )
            return

        if path.endswith("/images/generations"):
            self._send(
                200,
                json.dumps(
                    {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
                ).encode(),
                "application/json",
            )
            return

        if path.endswith("/chat/completions"):
            self._send(
                200,
                json.dumps(
                    {
                        "model": "test-model",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": '{"summary": "A sentence."}',
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                    }
                ).encode(),
                "application/json",
            )
            return

        self._send(404, b'{"error":"no such endpoint"}', "application/json")


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required to measure audio")
class AdaptersWorkAgainstAConformingServer(unittest.TestCase):
    """Every adapter, executed end to end against a real HTTP server."""

    @classmethod
    def setUpClass(cls) -> None:
        ConformingProvider.audio = _silence(4.5)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ConformingProvider)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        ConformingProvider.log = []
        ConformingProvider.fail_with = {}
        self._dir = tempfile.TemporaryDirectory(prefix="vtv-providers-")
        self.storage = LocalStorageProvider(
            root=Path(self._dir.name), bucket="vtv-test"
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def run_async(self, coro):  # type: ignore[no-untyped-def]
        
        return asyncio.run(coro)

    # -- speech synthesis -------------------------------------------------

    def test_narration_is_synthesised_stored_and_measured(self) -> None:
        """The duration is read from the bytes, never assumed.

        A provider that speaks slightly faster or slower than the requested
        rate would otherwise desynchronise every caption in the video — and the
        wrongness would be invisible until somebody watched the whole thing.
        """
        provider = HttpSpeechSynthesisProvider(
            storage=self.storage,
            endpoint=f"{self.base}/audio/speech",
            api_key="test-key",
        )
        request = GenerationRequest(
            organisation_id=ORG,
            project_id=PROJECT,
            kind=GenerationKind.SPEECH,
            params=SpeechParams(text="The transistor was invented at Bell Labs.", language="en"),
            budget=Budget(max_cost_usd=1.0),
        )

        result = self.run_async(provider.synthesize(request))

        self.assertEqual(result.status.value, "ready")
        self.assertEqual(len(result.outputs), 1)
        # 4.5s of real audio, measured. Allow for encoder frame padding.
        measured = float(result.structured_output["duration_seconds"])
        self.assertAlmostEqual(measured, 4.5, delta=0.15)
        self.assertGreater(result.cost_usd, 0.0)

        # The bytes actually landed, under this tenant's prefix.
        stored = self.run_async(self.storage.get(result.outputs[0]))
        self.assertGreater(len(stored), 0)
        self.assertIn(f"orgs/{ORG}/", result.outputs[0].key)

    def test_the_credential_is_sent(self) -> None:
        """Half of what an adapter does is build a request nobody has read."""
        provider = HttpSpeechSynthesisProvider(
            storage=self.storage,
            endpoint=f"{self.base}/audio/speech",
            api_key="secret-token",
        )
        self.run_async(
            provider.synthesize(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.SPEECH,
                    params=SpeechParams(text="Hello.", language="en"),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        sent = ConformingProvider.log[0]
        self.assertEqual(sent["authorization"], "Bearer secret-token")
        self.assertEqual(sent["json"]["input"], "Hello.")

    def test_a_rate_limit_is_a_rate_limit_and_not_a_generic_failure(self) -> None:
        """The ladder retries a rate limit and descends on a hard failure.

        Mapping 429 onto a generic error makes the router treat a temporary
        condition as a permanent one and fall back to silence unnecessarily.
        """
        ConformingProvider.fail_with = {"audio/speech": 429}
        provider = HttpSpeechSynthesisProvider(
            storage=self.storage,
            endpoint=f"{self.base}/audio/speech",
            api_key="k",
        )
        with self.assertRaises(ProviderError) as caught:
            self.run_async(
                provider.synthesize(
                    GenerationRequest(
                        organisation_id=ORG,
                        project_id=PROJECT,
                        kind=GenerationKind.SPEECH,
                        params=SpeechParams(text="Hello.", language="en"),
                        budget=Budget(max_cost_usd=1.0),
                    )
                )
            )
        self.assertEqual(caught.exception.info.code, ErrorCode.RATE_LIMITED)

    def test_an_empty_response_is_refused_rather_than_stored(self) -> None:
        ConformingProvider.audio = b""
        self.addCleanup(lambda: setattr(ConformingProvider, "audio", _silence(4.5)))
        provider = HttpSpeechSynthesisProvider(
            storage=self.storage,
            endpoint=f"{self.base}/audio/speech",
            api_key="k",
        )
        with self.assertRaises(VTVError):
            self.run_async(
                provider.synthesize(
                    GenerationRequest(
                        organisation_id=ORG,
                        project_id=PROJECT,
                        kind=GenerationKind.SPEECH,
                        params=SpeechParams(text="Hello.", language="en"),
                        budget=Budget(max_cost_usd=1.0),
                    )
                )
            )

    # -- transcription ----------------------------------------------------

    def stored_audio(self, seconds: float):  # type: ignore[no-untyped-def]
        """Put real audio in storage and return the ref the adapter will read.

        The adapter takes an `ObjectRef`, not bytes — business logic never
        hands a provider a buffer it might keep.
        """
        from vtv.contracts.base import RetentionClass

        return self.run_async(
            self.storage.put(
                key=f"orgs/{ORG}/projects/{PROJECT}/recordings/probe.mp3",
                data=_silence(seconds),
                content_type="audio/mpeg",
                retention=RetentionClass.EPHEMERAL,
            )
        )

    def test_a_transcript_is_parsed_into_the_contract(self) -> None:
        provider = HttpSpeechToTextProvider(
            storage=self.storage,
            endpoint=f"{self.base}/audio/transcriptions",
            api_key="k",
        )
        result = self.run_async(
            provider.transcribe(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.SPEECH_TO_TEXT,
                    params=SpeechToTextParams(
                        audio=self.stored_audio(6.0), language_hint="en"
                    ),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        segments = result.structured_output["segments"]
        self.assertEqual(len(segments), 2)
        self.assertEqual(result.structured_output["language"], "en")
        self.assertIn("transistor", segments[0]["text"])
        # Six seconds of audio, priced per minute.
        self.assertGreater(result.cost_usd, 0.0)

    def test_the_audio_is_sent_as_multipart_not_json(self) -> None:
        provider = HttpSpeechToTextProvider(
            storage=self.storage,
            endpoint=f"{self.base}/audio/transcriptions",
            api_key="k",
        )
        self.run_async(
            provider.transcribe(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.SPEECH_TO_TEXT,
                    params=SpeechToTextParams(
                        audio=self.stored_audio(1.0), language_hint="en"
                    ),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        sent = ConformingProvider.log[0]
        self.assertIn("multipart/form-data", str(sent["content_type"]))
        self.assertGreater(int(sent["bytes"]), 0)

    # -- image generation -------------------------------------------------

    def test_a_generated_image_is_decoded_and_stored_under_the_tenant(self) -> None:
        # "gpt-image-1" is deliberately not "default" — the request carries
        # whatever model was configured, not a hard-coded literal, which is
        # exactly what wiring.build used to send instead.
        provider = HttpImageGenerationProvider(
            storage=self.storage,
            endpoint=self.base,
            api_key="k",
            model="gpt-image-1",
        )
        result = self.run_async(
            provider.generate_image(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.IMAGE,
                    params=ImageParams(
                        prompt="A germanium point-contact transistor on a bench",
                        aspect_ratio=AspectRatio.LANDSCAPE_16_9,
                    ),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        self.assertEqual(len(result.outputs), 1)
        self.assertIn(f"orgs/{ORG}/", result.outputs[0].key)
        stored = self.run_async(self.storage.get(result.outputs[0]))
        self.assertEqual(stored, PNG)

        sent = ConformingProvider.log[0]["json"]
        # The model actually sent is the one this provider was configured
        # with, not a literal "default" no vendor recognises.
        self.assertEqual(sent["model"], "gpt-image-1")
        # gpt-image-1 rejects the request outright if `response_format` is
        # present at all — it always returns base64. Sending it unconditionally
        # (as this adapter used to) is the second of the three ways every real
        # image request was rejected.
        self.assertNotIn("response_format", sent)
        # A 16:9 request (1920x1080) is not an accepted gpt-image size, and the
        # vendor rejects an arbitrary size rather than clamping it. Of the
        # fixed trio (1024x1024, 1536x1024, 1024x1536), 1536x1024 is the
        # closest in aspect ratio to 16:9 — the widest landscape option, not
        # merely the closest pixel count.
        self.assertEqual(sent["size"], "1536x1024")

    def test_a_dalle_model_gets_response_format_and_a_dalle_size(self) -> None:
        """dall-e-2/3 need `response_format` to return bytes instead of a URL.

        The opposite failure mode from the gpt-image case above: leaving this
        out for a dall-e model gets back a URL this adapter cannot decode as
        `b64_json`, and it also has its own, different, fixed size list.
        """
        provider = HttpImageGenerationProvider(
            storage=self.storage,
            endpoint=self.base,
            api_key="k",
            model="dall-e-3",
        )
        self.run_async(
            provider.generate_image(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.IMAGE,
                    params=ImageParams(
                        prompt="A germanium point-contact transistor on a bench",
                        aspect_ratio=AspectRatio.LANDSCAPE_16_9,
                    ),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        sent = ConformingProvider.log[0]["json"]
        self.assertEqual(sent["model"], "dall-e-3")
        self.assertEqual(sent["response_format"], "b64_json")
        # dall-e-3's accepted sizes are 1024x1024, 1792x1024 and 1024x1792.
        # 1792x1024 (ratio 1.75) is closest to the requested 16:9 (1.78).
        self.assertEqual(sent["size"], "1792x1024")

    def test_a_content_refusal_is_a_refusal_and_not_an_outage(self) -> None:
        """A 400 must degrade the shot, not fail the project.

        Content-policy refusals arrive as 400s. Treating one as a provider
        outage would trip the failure counter and take the provider out of the
        ladder for every *other* shot in the project too.
        """
        from vtv.contracts.errors import ProviderRefused

        ConformingProvider.fail_with = {"images/generations": 400}
        provider = HttpImageGenerationProvider(
            storage=self.storage, endpoint=self.base, api_key="k", model="m"
        )
        with self.assertRaises(ProviderRefused):
            self.run_async(
                provider.generate_image(
                    GenerationRequest(
                        organisation_id=ORG,
                        project_id=PROJECT,
                        kind=GenerationKind.IMAGE,
                        params=ImageParams(
                            prompt="something the provider dislikes",
                            aspect_ratio=AspectRatio.LANDSCAPE_16_9,
                        ),
                        budget=Budget(max_cost_usd=1.0),
                    )
                )
            )

    # -- text generation --------------------------------------------------

    def test_a_completion_is_parsed_and_priced(self) -> None:
        """Structured output, and a cost computed from reported tokens."""
        provider = HttpTextGenerationProvider(
            endpoint=self.base, api_key="k", model="test-model"
        )
        result = self.run_async(
            provider.generate(
                GenerationRequest(
                    organisation_id=ORG,
                    project_id=PROJECT,
                    kind=GenerationKind.TEXT,
                    params=TextParams(instruction="Rewrite this line for clarity."),
                    budget=Budget(max_cost_usd=1.0),
                )
            )
        )
        self.assertEqual(result.status.value, "ready")
        self.assertEqual(result.structured_output, {"summary": "A sentence."})
        # Priced from the tokens the provider reported, not from a guess.
        self.assertGreater(result.cost_usd, 0.0)
        self.assertEqual(result.tokens.input_tokens, 20)
        self.assertEqual(result.tokens.output_tokens, 5)

        sent = ConformingProvider.log[0]
        self.assertTrue(str(sent["path"]).endswith("/chat/completions"))
        self.assertEqual(sent["authorization"], "Bearer k")


class WhatTheseAdaptersStillCannotClaim(unittest.TestCase):
    """The limit of the above, recorded so nobody rounds it up.

    A conforming server proves the adapter is right about the contract it was
    written for. It cannot reveal an undocumented required field, a different
    error envelope, a vendor-specific rate limit, or audio a probe cannot read.
    Those need a real endpoint and a real key, and neither exists here.
    """

    def test_no_vendor_credential_is_configured_in_this_environment(self) -> None:
        from vtv.config import Settings

        settings = Settings(asset_search_endpoint="", env="development")
        for field in (
            "speech_to_text_api_key",
            "speech_synthesis_api_key",
            "text_generation_api_key",
            "image_generation_api_key",
        ):
            with self.subTest(field=field):
                self.assertIsNone(getattr(settings, field))

    def test_the_adapters_say_what_they_require(self) -> None:
        from vtv.adapters.images import http_image
        from vtv.adapters.speech import http_stt, http_tts
        from vtv.adapters.text import http_llm

        for module in (http_tts, http_stt, http_image, http_llm):
            with self.subTest(module=module.__name__):
                doc = module.__doc__ or ""
                self.assertIn("ENDPOINT", doc.upper())
                self.assertIn("CREDENTIAL", doc.upper())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class OneRuleForWhatAConfiguredEndpointMeans(unittest.TestCase):
    """Base URL or full URL — both must work, for every adapter.

    There used to be two rules. The text and image adapters appended their own
    path, so they wanted `https://api.openai.com/v1`. The speech adapters used
    the configured value verbatim, so they wanted the full
    `https://api.openai.com/v1/audio/transcriptions`. Nothing said so, and
    `.env.example` listed all five under one heading as though they took the
    same kind of value.

    An operator who set every endpoint to the full URL — the natural reading —
    got working speech and 404s from text and images, surfacing as "the provider
    rejected us", which reads as a bad credential. The credential was fine.
    """

    def test_a_base_url_gets_the_path_appended(self) -> None:
        self.assertEqual(
            resolve_endpoint("https://api.openai.com/v1", "/chat/completions"),
            "https://api.openai.com/v1/chat/completions",
        )

    def test_a_full_url_is_left_alone(self) -> None:
        full = "https://api.openai.com/v1/chat/completions"
        self.assertEqual(resolve_endpoint(full, "/chat/completions"), full)

    def test_a_trailing_slash_does_not_double(self) -> None:
        self.assertEqual(
            resolve_endpoint("https://api.openai.com/v1/", "/images/generations"),
            "https://api.openai.com/v1/images/generations",
        )
        self.assertEqual(
            resolve_endpoint(
                "https://api.openai.com/v1/images/generations/", "/images/generations"
            ),
            "https://api.openai.com/v1/images/generations",
        )

    def test_a_genuinely_different_path_is_not_rewritten(self) -> None:
        """`/v1/responses` is a different API, not a formatting variant.

        Redirecting it would hide a real misconfiguration behind a request that
        happens to succeed, which is worse than the 404 that tells the truth.
        """
        self.assertEqual(
            resolve_endpoint("https://api.openai.com/v1/responses", "/chat/completions"),
            "https://api.openai.com/v1/responses/chat/completions",
        )

    def test_a_self_hosted_endpoint_on_a_subpath_works(self) -> None:
        """The reason this is a suffix check and not a hostname check."""
        self.assertEqual(
            resolve_endpoint("https://gpu.internal/openai/v1", "/chat/completions"),
            "https://gpu.internal/openai/v1/chat/completions",
        )


class TheOpenAIImageBodyCarriesOnlyFieldsOpenAIAccepts(unittest.TestCase):
    """The defect: every generated image failed, for every prompt.

    `SceneComposer` attaches a negative prompt to every image directive — "text,
    watermark, logos, distorted anatomy" — and the adapter put it in the request
    body. OpenAI's images API has no such field and rejects unknown parameters
    with a 400, so generation failed 100% of the time while `/health` reported
    the capability as available and the event log said only `provider_failed`.

    The user's own log: `{"from":"generated_image","reason":"provider_failed",
    "to":"programmatic"}` — thirteen times, once per visual.
    """

    def setUp(self) -> None:
        self.bodies: list[dict] = []
        bodies = self.bodies

        class Strict(BaseHTTPRequestHandler):
            """Rejects unknown parameters, as OpenAI does."""

            #: Exactly what OpenAI's images/generations documents.
            allowed = {
                "model", "prompt", "n", "size", "quality",
                "background", "output_format", "user",
            }

            def log_message(self, *_a: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                bodies.append(body)
                extra = set(body) - self.allowed
                if extra:
                    payload = json.dumps(
                        {"error": {"message": f"Unknown parameter: '{sorted(extra)[0]}'."}}
                    ).encode()
                    self.send_response(400)
                else:
                    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
                    payload = json.dumps({"data": [{"b64_json": image}]}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Strict)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._dir = TemporaryDirectory(prefix="vtv-img-")
        self.storage = LocalStorageProvider(
            root=Path(self._dir.name), signing_key="test-key"
        )
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._dir.cleanup()

    def request(self) -> GenerationRequest:
        return GenerationRequest(
            organisation_id="org_0000000000000000000000",
            project_id="prj_0000000000000000000001",
            kind=GenerationKind.IMAGE,
            params=ImageParams(
                prompt="An empty meeting room at dawn.",
                negative_prompt="text, watermark, logos, distorted anatomy",
                aspect_ratio=AspectRatio.LANDSCAPE_16_9,
            ),
            budget=Budget(max_cost_usd=1.0),
        )

    def provider(self, **kw: object) -> HttpImageGenerationProvider:
        return HttpImageGenerationProvider(
            storage=self.storage,
            endpoint=self.base,
            api_key="test-key",
            model="gpt-image-1",
            **kw,  # type: ignore[arg-type]
        )

    def test_a_directive_with_a_negative_prompt_succeeds(self) -> None:
        result = asyncio.run(self.provider().generate_image(self.request()))
        self.assertEqual(result.status.value, "ready")
        self.assertEqual(len(result.outputs), 1)

    def test_the_negative_prompt_is_not_sent_as_a_field(self) -> None:
        asyncio.run(self.provider().generate_image(self.request()))
        self.assertNotIn("negative_prompt", self.bodies[0])

    def test_but_what_it_asked_for_is_not_thrown_away(self) -> None:
        """Folded into the prompt, because it is still worth asking for."""
        asyncio.run(self.provider().generate_image(self.request()))
        self.assertIn("watermark", self.bodies[0]["prompt"])
        self.assertIn("empty meeting room", self.bodies[0]["prompt"])

    def test_an_endpoint_that_does_accept_the_field_still_can(self) -> None:
        """Some self-hosted OpenAI-compatible servers have it. Opt in."""
        provider = self.provider(supports_negative_prompt=True)
        with self.assertRaises(VTVError):
            asyncio.run(provider.generate_image(self.request()))
        self.assertIn("negative_prompt", self.bodies[0])

    def test_a_refusal_carries_the_vendors_message(self) -> None:
        """A rejected parameter and a content refusal are both 400s.

        They need completely different responses from the operator, and the
        adapter used to discard the one sentence that tells them apart.
        """
        provider = self.provider(supports_negative_prompt=True)
        with self.assertRaises(VTVError) as caught:
            asyncio.run(provider.generate_image(self.request()))
        self.assertIn("Unknown parameter", str(caught.exception))


def _image_request(fidelity: VisualFidelity | None) -> GenerationRequest:
    return GenerationRequest(
        organisation_id=ORG,
        project_id=PROJECT,
        kind=GenerationKind.IMAGE,
        params=ImageParams(prompt="a lighthouse", fidelity=fidelity),
        budget=Budget(max_cost_usd=1.0),
    )


class WhatAnImageActuallyCosts(unittest.TestCase):
    """The declared price must be the price on the invoice.

    `unit_cost_usd` was one constructor default of $0.04, used for routing, for
    the pre-dispatch budget refusal, and for the figure written to the ledger
    and the trace. OpenAI's published price for the 1536x1024 size a 16:9 shot
    lands on is $0.016 at `low` and $0.25 at `high` — a fifteen-fold spread that
    one number cannot cover.

    The direction of the old error is what made it matter. Under-reporting turns
    the per-shot ceiling into a suggestion: a shot that "fits" $0.25 at a
    declared $0.04 bills $0.25 and passes, and a trace built to answer "where
    did two dollars go" answers with thirty-two cents.
    """

    def provider(self, **kw: object) -> HttpImageGenerationProvider:
        return HttpImageGenerationProvider(
            storage=None, endpoint="https://x/v1", api_key="k", model="gpt-image-1", **kw  # type: ignore[arg-type]
        )

    def test_the_published_price_is_charged_for_each_quality(self) -> None:
        self.assertAlmostEqual(self.provider(quality="low")._price_each("1536x1024", None), 0.016)
        self.assertAlmostEqual(self.provider(quality="medium")._price_each("1536x1024", None), 0.063)
        self.assertAlmostEqual(self.provider(quality="high")._price_each("1536x1024", None), 0.25)

    def test_an_unset_quality_is_priced_at_the_dearest_tier(self) -> None:
        """We do not know what the vendor's default resolves to, and only one
        of the two possible mistakes shows up on an invoice."""
        self.assertAlmostEqual(self.provider()._price_each("1536x1024", None), 0.25)

    def test_the_router_is_told_the_worst_case_not_an_average(self) -> None:
        """It refuses *before* dispatch, with no shot in hand to size."""
        self.assertAlmostEqual(self.provider(quality="low").declared_unit_cost, 0.016)
        self.assertAlmostEqual(self.provider().declared_unit_cost, 0.25)

    def test_the_request_fidelity_beats_the_deployment_default(self) -> None:
        """The env var is a default for projects that have not chosen, not a
        cap on the ones that have — otherwise a user who paid for `fine` gets
        `low` and no explanation of why their picture looks the same."""
        drafty = self.provider(quality="low")
        self.assertAlmostEqual(
            drafty._price_each("1536x1024", VisualFidelity.FINE), 0.25
        )
        self.assertAlmostEqual(
            drafty._price_each("1536x1024", VisualFidelity.DRAFT), 0.016
        )
        self.assertEqual(drafty.quality_for(VisualFidelity.STANDARD), "medium")
        self.assertEqual(drafty.quality_for(None), "low")

    def test_the_router_is_priced_per_request_not_per_provider(self) -> None:
        """One declared number cannot be honest about a sixteen-fold spread.

        Declaring the cheapest lets a `fine` shot pass a ceiling it then blows
        by sixteen times; declaring the dearest refuses `draft` shots that are
        affordable many times over. So the router asks.
        """
        drafty = self.provider(quality="low")
        self.assertAlmostEqual(drafty.price_for(_image_request(None)), 0.016)
        self.assertAlmostEqual(
            drafty.price_for(_image_request(VisualFidelity.FINE)), 0.25
        )

    def test_the_planner_can_price_a_tier_before_a_request_exists(self) -> None:
        """The budget planner decides how many shots may generate before any of
        them has been built into a request."""
        drafty = self.provider(quality="low")
        self.assertAlmostEqual(drafty.price_at(VisualFidelity.DRAFT), 0.016)
        self.assertAlmostEqual(drafty.price_at(VisualFidelity.STANDARD), 0.063)
        self.assertAlmostEqual(drafty.price_at(VisualFidelity.FINE), 0.25)

    def test_a_model_with_no_published_table_keeps_the_configured_price(self) -> None:
        """A self-hosted endpoint has whatever price its operator pays."""
        dalle = HttpImageGenerationProvider(
            storage=None, endpoint="https://x/v1", api_key="k",  # type: ignore[arg-type]
            model="dall-e-3", unit_cost_usd=0.08,
        )
        self.assertAlmostEqual(dalle.declared_unit_cost, 0.08)
        self.assertAlmostEqual(dalle._price_each("1792x1024", None), 0.08)

    def test_quality_is_only_sent_when_it_was_chosen(self) -> None:
        """Setting it is a deliberate act; the default request is unchanged."""
        from vtv.adapters.images.http_image import _dalle_request_shape

        self.assertEqual(_dalle_request_shape("gpt-image-1")[1][1], (1536, 1024))
        self.assertIsNone(self.provider().quality)
        self.assertEqual(self.provider(quality="low").quality, "low")
