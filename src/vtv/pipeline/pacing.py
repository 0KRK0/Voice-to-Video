"""Planning where the time goes.

The one rule this module exists to enforce:

> **Narration is never distorted to hit a target duration.**

Everything else is a consequence. If the target is longer than the words, the
planner spends the difference on visual time, working down a list ordered by how
little each option intrudes. If the target is shorter than the words, it does
not cut them — it reports `OVERRUN` and hands the decision back, because
deleting something a user wrote is not a pacing decision.

## The order of fills, and why

1. **Hold** — extend a shot the viewer is already looking at. Invisible.
2. **Transition** — lengthen a cross-fade. Nearly invisible.
3. **Pause** — silence between blocks. Noticeable, and often good: a point
   lands better with a beat after it.
4. **Intro / outro** — cards at the ends. Visible, but at the moments a viewer
   expects something ceremonial.
5. **Chapter card** — a card mid-video. Clearly an insertion.
6. **Visual-only** — a stretch with no narration at all. The most intrusive,
   and only reached when the target is far beyond what the script supports.

Each is capped by the profile, so `TIGHT` cannot be talked into a four-second
hold by a large target. When every option is exhausted the verdict is
`UNDERFILLED` rather than a worse video: the planner would rather return a
five-minute plan and say "you asked for seven" than pad two minutes of nothing.

## Why holds are distributed and not appended

Spare time added at the end is dead air. Spread across units in proportion to
their length, it reads as a considered pace. The distribution is
proportional-with-a-ceiling: a long shot absorbs more than a short one, and no
shot is stretched past the ceiling in `_capped_hold` — which is the *looser* of
an absolute shot length and a proportional limit, for the reason documented
there.
"""

from __future__ import annotations

from dataclasses import dataclass

from vtv.contracts.base import Seconds
from vtv.contracts.pacing import (
    MAX_STRETCH_RATIO,
    TARGET_TOLERANCE_SECONDS,
    DurationVerdict,
    FillAllocation,
    FillStrategy,
    PacingMode,
    PacingPlan,
    PacingProfile,
    profile_for,
)


@dataclass(frozen=True)
class PaceableUnit:
    """What the planner needs to know about one visual.

    A narrow view rather than the whole `VisualUnit`, so the planner can be
    tested without constructing the world and so it cannot accidentally depend
    on something it should not — a locked unit's *pacing* is still adjustable,
    for instance, and passing the full object invites confusion about that.
    """

    visual_unit_id: str
    narration_seconds: Seconds
    #: A locked or approved unit still accepts a hold. What it does not accept
    #: is a different picture. Kept as a field so the planner can prefer to
    #: leave user-owned units at their natural length when it has a choice.
    user_owned: bool = False


