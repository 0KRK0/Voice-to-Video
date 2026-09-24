"""Planning visual units, and building the editable timeline from them.

Two jobs, in one module because they are two halves of one idea: turning a
script into a set of things a user can direct, and laying those things out in
time.

## Grouping: why not one visual per line

Because the eye does not want a cut per sentence. Three consecutive lines about
the same idea are one shot; the reader of the finished video experiences a cut
as "we have moved on", and cutting when we have not moved on is the single most
common way automatically-generated video looks automatic.

The grouping signals, in order of trust:

1. **A paragraph break.** The author said a new idea starts here. Nothing the
   system infers beats a signal the human gave deliberately.
2. **A scene boundary from the scene engine.** When a scene graph exists it is
   already a meaning-level grouping, and re-deriving one would produce two
   groupings that disagree.
3. **Accumulated duration.** Past `max_visual_seconds` for the pacing mode, a
   shot has outstayed its welcome whatever the text says.
4. **A topic shift.** Fallback only: lexical overlap between adjacent lines.
   Crude, and better than nothing when the input is one long paragraph.

## Preservation: the part that actually matters

Re-planning happens constantly — the script changed, the pacing changed, the
user asked for a different style. Every re-plan must **preserve user-owned
units**: a locked visual, an approved one, the version history behind them.

`plan()` therefore takes the existing units and matches new groups to old ones
by the script blocks they cover. A group whose blocks are unchanged keeps its
unit — same id, same versions, same lock. That is what makes "I locked unit 4"
survive "regenerate the project", and it is the difference between a lock that
is a rule and a lock that is a hope.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from vtv.contracts.base import TIME_EPSILON, TimeSpan, utc_now
from vtv.contracts.errors import ErrorCode, VTVError
from vtv.contracts.pacing import PacingMode, PacingPlan, PacingProfile, profile_for
from vtv.contracts.scene import SceneGraph
from vtv.contracts.script import Script, ScriptBlock
from vtv.contracts.timeline import TransitionKind
from vtv.contracts.tracks import (
    MIN_CLIP_SECONDS,
    ClipSourceKind,
    EditTimeline,
    TimelineClip,
    Track,
    TrackKind,
    quantise,
)
from vtv.contracts.visual_unit import VisualUnit
from vtv.observability.events import EventName, EventSink

#: Below this Jaccard overlap between adjacent lines' significant words, the
#: topic may have moved. Deliberately *very* low, and never sufficient on its
#: own — see `_boundary_reason`.
#:
#: The first implementation used 0.08 and it cut on almost every line, because
#: two ordinary consecutive sentences share about one significant word out of
#: fifteen. That produced exactly the failure this whole grouping step exists to
#: prevent: a cut per sentence, which is what automatically-generated video looks
#: like. Erring towards under-cutting is right — a shot held one line too long is
#: a pacing complaint, a cut mid-idea is a mistake.
TOPIC_SHIFT_THRESHOLD = 0.001

#: Words too common to carry topic. Not a full stop-word list — just enough that
#: "the" and "of" do not make every pair of lines look related.
_COMMON = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from", "had", "has", "have", "he", "her", "his", "i", "in", "is", "it", "its", "of", "on", "or", "that", "the", "their", "then", "there", "these", "they", "this", "to", "was", "were", "what", "when", "which", "who", "will", "with", "would", "you", "your", "we", "our", "us", "not", "no", "so", "if", "than", "them", "him", "she"]
)


def _significant(text: str) -> set[str]:
    return {
        word
        for word in "".join(
            char.lower() if char.isalnum() or char.isspace() else " " for char in text
        ).split()
        if len(word) > 2 and word not in _COMMON
    }


def _related(left: str, right: str) -> float:
    a, b = _significant(left), _significant(right)
    if not a or not b:
        return 1.0  # No evidence of a shift is not evidence of one.
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class BlockGroup:
    """Blocks that will share one visual."""

    blocks: tuple[ScriptBlock, ...]
    reason: str = ""

    @property
    def key(self) -> tuple[str, ...]:
        """Identity for matching against an existing plan.

        Block *ids*, not text: an edit to a line's wording must keep its unit
        (and its lock, and its versions). Only adding, removing or re-grouping
        lines changes what a unit covers.
        """
        return tuple(block.block_id for block in self.blocks)

    @property
    def text(self) -> str:
        return " ".join(block.text for block in self.blocks)

    @property
    def seconds(self) -> float:
        return round(sum(block.duration_seconds for block in self.blocks), 3)


@dataclass
class VisualUnitPlanner:
    """Groups blocks into units, preserving what the user has decided."""

    events: EventSink = field(default_factory=EventSink)

    def group(
        self,
        script: Script,
        *,
        scene_graph: SceneGraph | None = None,
        mode: PacingMode = PacingMode.NATURAL,
        custom: PacingProfile | None = None,
        pinned: Sequence[Sequence[str]] = (),
    ) -> list[BlockGroup]:
        """Decide which lines share a visual.

        `pinned` is a list of block-id runs that must each come back as exactly
        one group. It is how a lock survives a re-grouping: without it, a
        re-plan that merged two shots into one would produce a single group
        matching only one of the two units, and the other — locked, with the
        picture the user chose and paid for — would simply not appear in the
        returned list and would be written out of storage by the caller. The
        segmentation is where that has to be prevented; a check afterwards can
        only report the loss.
        """
        profile = profile_for(mode, custom=custom)
        blocks = script.narrated_blocks
        if not blocks:
            return []

        boundaries = self._scene_boundaries(script, scene_graph)

        groups: list[BlockGroup] = []
        for is_pinned, run in self._segments(blocks, pinned):
            if is_pinned:
                groups.append(
                    BlockGroup(blocks=tuple(run), reason="you locked this visual")
                )
                continue
            groups.extend(self._group_run(run, profile=profile, boundaries=boundaries))
        return groups

    def _segments(
        self, blocks: list[ScriptBlock], pinned: Sequence[Sequence[str]]
    ) -> list[tuple[bool, list[ScriptBlock]]]:
        """Split the script into pinned runs and the free stretches between.

        A pinned run is the *contiguous* stretch of blocks belonging to one pin,
        starting where its first surviving block appears. If a new line was
        inserted into the middle of a pinned unit the run stops there, and the
        remainder is grouped freely — the locked unit keeps the first stretch,
        which is enough to guarantee it survives.
        """
        member: dict[str, int] = {}
        for position, pin in enumerate(pinned):
            for block_id in pin:
                member.setdefault(block_id, position)
        if not member:
            return [(False, list(blocks))]

        segments: list[tuple[bool, list[ScriptBlock]]] = []
        free: list[ScriptBlock] = []
        index = 0
        while index < len(blocks):
            pin_index = member.get(blocks[index].block_id)
            if pin_index is None:
                free.append(blocks[index])
                index += 1
                continue
            if free:
                segments.append((False, free))
                free = []
            run: list[ScriptBlock] = []
            while (
                index < len(blocks)
                and member.get(blocks[index].block_id) == pin_index
            ):
                run.append(blocks[index])
                index += 1
            # Consumed, so a pin whose blocks are no longer contiguous cannot
            # claim a second run and produce two groups for one unit.
            member = {
                key: value for key, value in member.items() if value != pin_index
            }
            segments.append((True, run))
        if free:
            segments.append((False, free))
        return segments

    def _group_run(
        self,
        blocks: list[ScriptBlock],
        *,
        profile: PacingProfile,
        boundaries: set[str],
    ) -> list[BlockGroup]:
        """The ordinary grouping decision, over one free stretch."""
        groups: list[BlockGroup] = []
        current: list[ScriptBlock] = []
        reason = "opening"
        for index, block in enumerate(blocks):
            if not current:
                current = [block]
                continue

            duration = sum(item.duration_seconds for item in current)
            why = self._boundary_reason(
                block,
                previous=current[-1],
                index=index,
                duration=duration,
                profile=profile,
                boundaries=boundaries,
            )
            if why is None:
                current.append(block)
                continue
            groups.append(BlockGroup(blocks=tuple(current), reason=reason))
            current = [block]
            reason = why

        if current:
            groups.append(BlockGroup(blocks=tuple(current), reason=reason))
        return groups

    def _boundary_reason(
        self,
        block: ScriptBlock,
        *,
        previous: ScriptBlock,
        index: int,
        duration: float,
        profile: PacingProfile,
        boundaries: set[str],
    ) -> str | None:
        """Why this block starts a new visual, or `None` to keep grouping."""
        del index
        if block.block_id in boundaries:
            return "a new scene begins here"
        if duration + block.duration_seconds > profile.max_visual_seconds:
            return "the shot would outstay its welcome"
        # A topic shift alone is not enough. It is the weakest of the four
        # signals and it fires readily on ordinary prose, so it may only end a
        # shot that has already earned its keep — otherwise the fallback
        # overrides the three better signals and every line gets its own cut.
        if (
            duration >= profile.min_visual_seconds
            and _related(previous.text, block.text) < TOPIC_SHIFT_THRESHOLD
        ):
            return "the subject changes"
        return None

    def _scene_boundaries(
        self, script: Script, scene_graph: SceneGraph | None
    ) -> set[str]:
        """Block ids at which a new visual must start.

        A paragraph break in the source is the strongest signal there is — the
        author put it there. Scene boundaries from the scene engine come second,
        matched by time when the script carries measured timings.
        """
        boundaries: set[str] = set()

        # Paragraph breaks, recovered from source offsets: a gap in the source
        # that contains a blank line is a paragraph boundary.
        source = script.source_text
        previous_end: int | None = None
        for block in script.narrated_blocks:
            if (
                previous_end is not None
                and block.source_start is not None
                and "\n\n" in source[previous_end : block.source_start]
            ):
                boundaries.add(block.block_id)
            previous_end = block.source_end if block.source_end is not None else None

        if scene_graph is not None:
            starts = [scene.span.start for scene in scene_graph.scenes]
            for block in script.narrated_blocks:
                if block.measured_start is None:
                    continue
                for start in starts:
                    if abs(block.measured_start - start) < 0.25:
                        boundaries.add(block.block_id)
        return boundaries

    # -- planning ---------------------------------------------------------

    def plan(
        self,
        script: Script,
        *,
        existing: list[VisualUnit] | None = None,
        scene_graph: SceneGraph | None = None,
        mode: PacingMode = PacingMode.NATURAL,
        custom: PacingProfile | None = None,
    ) -> list[VisualUnit]:
        """Produce the unit list, keeping every unit the user owns.

        The matching is by covered block ids. A group whose blocks are unchanged
        keeps its unit outright — id, versions, lock, approval. A group whose
        blocks changed gets a new unit unless the old one was *locked*, in which
        case the lock wins and the unit is kept with its span updated: a user
        who pinned a visual did not thereby pin the sentence under it.

        A locked unit is additionally **pinned in the segmentation** (see
        `group`), so a re-grouping cannot merge two locked shots into one group
        and drop whichever unit lost the match. Matching afterwards is not
        enough to guarantee that: it can only decide between units that a group
        still exists for.
        """
        # `is_user_owned`, not `locked`.
        #
        # The property exists precisely for this — its docstring reads "the user
        # has expressed a preference that must survive a re-plan", and
        # `USER_OWNED_STATES` says "automatic processes must not move a unit out
        # of one of these without being told to". Both were written, both were
        # correct, and this line tested a narrower condition: an **approved**
        # visual was silently dropped by the next re-plan, because approval is
        # not a lock. The rule was on the object and the call site had its own.
        pins = [
            list(unit.script_block_ids)
            for unit in (existing or [])
            if unit.is_user_owned
        ]
        groups = self.group(
            script, scene_graph=scene_graph, mode=mode, custom=custom, pinned=pins
        )
        by_key = {tuple(unit.script_block_ids): unit for unit in (existing or [])}
        by_block: dict[str, VisualUnit] = {}
        for unit in existing or []:
            for block_id in unit.script_block_ids:
                by_block.setdefault(block_id, unit)

        units: list[VisualUnit] = []
        claimed: set[str] = set()
        for index, group in enumerate(groups):
            matched: VisualUnit | None = by_key.get(group.key)
            if matched is None:
                # No exact match. A unit the user has a stake in still has a
                # claim on these blocks: they pinned that picture, approved it,
                # or brought the file themselves, and a re-grouping is not
                # permission to discard any of the three.
                #
                # `shows_user_media` is the weakest of the three and the one
                # most easily missed. Unlocking an uploaded photograph says "the
                # system may propose something else" — a statement about
                # regeneration. It is not permission for a regroup to delete the
                # binding, leaving an empty unit and no trace a file was here.
                for block in group.blocks:
                    candidate = by_block.get(block.block_id)
                    if (
                        candidate is not None
                        and (candidate.is_user_owned or candidate.shows_user_media)
                        and candidate.visual_unit_id not in claimed
                    ):
                        matched = candidate
                        break

            if matched is None:
                unit = VisualUnit(
                    organisation_id=script.organisation_id,
                    project_id=script.project_id,
                    index=index,
                    script_block_ids=list(group.key),
                    detail=group.reason,
                )
            else:
                unit = matched.model_copy(deep=True)
                unit.index = index
                if list(group.key) != unit.script_block_ids:
                    unit.script_block_ids = list(group.key)
                    # The words under a locked visual moved. The picture stays;
                    # the timing is stale until narration is re-measured.
                    unit.invalidate_timing("the lines under this visual changed")

            unit.span = self._span_for(group)
            claimed.add(unit.visual_unit_id)
            units.append(unit)

        known = {unit.visual_unit_id for unit in (existing or [])}
        # Narrated blocks, not every block: `group()` only ever sees narrated
        # ones, so a locked unit whose lines were all *muted* genuinely has
        # nothing to illustrate and disappearing is correct. Testing against
        # `script.blocks` made muting a line raise and left the project
        # un-re-plannable until the user undid the mute.
        surviving = {block.block_id for block in script.narrated_blocks}
        # The invariant the pinning above exists to hold. A user-owned unit may
        # legitimately disappear when every line it covered was deleted — it has
        # nothing left to illustrate. It may never disappear while its lines are
        # still in the script, and if that ever happens it is a bug in the
        # segmentation, not something to absorb quietly.
        #
        # Widened from `locked` to `is_user_owned` in step with the pinning
        # above: this check is what turns a pinning bug into a loud failure
        # instead of a quietly missing visual, so it has to cover exactly what
        # is pinned. A narrower guard than the thing it guards is not a guard.
        orphaned = [
            unit
            for unit in (existing or [])
            if unit.is_user_owned
            and unit.visual_unit_id not in claimed
            and surviving.intersection(unit.script_block_ids)
        ]
        if orphaned:  # pragma: no cover - defended by `_segments`
            raise VTVError(
                "re-planning would have discarded "
                f"{len(orphaned)} visual(s) you locked or approved whose lines "
                "still exist",
                code=ErrorCode.VISUAL_PLANNING_FAILED,
                user_message=(
                    "We could not re-plan without losing a visual you had "
                    "locked or approved. Nothing was changed."
                ),
            )

        self.events.emit(
            EventName.VISUAL_UNITS_PLANNED,
            project_id=script.project_id,
            data={
                "units": len(units),
                # How many survived the re-plan with their identity — and so
                # their versions, their lock and their approval. The number an
                # operator looks at when a user says "it forgot my choices".
                "preserved": sum(
                    1 for unit in units if unit.visual_unit_id in known
                ),
                "created": sum(
                    1 for unit in units if unit.visual_unit_id not in known
                ),
                "locked": sum(1 for unit in units if unit.locked),
                "mode": mode.value,
            },
        )
        return units

    def _span_for(self, group: BlockGroup) -> TimeSpan | None:
        """Where this group sits on the narration clock.

        `None` when the blocks have no measured timing yet — an estimate laid
        end to end is the *pacing* layer's job, and inventing a span here would
        produce two answers to the same question.
        """
        starts = [b.measured_start for b in group.blocks if b.measured_start is not None]
        ends = [b.measured_end for b in group.blocks if b.measured_end is not None]
        if not starts or not ends:
            return None
        return TimeSpan.of(round(min(starts), 3), round(max(ends), 3))


# ---------------------------------------------------------------------------
# Timeline construction
# ---------------------------------------------------------------------------

@dataclass
class TimelineBuilder:
    """Lays units out in time, applying a pacing plan.

    Separate from the planner because grouping and laying out change for
    different reasons: a script edit re-groups, a pacing change re-lays-out, and
    conflating them means every pacing tweak discards the grouping.
    """

    events: EventSink = field(default_factory=EventSink)

    def build(
        self,
        *,
        script: Script,
        units: list[VisualUnit],
        pacing: PacingPlan,
        organisation_id: str,
        project_id: str,
        existing: EditTimeline | None = None,
    ) -> EditTimeline:
        """Produce the editable timeline.

        Clips are laid end to end with no gaps on the visual track: a gap is
        black screen, and while the editor permits one deliberately, the builder
        never produces one by accident.

        **The narration track is contiguous.** Every visual clip covers exactly
        the narration under it, except the first — which is extended backwards
        over the intro — and the last, which absorbs every hold and the outro.
        That is not a stylistic choice: the narration is one continuous audio
        file placed at one offset, so a gap between two narration clips is a gap
        the renderer cannot produce, and building one produces a video whose
        pictures drift away from the voice. `PacingPlanner._deliverable` moves
        interior pacing time to the end for the same reason; this is the other
        half of that agreement, and `flatten` refuses a timeline where the two
        have come apart.
        """
        profile = pacing.profile
        holds = {
            item.visual_unit_id: item.seconds
            for item in pacing.allocations
            if item.visual_unit_id
        }

        narration = Track(kind=TrackKind.NARRATION, name="Narration")
        visual = Track(kind=TrackKind.VISUAL, name="Visuals")
        caption = Track(kind=TrackKind.CAPTION, name="Captions")

        # Clip identity is reused across a rebuild so the editor keeps its
        # selection and an undo stack still refers to something real.
        previous_clips: dict[str, TimelineClip] = {}
        if existing is not None:
            visual_track = existing.track_of_kind(TrackKind.VISUAL)
            if visual_track is not None:
                previous_clips = {
                    clip.visual_unit_id: clip
                    for clip in visual_track.clips
                    if clip.visual_unit_id
                }

        intro = quantise(_intro_seconds(pacing))
        tail = quantise(_outro_seconds(pacing) + sum(holds.values()))
        cursor = intro
        blocks_by_id = {block.block_id: block for block in script.blocks}
        final = len(units) - 1

        for position, unit in enumerate(units):
            spoken = sum(
                blocks_by_id[block_id].duration_seconds
                for block_id in unit.script_block_ids
                if block_id in blocks_by_id
            )
            # The visual covers its narration exactly. Its *length* is not a
            # free parameter: stretching one shot delays every later one, and
            # the voice under them does not wait. `min_visual_seconds` is
            # honoured by the *grouping* — short lines are merged into one shot
            # before they reach here — not by padding a clip, which would only
            # move the problem to the next unit.
            length = quantise(max(MIN_CLIP_SECONDS, spoken))
            voice_start = cursor
            start, end = cursor, quantise(cursor + length)
            if position == 0:
                # The opening shot is what the intro plays over.
                start = 0.0
            if position == final:
                # And the closing shot holds through the outro and every second
                # of pacing time that could not be placed anywhere else.
                end = quantise(end + tail)

            previous = previous_clips.get(unit.visual_unit_id)
            clip = TimelineClip(
                track_id=visual.track_id,
                visual_unit_id=unit.visual_unit_id,
                start=start,
                end=end,
                locked=unit.locked or (previous.locked if previous else False),
                transition_in=(
                    # The first shot has nothing to come from. Every other shot
                    # transitions the way the pacing profile says — length and
                    # kind together, because they are one editorial decision.
                    TransitionKind.CUT if position == 0 else profile.transition_kind
                ),
                transition_in_seconds=(
                    0.0 if position == 0 else profile.transition_seconds
                ),
                label=f"Visual {position + 1:02d}",
                **_source_of(unit),
            )
            if previous is not None:
                # Reuse the identity, not the content. The editor's selection,
                # an undo entry and a client-side scroll position all key off
                # the clip id, and a rebuild that mints a new one loses all
                # three for no reason.
                clip = clip.model_copy(update={"clip_id": previous.clip_id})
            visual.clips.append(clip)

            if spoken > 0:
                narration.clips.append(
                    TimelineClip(
                        track_id=narration.track_id,
                        visual_unit_id=unit.visual_unit_id,
                        start=voice_start,
                        end=quantise(voice_start + spoken),
                        source_kind=ClipSourceKind.EMPTY,
                        label=f"Narration {position + 1:02d}",
                    )
                )
                for block_id in unit.script_block_ids:
                    block = blocks_by_id.get(block_id)
                    if block is None or not block.is_narrated:
                        continue
                    caption.clips.append(
                        TimelineClip(
                            track_id=caption.track_id,
                            visual_unit_id=unit.visual_unit_id,
                            start=quantise(
                                voice_start + _offset(unit, block, blocks_by_id)
                            ),
                            end=quantise(
                                voice_start
                                + _offset(unit, block, blocks_by_id)
                                + max(0.4, block.duration_seconds)
                            ),
                            source_kind=ClipSourceKind.TEXT,
                            text=block.text[:2000],
                            label=block.text[:40],
                        )
                    )

            # Advances by the narration, never by the visual: the cursor is the
            # voice's clock.
            cursor = quantise(voice_start + length)

        # Tracks the user added — music, sound effects, overlays — are carried
        # across unchanged. The rebuild owns the three tracks it derives from
        # the script and the plan; it does not own the ones a person put there.
        # Discarding them was silent data loss on an operation the editor has to
        # call after every script change.
        carried = [
            track.model_copy(deep=True)
            for track in (existing.tracks if existing is not None else [])
            if track.kind
            not in {TrackKind.NARRATION, TrackKind.VISUAL, TrackKind.CAPTION}
        ]
        timeline = EditTimeline(
            organisation_id=organisation_id,
            project_id=project_id,
            tracks=[narration, visual, caption, *carried],
            target_seconds=pacing.target_seconds,
            version=(existing.version + 1) if existing is not None else 1,
        )
        self.events.emit(
            EventName.TIMELINE_EDITED,
            project_id=project_id,
            data={
                "reason": "rebuilt",
                "clips": sum(len(track.clips) for track in timeline.tracks),
                "duration": timeline.duration,
                "version": timeline.version,
            },
        )
        return timeline


def _intro_seconds(pacing: PacingPlan) -> float:
    from vtv.contracts.pacing import FillStrategy

    return sum(
        item.seconds
        for item in pacing.allocations
        if item.strategy is FillStrategy.INTRO
    )


def _outro_seconds(pacing: PacingPlan) -> float:
    from vtv.contracts.pacing import FillStrategy

    return sum(
        item.seconds
        for item in pacing.allocations
        if item.strategy is FillStrategy.OUTRO
    )


def _offset(
    unit: VisualUnit, block: ScriptBlock, blocks_by_id: dict[str, ScriptBlock]
) -> float:
    """How far into its unit this block's caption starts."""
    total = 0.0
    for block_id in unit.script_block_ids:
        if block_id == block.block_id:
            break
        other = blocks_by_id.get(block_id)
        if other is not None:
            total += other.duration_seconds
    return round(total, 3)


