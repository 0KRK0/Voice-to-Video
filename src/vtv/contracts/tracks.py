"""The editable timeline — tracks and clips a user can actually manipulate.

The existing `Timeline` is a *render instruction*: a flat list of visual clips
plus captions plus one narration track, produced by composition and consumed by
the renderer. It is correct and it is not editable. There is nowhere to put
music, nowhere to put a second overlay, no way to say "this clip is locked", and
no identity that survives a re-plan.

This module adds the layer above it: an `EditTimeline` of typed tracks holding
addressable clips. Composition produces one; the user edits it; the renderer
consumes a `Timeline` flattened from it. Keeping them separate means the
renderer never has to understand editing, and the editor never has to understand
codecs.

## Why tracks are typed

A `MusicTrack` and a `VisualTrack` differ in more than a label:

* visual clips must not overlap — two pictures at once is a composite, which is
  a different thing with different semantics;
* music clips *must* be allowed to overlap, because that is what a cross-fade
  between two pieces is;
* caption clips are derived, so hand-editing them is a mistake to catch rather
  than a feature to support.

Encoding that in the track type means the validator knows the rule without the
caller passing a flag, and a new track type has to state its own answer rather
than inheriting a default that happens to be wrong for it.

## The invariants

Every operation goes through `apply()`, which validates before it commits.
Nothing here mutates in place on the way to failing — an operation either
produces a valid timeline or raises, and the caller still holds the old one.
That matters because a half-applied trim is not something a user can undo.

* no negative position, no negative or zero duration
* no overlap on a track that forbids it
* no clip referencing a visual unit that does not exist
* no operation on a locked clip without an explicit unlock
* durations and positions rounded to the millisecond, so float drift cannot
  accumulate into a one-frame gap after fifty edits
"""

from __future__ import annotations

from enum import Enum
from itertools import pairwise
from typing import Any

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Seconds,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.scale import MAX_TIMELINE_CLIPS
from vtv.contracts.timeline import TransitionKind

#: Positions and durations are rounded to this many decimal places. One
#: millisecond: finer than a frame at any frame rate this system renders, and
#: coarse enough that repeated arithmetic cannot drift.
PRECISION = 3

#: Below this, a clip is not a clip. A 10ms visual is a flash, and almost always
#: the result of a trim that went wrong rather than something anyone wanted.
MIN_CLIP_SECONDS = 0.04


def quantise(seconds: float) -> Seconds:
    """Round to the timeline's precision. Applied at every boundary."""
    return round(float(seconds), PRECISION)


class TrackKind(str, Enum):
    """What a track carries, and therefore what rules it obeys."""

    NARRATION = "narration"
    VISUAL = "visual"
    CAPTION = "caption"
    MUSIC = "music"
    SFX = "sfx"
    OVERLAY = "overlay"
    #: Reserved, so that adding them later is a value rather than a migration.
    BROLL = "broll"
    GRAPHICS = "graphics"
    SECONDARY_VOICE = "secondary_voice"
    CHAPTER = "chapter"


#: Tracks on which two clips may not occupy the same instant. Visual is the
#: obvious one — two pictures at once is a composite, not a timeline. Narration
#: too: two voices talking over each other is a mixing decision, and if someone
#: wants it they want `SECONDARY_VOICE`, which permits it.
EXCLUSIVE_TRACKS: frozenset[TrackKind] = frozenset(
    {TrackKind.NARRATION, TrackKind.VISUAL, TrackKind.CAPTION, TrackKind.CHAPTER}
)

#: Tracks the system owns. A user editing one of these by hand is desynchronising
#: it from what produced it, so the operations refuse rather than allow a change
#: the next re-plan will silently discard.
DERIVED_TRACKS: frozenset[TrackKind] = frozenset(
    {TrackKind.CAPTION, TrackKind.NARRATION}
)


class ClipSourceKind(str, Enum):
    """Where a clip's pixels or samples come from."""

    #: Stored bytes: a fetched asset, a generated image, rendered narration.
    OBJECT = "object"
    #: An animation spec the renderer draws. No bytes until render time.
    PROGRAMMATIC = "programmatic"
    #: Nothing. A deliberate gap — a hold on black, a beat of silence.
    EMPTY = "empty"
    #: Text drawn by the renderer: a caption, a chapter card.
    TEXT = "text"


