"""Video generation against OpenAI's `/videos` API.

STATUS: **WRITTEN AGAINST THE PUBLISHED REFERENCE AND EXERCISED AGAINST A LOCAL
SERVER IMPLEMENTING IT. NEVER CALLED AGAINST OPENAI.** The request shape, the
polling loop, the status vocabulary, the download step, the error mapping and
the storage write are all covered by `tests/test_openai_video.py`, which stands
up an HTTP server speaking this contract. What that cannot prove is OpenAI: a
real endpoint may differ in undocumented required fields, in what it accepts for
`seconds` and `size`, or in its error envelope. "Correct against the published
contract" and "works against OpenAI" are different claims and only the first is
made here.

## Why this exists separately from `HttpVideoGenerationProvider`

They are different APIs, not different formatting of one.

`HttpVideoGenerationProvider` speaks a create-then-poll contract where the job
is created at `…/video/generations`, polled at `…/video/generations/{id}`, and
the finished video is fetched from a `url` field on the completed job. Pointing
it at OpenAI produces `…/v1/videos/video/generations`, which is a 404 — and the
symptom a user sees is "video generation is configured and nothing ever
appears".

OpenAI's is:

* `POST {base}/videos` with `model`, `prompt`, and optionally `seconds` and
  `size`, returning an object with `id` and `status`;
* `GET {base}/videos/{id}` to poll, with `status` in `queued`, `in_progress`,
  `completed`, `failed`;
* `GET {base}/videos/{id}/content` returning the MP4 bytes directly — there is
  no URL to follow.

## Two values this adapter is deliberately conservative about

At the time of writing, OpenAI's own API reference and its video-generation
guide **disagreed** about what `seconds` and `size` accept: the reference listed
`"4"`, `"8"`, `"12"` and four sizes; the guide listed `8`, `16`, `20` and six
sizes. Both are OpenAI's documentation and they cannot both be complete.

So this adapter sends only values that appear in **both** lists — `"8"` for
duration, and `1280x720` / `720x1280` for size — and makes them overridable.
That is not timidity: an enum guessed wrong is a 400 on every request, and the
user reads a 400 as "my key is broken". Where the documentation is
self-contradictory the honest move is to use the intersection and say why.

If a vendor rejects them, the vendor's own message is surfaced rather than
replaced, so the next person sees what the API actually said.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from vtv.adapters.endpoints import resolve_endpoint
from vtv.contracts.base import RetentionClass
from vtv.contracts.errors import (
    ErrorCode,
    ProviderError,
    Status,
    TimeoutExceeded,
    ValidationFailed,
    VTVError,
)
from vtv.contracts.generation import GenerationRequest, GenerationResult, VideoParams
from vtv.contracts.style import AspectRatio
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth
from vtv.security.paths import tenant_key

#: Durations both published lists contain. See the module docstring.
DEFAULT_SECONDS = "8"

#: Sizes both published lists contain, by orientation.
LANDSCAPE_SIZE = "1280x720"
PORTRAIT_SIZE = "720x1280"

#: A finished clip larger than this is not something we asked for.
MAX_RESPONSE_BYTES = 512 * 1024 * 1024

_TERMINAL_FAILURE = {"failed", "error", "cancelled", "canceled"}
_TERMINAL_SUCCESS = {"completed", "succeeded"}


class OpenAIVideoGenerationProvider:
    """Sora-shaped video generation: create, poll, download."""

    def __init__(
        self,
        *,
        storage: object,
        endpoint: str,
        api_key: str,
        model: str,
        name: str = "openai-video",
        #: Per second of finished video. A ceiling used for routing and for the
        #: budget check, not a quote: the real price is the vendor's, and this
        #: number exists so the router can refuse an unaffordable shot *before*
        #: making the call rather than after the invoice.
        unit_cost_usd: float = 0.10,
        typical_latency_seconds: float = 120.0,
        seconds: str = DEFAULT_SECONDS,
        poll_interval_seconds: float = 5.0,
        request_timeout_seconds: float = 60.0,
        data_policy: DataPolicy | None = None,
    ) -> None:
        self.storage = storage
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.name = name
        self.unit_cost_usd = unit_cost_usd
        self.typical_latency_seconds = typical_latency_seconds
        self.seconds = seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self._data_policy = data_policy or DataPolicy()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model=self.model,
            unit_cost_usd=self.unit_cost_usd,
            unit="second of video",
            typical_latency_seconds=self.typical_latency_seconds,
            max_duration_seconds=float(self.seconds),
            data_policy=self._data_policy,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    # -- the contract -----------------------------------------------------

    def _url(self, *parts: str) -> str:
        base = resolve_endpoint(self.endpoint, "/videos")
        return "/".join([base, *parts]) if parts else base

    def _size_for(self, aspect: AspectRatio) -> str:
        width, height = aspect.dimensions_1080
        return PORTRAIT_SIZE if height > width else LANDSCAPE_SIZE

    async def generate_video(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, VideoParams):
            raise VTVError("video request carried the wrong params")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError(
                "httpx is required for OpenAIVideoGenerationProvider"
            ) from exc

        # The whole operation, not one request. Video takes minutes, and the
        # point of the deadline is that the ladder descends to a still rather
        # than the project hanging on somebody else's queue.
        deadline = time.perf_counter() + (
            request.budget.max_latency_seconds or self.typical_latency_seconds * 3
        )
        headers = {"Authorization": f"Bearer {self._api_key}"}

        async with httpx.AsyncClient(timeout=self.request_timeout_seconds) as client:
            job_id = await self._create(client, request, params, headers)
            state = await self._await_completion(client, job_id, headers, deadline)
            data = await self._download(client, job_id, headers)

        seconds = _reported_seconds(state, self.seconds)
        ref = await self.storage.put(  # type: ignore[attr-defined]
            key=tenant_key(
                request.organisation_id,
                "projects",
                request.project_id or "shared",
                "generated",
                f"{request.request_id}.mp4",
            ),
            data=data,
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
            cost_usd=round(self.unit_cost_usd * seconds, 6),
            latency_ms=int(self.typical_latency_seconds * 1000),
            structured_output={"seconds": seconds, "video_id": job_id},
        )

    async def _create(
        self,
        client: Any,
        request: GenerationRequest,
        params: VideoParams,
        headers: dict[str, str],
    ) -> str:
        body = {
            "model": self.model,
            "prompt": params.prompt,
            "seconds": self.seconds,
            "size": self._size_for(params.aspect_ratio),
        }
        try:
            created = await client.post(self._url(), headers=headers, json=body)
        except Exception as exc:
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutExceeded("video creation timed out") from exc
            raise ProviderError(f"video request failed: {type(exc).__name__}") from exc

        if created.status_code == 429:
            raise ProviderError(
                "rate limited by video provider", code=ErrorCode.RATE_LIMITED
            )
        if created.status_code >= 400:
            # The vendor's own words. An enum we sent wrongly, a model the
            # account cannot reach and a revoked key are three different
            # problems, and only the vendor can tell them apart.
            raise ProviderError(
                f"video provider returned {created.status_code}: "
                f"{_message_from(created)}",
                code=(
                    ErrorCode.GENERATION_REFUSED
                    if created.status_code in (400, 403, 422)
                    else ErrorCode.PROVIDER_UNAVAILABLE
                ),
            )

        job_id = str((_json(created) or {}).get("id") or "")
        if not job_id:
            raise ValidationFailed("video provider returned no job id")
        return job_id

    async def _await_completion(
        self,
        client: Any,
        job_id: str,
        headers: dict[str, str],
        deadline: float,
    ) -> dict[str, Any]:
        while True:
            if time.perf_counter() > deadline:
                raise TimeoutExceeded("video generation exceeded its latency budget")
            await asyncio.sleep(self.poll_interval_seconds)
            poll = await client.get(self._url(job_id), headers=headers)
            if poll.status_code >= 400:
                raise ProviderError(
                    f"video poll returned {poll.status_code}: {_message_from(poll)}"
                )
            state = _json(poll) or {}
            status = str(state.get("status", "")).lower()
            if status in _TERMINAL_FAILURE:
                raise ProviderError(
                    "video generation failed upstream: "
                    f"{_message_from_state(state)}",
                    code=ErrorCode.GENERATION_FAILED,
                )
            if status in _TERMINAL_SUCCESS:
                return state

    async def _download(
        self, client: Any, job_id: str, headers: dict[str, str]
    ) -> bytes:
        """The finished bytes.

        OpenAI serves them from the job itself rather than handing back a URL,
        which is the single largest difference from the generic adapter and the
        reason that one cannot be pointed here.
        """
        response = await client.get(
            self._url(job_id, "content"), headers=headers, follow_redirects=True
        )
        if response.status_code >= 400:
            raise ProviderError(
                f"video download returned {response.status_code}: "
                f"{_message_from(response)}"
            )
        data = bytes(response.content)
        if not data:
            raise ValidationFailed("a completed video job returned no bytes")
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValidationFailed("video provider returned an implausibly large file")
        return data


def _json(response: Any) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _message_from(response: Any) -> str:
    payload = _json(response)
    if not payload:
        return (getattr(response, "text", "") or "")[:200]
    return _message_from_state(payload)


def _message_from_state(state: dict[str, Any]) -> str:
    error = state.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    if error:
        return str(error)[:200]
    return "no message"


def _reported_seconds(state: dict[str, Any], fallback: str) -> float:
    """What the vendor says it made, not what we asked for.

    The charge follows the delivered clip. A provider that rounded eight seconds
    up to twelve has billed for twelve, and a cost line that says eight is
    wrong in the direction that matters.
    """
    for key in ("seconds", "duration", "duration_seconds"):
        value = state.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    try:
        return float(fallback)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "DEFAULT_SECONDS",
    "LANDSCAPE_SIZE",
    "MAX_RESPONSE_BYTES",
    "PORTRAIT_SIZE",
    "OpenAIVideoGenerationProvider",
]
