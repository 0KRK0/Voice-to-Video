"""Speech-to-text over HTTP.

STATUS: **REAL IMPLEMENTATION — REQUIRES AN ENDPOINT AND A CREDENTIAL.**
Not executed in this environment: no network access and no key. The code is
complete and the request shape is the widely-implemented "OpenAI-compatible
audio transcription" contract, which several hosted and self-hosted Whisper
deployments accept unchanged.

Configure with:

    VTV_SPEECH_TO_TEXT_ENDPOINT=https://.../v1/audio/transcriptions
    VTV_SPEECH_TO_TEXT_API_KEY=...

Note what this adapter does *not* do: retry, cache, or choose a different model
than it advertises. Those belong to the router (`docs/AI_PROVIDER_POLICY.md`).
"""

from __future__ import annotations

from typing import Any

from vtv.contracts.base import TimeSpan
from vtv.contracts.errors import (
    ErrorCode,
    ProviderError,
    Status,
    TimeoutExceeded,
    ValidationFailed,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationRequest,
    GenerationResult,
    SpeechToTextParams,
)
from vtv.contracts.transcript import TranscriptSegment, TranscriptWord
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth


class HttpSpeechToTextProvider:
    """Transcription against an OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        *,
        storage: object,
        endpoint: str,
        api_key: str,
        model: str = "whisper-1",
        name: str = "http-stt",
        unit_cost_usd: float = 0.006,
        data_policy: DataPolicy | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.storage = storage
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.name = name
        self.unit_cost_usd = unit_cost_usd
        self.timeout_seconds = timeout_seconds
        # Pessimistic by default. A provider is only cleared for voice once
        # somebody has verified its terms and recorded that here.
        self._data_policy = data_policy or DataPolicy()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model=self.model,
            unit_cost_usd=self.unit_cost_usd,
            unit="minute",
            typical_latency_seconds=20.0,
            supports_word_timestamps=True,
            data_policy=self._data_policy,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def transcribe(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, SpeechToTextParams):
            raise VTVError("speech-to-text request carried the wrong params")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError(
                "httpx is required for HttpSpeechToTextProvider"
            ) from exc

        audio = await self.storage.get(params.audio)  # type: ignore[attr-defined]

        form: dict[str, Any] = {
            "model": self.model,
            "response_format": "verbose_json",
        }
        if params.word_timestamps:
            form["timestamp_granularities[]"] = "word"
        if params.language_hint:
            form["language"] = params.language_hint
        if params.vocabulary:
            # Domain vocabulary materially improves recognition of names and
            # jargon, which is exactly what an explainer is full of.
            form["prompt"] = ", ".join(params.vocabulary[:64])

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data=form,
                    files={"file": ("audio", audio, params.audio.content_type)},
                )
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutExceeded("transcription timed out") from exc
            raise ProviderError(f"transcription request failed: {type(exc).__name__}") from exc

        if response.status_code == 429:
            raise ProviderError(
                "rate limited by transcription provider", code=ErrorCode.RATE_LIMITED
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"transcription provider returned {response.status_code}"
            )

        payload = response.json()
        segments = self._parse(payload)
        if not segments:
            raise ValidationFailed("transcription returned no usable segments")

        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model=self.model,
            structured_output={
                "language": payload.get("language", params.language_hint or "en"),
                "segments": [s.model_dump(mode="json") for s in segments],
            },
            cost_usd=self._estimate_cost(payload),
            latency_ms=int(response.elapsed.total_seconds() * 1000),
        )

    def _estimate_cost(self, payload: dict[str, Any]) -> float:
        duration = float(payload.get("duration") or 0.0)
        return round((duration / 60.0) * self.unit_cost_usd, 6)

    def _parse(self, payload: dict[str, Any]) -> list[TranscriptSegment]:
        """Map the provider's response onto our contract.

        Provider responses drift. Anything that does not parse cleanly is
        dropped rather than coerced, and an empty result raises — a transcript
        with invented timings would corrupt every stage after it.
        """
        words_by_time: list[dict[str, Any]] = payload.get("words") or []
        segments: list[TranscriptSegment] = []
        cursor = 0.0
        for raw in payload.get("segments") or []:
            try:
                start = max(float(raw["start"]), cursor)
                end = float(raw["end"])
                text = str(raw["text"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not text or end <= start:
                continue
            words = [
                TranscriptWord(
                    text=str(word["word"]).strip(),
                    span=TimeSpan.of(float(word["start"]), float(word["end"])),
                )
                for word in words_by_time
                if _inside(word, start, end)
            ]
            segments.append(
                TranscriptSegment(
                    span=TimeSpan.of(start, end),
                    text=text,
                    confidence=_confidence(raw),
                    words=words,
                )
            )
            cursor = end
        return segments


def _inside(word: dict[str, Any], start: float, end: float) -> bool:
    try:
        return start <= float(word["start"]) and float(word["end"]) <= end
    except (KeyError, TypeError, ValueError):
        return False


def _confidence(raw: dict[str, Any]) -> float | None:
    """Map log-probability to a rough confidence, when one is offered."""
    value = raw.get("avg_logprob")
    if value is None:
        return None
    try:
        import math

        return max(0.0, min(1.0, math.exp(float(value))))
    except (TypeError, ValueError):
        return None


__all__ = ["HttpSpeechToTextProvider"]
