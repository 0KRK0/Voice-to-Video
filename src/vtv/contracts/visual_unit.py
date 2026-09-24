"""The visual unit — what a user actually directs.

This is the missing object. The pipeline already had a `Scene` (a coherent
visual idea, anchored to a span of narration) and a `VisualClip` (a rendered
thing on a timeline). Neither is what a person points at when they say *"I don't
like that one"*.

A `VisualUnit` is that thing. It is the single object connecting:

```
ScriptBlock(s)  →  VisualUnit  →  Scene  →  TimelineClip  →  rendered seconds
```

and it is the reason clip-level regeneration is expressible at all. Before it,
"regenerate this visual" had no subject: you could re-run the whole pipeline, or
you could hand-edit a `Timeline`, and neither survives a re-render.

## Why it is not just a Scene

A `Scene` is a *planning* artefact — produced by the scene engine, replaced
wholesale on every run. A `VisualUnit` is a *user-owned* artefact: it has
versions the user chose between, a lock the user set, and an approval the user
gave. Those must survive a re-plan, and a scene cannot carry them because the
scene engine will overwrite it.

That distinction is the whole reason for a separate object. Putting `locked` on
`Scene` would mean the next planning pass either respects a field it does not
own, or silently discards a user's decision — and the second is what actually
happens, every time.

## The three states that matter most

* **`LOCKED`** — the user pressed "keep this". No automatic process may replace
  it. A full-project regeneration must step around it. This is checked at the
  regeneration boundary, not by convention.
* **`FAILED`** — this unit could not be produced. The *project* is still
  editable. One bad visual must not destroy a video.
* **`TIMING_INVALIDATED`** — the narration under this unit changed, so its
  span no longer means anything. Kept explicit because the alternative is a
  timeline that is quietly wrong.

## Versions

Every regeneration produces a `VisualVersion` and leaves the previous ones
intact. A user who tries three approaches and prefers the first must be able to
go back to it; a system that only keeps the newest has thrown away the work it
charged them for.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Seconds,
    TimeSpan,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.visual_plan import VisualStrategy


class VisualUnitStatus(str, Enum):
    """Where this visual is in its life.

    The frontend renders progress from these, so they are ordered roughly by
    how far along the unit is — but the ordering is presentational, not
    semantic, and nothing branches on it.
    """

    #: The director has decided what this should be; nothing has been fetched.
    PLANNED = "planned"
    #: Looking for a licensed asset.
    SEARCHING = "searching"
    #: A provider is producing an image or a video.
    GENERATING = "generating"
    #: There is a usable visual.
    READY = "ready"
    #: The user has looked at it and said yes.
    APPROVED = "approved"
    #: The user has said "keep this one". Automatic processes must not replace
    #: it. Distinct from APPROVED: approval is an opinion, a lock is a rule.
    LOCKED = "locked"
    #: A new version is being produced. The previous one is still current until
    #: the new one succeeds, so a failed regeneration leaves a working video.
    REGENERATING = "regenerating"
    #: Could not be produced. Scoped to this unit; the project survives.
    FAILED = "failed"
    #: Produced, but not the way it was planned — the ladder descended.
    DEGRADED = "degraded"
    #: The narration under this unit changed. Its span is stale.
    TIMING_INVALIDATED = "timing_invalidated"


#: States a user has deliberately put a unit into. Automatic processes must not
#: move a unit out of one of these without being told to.
USER_OWNED_STATES: frozenset[VisualUnitStatus] = frozenset(
    {VisualUnitStatus.APPROVED, VisualUnitStatus.LOCKED}
)

#: States from which a regeneration may start. Regenerating a `LOCKED` unit
#: requires an explicit unlock — the request is refused rather than silently
#: honoured, because "I locked it and it changed anyway" is unrecoverable trust.
REGENERATABLE_STATES: frozenset[VisualUnitStatus] = frozenset(
    {
        VisualUnitStatus.PLANNED,
        VisualUnitStatus.SEARCHING,
        VisualUnitStatus.GENERATING,
        VisualUnitStatus.READY,
        VisualUnitStatus.APPROVED,
        VisualUnitStatus.FAILED,
        VisualUnitStatus.DEGRADED,
        VisualUnitStatus.TIMING_INVALIDATED,
    }
)


class GroundingStatus(str, Enum):
    """Whether this visual's factual claims are supported.

    Re-evaluated after every regeneration. A new version is a new claim, and a
    grounding verdict that carries over from the version before it is a verdict
    about something else.
    """

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    GROUNDED = "grounded"
    REFUSED = "refused"


class ConsistencyStatus(str, Enum):
    """Whether this visual obeys the project's Visual Bible."""

    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    CONSISTENT = "consistent"
    #: Uses a different asset or colour for an entity that is bound elsewhere.
    CONFLICTING = "conflicting"


