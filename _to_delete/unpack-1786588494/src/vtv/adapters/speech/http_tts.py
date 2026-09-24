"""Speech synthesis over HTTP.

STATUS: **REAL IMPLEMENTATION — REQUIRES AN ENDPOINT AND A CREDENTIAL.**
Not executed in this environment: no network access and no key. The request
shape is the widely-implemented "OpenAI-compatible audio/speech" contract, which
several hosted and self-hosted synthesisers accept unchanged.

Configure with:

    VTV_SPEECH_SYNTHESIS_ENDPOINT=https://.../v1/audio/speech
    VTV_SPEECH_SYNTHESIS_API_KEY=...
    VTV_SPEECH_SYNTHESIS_MODEL=...

The duration is *measured from the returned bytes*, never assumed. The timeline
is built against it, and a provider that speaks slightly faster or slower than
the requested rate would otherwise desynchronise every caption in the video.
"""

from __future__ import annotations

import tempfile
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

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "httpx is required for HttpSpeechSynthesisProvider"
            ) from exc

        payload = {
            "model": self.model,
            "input": params.text[:20000],
            "voice": params.voice or self.default_voice,
            "response_format": "mp3",
            "speed": params.speaking_rate,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
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

        duration = self._measure(audio)

        ref = await self.storage.put(  # type: ignore[attr-defined]
            key=(
                f"projects/{request.project_id or 'anonymous'}"
                f"/narration/{request.cache_key()[:32]}.mp3"
            ),
            data=audio,
            content_type="audio/mpeg",
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
            latency_ms=int(response.elapsed.total_seconds() * 1000),
            structured_output={
                "duration_seconds": duration,
                "has_speech": True,
                "language": params.language,
            },
        )

    def _measure(self, audio: bytes) -> float:
        """Real duration of the returned file.

        Raises rather than guessing: a wrong duration silently desynchronises
        every caption and every visual in the finished video, which is far worse
        than a failed request that falls back cleanly.
        """
        if not ffmpeg.is_available():
            raise ProviderError(
                "ffmpeg is required to measure synthesised narration"
            )
        with tempfile.TemporaryDirectory(prefix="vtv-tts-") as scratch:
            path = Path(scratch) / "narration.mp3"
            path.write_bytes(audio)
            probed = ffmpeg.probe_audio(path)
        if probed.duration_seconds <= 0:
            raise ValidationFailed("synthesised narration had no measurable duration")
        return round(probed.duration_seconds, 3)


__all__ = ["DEFAULT_LANGUAGES", "MAX_RESPONSE_BYTES", "HttpSpeechSynthesisProvider"]
