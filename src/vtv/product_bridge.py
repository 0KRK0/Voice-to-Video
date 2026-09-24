"""The gap between the two lanes, closed.

The voice pipeline (`vtv.pipeline.orchestrator.Pipeline`) and the product lane
(`vtv.api.product`) grew independently and write different documents. A
finished recording produces a `transcript`, a `scene_graph`, a `visual_plan`
and a `Timeline` — the pipeline's own vocabulary — while Studio, the editor a
user actually opens, reads `script`, `visual_units` and `edit_timeline`. Before
this module, nothing translated one vocabulary into the other, so a completed
voice recording — verified, rendered, a real MP4 on disk — opened onto an
empty project: `GET /script` 404, `GET /timeline` 404, nothing to look at,
nothing to edit. Speaking is the product's headline feature; this is what
makes its output reachable.

## What this is not

This is **not** a second planner. `VisualUnitPlanner` groups a script's blocks
into shots using signals — paragraph breaks, scene boundaries, a topic-shift
heuristic — that make sense when nothing else has planned anything yet. A
voice recording has already been all the way through the pipeline by the time
this module runs: the scenes exist, the Visual Director already decided what
each one shows, and the composer already fetched or drew it. Re-deriving a
grouping from the words alone and asking a provider to plan visuals again
would produce a *second, different* answer next to the one already rendered —
which is a worse gap than the one this closes. So the units built here are
one-per-scene, and each one's visual is read back from the render's own
`Timeline`, not re-planned.

## What is measured and what is approximate

**Measured, not estimated.** Every block's timing comes from the transcript
segment's own span — the whole advantage of the spoken path over pasting text
is that the timing already exists, so `ScriptBlock.timing_invalidated` is left
`False` and `measured_start`/`measured_end` are set directly. A `VisualUnit`'s
span is the span of the clip that actually rendered for its scene, not an
estimate laid end to end.

**Approximate, and said so here rather than left to be discovered.** The
render's `Timeline` carries one continuous narration file, not a clip per
scene, so the `EditTimeline`'s narration track is *reconstructed* as one clip
per unit, spanning the same time the unit's visual occupies. For a voice
recording this is exact — `SceneComposer.compose` always builds the render
`Timeline` with `narration_start_seconds` and `fill_seconds` at their zero
defaults, so the visual track already covers the narration exactly — but it is
a reconstruction, not a value read off the recording, and this paragraph is
the whole reason to say so.

## Idempotency

Nothing here is read-modify-write. Every call recomputes the script, the
units and the edit timeline from the pipeline's own documents and overwrites
whatever product-lane documents existed — the same pattern `persist_result`
already uses for the pipeline's own documents. Two calls against the same
`PipelineResult` produce the same block count, the same unit count and the
same clip count; only the opaque ids differ, because minting an id is the one
part of building any of these documents that is not a pure function of the
input.
"""

from __future__ import annotations

import json

#: Document kinds, imported from the product API so the two cannot drift — a
#: mismatched string here would silently write a document Studio never reads.
from vtv.api.product import SCRIPT_DOC, TIMELINE_DOC, UNITS_DOC
from vtv.contracts.project import Project
from vtv.contracts.scene import Scene
from vtv.contracts.script import BlockStatus, Script, ScriptBlock, ScriptOrigin
from vtv.contracts.timeline import (
    AssetClipSource,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    VisualClip,
)
from vtv.contracts.tracks import (
    ClipSourceKind,
    EditTimeline,
    TimelineClip,
    Track,
    TrackKind,
)
from vtv.contracts.transcript import Transcript
from vtv.contracts.visual_plan import SceneVisualPlan, VisualDirective, VisualStrategy
from vtv.contracts.visual_unit import VisualUnit, VisualUnitStatus, VisualVersion
from vtv.jobs import JobContext
from vtv.observability.events import EventName
from vtv.pipeline.orchestrator import PipelineResult