class RegenerationIntent(str, Enum):
    """What the user asked for when they pressed regenerate.

    A closed set, mapped to structured director instructions. Free text here
    would be a prompt-injection surface reaching a model that decides what
    appears in someone's video.
    """

    SAME_IDEA = "same_idea"
    MORE_CINEMATIC = "more_cinematic"
    MORE_REALISTIC = "more_realistic"
    MORE_EDUCATIONAL = "more_educational"
    SIMPLER = "simpler"
    USE_REAL_SOURCE = "use_real_source"
    USE_ANIMATION = "use_animation"
    USE_GENERATED_IMAGE = "use_generated_image"
    USE_GENERATED_VIDEO = "use_generated_video"
    USE_TYPOGRAPHY = "use_typography"


class VisualVersion(VTVModel):
    """One attempt at this visual, kept forever.

    A user who tried three approaches and prefers the first must be able to
    return to it. A system that keeps only the newest has discarded work it
    charged for.
    """

    version_id: Id = Field(default_factory=lambda: new_id(IdPrefix.VISUAL_VERSION))
    #: Monotonic within the unit, starting at 1. What the user sees as "v2".
    version: int = Field(ge=1)

    strategy: VisualStrategy
    #: The stored bytes, when there are any. Programmatic visuals have none —
    #: they are a spec the renderer draws, which is why this is optional.
    object: ObjectRef | None = None
    asset_id: Id | None = None
    generation_id: Id | None = None
    #: Serialised animation spec for a programmatic visual. `Any` rather than
    #: `object` because the field named `object` above shadows the builtin
    #: inside this class body — the existing `Asset` contract uses the same
    #: field name, and matching it is worth one import.
    spec: dict[str, Any] | None = None

    #: Why this version exists. `None` for the first.
    intent: RegenerationIntent | None = None
    #: The version this was produced from, so a lineage is reconstructable.
    parent_version_id: Id | None = None

    grounding: GroundingStatus = GroundingStatus.NOT_APPLICABLE
    consistency: ConsistencyStatus = ConsistencyStatus.NOT_APPLICABLE

    #: What this version cost to produce. Per-version, not per-unit, because
    #: "regenerating cost me four times" is a question users ask.
    cost_usd: float = Field(default=0.0, ge=0.0)
    #: One line the user can read: why the director chose this.
    rationale: str = Field(default="", max_length=400)

    #: This version is the user's own file.
    #:
    #: Recorded rather than inferred. `EXISTING_ASSET` covers both "a file the
    #: user uploaded" and "something already in our library", and those two
    #: carry opposite answers to the only question the origin badge exists to
    #: answer: is there a licence question here. A client that guessed from the
    #: strategy would mislabel one of them, and the promise is that no asset is
    #: ever shown without its origin — which is only worth making if the origin
    #: is right.
    user_owned: bool = False

    #: The credit line this picture must be shown with, when it has one.
    #:
    #: Not decoration. A CC-BY photograph used without attribution is a licence
    #: breach, and the person exposed to it is the customer who published the
    #: video. `Asset.attribution_line()` composes it, the renderer draws it, and
    #: `Timeline.attributions` collects it — all of which existed before this
    #: field, and none of which the product lane could reach, because a
    #: `VisualUnit` had nowhere to keep it between the search and the encode.
    #:
    #: While the commons rung was unreachable this cost nothing. It became
    #: urgent the moment that rung started returning photographs.
    attribution: str | None = Field(default=None, max_length=300)

    @property
    def is_usable(self) -> bool:
        """Has something to show, and nothing has refused it."""
        if self.grounding is GroundingStatus.REFUSED:
            return False
        return self.object is not None or self.spec is not None

    @property
    def origin(self) -> str:
        """Where this picture came from, in the badge's vocabulary.

        The same four words the media library uses, so one badge component
        serves the library card, the script block, the timeline clip and the
        inspector — four places, one vocabulary, as the design requires.
        """
        if self.user_owned:
            return "user_upload"
        if self.strategy is VisualStrategy.PROGRAMMATIC:
            return "programmatic"
        if self.strategy in {
            VisualStrategy.GENERATED_IMAGE,
            VisualStrategy.GENERATED_VIDEO,
        }:
            return "ai_generated"
        return "licensed_source"