def flatten(
    timeline: EditTimeline,
    *,
    narration: Any,
    style: Any,
    aspect_ratio: Any,
    scene_graph_id: str,
) -> Any:
    """Turn the editable timeline into the render contract.

    Two objects rather than one, on purpose: `EditTimeline` is what a user
    manipulates — typed tracks, locks, versions, gaps — and `Timeline` is what
    ffmpeg is told to draw. Keeping them apart means the renderer never has to
    understand editing and the editor never has to understand codecs.

    This is the seam between them, and it is the only place that knows both.

    A gap on the visual track becomes a placeholder clip rather than an error.
    A user may deliberately leave black screen, and the renderer needs
    *something* for every instant or the output has a hole in it.
    """
    from vtv.contracts.errors import Status
    from vtv.contracts.timeline import (
        AssetClipSource,
        CaptionCue,
        FitPolicy,
        PlaceholderClipSource,
        ProgrammaticClipSource,
        VisualClip,
    )
    from vtv.contracts.timeline import (
        Timeline as RenderTimeline,
    )
    from vtv.contracts.visual_language import TypographySpec

    visual_track = timeline.track_of_kind(TrackKind.VISUAL)
    caption_track = timeline.track_of_kind(TrackKind.CAPTION)

    clips: list[Any] = []
    for clip in sorted(
        visual_track.clips if visual_track else [], key=lambda item: item.start
    ):
        span = TimeSpan.of(clip.start, clip.end)
        if clip.source_kind is ClipSourceKind.OBJECT and clip.object is not None:
            source: Any = AssetClipSource(
                asset_id=clip.asset_id or clip.clip_id,
                object=clip.object,
                # The renderer draws this and `Timeline.attributions` collects
                # it. Dropping it here is how a CC-BY photograph reached a
                # customer's published video with no credit on it.
                attribution=clip.attribution,
            )
        elif clip.source_kind is ClipSourceKind.PROGRAMMATIC and clip.spec:
            source = ProgrammaticClipSource(spec=_spec(clip.spec))
        elif clip.source_kind is ClipSourceKind.TEXT and clip.text:
            source = ProgrammaticClipSource(
                spec=TypographySpec(headline=clip.text[:120])
            )
        else:
            # A deliberate gap is still a frame the renderer must fill.
            source = PlaceholderClipSource(message="")
        clips.append(
            VisualClip(
                scene_id=clip.visual_unit_id or clip.clip_id,
                span=span,
                source=source,
                fit=FitPolicy.STILL,
                # The editor's transition, carried into the render contract.
                #
                # `TimelineBuilder` has always set `transition_in` on every clip
                # after the first, from the pacing profile, and the renderer has
                # always known how to draw a dissolve — it blends two frames,
                # which is why the module docstring calls transitions free. This
                # is the one line between the two, and without it every Studio
                # render was a hard cut between every shot while the timeline
                # said otherwise. Exactly the failure the attribution field had:
                # data present at both ends and dropped in the middle.
                transition_in=_transition_of(clip),
            )
        )

    cues = [
        CaptionCue(
            span=TimeSpan.of(clip.start, clip.end), text=(clip.text or "")[:400]
        )
        for clip in sorted(
            caption_track.clips if caption_track else [], key=lambda item: item.start
        )
        if (clip.text or "").strip()
    ]

    # Where the voice sits on the edited clock, and how much deliberate visual
    # time surrounds it. The narration track carries the answer: the editor put
    # narration clips exactly where the voice plays, so the first one's start is
    # the offset and everything after the last one's end is fill. Deriving it
    # here rather than passing it in keeps one source of truth — an edit that
    # moved the opening title lengthens the intro without anybody recomputing a
    # number and passing it along.
    narration_track = timeline.track_of_kind(TrackKind.NARRATION)
    spoken = sorted(
        narration_track.clips if narration_track else [], key=lambda c: c.start
    )
    start_offset = quantise(spoken[0].start) if spoken else 0.0

    # The chokepoint for the whole synchronisation question. The renderer places
    # **one** continuous audio file at **one** offset, so a gap between two
    # narration clips is a gap it cannot produce: it would play the voice
    # straight through while the pictures waited, and every visual after the gap
    # would be late by the size of the gap. The builder is written not to create
    # one and the planner is written not to ask for one — this refuses to render
    # the result if either of them is ever wrong, because the failure is
    # invisible in the output. A video that is subtly out of sync is worse than
    # a job that failed loudly.
    for earlier, later in pairwise(spoken):
        if later.start - earlier.end > TIME_EPSILON:
            raise VTVError(
                f"narration clips {earlier.clip_id} and {later.clip_id} leave a "
                f"{round(later.start - earlier.end, 3)}s gap; the renderer places "
                "one continuous recording and cannot insert silence into it",
                code=ErrorCode.RENDER_FAILED,
                user_message=(
                    "We could not build this video without the pictures drifting "
                    "away from your voice. Nothing was rendered."
                ),
            )

    voice_ends = quantise(start_offset + narration.duration_seconds)
    drawn = quantise(
        max(
            [clip.span.end for clip in clips] + [cue.span.end for cue in cues],
            default=0.0,
        )
    )
    fill = quantise(max(0.0, drawn - voice_ends))

    return RenderTimeline(
        organisation_id=timeline.organisation_id,
        project_id=timeline.project_id,
        scene_graph_id=scene_graph_id,
        narration=narration,
        narration_start_seconds=start_offset,
        fill_seconds=fill,
        clips=clips,
        captions=cues,
        style=style,
        aspect_ratio=aspect_ratio,
        status=Status.READY,
    )


