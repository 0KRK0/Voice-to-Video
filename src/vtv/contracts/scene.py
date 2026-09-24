"""Scenes: the narrative structure of the video.

The single most important rule in this file is that **one sentence is not one
scene** (Rule 7). A naive system splits on punctuation and produces a slideshow
of unrelated pictures. This system groups semantic units into scenes by
*continuity of idea*, and records what carries over from one scene to the next,
so the finished video reads as one argument rather than a list of statements.

The example from the brief:

    "The transistor was invented in 1947 at Bell Labs. It was smaller, more
    efficient, and eventually replaced vacuum tubes."

is two sentences, five or six semantic units, and — correctly handled — one or
two scenes built around a single visual through-line, not five unrelated shots.

The ``SceneGraph`` is the root document. It is deliberately a *graph* rather than
a list: scenes carry forward motifs and entities, and that carried-forward state
is data the Visual Director reads when deciding what a shot should look like.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    TIME_EPSILON,
    Confidence,
    Id,
    IdPrefix,
    Importance,
    RootDocument,
    TimeSpan,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import Status
from vtv.contracts.style import StyleProfile

#: A scene shorter than this cannot land with a viewer, no matter how brief the
#: sentence was. The Scene Engine merges anything below it into a neighbour.
MIN_SCENE_SECONDS: float = 1.5

#: Beyond this, a single static idea starts to feel abandoned on screen. Longer
#: spans are split into shots rather than left as one held frame.
MAX_SCENE_SECONDS: float = 25.0


class ScenePurpose(str, Enum):
    """The role a scene plays in the narrative arc.

    Purpose is what allows pacing to be reasoned about: an opening and a
    conclusion deserve production value, a supporting detail does not.
    """

    OPENING = "opening"
    CONTEXT = "context"
    DEFINITION = "definition"
    EXPLANATION = "explanation"
    EVIDENCE = "evidence"
    EXAMPLE = "example"
    CONTRAST = "contrast"
    TURNING_POINT = "turning_point"
    IMPLICATION = "implication"
    CONCLUSION = "conclusion"


class VisualGoal(str, Enum):
    """What the visual has to *accomplish* for this scene.

    This is the Scene Engine's brief to the Visual Director. It is intentionally
    stated in terms of communication rather than medium: the Scene Engine says
    "show change over time"; choosing a chart, a timeline or a dissolve between
    two photographs is the Director's decision.
    """

    ESTABLISH_SUBJECT = "establish_subject"
    SHOW_ENTITY = "show_entity"
    SHOW_PLACE = "show_place"
    SHOW_QUANTITY = "show_quantity"
    SHOW_CHANGE_OVER_TIME = "show_change_over_time"
    SHOW_STRUCTURE = "show_structure"
    SHOW_PROCESS = "show_process"
    SHOW_CONTRAST = "show_contrast"
    SHOW_CAUSE_EFFECT = "show_cause_effect"
    ILLUSTRATE_ABSTRACT = "illustrate_abstract"
    EMPHASISE_STATEMENT = "emphasise_statement"
    SET_MOOD = "set_mood"


class Continuity(VTVModel):
    """What this scene inherits from the one before it.

    Carrying entities and motifs forward explicitly is how narrative coherence
    becomes a property the system can be *tested* on rather than a quality we
    hope emerges from independent per-scene generation.
    """

    #: Entities that were on screen in the previous scene and remain relevant.
    carried_entity_ids: list[Id] = Field(default_factory=list, max_length=16)
    #: Recurring visual devices, e.g. "vertical timeline on the left".
    motifs: list[str] = Field(default_factory=list, max_length=8)
    #: If true, the visual should evolve from the previous shot rather than cut
    #: to something unrelated — the difference between a film and a slideshow.
    continues_previous_visual: bool = False


class Shot(VTVModel):
    """A subdivision of a scene, used when one idea needs more than one image.

    Shots exist so that a long scene can breathe without being torn into
    unrelated scenes. They inherit the scene's visual goal.
    """

    shot_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SHOT))
    span: TimeSpan
    #: What changes on screen at this point, in one line.
    beat: str = Field(min_length=1, max_length=200)


class Scene(VTVModel):
    """One coherent visual idea, anchored to a span of the narration."""

    scene_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SCENE))
    index: int = Field(ge=0, description="Position in the video, zero-based.")
    span: TimeSpan

    #: The narration this scene covers, verbatim. This is what the viewer hears
    #: while the scene is on screen, and the ground truth for caption timing.
    narration: str = Field(min_length=1, max_length=8000)
    #: Semantic units that were merged to form this scene. The audit trail from
    #: pixels back to spoken words runs through here.
    semantic_unit_ids: list[Id] = Field(default_factory=list, max_length=64)
    entity_ids: list[Id] = Field(default_factory=list, max_length=32)

    purpose: ScenePurpose
    visual_goal: VisualGoal
    #: One line stating what the viewer should understand from this shot. The
    #: Visual Director optimises against this sentence.
    visual_brief: str = Field(min_length=1, max_length=400)

    importance: Importance = 0.5
    continuity: Continuity = Field(default_factory=Continuity)
    shots: list[Shot] = Field(default_factory=list, max_length=12)

    status: Status = Status.PENDING
    confidence: Confidence | None = None

    @model_validator(mode="after")
    def _validate_span_and_shots(self) -> Scene:
        if self.span.duration < MIN_SCENE_SECONDS - TIME_EPSILON:
            raise ValueError(
                f"scene {self.index} is {self.span.duration:.2f}s, below the "
                f"{MIN_SCENE_SECONDS}s minimum; merge it into a neighbour"
            )
        for shot in self.shots:
            if not self.span.contains(shot.span):
                raise ValueError(
                    f"shot {shot.shot_id} lies outside scene {self.scene_id}"
                )
        previous: Shot | None = None
        for shot in self.shots:
            if previous is not None and shot.span.start < previous.span.end - TIME_EPSILON:
                raise ValueError("shots within a scene must not overlap")
            previous = shot
        return self

    @property
    def duration(self) -> float:
        return self.span.duration


class NarrativeArc(VTVModel):
    """The story the whole video tells, stated once.

    Every scene is checked against this. A scene that does not serve the thesis
    is a signal that the Scene Engine mis-segmented, and that is measurable.
    """

    thesis: str = Field(default="", max_length=400)
    #: Ordered beats of the argument, independent of timing.
    beats: list[str] = Field(default_factory=list, max_length=24)
    #: Visual devices intended to recur across the whole video.
    motifs: list[str] = Field(default_factory=list, max_length=8)


class SceneGraph(RootDocument, Timestamped):
    """The complete scene structure for one project."""

    document_name = "scene_graph"

    scene_graph_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SCENE_GRAPH))
    #: The tenant this belongs to. Carried on every project-scoped document so
    #: that a service deep in the pipeline can name the storage namespace it is
    #: allowed to write to without an ambient lookup — `LocalStorageProvider`
    #: refuses any key outside `orgs/<organisation_id>/`.
    organisation_id: Id
    project_id: Id
    understanding_id: Id
    transcript_id: Id

    style: StyleProfile = Field(default_factory=StyleProfile)
    narrative: NarrativeArc = Field(default_factory=NarrativeArc)
    scenes: list[Scene] = Field(default_factory=list, max_length=400)

    status: Status = Status.PENDING

    @model_validator(mode="after")
    def _scenes_are_contiguous_and_ordered(self) -> SceneGraph:
        seen: set[str] = set()
        previous: Scene | None = None
        for position, scene in enumerate(self.scenes):
            if scene.scene_id in seen:
                raise ValueError(f"duplicate scene_id {scene.scene_id}")
            seen.add(scene.scene_id)
            if scene.index != position:
                raise ValueError(
                    f"scene index {scene.index} does not match its position {position}"
                )
            if (
                previous is not None
                and scene.span.start < previous.span.end - TIME_EPSILON
            ):
                raise ValueError(
                    f"scenes {previous.index} and {scene.index} overlap in time"
                )
            previous = scene
        return self

    @property
    def span(self) -> TimeSpan | None:
        if not self.scenes:
            return None
        return TimeSpan(
            start=self.scenes[0].span.start, end=self.scenes[-1].span.end
        )

    def scene_by_id(self, scene_id: str) -> Scene | None:
        return next((s for s in self.scenes if s.scene_id == scene_id), None)

    def gaps(self) -> list[TimeSpan]:
        """Stretches of narration no scene covers.

        A gap means the viewer hears the speaker with nothing on screen, so this
        is checked before a scene graph is accepted rather than discovered in
        the finished MP4.
        """
        found: list[TimeSpan] = []
        for previous, following in zip(self.scenes, self.scenes[1:], strict=False):
            if following.span.start - previous.span.end > TIME_EPSILON:
                found.append(
                    TimeSpan(start=previous.span.end, end=following.span.start)
                )
        return found


__all__ = [
    "MAX_SCENE_SECONDS",
    "MIN_SCENE_SECONDS",
    "Continuity",
    "NarrativeArc",
    "Scene",
    "SceneGraph",
    "ScenePurpose",
    "Shot",
    "VisualGoal",
]
