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

Every request also carries a *bounded* budget, and that is enforced here rather
than trusted to callers. It used to be optional in practice: `budget` defaulted
to `Budget()`, whose `max_cost_usd` is None, and both of the router's budget
checks are `is not None`-guarded, so a request built without one could not be
refused however much a provider charged for it. The composer built exactly such
requests on the golden path. The rule now lives on the object — see
:data:`DEFAULT_MAX_COST_USD` — because a rule that lives at a call site protects
only the call sites that existed when it was written.
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
from vtv.contracts.scale import MAX_SCRIPT_CHARS, MAX_SPEECH_SEGMENTS
from vtv.contracts.style import AspectRatio


class GenerationKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    TEXT = "text"
    SPEECH_TO_TEXT = "speech_to_text"
    SPEECH = "speech"


class VisualFidelity(str, Enum):
    """How good the picture needs to be, in the user's terms rather than a
    vendor's.

    ## Why not just pass the vendor's word through

    OpenAI calls these `low`, `medium` and `high`; other image APIs call them
    `standard`/`hd`, or a step count, or nothing at all. Putting one vendor's
    vocabulary in the contract would mean either lying to the others or teaching
    every caller which vendor is configured — and the second is Rule 3 inverted.

    ## Why three, and why these

    Because there are three real prices, and they are fifteen times apart. For
    the 1536x1024 a 16:9 shot lands on, OpenAI publishes $0.016, $0.063 and
    $0.25. Any product decision here is a decision about money:

    * ``DRAFT`` — $0.016 a shot. Still 1.5 megapixels. A ten-minute video costs
      about $1.70. This is the default, and the right one for the great majority
      of work.
    * ``STANDARD`` — $0.063. Four times draft. Worth it where the picture is the
      point rather than the illustration.
    * ``FINE`` — $0.25. Sixteen times draft. An hour of video at this tier is
      about $163, which is why the budget planner exists.

    Note this is *fidelity*, not resolution: every tier renders at the same
    pixel dimensions. What rises is how much compute the vendor spends getting
    there.
    """

    DRAFT = "draft"
    STANDARD = "standard"
    FINE = "fine"


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
    #: How good this shot needs to be. `None` means the deployment's configured
    #: tier applies (`VTV_IMAGE_GENERATION_QUALITY`), which is what every
    #: request did before projects could choose.
    #:
    #: It lives on the *request* rather than only on the provider because it is
    #: a per-project decision that changes the price by up to sixteen times, and
    #: a price that varies per request cannot be declared once at construction
    #: and still be right — which is why the router asks the provider to price
    #: the request rather than reading a static capability.
    fidelity: VisualFidelity | None = None


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
    #: As :attr:`ImageParams.fidelity`. Carried here so a project that has asked
    #: for draft visuals is not quietly billed a vendor's default for the one
    #: rung that costs the most.
    fidelity: VisualFidelity | None = None


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


class SpeechParams(VTVModel):
    """Text to spoken audio — the inverse of transcription.

    Needed because the platform accepts input that has no voice: a PDF, a deck,
    a paragraph typed into a box. Those still have to become a video, and a
    video without narration has no clock (Rule 5).

    ``voice`` is a provider-neutral slug the adapter maps to its own catalogue.
    Business logic never names a vendor voice id, exactly as it never names a
    vendor model.
    """

    kind: Literal[GenerationKind.SPEECH] = GenerationKind.SPEECH
    #: The whole narration, joined. Not what any single request carries — since
    #: the caption-drift fix the adapter synthesises one sentence at a time from
    #: `segment_texts` — but the text this request is *about*, and therefore
    #: what its cache key hashes.
    text: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARS)
    #: BCP-47 tag. The provider is selected partly on whether it supports it,
    #: which is what makes eighteen-language narration a routing question rather
    #: than a rewrite.
    language: str = Field(default="en", max_length=32)
    voice: str | None = Field(default=None, max_length=64)
    #: 1.0 is the provider's natural pace.
    speaking_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    #: Segment boundaries in the source text, so a provider that can report
    #: word timings has something to align them against, and so a synthesiser
    #: that cannot still knows where the pauses belong.
    segment_texts: list[str] = Field(
        default_factory=list, max_length=MAX_SPEECH_SEGMENTS
    )
    sample_rate: int = Field(default=44100, ge=8000, le=48000)


GenerationParams = Annotated[
    ImageParams | VideoParams | TextParams | SpeechToTextParams | SpeechParams,
    Field(discriminator="kind"),
]