def retarget(timeline: Any, units: list[VisualUnit]) -> Any:
    """Make the timeline show what the units currently say they show.

    ## Why this is one function and not three call sites

    Three operations change what a visual shows — regenerating it, choosing a
    different version, and giving it a file of your own — and only the first one
    ever repointed the timeline. The other two changed the *unit* and left the
    *clip* pointing at the previous content.

    That is not a cosmetic difference. `run_render_scope` flattens the timeline
    and nothing else: the units document is not consulted at render time. So
    "Switching a version is free and instant", said next to a version list, was
    true of the inspector and false of the exported video — the user picked v1,
    the interface agreed, and the file they downloaded contained v2. Handing
    somebody a video that is not the one they approved is the worst failure this
    product has, and it was reachable by clicking the thing the interface most
    encourages you to click.

    So the repointing is a function of the whole units list rather than one
    unit, and every path that stores units runs it. A fourth operation that
    changes a unit's content gets this for free, which is the only way it can be
    right for operations nobody has written yet.

    ## What it will not touch

    * **A locked clip.** The unit may be unlocked while one of its clips is not,
      and an automatic repaint that stepped over that is the same broken promise
      as regenerating a locked visual.
    * **Position, identity and lock state.** Content only. The clip id is what
      the editor's selection, the undo stack and the script link all hang off.
    * **A clip whose source the user placed by hand** — a text card, or an
      overlay from the media library — because those are not this unit's
      picture, they are something the user put on top of it.

    Returns the timeline unchanged, and *without* advancing the version, when
    nothing needed repointing. A version bump nobody caused turns every other
    editor's next save into a spurious conflict.
    """
    if timeline is None:
        return None

    by_unit = {unit.visual_unit_id: unit for unit in units}
    working = timeline.model_copy(deep=True)
    changed = False

    for track in working.tracks:
        clips = []
        for clip in track.clips:
            unit = by_unit.get(clip.visual_unit_id or "")
            if (
                unit is None
                or clip.locked
                or clip.source_kind not in _REPAINTABLE
            ):
                clips.append(clip)
                continue
            wanted = _source_of(unit)
            if all(
                getattr(clip, field, None) == value for field, value in wanted.items()
            ):
                clips.append(clip)
                continue
            clips.append(clip.model_copy(update=wanted))
            changed = True
        track.clips = clips

    if not changed:
        return timeline

    working.version += 1
    working.updated_at = utc_now()
    return working


