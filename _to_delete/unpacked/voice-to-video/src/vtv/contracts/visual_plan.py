"""The Visual Director's output contract.

This is the most important contract in the system. Everything before it decides
*what the speaker meant*; everything after it merely executes. This file is where
meaning becomes a decision about images.

Three properties are non-negotiable.

**A plan is a decision, not a picture.** The Director returns structured intent.
It never fetches, never generates, never renders. That separation is what makes
the decision itself testable — we can evaluate whether "chart" was the right call
for a sentence about population growth without spending a cent on generation.

**A plan is provider-independent.** Nothing here names a vendor, a model or an
API. Providers are selected later by the generation router based on capability,
price and availability (Rule 3).

**A plan always has a way to fail gracefully.** ``fallbacks`` is not decoration:
it is the ordered ladder the system climbs down when generation fails, costs too
much or takes too long, so that one bad shot degrades to a simpler shot instead
of killing the project (Rule 8).

A note on the strategy vocabulary. The engineering brief lists eleven strategies:
``existing_asset, licensed_media, programmatic_animation, diagram, chart,
timeline, map, typography, generated_image, generated_video, composite``. Six of
those — diagram, chart, timeline, map, typography and most of what "animation"
means — are not different ways of *obtaining* a visual, they are different things
our own animation engine *draws*. Modelling them as sibling strategies would mean
the Director had to re-learn the same "draw it ourselves" decision six times over.
So the vocabulary is split in two: :class:`VisualStrategy` answers *how do we get
this visual*, and :class:`~vtv.contracts.visual_language.VisualPrimitive` answers
*what do we draw*. The eleven original strategies map onto that pair without loss;
the mapping is written out in ``docs/VISUAL_DIRECTOR.md``.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vtv.contracts.asset import AssetKind
from vtv.contracts.base import (
    Budget,
    Confidence,
    Duration,
    Id,
    IdPrefix,
    RootDocument,
    Timestamped,
    UsdAmount,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import Status
from vtv.contracts.style import AspectRatio
from vtv.contracts.visual_language import AnimationSpec, CameraMotion


class VisualStrategy(str, Enum):
    """How a visual is obtained.

    Listed in the order the Director should prefer them, all else being equal.
    All else is frequently not equal — a generated image can be the right answer
    for scene one — but the ordering encodes the default economic and quality
    posture described in ``docs/AI_PROVIDER_POLICY.md``.
    """

    #: Something we already hold: our library, the user's upload, or an asset
    #: already fetched earlier in this same project.
    EXISTING_ASSET = "existing_asset"
    #: Drawn by our own animation engine from an AnimationSpec. Deterministic,
    #: near-free, instant, always on-brand, and factually exact.
    PROGRAMMATIC = "programmatic"
    #: Retrieved from an open-licence or contracted media source.
    LICENSED_MEDIA = "licensed_media"
    #: Produced by an image generation model, usually with camera motion applied.
    GENERATED_IMAGE = "generated_image"
    #: Produced by a video generation model. The most expensive and slowest
    #: option, reserved for shots where motion itself carries the meaning.
    GENERATED_VIDEO = "generated_video"
    #: Several of the above layered into one shot.
    COMPOSITE = "composite"


class MediaSearchConstraints(VTVModel):
    """Non-negotiable constraints applied to every media search.

    These are defaults that the search adapter must honour, not preferences.
    Requiring commercial use and modification rights at *query* time means we
    never spend engineering effort or user attention on assets we could not have
    used anyway.
    """

    require_commercial_use: bool = True
    require_modification: bool = True
    exclude_share_alike: bool = False
    min_width: int = Field(default=1280, ge=1)
    min_height: int = Field(default=720, ge=1)


class ExistingAssetRequirements(VTVModel):
    """Reuse something we already have."""

    strategy: Literal[VisualStrategy.EXISTING_ASSET] = VisualStrategy.EXISTING_ASSET
    #: Bind directly to a known asset, e.g. when the user replaced a shot by hand.
    asset_id: Id | None = None
    #: Or search our own library semantically.
    query: str | None = Field(default=None, max_length=300)
    kind: AssetKind = AssetKind.IMAGE
    camera_motion: CameraMotion = CameraMotion.KEN_BURNS

    @model_validator(mode="after")
    def _need_one_selector(self) -> ExistingAssetRequirements:
        if not self.asset_id and not self.query:
            raise ValueError("provide either an asset_id or a query")
        return self


class ProgrammaticRequirements(VTVModel):
    """Draw it ourselves.

    The spec is validated data consumed by our renderer. No model-authored code
    is ever executed — see :mod:`vtv.contracts.visual_language`.
    """

    strategy: Literal[VisualStrategy.PROGRAMMATIC] = VisualStrategy.PROGRAMMATIC
    spec: AnimationSpec


class LicensedMediaRequirements(VTVModel):
    """Find real footage or photography we are allowed to use."""

    strategy: Literal[VisualStrategy.LICENSED_MEDIA] = VisualStrategy.LICENSED_MEDIA
    query: str = Field(min_length=2, max_length=300)
    #: Alternate phrasings tried in order if the first query returns nothing
    #: suitable. Cheap insurance against a single unlucky search.
    alternate_queries: list[str] = Field(default_factory=list, max_length=4)
    kind: AssetKind = AssetKind.IMAGE
    constraints: MediaSearchConstraints = Field(default_factory=MediaSearchConstraints)
    camera_motion: CameraMotion = CameraMotion.KEN_BURNS
    #: What the picture must actually contain for it to be correct. Used to
    #: reject plausible-but-wrong search results.
    must_depict: str | None = Field(default=None, max_length=300)


class ImageGenerationRequirements(VTVModel):
    """Generate a still image."""

    strategy: Literal[VisualStrategy.GENERATED_IMAGE] = VisualStrategy.GENERATED_IMAGE
    prompt: str = Field(min_length=8, max_length=2000)
    negative_prompt: str | None = Field(default=None, max_length=1000)
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    #: An asset whose look this image should match, to hold style across shots.
    style_reference_asset_id: Id | None = None
    camera_motion: CameraMotion = CameraMotion.ZOOM_IN
    #: Set when the image depicts something real. Generated imagery of real
    #: people, places or events must be labelled as illustrative in the finished
    #: video; this flag is what drives that label.
    depicts_reality: bool = False


class VideoGenerationRequirements(VTVModel):
    """Generate moving footage. The expensive option; justify it."""

    strategy: Literal[VisualStrategy.GENERATED_VIDEO] = VisualStrategy.GENERATED_VIDEO
    prompt: str = Field(min_length=8, max_length=2000)
    negative_prompt: str | None = Field(default=None, max_length=1000)
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    duration_seconds: Duration = Field(default=5.0, le=30.0)
    #: What the motion is doing, separately from what the frame contains. This
    #: is the only reason to choose video over a still with camera motion.
    motion_description: str | None = Field(default=None, max_length=500)
    depicts_reality: bool = False


#: Everything except composite. Layers of a composite draw from this set, which
#: makes unbounded nesting structurally impossible.
LayerRequirements = Annotated[
    ExistingAssetRequirements
    | ProgrammaticRequirements
    | LicensedMediaRequirements
    | ImageGenerationRequirements
    | VideoGenerationRequirements,
    Field(discriminator="strategy"),
]


class CompositeLayer(VTVModel):
    """One layer of a composite shot, back to front."""

    requirements: LayerRequirements
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    #: Fractional bounds within the frame: x, y, width, height in ``[0, 1]``.
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

    @model_validator(mode="after")
    def _bounds_in_frame(self) -> CompositeLayer:
        x, y, w, h = self.bounds
        if not all(0.0 <= v <= 1.0 for v in self.bounds):
            raise ValueError("composite bounds must be fractions in [0, 1]")
        if w <= 0 or h <= 0:
            raise ValueError("composite layer must have positive width and height")
        if x + w > 1.0 + 1e-6 or y + h > 1.0 + 1e-6:
            raise ValueError("composite layer extends beyond the frame")
        return self


class CompositeRequirements(VTVModel):
    """Layered shots: a licensed photograph under an animated annotation, a map
    with a chart in the corner. Often the most communicative option and rarely
    the most expensive one."""

    strategy: Literal[VisualStrategy.COMPOSITE] = VisualStrategy.COMPOSITE
    layers: list[CompositeLayer] = Field(min_length=2, max_length=4)


#: The full discriminated union of visual requirements.
VisualRequirements = Annotated[
    ExistingAssetRequirements
    | ProgrammaticRequirements
    | LicensedMediaRequirements
    | ImageGenerationRequirements
    | VideoGenerationRequirements
    | CompositeRequirements,
    Field(discriminator="strategy"),
]


class CostEstimate(VTVModel):
    """What the Director believes a directive will cost in money and time.

    Estimates are supplied by the generation router from provider capability
    data, not guessed by a language model. They are what make the trade-off in
    Section 28 of the brief an actual computation rather than a slogan.
    """

    usd: UsdAmount = 0.0
    latency_seconds: float = Field(default=0.0, ge=0.0)


class VisualDirective(VTVModel):
    """One way of realising a scene's visual, with its justification."""

    strategy: VisualStrategy
    requirements: VisualRequirements
    #: Why this serves the scene's visual brief. Written for a human reviewer,
    #: and the field the evaluation system grades for soundness.
    rationale: str = Field(min_length=4, max_length=600)
    estimate: CostEstimate = Field(default_factory=CostEstimate)
    confidence: Confidence = 0.5

    @model_validator(mode="after")
    def _strategy_matches_requirements(self) -> VisualDirective:
        if self.requirements.strategy is not self.strategy:
            raise ValueError(
                "directive strategy and requirements disagree: "
                f"{self.strategy.value} vs {self.requirements.strategy.value}"
            )
        return self