class TimelineClip(VTVModel):
    """One addressable thing at one place on one track."""

    clip_id: Id = Field(default_factory=lambda: new_id(IdPrefix.CLIP))
    track_id: Id

    #: The visual this clip shows, when it shows one. This is the link that
    #: makes "click the clip, highlight the script line" possible, and it must
    #: survive regeneration — a new version of a unit replaces the clip's
    #: *source*, never its identity.
    visual_unit_id: Id | None = None

    start: Seconds = Field(ge=0.0)
    #: Exclusive, like every other span in this system.
    end: Seconds = Field(gt=0.0)

    source_kind: ClipSourceKind = ClipSourceKind.EMPTY
    object: ObjectRef | None = None
    asset_id: Id | None = None
    generation_id: Id | None = None
    #: The credit line the licence obliges us to draw over this clip. Travels
    #: with the object, because it is a property of *this picture* and not of
    #: the unit that chose it — swap the version and the credit must change with
    #: it or the video credits the wrong photographer.
    attribution: str | None = Field(default=None, max_length=300)
    #: Serialised animation spec, for `PROGRAMMATIC`. `Any`, not `object`:
    #: the field named `object` above shadows the builtin in this class body.
    spec: dict[str, Any] | None = None
    #: For `TEXT`.
    text: str | None = Field(default=None, max_length=2000)

    transition_in: TransitionKind = TransitionKind.CUT
    transition_out: TransitionKind = TransitionKind.CUT
    transition_in_seconds: Seconds = Field(default=0.0, ge=0.0, le=8.0)
    transition_out_seconds: Seconds = Field(default=0.0, ge=0.0, le=8.0)

    #: Stacking order where a track permits overlap. Higher draws later.
    z_index: int = Field(default=0, ge=-100, le=100)
    #: Gain for audio tracks, 0.0 to 1.0. Ignored on visual tracks.
    gain: float = Field(default=1.0, ge=0.0, le=1.0)

    #: The user pinned this. Operations refuse rather than move it.
    locked: bool = False
    label: str = Field(default="", max_length=120)

    @property
    def duration(self) -> Seconds:
        return quantise(self.end - self.start)

    def overlaps(self, other: TimelineClip) -> bool:
        """Half-open intervals, so touching is not overlapping.

        A clip ending at 4.0 and one starting at 4.0 are adjacent. Getting this
        wrong makes every contiguous timeline look like a conflict.
        """
        return self.start < other.end and other.start < self.end

    @model_validator(mode="after")
    def _span_and_source_agree(self) -> TimelineClip:
        if self.end <= self.start:
            raise ValueError("a clip must advance: end is not after start")
        if quantise(self.end - self.start) < MIN_CLIP_SECONDS:
            raise ValueError(
                f"a clip must last at least {MIN_CLIP_SECONDS}s"
            )
        if self.source_kind is ClipSourceKind.OBJECT and self.object is None:
            raise ValueError("an object clip must carry an object reference")
        if self.source_kind is ClipSourceKind.PROGRAMMATIC and self.spec is None:
            raise ValueError("a programmatic clip must carry a spec")
        if self.source_kind is ClipSourceKind.TEXT and not (self.text or "").strip():
            raise ValueError("a text clip must carry text")
        return self