#: Clip sources this repaints. A `TEXT` clip is something the user typed onto
#: the timeline, not a rendering of the unit, and overwriting it would delete
#: their words.
_REPAINTABLE = frozenset(
    {ClipSourceKind.OBJECT, ClipSourceKind.PROGRAMMATIC, ClipSourceKind.EMPTY}
)


def retime(timeline: EditTimeline, script: Script) -> EditTimeline:
    """Make every clip boundary agree with the voice that was actually recorded.

    ## The drift this exists to close

    `TimelineBuilder.build` lays every track out from `ScriptBlock.duration_seconds`
    — which, before anything is synthesised, is `estimated_seconds`, a
    speaking-rate guess. Once the voice is real, `measured_start` /
    `measured_end` land on the script (see `_apply_measured` in
    `product_jobs.py`) and are usually a little off from the guess on almost
    every line — sometimes longer, sometimes shorter. The timeline never heard
    about it. Each block's small error adds to the next, and by the end of a
    long video the visuals and captions can be seconds away from the voice
    that is supposedly under them, while the *total* length can still look
    right (`_require_fits` only ever checked the sum). This is the retiming
    that closes that gap: it does not rebuild the timeline — that would throw
    away every edit the user made to it — it moves the timeline that exists.

    ## The map

    A piecewise-linear, monotonic function from estimated time to measured
    time, anchored at both ends of every narrated block's span: `est_start`
    and `est_end` are that block's position in the cumulative sum of
    `estimated_seconds` the builder used, and they map onto that block's
    `measured_start` and `measured_end`. Linear *between* those points because
    there is no finer information to place a boundary that falls inside a
    block's own span (a caption padded past its block's natural end by the
    0.4s minimum, say) — a straight line across the block is the least this
    function can assume, and it is exact everywhere the builder actually put a
    boundary at a block edge, which is most places.

    Every anchor is shifted by `voice_offset` — where the narration track's
    first clip currently sits — so the map lives in the same coordinate space
    as the timeline's own clips rather than a separate zero-based "script
    clock". An intro card before the voice begins is a decision about the
    opening of the video, not a consequence of how fast someone talks, so it
    is not stretched by this: everything at or before frame zero stays at
    frame zero, and anything else before the voice starts is only shifted by
    however much the voice's own onset drifted, never scaled. Content after
    the last narrated block — an outro, a held final shot — is shifted by
    however much the whole voice track grew or shrank and keeps its own
    length: `_deliverable` already moved every bit of interior pacing time to
    the end for exactly this reason, and this is the other half of honouring
    that. Both directions extrapolate the two end anchors with slope 1, which
    is what "keep your own length, just move" means for a straight line.

    ## What this will not do

    It will not guess. A script with no measured timing at all has nothing to
    map from, and a script where `_apply_measured` did not manage to set
    *every* narrated block (it is a positional, all-or-nothing write — see its
    docstring) is in the same position: mapping the ones that did land would
    invent a timing for the ones that did not. Either way this returns the
    timeline unchanged, without advancing its version, exactly as `retarget`
    does when nothing needed repointing.

    It will not skip a locked clip. A lock says "an automatic re-plan may not
    change what this shows or where a person dragged it" — it has never meant
    "this clip is exempt from the fact that the voice under it takes a
    different amount of time to say." Leaving a locked clip at its estimated
    position while every other clip moves to the measured one would not
    protect the user's edit, it would desynchronise the one clip they cared
    about most.
    """
    blocks = script.narrated_blocks
    if not blocks or any(
        block.measured_start is None or block.measured_end is None
        for block in blocks
    ):
        return timeline

    narration = timeline.track_of_kind(TrackKind.NARRATION)
    spoken = sorted(narration.clips, key=lambda clip: clip.start) if narration else []
    #: Where the voice currently begins on this timeline. Kept fixed as the
    #: origin of the map rather than assumed to be zero, so an intro card
    #: (`FillStrategy.INTRO`) is not itself stretched by this function.
    voice_offset = spoken[0].start if spoken else 0.0

    anchors = _timing_anchors(blocks, voice_offset)

    def moved(point: float) -> float:
        if point <= 0.0:
            # Frame zero is frame zero, whatever the voice's own onset does.
            return 0.0
        return quantise(_interpolate(anchors, point))

    working = timeline.model_copy(deep=True)
    changed = False
    for track in working.tracks:
        ordered = sorted(track.clips, key=lambda clip: clip.start)
        cursor = 0.0
        moved_clips: list[TimelineClip] = []
        for clip in ordered:
            new_start = moved(clip.start)
            new_end = moved(clip.end)
            if track.is_exclusive:
                # The map is monotonic and single-valued, so two boundaries
                # that touched before this ran touch after it too — that is
                # what keeps the narration track gapless, not this line. This
                # is only a backstop against float rounding at the edge of an
                # anchor.
                new_start = max(new_start, cursor)
                new_end = max(new_end, new_start)
            if new_end - new_start < MIN_CLIP_SECONDS:
                new_end = quantise(new_start + MIN_CLIP_SECONDS)
            if new_start != clip.start or new_end != clip.end:
                changed = True
            moved_clips.append(
                clip.model_copy(update={"start": new_start, "end": new_end})
            )
            cursor = new_end
        track.clips = moved_clips

    if not changed:
        return timeline

    working.version += 1
    working.updated_at = utc_now()
    return working


