"""The product layer's rules, tested where they are decided.

`tests/test_product_scenarios.py` proves the promises hold through the real HTTP
application. This file tests the edges those scenarios cannot reach cheaply: the
arithmetic of pacing, the refusals in the timeline editor, the grouping
heuristics, and the state machine around locking and versions.

Both matter, and for different reasons. An integration test proves the feature is
wired; a unit test proves it is *right at the boundary*, which is where a feature
that is wired but wrong actually fails.
"""

from __future__ import annotations

import asyncio
import unittest
from itertools import pairwise

from vtv.contracts.base import IdPrefix, ObjectRef, TimeSpan, new_id
from vtv.contracts.errors import PolicyViolation, ValidationFailed, VTVError
from vtv.contracts.pacing import (
    DurationVerdict,
    FillStrategy,
    PacingMode,
    PacingProfile,
    profile_for,
)
from vtv.contracts.render_scope import (
    BOUNDARY_PADDING_SECONDS,
    RenderRegion,
    RenderScope,
)
from vtv.contracts.script import (
    RevisionKind,
    Script,
    ScriptBlock,
    ScriptOrigin,
    estimate_seconds,
)
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.tracks import (
    MIN_CLIP_SECONDS,
    ClipSourceKind,
    EditTimeline,
    TimelineClip,
    Track,
    TrackKind,
)
from vtv.contracts.visual_plan import VisualStrategy
from vtv.contracts.visual_unit import (
    ConsistencyStatus,
    GroundingStatus,
    RegenerationIntent,
    VisualUnit,
    VisualUnitStatus,
    VisualVersion,
)
from vtv.pipeline.editing import OperationKind, TimelineEditor, TimelineOperation
from vtv.pipeline.pacing import PaceableUnit, PacingPlanner
from vtv.pipeline.regeneration import RegenerationService
from vtv.pipeline.revision import INSTRUCTIONS, RevisionService
from vtv.pipeline.scripting import ScriptService, split_blocks
from vtv.pipeline.units import TimelineBuilder, VisualUnitPlanner, retime

