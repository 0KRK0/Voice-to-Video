"""The timeline: everything the renderer needs, and nothing it has to think about.

The timeline is the boundary between intelligence and rendering (Rule 13). By the
time a :class:`Timeline` exists, every decision has been made: which asset, how
long, what motion, which caption, what to do if a clip is shorter than its slot.
The renderer's job is reduced to executing it exactly, which is precisely what we
want from the part of the system that is slowest to iterate on.

**The narration is the clock.** The user's voice is laid down first and never
stretched, cut or time-shifted to accommodate a visual. Visuals are fitted to the
voice, never the reverse. That single decision removes an entire class of bugs in
which a regenerated shot silently pushes every subsequent caption out of sync.

**Fitting is explicit.** A generated clip will not come back at exactly the
requested length; a licensed photograph has no length at all. Rather than hoping,
every clip declares a :class:`FitPolicy` for what to do when its media is shorter
or longer than its slot. Stage 10 of the brief calls this out as a hard
requirement, and it is enforced here in the type system.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vtv.contracts.base import (
    TIME_EPSILON,
    Duration,
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    TimeSpan,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import DegradationStep, ErrorInfo, Status
from vtv.contracts.style import AspectRatio, StyleProfile
from vtv.contracts.visual_language import AnimationSpec, CameraMotion


class FitPolicy(str, Enum):
    """How a clip reconciles its media duration with its slot on the timeline."""

    #: Media is longer: play the first part and stop. The default for footage.
    TRIM = "trim"
    #: Media is shorter: repeat it. Only sensible for seamless motion.
    LOOP = "loop"
    #: Media is shorter: freeze the final frame. Safe, and usually invisible.
    HOLD_LAST = "hold_last"
    #: Retime the media to fit exactly. Cheap for small deltas, ugly for large.
    SPEED_RAMP = "speed_ramp"
    #: Stills have no duration; hold the image and let camera motion carry it.
    STILL = "still"


class TransitionKind(str, Enum):
    CUT = "cut"
    FADE = "fade"
    DISSOLVE = "dissolve"
    WIPE = "wipe"
    PUSH = "push"


class Transition(VTVModel):
    kind: TransitionKind = TransitionKind.CUT
    duration_seconds: float = Field(default=0.0, ge=0.0, le=3.0)

    @model_validator(mode="after")
    def _cut_is_instant(self) -> Transition:
        if self.kind is TransitionKind.CUT and self.duration_seconds != 0.0:
            raise ValueError("a cut has no duration")
        if self.kind is not TransitionKind.CUT and self.duration_seconds <= 0.0:
            raise ValueError(f"a {self.kind.value} needs a positive duration")
        return self


class AssetClipSource(VTVModel):
    """Play a resolved asset: a licensed photograph, generated frame or footage."""

    source_type: Literal["asset"] = "asset"
    asset_id: Id
    object: ObjectRef
    #: Credit that must be shown on screen, already formatted by the asset layer.
    attribution: str | None = Field(default=None, max_length=300)
    #: Set for generated imagery depicting real subjects, so the renderer can
    #: place the required "illustrative" label.
    illustrative_label: bool = False


class ProgrammaticClipSource(VTVModel):
    """Draw the animation at render time from its specification.

    Nothing is pre-baked into a file: the renderer holds the spec and draws it,
    which keeps these shots resolution-independent, instantly re-editable and
    free to regenerate.
    """

    source_type: Literal["programmatic"] = "programmatic"
    spec: AnimationSpec


class PlaceholderClipSource(VTVModel):
    """The controlled failure state.

    When every rung of the fallback ladder has been exhausted, the timeline gets
    one of these rather than a hole. The video still renders, the narration is
    still heard, and the storyboard shows the user exactly which shot needs
    attention (Rule 8, Section 29).
    """

    source_type: Literal["placeholder"] = "placeholder"
    #: Shown on screen in the editor; suppressed to a neutral card on export.
    message: str = Field(default="Visual unavailable", max_length=200)
    error: ErrorInfo | None = None


ClipSource = Annotated[
    AssetClipSource | ProgrammaticClipSource | PlaceholderClipSource,
    Field(discriminator="source_type"),
]


class VisualClip(VTVModel):
    """One visual occupying one span of the timeline."""

    clip_id: Id = Field(default_factory=lambda: new_id(IdPrefix.CLIP))
    scene_id: Id
    span: TimeSpan
    source: ClipSource

    fit: FitPolicy = FitPolicy.STILL
    camera_motion: CameraMotion = CameraMotion.NONE
    transition_in: Transition = Field(default_factory=Transition)
    #: Intrinsic length of the underlying media, where it has one. Used together
    #: with ``fit`` to compute retiming; ``None`` for stills and animations.
    media_duration_seconds: Duration | None = None
    #: The ladder this clip descended, if it is not the Director's first choice.
    degradation: list[DegradationStep] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _fit_is_possible(self) -> VisualClip:
        if self.media_duration_seconds is None:
            if self.fit not in {FitPolicy.STILL, FitPolicy.HOLD_LAST}:
                raise ValueError(
                    f"fit policy {self.fit.value} requires a known media duration"
                )
            return self
        if self.fit is FitPolicy.STILL:
            raise ValueError("STILL fit is only valid for media without a duration")
        if (
            self.media_duration_seconds < self.span.duration - TIME_EPSILON
            and self.fit is FitPolicy.TRIM
        ):
            raise ValueError(
                f"clip {self.clip_id} is {self.media_duration_seconds:.2f}s but its "
                f"slot is {self.span.duration:.2f}s; TRIM cannot fill it — choose "
                "LOOP, HOLD_LAST or SPEED_RAMP"
            )
        return self

    @property
    def is_placeholder(self) -> bool:
        return self.source.source_type == "placeholder"


class CaptionCue(VTVModel):
    """One caption, timed against the narration it transcribes."""

    cue_id: Id = Field(default_factory=lambda: new_id(IdPrefix.CAPTION))
    span: TimeSpan
    text: str = Field(min_length=1, max_length=400)
    #: Per-word spans for karaoke-style highlighting. Optional, because not
    #: every provider supplies word timing.
    word_spans: list[TimeSpan] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _words_inside_cue(self) -> CaptionCue:
        for span in self.word_spans:
            if not self.span.contains(span):
                raise ValueError("caption word timing falls outside its cue")
        return self


class NarrationTrack(VTVModel):
    """The user's voice. The spine of the whole timeline."""

    audio: ObjectRef
    duration_seconds: Duration
    gain_db: float = Field(default=0.0, ge=-24.0, le=12.0)
    #: Silence trimmed from the head of the original recording. Recorded so that
    #: timings can always be mapped back to the raw file.
    offset_seconds: float = Field(default=0.0, ge=0.0)