def _timing_anchors(
    blocks: Sequence[ScriptBlock], voice_offset: float
) -> list[tuple[float, float]]:
    """(estimated, measured) boundary pairs, in the timeline's own seconds.

    Two anchors per narrated block, in script order: its estimated start and
    end — the cumulative sum of `estimated_seconds` the builder laid clips out
    from — mapped onto its measured start and end. Both shifted by
    `voice_offset` so an anchor lands exactly where that boundary already sits
    on this timeline before anything moves.

    Clamped to be non-decreasing in the measured value. Nothing enforces that
    one block's measured span starts no earlier than the previous one's ends
    — each block's own timing is validated in isolation, not against its
    neighbours — and a map built from values that run backwards is not a
    function this can use safely. A block whose measured timing regressed has
    its anchor pinned to the previous one instead of being trusted, which is a
    narrower claim than the rest of this function makes and an honest one:
    something upstream is already wrong, and this is not the place to decide
    what the right number would have been.
    """
    anchors: list[tuple[float, float]] = []
    est_cursor = 0.0
    last_measured = voice_offset
    for block in blocks:
        # The caller only reaches here once every narrated block has both
        # ends of its measured span set, so these are never `None` in
        # practice — but the field itself is `Seconds | None`, so a bare
        # `0.0` fallback keeps this a plain float without asserting on data
        # the caller has already checked.
        measured_start = block.measured_start or 0.0
        measured_end = block.measured_end or 0.0
        est_start = est_cursor
        est_cursor = round(est_cursor + block.estimated_seconds, 3)
        for est, measured in ((est_start, measured_start), (est_cursor, measured_end)):
            candidate = round(voice_offset + measured, 3)
            last_measured = max(last_measured, candidate)
            anchors.append((round(voice_offset + est, 3), last_measured))
    return anchors


