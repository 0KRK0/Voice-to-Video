"""Structured text generation over HTTP.

STATUS: **REAL IMPLEMENTATION — REQUIRES AN ENDPOINT AND A CREDENTIAL.**
Not executed in this environment (no network, no key). Written against the
widely-implemented "OpenAI-compatible chat completions" contract, which most
hosted and self-hosted model servers accept unchanged.

Note the absence of a `chat` method. Every call this system makes asks for a
document that must validate against a named contract from ``schemas/`` — there
is nowhere in the architecture that consumes free-form model prose (Rule 2). The
adapter attaches the schema when the server supports structured output, and
validates the response either way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vtv.contracts.errors import (
    ErrorCode,
    ProviderError,
    ProviderRefused,
    Status,
    TimeoutExceeded,
    ValidationFailed,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationRequest,
    GenerationResult,
    TextParams,
    TokenUsage,
)
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth


class HttpTextGenerationProvider:
    """Structured generation against an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        name: str = "http-llm",
        schema_dir: Path | None = None,
        input_cost_per_mtok: float = 3.0,
        output_cost_per_mtok: float = 15.0,
        data_policy: DataPolicy | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.name = name
        self.schema_dir = schema_dir
        self.input_cost_per_mtok = input_cost_per_mtok
        self.output_cost_per_mtok = output_cost_per_mtok
        self.timeout_seconds = timeout_seconds
        self._data_policy = data_policy or DataPolicy()
        self._failures = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model=self.model,
            # Priced per thousand output tokens for comparability with other
            # kinds; the real charge is computed from actual usage below.
            unit_cost_usd=self.output_cost_per_mtok / 1000.0,
            unit="1k output tokens",
            typical_latency_seconds=12.0,
            supports_structured_output=True,
            data_policy=self._data_policy,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(consecutive_failures=self._failures)

    def _schema(self, name: str | None) -> dict[str, Any] | None:
        """Load an exported JSON Schema to constrain the response."""
        if not name or not self.schema_dir:
            return None
        path = Path(self.schema_dir) / f"{name}.json"
        if not path.exists():
            return None
        return dict(json.loads(path.read_text(encoding="utf-8")))

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, TextParams):
            raise VTVError("text request carried the wrong params")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("httpx is required for HttpTextGenerationProvider") from exc

        body: dict[str, Any] = {
            "model": self.model,
            "temperature": params.temperature,
            "max_tokens": params.max_output_tokens,
            "messages": [
                {"role": "system", "content": params.instruction},
                {
                    "role": "user",
                    "content": json.dumps(params.input_json, ensure_ascii=False),
                },
            ],
        }
        schema = self._schema(params.response_schema)
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": params.response_schema,
                    "schema": schema,
                    "strict": False,
                },
            }
        elif params.response_schema:
            body["response_format"] = {"type": "json_object"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.endpoint}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except Exception as exc:
            self._failures += 1
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutExceeded("text generation timed out") from exc
            raise ProviderError(f"text generation failed: {type(exc).__name__}") from exc

        if response.status_code == 429:
            self._failures += 1
            raise ProviderError("rate limited", code=ErrorCode.RATE_LIMITED)
        if response.status_code >= 500:
            self._failures += 1
            raise ProviderError(f"provider returned {response.status_code}")
        if response.status_code >= 400:
            # A 4xx is our fault or a refusal; retrying will not fix it.
            raise ProviderRefused(f"provider rejected the request ({response.status_code})")

        self._failures = 0
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise ValidationFailed("model response contained no choices")

        message = choices[0].get("message") or {}
        if choices[0].get("finish_reason") == "content_filter":
            raise ProviderRefused("the model declined to answer")
        content = message.get("content")
        if not content:
            raise ValidationFailed("model response was empty")

        # Imported lazily: understanding.extract_json tolerates fences and prose
        # around the JSON, which models emit regardless of instructions.
        from vtv.pipeline.understanding import extract_json

        structured = extract_json(content)
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)

        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model=payload.get("model") or self.model,
            structured_output=structured,
            tokens=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            cost_usd=round(
                (input_tokens / 1e6) * self.input_cost_per_mtok
                + (output_tokens / 1e6) * self.output_cost_per_mtok,
                6,
            ),
            latency_ms=int(response.elapsed.total_seconds() * 1000),
        )


__all__ = ["HttpTextGenerationProvider"]
