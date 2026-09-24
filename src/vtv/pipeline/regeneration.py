"""Regenerating one visual — the operation the whole product turns on.

A user watches their video, dislikes shot seven, and presses regenerate. What
happens next decides whether this is an editor or a slot machine:

* shots one to six and eight to forty must be **untouched** — not
  re-planned, not re-fetched, not re-charged
* a shot the user **locked** must be refused, loudly, not quietly skipped
* the new visual must be **re-grounded**: a new picture is a new claim, and a
  grounding verdict inherited from the version before it is a verdict about
  something else
* the new visual must be **re-checked against the Visual Bible**, for the same
  reason
* the previous version must **survive**, because "actually the first one was
  better" is the second most common thing a user says
* only the **affected region** should be re-rendered

Every one of those is a property of this module, and every one of them is a
place where the audit's recurring defect could reappear — a guarantee that
lives at a call site and is forgotten at one of them. So they live here, on the
path, and the tests assert them through this service rather than around it.

## Why the previous version stays selected until the new one succeeds

A regeneration that fails must leave a working video. If the unit's selected
version were cleared at the start, a provider outage would turn "shot seven
looks wrong" into "shot seven is missing" — strictly worse, and caused by us.

So the sequence is: mark `REGENERATING`, produce a candidate, validate it,
*then* select it. A failure at any point leaves the old version selected and the
unit `FAILED` with a reason. The project stays renderable throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from vtv.contracts.errors import ErrorCode, PolicyViolation, VTVError
from vtv.contracts.render_scope import RenderRegion, RenderScope
from vtv.contracts.tracks import EditTimeline
from vtv.contracts.visual_unit import (
    ConsistencyStatus,
    GroundingStatus,
    RegenerationIntent,
    VisualUnit,
    VisualUnitStatus,
    VisualVersion,
)
from vtv.observability.events import EventName, EventSink


class VersionProducer(Protocol):
    """Produces one new version of one visual.

    A port, so this service can be tested without a provider and so the real
    implementation — director plus asset resolver plus generation router — is
    not a dependency of the locking and grounding rules that surround it.
    """

    async def produce(
        self,
        *,
        unit: VisualUnit,
        intent: RegenerationIntent,
        narration: str,
    ) -> VisualVersion:
        """Return a candidate. Raise `VTVError` if nothing can be produced."""
        ...


class VersionValidator(Protocol):
    """Decides whether a candidate may be shown.

    Two questions, deliberately one interface: is it grounded, and is it
    consistent with the project's Visual Bible. Both must be re-asked for every
    new version, and putting them behind one call means a caller cannot ask one
    and forget the other.
    """

    def validate(
        self, *, unit: VisualUnit, version: VisualVersion, narration: str
    ) -> tuple[GroundingStatus, ConsistencyStatus, str]:
        """Return `(grounding, consistency, reason)`."""
        ...


@dataclass(frozen=True)
class RegenerationResult:
    """What one regeneration did."""

    unit: VisualUnit
    version: VisualVersion | None
    accepted: bool
    reason: str = ""
    #: What must be re-rendered. Empty when nothing changed on screen.
    region: RenderRegion | None = None

    @property
    def is_refused(self) -> bool:
        return not self.accepted


def _wrapped(error: Exception) -> VTVError:
    """Turn anything at all into a `VTVError` without leaking its text.

    An adapter that raises a bare `TimeoutError` is a bug in that adapter. The
    user should still get a sentence rather than a stack trace, and the
    operator should still get the real type — so the type goes in the internal
    message and a generic apology goes in the user-facing one.
    """
    return VTVError(
        f"unhandled {type(error).__name__} from a provider adapter: {error}",
        code=ErrorCode.GENERATION_FAILED,
        user_message="We could not create that visual just now.",
    )


def _status_after_failure(
    previous: VisualVersion | None, was: VisualUnitStatus
) -> VisualUnitStatus:
    """Where a unit lands when a regeneration does not produce a new version.

    With nothing showable there is nothing to show, so the unit failed. With
    something showable, the unit is exactly where it was — including still being
    marked stale if it was stale. Collapsing every survivable failure to `READY`
    would let a failed operation clear a warning it never addressed.

    The test is `is_usable`, not "a previous version exists". `selected` returns
    the latest version even when the last one stored was *refused* by the
    grounding gate and deliberately not selected, so existence alone would call
    a unit `READY` while `is_deliverable` was false — a status that lies, which
    is the exact failure this function was written to stop.
    """
    if previous is None or not previous.is_usable:
        return VisualUnitStatus.FAILED
    if was in {
        VisualUnitStatus.TIMING_INVALIDATED,
        VisualUnitStatus.DEGRADED,
        VisualUnitStatus.APPROVED,
    }:
        return was
    return VisualUnitStatus.READY


@dataclass
class RegenerationService:
    """The chokepoint for changing a visual.

    Every path that replaces what a unit shows goes through `regenerate`. Not
    because it is convenient, but because the lock check, the grounding check
    and the Bible check are the kind of guarantee that is only real when there
    is exactly one place to put it.
    """

    producer: VersionProducer
    validator: VersionValidator
    events: EventSink = field(default_factory=EventSink)

    async def regenerate(
        self,
        unit: VisualUnit,
        *,
        narration: str,
        intent: RegenerationIntent = RegenerationIntent.SAME_IDEA,
        timeline: EditTimeline | None = None,
    ) -> RegenerationResult:
        """Produce a new version of one visual and nothing else."""
        if unit.locked:
            # Refused, not skipped. A user who locked a visual and watched it
            # change anyway has learned the control does not work, and nothing
            # recovers from that.
            raise PolicyViolation(
                f"visual {unit.index + 1} is locked; unlock it to regenerate it",
                code=ErrorCode.PERMISSION_DENIED,
                user_message=(
                    f"Visual {unit.index + 1} is locked. Unlock it first if you "
                    "want it regenerated."
                ),
            )
        if not unit.may_regenerate:
            raise PolicyViolation(
                f"visual {unit.index + 1} is {unit.status.value} and cannot be "
                "regenerated right now",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"Visual {unit.index + 1} is busy. Wait for it to finish."
                ),
            )

        working = unit.model_copy(deep=True)
        previous = working.selected
        # Remembered so a failure can put the unit back where it was. An
        # earlier version always fell back to READY, which quietly erased a
        # `TIMING_INVALIDATED` marker — an operation that achieved nothing
        # would leave the timeline looking correct when it was not.
        was_status, was_detail = unit.status, unit.detail
        working.status = VisualUnitStatus.REGENERATING

        try:
            candidate = await self.producer.produce(
                unit=working, intent=intent, narration=narration
            )
        except Exception as raised:
            # Deliberately not `except VTVError`. A provider adapter that lets a
            # `TimeoutError` or a `KeyError` through is a bug, but the cost of
            # that bug must be one failed visual, not a failed project — this is
            # the boundary where "one bad visual does not destroy a project"
            # either holds or does not.
            error = raised if isinstance(raised, VTVError) else _wrapped(raised)
            # The old version is still selected. The video still plays.
            working.status = _status_after_failure(previous, was_status)
            working.detail = error.info.user_message or "that visual could not be made"
            if working.status is was_status and was_detail:
                working.detail = was_detail
            self.events.emit(
                EventName.VISUAL_UNIT_FAILED,
                project_id=working.project_id,
                data={
                    "visual_unit_id": working.visual_unit_id,
                    "index": working.index,
                    "intent": intent.value,
                    "code": error.info.code.value,
                    # Named so an operator can see that the project survived.
                    "project_still_renderable": bool(previous and previous.is_usable),
                },
            )
            return RegenerationResult(
                unit=working, version=None, accepted=False, reason=working.detail
            )

        candidate = candidate.model_copy(
            update={
                "version": working.next_version_number,
                "intent": intent,
                "parent_version_id": previous.version_id if previous else None,
            }
        )

        # A new picture is a new claim. Both verdicts are recomputed; neither is
        # inherited. This is the P0-1 lesson applied to the editing path — the
        # gate that guards the first render must guard the fortieth too.
        #
        # Guarded like the producer call: a gate that throws must fail closed on
        # this one unit, not escape and take the project with it.
        try:
            grounding, consistency, reason = self.validator.validate(
                unit=working, version=candidate, narration=narration
            )
        except Exception as raised:
            error = raised if isinstance(raised, VTVError) else _wrapped(raised)
            working.status = _status_after_failure(previous, was_status)
            working.detail = (
                error.info.user_message or "that visual could not be checked"
            )
            # Same rule as the producer branch: a failure that changed nothing
            # must not overwrite the explanation of why the unit was already
            # marked.
            if working.status is was_status and was_detail:
                working.detail = was_detail
            self.events.emit(
                EventName.VISUAL_UNIT_FAILED,
                project_id=working.project_id,
                data={
                    "visual_unit_id": working.visual_unit_id,
                    "index": working.index,
                    "intent": intent.value,
                    "code": error.info.code.value,
                    "stage": "validation",
                    "project_still_renderable": bool(previous and previous.is_usable),
                },
            )
            return RegenerationResult(
                unit=working, version=None, accepted=False, reason=working.detail
            )
        candidate = candidate.model_copy(
            update={"grounding": grounding, "consistency": consistency}
        )

        if grounding is GroundingStatus.REFUSED:
            # Kept, not discarded: the user asked for it and paid for it, and
            # showing them what was refused and why is more useful than a
            # shrug. It simply does not become what plays.
            working.add_version(candidate, select=False)
            working.status = (
                VisualUnitStatus.READY if previous else VisualUnitStatus.FAILED
            )
            working.detail = reason or "that visual claimed something unsupported"
            self.events.emit(
                EventName.GROUNDING_REFUSED,
                project_id=working.project_id,
                data={
                    "visual_unit_id": working.visual_unit_id,
                    "version": candidate.version,
                    "reason": working.detail,
                    "primitive": (candidate.spec or {}).get("primitive", "unknown"),
                },
            )
            return RegenerationResult(
                unit=working,
                version=candidate,
                accepted=False,
                reason=working.detail,
            )

        working.add_version(candidate, select=True)
        working.status = (
            VisualUnitStatus.DEGRADED
            if consistency is ConsistencyStatus.CONFLICTING
            else VisualUnitStatus.READY
        )
        working.detail = (
            reason if consistency is ConsistencyStatus.CONFLICTING else ""
        )

        region = self._region_for(working, timeline)
        self.events.emit(
            EventName.VISUAL_UNIT_REGENERATED,
            project_id=working.project_id,
            cost_usd=candidate.cost_usd,
            data={
                "visual_unit_id": working.visual_unit_id,
                "index": working.index,
                "intent": intent.value,
                "version": candidate.version,
                "strategy": candidate.strategy.value,
                "consistency": consistency.value,
                # The number that proves the product's economics: how much of
                # the video this cost to change.
                "render_scope": region.scope.value if region else "none",
            },
        )
        return RegenerationResult(
            unit=working, version=candidate, accepted=True, region=region
        )

    async def regenerate_project(
        self,
        units: list[VisualUnit],
        *,
        narrations: dict[str, str],
        intent: RegenerationIntent = RegenerationIntent.SAME_IDEA,
    ) -> tuple[list[VisualUnit], list[RegenerationResult]]:
        """Regenerate everything the user has not claimed.

        A locked unit is stepped around silently *here* — unlike the
        single-unit path, which refuses. The difference is what the user asked
        for: "regenerate this one" aimed at a locked unit is a mistake worth
        reporting, while "regenerate the project" means "the ones I have not
        decided about", and refusing the whole operation because one unit is
        locked would make locking unusable.
        """
        out: list[VisualUnit] = []
        results: list[RegenerationResult] = []
        for unit in units:
            if unit.locked or not unit.may_regenerate:
                out.append(unit)
                continue
            try:
                result = await self.regenerate(
                    unit,
                    narration=narrations.get(unit.visual_unit_id, ""),
                    intent=intent,
                )
            except Exception as raised:
                # `regenerate` already absorbs everything a producer or a
                # validator can throw, so reaching here means the failure was in
                # this service's own bookkeeping. Even then: one unit fails, the
                # batch continues. Aborting would discard every unit already
                # regenerated *and* paid for, which is the most expensive
                # possible reaction to a bug.
                error = raised if isinstance(raised, VTVError) else _wrapped(raised)
                failed = unit.model_copy(deep=True)
                failed.status = _status_after_failure(unit.selected, unit.status)
                failed.detail = (
                    error.info.user_message or "that visual could not be made"
                )
                out.append(failed)
                results.append(
                    RegenerationResult(
                        unit=failed,
                        version=None,
                        accepted=False,
                        reason=failed.detail,
                    )
                )
                continue
            out.append(result.unit)
            results.append(result)
        return out, results

    def select_version(
        self, unit: VisualUnit, *, version_id: str, timeline: EditTimeline | None = None
    ) -> RegenerationResult:
        """Go back to an earlier version. Free, and instant.

        No provider call, no cost. The whole point of keeping versions is that
        changing your mind about which one to use should not cost anything.
        """
        if unit.locked:
            raise PolicyViolation(
                f"visual {unit.index + 1} is locked; unlock it to change it",
                code=ErrorCode.PERMISSION_DENIED,
                user_message=(
                    f"Visual {unit.index + 1} is locked. Unlock it to change it."
                ),
            )
        working = unit.model_copy(deep=True)
        version = working.select_version(version_id)
        working.status = VisualUnitStatus.READY
        return RegenerationResult(
            unit=working,
            version=version,
            accepted=True,
            region=self._region_for(working, timeline),
        )

    def set_locked(self, unit: VisualUnit, *, locked: bool) -> VisualUnit:
        """Pin or release a visual.

        `locked` is a field of its own rather than a status, so a later
        regeneration that moves `status` cannot clear the lock as a side
        effect — which is exactly how locks stop working in systems that
        conflate the two.
        """
        working = unit.model_copy(deep=True)
        working.locked = locked
        if locked:
            working.status = VisualUnitStatus.LOCKED
        elif working.status is VisualUnitStatus.LOCKED:
            working.status = (
                VisualUnitStatus.READY if working.is_deliverable else VisualUnitStatus.PLANNED
            )
        self.events.emit(
            EventName.VISUAL_UNIT_LOCKED,
            project_id=working.project_id,
            data={
                "visual_unit_id": working.visual_unit_id,
                "index": working.index,
                "locked": locked,
            },
        )
        return working

    # -- scope ------------------------------------------------------------

    def _region_for(
        self, unit: VisualUnit, timeline: EditTimeline | None
    ) -> RenderRegion | None:
        """The smallest window that must be re-rendered.

        `None` when there is no timeline yet — nothing has been rendered, so
        nothing needs re-rendering, and returning a full-project scope would
        make the first regeneration look expensive.
        """
        if timeline is None:
            return None
        clips = timeline.clips_for_unit(unit.visual_unit_id)
        if not clips:
            return None
        start = min(clip.start for clip in clips)
        end = max(clip.end for clip in clips)
        region = RenderRegion(
            scope=RenderScope.SCENE,
            start=start,
            end=end,
            clip_ids=[clip.clip_id for clip in clips],
            visual_unit_ids=[unit.visual_unit_id],
        )
        # Widen for the transitions either side, or the seam is visible.
        return region.expand(limit=timeline.duration)


# ---------------------------------------------------------------------------
# A validator over the existing grounding and consistency machinery
# ---------------------------------------------------------------------------

@dataclass
class GateValidator:
    """Wires regeneration to the grounding gate and the Visual Bible.

    Deliberately thin. The rules live where they already lived — `check_spec`
    and the `VisualBible` — and this only translates between their vocabulary
    and the visual unit's. A second implementation of either rule here is how
    the two would drift.
    """

    #: `vtv.pipeline.grounding.Evidence`, built from the narration. Optional so
    #: a caller that has no evidence gets `PENDING` rather than a false pass.
    evidence: Any = None
    #: The project's `VisualBible`, when there is one.
    visual_bible: Any = None

    def validate(
        self, *, unit: VisualUnit, version: VisualVersion, narration: str
    ) -> tuple[GroundingStatus, ConsistencyStatus, str]:
        grounding, reason = self._ground(version, narration)
        consistency = self._consistency(version)
        if consistency is ConsistencyStatus.CONFLICTING and not reason:
            reason = "this uses a different look for something bound elsewhere"
        del unit
        return grounding, consistency, reason

    def _ground(
        self, version: VisualVersion, narration: str
    ) -> tuple[GroundingStatus, str]:
        if version.spec is None:
            # A photograph makes no checkable claim of the kind `check_spec`
            # evaluates. Licence and provenance are checked elsewhere, by the
            # asset resolver, and duplicating that here would give two answers.
            return GroundingStatus.NOT_APPLICABLE, ""
        if self.evidence is None:
            return GroundingStatus.PENDING, "no evidence was available to check against"

        from vtv.pipeline.grounding import check_spec

        try:
            spec = _spec_from(version.spec)
        except Exception:
            # An unparseable spec is refused, not skipped. A visual whose
            # description we cannot read is a visual whose claims we cannot
            # check, and unchecked is what the grounding gate exists to prevent.
            return GroundingStatus.REFUSED, "that visual's specification was malformed"

        result = check_spec(spec, self.evidence)
        del narration
        if not result.is_acceptable:
            return GroundingStatus.REFUSED, result.reason()
        return GroundingStatus.GROUNDED, ""

    def _consistency(self, version: VisualVersion) -> ConsistencyStatus:
        if self.visual_bible is None:
            return ConsistencyStatus.NOT_APPLICABLE
        from vtv.contracts.consistency import BindingKind

        # Only asset choices can conflict with a binding; a drawn visual takes
        # its colours from the locked palette by construction.
        if version.object is None:
            return ConsistencyStatus.CONSISTENT
        for binding in getattr(self.visual_bible, "bindings", []):
            if binding.kind is not BindingKind.ASSET or binding.asset is None:
                continue
            if binding.is_locked and binding.asset.key != version.object.key:
                return ConsistencyStatus.CONFLICTING
        return ConsistencyStatus.CONSISTENT


def _spec_from(payload: dict[str, Any]) -> Any:
    """Rebuild an animation spec from its serialised form."""
    from pydantic import TypeAdapter

    from vtv.contracts.visual_language import AnimationSpec

    return TypeAdapter(AnimationSpec).validate_python(payload)


__all__ = [
    "GateValidator",
    "RegenerationResult",
    "RegenerationService",
    "VersionProducer",
    "VersionValidator",
]
