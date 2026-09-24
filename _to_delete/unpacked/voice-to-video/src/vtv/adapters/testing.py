"""Deterministic stub providers, for tests only.

**These are stubs. They are never registered by the application wiring, and
nothing here should ever run in front of a user.** Their sole purpose is to let
the success path of the pipeline be exercised without network access or
credentials, so that behaviour like caching, retry, fallback and budget
enforcement can be tested at all.

Three rules keep them honest:

1. Every provider name is prefixed ``stub-``, and that name is recorded on every
   `GenerationResult`, so a stubbed artefact is identifiable in the database
   forever.
2. Images they produce carry a visible "STUB" label burnt into the frame. A
   placeholder that looks like a real generation is exactly the kind of fake
   success this project refuses to ship.
3. `FailingProvider` exists alongside them, because a test suite that only ever
   exercises success proves nothing about a system whose defining feature is
   graceful degradation.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from vtv.contracts.base import RetentionClass
from vtv.contracts.errors import ProviderError, Status, VTVError
from vtv.contracts.generation import (
    GenerationRequest,
    GenerationResult,
    ImageParams,
    TextParams,
    TokenUsage,
    VideoParams,
)
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth

_LOCAL_POLICY = DataPolicy(
    retains_input=False,
    trains_on_input=False,
    retention_days=0,
    region="local",
    dpa_in_place=True,
)


def _seeded_colour(text: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Mid-range values only, so the label stays legible on every result.
    return (60 + digest[0] % 120, 60 + digest[1] % 120, 60 + digest[2] % 120)


def stub_image_bytes(prompt: str, width: int, height: int) -> bytes:
    """A clearly-labelled placeholder. Deterministic for a given prompt."""
    image = Image.new("RGB", (width, height), _seeded_colour(prompt))
    draw = ImageDraw.Draw(image)
    for index in range(0, height, max(8, height // 24)):
        shade = int(255 * (index / max(1, height)) * 0.25)
        draw.line([(0, index), (width, index)], fill=(shade, shade, shade), width=2)
    band = max(28, height // 12)
    draw.rectangle([0, 0, width, band], fill=(200, 40, 40))
    draw.text((12, band // 3), "STUB IMAGE — NOT A REAL GENERATION", fill=(255, 255, 255))
    draw.text((12, band + 12), prompt[:120], fill=(240, 240, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class StubImageGenerationProvider:
    """Produces a labelled placeholder image. Test wiring only."""

    storage: object
    name: str = "stub-image"
    unit_cost_usd: float = 0.01
    calls: int = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model="stub",
            unit_cost_usd=self.unit_cost_usd,
            unit="image",
            typical_latency_seconds=0.05,
            max_output_width=1920,
            max_output_height=1920,
            supports_seed=True,
            data_policy=_LOCAL_POLICY,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate_image(self, request: GenerationRequest) -> GenerationResult:
        params = request.params
        if not isinstance(params, ImageParams):
            raise VTVError("image request carried the wrong params")
        self.calls += 1
        width, height = params.aspect_ratio.dimensions_1080
        data = stub_image_bytes(params.prompt, width // 2, height // 2)
        ref = await self.storage.put(  # type: ignore[attr-defined]
            key=f"projects/{request.project_id or 'shared'}/generated/{request.request_id}.png",
            data=data,
            content_type="image/png",
            retention=RetentionClass.PROJECT,
        )
        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model="stub",
            outputs=[ref],
            cost_usd=self.unit_cost_usd,
            latency_ms=50,
            seed=params.seed,
        )


@dataclass
class StubVideoGenerationProvider:
    """Produces a short silent clip from the stub image. Test wiring only."""

    storage: object
    name: str = "stub-video"
    unit_cost_usd: float = 0.20
    calls: int = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model="stub",
            unit_cost_usd=self.unit_cost_usd,
            unit="second of video",
            typical_latency_seconds=0.5,
            max_duration_seconds=10.0,
            data_policy=_LOCAL_POLICY,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate_video(self, request: GenerationRequest) -> GenerationResult:
        import subprocess
        import tempfile
        from pathlib import Path

        from vtv.adapters.media import ffmpeg

        params = request.params
        if not isinstance(params, VideoParams):
            raise VTVError("video request carried the wrong params")
        self.calls += 1
        width, height = params.aspect_ratio.dimensions_1080
        width, height = width // 2, height // 2

        with tempfile.TemporaryDirectory(prefix="vtv-stub-video-") as directory:
            still = Path(directory) / "frame.png"
            still.write_bytes(stub_image_bytes(params.prompt, width, height))
            clip = Path(directory) / "clip.mp4"
            subprocess.run(
                [
                    ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-loop", "1", "-i", str(still),
                    "-t", f"{params.duration_seconds:.2f}",
                    "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(clip),
                ],
                check=True,
                capture_output=True,
            )
            ref = await self.storage.put(  # type: ignore[attr-defined]
                key=f"projects/{request.project_id or 'shared'}/generated/{request.request_id}.mp4",
                data=clip.read_bytes(),
                content_type="video/mp4",
                retention=RetentionClass.PROJECT,
            )

        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model="stub",
            outputs=[ref],
            cost_usd=round(self.unit_cost_usd * params.duration_seconds, 6),
            latency_ms=500,
        )


@dataclass
class ScriptedTextGenerationProvider:
    """Returns a pre-supplied structured response. Test wiring only.

    Used to test the *validation* path — that a malformed or partial model
    response is repaired or rejected correctly — without a model.
    """

    responses: list[dict[str, object]] = field(default_factory=list)
    name: str = "stub-text"
    calls: int = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            model="stub",
            unit_cost_usd=0.0,
            unit="1k output tokens",
            typical_latency_seconds=0.01,
            supports_structured_output=True,
            data_policy=_LOCAL_POLICY,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if not isinstance(request.params, TextParams):
            raise VTVError("text request carried the wrong params")
        if not self.responses:
            raise ProviderError("scripted text provider has no responses left")
        payload = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model="stub",
            structured_output=dict(payload),
            tokens=TokenUsage(input_tokens=100, output_tokens=200),
            cost_usd=0.0,
            latency_ms=10,
        )


@dataclass
class FailingProvider:
    """Always fails. The most useful stub in the suite.

    A system whose defining feature is graceful degradation has to be tested
    against failure far more than against success.
    """

    kind_method: str = "generate_image"
    name: str = "stub-failing"
    error: VTVError | None = None
    calls: int = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            unit_cost_usd=0.0,
            typical_latency_seconds=0.01,
            data_policy=_LOCAL_POLICY,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    def __getattr__(self, item: str) -> object:
        if item == self.kind_method:

            async def call(request: GenerationRequest) -> GenerationResult:
                self.calls += 1
                raise self.error or ProviderError("stub provider always fails")

            return call
        raise AttributeError(item)


__all__ = [
    "FailingProvider",
    "ScriptedTextGenerationProvider",
    "StubImageGenerationProvider",
    "StubVideoGenerationProvider",
    "stub_image_bytes",
]