class SceneVisualPlan(VTVModel):
    """The Director's complete answer for one scene."""

    plan_id: Id = Field(default_factory=lambda: new_id(IdPrefix.VISUAL_PLAN))
    scene_id: Id

    primary: VisualDirective
    #: Ordered descent. Each entry must be cheaper or more reliable than the one
    #: before it, and the last is expected to be one we can always satisfy —
    #: in practice a typography shot, which never fails.
    fallbacks: list[VisualDirective] = Field(default_factory=list, max_length=4)

    budget: Budget = Field(default_factory=Budget)
    status: Status = Status.PENDING

    @model_validator(mode="after")
    def _fallbacks_are_distinct_and_terminating(self) -> SceneVisualPlan:
        seen = [self.primary.strategy]
        for directive in self.fallbacks:
            if directive.strategy in seen and directive.strategy not in {
                VisualStrategy.PROGRAMMATIC,
                VisualStrategy.LICENSED_MEDIA,
            }:
                raise ValueError(
                    f"fallback repeats strategy {directive.strategy.value} without "
                    "offering a different approach"
                )
            seen.append(directive.strategy)
        return self

    @property
    def ladder(self) -> list[VisualDirective]:
        """Primary followed by fallbacks, in the order they will be attempted."""
        return [self.primary, *self.fallbacks]

    @property
    def worst_case_estimate(self) -> CostEstimate:
        """Cost if every rung of the ladder is attempted and fails.

        Budgets are checked against this, not against the primary alone,
        otherwise a project can quietly cost several times its ceiling.
        """
        return CostEstimate(
            usd=sum(d.estimate.usd for d in self.ladder),
            latency_seconds=sum(d.estimate.latency_seconds for d in self.ladder),
        )