async def derive_product_documents(context: JobContext, result: PipelineResult) -> None:
    """Translate a finished voice recording into an editable Studio project.

    Called once, after the pipeline's own documents are already persisted —
    the whole point is that both lanes end up describing the same recording,
    so the pipeline's documents are the only input this reads. Nothing is
    fabricated: a transcript with no segments produces no script, and a
    project the pipeline could not build a scene graph, visual plan or
    timeline for produces no visual units and no edit timeline, and Studio
    shows its honest empty state rather than an invented one.
    """
    script = _script_from_transcript(result.transcript, result.project)
    if script is None:
        return

    await context.repository.put_document(
        project_id=result.project.project_id,
        kind=SCRIPT_DOC,
        document_id=script.script_id,
        payload=json.loads(script.model_dump_json()),
    )
    context.events.emit(
        EventName.SCRIPT_CREATED,
        project_id=result.project.project_id,
        data={"origin": ScriptOrigin.SPOKEN.value, "blocks": len(script.blocks)},
    )

    units, edit_timeline = _derive_visuals(script, result)
    if not units or edit_timeline is None:
        return

    await context.repository.put_document(
        project_id=result.project.project_id,
        kind=UNITS_DOC,
        document_id=UNITS_DOC,
        payload={"units": [json.loads(u.model_dump_json()) for u in units]},
    )
    context.events.emit(
        EventName.VISUAL_UNITS_PLANNED,
        project_id=result.project.project_id,
        data={"units": len(units), "source": "spoken_render"},
    )

    await context.repository.put_document(
        project_id=result.project.project_id,
        kind=TIMELINE_DOC,
        document_id=edit_timeline.edit_timeline_id,
        payload=json.loads(edit_timeline.model_dump_json()),
    )
    context.events.emit(
        EventName.TIMELINE_EDITED,
        project_id=result.project.project_id,
        data={
            "reason": "derived_from_render",
            "clips": sum(len(track.clips) for track in edit_timeline.tracks),
            "duration": edit_timeline.duration,
            "version": edit_timeline.version,
        },
    )


# ---------------------------------------------------------------------------
# The script: transcript segments become script blocks
# ---------------------------------------------------------------------------


def _script_from_transcript(transcript: Transcript | None, project: Project) -> Script | None:
    """One block per segment, timed from the segment's own measured span.

    No grouping happens here. `ScriptBlock` is a sentence-scale unit and a
    transcript segment already is one — a provider segments on pauses, which
    is close enough to what a speaker means by "a line" that inventing a
    second segmentation on top of the first would only introduce disagreement
    between the block boundaries and the timing they carry.
    """
    if transcript is None or not transcript.segments:
        return None

    source_text = transcript.text
    blocks: list[ScriptBlock] = []
    cursor = 0
    for segment in transcript.segments:
        text = segment.text.strip()
        if not text:
            continue
        # Recover this block's real position in `source_text`, which is built
        # by joining segment text with single spaces (`Transcript.text`). A
        # block that cannot be located keeps no source range rather than a
        # guessed one — the editor's "highlight the original passage" feature
        # simply does not light up for it.
        found = source_text.find(text, cursor)
        source_start = source_end = None
        if found != -1:
            source_start, source_end = found, found + len(text)
            cursor = source_end
        blocks.append(
            ScriptBlock(
                order=len(blocks),
                text=text,
                source_start=source_start,
                source_end=source_end,
                # An estimate field the type requires, filled with the
                # measured duration rather than a speaking-rate guess: there
                # is nothing to estimate when the real timing is already
                # known.
                estimated_seconds=round(segment.span.end - segment.span.start, 3),
                measured_start=round(segment.span.start, 3),
                measured_end=round(segment.span.end, 3),
                status=BlockStatus.DRAFT,
                timing_invalidated=False,
            )
        )

    if not blocks:
        return None

    try:
        return Script(
            organisation_id=project.organisation_id,
            project_id=project.project_id,
            origin=ScriptOrigin.SPOKEN,
            language=transcript.language,
            source_text=source_text,
            current_text=source_text,
            blocks=blocks,
            version=1,
        )
    except ValueError:
        # A schema disagreement here beats a broken job. The recording still
        # rendered; only the editable copy of it is missing.
        return None