class Track(VTVModel):
    """A lane. Owns its clips and the rule about whether they may overlap."""

    track_id: Id = Field(default_factory=lambda: new_id(IdPrefix.TRACK))
    kind: TrackKind
    name: str = Field(default="", max_length=64)
    clips: list[TimelineClip] = Field(
        default_factory=list, max_length=MAX_TIMELINE_CLIPS
    )

    muted: bool = False
    #: The whole lane is pinned. Stronger than per-clip locking and easier to
    #: reason about for "don't touch my music".
    locked: bool = False

    @property
    def is_exclusive(self) -> bool:
        return self.kind in EXCLUSIVE_TRACKS

    @property
    def is_derived(self) -> bool:
        return self.kind in DERIVED_TRACKS

    @property
    def duration(self) -> Seconds:
        return quantise(max((clip.end for clip in self.clips), default=0.0))

    def clip(self, clip_id: str) -> TimelineClip | None:
        for item in self.clips:
            if item.clip_id == clip_id:
                return item
        return None

    def gaps(self) -> list[tuple[Seconds, Seconds]]:
        """Uncovered stretches. Only meaningful on an exclusive track.

        Reported rather than forbidden: a gap on the visual track is black
        screen, which is sometimes exactly what a user wants and sometimes a
        bug. The editor shows them; it does not refuse them.
        """
        gaps: list[tuple[Seconds, Seconds]] = []
        cursor = 0.0
        for clip in sorted(self.clips, key=lambda item: item.start):
            if clip.start > cursor + MIN_CLIP_SECONDS:
                gaps.append((quantise(cursor), quantise(clip.start)))
            cursor = max(cursor, clip.end)
        return gaps

    @model_validator(mode="after")
    def _clips_are_valid_for_this_kind(self) -> Track:
        identifiers = [clip.clip_id for clip in self.clips]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("clip ids must be unique within a track")
        for clip in self.clips:
            if clip.track_id != self.track_id:
                raise ValueError("a clip must name the track that holds it")
        if self.is_exclusive:
            ordered = sorted(self.clips, key=lambda item: item.start)
            for earlier, later in pairwise(ordered):
                if earlier.overlaps(later):
                    raise ValueError(
                        f"{self.kind.value} clips must not overlap: "
                        f"{earlier.clip_id} and {later.clip_id}"
                    )
        return self


class EditTimeline(RootDocument, Timestamped):
    """The multi-track timeline the user edits.

    Versioned, because "what produced the video I published last week" has to
    be answerable, and because an editor without undo is not an editor.
    """

    document_name = "edit_timeline"

    edit_timeline_id: Id = Field(default_factory=lambda: new_id(IdPrefix.TIMELINE))
    organisation_id: Id
    project_id: Id

    tracks: list[Track] = Field(default_factory=list, max_length=32)
    version: int = Field(default=1, ge=1)

    #: What the video is meant to be, carried here so the editor can show the
    #: gap between intent and reality without recomputing a pacing plan.
    target_seconds: Seconds | None = Field(default=None, ge=0.0)

    # -- queries ----------------------------------------------------------

    def track(self, track_id: str) -> Track | None:
        for item in self.tracks:
            if item.track_id == track_id:
                return item
        return None

    def track_of_kind(self, kind: TrackKind) -> Track | None:
        for item in self.tracks:
            if item.kind is kind:
                return item
        return None

    def clip(self, clip_id: str) -> tuple[Track, TimelineClip] | None:
        for track in self.tracks:
            found = track.clip(clip_id)
            if found is not None:
                return track, found
        return None

    def clips_for_unit(self, visual_unit_id: str) -> list[TimelineClip]:
        return [
            clip
            for track in self.tracks
            for clip in track.clips
            if clip.visual_unit_id == visual_unit_id
        ]

    def clip_at(self, seconds: Seconds, *, kind: TrackKind = TrackKind.VISUAL):  # type: ignore[no-untyped-def]
        """What is on screen at this instant. The seek query."""
        track = self.track_of_kind(kind)
        if track is None:
            return None
        for clip in track.clips:
            if clip.start <= seconds < clip.end:
                return clip
        return None

    @property
    def duration(self) -> Seconds:
        return quantise(max((track.duration for track in self.tracks), default=0.0))

    @property
    def locked_clip_ids(self) -> set[str]:
        return {
            clip.clip_id
            for track in self.tracks
            for clip in track.clips
            if clip.locked or track.locked
        }

    @model_validator(mode="after")
    def _tracks_are_unique(self) -> EditTimeline:
        identifiers = [track.track_id for track in self.tracks]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("track ids must be unique")
        exclusive_kinds = [
            track.kind for track in self.tracks if track.kind in EXCLUSIVE_TRACKS
        ]
        if len(set(exclusive_kinds)) != len(exclusive_kinds):
            raise ValueError(
                "there can be only one narration, visual, caption or chapter track"
            )
        clip_ids = [
            clip.clip_id for track in self.tracks for clip in track.clips
        ]
        if len(set(clip_ids)) != len(clip_ids):
            raise ValueError("clip ids must be unique across the whole timeline")
        return self


__all__ = [
    "DERIVED_TRACKS",
    "EXCLUSIVE_TRACKS",
    "MIN_CLIP_SECONDS",
    "PRECISION",
    "ClipSourceKind",
    "EditTimeline",
    "TimelineClip",
    "Track",
    "TrackKind",
    "quantise",
]