PROJECT = new_id(IdPrefix.PROJECT)
OBJECT = ObjectRef(
    bucket="vtv-media",
    key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/{PROJECT}/assets/a.jpg",
    content_type="image/jpeg",
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def units(count: int, seconds: float = 20.0) -> list[PaceableUnit]:
    return [PaceableUnit(new_id(IdPrefix.VISUAL_UNIT), seconds) for _ in range(count)]


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------

class NarrationIsNeverDistorted(unittest.TestCase):
    """The one rule the pacing layer exists to enforce."""

    def setUp(self) -> None:
        self.planner = PacingPlanner()

    def test_a_longer_target_never_changes_the_narration_figure(self) -> None:
        plan = self.planner.plan(
            units=units(12), narration_seconds=240.0, target_seconds=360.0
        )
        self.assertEqual(plan.narration_seconds, 240.0)
        self.assertGreater(plan.fill_seconds, 0)

    def test_a_shorter_target_is_an_overrun_and_fills_nothing(self) -> None:
        """Adding pacing time to something already over target makes it worse."""
        plan = self.planner.plan(
            units=units(12), narration_seconds=420.0, target_seconds=300.0
        )
        self.assertIs(plan.verdict, DurationVerdict.OVERRUN)
        self.assertEqual(plan.allocations, [])
        self.assertEqual(plan.overrun_seconds, 120.0)
        self.assertTrue(plan.needs_user_decision)

    def test_the_overrun_message_names_shortening_the_script(self) -> None:
        plan = self.planner.plan(
            units=units(4), narration_seconds=400.0, target_seconds=300.0
        )
        self.assertIn("shorten", plan.message.lower())
        self.assertIn("speed up your voice", plan.message.lower())

    def test_a_five_minute_script_reaches_a_seven_minute_target(self) -> None:
        """Scenario D, at the layer that computes it."""
        for mode in (PacingMode.NATURAL, PacingMode.CINEMATIC, PacingMode.EDUCATIONAL):
            with self.subTest(mode.value):
                plan = self.planner.plan(
                    units=units(12, 25.0),
                    narration_seconds=300.0,
                    target_seconds=420.0,
                    mode=mode,
                )
                self.assertIn(
                    plan.verdict, {DurationVerdict.FILLED, DurationVerdict.ON_TARGET}
                )
                self.assertLessEqual(plan.shortfall_seconds, 2.0)

    def test_an_absurd_target_is_admitted_rather_than_padded(self) -> None:
        plan = self.planner.plan(
            units=units(6, 10.0), narration_seconds=60.0, target_seconds=900.0
        )
        self.assertIs(plan.verdict, DurationVerdict.UNDERFILLED)
        self.assertGreater(plan.shortfall_seconds, 0)
        self.assertIn("padding", plan.message.lower())

    def test_no_target_still_applies_the_mode(self) -> None:
        """A cinematic project is longer than its narration by definition."""
        plan = self.planner.plan(
            units=units(5), narration_seconds=100.0, mode=PacingMode.CINEMATIC
        )
        self.assertGreater(plan.fill_seconds, 0)
        self.assertIs(plan.verdict, DurationVerdict.ON_TARGET)

    def test_the_mode_is_trimmed_when_it_overshoots_the_target(self) -> None:
        plan = self.planner.plan(
            units=units(8),
            narration_seconds=160.0,
            target_seconds=165.0,
            mode=PacingMode.CINEMATIC,
        )
        self.assertIs(plan.verdict, DurationVerdict.ON_TARGET)
        self.assertLessEqual(plan.planned_seconds, 167.0)

    def test_fill_lands_where_the_renderer_can_actually_place_it(self) -> None:
        """Interior holds read better and cannot be delivered.

        This test asserted the opposite — that fill is spread across every
        visual — until an audit followed the plan into the renderer. The
        narration is one continuous recording placed at one offset, so time
        added *between* two shots is time the voice does not wait for: every
        later picture arrives late by the size of the gap, and the video is the
        right length and out of sync. `PacingPlanner._deliverable` therefore
        moves interior time to the end, preserving the total so the target is
        still met and `planned_seconds` is still true.

        Distributing it properly needs the narration rendered as several placed
        segments rather than one file. That is a real feature and it is not
        built.
        """
        paced = units(6, 20.0)
        plan = self.planner.plan(
            units=paced, narration_seconds=120.0, target_seconds=160.0
        )
        holds = [a for a in plan.allocations if a.strategy is FillStrategy.HOLD]
        self.assertEqual(len(holds), 1)
        # On the last unit, so it plays after the voice has finished.
        self.assertEqual(holds[0].visual_unit_id, paced[-1].visual_unit_id)
        # Nothing interior survives.
        self.assertFalse(
            [
                a
                for a in plan.allocations
                if a.strategy in {FillStrategy.PAUSE, FillStrategy.TRANSITION}
            ]
        )
        # And the total is unchanged: the time moved, it was not dropped.
        self.assertGreaterEqual(plan.planned_seconds, 158.0)

    def test_tight_pacing_produces_a_shorter_video_than_cinematic(self) -> None:
        tight = self.planner.plan(
            units=units(8), narration_seconds=160.0, mode=PacingMode.TIGHT
        )
        cinematic = self.planner.plan(
            units=units(8), narration_seconds=160.0, mode=PacingMode.CINEMATIC
        )
        self.assertLess(cinematic.planned_seconds, cinematic.planned_seconds + 1)
        self.assertLess(tight.planned_seconds, cinematic.planned_seconds)

    def test_custom_pacing_without_a_profile_is_refused(self) -> None:
        """Falling back to natural would silently ignore a configured profile."""
        with self.assertRaises(ValueError):
            profile_for(PacingMode.CUSTOM)

    def test_a_profile_whose_bounds_cross_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            PacingProfile(min_visual_seconds=10.0, max_visual_seconds=4.0)


# ---------------------------------------------------------------------------
# Timeline editing
# ---------------------------------------------------------------------------

def contiguous(count: int = 4, length: float = 4.0) -> tuple[EditTimeline, Track]:
    track = Track(kind=TrackKind.VISUAL, name="Visuals")
    track.clips = [
        TimelineClip(
            track_id=track.track_id,
            start=index * length,
            end=index * length + length,
            source_kind=ClipSourceKind.TEXT,
            text=f"clip {index}",
        )
        for index in range(count)
    ]
    timeline = EditTimeline(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=PROJECT,
        tracks=[track],
    )
    return timeline, timeline.tracks[0]


class EveryEditIsValidatedBeforeItCommits(unittest.TestCase):
    def setUp(self) -> None:
        self.editor = TimelineEditor()
        self.timeline, self.track = contiguous()

    def op(self, **fields: object) -> TimelineOperation:
        return TimelineOperation.model_validate(fields)

    def test_a_failed_operation_leaves_the_original_untouched(self) -> None:
        """The caller still holds a valid timeline after any refusal."""
        before = self.timeline.model_dump_json()
        with self.assertRaises(VTVError):
            self.editor.apply(
                self.timeline,
                self.op(
                    kind=OperationKind.MOVE,
                    clip_id=self.track.clips[0].clip_id,
                    start=5.0,
                ),
            )
        self.assertEqual(self.timeline.model_dump_json(), before)

    def test_a_split_produces_two_clips_that_share_the_visual(self) -> None:
        clip = self.track.clips[1]
        result = self.editor.apply(
            self.timeline, self.op(kind=OperationKind.SPLIT, clip_id=clip.clip_id, at=6.0)
        )
        track = result.timeline.tracks[0]
        self.assertEqual(len(track.clips), 5)
        self.assertEqual(len(set(c.clip_id for c in track.clips)), 5)

    def test_a_split_that_would_make_a_flash_is_refused(self) -> None:
        clip = self.track.clips[0]
        with self.assertRaises(ValidationFailed):
            self.editor.apply(
                self.timeline,
                self.op(
                    kind=OperationKind.SPLIT,
                    clip_id=clip.clip_id,
                    at=clip.start + MIN_CLIP_SECONDS / 2,
                ),
            )

    def test_a_split_outside_the_clip_is_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.editor.apply(
                self.timeline,
                self.op(
                    kind=OperationKind.SPLIT,
                    clip_id=self.track.clips[0].clip_id,
                    at=99.0,
                ),
            )

    def test_a_trim_to_nothing_says_to_remove_it_instead(self) -> None:
        clip = self.track.clips[0]
        with self.assertRaises(ValidationFailed) as caught:
            self.editor.apply(
                self.timeline,
                self.op(
                    kind=OperationKind.TRIM,
                    clip_id=clip.clip_id,
                    start=clip.start,
                    end=clip.start + 0.001,
                ),
            )
        self.assertIn("remove it", str(caught.exception))

    def test_a_locked_clip_refuses_a_move_and_names_the_remedy(self) -> None:
        locked = self.editor.apply(
            self.timeline,
            self.op(kind=OperationKind.LOCK, clip_id=self.track.clips[2].clip_id),
        ).timeline
        with self.assertRaises(PolicyViolation) as caught:
            self.editor.apply(
                locked,
                self.op(
                    kind=OperationKind.MOVE,
                    clip_id=self.track.clips[2].clip_id,
                    start=40.0,
                ),
            )
        self.assertIn("unlock", str(caught.exception).lower())

    def test_unlocking_restores_the_ability_to_edit(self) -> None:
        clip_id = self.track.clips[2].clip_id
        locked = self.editor.apply(
            self.timeline, self.op(kind=OperationKind.LOCK, clip_id=clip_id)
        ).timeline
        unlocked = self.editor.apply(
            locked, self.op(kind=OperationKind.UNLOCK, clip_id=clip_id)
        ).timeline
        moved = self.editor.apply(
            unlocked, self.op(kind=OperationKind.MOVE, clip_id=clip_id, start=40.0)
        )
        self.assertEqual(moved.changed_clip_ids, (clip_id,))

    def test_replacing_a_source_keeps_identity_and_span(self) -> None:
        """The operation behind "use v2". Everything hangs off the clip id."""
        clip = self.track.clips[1]
        result = self.editor.apply(
            self.timeline,
            self.op(
                kind=OperationKind.REPLACE_SOURCE,
                clip_id=clip.clip_id,
                source_kind=ClipSourceKind.OBJECT,
                object=OBJECT.model_dump(mode="json"),
            ),
        )
        found = result.timeline.clip(clip.clip_id)
        assert found is not None
        _track, replaced = found
        self.assertEqual(replaced.start, clip.start)
        self.assertEqual(replaced.end, clip.end)
        self.assertIs(replaced.source_kind, ClipSourceKind.OBJECT)

    def test_replacing_the_source_of_a_locked_clip_is_refused(self) -> None:
        """The one operation that changes the picture must honour the lock.

        This asserted the opposite until an audit pointed out what it was
        actually saying: that a lock protects a clip's *position* and not what
        it shows. A user who locks a visual is protecting the picture — the
        position is the part they can see has not moved.
        """
        clip = self.track.clips[1]
        locked = self.editor.apply(
            self.timeline, self.op(kind=OperationKind.LOCK, clip_id=clip.clip_id)
        ).timeline
        with self.assertRaises(PolicyViolation) as caught:
            self.editor.apply(
                locked,
                self.op(
                    kind=OperationKind.REPLACE_SOURCE,
                    clip_id=clip.clip_id,
                    source_kind=ClipSourceKind.OBJECT,
                    object=OBJECT.model_dump(mode="json"),
                ),
            )
        self.assertIn("locked", str(caught.exception.info.user_message).lower())

        found = locked.clip(clip.clip_id)
        assert found is not None
        _track, unchanged = found
        self.assertIsNot(unchanged.source_kind, ClipSourceKind.OBJECT)

    def test_music_clips_may_overlap_and_visual_clips_may_not(self) -> None:
        """Two pieces of music cross-fading is a feature; two pictures is not."""
        music = Track(kind=TrackKind.MUSIC, name="Music")
        timeline = EditTimeline(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=PROJECT,
            tracks=[self.track, music],
        )
        first = self.editor.apply(
            timeline,
            self.op(
                kind=OperationKind.INSERT,
                track_id=music.track_id,
                start=0.0,
                end=10.0,
                source_kind=ClipSourceKind.EMPTY,
            ),
        ).timeline
        both = self.editor.apply(
            first,
            self.op(
                kind=OperationKind.INSERT,
                track_id=music.track_id,
                start=8.0,
                end=18.0,
                source_kind=ClipSourceKind.EMPTY,
            ),
        )
        self.assertEqual(len(both.timeline.tracks[1].clips), 2)

    def test_the_narration_and_visual_tracks_cannot_be_removed(self) -> None:
        with self.assertRaises(PolicyViolation) as caught:
            self.editor.apply(
                self.timeline,
                self.op(kind=OperationKind.REMOVE_TRACK, track_id=self.track.track_id),
            )
        self.assertIn("mute", str(caught.exception).lower())

    def test_a_second_visual_track_is_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.editor.apply(
                self.timeline,
                self.op(kind=OperationKind.ADD_TRACK, track_kind=TrackKind.VISUAL),
            )

    def test_a_transition_longer_than_half_the_clip_is_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.editor.apply(
                self.timeline,
                self.op(
                    kind=OperationKind.SET_TRANSITION,
                    clip_id=self.track.clips[0].clip_id,
                    transition_in="dissolve",
                    transition_seconds=3.0,
                ),
            )

    def test_removing_a_clip_warns_about_the_gap_it_leaves(self) -> None:
        result = self.editor.apply(
            self.timeline,
            self.op(kind=OperationKind.REMOVE, clip_id=self.track.clips[1].clip_id),
        )
        self.assertTrue(any("gap" in w for w in result.warnings))
        self.assertEqual(len(result.timeline.tracks[0].gaps()), 1)

    def test_every_edit_reports_the_units_it_affected(self) -> None:
        """What partial rendering is scoped by."""
        unit_id = new_id(IdPrefix.VISUAL_UNIT)
        track = Track(kind=TrackKind.VISUAL)
        track.clips = [
            TimelineClip(
                track_id=track.track_id,
                start=0.0,
                end=4.0,
                visual_unit_id=unit_id,
                source_kind=ClipSourceKind.EMPTY,
            )
        ]
        timeline = EditTimeline(
            organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT, tracks=[track]
        )
        result = self.editor.apply(
            timeline,
            self.op(kind=OperationKind.LOCK, clip_id=track.clips[0].clip_id),
        )
        self.assertEqual(result.affected_unit_ids, (unit_id,))


# ---------------------------------------------------------------------------
# Render scope
# ---------------------------------------------------------------------------

class AScopedRegionIncludesItsTransitions(unittest.TestCase):
    """A region that cuts a cross-fade in half produces a visible seam."""

    def test_expansion_pads_both_ends(self) -> None:
        region = RenderRegion(
            scope=RenderScope.SCENE,
            start=20.0,
            end=28.0,
            visual_unit_ids=[new_id(IdPrefix.VISUAL_UNIT)],
        ).expand(limit=120.0)
        self.assertEqual(region.start, 20.0 - BOUNDARY_PADDING_SECONDS)
        self.assertEqual(region.end, 28.0 + BOUNDARY_PADDING_SECONDS)

    def test_expansion_is_clamped_to_the_project(self) -> None:
        region = RenderRegion(
            scope=RenderScope.RANGE, start=0.2, end=59.5
        ).expand(limit=60.0)
        self.assertEqual(region.start, 0.0)
        self.assertEqual(region.end, 60.0)

    def test_a_full_render_is_not_expanded(self) -> None:
        region = RenderRegion(scope=RenderScope.FULL_PROJECT)
        self.assertIs(region.expand(limit=60.0), region)

    def test_a_scoped_region_must_name_what_it_covers(self) -> None:
        with self.assertRaises(ValueError):
            RenderRegion(scope=RenderScope.CLIP, start=0.0, end=1.0)
        with self.assertRaises(ValueError):
            RenderRegion(scope=RenderScope.SCENE, start=0.0, end=1.0)

    def test_only_a_full_render_may_omit_an_end(self) -> None:
        with self.assertRaises(ValueError):
            RenderRegion(scope=RenderScope.RANGE, start=0.0)


# ---------------------------------------------------------------------------
# Script splitting
# ---------------------------------------------------------------------------

class SplittingProducesLinesAUserRecognises(unittest.TestCase):
    def test_sentences_become_separate_lines(self) -> None:
        blocks = split_blocks("One thing happened. Then another. Then a third.")
        self.assertEqual(len(blocks), 3)

    def test_a_paragraph_break_is_carried_through(self) -> None:
        blocks = split_blocks("First idea.\n\nSecond idea. And more.")
        self.assertEqual([starts for _text, starts in blocks], [True, True, False])

    def test_an_abbreviation_does_not_become_its_own_line(self) -> None:
        blocks = split_blocks("Dr. Babbage designed engines.")
        self.assertEqual(len(blocks), 1)

    def test_an_unpunctuated_wall_of_text_is_broken_up(self) -> None:
        wall = ", ".join(f"clause number {index}" for index in range(80))
        blocks = split_blocks(wall)
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(len(text) <= 420 for text, _ in blocks))

    def test_estimation_scales_with_word_count(self) -> None:
        short = estimate_seconds("Three short words.")
        longer = estimate_seconds(" ".join(["word"] * 60))
        self.assertLess(short, longer)

    def test_an_empty_script_is_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            ScriptService().from_text(
                "   ", organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT
            )