# ---------------------------------------------------------------------------
# Visual units and the edit timeline: one per scene, read back from the render
# ---------------------------------------------------------------------------


def _derive_visuals(
    script: Script, result: PipelineResult
) -> tuple[list[VisualUnit], EditTimeline | None]:
    """One `VisualUnit` per scene, holding the visual the render actually used.

    The strategy and content on each unit's single version are read back from
    the rendered `Timeline`'s clip for that scene — including, when the
    ladder descended, which rung actually succeeded — rather than from the
    Director's first choice. A scene whose primary strategy failed and fell
    back to a licensed photograph shows up here as a licensed photograph,
    because that is what the pipeline actually put in the video.
    """
    scene_graph = result.scene_graph
    visual_plan = result.visual_plan
    render_timeline = result.timeline
    if scene_graph is None or visual_plan is None or render_timeline is None:
        return [], None

    clips_by_scene: dict[str, VisualClip] = {
        clip.scene_id: clip for clip in render_timeline.clips
    }
    blocks_by_scene = _blocks_by_scene(script, scene_graph.scenes)

    units: list[VisualUnit] = []
    visual_track = Track(kind=TrackKind.VISUAL, name="Visuals")
    narration_track = Track(kind=TrackKind.NARRATION, name="Narration")
    caption_track = Track(kind=TrackKind.CAPTION, name="Captions")

    for scene in scene_graph.scenes:
        clip = clips_by_scene.get(scene.scene_id)
        if clip is None:
            # Dropped by `_make_contiguous` (an under-0.05s sliver) or never
            # produced. Either way there is nothing rendered to show for this
            # scene, so it gets no unit rather than an empty one.
            continue

        plan = visual_plan.plan_for(scene.scene_id)
        strategy, rationale = _realised_strategy(clip, plan)
        is_placeholder = isinstance(clip.source, PlaceholderClipSource)

        version = VisualVersion(
            version=1,
            strategy=strategy,
            object=clip.source.object if isinstance(clip.source, AssetClipSource) else None,
            asset_id=(
                clip.source.asset_id if isinstance(clip.source, AssetClipSource) else None
            ),
            spec=(
                json.loads(clip.source.spec.model_dump_json())
                if isinstance(clip.source, ProgrammaticClipSource)
                else None
            ),
            rationale=rationale[:400],
        )

        try:
            unit = VisualUnit(
                organisation_id=result.project.organisation_id,
                project_id=result.project.project_id,
                index=len(units),
                script_block_ids=blocks_by_scene.get(scene.scene_id, []),
                scene_id=scene.scene_id,
                span=clip.span,
                status=(
                    VisualUnitStatus.FAILED
                    if is_placeholder
                    else VisualUnitStatus.DEGRADED
                    if clip.degradation
                    else VisualUnitStatus.READY
                ),
                detail=(
                    clip.source.message
                    if isinstance(clip.source, PlaceholderClipSource)
                    else ""
                ),
            )
            unit.add_version(version, select=True)
        except ValueError:
            continue

        units.append(unit)
        position = len(units)

        visual_track.clips.append(
            TimelineClip(
                track_id=visual_track.track_id,
                visual_unit_id=unit.visual_unit_id,
                start=clip.span.start,
                end=clip.span.end,
                source_kind=_clip_source_kind(clip),
                object=version.object,
                asset_id=version.asset_id,
                spec=version.spec,
                transition_in=clip.transition_in.kind,
                transition_in_seconds=clip.transition_in.duration_seconds,
                label=f"Visual {position:02d}",
            )
        )
        # The render's `Timeline` carries one continuous narration file, not
        # a clip per scene — see the module docstring. Reconstructed here as
        # one clip per unit, spanning what the unit's visual occupies, which
        # is exact for a voice recording because `narration_start_seconds`
        # and `fill_seconds` are always zero on this path.
        narration_track.clips.append(
            TimelineClip(
                track_id=narration_track.track_id,
                visual_unit_id=unit.visual_unit_id,
                start=clip.span.start,
                end=clip.span.end,
                source_kind=ClipSourceKind.EMPTY,
                label=f"Narration {position:02d}",
            )
        )

    if not units:
        return [], None

    for cue in render_timeline.captions:
        midpoint = (cue.span.start + cue.span.end) / 2.0
        owner = next(
            (u for u in units if u.span and u.span.start <= midpoint < u.span.end),
            None,
        )
        try:
            caption_track.clips.append(
                TimelineClip(
                    track_id=caption_track.track_id,
                    visual_unit_id=owner.visual_unit_id if owner else None,
                    start=cue.span.start,
                    end=cue.span.end,
                    source_kind=ClipSourceKind.TEXT,
                    text=cue.text[:2000],
                    label=cue.text[:40],
                )
            )
        except ValueError:
            # A cue too short to be a clip (below `MIN_CLIP_SECONDS`). Dropping
            # one caption is preferable to failing the whole derivation.
            continue

    edit_timeline = EditTimeline(
        organisation_id=result.project.organisation_id,
        project_id=result.project.project_id,
        tracks=[narration_track, visual_track, caption_track],
        # No target was asked for — this is a straight recording, not a video
        # planned against a length. Recording an invented one would be the
        # exact fabrication this module exists to avoid.
        target_seconds=None,
        version=1,
    )
    return units, edit_timeline


