"""Timeline operations — every edit validated before it commits.

One rule, and everything here follows from it:

> **An operation either produces a valid timeline or raises. It never leaves a
> half-applied one.**

Each operation works on a deep copy, mutates that, and re-validates the whole
document through Pydantic before returning it. The caller still holds the
original if anything failed. This costs a copy per edit — kilobytes, on an
operation a human triggers — and buys the property that a failed trim cannot
strand a clip with a negative duration that the next operation then divides by.

## Why operations are objects

An `Operation` is a validated request, not a method call with loose arguments.
That is what makes an undo stack, an audit trail and an idempotent HTTP endpoint
all express the same thing, and it means a malformed edit is rejected by the
contract layer before any code has to think about it.

## What is refused, and why refusal is the feature

* **A locked clip.** The user pinned it. An operation that moved it anyway
  would make locking advisory, and an advisory lock is worse than none: it
  invites reliance it cannot support.
* **A derived track.** Captions come from the transcript. Hand-editing one
  desynchronises it from the words, and the next re-plan silently discards the
  edit — so the edit is refused at the point it is made, while there is still a
  person to tell.
* **An overlap on an exclusive track.** Two pictures at one instant is a
  composite, which is a different feature with different semantics.

Each refusal names what to do instead. "Refused" with no next step is a dead end
a user cannot get out of.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import Field

from vtv.contracts.base import Id, Seconds, VTVModel
from vtv.contracts.errors import ErrorCode, PolicyViolation, ValidationFailed
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


class OperationKind(str, Enum):
    INSERT = "insert"
    REMOVE = "remove"
    MOVE = "move"
    TRIM = "trim"
    SPLIT = "split"
    EXTEND = "extend"
    REPLACE_SOURCE = "replace_source"
    LOCK = "lock"
    UNLOCK = "unlock"
    SET_TRANSITION = "set_transition"
    SET_GAIN = "set_gain"
    ADD_TRACK = "add_track"
    REMOVE_TRACK = "remove_track"
    SET_TRACK = "set_track"


class TimelineOperation(VTVModel):
    """One validated edit.

    A single model with optional fields rather than a discriminated union of
    thirteen, because the API takes these as JSON and a union of thirteen
    shapes is a client burden out of proportion to the safety it buys here —
    the operation-specific requirements are checked in `apply`, which has to
    check them against the *timeline* anyway.
    """

    kind: OperationKind

    clip_id: Id | None = None
    track_id: Id | None = None

    #: INSERT, MOVE, TRIM, SPLIT, EXTEND.
    start: Seconds | None = Field(default=None, ge=0.0)
    end: Seconds | None = Field(default=None, ge=0.0)
    at: Seconds | None = Field(default=None, ge=0.0)

    #: INSERT and REPLACE_SOURCE.
    visual_unit_id: Id | None = None
    source_kind: ClipSourceKind | None = None
    #: INSERT and REPLACE_SOURCE, alternative to `object`.
    #:
    #: A file from the project's own media library, named by id. The API layer
    #: resolves it to the `object` below before this module sees the operation.
    #:
    #: It exists because the alternative is worse: placing an uploaded file on
    #: a lane otherwise requires the client to hold a storage `ObjectRef`, which
    #: means the asset view has to hand out object keys — a tenant's key space,
    #: published to every browser, to save one lookup. Naming an asset the
    #: caller can already see and letting the server resolve it keeps keys where
    #: they belong and makes the tenant check structural rather than remembered.
    media_asset_id: Id | None = None
    #: Serialised `ObjectRef`, kept loose so the API layer can pass what it got
    #: and this layer can validate it into the contract.
    object: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    text: str | None = Field(default=None, max_length=2000)

    #: SET_TRANSITION.
    transition_in: TransitionKind | None = None
    transition_out: TransitionKind | None = None
    transition_seconds: Seconds | None = Field(default=None, ge=0.0, le=8.0)

    #: SET_GAIN.
    gain: float | None = Field(default=None, ge=0.0, le=1.0)

    #: ADD_TRACK.
    track_kind: TrackKind | None = None
    track_name: str = Field(default="", max_length=64)

    #: SET_TRACK. Either, both, or neither — omitting one leaves it alone.
    muted: bool | None = None
    track_locked: bool | None = None

    #: Set by an operator or by the system to move a clip the user locked.
    #: Never defaulted true, and audited by the caller when it is used.
    force: bool = False


@dataclass(frozen=True)
class EditResult:
    """The new timeline, and what changed.

    `affected_unit_ids` is what drives partial rendering: an edit that touched
    three clips does not require re-rendering the other forty.
    """

    timeline: EditTimeline
    changed_clip_ids: tuple[str, ...] = ()
    affected_unit_ids: tuple[str, ...] = ()
    #: Non-fatal observations. A gap opened by a trim is legal and worth saying.
    warnings: tuple[str, ...] = ()


@dataclass
class TimelineEditor:
    """Applies operations. Holds no state; every method is a pure function.

    Stateless on purpose: two API replicas editing the same project must not
    depend on either of them having seen the previous edit, and the version
    check in `apply` is what makes concurrent edits detectable rather than
    last-writer-wins.
    """

    def apply(
        self,
        timeline: EditTimeline,
        operation: TimelineOperation,
        *,
        expected_version: int | None = None,
    ) -> EditResult:
        """Apply one operation, or raise leaving `timeline` untouched.

        `expected_version` is optimistic concurrency. Two people editing the
        same project is normal; the second one silently overwriting the first
        is not, and a version check is the cheapest way to turn that into a
        visible conflict.
        """
        if expected_version is not None and expected_version != timeline.version:
            raise PolicyViolation(
                "this timeline has changed since you loaded it; reload and retry",
                code=ErrorCode.SCHEMA_INVALID,
                # Stated explicitly. `PolicyViolation` defaults its user-facing
                # text to a licence message, so a refusal that does not name its
                # own reason reaches the user as something unrelated.
                user_message=(
                    "Someone else changed this timeline. Reload and try again."
                ),
            )

        working = timeline.model_copy(deep=True)
        handler = getattr(self, f"_{operation.kind.value}", None)
        if handler is None:  # pragma: no cover - the enum is exhaustive
            raise ValidationFailed(f"unsupported operation {operation.kind.value!r}")

        result: EditResult = handler(working, operation)
        # Re-validate the whole document. Pydantic runs every model validator,
        # so an operation that produced an overlap or a negative span fails
        # here rather than being discovered by the renderer.
        validated = EditTimeline.model_validate(
            result.timeline.model_dump(mode="python")
        )
        validated.version = timeline.version + 1
        return EditResult(
            timeline=validated,
            changed_clip_ids=result.changed_clip_ids,
            affected_unit_ids=result.affected_unit_ids,
            warnings=result.warnings,
        )

    def apply_all(
        self,
        timeline: EditTimeline,
        operations: list[TimelineOperation],
        *,
        expected_version: int | None = None,
    ) -> EditResult:
        """Apply a batch atomically.

        All or nothing: a batch that fails half way would leave the user with a
        timeline in a state they never asked for and cannot describe. The
        version advances once, not once per operation, so a batch is one undo
        step — which is what a user means by "that edit".
        """
        if expected_version is not None and expected_version != timeline.version:
            raise PolicyViolation(
                "this timeline has changed since you loaded it; reload and retry",
                code=ErrorCode.SCHEMA_INVALID,
                # Stated explicitly. `PolicyViolation` defaults its user-facing
                # text to a licence message, so a refusal that does not name its
                # own reason reaches the user as something unrelated.
                user_message=(
                    "Someone else changed this timeline. Reload and try again."
                ),
            )
        current = timeline
        changed: list[str] = []
        affected: list[str] = []
        warnings: list[str] = []
        for operation in operations:
            step = self.apply(current, operation)
            current = step.timeline
            changed.extend(step.changed_clip_ids)
            affected.extend(step.affected_unit_ids)
            warnings.extend(step.warnings)
        current.version = timeline.version + 1
        return EditResult(
            timeline=current,
            changed_clip_ids=tuple(dict.fromkeys(changed)),
            affected_unit_ids=tuple(dict.fromkeys(affected)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    # -- operations -------------------------------------------------------

    def _insert(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        track = self._writable_track(timeline, op)
        if op.start is None or op.end is None:
            raise ValidationFailed("an insert needs a start and an end")

        clip = TimelineClip(
            track_id=track.track_id,
            visual_unit_id=op.visual_unit_id,
            start=quantise(op.start),
            end=quantise(op.end),
            source_kind=op.source_kind or ClipSourceKind.EMPTY,
            object=_object_ref(op.object),
            spec=op.spec,
            text=op.text,
        )
        conflict = self._first_conflict(track, clip)
        if conflict is not None:
            raise PolicyViolation(
                f"that would overlap clip {conflict.clip_id} on the "
                f"{track.kind.value} track; move or trim it first",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"That would overlap another clip on the "
                    f"{track.kind.value} track. Move or trim it first."
                ),
            )
        track.clips = sorted([*track.clips, clip], key=lambda item: item.start)
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
        )

    def _remove(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        track, clip = self._writable_clip(timeline, op)
        track.clips = [item for item in track.clips if item.clip_id != clip.clip_id]
        warnings: list[str] = []
        if track.is_exclusive:
            warnings.append(
                f"removing this leaves a {clip.duration}s gap on the "
                f"{track.kind.value} track"
            )
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
            warnings=tuple(warnings),
        )

    def _move(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        track, clip = self._writable_clip(timeline, op)
        if op.start is None:
            raise ValidationFailed("a move needs a new start")
        duration = clip.duration
        moved = clip.model_copy(
            update={
                "start": quantise(op.start),
                "end": quantise(op.start + duration),
            }
        )
        self._replace_clip(track, clip, moved)
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
        )

    def _trim(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        track, clip = self._writable_clip(timeline, op)
        start = quantise(op.start) if op.start is not None else clip.start
        end = quantise(op.end) if op.end is not None else clip.end
        if end - start < MIN_CLIP_SECONDS:
            raise ValidationFailed(
                f"a clip must last at least {MIN_CLIP_SECONDS}s; "
                "remove it instead of trimming it to nothing"
            )
        trimmed = clip.model_copy(update={"start": start, "end": end})
        self._replace_clip(track, clip, trimmed)
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
        )

    def _extend(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        """A trim by delta rather than by absolute position.

        Separate from `_trim` because "hold this two seconds longer" is what a
        user means, and making them compute an absolute end from a start they
        cannot see is how off-by-one edits happen.
        """
        track, clip = self._writable_clip(timeline, op)
        if op.at is None:
            raise ValidationFailed("an extend needs a number of seconds")
        end = quantise(clip.end + op.at)
        if end - clip.start < MIN_CLIP_SECONDS:
            raise ValidationFailed("that would shorten the clip out of existence")
        extended = clip.model_copy(update={"end": end})
        self._replace_clip(track, clip, extended)
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
        )

    def _split(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        track, clip = self._writable_clip(timeline, op)
        if op.at is None:
            raise ValidationFailed("a split needs a position")
        at = quantise(op.at)
        if not (clip.start < at < clip.end):
            raise ValidationFailed("the split point must be inside the clip")
        if at - clip.start < MIN_CLIP_SECONDS or clip.end - at < MIN_CLIP_SECONDS:
            raise ValidationFailed(
                f"both halves must last at least {MIN_CLIP_SECONDS}s"
            )

        left = clip.model_copy(update={"end": at})
        # A new identity for the right half, and it keeps the visual unit: both
        # halves show the same picture, and both must still highlight the same
        # script line when clicked.
        right = clip.model_copy(
            update={"start": at, "clip_id": None}, deep=True
        )
        right = TimelineClip.model_validate(
            {**right.model_dump(mode="python", exclude={"clip_id"})}
        )
        track.clips = sorted(
            [item for item in track.clips if item.clip_id != clip.clip_id]
            + [left, right],
            key=lambda item: item.start,
        )
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(left.clip_id, right.clip_id),
            affected_unit_ids=_units(clip),
        )

    def _replace_source(
        self, timeline: EditTimeline, op: TimelineOperation
    ) -> EditResult:
        """Swap what a clip shows, keeping where and when it shows it.

        The operation behind "use v2 instead of v1". Identity and span are
        preserved deliberately: the script link, the seek target and any lock
        all hang off the clip id, and replacing the clip wholesale would break
        every one of them to change a picture.

        A locked clip refuses this like every other operation. An earlier
        version allowed it — reasoning that the *span* was unchanged, so the
        lock was not really being violated — which was wrong: a lock protects
        the picture, and this is the one operation that changes the picture.
        Selecting a different version for a locked unit is refused upstream by
        `RegenerationService.select_version` for the same reason.
        """
        track, clip = self._writable_clip(timeline, op)
        if op.source_kind is None:
            raise ValidationFailed("a replacement needs a source kind")
        replaced = clip.model_copy(
            update={
                "source_kind": op.source_kind,
                "object": _object_ref(op.object),
                "spec": op.spec,
                "text": op.text,
                "asset_id": None,
                "generation_id": None,
            }
        )
        self._replace_clip(track, clip, replaced)
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
        )

    def _lock(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        return self._set_lock(timeline, op, locked=True)

    def _unlock(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        return self._set_lock(timeline, op, locked=False)

    def _set_lock(
        self, timeline: EditTimeline, op: TimelineOperation, *, locked: bool
    ) -> EditResult:
        """Pin or unpin one clip.

        Deliberately does *not* go through `_writable_clip`: locking an already
        locked clip would refuse itself, and unlocking would be impossible. It
        does check the **track**, because a track lock is documented as the
        stronger of the two — without that check a user could unlock every clip
        on a lane they had explicitly pinned.
        """
        found = timeline.clip(op.clip_id or "")
        if found is None:
            raise ValidationFailed("no such clip")
        track, clip = found
        if track.locked and not op.force:
            raise PolicyViolation(
                f"the {track.kind.value} track is locked; unlock the track first",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"The {track.kind.value} track is locked. Unlock the track "
                    "first."
                ),
            )
        self._replace_clip(track, clip, clip.model_copy(update={"locked": locked}))
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
        )

    def _set_transition(
        self, timeline: EditTimeline, op: TimelineOperation
    ) -> EditResult:
        track, clip = self._writable_clip(timeline, op)
        update: dict[str, Any] = {}
        if op.transition_in is not None:
            update["transition_in"] = op.transition_in
        if op.transition_out is not None:
            update["transition_out"] = op.transition_out
        if op.transition_seconds is not None:
            seconds = quantise(op.transition_seconds)
            # A transition longer than the clip would consume the whole shot.
            if seconds * 2 > clip.duration:
                raise ValidationFailed(
                    "a transition cannot be longer than half the clip it is on"
                )
            if op.transition_in is not None:
                update["transition_in_seconds"] = seconds
            if op.transition_out is not None:
                update["transition_out_seconds"] = seconds
        self._replace_clip(track, clip, clip.model_copy(update=update))
        return EditResult(
            timeline=timeline,
            changed_clip_ids=(clip.clip_id,),
            affected_unit_ids=_units(clip),
        )

    def _set_gain(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        track, clip = self._writable_clip(timeline, op)
        if op.gain is None:
            raise ValidationFailed("a gain change needs a level")
        self._replace_clip(track, clip, clip.model_copy(update={"gain": op.gain}))
        return EditResult(timeline=timeline, changed_clip_ids=(clip.clip_id,))

    def _add_track(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        if op.track_kind is None:
            raise ValidationFailed("a new track needs a kind")
        if (
            op.track_kind in {TrackKind.NARRATION, TrackKind.VISUAL, TrackKind.CAPTION}
            and timeline.track_of_kind(op.track_kind) is not None
        ):
            raise ValidationFailed(f"there is already a {op.track_kind.value} track")
        track = Track(kind=op.track_kind, name=op.track_name)
        timeline.tracks = [*timeline.tracks, track]
        return EditResult(timeline=timeline)

    def _set_track(self, timeline: EditTimeline, op: TimelineOperation) -> EditResult:
        """Mute or lock a whole lane.

        `Track.muted` and `Track.locked` have existed since the contract was
        written, and `locked_clip_ids` has always folded `track.locked` into the
        set of clips no operation may touch — so the *rule* was implemented and
        enforced from the start. There was simply no way to set the flag. The
        editor drew a lock button with no handler behind it and a mute button
        that apologised, which is two controls that look like features.

        Muting a derived lane is allowed and locking one is redundant: the
        narration and caption tracks refuse every edit already, but a user who
        wants to hear the video without the voice is asking a mixing question,
        not trying to edit the script.
        """
        track = timeline.track(op.track_id or "")
        if track is None:
            raise ValidationFailed(
                "no such track",
                user_message="That lane is not in this project any more.",
            )
        if op.muted is None and op.track_locked is None:
            raise ValidationFailed("set_track needs `muted` or `track_locked`")

        update: dict[str, Any] = {}
        if op.muted is not None:
            update["muted"] = bool(op.muted)
        if op.track_locked is not None:
            update["locked"] = bool(op.track_locked)

        timeline.tracks = [
            item.model_copy(update=update) if item.track_id == track.track_id else item
            for item in timeline.tracks
        ]
        return EditResult(timeline=timeline)

    def _remove_track(
        self, timeline: EditTimeline, op: TimelineOperation
    ) -> EditResult:
        track = timeline.track(op.track_id or "")
        if track is None:
            raise ValidationFailed("no such track")
        if track.kind in {TrackKind.NARRATION, TrackKind.VISUAL}:
            raise PolicyViolation(
                f"the {track.kind.value} track cannot be removed; "
                "mute it instead",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"The {track.kind.value} track cannot be removed. "
                    "Mute it instead."
                ),
            )
        if track.is_derived and not op.force:
            # Removing a derived track is the largest possible edit to one, and
            # it was the one operation that never asked. Every other route into
            # a caption track refuses; deleting the whole thing did not.
            raise PolicyViolation(
                f"the {track.kind.value} track comes from your script and "
                "cannot be removed; mute it instead",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"The {track.kind.value} track comes from your script. "
                    "Mute it instead of removing it."
                ),
            )
        if track.locked and not op.force:
            raise PolicyViolation(
                "that track is locked; unlock it first",
                code=ErrorCode.SCHEMA_INVALID,
                user_message="That track is locked. Unlock it first.",
            )
        pinned = [clip for clip in track.clips if clip.locked]
        if pinned and not op.force:
            # A lock on a clip has to survive its container being deleted, or
            # "locked" means "until somebody removes the lane it is on".
            raise PolicyViolation(
                f"that track holds {len(pinned)} locked clip(s); "
                "unlock them first",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"That track holds {len(pinned)} locked clip"
                    f"{'s' if len(pinned) != 1 else ''}. Unlock "
                    f"{'them' if len(pinned) != 1 else 'it'} first."
                ),
            )
        timeline.tracks = [
            item for item in timeline.tracks if item.track_id != track.track_id
        ]
        return EditResult(
            timeline=timeline,
            changed_clip_ids=tuple(clip.clip_id for clip in track.clips),
            affected_unit_ids=tuple(
                dict.fromkeys(
                    clip.visual_unit_id
                    for clip in track.clips
                    if clip.visual_unit_id
                )
            ),
        )

    # -- helpers ----------------------------------------------------------

    def _writable_track(
        self, timeline: EditTimeline, op: TimelineOperation
    ) -> Track:
        track = timeline.track(op.track_id or "")
        if track is None:
            raise ValidationFailed("no such track")
        self._check_track_writable(track, op)
        return track

    def _writable_clip(
        self, timeline: EditTimeline, op: TimelineOperation
    ) -> tuple[Track, TimelineClip]:
        """The clip named by the operation, or a refusal.

        There is no `allow_locked` escape hatch. Every operation that changes a
        clip goes through here, and a lock that one operation may step over is
        not a lock — it is a suggestion with an exception list nobody can
        remember. The only override is `force`, which the API strips from the
        wire and only an operator path may set, with an audit record.
        """
        found = timeline.clip(op.clip_id or "")
        if found is None:
            raise ValidationFailed("no such clip")
        track, clip = found
        self._check_track_writable(track, op)
        if clip.locked and not op.force:
            raise PolicyViolation(
                f"clip {clip.clip_id} is locked; unlock it to change it",
                code=ErrorCode.SCHEMA_INVALID,
                user_message="That clip is locked. Unlock it to change it.",
            )
        return track, clip

    def _check_track_writable(self, track: Track, op: TimelineOperation) -> None:
        if track.locked and not op.force:
            raise PolicyViolation(
                f"the {track.kind.value} track is locked; unlock it to change it",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"The {track.kind.value} track is locked. "
                    "Unlock it to change it."
                ),
            )
        if track.is_derived and not op.force:
            raise PolicyViolation(
                f"the {track.kind.value} track is generated from the narration; "
                "edit the script instead — a hand edit here would be discarded "
                "the next time the project is re-planned",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"The {track.kind.value} track comes from your script. "
                    "Edit the script instead — a change here would be lost the "
                    "next time the project is re-planned."
                ),
            )

    def _first_conflict(
        self, track: Track, candidate: TimelineClip
    ) -> TimelineClip | None:
        if not track.is_exclusive:
            return None
        for existing in track.clips:
            if existing.clip_id == candidate.clip_id:
                continue
            if existing.overlaps(candidate):
                return existing
        return None

    def _replace_clip(
        self, track: Track, old: TimelineClip, new: TimelineClip
    ) -> None:
        conflict = self._first_conflict(track, new)
        if conflict is not None:
            raise PolicyViolation(
                f"that would overlap clip {conflict.clip_id} on the "
                f"{track.kind.value} track",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"That would overlap another clip on the "
                    f"{track.kind.value} track."
                ),
            )
        track.clips = sorted(
            [item for item in track.clips if item.clip_id != old.clip_id] + [new],
            key=lambda item: item.start,
        )


def _units(clip: TimelineClip) -> tuple[str, ...]:
    return (clip.visual_unit_id,) if clip.visual_unit_id else ()


def _object_ref(payload: dict[str, Any] | None):  # type: ignore[no-untyped-def]
    if payload is None:
        return None
    from vtv.contracts.base import ObjectRef

    return ObjectRef.model_validate(payload)


__all__ = ["EditResult", "OperationKind", "TimelineEditor", "TimelineOperation"]