class GroupingDoesNotCutOnEverySentence(unittest.TestCase):
    """The single most common way generated video looks generated."""

    def setUp(self) -> None:
        self.service = ScriptService()
        self.planner = VisualUnitPlanner()

    def script(self, text: str):  # type: ignore[no-untyped-def]
        return self.service.from_text(
            text, organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT
        )

    def test_related_lines_share_a_visual_under_cinematic_pacing(self) -> None:
        script = self.script(
            "The transistor changed computing. The transistor replaced the "
            "vacuum tube. Computing transistors became small."
        )
        groups = self.planner.group(script, mode=PacingMode.CINEMATIC)
        self.assertLess(len(groups), len(script.blocks))

    def test_a_paragraph_break_always_starts_a_new_visual(self) -> None:
        script = self.script("A thing happened.\n\nA thing happened.")
        groups = self.planner.group(script)
        self.assertEqual(len(groups), 2)
        self.assertIn("scene", groups[1].reason)

    def test_tight_pacing_cuts_more_than_cinematic(self) -> None:
        text = " ".join(f"Sentence number {i} about computing." for i in range(12))
        script = self.script(text)
        tight = self.planner.group(script, mode=PacingMode.TIGHT)
        cinematic = self.planner.group(script, mode=PacingMode.CINEMATIC)
        self.assertGreaterEqual(len(tight), len(cinematic))

    def test_every_line_lands_in_exactly_one_group(self) -> None:
        script = self.script(
            "One. Two. Three.\n\nFour. Five.\n\nSix and seven and eight."
        )
        groups = self.planner.group(script)
        seen = [b.block_id for group in groups for b in group.blocks]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {b.block_id for b in script.narrated_blocks})