class VisualPlan(RootDocument, Timestamped):
    """Visual decisions for an entire project."""

    document_name = "visual_plan"

    visual_plan_id: Id = Field(default_factory=lambda: new_id(IdPrefix.VISUAL_PLAN))
    project_id: Id
    scene_graph_id: Id

    scene_plans: list[SceneVisualPlan] = Field(default_factory=list, max_length=400)
    #: Ceiling for the whole project, checked against the sum of primaries.
    budget: Budget = Field(default_factory=Budget)
    status: Status = Status.PENDING

    @model_validator(mode="after")
    def _one_plan_per_scene(self) -> VisualPlan:
        scene_ids = [plan.scene_id for plan in self.scene_plans]
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("each scene may have at most one visual plan")
        return self

    def plan_for(self, scene_id: str) -> SceneVisualPlan | None:
        return next((p for p in self.scene_plans if p.scene_id == scene_id), None)

    @property
    def expected_cost_usd(self) -> float:
        """Cost if every primary directive succeeds first time."""
        return sum(plan.primary.estimate.usd for plan in self.scene_plans)

    @property
    def worst_case_cost_usd(self) -> float:
        return sum(plan.worst_case_estimate.usd for plan in self.scene_plans)

    def strategy_mix(self) -> dict[str, int]:
        """How many scenes chose each strategy.

        A healthy explainer is mostly programmatic and licensed media with
        generation used sparingly. This one line is the cheapest early warning
        that the Director has started reaching for expensive options by reflex.
        """
        mix: dict[str, int] = {}
        for plan in self.scene_plans:
            key = plan.primary.strategy.value
            mix[key] = mix.get(key, 0) + 1
        return mix


__all__ = [
    "CompositeLayer",
    "CompositeRequirements",
    "CostEstimate",
    "ExistingAssetRequirements",
    "ImageGenerationRequirements",
    "LayerRequirements",
    "LicensedMediaRequirements",
    "MediaSearchConstraints",
    "ProgrammaticRequirements",
    "SceneVisualPlan",
    "VideoGenerationRequirements",
    "VisualDirective",
    "VisualPlan",
    "VisualRequirements",
    "VisualStrategy",
]
