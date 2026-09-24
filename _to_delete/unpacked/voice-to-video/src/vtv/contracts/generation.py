"""Requests to, and results from, AI providers.

Business logic constructs a :class:`GenerationRequest` and receives a
:class:`GenerationResult`. It never learns which provider ran, unless it asks the
result. This indirection is the whole of Rule 3, and it buys three things that
matter commercially: we can switch providers when prices move, we can fall back
when one is down, and we can cache aggressively because every request has a
stable content-addressed key.

``cache_key`` is worth dwelling on. It hashes exactly the inputs that determine
the output — kind and parameters — and deliberately excludes the request id, the
timestamps, the scene it belongs to and the budget. Two scenes that need the same
image pay for it once. Regenerating a project after an unrelated edit costs
nothing for the shots that did not change. At scale this is the difference
between a viable margin and an unviable one (Section 28).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Budget,
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Timestamped,
    UsdAmount,
    VTVModel,
    new_id,
    stable_fingerprint,
)
from vtv.contracts.errors import ErrorInfo, Status
from vtv.contracts.style import AspectRatio


class GenerationKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    TEXT = "text"
    SPEECH_TO_TEXT = "speech_to_text"


class ImageParams(VTVModel):
    kind: Literal[GenerationKind.IMAGE] = GenerationKind.IMAGE
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str | None = Field(default=None, max_length=2000)
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    #: Pinning the seed makes a shot reproducible, which is what allows "keep
    #: this one, change the next" editing to behave predictably.
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    #: Object reference to an image whose style should be matched.
    style_reference: ObjectRef | None = None
    count: int = Field(default=1, ge=1, le=4)


class VideoParams(VTVModel):
    kind: Literal[GenerationKind.VIDEO] = GenerationKind.VIDEO
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str | None = Field(default=None, max_length=2000)
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    duration_seconds: float = Field(default=5.0, gt=0, le=30.0)
    #: Animate from a still we already have, rather than from text alone. Almost
    #: always cheaper, faster and more controllable than pure text-to-video.
    init_image: ObjectRef | None = None
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)


class TextParams(VTVModel):
    """A structured-output request to a language model.

    Note what is *not* here: no message list, no role juggling, no provider
    formatting. Callers state an instruction, supply structured input, and name
    the schema the answer must satisfy. The adapter is responsible for turning
    that into whatever shape its provider wants, and for rejecting a response
    that does not validate.
    """

    kind: Literal[GenerationKind.TEXT] = GenerationKind.TEXT
    instruction: str = Field(min_length=1, max_length=20000)
    input_json: dict[str, Any] = Field(default_factory=dict)
    #: Name of the contract the response must validate against, e.g.
    #: ``"understanding"``. The adapter looks it up in the exported schemas.
    response_schema: str | None = Field(default=None, max_length=64)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, ge=1, le=200000)


class SpeechToTextParams(VTVModel):
    kind: Literal[GenerationKind.SPEECH_TO_TEXT] = GenerationKind.SPEECH_TO_TEXT
    audio: ObjectRef
    language_hint: str | None = Field(default=None, max_length=16)
    word_timestamps: bool = True
    #: Domain vocabulary that improves recognition of names and jargon.
    vocabulary: list[str] = Field(default_factory=list, max_length=64)


GenerationParams = Annotated[
    ImageParams | VideoParams | TextParams | SpeechToTextParams,
    Field(discriminator="kind"),
]


class GenerationRequest(RootDocument, Timestamped):
    """A unit of work for the generation router."""

    document_name = "generation_request"

    request_id: Id = Field(default_factory=lambda: new_id(IdPrefix.GENERATION))
    project_id: Id | None = None
    #: What this is for, so cost can be attributed back to a shot.
    scene_id: Id | None = None

    kind: GenerationKind
    params: GenerationParams
    budget: Budget = Field(default_factory=Budget)

    #: A preferred provider, honoured only if it is healthy and within budget.
    #: Used by evaluation runs and by user-visible quality settings, never as a
    #: way for business logic to hard-code a vendor.
    provider_hint: str | None = Field(default=None, max_length=64)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _kind_matches_params(self) -> GenerationRequest:
        if self.params.kind is not self.kind:
            raise ValueError(
                f"request kind {self.kind.value} does not match params "
                f"{self.params.kind.value}"
            )
        return self

    def cache_key(self) -> str:
        """Content-addressed key over the inputs that determine the output.

        Excludes identifiers, timestamps, budgets and provider hints so that the
        same creative request hashes identically wherever it comes from.
        """
        return stable_fingerprint(
            {
                "v": self.schema_version,
                "kind": self.kind.value,
                "params": self.params.model_dump(mode="json", exclude_none=True),
            }
        )


class TokenUsage(VTVModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class GenerationResult(RootDocument, Timestamped):
    """The outcome of one generation request.

    Cost and latency are recorded on every result, successful or not. Without
    that, the cost optimisation work in Stage 17 has no data to optimise against
    and the Director's estimates can never be calibrated against reality.
    """

    document_name = "generation_result"

    result_id: Id = Field(default_factory=lambda: new_id(IdPrefix.GENERATION))
    request_id: Id
    cache_key: str = Field(min_length=64, max_length=64)

    status: Status = Status.PENDING
    #: Provider and model identity as *data*. Recorded for reproducibility,
    #: billing and evaluation; never branched on by business logic.
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)

    #: Binary outputs, if any: images, video files, audio.
    outputs: list[ObjectRef] = Field(default_factory=list, max_length=8)
    #: Structured output for text generation, already validated by the adapter
    #: against the requested response schema.
    structured_output: dict[str, Any] | None = None

    cost_usd: UsdAmount = 0.0
    latency_ms: int = Field(default=0, ge=0)
    tokens: TokenUsage | None = None
    seed: int | None = Field(default=None, ge=0)
    attempt: int = Field(default=1, ge=1)
    #: True when this result was served from cache. Cached results still record
    #: the original provider and model but cost nothing.
    from_cache: bool = False

    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _result_is_coherent(self) -> GenerationResult:
        if self.status is Status.READY:
            if not self.outputs and self.structured_output is None:
                raise ValueError("a READY result must carry outputs")
            if self.provider is None:
                raise ValueError("a READY result must record its provider")
        if self.status is Status.FAILED and self.error is None:
            raise ValueError("a FAILED result must carry an ErrorInfo")
        if self.from_cache and self.cost_usd > 0:
            raise ValueError("a cached result must not be charged for")
        return self


__all__ = [
    "GenerationKind",
    "GenerationParams",
    "GenerationRequest",
    "GenerationResult",
    "ImageParams",
    "SpeechToTextParams",
    "TextParams",
    "TokenUsage",
    "VideoParams",
]