class VisualUnit(RootDocument, Timestamped):
    """One visual moment, addressable and owned by the user."""

    document_name = "visual_unit"

    visual_unit_id: Id = Field(default_factory=lambda: new_id(IdPrefix.VISUAL_UNIT))
    organisation_id: Id
    project_id: Id

    #: Position in the video, zero-based. What the UI calls "Visual Unit 07".
    index: int = Field(ge=0)

    #: The narration this covers. Several blocks share one visual when they are
    #: one idea — the eye does not want a cut per sentence.
    script_block_ids: list[Id] = Field(default_factory=list, max_length=64)
    #: The planning artefact this came from, when there is one. Nullable
    #: because a unit outlives the scene graph that produced it.
    scene_id: Id | None = None

    #: Where this sits on the narration clock.
    span: TimeSpan | None = None

    versions: list[VisualVersion] = Field(default_factory=list, max_length=32)
    #: Which version is in the video. Not necessarily the newest — the user may
    #: have gone back to v1 after trying v2 and v3.
    selected_version_id: Id | None = None

    status: VisualUnitStatus = VisualUnitStatus.PLANNED
    #: A user decision, not a status. Kept separate so that a regeneration
    #: which moves `status` cannot clear the lock as a side effect.
    locked: bool = False

    #: Populated when `status` is FAILED or DEGRADED. Shown to the user.
    detail: str = Field(default="", max_length=400)

    # -- versions ---------------------------------------------------------

    @property
    def selected(self) -> VisualVersion | None:
        if self.selected_version_id is None:
            return self.versions[-1] if self.versions else None
        for version in self.versions:
            if version.version_id == self.selected_version_id:
                return version
        return None

    @property
    def latest(self) -> VisualVersion | None:
        return self.versions[-1] if self.versions else None

    @property
    def next_version_number(self) -> int:
        return max((item.version for item in self.versions), default=0) + 1

    def version_by_id(self, version_id: str) -> VisualVersion | None:
        for version in self.versions:
            if version.version_id == version_id:
                return version
        return None

    def add_version(self, version: VisualVersion, *, select: bool = True) -> VisualVersion:
        """Append a version, optionally making it the one in the video.

        `select=False` exists for the case where a regeneration produced
        something the grounding gate refused: the version is kept, so the user
        can see what was tried and why it was rejected, but it does not become
        what plays.
        """
        self.versions = [*self.versions, version]
        if select:
            self.selected_version_id = version.version_id
        return version

    def select_version(self, version_id: str) -> VisualVersion:
        version = self.version_by_id(version_id)
        if version is None:
            raise ValueError(f"unknown version {version_id!r} for this visual")
        if not version.is_usable:
            raise ValueError("that version was refused and cannot be selected")
        self.selected_version_id = version_id
        return version

    # -- state ------------------------------------------------------------

    @property
    def is_user_owned(self) -> bool:
        """The user has expressed a preference that must survive a re-plan."""
        return self.locked or self.status in USER_OWNED_STATES

    @property
    def shows_user_media(self) -> bool:
        """The picture here is a file the user brought.

        Distinct from `is_user_owned`, which is about a *state* the user put the
        unit into. This is about the content: somebody uploaded a photograph and
        made it this shot. They may have unlocked it afterwards — unlocking says
        "the system may propose something else", which is a statement about
        regeneration. It is not permission for a regrouping to delete the
        binding silently, leaving a fresh empty unit and no trace that a file
        was ever here.

        Read from the selected version rather than from the unit's status,
        because the status is whatever the last operation set and the version is
        what is actually on screen.
        """
        selected = self.selected
        return bool(selected and selected.user_owned)

    @property
    def may_regenerate(self) -> bool:
        """A locked unit is refused, not silently regenerated.

        This is the property `RegenerationService` checks. Making it a method on
        the object rather than an `if` at the call site is deliberate: the audit
        found six guarantees that lived at call sites and were forgotten at one
        of them.
        """
        return not self.locked and self.status in REGENERATABLE_STATES

    @property
    def is_deliverable(self) -> bool:
        """There is something to put on the timeline."""
        selected = self.selected
        return selected is not None and selected.is_usable

    @property
    def duration_seconds(self) -> Seconds:
        return round(self.span.end - self.span.start, 3) if self.span else 0.0

    def invalidate_timing(self, reason: str = "the narration under this changed") -> None:
        """Mark the span stale. Never silently keep it."""
        if self.locked:
            # A locked *visual* can still have stale *timing*: the lock is about
            # which picture, not about where it sits. Recording it on a locked
            # unit is what lets the editor warn instead of quietly re-cutting.
            self.detail = reason
            return
        self.status = VisualUnitStatus.TIMING_INVALIDATED
        self.detail = reason

    @model_validator(mode="after")
    def _versions_are_coherent(self) -> VisualUnit:
        numbers = [item.version for item in self.versions]
        if numbers != sorted(numbers):
            raise ValueError("visual versions must be in ascending order")
        if len(set(numbers)) != len(numbers):
            raise ValueError("visual version numbers must be unique")
        identifiers = {item.version_id for item in self.versions}
        if self.selected_version_id and self.selected_version_id not in identifiers:
            raise ValueError("the selected version is not one of this unit's versions")
        if self.locked and self.status is VisualUnitStatus.REGENERATING:
            raise ValueError("a locked visual cannot be regenerating")
        return self


__all__ = [
    "REGENERATABLE_STATES",
    "USER_OWNED_STATES",
    "ConsistencyStatus",
    "GroundingStatus",
    "RegenerationIntent",
    "VisualUnit",
    "VisualUnitStatus",
    "VisualVersion",
]
