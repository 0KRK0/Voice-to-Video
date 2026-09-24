"""Image and video generation over HTTP.

STATUS: **EXECUTED AGAINST A CONFORMING SERVER. REQUIRES AN ENDPOINT, A
CREDENTIAL AND A MODEL FOR REAL USE.** `tests/test_provider_adapters_live.py`
stands up a local HTTP server implementing this contract and drives this
adapter through it — request shape, credential, response parsing, error
mapping and storage write are all exercised. What that cannot prove is any
particular vendor: a real endpoint may differ in undocumented required
fields, error envelopes or rate limits. "Correct against the contract" and
"works against Vendor X" are different claims, and only the first is made
here.

Configure image generation with:

    VTV_IMAGE_GENERATION_ENDPOINT=https://api.openai.com/v1
    VTV_IMAGE_GENERATION_API_KEY=sk-...
    VTV_IMAGE_GENERATION_MODEL=gpt-image-1   # or dall-e-2, dall-e-3, ...

There is no default model. "default" is not a real model name for any
vendor's image API, and this file used to send it as a literal — which the
vendor rejects on every call. `wiring.build` refuses to report
`real_image_generation: true` until a real model is configured; see the
comment there.

**`HttpImageGenerationProvider` knows two shapes of "OpenAI-compatible" image
API, because they are not one contract:** dall-e-2/dall-e-3 accept a
`response_format` field and only a fixed list of square (dall-e-2) or
1024-tall (dall-e-3) sizes; the gpt-image-* family rejects `response_format`
outright (it always returns base64) and accepts a different fixed trio of
sizes. `_dalle_request_shape` decides which rules apply from the configured
model name, and `_snap_size` picks the accepted size closest in aspect ratio
to what the shot actually asked for — see its docstring for why "closest
aspect ratio" and not "closest area", and why the gpt-image custom-size path
is not implemented.

**`HttpVideoGenerationProvider` speaks a different shape entirely: synchronous
create, then poll a job id, then download a URL from the completed job.**
That is a real, implemented pattern — it is not OpenAI's. OpenAI's video API
(Sora) is a multipart create call that returns immediately with a job in
`queued` state, polled by a *different* endpoint shape than the one below,
and downloaded via a `/content` endpoint rather than a bare URL in the poll
response. Do not point `VTV_VIDEO_GENERATION_ENDPOINT` at OpenAI expecting
this adapter to work; nothing here has been verified against that API, and
nothing here fabricates it. This adapter's contract is a reasonable fit for
vendors that already publish an async create/poll/url video API in roughly
this shape (Runway and Luma's HTTP APIs, at the time this was written, both
do) — verify against the vendor's own reference before configuring, the same
caveat as everything else in this module.

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

from vtv.adapters.endpoints import resolve_endpoint
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
    VisualFidelity,
)
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth
from vtv.security.paths import tenant_key

#: Sizes each OpenAI-shaped image model actually accepts. Vendor validation
#: rejects an arbitrary size outright rather than clamping it into range, so
#: the request must choose *from this list* — not cap the requested width and
#: height within some maximum, which is what this file used to do and which
#: sent "1920x1080" to a vendor that has never accepted it.
#:
#: dall-e-2 offers only squares; dall-e-3 offers one square and two
#: 1024-on-the-short-side rectangles. gpt-image-1/1-mini/1.5/2 share a fixed
#: trio in the same shape as dall-e-3's, which is why they are not
#: distinguished below — see `_dalle_request_shape` for the one thing that
#: *does* distinguish them (`response_format`).
#:
#: gpt-image-* models also accept a custom "WxH" (both multiples of 16,
#: aspect ratio between 1:3 and 3:1, up to 3840x2160) which could serve a
#: 16:9 request closer to its true ratio than 1536x1024 manages. That path is
#: deliberately not implemented: its constraints are numerous and
#: interdependent enough that a rounding mistake produces a request rejected
#: for a reason far harder to diagnose than "picked the wrong fixed size",
#: and nothing here can be run against a real account to check it. The fixed
#: trio is documented, always valid, and good enough to unblock real use;
#: implement the custom-size path against a real vendor response, not a
#: guess, if it proves too coarse.
_DALLE2_SIZES = [(256, 256), (512, 512), (1024, 1024)]
_DALLE3_SIZES = [(1024, 1024), (1792, 1024), (1024, 1792)]
_GPT_IMAGE_SIZES = [(1024, 1024), (1536, 1024), (1024, 1536)]


#: Published per-image prices for the gpt-image family, by quality and size,
#: read off OpenAI's model reference. Keyed `(quality, "WIDTHxHEIGHT")`.
#:
#: ## Why a price table exists here at all
#:
#: `unit_cost_usd` was a single constructor default of $0.04 used for three
#: things at once: routing, the pre-dispatch budget refusal, and the cost
#: written to the ledger and the trace. For a 16:9 shot the real price is
#: between $0.016 and $0.25 depending on a `quality` field — a **fifteen-fold
#: spread** — so one number could not be right for more than one configuration,
#: and at high quality it under-reported by six times.
#:
#: An under-reported cost is worse than no cost: the per-shot ceiling is checked
#: against it, so a shot that "fits" a $0.25 budget at the declared $0.04 can
#: bill $0.25 and pass, and the trace built to answer "where did two dollars
#: go" would answer with thirty-two cents.
_GPT_IMAGE_PRICES: dict[tuple[str, str], float] = {
    ("low", "1024x1024"): 0.011,
    ("low", "1536x1024"): 0.016,
    ("low", "1024x1536"): 0.016,
    ("medium", "1024x1024"): 0.042,
    ("medium", "1536x1024"): 0.063,
    ("medium", "1024x1536"): 0.063,
    ("high", "1024x1024"): 0.167,
    ("high", "1536x1024"): 0.25,
    ("high", "1024x1536"): 0.25,
}

#: What to assume when `quality` is not configured and therefore not sent.
#:
#: The vendor's reference does not say which tier its default resolves to, and
#: this adapter has never been run against a real account to find out. So the
#: assumption is the **most expensive** one. Erring high makes the budget guard
#: refuse a shot it could have afforded; erring low makes it approve a shot that
#: bills six times the estimate. Only one of those two mistakes shows up on an
#: invoice.
UNKNOWN_QUALITY_ASSUMPTION = "high"


#: Our fidelity vocabulary to OpenAI's `quality` field.
#:
#: The mapping is here, in the adapter, and nowhere else. `VisualFidelity` is
#: the word the product and the user use; `low`/`medium`/`high` is one vendor's
#: spelling of it, and a second vendor with a different spelling needs a second
#: table in its own adapter rather than a change to the contract.
_FIDELITY_TO_QUALITY: dict[VisualFidelity, str] = {
    VisualFidelity.DRAFT: "low",
    VisualFidelity.STANDARD: "medium",
    VisualFidelity.FINE: "high",
}


def gpt_image_price(quality: str | None, size: str) -> float | None:
    """Published price for one image, or `None` for a model we have no table for.

    `None` is a real answer and the caller must handle it: a self-hosted
    OpenAI-compatible endpoint has whatever price its operator pays, and
    inventing one would be worse than falling back to the configured default.
    """
    tier = (quality or UNKNOWN_QUALITY_ASSUMPTION).strip().lower()
    return _GPT_IMAGE_PRICES.get((tier, size))


def _dalle_request_shape(model: str) -> tuple[bool, list[tuple[int, int]]]:
    """(sends `response_format`, accepted sizes) for the configured model.

    dall-e-2 and dall-e-3 default to returning a URL and must be told
    `response_format: b64_json` to return the bytes this adapter decodes.
    The gpt-image family has no such parameter — it always returns base64,
    and *rejects the request outright* if `response_format` is present at
    all, which is exactly the second of the three ways every image request
    was failing before this function existed.
    """
    lowered = model.lower()
    if lowered.startswith("dall-e-2"):
        return True, _DALLE2_SIZES
    if lowered.startswith("dall-e-3"):
        return True, _DALLE3_SIZES
    # Anything else configured — gpt-image-1, gpt-image-1-mini, gpt-image-1.5,
    # gpt-image-2, and whatever the vendor names next in the same family — is
    # treated as gpt-image-shaped rather than rejected outright: refusing an
    # unrecognised model name here would make adding a new model a code
    # change, which is exactly the rigidity `VTV_IMAGE_GENERATION_MODEL`
    # exists to avoid.
    return False, _GPT_IMAGE_SIZES


def _snap_size(width: int, height: int, sizes: list[tuple[int, int]]) -> str:
    """The accepted size closest in *aspect ratio* to what was requested.

    Not closest in area, and not the raw width/height clamped into range —
    the vendor does not accept an arbitrary size at all, so there is no
    "clamp" available; there is only a fixed menu to pick from. Choosing by
    aspect ratio rather than area is what makes a 16:9 project land on the
    widest landscape option a model offers rather than, say, a square that
    merely has the closest pixel count: a square 16:9 shot cropped to fit a
    scene loses the edges of the frame the Visual Director composed, while a
    landscape shot letterboxed loses nothing.
    """
    target = width / height
    return "x".join(
        str(v)
        for v in min(sizes, key=lambda wh: abs((wh[0] / wh[1]) - target))
    )


def _message_from(response: Any) -> str:
    """The vendor's own words about why it said no.

    Bounded, and falling back to the raw body, because an endpoint that answers
    with a bare string or an HTML error page is still telling us something.
    """
    try:
        payload = response.json()
    except Exception:
        return (getattr(response, "text", "") or "")[:200]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:200]
        if error:
            return str(error)[:200]
    return str(payload)[:200]


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
        #: Whether the endpoint accepts a `negative_prompt` body field.
        #:
        #: False by default because the contract this adapter speaks is
        #: OpenAI's, and OpenAI has no such field and rejects unknown ones. Some
        #: OpenAI-compatible servers (self-hosted diffusion frontends, mostly)
        #: do accept it; set this true for those. When false the negative prompt
        #: is folded into the prompt text rather than dropped, because what it
        #: asks for is worth asking for either way.
        supports_negative_prompt: bool = False,
        #: `low`, `medium` or `high`, or `None` to send nothing and let the
        #: vendor's default apply.
        #:
        #: Deliberately `None` by default, which changes nothing about the
        #: request this adapter has always sent. It is exposed because it is the
        #: largest cost lever in the product by a wide margin — the same 16:9
        #: image is $0.016 at `low` and $0.25 at `high` — and because a price
        #: nobody chose is a price nobody can predict.
        quality: str | None = None,
        #: Most images to have in flight at once. See `capabilities`.
        max_concurrency: int | None = 2,
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
        self.supports_negative_prompt = supports_negative_prompt
        self.quality = quality
        self.max_concurrency = max_concurrency
        self.timeout_seconds = timeout_seconds
        self._data_policy = data_policy or DataPolicy()
        self._failures = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model=self.model,
            # The declared price the router routes and refuses on. It has to be
            # the one we will actually be billed, or the budget guard is
            # guarding a number nobody is charged.
            unit_cost_usd=self.declared_unit_cost,
            unit="image",
            typical_latency_seconds=self.typical_latency_seconds,
            max_output_width=self.max_output_width,
            max_output_height=self.max_output_height,
            supports_seed=self.supports_seed,
            supports_style_reference=False,
            # Image endpoints have the tightest quotas of anything this system
            # calls, and a render sources several visuals at once. Two at a time
            # keeps the pipeline busy without asking for a 429 — which is worth
            # avoiding rather than merely surviving, because each one costs a
            # multi-second backoff on a shot somebody is waiting for.
            max_concurrency=self.max_concurrency,
            data_policy=self._data_policy,
        )

    def quality_for(self, fidelity: VisualFidelity | None) -> str | None:
        """The vendor word for a requested fidelity.

        A request that names a fidelity wins over the deployment's configured
        tier, and that direction is deliberate: the deployment setting is a
        *default* for projects that have not chosen, not a cap on the ones that
        have. An operator who needs a hard cap has the project budget, which is
        enforced in money rather than in tiers.
        """
        if fidelity is not None:
            return _FIDELITY_TO_QUALITY[fidelity]
        return self.quality

    def price_for(self, request: GenerationRequest) -> float:
        """:class:`~vtv.ports.ai.PricesRequests` — what this request will cost.

        The router calls this instead of reading `unit_cost_usd`, because the
        same shot is sixteen times dearer at `fine` than at `draft` and one
        declared number cannot be honest about both.
        """
        params = request.params
        fidelity = params.fidelity if isinstance(params, ImageParams) else None
        return self._dearest_at(self.quality_for(fidelity))

    def _dearest_at(self, quality: str | None) -> float:
        """The dearest this provider could charge for one image at a tier.

        The dearest, not an average: the router compares this against a shot's
        ceiling *before* dispatch, so a declared price below the real one turns
        the ceiling into a suggestion. Which size a given shot lands on depends
        on its aspect ratio, and the router has no shot in hand when it selects,
        so the worst case across the accepted sizes is the only figure that
        cannot mislead it.
        """
        if _dalle_request_shape(self.model)[0]:
            return self.unit_cost_usd
        prices = [
            price
            for width, height in _GPT_IMAGE_SIZES
            if (price := gpt_image_price(quality, f"{width}x{height}")) is not None
        ]
        return max(prices) if prices else self.unit_cost_usd

    @property
    def declared_unit_cost(self) -> float:
        """The price at the deployment's default tier.

        This is what goes in the capability record and what the budget planner
        multiplies by a shot count. It is *not* what the router refuses on any
        more — that is :meth:`price_for`, which knows the fidelity the request
        actually named.
        """
        return self._dearest_at(self.quality)

    def price_at(self, fidelity: VisualFidelity | None) -> float:
        """The price one image would cost at a given fidelity.

        For the budget planner, which decides how many shots may generate
        *before* any request exists to price.
        """
        return self._dearest_at(self.quality_for(fidelity))

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
        wants_response_format, accepted_sizes = _dalle_request_shape(self.model)
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": params.prompt,
            "n": params.count,
            "size": _snap_size(width, height, accepted_sizes),
        }
        if (quality := self.quality_for(params.fidelity)) is not None:
            body["quality"] = quality
        if wants_response_format:
            body["response_format"] = "b64_json"
        if params.negative_prompt:
            if self.supports_negative_prompt:
                body["negative_prompt"] = params.negative_prompt
            else:
                # OpenAI's images API has no negative-prompt field and rejects
                # unknown body parameters outright — a 400 on *every* call, for
                # every prompt, which is indistinguishable from a content
                # refusal unless you read the vendor's message. The Visual
                # Director attaches a negative prompt to every generated image
                # ("text, watermark, logos, distorted anatomy"), so this one
                # field made generation fail 100% of the time against OpenAI
                # while `/health` reported the capability as available.
                #
                # Folding it into the prompt is what the vendor's own guidance
                # suggests in the absence of a field, and it works against every
                # endpoint rather than only the ones with the field.
                body["prompt"] = (
                    f"{params.prompt}\n\nDo not include: {params.negative_prompt}."
                )
        if params.seed is not None and self.supports_seed:
            body["seed"] = params.seed

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    resolve_endpoint(self.endpoint, "/images/generations"),
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
            # The vendor's own number, when it sends one. It knows its quota
            # window and we are guessing at it, so its answer wins — the router
            # clamps it, because a header is data we did not write.
            raise ProviderError(
                "rate limited",
                code=ErrorCode.RATE_LIMITED,
                context={"retry_after": response.headers.get("retry-after")},
            )
        if response.status_code == 400:
            # Content policy refusals arrive as 400s. They are not retryable and
            # must degrade the shot rather than fail the project.
            #
            # So do rejected parameters, unknown models and unverified
            # organisations — and this used to discard the vendor's message, so
            # all four read as "the provider refused your picture". They need
            # completely different responses from the operator, and only the
            # vendor can tell them apart.
            raise ProviderRefused(
                f"image request was refused by the provider: {_message_from(response)}"
            )
        if response.status_code >= 400:
            self._failures += 1
            raise ProviderError(
                f"image provider returned {response.status_code}: "
                f"{_message_from(response)}"
            )

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
                    key=tenant_key(
                        request.organisation_id,
                        "projects",
                        request.project_id or "shared",
                        "generated",
                        f"{request.request_id}-{index}.png",
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
            # The published price for what was actually asked for, when this
            # is a model the table covers. `unit_cost_usd` remains the answer
            # for everything else — a self-hosted endpoint has whatever price
            # its operator pays, and inventing one would be worse than using
            # the number the deployment configured.
            cost_usd=round(
                self._price_each(body["size"], params.fidelity) * len(refs), 6
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
            seed=params.seed,
        )

    def _price_each(self, size: str, fidelity: VisualFidelity | None) -> float:
        if _dalle_request_shape(self.model)[0]:
            return self.unit_cost_usd
        published = gpt_image_price(self.quality_for(fidelity), size)
        return published if published is not None else self.unit_cost_usd


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
                    resolve_endpoint(self.endpoint, "/video/generations"),
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
                    f'{resolve_endpoint(self.endpoint, "/video/generations")}/{job_id}',
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
                        key=tenant_key(
                            request.organisation_id,
                            "projects",
                            request.project_id or "shared",
                            "generated",
                            f"{request.request_id}.mp4",
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