class Timeline(RootDocument, Timestamped):
    """The complete, render-ready description of one video."""

    document_name = "timeline"

    timeline_id: Id = Field(default_factory=lambda: new_id(IdPrefix.TIMELINE))
    project_id: Id
    scene_graph_id: Id

    narration: NarrationTrack
    clips: list[VisualClip] = Field(default_factory=list, max_length=800)
    captions: list[CaptionCue] = Field(default_factory=list, max_length=2000)

    style: StyleProfile = Field(default_factory=StyleProfile)
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    frame_rate: int = Field(default=30, ge=24, le=60)
    status: Status = Status.PENDING

    @model_validator(mode="after")
    def _clips_are_ordered_and_bounded(self) -> Timeline:
        limit = self.narration.duration_seconds + TIME_EPSILON
        previous: VisualClip | None = None
        seen: set[str] = set()
        for clip in self.clips:
            if clip.clip_id in seen:
                raise ValueError(f"duplicate clip_id {clip.clip_id}")
            seen.add(clip.clip_id)
            if clip.span.end > limit:
                raise ValueError(
                    f"clip {clip.clip_id} ends at {clip.span.end:.2f}s, beyond the "
                    f"{self.narration.duration_seconds:.2f}s narration"
                )
            if previous is not None and clip.span.start < previous.span.end - TIME_EPSILON:
                raise ValueError(
                    f"clips {previous.clip_id} and {clip.clip_id} overlap"
                )
            previous = clip

        for cue in self.captions:
            if cue.span.end > limit:
                raise ValueError(
                    f"caption {cue.cue_id} extends past the end of the narration"
                )
        return self

    @property
    def duration_seconds(self) -> float:
        """The video is exactly as long as the voice. Nothing else decides this."""
        return self.narration.duration_seconds

    def coverage_gaps(self) -> list[TimeSpan]:
        """Stretches with narration but no visual.

        Checked before rendering. A gap is not automatically an error — a
        deliberate black frame is legitimate — but an unintended one produces a
        video where the speaker talks over nothing, so it must be a decision
        rather than an accident.
        """
        gaps: list[TimeSpan] = []
        cursor = 0.0
        for clip in self.clips:
            if clip.span.start - cursor > TIME_EPSILON:
                gaps.append(TimeSpan(start=cursor, end=clip.span.start))
            cursor = max(cursor, clip.span.end)
        if self.duration_seconds - cursor > TIME_EPSILON:
            gaps.append(TimeSpan(start=cursor, end=self.duration_seconds))
        return gaps

    @property
    def placeholder_count(self) -> int:
        return sum(1 for clip in self.clips if clip.is_placeholder)

    def attributions(self) -> list[str]:
        """Every credit that must appear, de-duplicated and in first-use order."""
        seen: list[str] = []
        for clip in self.clips:
            source = clip.source
            if (
                isinstance(source, AssetClipSource)
                and source.attribution
                and source.attribution not in seen
            ):
                seen.append(source.attribution)
        return seen

    def is_renderable(self) -> tuple[bool, list[str]]:
        """Pre-flight check run immediately before a render is queued.

        Returns the verdict and the human-readable reasons, so the caller can
        surface them in the storyboard rather than discovering the problem in a
        render worker twenty minutes later.
        """
        problems: list[str] = []
        if not self.clips:
            problems.append("timeline has no visual clips")
        gaps = self.coverage_gaps()
        if gaps:
            problems.append(
                f"{len(gaps)} uncovered stretch(es) of narration, "
                f"totalling {sum(g.duration for g in gaps):.1f}s"
            )
        if self.style.captions_enabled and not self.captions:
            problems.append("captions are enabled but no cues were produced")
        return (not problems, problems)


__all__ = [
    "AssetClipSource",
    "CaptionCue",
    "ClipSource",
    "FitPolicy",
    "NarrationTrack",
    "PlaceholderClipSource",
    "ProgrammaticClipSource",
    "Timeline",
    "Transition",
    "TransitionKind",
    "VisualClip",
]