def _blocks_by_scene(script: Script, scenes: list[Scene]) -> dict[str, list[str]]:
    """Which script blocks belong to which scene, by the block's midpoint.

    A transcript segment occasionally straddles a scene boundary the scene
    engine drew between two semantic units; assigning by midpoint gives every
    block exactly one owning scene rather than letting it be double-counted
    or dropped.
    """
    by_scene: dict[str, list[str]] = {}
    for block in script.blocks:
        if block.measured_start is None or block.measured_end is None:
            continue
        midpoint = (block.measured_start + block.measured_end) / 2.0
        scene = _scene_at(scenes, midpoint)
        if scene is not None:
            by_scene.setdefault(scene.scene_id, []).append(block.block_id)
    return by_scene


def _scene_at(scenes: list[Scene], moment: float) -> Scene | None:
    for scene in scenes:
        if scene.span.start <= moment < scene.span.end:
            return scene
    if not scenes:
        return None
    return scenes[0] if moment < scenes[0].span.start else scenes[-1]


def _clip_source_kind(clip: VisualClip) -> ClipSourceKind:
    if isinstance(clip.source, AssetClipSource):
        return ClipSourceKind.OBJECT
    if isinstance(clip.source, ProgrammaticClipSource):
        return ClipSourceKind.PROGRAMMATIC
    return ClipSourceKind.EMPTY


def _realised_strategy(
    clip: VisualClip, plan: SceneVisualPlan | None
) -> tuple[VisualStrategy, str]:
    """Which rung of the ladder actually produced this clip, and why.

    `SceneComposer._realise` records one `DegradationStep` per rung that
    failed before recording `degradation` on the clip that finally succeeded
    (`composition.py`), so the count of failed rungs is exactly the index of
    the directive that worked — this is read back rather than guessed from
    the clip's content, which cannot on its own distinguish, say, a licensed
    photograph from an existing asset.
    """
    ladder = plan.ladder if plan is not None else []
    position = len(clip.degradation)
    if not isinstance(clip.source, PlaceholderClipSource) and position < len(ladder):
        directive: VisualDirective = ladder[position]
        return directive.strategy, directive.rationale

    if isinstance(clip.source, ProgrammaticClipSource):
        # Unambiguous regardless of the ladder: only the programmatic
        # strategy ever produces this source kind.
        return VisualStrategy.PROGRAMMATIC, "drawn by the animation engine"

    if isinstance(clip.source, PlaceholderClipSource):
        fallback = plan.primary.strategy if plan is not None else VisualStrategy.PROGRAMMATIC
        return fallback, clip.source.message or "no visual could be produced for this scene"

    return VisualStrategy.EXISTING_ASSET, "resolved outside the recorded plan"


__all__ = ["derive_product_documents"]