def _interpolate(anchors: list[tuple[float, float]], point: float) -> float:
    """Piecewise-linear lookup, extrapolated with slope 1 past either end.

    Slope 1 outside the anchored region is what "keep your own length, just
    move" means for a straight line: content before the voice starts or after
    it ends is shifted by exactly as much as the edge of the mapped region
    shifted by, never stretched or squeezed.
    """
    first_x, first_y = anchors[0]
    if point <= first_x:
        return first_y + (point - first_x)
    last_x, last_y = anchors[-1]
    if point >= last_x:
        return last_y + (point - last_x)
    for (x0, y0), (x1, y1) in pairwise(anchors):
        if x0 <= point <= x1:
            if x1 <= x0:
                return y1
            fraction = (point - x0) / (x1 - x0)
            return y0 + fraction * (y1 - y0)
    return last_y  # pragma: no cover - unreachable, anchors span [first_x, last_x]


def _spec(payload: dict[str, Any]) -> Any:
    from pydantic import TypeAdapter

    from vtv.contracts.visual_language import AnimationSpec

    return TypeAdapter(AnimationSpec).validate_python(payload)


def _transition_of(clip: Any) -> Any:
    """The render contract's transition for an edit-timeline clip.

    `TimelineClip` carries the kind and the duration in two fields — the editor
    manipulates them separately — and `VisualClip` wants one object. A clip with
    no kind, or a zero duration, becomes a cut, which is what "no transition"
    means to the renderer.
    """
    from vtv.contracts.timeline import Transition, TransitionKind

    kind = getattr(clip, "transition_in", None) or TransitionKind.CUT
    seconds = float(getattr(clip, "transition_in_seconds", 0.0) or 0.0)
    if kind is TransitionKind.CUT or seconds <= 0:
        return Transition(kind=TransitionKind.CUT, duration_seconds=0.0)
    return Transition(kind=kind, duration_seconds=min(seconds, 2.0))


def _source_of(unit: VisualUnit) -> dict[str, Any]:
    """The clip fields describing what this unit currently shows."""
    selected = unit.selected
    if selected is None or not selected.is_usable:
        return {"source_kind": ClipSourceKind.EMPTY}
    if selected.object is not None:
        return {
            "source_kind": ClipSourceKind.OBJECT,
            "object": selected.object,
            "asset_id": selected.asset_id,
            "generation_id": selected.generation_id,
            # Carried with the object rather than left on the unit. `retarget`
            # compares this dict field by field against the clip, so including
            # it is also what makes "the version changed but the credit did not"
            # count as a difference worth repointing.
            "attribution": selected.attribution,
        }
    return {"source_kind": ClipSourceKind.PROGRAMMATIC, "spec": selected.spec}


__all__ = [
    "TOPIC_SHIFT_THRESHOLD",
    "BlockGroup",
    "TimelineBuilder",
    "VisualUnitPlanner",
    "flatten",
    "retarget",
    "retime",
]
