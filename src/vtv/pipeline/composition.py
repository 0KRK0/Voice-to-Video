"""Stages 9 and 10 — Scene composition and the timeline.

The Director decided; this is where those decisions are executed and turned into
a fully-resolved `Timeline`.

The centre of this module is `_realise`, which walks a scene's fallback ladder.
Every rung that fails records a `DegradationStep` and the next is attempted. When
the ladder is exhausted the scene gets a placeholder — never a hole, never a
dropped span of narration (Rule 8).

Once a `Timeline` exists, every creative decision has been made. The renderer
that consumes it makes none.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vtv.contracts.asset import Asset, AssetKind, AssetSource
from vtv.contracts.base import Budget, TimeSpan
from vtv.contracts.consistency import BindingKind, VisualBible
from vtv.contracts.errors import (
    DegradationReason,
    DegradationStep,
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    Status,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    ImageParams,
    VideoParams,
    VisualFidelity,
)
from vtv.contracts.scene import Scene, SceneGraph
from vtv.contracts.timeline import (
    AssetClipSource,
    CaptionCue,
    FitPolicy,
    NarrationTrack,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    Timeline,
    Transition,
    TransitionKind,
    VisualClip,
)
from vtv.contracts.transcript import Transcript
from vtv.contracts.visual_language import CameraMotion, TypographySpec
from vtv.contracts.visual_plan import (
    ExistingAssetRequirements,
    ImageGenerationRequirements,
    LicensedMediaRequirements,
    ProgrammaticRequirements,
    SceneVisualPlan,
    VideoGenerationRequirements,
    VisualDirective,
    VisualPlan,
)
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.assets import AssetResolver
from vtv.pipeline.captions import CaptionBuilder

#: Reason recorded when a rung was skipped because nothing could serve it.
_REASON_BY_CODE: dict[ErrorCode, DegradationReason] = {
    ErrorCode.BUDGET_EXCEEDED: DegradationReason.BUDGET_EXCEEDED,
    ErrorCode.LATENCY_EXCEEDED: DegradationReason.LATENCY_EXCEEDED,
    ErrorCode.ASSET_LICENSE_UNACCEPTABLE: DegradationReason.LICENSE_UNACCEPTABLE,
    ErrorCode.ASSET_NOT_FOUND: DegradationReason.NO_SUITABLE_ASSET,
    ErrorCode.GENERATION_REFUSED: DegradationReason.SAFETY_REFUSED,
    # A tenant at their plan's spend limit is a budget outcome, not a provider
    # fault. Recorded as such so the storyboard can say "this shot is plainer
    # because the account is at its limit" rather than blaming a vendor.
    ErrorCode.QUOTA_EXCEEDED: DegradationReason.BUDGET_EXCEEDED,
}


def _unexpected(error: Exception) -> VTVError:
    """Wrap a failure nobody anticipated, keeping what it said.

    The type name is part of the message on purpose: `ValidationError` and
    `TypeError` from an adapter mean "our code disagreed with a vendor's
    response", which is a different investigation from a 500.
    """
    return VTVError(
        f"{type(error).__name__}: {error}",
        code=ErrorCode.INTERNAL_ERROR,
        user_message=(
            "We could not create this part of your video, so we used a simpler "
            "visual instead."
        ),
    )


#: The budget used when a scene has no plan at all. Zero, not unbounded: with no
#: plan there is no authorised spend for this shot, and the ladder below is empty
#: anyway, so this value denies rather than invents a ceiling.
_NO_PLAN_BUDGET = Budget(max_cost_usd=0.0)


@dataclass
class CompositionResult:
    timeline: Timeline
    assets: list[Asset] = field(default_factory=list)


@dataclass
class SceneComposer:
    """Executes a visual plan and assembles the timeline."""

    storage: object  # StorageProvider
    events: EventSink
    router: object | None = None  # GenerationRouter
    #: Typed, not `object`. It was `object` with a `# type: ignore[attr-defined]`
    #: on the one call below, and that pairing hid a missing argument: the call
    #: omitted `organisation_id`, which `AssetResolver.resolve` requires, so the
    #: licensed-media rung raised `TypeError` — not a `VTVError`, so `_realise`'s
    #: handler did not catch it and the whole composition failed rather than the
    #: ladder descending. Every test that reached this rung used a double
    #: declared `async def resolve(self, **_: object)`, which accepts anything,
    #: so nothing ever noticed. Naming the real type is what makes the type
    #: checker read the call site, and `tests/test_asset_resolution.py` now
    #: drives this rung through the real resolver.
    asset_resolver: AssetResolver | None = None
    captions: CaptionBuilder = field(default_factory=CaptionBuilder)

    async def compose(
        self,
        *,
        scene_graph: SceneGraph,
        visual_plan: VisualPlan,
        transcript: Transcript,
        narration: NarrationTrack,
        #: The project's chosen visual fidelity. `None` means the deployment's
        #: configured tier, which is what every render did before projects could
        #: choose — and is never the *more expensive* answer, so a caller that
        #: forgets under-delivers rather than over-bills.
        fidelity: VisualFidelity | None = None,
        visual_bible: VisualBible | None = None,
    ) -> CompositionResult:
        """Execute the plan.

        `visual_bible` is P1-1. Before it, the Bible was built one stage
        earlier, reviewed, attached to the result — and then never consulted
        again. Composition searched for a fresh asset for every appearance of
        the same entity and the renderer picked colours off a categorical
        wheel, so "the same thing looks the same each time it appears" was
        true of the data structure and false of the video.

        Two effects, both visible in the output:

        * an entity with a bound asset reuses **that stored object**, which
          means identical pixels, no second licence check and no second fetch;
        * the colour lock travels onto the Timeline, so the renderer draws a
          bound entity in its bound colour.
        """
        clips: list[VisualClip] = []
        assets: list[Asset] = []
        duration = narration.duration_seconds

        for index, scene in enumerate(scene_graph.scenes):
            plan = visual_plan.plan_for(scene.scene_id)
            span = _clamp(scene.span, duration)
            if span is None:
                continue
            clip, produced = await self._realise(
                scene,
                plan,
                span,
                organisation_id=scene_graph.organisation_id,
                project_id=scene_graph.project_id,
                fidelity=fidelity,
                visual_bible=visual_bible,
            )
            if index > 0:
                clip = clip.model_copy(
                    update={
                        "transition_in": Transition(
                            kind=TransitionKind.DISSOLVE, duration_seconds=0.4
                        )
                    }
                )
            clips.append(clip)
            assets.extend(produced)

        clips = _make_contiguous(clips, duration)
        cues: list[CaptionCue] = (
            self.captions.build(transcript, limit=duration)
            if scene_graph.style.captions_enabled
            else []
        )

        timeline = Timeline(
            entity_colours=dict(visual_bible.palette.reserved)
            if visual_bible is not None
            else {},
            organisation_id=scene_graph.organisation_id,
            project_id=scene_graph.project_id,
            scene_graph_id=scene_graph.scene_graph_id,
            narration=narration,
            clips=clips,
            captions=cues,
            style=scene_graph.style,
            aspect_ratio=scene_graph.style.aspect_ratio,
            status=Status.READY,
        )

        self.events.emit(
            EventName.TIMELINE_CREATED,
            project_id=scene_graph.project_id,
            data={
                "clips": len(clips),
                "captions": len(cues),
                "placeholders": timeline.placeholder_count,
                "degraded": sum(1 for clip in clips if clip.degradation),
                "gaps": len(timeline.coverage_gaps()),
                "duration_seconds": round(duration, 3),
            },
        )
        return CompositionResult(timeline=timeline, assets=assets)

    # -- one scene --------------------------------------------------------

    async def realise(
        self,
        scene: Scene,
        plan: SceneVisualPlan | None,
        span: TimeSpan,
        *,
        organisation_id: str,
        project_id: str,
        fidelity: VisualFidelity | None = None,
        #: The photograph the selector judged best for this shot, if it judged.
        chosen_media: object | None = None,
        visual_bible: VisualBible | None = None,
    ) -> tuple[VisualClip, list[Asset]]:
        """Public entry to the ladder, for callers outside a full compose.

        The Studio regenerates one visual at a time and needs exactly this —
        walk a ladder, take the first rung that works, record what was given up
        on the way. Exposing the existing method is deliberately all this does:
        a second ladder executor written for the product lane would be a second
        set of rules to keep in step with the pipeline lane's, and the two would
        diverge the first time one of them was fixed.
        """
        return await self._realise(
            scene,
            plan,
            span,
            organisation_id=organisation_id,
            project_id=project_id,
            fidelity=fidelity,
            chosen_media=chosen_media,
            visual_bible=visual_bible,
        )

    async def _realise(
        self,
        scene: Scene,
        plan: SceneVisualPlan | None,
        span: TimeSpan,
        *,
        organisation_id: str,
        project_id: str,
        fidelity: VisualFidelity | None = None,
        #: The photograph the selector judged best for this shot, if it judged.
        chosen_media: object | None = None,
        visual_bible: VisualBible | None = None,
    ) -> tuple[VisualClip, list[Asset]]:
        """Walk the ladder until something works, recording every step down."""
        degradation: list[DegradationStep] = []
        assets: list[Asset] = []

        ladder = plan.ladder if plan else []
        # The Director computed a per-scene ceiling — importance-weighted, with a
        # latency bound — and until now nothing read it. Every paid attempt for
        # this scene is made under it, so an expensive rung is refused by the
        # router and the ladder descends, instead of the shot quietly costing
        # whatever the provider felt like charging.
        budget = plan.budget if plan else _NO_PLAN_BUDGET
        for position, directive in enumerate(ladder):
            try:
                clip, produced = await self._attempt(
                    scene,
                    directive,
                    span,
                    organisation_id=organisation_id,
                    project_id=project_id,
                    budget=budget,
                    fidelity=fidelity,
                    chosen_media=chosen_media,
                    visual_bible=visual_bible,
                )
            except Exception as raised:
                # `Exception`, not `VTVError`. The ladder exists to descend when
                # a rung fails, and it could only descend on failures we had
                # anticipated well enough to wrap. Everything below this line is
                # a vendor's HTTP response being parsed, and a
                # `ValidationError`, a `KeyError` or a `TypeError` from that
                # parsing used to travel straight past here and kill the render
                # job — which is the loudest possible reaction to one shot being
                # unavailable, and the least useful.
                #
                # Two have actually happened: a missing `organisation_id`
                # argument (`TypeError`) and a Wikimedia scan too wide for
                # `AssetDimensions` (`ValidationError`). Neither was a reason to
                # lose a video.
                error = raised if isinstance(raised, VTVError) else _unexpected(raised)
                reason = _REASON_BY_CODE.get(
                    error.info.code, DegradationReason.PROVIDER_FAILED
                )
                next_strategy = (
                    ladder[position + 1].strategy.value
                    if position + 1 < len(ladder)
                    else "placeholder"
                )
                degradation.append(
                    DegradationStep(
                        from_strategy=directive.strategy.value,
                        to_strategy=next_strategy,
                        reason=reason,
                        error=error.info,
                    )
                )
                self.events.emit(
                    EventName.DEGRADED,
                    project_id=project_id,
                    scene_id=scene.scene_id,
                    data={
                        "from": directive.strategy.value,
                        "to": next_strategy,
                        "reason": reason.value,
                        # What the vendor actually said. Without these two the
                        # log reads `provider_failed` and the operator has to
                        # guess between a revoked key, a model the account
                        # cannot reach, a rejected parameter and an outage.
                        "code": error.info.code.value,
                        "detail": error.info.message[:300],
                    },
                )
                continue

            if clip is None:
                continue
            assets.extend(produced)
            return (
                clip.model_copy(update={"degradation": degradation[:6]}),
                assets,
            )

        # Ladder exhausted. A placeholder keeps the narration audible and the
        # project renderable, and marks the shot for the user's attention.
        return (
            VisualClip(
                scene_id=scene.scene_id,
                span=span,
                source=PlaceholderClipSource(
                    message="Visual unavailable",
                    error=ErrorInfo(
                        code=ErrorCode.ASSET_NOT_FOUND,
                        category=ErrorCategory.NOT_FOUND,
                        message="every visual strategy for this scene was exhausted",
                        user_message="We could not create a visual for this part.",
                    ),
                ),
                fit=FitPolicy.STILL,
                degradation=degradation[:6],
            ),
            assets,
        )

    async def _attempt(
        self,
        scene: Scene,
        directive: VisualDirective,
        span: TimeSpan,
        *,
        organisation_id: str,
        project_id: str,
        budget: Budget,
        fidelity: VisualFidelity | None = None,
        chosen_media: object | None = None,
        visual_bible: VisualBible | None = None,
    ) -> tuple[VisualClip | None, list[Asset]]:
        """Execute one rung.

        ``budget`` has no default on purpose. A default would let a new rung —
        or a new caller — be written without one and spend unbounded, which is
        the defect this parameter exists to close; making it required means the
        omission is a TypeError at import time rather than a line on an invoice.
        """
        requirements = directive.requirements

        if isinstance(requirements, ProgrammaticRequirements):
            # Nothing to fetch and nothing to pay for: the spec travels into the
            # timeline and the renderer draws it. This is why a typography
            # fallback can be relied on never to fail.
            return (
                VisualClip(
                    scene_id=scene.scene_id,
                    span=span,
                    source=ProgrammaticClipSource(spec=requirements.spec),
                    fit=FitPolicy.STILL,
                ),
                [],
            )

        if isinstance(requirements, LicensedMediaRequirements | ExistingAssetRequirements):
            if self.asset_resolver is None:
                raise VTVError(
                    "no asset resolver configured",
                    code=ErrorCode.ASSET_NOT_FOUND,
                )
            # P1-1. A bound entity reuses the object already chosen for it.
            # This is the half of consistency that a search cannot provide:
            # two searches for "Bell Labs" return two different photographs,
            # and the viewer notices.
            bound = _bound_clip(visual_bible, scene, span, requirements)
            if bound is not None:
                return bound, []

            search = (
                requirements
                if isinstance(requirements, LicensedMediaRequirements)
                else LicensedMediaRequirements(
                    query=requirements.query or "",
                    kind=requirements.kind,
                    camera_motion=requirements.camera_motion,
                )
            )
            asset = await self.asset_resolver.resolve(
                organisation_id=organisation_id,
                project_id=project_id,
                scene_id=scene.scene_id,
                requirements=search,
                chosen=chosen_media,  # type: ignore[arg-type]
            )
            if asset is None or asset.object is None:
                raise VTVError(
                    "no usable asset found", code=ErrorCode.ASSET_NOT_FOUND
                )
            if not asset.is_commercially_usable:
                raise VTVError(
                    "asset licence does not permit use",
                    code=ErrorCode.ASSET_LICENSE_UNACCEPTABLE,
                )
            return (
                VisualClip(
                    scene_id=scene.scene_id,
                    span=span,
                    source=AssetClipSource(
                        asset_id=asset.asset_id,
                        object=asset.object,
                        attribution=asset.attribution_line(),
                    ),
                    fit=FitPolicy.STILL,
                    camera_motion=requirements.camera_motion,
                ),
                [asset],
            )

        if isinstance(requirements, ImageGenerationRequirements):
            if self.router is None:
                raise VTVError(
                    "no generation router configured",
                    code=ErrorCode.PROVIDER_UNAVAILABLE,
                )
            request = GenerationRequest(
                organisation_id=organisation_id,
                project_id=project_id,
                scene_id=scene.scene_id,
                kind=GenerationKind.IMAGE,
                params=ImageParams(
                    prompt=requirements.prompt,
                    negative_prompt=requirements.negative_prompt,
                    aspect_ratio=requirements.aspect_ratio,
                    fidelity=fidelity,
                ),
                budget=budget,
            )
            result = await self.router.generate(request)  # type: ignore[attr-defined]
            if not result.outputs:
                raise VTVError("generation returned no image", code=ErrorCode.GENERATION_FAILED)
            asset = Asset(
                kind=AssetKind.IMAGE,
                source=AssetSource.GENERATED,
                generation_id=result.result_id,
                object=result.outputs[0],
                description=requirements.prompt[:1000],
                status=Status.READY,
            )
            return (
                VisualClip(
                    scene_id=scene.scene_id,
                    span=span,
                    source=AssetClipSource(
                        asset_id=asset.asset_id,
                        object=result.outputs[0],
                        illustrative_label=requirements.depicts_reality,
                    ),
                    fit=FitPolicy.STILL,
                    camera_motion=requirements.camera_motion,
                ),
                [asset],
            )

        if isinstance(requirements, VideoGenerationRequirements):
            if self.router is None:
                raise VTVError(
                    "no generation router configured",
                    code=ErrorCode.PROVIDER_UNAVAILABLE,
                )
            request = GenerationRequest(
                organisation_id=organisation_id,
                project_id=project_id,
                scene_id=scene.scene_id,
                kind=GenerationKind.VIDEO,
                params=VideoParams(
                    prompt=requirements.prompt,
                    negative_prompt=requirements.negative_prompt,
                    aspect_ratio=requirements.aspect_ratio,
                    duration_seconds=requirements.duration_seconds,
                    fidelity=fidelity,
                ),
                budget=budget,
            )
            result = await self.router.generate(request)  # type: ignore[attr-defined]
            if not result.outputs:
                raise VTVError("generation returned no video", code=ErrorCode.GENERATION_FAILED)
            asset = Asset(
                kind=AssetKind.VIDEO,
                source=AssetSource.GENERATED,
                generation_id=result.result_id,
                object=result.outputs[0],
                description=requirements.prompt[:1000],
                status=Status.READY,
            )
            # The clip is almost never exactly the requested length, which is
            # precisely what FitPolicy exists for.
            media = requirements.duration_seconds
            fit = FitPolicy.TRIM if media >= span.duration else FitPolicy.HOLD_LAST
            return (
                VisualClip(
                    scene_id=scene.scene_id,
                    span=span,
                    source=AssetClipSource(
                        asset_id=asset.asset_id,
                        object=result.outputs[0],
                        illustrative_label=requirements.depicts_reality,
                    ),
                    fit=fit,
                    media_duration_seconds=media,
                ),
                [asset],
            )

        # Composite is defined in the contract but not yet executed. Raising
        # here is the honest behaviour: the ladder descends to something we can
        # actually render rather than a half-drawn shot.
        raise VTVError(
            f"strategy {directive.strategy.value} is not yet executable",
            code=ErrorCode.VISUAL_PLANNING_FAILED,
        )



def _bound_clip(
    visual_bible: VisualBible | None,
    scene: Scene,
    span: TimeSpan,
    requirements: LicensedMediaRequirements | ExistingAssetRequirements,
) -> VisualClip | None:
    """The clip a Visual Bible binding dictates, or ``None`` to search.

    Matching is by entity *name*, because that is what the Bible binds and what
    a viewer perceives as "the same thing". The scene's entity names are not on
    the scene itself, so the search query stands in for them — which is exactly
    what the Director wrote to describe the subject.

    Returns ``None`` for a binding without a stored object. A binding can exist
    as a colour or a label lock with no asset behind it, and inventing one would
    be worse than searching.
    """
    if visual_bible is None:
        return None

    query = getattr(requirements, "query", None) or ""
    candidates = [query, *getattr(requirements, "alternate_queries", [])]
    for name in candidates:
        if not name:
            continue
        binding = visual_bible.binding_for(name, kind=BindingKind.ASSET)
        if binding is None or binding.asset is None:
            continue
        visual_bible.note_use(name, scene.scene_id, kind=BindingKind.ASSET)
        return VisualClip(
            scene_id=scene.scene_id,
            span=span,
            source=AssetClipSource(
                asset_id=binding.asset_id or binding.binding_id,
                object=binding.asset,
                attribution=binding.note,
            ),
            fit=FitPolicy.STILL,
            camera_motion=requirements.camera_motion,
        )
    return None

def _clamp(span: TimeSpan, duration: float) -> TimeSpan | None:
    start = max(0.0, min(span.start, duration))
    end = max(0.0, min(span.end, duration))
    if end - start < 0.05:
        return None
    return TimeSpan.of(start, end)


def _make_contiguous(clips: list[VisualClip], duration: float) -> list[VisualClip]:
    """Close gaps between clips so no narration plays over nothing.

    Each clip is extended to meet the next; the last runs to the end of the
    recording. Gaps here would be silent failures — the video would render
    perfectly and simply show black while the speaker talks.
    """
    if not clips:
        return clips
    ordered = sorted(clips, key=lambda clip: clip.span.start)
    adjusted: list[VisualClip] = []
    for index, clip in enumerate(ordered):
        start = 0.0 if index == 0 else adjusted[-1].span.end
        end = duration if index == len(ordered) - 1 else max(clip.span.end, start + 0.2)
        end = min(end, duration)
        if end - start < 0.05:
            continue
        adjusted.append(clip.model_copy(update={"span": TimeSpan.of(start, end)}))
    return adjusted


def placeholder_clip(scene: Scene, span: TimeSpan, message: str) -> VisualClip:
    """A controlled failure state, used by the editor when a user clears a shot."""
    return VisualClip(
        scene_id=scene.scene_id,
        span=span,
        source=PlaceholderClipSource(message=message),
        fit=FitPolicy.STILL,
    )


def typography_clip(scene: Scene, span: TimeSpan, headline: str) -> VisualClip:
    """The universal safety net: type, drawn at render time, cannot fail."""
    return VisualClip(
        scene_id=scene.scene_id,
        span=span,
        source=ProgrammaticClipSource(
            spec=TypographySpec(
                headline=headline[:120], preferred_duration=max(1.5, span.duration)
            )
        ),
        fit=FitPolicy.STILL,
        camera_motion=CameraMotion.NONE,
    )


__all__ = [
    "CompositionResult",
    "SceneComposer",
    "placeholder_clip",
    "typography_clip",
]
