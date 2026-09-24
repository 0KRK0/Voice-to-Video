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
from vtv.contracts.base import TimeSpan
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
from vtv.pipeline.captions import CaptionBuilder

#: Reason recorded when a rung was skipped because nothing could serve it.
_REASON_BY_CODE: dict[ErrorCode, DegradationReason] = {
    ErrorCode.BUDGET_EXCEEDED: DegradationReason.BUDGET_EXCEEDED,
    ErrorCode.LATENCY_EXCEEDED: DegradationReason.LATENCY_EXCEEDED,
    ErrorCode.ASSET_LICENSE_UNACCEPTABLE: DegradationReason.LICENSE_UNACCEPTABLE,
    ErrorCode.ASSET_NOT_FOUND: DegradationReason.NO_SUITABLE_ASSET,
    ErrorCode.GENERATION_REFUSED: DegradationReason.SAFETY_REFUSED,
}


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
    asset_resolver: object | None = None  # AssetResolver
    captions: CaptionBuilder = field(default_factory=CaptionBuilder)

    async def compose(
        self,
        *,
        scene_graph: SceneGraph,
        visual_plan: VisualPlan,
        transcript: Transcript,
        narration: NarrationTrack,
    ) -> CompositionResult:
        clips: list[VisualClip] = []
        assets: list[Asset] = []
        duration = narration.duration_seconds

        for index, scene in enumerate(scene_graph.scenes):
            plan = visual_plan.plan_for(scene.scene_id)
            span = _clamp(scene.span, duration)
            if span is None:
                continue
            clip, produced = await self._realise(scene, plan, span)
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

    async def _realise(
        self, scene: Scene, plan: SceneVisualPlan | None, span: TimeSpan
    ) -> tuple[VisualClip, list[Asset]]:
        """Walk the ladder until something works, recording every step down."""
        degradation: list[DegradationStep] = []
        assets: list[Asset] = []

        ladder = plan.ladder if plan else []
        for position, directive in enumerate(ladder):
            try:
                clip, produced = await self._attempt(scene, directive, span)
            except VTVError as error:
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
                    project_id=scene.scene_id and None,
                    scene_id=scene.scene_id,
                    data={
                        "from": directive.strategy.value,
                        "to": next_strategy,
                        "reason": reason.value,
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
        self, scene: Scene, directive: VisualDirective, span: TimeSpan
    ) -> tuple[VisualClip | None, list[Asset]]:
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
            search = (
                requirements
                if isinstance(requirements, LicensedMediaRequirements)
                else LicensedMediaRequirements(
                    query=requirements.query or "",
                    kind=requirements.kind,
                    camera_motion=requirements.camera_motion,
                )
            )
            asset = await self.asset_resolver.resolve(  # type: ignore[attr-defined]
                project_id=scene.scene_id and "",
                scene_id=scene.scene_id,
                requirements=search,
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
                project_id=None,
                scene_id=scene.scene_id,
                kind=GenerationKind.IMAGE,
                params=ImageParams(
                    prompt=requirements.prompt,
                    negative_prompt=requirements.negative_prompt,
                    aspect_ratio=requirements.aspect_ratio,
                ),
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
                scene_id=scene.scene_id,
                kind=GenerationKind.VIDEO,
                params=VideoParams(
                    prompt=requirements.prompt,
                    negative_prompt=requirements.negative_prompt,
                    aspect_ratio=requirements.aspect_ratio,
                    duration_seconds=requirements.duration_seconds,
                ),
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
