"""Speech synthesis over HTTP.

STATUS: **EXECUTED AGAINST A CONFORMING SERVER. REQUIRES AN ENDPOINT AND A
CREDENTIAL FOR REAL USE.** `tests/test_provider_adapters_live.py` stands up a
local HTTP server implementing this contract and drives this adapter through
it — request shape, credential, response parsing, error mapping and storage
write are all exercised. What that cannot prove is any particular vendor: a
real endpoint may differ in undocumented required fields, error envelopes or
rate limits. "Correct against the contract" and "works against Vendor X" are
different claims, and only the first is made here.

The request shape is the widely-implemented "OpenAI-compatible audio/speech"
contract, which several hosted and self-hosted synthesisers accept unchanged.

Configure with:

    VTV_SPEECH_SYNTHESIS_ENDPOINT=https://.../v1/audio/speech
    VTV_SPEECH_SYNTHESIS_API_KEY=...
    VTV_SPEECH_SYNTHESIS_MODEL=...

The duration is *measured from the returned bytes*, never assumed. The timeline
is built against it, and a provider that speaks slightly faster or slower than
the requested rate would otherwise desynchronise every caption in the video.

## One request per sentence, not one per script

`SpeechParams.segment_texts` carries the sentences the caller wants timings
for, and this adapter synthesises them **one at a time**, measures each, and
joins the audio losslessly.

That is not an optimisation; it is the only way to know where a sentence
starts. The previous version sent the whole script as one string, measured one
total duration, and `NarrationService` then scaled the estimated per-sentence
timings by a single factor to fit it. A uniform scale corrects the *total* and
cannot correct the *distribution*: a sentence containing "1,024" or
"asynchronous" takes proportionally longer to say than its character count
predicts, so its neighbours absorb the difference and every subsequent caption
sits slightly wrong. The error is worst in the middle and only returns to zero
at the very end, which is precisely the "the captions drift and the gap keeps
growing" report this docstring exists to explain.

The cost is unchanged — vendors price speech per character — and the requests
run concurrently, so the wall-clock penalty is bounded by
`max_parallel_segments` rather than by the number of sentences.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from vtv.adapters.media import ffmpeg
from vtv.contracts.base import RetentionClass
from vtv.contracts.errors import (
    ErrorCode,
    ProviderError,
    Status,
    TimeoutExceeded,
    ValidationFailed,
)
from vtv.contracts.generation import (
    GenerationRequest,
    GenerationResult,
    SpeechParams,
)
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth
from vtv.security.paths import tenant_key

#: Languages the platform commits to routing. A provider that does not list one
#: is simply not selected for it; the ladder then descends rather than producing
#: confident-sounding nonsense in the wrong accent.
DEFAULT_LANGUAGES = [
    "en", "hi", "te", "ta", "kn", "ml", "bn", "mr", "gu", "pa", "ur",
    "es", "fr", "de", "pt", "ar", "ja", "ko", "zh",
]

MAX_RESPONSE_BYTES = 200 * 1024 * 1024


class HttpSpeechSynthesisProvider:
    """Narration from an OpenAI-compatible speech endpoint."""

    def __init__(
        self,
        *,
        storage: object,
        endpoint: str,
        api_key: str,
        model: str = "tts-1",
        default_voice: str = "alloy",
        name: str = "http-tts",
        unit_cost_usd: float = 0.015,
        languages: list[str] | None = None,
        data_policy: DataPolicy | None = None,
        timeout_seconds: float = 180.0,
        max_parallel_segments: int = 4,
    ) -> None:
        self.storage = storage
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.default_voice = default_voice
        self.name = name
        self.unit_cost_usd = unit_cost_usd
        self.languages = languages if languages is not None else list(DEFAULT_LANGUAGES)
        self.timeout_seconds = timeout_seconds
        #: How many sentences are in flight at once. Above about four, hosted
        #: speech endpoints start returning 429 rather than going faster, and a
        #: rate-limited synthesis costs the user a whole retry of the project.
        self.max_parallel_segments = max(1, max_parallel_segments)
        self._data_policy = data_policy or DataPolicy()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model=self.model,
            unit_cost_usd=self.unit_cost_usd,
            unit="1k-characters",
            typical_latency_seconds=8.0,
            languages=list(self.languages),
            data_policy=self._data_policy,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def synthesize(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, SpeechParams):
            raise ProviderError("speech request carried the wrong params")
        if not ffmpeg.is_available():
            raise ProviderError(
                "ffmpeg is required to measure synthesised narration"
            )

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "httpx is required for HttpSpeechSynthesisProvider"
            ) from exc

        pieces = [text for text in params.segment_texts if text.strip()]
        if not pieces:
            pieces = [params.text]

        started = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            gate = asyncio.Semaphore(self.max_parallel_segments)

            async def one(text: str) -> bytes:
                async with gate:
                    return await self._speak(client, text, params)

            audio_pieces = await asyncio.gather(*(one(text) for text in pieces))
        latency_ms = int((time.monotonic() - started) * 1000)

        with tempfile.TemporaryDirectory(prefix="vtv-tts-") as scratch:
            root = Path(scratch)
            paths: list[Path] = []
            for index, blob in enumerate(audio_pieces):
                part = root / f"part-{index:05d}.audio"
                part.write_bytes(blob)
                paths.append(part)
            joined = root / "narration.wav"
            # The durations are measured after decoding, which is what makes
            # them true regardless of what container the vendor returned.
            durations = ffmpeg.concat_audio(paths, joined)
            audio = joined.read_bytes()
            total = round(ffmpeg.probe_audio(joined).duration_seconds, 3)

        if total <= 0:
            raise ValidationFailed("synthesised narration had no measurable duration")

        segments: list[dict[str, object]] = []
        cursor = 0.0
        for text, length in zip(pieces, durations, strict=True):
            start = round(cursor, 3)
            cursor = round(cursor + length, 3)
            segments.append({"start": start, "end": cursor, "text": text})

        ref = await self.storage.put(  # type: ignore[attr-defined]
            key=tenant_key(
                request.organisation_id,
                "projects",
                request.project_id or "anonymous",
                "narration",
                f"{request.cache_key()[:32]}.wav",
            ),
            data=audio,
            content_type="audio/wav",
            retention=RetentionClass.PROJECT,
        )

        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model=self.model,
            outputs=[ref],
            cost_usd=round((len(params.text) / 1000.0) * self.unit_cost_usd, 6),
            latency_ms=latency_ms,
            structured_output={
                "duration_seconds": total,
                "has_speech": True,
                "language": params.language,
                # Where each sentence actually begins and ends in the file
                # above. Measured, not apportioned — see this module's
                # docstring for why the difference is the whole point.
                "segments": segments,
            },
        )

    async def _speak(self, client: object, text: str, params: SpeechParams) -> bytes:
        """One sentence, one request. Returns the vendor's bytes, unmodified."""
        payload = {
            "model": self.model,
            "input": text[:20000],
            "voice": params.voice or self.default_voice,
            "response_format": "mp3",
            "speed": params.speaking_rate,
        }
        try:
            response = await client.post(  # type: ignore[attr-defined]
                self.endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutExceeded("speech synthesis timed out") from exc
            raise ProviderError(
                f"speech synthesis request failed: {type(exc).__name__}"
            ) from exc

        if response.status_code == 429:
            raise ProviderError(
                "rate limited by speech provider", code=ErrorCode.RATE_LIMITED
            )
        if response.status_code >= 400:
            raise ProviderError(f"speech provider returned {response.status_code}")

        audio = response.content
        if not audio:
            raise ValidationFailed("speech provider returned no audio")
        if len(audio) > MAX_RESPONSE_BYTES:
            raise ValidationFailed("speech provider returned an implausibly large file")
        return bytes(audio)


__all__ = ["DEFAULT_LANGUAGES", "MAX_RESPONSE_BYTES", "HttpSpeechSynthesisProvider"]