#: The most one call of each kind may cost when the caller states no ceiling of
#: its own. This is a *backstop*, not a pricing policy: each figure is roughly an
#: order of magnitude above what the configured providers charge today (an image
#: is $0.04, a second of video $0.25, transcription $0.006 a minute), so it never
#: changes which provider is chosen — it only bounds the blast radius of a
#: provider that charges far more than it advertises, or of a caller that forgot
#: to say what it was willing to pay.
#:
#: The alternative — refusing to construct a request with no budget — is the
#: stricter rule and was the first design. It was rejected because the callers
#: that build unbudgeted requests today (transcription, understanding, revision)
#: would then fail to *build* a request at all, and a contract change that turns
#: a live pipeline off is a change nobody lands. A conservative default fails
#: closed for those callers while the router still refuses anything that reaches
#: it unbounded, which is the case this table cannot cover: `model_copy`,
#: `model_construct` and hand-built stand-ins all skip validation.
DEFAULT_MAX_COST_USD: dict[GenerationKind, float] = {
    GenerationKind.IMAGE: 0.50,
    GenerationKind.VIDEO: 3.00,
    GenerationKind.TEXT: 1.00,
    GenerationKind.SPEECH_TO_TEXT: 2.00,
    GenerationKind.SPEECH: 2.00,
}

#: Used for a kind this build does not have a figure for. Failing closed: an
#: unrecognised kind is a new one somebody added without pricing it, and the
#: cheapest thing we sell is worth less than a dollar.
FALLBACK_MAX_COST_USD = 0.50


class GenerationRequest(RootDocument, Timestamped):
    """A unit of work for the generation router."""

    document_name = "generation_request"

    request_id: Id = Field(default_factory=lambda: new_id(IdPrefix.GENERATION))
    #: The tenant this work belongs to. Required, and *required to be correct*:
    #: it decides the storage namespace every object this produces is written
    #: to, and `LocalStorageProvider` refuses any key outside one. Optional
    #: ownership is what let the API read a null owner as "unowned, therefore
    #: yours" (P0-3); the same mistake in storage would put one customer's
    #: render beside another's.
    organisation_id: Id
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

    def trace_summary(self) -> dict[str, object]:
        """What was asked for, for the local provider trace.

        On the request rather than in the trace module, so a new
        `GenerationParams` variant is summarised once and every caller — the
        router, a test, a future CLI — sees the same thing. A summariser that
        lived in the observability layer would have to know every params class,
        which is the coupling the params classes exist to avoid.

        **This deliberately includes prompt text**, which is user content, and
        is why the trace it feeds is local-only, opt-in and refused in
        production. See `observability/trace.py`.
        """
        params = self.params
        out: dict[str, object] = {"kind": self.kind.value}

        prompt = getattr(params, "prompt", None)
        text = getattr(params, "text", None)
        instruction = getattr(params, "instruction", None)

        if prompt:
            out["prompt"] = str(prompt)[:2000]
            out["summary"] = str(prompt)
        elif text:
            out["characters"] = len(str(text))
            out["summary"] = str(text)
        elif instruction:
            out["instruction"] = str(instruction)[:400]
            payload = getattr(params, "input_json", None)
            if payload is not None:
                rendered = str(payload)
                out["input"] = rendered[:2000]
                out["summary"] = rendered

        for name in (
            "aspect_ratio", "duration_seconds", "count", "seed", "voice",
            "speaking_rate", "language", "negative_prompt", "response_schema",
            "max_output_tokens", "temperature",
        ):
            value = getattr(params, name, None)
            if value is None:
                continue
            out[name] = getattr(value, "value", value)

        segments = getattr(params, "segment_texts", None)
        if segments:
            out["segments"] = len(segments)
        return out

    @model_validator(mode="after")
    def _kind_matches_params(self) -> GenerationRequest:
        if self.params.kind is not self.kind:
            raise ValueError(
                f"request kind {self.kind.value} does not match params "
                f"{self.params.kind.value}"
            )
        return self

    @model_validator(mode="after")
    def _budget_is_bounded(self) -> GenerationRequest:
        """No request leaves validation with an open-ended cost ceiling.

        `Budget()` means "unbounded", and unbounded is the one value the router
        cannot enforce: both of its budget checks are `is not None`-guarded, so a
        None ceiling silently disables selection *and* the post-hoc overspend
        check. Filling it here means a caller that says nothing gets the
        backstop; a caller that states a real per-scene ceiling keeps it.

        The assignment re-enters this validator once (`validate_assignment` is
        on) and terminates immediately, because `max_cost_usd` is no longer None.
        """
        if self.budget.max_cost_usd is None:
            self.budget = self.budget.model_copy(
                update={
                    "max_cost_usd": DEFAULT_MAX_COST_USD.get(
                        self.kind, FALLBACK_MAX_COST_USD
                    )
                }
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
    "DEFAULT_MAX_COST_USD",
    "FALLBACK_MAX_COST_USD",
    "GenerationKind",
    "GenerationParams",
    "GenerationRequest",
    "GenerationResult",
    "ImageParams",
    "SpeechParams",
    "SpeechToTextParams",
    "TextParams",
    "TokenUsage",
    "VideoParams",
    "VisualFidelity",
]