@dataclass
class PacingPlanner:
    """Turns narration, a target and a mode into an explicit plan."""

    def plan(
        self,
        *,
        units: list[PaceableUnit],
        narration_seconds: Seconds,
        target_seconds: Seconds | None = None,
        mode: PacingMode = PacingMode.NATURAL,
        custom: PacingProfile | None = None,
    ) -> PacingPlan:
        profile = profile_for(mode, custom=custom)

        # The mode's own contribution first. A cinematic project is longer than
        # its narration even with no target at all — that is what the mode
        # means, and applying it only when a target exists would make the mode
        # silently conditional.
        allocations = self._deliverable(self._baseline(units, profile), units)
        baseline = sum(item.seconds for item in allocations)

        if target_seconds is None:
            return PacingPlan(
                profile=profile,
                narration_seconds=narration_seconds,
                target_seconds=None,
                verdict=DurationVerdict.ON_TARGET,
                allocations=allocations,
            )

        if narration_seconds - target_seconds > TARGET_TOLERANCE_SECONDS:
            # The words alone are longer than the target. Not a pacing problem.
            overrun = round(narration_seconds - target_seconds, 3)
            return PacingPlan(
                profile=profile,
                narration_seconds=narration_seconds,
                target_seconds=target_seconds,
                verdict=DurationVerdict.OVERRUN,
                # Baseline dropped: adding pacing time to something already
                # over target would make the miss worse, and a plan that makes
                # the stated problem worse is not a plan.
                allocations=[],
                message=(
                    f"The narration is {_minutes(narration_seconds)} long, which is "
                    f"{_minutes(overrun)} more than the {_minutes(target_seconds)} "
                    "you asked for. Shortening the script is the only way to reach "
                    "it — nothing here will speed up your voice."
                ),
            )

        current = narration_seconds + baseline
        remaining = round(target_seconds - current, 3)

        if abs(remaining) <= TARGET_TOLERANCE_SECONDS:
            return PacingPlan(
                profile=profile,
                narration_seconds=narration_seconds,
                target_seconds=target_seconds,
                verdict=DurationVerdict.ON_TARGET,
                allocations=allocations,
            )

        if remaining < 0:
            # The mode overshot. Trim the mode's own contribution rather than
            # the narration: the user asked for a duration and chose a mode,
            # and the duration is the harder constraint.
            allocations = self._trim(allocations, by=-remaining)
            return PacingPlan(
                profile=profile,
                narration_seconds=narration_seconds,
                target_seconds=target_seconds,
                verdict=DurationVerdict.ON_TARGET,
                allocations=allocations,
                message=(
                    f"{mode.value.title()} pacing was reduced to fit "
                    f"{_minutes(target_seconds)}."
                ),
            )

        spent, unfilled = self._fill(units, profile, seconds=remaining)
        extra = self._deliverable(spent, units)
        allocations = self._deliverable([*allocations, *extra], units)

        if unfilled > TARGET_TOLERANCE_SECONDS:
            return PacingPlan(
                profile=profile,
                narration_seconds=narration_seconds,
                target_seconds=target_seconds,
                verdict=DurationVerdict.UNDERFILLED,
                allocations=allocations,
                message=(
                    f"This plan reaches "
                    f"{_minutes(narration_seconds + sum(a.seconds for a in allocations))}"
                    f", {_minutes(unfilled)} short of your "
                    f"{_minutes(target_seconds)} target. Stretching further would "
                    "be padding rather than pacing — expanding the script or "
                    "adding a section is the honest way to fill it."
                ),
            )

        return PacingPlan(
            profile=profile,
            narration_seconds=narration_seconds,
            target_seconds=target_seconds,
            verdict=DurationVerdict.FILLED,
            allocations=allocations,
            message=(
                f"Added {_minutes(sum(a.seconds for a in extra))} of visual time to "
                f"reach {_minutes(target_seconds)}. Your narration is unchanged."
            ),
        )

    # -- internals --------------------------------------------------------

    def _deliverable(
        self, allocations: list[FillAllocation], units: list[PaceableUnit]
    ) -> list[FillAllocation]:
        """Collapse time the renderer cannot actually place.

        The narration is **one continuous audio file** placed at one offset
        (`Timeline.narration` / `narration_start_seconds`). Silence cannot be
        inserted between two sentences of a recording that has already been
        made, so any allocation that would open an interior gap — a hold on a
        unit that is not the last, an inter-block pause, a lengthened
        transition — cannot be delivered. Planning it anyway produced a timeline
        whose visuals drifted seconds away from the voice under them: the video
        was the right *length* and out of sync, which is worse than being short.

        Rather than dropping the time (which would silently miss the target the
        user asked for) or keeping it (which desynchronises the video), it is
        moved to where a continuous recording permits it: the end. Total seconds
        are preserved, so `planned_seconds` and every message built from it stay
        true.

        Interior pacing needs the narration to be rendered as several placed
        segments rather than one file. That is a real feature and it is not
        built; see `docs/PRODUCT_BACKEND_COMPLETION_REPORT.md`.
        """
        if not allocations:
            return []
        last = units[-1].visual_unit_id if units else None

        keep: list[FillAllocation] = []
        moved = 0.0
        for item in allocations:
            interior = (
                item.strategy is FillStrategy.PAUSE
                or item.strategy is FillStrategy.TRANSITION
                or (
                    item.strategy is FillStrategy.HOLD
                    and item.visual_unit_id is not None
                    and item.visual_unit_id != last
                )
            )
            if interior:
                moved = round(moved + item.seconds, 3)
                continue
            keep.append(item)

        if moved <= 0:
            return keep

        # Merged into the final hold if there is one, so the plan does not grow
        # a second allocation against the same unit.
        for index, item in enumerate(keep):
            if item.strategy is FillStrategy.HOLD and item.visual_unit_id == last:
                keep[index] = item.model_copy(
                    update={"seconds": round(item.seconds + moved, 3)}
                )
                return keep

        keep.append(
            FillAllocation(
                strategy=FillStrategy.HOLD, seconds=moved, visual_unit_id=last
            )
        )
        return keep

    def _baseline(
        self, units: list[PaceableUnit], profile: PacingProfile
    ) -> list[FillAllocation]:
        """What the mode costs before any target is considered."""
        allocations: list[FillAllocation] = []
        if profile.intro_seconds:
            allocations.append(
                FillAllocation(
                    strategy=FillStrategy.INTRO, seconds=profile.intro_seconds
                )
            )
        for unit in units:
            if profile.hold_seconds:
                allocations.append(
                    FillAllocation(
                        strategy=FillStrategy.HOLD,
                        seconds=self._capped_hold(unit, profile, profile.hold_seconds),
                        visual_unit_id=unit.visual_unit_id,
                    )
                )
            if profile.inter_block_pause_seconds:
                allocations.append(
                    FillAllocation(
                        strategy=FillStrategy.PAUSE,
                        seconds=profile.inter_block_pause_seconds,
                        visual_unit_id=unit.visual_unit_id,
                    )
                )
        if profile.outro_seconds:
            allocations.append(
                FillAllocation(
                    strategy=FillStrategy.OUTRO, seconds=profile.outro_seconds
                )
            )
        return [item for item in allocations if item.seconds > 0]

    def _fill(
        self, units: list[PaceableUnit], profile: PacingProfile, *, seconds: Seconds
    ) -> tuple[list[FillAllocation], Seconds]:
        """Spend `seconds` on visual time. Returns what was spent and what was not."""
        allocations: list[FillAllocation] = []
        remaining = seconds

        # 1. Holds, distributed in proportion to narration length. A long shot
        #    absorbs more than a short one, which is what "considered pacing"
        #    looks like as opposed to a uniform pad.
        total = sum(max(0.1, unit.narration_seconds) for unit in units)
        if units and total > 0 and remaining > 0:
            for unit in units:
                if remaining <= 0:
                    break
                share = (max(0.1, unit.narration_seconds) / total) * seconds
                allowed = self._capped_hold(unit, profile, share)
                spend = round(min(allowed, remaining), 3)
                if spend <= 0:
                    continue
                allocations.append(
                    FillAllocation(
                        strategy=FillStrategy.HOLD,
                        seconds=spend,
                        visual_unit_id=unit.visual_unit_id,
                    )
                )
                remaining = round(remaining - spend, 3)

        # 2. Longer transitions, up to double the profile's own length.
        if remaining > 0 and units and profile.transition_seconds > 0:
            headroom = profile.transition_seconds * max(0, len(units) - 1)
            spend = round(min(headroom, remaining), 3)
            if spend > 0:
                allocations.append(
                    FillAllocation(strategy=FillStrategy.TRANSITION, seconds=spend)
                )
                remaining = round(remaining - spend, 3)

        # 3. Ceremonial time at the ends, if the mode did not already use it.
        for strategy, cap in (
            (FillStrategy.INTRO, 5.0),
            (FillStrategy.OUTRO, 6.0),
        ):
            if remaining <= 0:
                break
            spend = round(min(cap, remaining), 3)
            allocations.append(FillAllocation(strategy=strategy, seconds=spend))
            remaining = round(remaining - spend, 3)

        return allocations, max(0.0, remaining)

    def _capped_hold(
        self, unit: PaceableUnit, profile: PacingProfile, wanted: Seconds
    ) -> Seconds:
        """How much hold this unit can take without becoming a slideshow.

        The ceiling on a shot's total screen time is the *looser* of two
        limits, and which one governs depends on the length of the line:

        * a short line is governed by `max_visual_seconds` — two seconds of
          narration can carry ten of visual without looking odd, and a strict
          proportional cap would give it 1.2s and make short lines unfillable;
        * a long line is governed by the ratio — a 25-second block held for a
          further 25 is a slideshow whatever the absolute number says.

        Taking the tighter of the two was the first implementation and it was
        wrong: `max_visual_seconds` is 12, so any block longer than 12 seconds
        had *zero* headroom and a 5-minute script could never reach a 7-minute
        target however much room the pacing mode allowed.
        """
        ceiling = max(
            profile.max_visual_seconds, unit.narration_seconds * MAX_STRETCH_RATIO
        )
        headroom = max(0.0, ceiling - unit.narration_seconds)
        return round(max(0.0, min(wanted, headroom)), 3)

    def _trim(
        self, allocations: list[FillAllocation], *, by: Seconds
    ) -> list[FillAllocation]:
        """Reduce the mode's contribution to fit, most intrusive first.

        The reverse of the fill order: if something has to go, the chapter card
        goes before the hold does.
        """
        order = {
            FillStrategy.VISUAL_ONLY: 0,
            FillStrategy.CHAPTER_CARD: 1,
            FillStrategy.OUTRO: 2,
            FillStrategy.INTRO: 3,
            FillStrategy.PAUSE: 4,
            FillStrategy.TRANSITION: 5,
            FillStrategy.HOLD: 6,
        }
        remaining = by
        indexed = sorted(
            range(len(allocations)), key=lambda i: order[allocations[i].strategy]
        )
        kept = list(allocations)
        for position in indexed:
            if remaining <= 0:
                break
            item = kept[position]
            take = round(min(item.seconds, remaining), 3)
            kept[position] = item.model_copy(
                update={"seconds": round(item.seconds - take, 3)}
            )
            remaining = round(remaining - take, 3)
        return [item for item in kept if item.seconds > 0]


def _minutes(seconds: Seconds) -> str:
    """A duration a person can read. Used only in messages shown to users."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s" if rest else f"{minutes}m"


__all__ = ["PaceableUnit", "PacingPlanner"]
