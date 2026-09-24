"""Pacing and target duration — three durations that are not the same number.

A user says "make it seven minutes". Their narration is five. There are two
obvious things to do and both are wrong:

* **Slow the speech down.** The narration becomes 71% speed. It sounds like a
  recording playing back badly. Nobody watches it.
* **Pad silence at the end.** Two minutes of nothing. Nobody watches that either.

The correct answer is that a video's length is not its narration's length. The
extra two minutes are *visual* time: holds on a good image, transitions with
room to breathe, a chapter card, a beat of silence where a point lands. Those
are editorial decisions, and this module is where they are decided.

## Three durations

* **`narration_seconds`** — how long the words take. Set by the words. Not
  negotiable without changing the words.
* **`visual_seconds`** — how long the visuals take. Negotiable.
* **`target_seconds`** — what the user asked for. A constraint, not a fact.

The invariant that matters:

> **The narration is never distorted to hit a target.**

If the target is longer, visual time absorbs the difference. If the target is
*shorter* than the narration, the system cannot honour it by cutting — that
would silently delete something the user wrote — so it **warns**, and offers to
shorten the script through the same visible-proposal flow as every other text
change.

## Why pacing is a mode and not a slider

A slider implies the trade-off is one-dimensional. It is not: `TIGHT` and
`CINEMATIC` differ in *where* time goes, not only how much. Tight pacing cuts
transition length and hold time together; cinematic pacing extends holds and
transitions while keeping the cut rate low. A single number cannot express that,
and a user who is given one will move it back and forth without ever getting the
feel they wanted.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import Seconds, VTVModel
from vtv.contracts.timeline import TransitionKind


class PacingMode(str, Enum):
    """How time is spent between the words."""

    #: Follow the narration. Minimal holds, standard transitions. The default,
    #: because it is the one that never looks like a decision.
    NATURAL = "natural"
    #: Cut close. Short transitions, no holds, no visual-only moments. For
    #: social and for anything where attention is the scarce resource.
    TIGHT = "tight"
    #: Room to breathe. Longer holds, longer transitions, silence allowed after
    #: a point lands.
    CINEMATIC = "cinematic"
    #: Holds after each new concept so a viewer can absorb it before the next
    #: sentence starts. Slower than natural, for a different reason than
    #: cinematic — comprehension rather than mood.
    EDUCATIONAL = "educational"
    #: Faster than tight. Aggressive cuts. For recap and montage.
    FAST = "fast"
    #: Explicit numbers supplied by the caller.
    CUSTOM = "custom"


class PacingProfile(VTVModel):
    """The numbers behind a mode.

    Multipliers and ceilings rather than absolute durations, because the same
    profile has to work for a 30-second short and a 20-minute lesson.
    """

    mode: PacingMode = PacingMode.NATURAL

    #: Seconds of still visual after a block's narration ends.
    hold_seconds: Seconds = Field(default=0.0, ge=0.0, le=10.0)
    #: Cross-fade length between visuals.
    transition_seconds: Seconds = Field(default=0.4, ge=0.0, le=4.0)
    #: How one visual becomes the next.
    #:
    #: Part of the pacing profile because it *is* pacing: transition length and
    #: transition kind are the same editorial decision, and holding them in two
    #: places is how a profile ends up with a nine-tenths-of-a-second cut.
    #:
    #: Every profile below dissolves, which is what every video did before this
    #: field existed — except `fast`, which cuts. An eighty-millisecond dissolve
    #: is not a dissolve anybody can see; it is a cut with two smeared frames,
    #: and naming it correctly is the difference between a deliberate edit and
    #: an artefact.
    transition_kind: TransitionKind = TransitionKind.DISSOLVE
    #: Silence inserted between blocks, on top of the speaker's own pause.
    inter_block_pause_seconds: Seconds = Field(default=0.0, ge=0.0, le=5.0)
    #: A card at the start and end. Zero disables them.
    intro_seconds: Seconds = Field(default=0.0, ge=0.0, le=15.0)
    outro_seconds: Seconds = Field(default=0.0, ge=0.0, le=15.0)
    #: Longest a single visual may stay on screen before the system wants to
    #: cut to something else — a ceiling on boredom.
    max_visual_seconds: Seconds = Field(default=12.0, gt=0.0, le=120.0)
    #: Shortest a visual may be. Below this a cut reads as a flicker.
    min_visual_seconds: Seconds = Field(default=1.6, gt=0.0, le=30.0)

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> PacingProfile:
        if self.min_visual_seconds >= self.max_visual_seconds:
            raise ValueError("the minimum visual length must be below the maximum")
        return self


#: The profiles behind each mode. A table rather than a formula, because these
#: are editorial judgements and a formula would imply they are derivable.
PROFILES: dict[PacingMode, PacingProfile] = {
    PacingMode.NATURAL: PacingProfile(
        mode=PacingMode.NATURAL,
        hold_seconds=0.0,
        transition_seconds=0.4,
        max_visual_seconds=12.0,
        min_visual_seconds=1.6,
    ),
    PacingMode.TIGHT: PacingProfile(
        mode=PacingMode.TIGHT,
        hold_seconds=0.0,
        transition_seconds=0.15,
        inter_block_pause_seconds=0.0,
        max_visual_seconds=7.0,
        min_visual_seconds=1.1,
    ),
    PacingMode.FAST: PacingProfile(
        mode=PacingMode.FAST,
        hold_seconds=0.0,
        transition_seconds=0.08,
        # A cut, not an eighty-millisecond dissolve. At 30fps that is two
        # frames of blend, which reads as a compression artefact rather than a
        # transition — and fast pacing means cutting.
        transition_kind=TransitionKind.CUT,
        max_visual_seconds=4.0,
        min_visual_seconds=0.8,
    ),
    PacingMode.CINEMATIC: PacingProfile(
        mode=PacingMode.CINEMATIC,
        hold_seconds=1.2,
        transition_seconds=0.9,
        inter_block_pause_seconds=0.35,
        intro_seconds=3.0,
        outro_seconds=4.0,
        max_visual_seconds=18.0,
        min_visual_seconds=2.5,
    ),
    PacingMode.EDUCATIONAL: PacingProfile(
        mode=PacingMode.EDUCATIONAL,
        hold_seconds=0.8,
        transition_seconds=0.5,
        inter_block_pause_seconds=0.5,
        intro_seconds=2.0,
        outro_seconds=2.0,
        max_visual_seconds=15.0,
        min_visual_seconds=2.0,
    ),
}


class FillStrategy(str, Enum):
    """How spare time is spent when the target is longer than the narration.

    Ordered by how little they intrude. The planner works down the list, which
    is why `HOLD` comes before `CHAPTER_CARD`: extending a shot the viewer is
    already looking at is invisible, and inserting a card is not.
    """

    HOLD = "hold"
    TRANSITION = "transition"
    PAUSE = "pause"
    INTRO = "intro"
    OUTRO = "outro"
    CHAPTER_CARD = "chapter_card"
    VISUAL_ONLY = "visual_only"


class DurationVerdict(str, Enum):
    """Whether the target can be met, and what it would cost to try."""

    #: Target matches the narration closely enough that nothing is needed.
    ON_TARGET = "on_target"
    #: Target is longer; visual time absorbs the difference.
    FILLED = "filled"
    #: Target is longer than visual pacing can plausibly stretch to. Honoured
    #: as far as it goes, and said so — padding four minutes onto a two-minute
    #: script produces something nobody wants to watch.
    UNDERFILLED = "underfilled"
    #: Target is *shorter* than the narration. Cannot be honoured without
    #: cutting words, which is the user's decision and not ours.
    OVERRUN = "overrun"


#: How far from the target counts as hitting it. Two seconds either way on a
#: video of any real length is not something a person perceives, and treating it
#: as a miss produces warnings nobody should act on.
TARGET_TOLERANCE_SECONDS = 2.0

#: The most a pacing plan will stretch the visual track relative to narration
#: before reporting `UNDERFILLED`. Beyond roughly half again, the result reads
#: as padding however carefully it is distributed.
MAX_STRETCH_RATIO = 1.6


class FillAllocation(VTVModel):
    """One decision about where a slice of spare time went.

    Itemised rather than summarised so the editor can show *why* the video is
    longer than the words, and so a user who dislikes the answer can change the
    pacing mode rather than the script.
    """

    strategy: FillStrategy
    seconds: Seconds = Field(ge=0.0)
    #: The unit this applies to, when it applies to one.
    visual_unit_id: str | None = None


class PacingPlan(VTVModel):
    """What the timeline builder should do about duration.

    A plan rather than a mutation: it is computed, shown to the user, and only
    then applied. The same shape as `RevisionProposal`, and for the same
    reason — the system does not get to quietly reshape someone's video.
    """

    profile: PacingProfile
    narration_seconds: Seconds = Field(ge=0.0)
    target_seconds: Seconds | None = Field(default=None, ge=0.0)

    verdict: DurationVerdict = DurationVerdict.ON_TARGET
    allocations: list[FillAllocation] = Field(default_factory=list, max_length=4000)
    #: Plain language, for the user. Empty when there is nothing to say.
    message: str = Field(default="", max_length=400)

    @property
    def fill_seconds(self) -> Seconds:
        return round(sum(item.seconds for item in self.allocations), 3)

    @property
    def planned_seconds(self) -> Seconds:
        """What the video will actually be."""
        return round(self.narration_seconds + self.fill_seconds, 3)

    @property
    def shortfall_seconds(self) -> Seconds:
        """How far short of the target the plan lands. Zero when it does not."""
        if self.target_seconds is None:
            return 0.0
        return max(0.0, round(self.target_seconds - self.planned_seconds, 3))

    @property
    def overrun_seconds(self) -> Seconds:
        """How far past the target the narration alone already is."""
        if self.target_seconds is None:
            return 0.0
        return max(0.0, round(self.narration_seconds - self.target_seconds, 3))

    @property
    def needs_user_decision(self) -> bool:
        """The system has done what it can and the rest is the user's call."""
        return self.verdict in {DurationVerdict.OVERRUN, DurationVerdict.UNDERFILLED}


def profile_for(
    mode: PacingMode, *, custom: PacingProfile | None = None
) -> PacingProfile:
    """The profile for a mode.

    `CUSTOM` requires the caller to supply one; falling back to `NATURAL` would
    mean a user who configured custom pacing silently got the default.
    """
    if mode is PacingMode.CUSTOM:
        if custom is None:
            raise ValueError("custom pacing requires an explicit profile")
        return custom.model_copy(update={"mode": PacingMode.CUSTOM})
    return PROFILES[mode]


__all__ = [
    "MAX_STRETCH_RATIO",
    "PROFILES",
    "TARGET_TOLERANCE_SECONDS",
    "DurationVerdict",
    "FillAllocation",
    "FillStrategy",
    "PacingMode",
    "PacingPlan",
    "PacingProfile",
    "profile_for",
]