class ReplanningPreservesWhatTheUserOwns(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ScriptService()
        self.planner = VisualUnitPlanner()
        self.script_doc = self.service.from_text(
            "First idea here.\n\nSecond idea here.\n\nThird idea here.",
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=PROJECT,
        )
        self.units = self.planner.plan(self.script_doc)

    def test_identity_survives_an_unchanged_replan(self) -> None:
        again = self.planner.plan(self.script_doc, existing=self.units)
        self.assertEqual(
            [u.visual_unit_id for u in again],
            [u.visual_unit_id for u in self.units],
        )

    def test_versions_and_locks_survive(self) -> None:
        self.units[1].locked = True
        self.units[1].add_version(
            VisualVersion(version=1, strategy=VisualStrategy.PROGRAMMATIC, spec={})
        )
        again = self.planner.plan(self.script_doc, existing=self.units)
        self.assertTrue(again[1].locked)
        self.assertEqual(len(again[1].versions), 1)

    def test_editing_a_lines_wording_keeps_its_visual(self) -> None:
        """Only adding, removing or re-grouping lines changes what a unit covers."""
        before = self.units[1].visual_unit_id
        self.service.replace_block_text(
            self.script_doc,
            block_id=self.script_doc.blocks[1].block_id,
            text="The second idea, restated.",
        )
        again = self.planner.plan(self.script_doc, existing=self.units)
        self.assertEqual(again[1].visual_unit_id, before)

    def test_a_locked_unit_keeps_its_picture_when_its_lines_move(self) -> None:
        self.units[1].locked = True
        self.units[1].add_version(
            VisualVersion(version=1, strategy=VisualStrategy.PROGRAMMATIC, spec={})
        )
        # Re-plan under a mode that groups differently.
        again = self.planner.plan(
            self.script_doc, existing=self.units, mode=PacingMode.CINEMATIC
        )
        locked = [u for u in again if u.locked]
        self.assertEqual(len(locked), 1)
        self.assertEqual(len(locked[0].versions), 1)


# ---------------------------------------------------------------------------
# Retiming a timeline built from estimates onto the measured voice
# ---------------------------------------------------------------------------

#: Short, topically unrelated lines. One block per line and one visual unit
#: per block, so a boundary on the built timeline can be checked against one
#: block's measured span without the grouping heuristics in `VisualUnitPlanner`
#: deciding to merge two lines into one shot first.
_LINES = [
    "Before computer science was born, mathematical methods solved problems.",
    "These methods were slow.",
    "Mechanical computation eventually emerged in the nineteenth century.",
    "It was not fast enough for anyone who needed an answer quickly.",
]


def _narrated_script(lines: list[str] = _LINES) -> Script:
    blocks = [
        ScriptBlock(order=index, text=text, estimated_seconds=estimate_seconds(text))
        for index, text in enumerate(lines)
    ]
    return Script(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=PROJECT,
        origin=ScriptOrigin.AUTHORED,
        source_text=" ".join(lines),
        current_text=" ".join(lines),
        blocks=blocks,
    )


def _timeline_from_estimates(script: Script) -> EditTimeline:
    """What `TimelineBuilder.build` produces before any voice has been heard.

    One unit per block, and a `NATURAL` plan with no target — no intro, no
    outro, no holds — so every clip boundary is exactly the cumulative sum of
    `estimated_seconds`, which is what makes the drift in the tests below
    attributable to a single, known cause rather than pacing fill.
    """
    units = [
        VisualUnit(
            organisation_id=script.organisation_id,
            project_id=script.project_id,
            index=index,
            script_block_ids=[block.block_id],
        )
        for index, block in enumerate(script.narrated_blocks)
    ]
    plan = PacingPlanner().plan(
        units=[
            PaceableUnit(
                visual_unit_id=unit.visual_unit_id,
                narration_seconds=block.estimated_seconds,
            )
            for unit, block in zip(units, script.narrated_blocks, strict=True)
        ],
        narration_seconds=round(
            sum(block.estimated_seconds for block in script.narrated_blocks), 3
        ),
        mode=PacingMode.NATURAL,
    )
    return TimelineBuilder().build(
        script=script,
        units=units,
        pacing=plan,
        organisation_id=script.organisation_id,
        project_id=script.project_id,
    )


def _measured(script: Script, factors: list[float]) -> Script:
    """A copy of `script` with each block's span scaled by its own factor.

    Laid contiguously from zero, as `_apply_measured` writes it back from a
    real transcript. Different factors per block — rather than one factor for
    the whole script — is deliberate: a uniform rescale is exactly what a
    piecewise map is *not* needed for, and it is not what a real voice does
    either. Real speech runs faster on some lines and slower on others; the
    per-line error is what accumulates into the drift this feature fixes.
    """
    blocks = []
    cursor = 0.0
    for block, factor in zip(script.blocks, factors, strict=True):
        length = round(block.estimated_seconds * factor, 3)
        blocks.append(
            block.model_copy(
                update={
                    "measured_start": round(cursor, 3),
                    "measured_end": round(cursor + length, 3),
                    "timing_invalidated": False,
                }
            )
        )
        cursor = round(cursor + length, 3)
    return script.model_copy(update={"blocks": blocks})


class TheTimelineFollowsTheMeasuredVoice(unittest.TestCase):
    """The defect: `TimelineBuilder.build` lays every clip out from
    `estimated_seconds`, and once the real voice exists and `measured_start` /
    `measured_end` land on the script, nothing ever moved the timeline to
    agree with them. Each block's small estimate error carried into the next,
    so a long video's captions and visuals ended up seconds away from the
    voice under them while the total length still looked right — `retime` is
    the fix, and every test below fails on the code before it existed.
    """

    def setUp(self) -> None:
        self.script = _narrated_script()
        self.timeline = _timeline_from_estimates(self.script)
        # Some lines spoken slower than estimated, some faster — never a
        # single uniform ratio, so a fix that only rescaled the whole
        # narration by one factor would still leave interior lines wrong.
        self.measured = _measured(self.script, factors=[1.4, 0.6, 1.2, 0.8])

    def test_a_script_with_no_measured_timing_is_a_noop(self) -> None:
        result = retime(self.timeline, self.script)
        self.assertIs(result, self.timeline)

    def test_a_partially_measured_script_is_also_a_noop(self) -> None:
        """`_apply_measured` sets every narrated block or none; so does this."""
        half_measured = self.measured.model_copy(
            update={
                "blocks": [
                    block.model_copy(update={"measured_start": None, "measured_end": None})
                    if index == 0
                    else block
                    for index, block in enumerate(self.measured.blocks)
                ]
            }
        )
        result = retime(self.timeline, half_measured)
        self.assertIs(result, self.timeline)

    def test_the_unretimed_timeline_actually_drifts(self) -> None:
        """Proves the fixture reproduces the reported bug, not just a fix.

        Without calling `retime` at all, the last caption is still sitting at
        its estimated position while the measured voice has already moved on
        — exactly the symptom the user reported, just compressed into a
        four-line script instead of a 79-second video.
        """
        captions = sorted(
            self.timeline.track_of_kind(TrackKind.CAPTION).clips,
            key=lambda clip: clip.start,
        )
        last_measured_start = self.measured.narrated_blocks[-1].measured_start
        self.assertGreater(
            abs(captions[-1].start - last_measured_start),
            0.5,
            "the fixture should itself demonstrate real drift",
        )

    def test_captions_and_narration_move_to_where_the_voice_actually_is(self) -> None:
        retimed = retime(self.timeline, self.measured)
        self.assertIsNot(retimed, self.timeline)
        self.assertEqual(retimed.version, self.timeline.version + 1)

        narrated = self.measured.narrated_blocks
        captions = sorted(
            retimed.track_of_kind(TrackKind.CAPTION).clips, key=lambda c: c.start
        )
        narration = sorted(
            retimed.track_of_kind(TrackKind.NARRATION).clips, key=lambda c: c.start
        )
        self.assertEqual(len(captions), len(narrated))
        self.assertEqual(len(narration), len(narrated))
        for clip, block in zip(captions, narrated, strict=True):
            self.assertAlmostEqual(clip.start, block.measured_start, places=3)
        for clip, block in zip(narration, narrated, strict=True):
            self.assertAlmostEqual(clip.start, block.measured_start, places=3)
            self.assertAlmostEqual(clip.end, block.measured_end, places=3)

        # The error at the *last* clip is what "accumulates" in the bug
        # report. After retiming it is exactly zero, not merely smaller.
        last_measured = narrated[-1].measured_end
        visuals = sorted(
            retimed.track_of_kind(TrackKind.VISUAL).clips, key=lambda c: c.start
        )
        self.assertAlmostEqual(visuals[-1].end, last_measured, places=3)
        self.assertAlmostEqual(narration[-1].end, last_measured, places=3)

    def test_the_first_frame_and_the_narration_track_stay_gapless(self) -> None:
        retimed = retime(self.timeline, self.measured)

        visuals = sorted(
            retimed.track_of_kind(TrackKind.VISUAL).clips, key=lambda c: c.start
        )
        self.assertEqual(visuals[0].start, 0.0)

        narration = sorted(
            retimed.track_of_kind(TrackKind.NARRATION).clips, key=lambda c: c.start
        )
        for earlier, later in pairwise(narration):
            self.assertEqual(
                earlier.end, later.start, "retiming opened a gap in the narration"
            )
            self.assertGreater(later.end, later.start)
            self.assertGreaterEqual(later.start, earlier.start)

    def test_a_locked_visual_clip_still_moves_with_the_voice(self) -> None:
        """A lock protects content and manual position, not sync with the voice.

        Leaving a locked clip at its estimated position while every other clip
        moved to the measured one would not honour the user's edit — it would
        desynchronise the one clip they said mattered most.
        """
        visual = self.timeline.track_of_kind(TrackKind.VISUAL)
        visual.clips[1] = visual.clips[1].model_copy(update={"locked": True})

        retimed = retime(self.timeline, self.measured)
        moved = sorted(
            retimed.track_of_kind(TrackKind.VISUAL).clips, key=lambda c: c.start
        )[1]
        self.assertTrue(moved.locked)
        self.assertAlmostEqual(
            moved.start,
            self.measured.narrated_blocks[1].measured_start,
            places=3,
        )


# ---------------------------------------------------------------------------
# Visual unit rules
# ---------------------------------------------------------------------------

def unit_with(*versions: VisualVersion, locked: bool = False) -> VisualUnit:
    unit = VisualUnit(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=PROJECT,
        index=0,
        span=TimeSpan.of(0.0, 5.0),
        locked=locked,
    )
    for version in versions:
        unit.add_version(version)
    if locked:
        unit.status = VisualUnitStatus.LOCKED
    return unit


def version(number: int = 1, *, refused: bool = False) -> VisualVersion:
    return VisualVersion(
        version=number,
        strategy=VisualStrategy.PROGRAMMATIC,
        spec={"primitive": "typography", "headline": f"v{number}"},
        grounding=(
            GroundingStatus.REFUSED if refused else GroundingStatus.GROUNDED
        ),
    )


class ALockIsARule(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RegenerationService(
            producer=_Producer(), validator=_Accepting()
        )

    def test_regenerating_a_locked_unit_raises(self) -> None:
        with self.assertRaises(PolicyViolation):
            run(
                self.service.regenerate(
                    unit_with(version(), locked=True), narration="anything"
                )
            )

    def test_selecting_a_version_on_a_locked_unit_raises(self) -> None:
        unit = unit_with(version(1), version(2), locked=True)
        with self.assertRaises(PolicyViolation):
            self.service.select_version(
                unit, version_id=unit.versions[0].version_id
            )

    def test_a_project_regeneration_steps_around_a_locked_unit(self) -> None:
        """Silently here, unlike the single-unit path — see the docstring."""
        locked = unit_with(version(), locked=True)
        free = unit_with(version())
        after, results = run(
            self.service.regenerate_project(
                [locked, free], narrations={locked.visual_unit_id: "a", free.visual_unit_id: "b"}
            )
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(after[0].versions), 1)
        self.assertEqual(len(after[1].versions), 2)

    def test_locking_does_not_clear_when_status_moves(self) -> None:
        unit = self.service.set_locked(unit_with(version()), locked=True)
        self.assertTrue(unit.locked)
        self.assertIs(unit.status, VisualUnitStatus.LOCKED)
        unlocked = self.service.set_locked(unit, locked=False)
        self.assertFalse(unlocked.locked)
        self.assertIsNot(unlocked.status, VisualUnitStatus.LOCKED)


class AFailedRegenerationLeavesAWorkingVideo(unittest.TestCase):
    def test_a_provider_failure_keeps_the_previous_version_selected(self) -> None:
        unit = unit_with(version(1))
        selected = unit.selected_version_id
        service = RegenerationService(producer=_Failing(), validator=_Accepting())
        result = run(service.regenerate(unit, narration="anything"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.unit.selected_version_id, selected)
        self.assertIs(result.unit.status, VisualUnitStatus.READY)

    def test_a_first_failure_marks_the_unit_failed_not_the_project(self) -> None:
        unit = unit_with()
        service = RegenerationService(producer=_Failing(), validator=_Accepting())
        result = run(service.regenerate(unit, narration="anything"))
        self.assertIs(result.unit.status, VisualUnitStatus.FAILED)
        self.assertTrue(result.unit.detail)

    def test_a_refused_version_is_kept_but_not_selected(self) -> None:
        unit = unit_with(version(1))
        keep = unit.selected_version_id
        service = RegenerationService(producer=_Producer(), validator=_Refusing())
        result = run(service.regenerate(unit, narration="anything"))
        self.assertFalse(result.accepted)
        self.assertEqual(len(result.unit.versions), 2)
        self.assertEqual(result.unit.selected_version_id, keep)

    def test_an_unusable_version_cannot_be_selected(self) -> None:
        unit = unit_with(version(1), version(2, refused=True))
        with self.assertRaises(ValueError):
            unit.select_version(unit.versions[1].version_id)

    def test_a_new_version_records_its_parent(self) -> None:
        unit = unit_with(version(1))
        parent = unit.versions[0].version_id
        service = RegenerationService(producer=_Producer(), validator=_Accepting())
        result = run(
            service.regenerate(
                unit, narration="anything", intent=RegenerationIntent.MORE_CINEMATIC
            )
        )
        assert result.version is not None
        self.assertEqual(result.version.parent_version_id, parent)
        self.assertIs(result.version.intent, RegenerationIntent.MORE_CINEMATIC)


class TimingInvalidationIsExplicit(unittest.TestCase):
    def test_a_unit_whose_lines_moved_says_so(self) -> None:
        unit = unit_with(version())
        unit.invalidate_timing()
        self.assertIs(unit.status, VisualUnitStatus.TIMING_INVALIDATED)
        self.assertTrue(unit.detail)

    def test_a_locked_unit_records_stale_timing_without_losing_its_lock(self) -> None:
        """The lock is about which picture, not about where it sits."""
        unit = unit_with(version(), locked=True)
        unit.invalidate_timing()
        self.assertTrue(unit.locked)
        self.assertIs(unit.status, VisualUnitStatus.LOCKED)
        self.assertTrue(unit.detail)


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------

class RevisionsAreProposalsNotEdits(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ScriptService()
        self.script = self.service.from_text(
            "This are a sentence with a error. Another line here.",
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=PROJECT,
        )

    def test_every_revision_kind_has_an_instruction(self) -> None:
        """A kind with no instruction would reach a model with an empty task."""
        for kind in RevisionKind:
            with self.subTest(kind.value):
                self.assertIn(kind, INSTRUCTIONS)
                self.assertGreater(len(INSTRUCTIONS[kind]), 40)

    def test_no_instruction_is_built_from_user_text(self) -> None:
        """The user's script is input, never the instruction."""
        for text in INSTRUCTIONS.values():
            with self.subTest(text[:30]):
                self.assertNotIn("{", text)

    def test_proposing_without_a_model_raises_rather_than_no_opping(self) -> None:
        """"Could not reach a model" and "no suggestions" are different answers."""
        with self.assertRaises(VTVError):
            run(
                RevisionService(router=None).propose(
                    self.script, kind=RevisionKind.FIX_GRAMMAR
                )
            )

    def test_a_translation_without_a_language_is_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            run(
                RevisionService(router=None).propose(
                    self.script, kind=RevisionKind.TRANSLATE
                )
            )

    def test_a_model_that_returns_the_wrong_number_of_lines_is_refused(self) -> None:
        service = RevisionService(router=_Router({"lines": [{"block_id": "x", "text": "y"}] * 9}))
        with self.assertRaises(VTVError):
            run(service.propose(self.script, kind=RevisionKind.FIX_GRAMMAR))

    def test_a_model_that_names_a_line_we_did_not_send_is_refused(self) -> None:
        blocks = self.script.narrated_blocks
        service = RevisionService(
            router=_Router(
                {
                    "lines": [
                        {"block_id": "sbk_" + "z" * 24, "text": "hijacked"}
                        for _ in blocks
                    ]
                }
            )
        )
        with self.assertRaises(VTVError):
            run(service.propose(self.script, kind=RevisionKind.FIX_GRAMMAR))

    def test_a_wildly_longer_line_is_refused(self) -> None:
        blocks = self.script.narrated_blocks
        service = RevisionService(
            router=_Router(
                {
                    "lines": [
                        {"block_id": block.block_id, "text": "x" * 5000}
                        for block in blocks
                    ]
                }
            )
        )
        with self.assertRaises(VTVError):
            run(service.propose(self.script, kind=RevisionKind.FIX_GRAMMAR))

    def test_a_proposal_carries_the_duration_change(self) -> None:
        blocks = self.script.narrated_blocks
        service = RevisionService(
            router=_Router(
                {
                    "lines": [
                        {
                            "block_id": block.block_id,
                            "text": block.text + " With more words added here.",
                            "reason": "clearer",
                        }
                        for block in blocks
                    ]
                }
            )
        )
        proposal = run(service.propose(self.script, kind=RevisionKind.EXPAND))
        self.assertGreater(proposal.duration_delta_seconds, 0)
        self.assertEqual(len(proposal.changed_block_ids), len(blocks))

    def test_proposing_changes_nothing_until_it_is_accepted(self) -> None:
        before = self.script.current_text
        blocks = self.script.narrated_blocks
        service = RevisionService(
            router=_Router(
                {
                    "lines": [
                        {"block_id": b.block_id, "text": "Corrected line."}
                        for b in blocks
                    ]
                }
            )
        )
        proposal = run(service.propose(self.script, kind=RevisionKind.FIX_GRAMMAR))
        self.assertEqual(self.script.current_text, before)

        service.accept(self.script, proposal)
        self.assertNotEqual(self.script.current_text, before)
        # The user's own words are still there, untouched.
        self.assertIn("This are a sentence", self.script.source_text)

    def test_accepting_a_stale_proposal_is_refused(self) -> None:
        blocks = self.script.narrated_blocks
        service = RevisionService(
            router=_Router(
                {
                    "lines": [
                        {
                            "block_id": b.block_id,
                            "text": b.text.replace("are a", "is a"),
                        }
                        for b in blocks
                    ]
                }
            )
        )
        proposal = run(service.propose(self.script, kind=RevisionKind.FIX_GRAMMAR))
        self.service.replace_block_text(
            self.script, block_id=blocks[0].block_id, text="Changed first."
        )
        with self.assertRaises(ValidationFailed):
            service.accept(self.script, proposal)

    def test_a_rejected_proposal_is_kept_as_a_record(self) -> None:
        blocks = self.script.narrated_blocks
        service = RevisionService(
            router=_Router(
                {
                    "lines": [
                        {
                            "block_id": b.block_id,
                            "text": b.text.replace("are a", "is a"),
                        }
                        for b in blocks
                    ]
                }
            )
        )
        proposal = run(service.propose(self.script, kind=RevisionKind.FIX_GRAMMAR))
        rejected = service.reject(self.script, proposal)
        self.assertEqual(rejected.status.value, "rejected")
        self.assertTrue(rejected.changes)

    def test_accepting_marks_affected_visuals_for_re_timing(self) -> None:
        planner = VisualUnitPlanner()
        units_list = planner.plan(self.script)
        blocks = self.script.narrated_blocks
        service = RevisionService(
            router=_Router(
                {
                    "lines": [
                        {"block_id": b.block_id, "text": b.text + " Extended."}
                        for b in blocks
                    ]
                }
            )
        )
        proposal = run(service.propose(self.script, kind=RevisionKind.EXPAND))
        service.accept(self.script, proposal)
        self.assertTrue(service.invalidated_units(self.script, units_list))


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _Producer:
    async def produce(self, *, unit: VisualUnit, intent, narration):  # type: ignore[no-untyped-def]
        del intent, narration
        return VisualVersion(
            version=unit.next_version_number,
            strategy=VisualStrategy.PROGRAMMATIC,
            spec={"primitive": "typography", "headline": "produced"},
        )


class _Failing:
    async def produce(self, **_: object) -> VisualVersion:
        raise VTVError("the provider is down")


class _Accepting:
    def validate(self, **_: object):  # type: ignore[no-untyped-def]
        return GroundingStatus.GROUNDED, ConsistencyStatus.CONSISTENT, ""


class _Refusing:
    def validate(self, **_: object):  # type: ignore[no-untyped-def]
        return (
            GroundingStatus.REFUSED,
            ConsistencyStatus.NOT_APPLICABLE,
            "that claimed something nobody said",
        )


class _Router:
    """A generation router that returns one canned structured response."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def generate(self, request):  # type: ignore[no-untyped-def]
        from vtv.contracts.errors import Status
        from vtv.contracts.generation import GenerationResult

        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            provider="test",
            model="test",
            status=Status.READY,
            structured_output=self.payload,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class MarkdownMarkersNeverReachTheVideo(unittest.TestCase):
    """The defect: `**computation**` was drawn into the captions.

    People paste from wherever they wrote. A script carrying Markdown emphasis
    was carried through verbatim, so the asterisks were shown in the script
    panel, burned into the video's captions, and handed to speech synthesis —
    which reads them aloud.

    Removing them is not the rewriting the product promises never to do.
    Nothing is rephrased or reordered; the markers are a formatting convention
    from another medium, and this one cannot honour them.
    """

    def setUp(self) -> None:
        self.service = ScriptService()

    def script_for(self, text: str):  # type: ignore[no-untyped-def]
        return self.service.from_text(
            text, organisation_id=new_id(IdPrefix.PROJECT), project_id=new_id(IdPrefix.PROJECT)
        )

    def test_emphasis_is_removed_and_the_words_are_not(self) -> None:
        script = self.script_for(
            "It starts with the fundamental idea of **computation** — taking a "
            "problem and breaking it into smaller steps."
        )
        self.assertNotIn("*", script.source_text)
        self.assertIn("computation", script.source_text)
        self.assertIn("breaking it into smaller steps", script.source_text)

    def test_arithmetic_and_file_names_survive(self) -> None:
        """A stripper that guessed would eat a real character eventually."""
        for text in ("The answer is 2 * 3 = 6.", "Open my_file_name.txt now.",
                     "A lone * asterisk stays."):
            with self.subTest(text):
                self.assertEqual(self.script_for(text).source_text, text)

    def test_nested_emphasis_is_fully_unwrapped(self) -> None:
        script = self.script_for("This is **bold _and_ italic** together.")
        self.assertEqual(script.source_text, "This is bold and italic together.")
