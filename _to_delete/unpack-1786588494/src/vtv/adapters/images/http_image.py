"""Image and video generation over HTTP.

STATUS: **REAL IMPLEMENTATION — REQUIRES AN ENDPOINT AND A CREDENTIAL.**
Not executed in this environment (no network, no key).

Both adapters store their output through the storage port before returning, so
the result carries an `ObjectRef` rather than raw bytes or a vendor URL that
expires in an hour. That detail is what lets a project be re-rendered a week
later without every generated frame having evaporated.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

from vtv.contracts.base import RetentionClass
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
    ImageParams,
    VideoParams,
)
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth


class HttpImageGenerationProvider:
    """Image generation against a JSON HTTP endpoint."""

    def __init__(
        self,
        *,
        storage: object,
        endpoint: str,
        api_key: str,
        model: str,
        name: str = "http-image",
        unit_cost_usd: float = 0.04,
        typical_latency_seconds: float = 14.0,
        max_output_width: int = 1920,
        max_output_height: int = 1920,
        supports_seed: bool = True,
        data_policy: DataPolicy | None = None,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.storage = storage
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.name = name
        self.unit_cost_usd = unit_cost_usd
        self.typical_latency_seconds = typical_latency_seconds
        self.max_output_width = max_output_width
        self.max_output_height = max_output_height
        self.supports_seed = supports_seed
        self.timeout_seconds = timeout_seconds
        self._data_policy = data_policy or DataPolicy()
        self._failures = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model=self.model,
            unit_cost_usd=self.unit_cost_usd,
            unit="image",
            typical_latency_seconds=self.typical_latency_seconds,
            max_output_width=self.max_output_width,
            max_output_height=self.max_output_height,
            supports_seed=self.supports_seed,
            supports_style_reference=False,
            data_policy=self._data_policy,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(consecutive_failures=self._failures)

    async def generate_image(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, ImageParams):
            raise VTVError("image request carried the wrong params")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("httpx is required for HttpImageGenerationProvider") from exc

        width, height = params.aspect_ratio.dimensions_1080
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": params.prompt,
            "n": params.count,
            "size": f"{min(width, self.max_output_width)}x{min(height, self.max_output_height)}",
            "response_format": "b64_json",
        }
        if params.negative_prompt:
            body["negative_prompt"] = params.negative_prompt
        if params.seed is not None and self.supports_seed:
            body["seed"] = params.seed

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.endpoint}/images/generations",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
        except Exception as exc:
            self._failures += 1
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutExceeded("image generation timed out") from exc
            raise ProviderError(f"image generation failed: {type(exc).__name__}") from exc

        if response.status_code == 429:
            self._failures += 1
            raise ProviderError("rate limited", code=ErrorCode.RATE_LIMITED)
        if response.status_code == 400:
            # Content policy refusals arrive as 400s. They are not retryable and
            # must degrade the shot rather than fail the project.
            raise ProviderRefused("image request was refused by the provider")
        if response.status_code >= 400:
            self._failures += 1
            raise ProviderError(f"image provider returned {response.status_code}")

        self._failures = 0
        payload = response.json()
        images = payload.get("data") or []
        if not images:
            raise ValidationFailed("image provider returned no images")

        refs = []
        for index, item in enumerate(images[: params.count]):
            encoded = item.get("b64_json")
            if not encoded:
                continue
            data = base64.b64decode(encoded)
            refs.append(
                await self.storage.put(  # type: ignore[attr-defined]
                    key=(
                        f"projects/{request.project_id or 'shared'}/generated/"
                        f"{request.request_id}-{index}.png"
                    ),
                    data=data,
                    content_type="image/png",
                    retention=RetentionClass.PROJECT,
                )
            )
        if not refs:
            raise ValidationFailed("image provider returned no decodable images")

        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model=self.model,
            outputs=refs,
            cost_usd=round(self.unit_cost_usd * len(refs), 6),
            latency_ms=int((time.perf_counter() - started) * 1000),
            seed=params.seed,
        )


class HttpVideoGenerationProvider:
    """Video generation against an asynchronous job endpoint.

    Video calls take minutes, so the adapter owns its own polling — and honours
    ``budget.max_latency_seconds`` by giving up, so the fallback ladder can
    proceed instead of the project hanging.
    """

    def __init__(
        self,
        *,
        storage: object,
        endpoint: str,
        api_key: str,
        model: str,
        name: str = "http-video",
        unit_cost_usd: float = 0.25,
        typical_latency_seconds: float = 150.0,
        max_duration_seconds: float = 10.0,
        poll_interval_seconds: float = 5.0,
        data_policy: DataPolicy | None = None,
    ) -> None:
        self.storage = storage
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.name = name
        self.unit_cost_usd = unit_cost_usd
        self.typical_latency_seconds = typical_latency_seconds
        self.max_duration_seconds = max_duration_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._data_policy = data_policy or DataPolicy()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model=self.model,
            unit_cost_usd=self.unit_cost_usd,
            unit="second of video",
            typical_latency_seconds=self.typical_latency_seconds,
            max_duration_seconds=self.max_duration_seconds,
            data_policy=self._data_policy,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate_video(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, VideoParams):
            raise VTVError("video request carried the wrong params")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("httpx is required for HttpVideoGenerationProvider") from exc

        deadline = time.perf_counter() + (
            request.budget.max_latency_seconds or self.typical_latency_seconds * 3
        )
        duration = min(params.duration_seconds, self.max_duration_seconds)

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                created = await client.post(
                    f"{self.endpoint}/video/generations",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self.model,
                        "prompt": params.prompt,
                        "duration": duration,
                        "aspect_ratio": params.aspect_ratio.value,
                        **({"seed": params.seed} if params.seed is not None else {}),
                    },
                )
            except Exception as exc:
                raise ProviderError(f"video request failed: {type(exc).__name__}") from exc

            if created.status_code >= 400:
                raise ProviderError(f"video provider returned {created.status_code}")
            job_id = (created.json() or {}).get("id")
            if not job_id:
                raise ValidationFailed("video provider returned no job id")

            while True:
                if time.perf_counter() > deadline:
                    raise TimeoutExceeded(
                        "video generation exceeded its latency budget"
                    )
                await asyncio.sleep(self.poll_interval_seconds)
                poll = await client.get(
                    f"{self.endpoint}/video/generations/{job_id}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if poll.status_code >= 400:
                    raise ProviderError(f"video poll returned {poll.status_code}")
                state = poll.json() or {}
                status = str(state.get("status", "")).lower()
                if status in {"failed", "error"}:
                    raise ProviderError("video generation failed upstream")
                if status in {"succeeded", "completed"}:
                    url = state.get("url")
                    if not url:
                        raise ValidationFailed("completed video job carried no url")
                    downloaded = await client.get(url)
                    ref = await self.storage.put(  # type: ignore[attr-defined]
                        key=(
                            f"projects/{request.project_id or 'shared'}/generated/"
                            f"{request.request_id}.mp4"
                        ),
                        data=downloaded.content,
                        content_type="video/mp4",
                        retention=RetentionClass.PROJECT,
                    )
                    return GenerationResult(
                        request_id=request.request_id,
                        cache_key=request.cache_key(),
                        status=Status.READY,
                        provider=self.name,
                        model=self.model,
                        outputs=[ref],
                        cost_usd=round(self.unit_cost_usd * duration, 6),
                        latency_ms=int(
                            (self.typical_latency_seconds) * 1000
                        ),
                        seed=params.seed,
                    )


__all__ = ["HttpImageGenerationProvider", "HttpVideoGenerationProvider"]
