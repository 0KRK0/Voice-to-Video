"""Stage 4 — The scene engine.

Semantic units become scenes. This is where Rule 7 is either honoured or lost:
**one sentence is not one scene.**

The algorithm is deterministic, which matters more here than anywhere else in
the pipeline. Segmentation is the structural decision every later stage inherits;
if it were non-deterministic, no test downstream could be stable and no
evaluation of the Visual Director would mean anything, because it would be
grading a different film each run.

The grouping decision is a single question asked of each pair of adjacent units:

    would showing these separately harm the point being made?

If yes, they belong in one scene. The signals that answer it — shared subjects,
elaboration, continued comparison, brevity — are scored in `continuity_score`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vtv.contracts.base import TIME_EPSILON, TimeSpan
from vtv.contracts.errors import Status
from vtv.contracts.scene import (
    MAX_SCENE_SECONDS,
    MIN_SCENE_SECONDS,
    Continuity,
    NarrativeArc,
    Scene,
    SceneGraph,
    ScenePurpose,
    Shot,
    VisualGoal,
)
from vtv.contracts.semantics import (
    EntityType,
    SemanticIntent,
    SemanticUnit,
    Understanding,
)
from vtv.contracts.style import StyleProfile
from vtv.contracts.transcript import Transcript
from vtv.observability.events import EventName, EventSink

SCENE_ENGINE_VERSION = "grouping-1.0"

#: Above this, two adjacent units are one idea.
MERGE_THRESHOLD = 0.5

#: Longer than this and a scene is split into shots rather than left as one
#: held frame.
SHOT_SECONDS = 9.0

#: Intents that continue the previous idea rather than starting a new one.
_ELABORATING = {
    SemanticIntent.EXAMPLE,
    SemanticIntent.NUMERIC_FACT,
    SemanticIntent.CAUSATION,
    SemanticIntent.ENUMERATION,
}

#: Intents that always begin something new, regardless of shared subject.
_OPENING_INTENTS = {
    SemanticIntent.QUESTION,
    SemanticIntent.CONCLUSION,
}

_PURPOSE_BY_INTENT: dict[SemanticIntent, ScenePurpose] = {
    SemanticIntent.DEFINITION: ScenePurpose.DEFINITION,
    SemanticIntent.CLAIM: ScenePurpose.EXPLANATION,
    SemanticIntent.EXAMPLE: ScenePurpose.EXAMPLE,
    SemanticIntent.COMPARISON: ScenePurpose.CONTRAST,
    SemanticIntent.PROCESS: ScenePurpose.EXPLANATION,
    SemanticIntent.CAUSATION: ScenePurpose.IMPLICATION,
    SemanticIntent.ENUMERATION: ScenePurpose.EVIDENCE,
    SemanticIntent.NUMERIC_FACT: ScenePurpose.EVIDENCE,
    SemanticIntent.EVENT_NARRATION: ScenePurpose.CONTEXT,
    SemanticIntent.QUESTION: ScenePurpose.OPENING,
    SemanticIntent.CONCLUSION: ScenePurpose.CONCLUSION,
    SemanticIntent.TRANSITION: ScenePurpose.CONTEXT,
    SemanticIntent.ASIDE: ScenePurpose.CONTEXT,
    SemanticIntent.FILLER: ScenePurpose.CONTEXT,
}


@dataclass
class _Group:
    """A run of units that will become one scene."""

    units: list[SemanticUnit] = field(default_factory=list)

    @property
    def span(self) -> TimeSpan:
        return TimeSpan.of(self.units[0].span.start, self.units[-1].span.end)

    @property
    def entity_ids(self) -> list[str]:
        seen: list[str] = []
        for unit in self.units:
            for entity_id in unit.entity_ids:
                if entity_id not in seen:
                    seen.append(entity_id)
        return seen

    @property
    def salience(self) -> float:
        return max(unit.salience for unit in self.units)


def continuity_score(previous: SemanticUnit, current: SemanticUnit) -> float:
    """How strongly these two units belong to the same visual idea, in [0, 1].

    Each term answers a different version of "is this still the same point?" —
    the same subject, an elaboration of it, the same rhetorical move, or a
    fragment too small to stand alone.
    """
    score = 0.0

    shared = set(previous.entity_ids) & set(current.entity_ids)
    if shared:
        # The strongest signal by far. Same subject, same shot.
        score += 0.45 + min(0.15, 0.05 * len(shared))
    elif previous.entity_ids and current.entity_ids:
        # Both name subjects and they are different: a genuine change of topic.
        score -= 0.25
    elif not current.entity_ids:
        # A pronoun-only continuation — "it was smaller" — refers back.
        score += 0.25

    if current.intent in _ELABORATING:
        score += 0.25
    if current.intent is previous.intent:
        score += 0.15
    if current.intent in _OPENING_INTENTS:
        score -= 0.4
    if current.intent in {SemanticIntent.FILLER, SemanticIntent.TRANSITION}:
        # Never give hesitation its own shot.
        score += 0.6

    if current.span.duration < MIN_SCENE_SECONDS:
        score += 0.35
    if previous.span.duration < MIN_SCENE_SECONDS:
        score += 0.2

    return max(0.0, min(1.0, score))


def group_units(units: list[SemanticUnit]) -> list[_Group]:
    """Group adjacent units into scene-sized ideas."""
    visualisable = [unit for unit in units if unit.intent is not SemanticIntent.FILLER]
    if not visualisable:
        visualisable = list(units)
    if not visualisable:
        return []

    groups: list[_Group] = [_Group([visualisable[0]])]
    for unit in visualisable[1:]:
        current = groups[-1]
        candidate_duration = unit.span.end - current.span.start
        score = continuity_score(current.units[-1], unit)

        if candidate_duration > MAX_SCENE_SECONDS and current.span.duration >= MIN_SCENE_SECONDS:
            # Even one continuous idea has to breathe. Beyond the maximum it
            # becomes a new scene; within a scene it becomes shots.
            groups.append(_Group([unit]))
        elif score >= MERGE_THRESHOLD:
            current.units.append(unit)
        else:
            groups.append(_Group([unit]))

    return _absorb_short_groups(groups)


def _absorb_short_groups(groups: list[_Group]) -> list[_Group]:
    """Merge anything too brief to land into the neighbour it fits best.

    A sub-second scene is the signature of punctuation-splitting. The `Scene`
    contract refuses to construct one, so they are removed here — by merging,
    never by dropping the narration.
    """
    if len(groups) <= 1:
        return groups

    merged: list[_Group] = []
    for group in groups:
        if (
            merged
            and group.span.duration < MIN_SCENE_SECONDS
            and merged[-1].span.duration + group.span.duration <= MAX_SCENE_SECONDS
        ):
            merged[-1].units.extend(group.units)
            continue
        merged.append(group)

    # A short *first* group has no predecessor to fold into; it takes the next.
    if len(merged) > 1 and merged[0].span.duration < MIN_SCENE_SECONDS:
        merged[1].units = merged[0].units + merged[1].units
        merged.pop(0)
    return merged


def choose_purpose(group: _Group, index: int, total: int) -> ScenePurpose:
    intents = [unit.intent for unit in group.units]
    if index == 0:
        return ScenePurpose.OPENING
    if index == total - 1:
        return (
            ScenePurpose.CONCLUSION
            if SemanticIntent.CONCLUSION in intents or total > 2
            else ScenePurpose.EXPLANATION
        )
    # The dominant intent decides, with the first as the tie-break.
    ranked = sorted(
        {intent: intents.count(intent) for intent in intents}.items(),
        key=lambda item: (-item[1], intents.index(item[0])),
    )
    return _PURPOSE_BY_INTENT.get(ranked[0][0], ScenePurpose.EXPLANATION)


def choose_visual_goal(group: _Group, understanding: Understanding) -> VisualGoal:
    """What the visual has to accomplish.

    Stated in terms of communication, never medium: this says "show change over
    time", and the Visual Director decides whether that is a chart, a timeline
    or a dissolve between two photographs.
    """
    intents = {unit.intent for unit in group.units}
    entities = [
        entity
        for entity in (understanding.entity_by_id(i) for i in group.entity_ids)
        if entity is not None
    ]
    types = {entity.type for entity in entities}
    quantities = [q for unit in group.units for q in unit.quantities]

    if SemanticIntent.COMPARISON in intents:
        return VisualGoal.SHOW_CONTRAST
    if len(quantities) >= 2:
        # Two numbers about the same thing is a change, not a statistic.
        return (
            VisualGoal.SHOW_CHANGE_OVER_TIME
            if EntityType.DATE in types
            else VisualGoal.SHOW_QUANTITY
        )
    if quantities:
        return VisualGoal.SHOW_QUANTITY
    if SemanticIntent.PROCESS in intents:
        return VisualGoal.SHOW_PROCESS
    if SemanticIntent.CAUSATION in intents:
        return VisualGoal.SHOW_CAUSE_EFFECT
    if EntityType.DATE in types and SemanticIntent.EVENT_NARRATION in intents:
        return VisualGoal.SHOW_CHANGE_OVER_TIME
    if EntityType.LOCATION in types:
        return VisualGoal.SHOW_PLACE
    if len(group.entity_ids) >= 3 or any(unit.relation_ids for unit in group.units):
        return VisualGoal.SHOW_STRUCTURE
    if types & {EntityType.PERSON, EntityType.ORGANIZATION, EntityType.PRODUCT}:
        return VisualGoal.SHOW_ENTITY
    if SemanticIntent.DEFINITION in intents:
        return VisualGoal.ESTABLISH_SUBJECT
    if SemanticIntent.CONCLUSION in intents:
        return VisualGoal.ILLUSTRATE_ABSTRACT
    return VisualGoal.EMPHASISE_STATEMENT


def _visual_brief(group: _Group, goal: VisualGoal, understanding: Understanding) -> str:
    """A one-line statement of what the viewer should take from this shot."""
    names = [
        entity.search_name
        for entity in (understanding.entity_by_id(i) for i in group.entity_ids[:3])
        if entity is not None
    ]
    subject = ", ".join(names) if names else group.units[0].proposition[:60]
    phrasing = {
        VisualGoal.SHOW_CONTRAST: f"Hold {subject} against what it is being compared with.",
        VisualGoal.SHOW_QUANTITY: f"Show the numbers stated about {subject}.",
        VisualGoal.SHOW_CHANGE_OVER_TIME: f"Show how {subject} changed across the stated period.",
        VisualGoal.SHOW_PROCESS: f"Show the steps of {subject} in order.",
        VisualGoal.SHOW_CAUSE_EFFECT: f"Show what {subject} led to.",
        VisualGoal.SHOW_PLACE: f"Show where {subject} is.",
        VisualGoal.SHOW_STRUCTURE: f"Show how {subject} relates to the rest.",
        VisualGoal.SHOW_ENTITY: f"Show {subject} as a real thing.",
        VisualGoal.ESTABLISH_SUBJECT: f"Establish what {subject} is.",
        VisualGoal.ILLUSTRATE_ABSTRACT: f"Give {subject} a concrete image.",
        VisualGoal.EMPHASISE_STATEMENT: f"Put the statement itself on screen: {subject}.",
        VisualGoal.SET_MOOD: f"Set the tone for {subject}.",
    }
    return phrasing.get(goal, f"Show {subject}.")[:400]


def _shots(group: _Group, span: TimeSpan) -> list[Shot]:
    """Subdivide a long scene at its own unit boundaries.

    Shots let one idea develop across several images without being torn into
    unrelated scenes. Boundaries come from the units, never from a clock, so a
    shot change always lands on something the speaker actually said.
    """
    if span.duration <= SHOT_SECONDS or len(group.units) < 2:
        return []
    shots: list[Shot] = []
    cursor = span.start
    for unit in group.units:
        end = min(span.end, max(unit.span.end, cursor + 0.3))
        if end - cursor < 0.3:
            continue
        shots.append(
            Shot(span=TimeSpan.of(cursor, end), beat=unit.proposition[:200])
        )
        cursor = end
    if shots:
        last = shots[-1]
        if abs(last.span.end - span.end) > TIME_EPSILON:
            shots[-1] = last.model_copy(
                update={"span": TimeSpan.of(last.span.start, span.end)}
            )
    return shots[:12]


@dataclass
class SceneEngine:
    """Groups semantic units into a coherent, contiguous scene graph."""

    events: EventSink
    version: str = SCENE_ENGINE_VERSION

    def build(
        self,
        *,
        transcript: Transcript,
        understanding: Understanding,
        style: StyleProfile | None = None,
        total_duration: float | None = None,
    ) -> SceneGraph:
        groups = group_units(understanding.units)
        duration = total_duration or (
            transcript.span.end if transcript.span else 0.0
        )
        spans = self._contiguous_spans(groups, duration)

        scenes: list[Scene] = []
        previous_entities: list[str] = []
        for index, (group, span) in enumerate(zip(groups, spans, strict=True)):
            goal = choose_visual_goal(group, understanding)
            carried = [e for e in group.entity_ids if e in previous_entities]
            scenes.append(
                Scene(
                    index=index,
                    span=span,
                    narration=" ".join(unit.text for unit in group.units).strip()
                    or "…",
                    semantic_unit_ids=[unit.unit_id for unit in group.units],
                    entity_ids=group.entity_ids[:32],
                    purpose=choose_purpose(group, index, len(groups)),
                    visual_goal=goal,
                    visual_brief=_visual_brief(group, goal, understanding),
                    importance=round(min(1.0, group.salience + (0.15 if index in {0, len(groups) - 1} else 0.0)), 3),
                    continuity=Continuity(
                        carried_entity_ids=carried[:16],
                        motifs=[],
                        continues_previous_visual=bool(carried),
                    ),
                    shots=_shots(group, span),
                    status=Status.READY,
                )
            )
            previous_entities = group.entity_ids

        graph = SceneGraph(
            organisation_id=understanding.organisation_id,
            project_id=understanding.project_id,
            understanding_id=understanding.understanding_id,
            transcript_id=transcript.transcript_id,
            style=style or StyleProfile(),
            narrative=self._arc(understanding, scenes),
            scenes=scenes,
            status=Status.READY,
        )
        self.events.emit(
            EventName.SCENES_CREATED,
            project_id=understanding.project_id,
            data={
                "engine": self.version,
                "units": len(understanding.units),
                "scenes": len(scenes),
                "gaps": len(graph.gaps()),
                "shots": sum(len(scene.shots) for scene in scenes),
            },
        )
        return graph

    @staticmethod
    def _contiguous_spans(groups: list[_Group], duration: float) -> list[TimeSpan]:
        """Stretch scene spans so no narration is left uncovered.

        A gap means the viewer hears the speaker over nothing. Rather than
        reporting one and hoping somebody notices, each scene is extended to
        meet the next; the first starts at zero and the last runs to the end.
        """
        spans: list[TimeSpan] = []
        for index, group in enumerate(groups):
            start = 0.0 if index == 0 else spans[index - 1].end
            end = (
                duration
                if index == len(groups) - 1
                else max(group.span.end, start + MIN_SCENE_SECONDS)
            )
            end = min(end, duration) if duration > 0 else end
            if end - start < MIN_SCENE_SECONDS:
                end = start + MIN_SCENE_SECONDS
            spans.append(TimeSpan.of(round(start, 3), round(end, 3)))
        return spans

    @staticmethod
    def _arc(understanding: Understanding, scenes: list[Scene]) -> NarrativeArc:
        return NarrativeArc(
            thesis=(understanding.summary or understanding.topic)[:400],
            beats=[scene.visual_brief[:120] for scene in scenes][:24],
            motifs=[],
        )


__all__ = [
    "MERGE_THRESHOLD",
    "SCENE_ENGINE_VERSION",
    "SHOT_SECONDS",
    "SceneEngine",
    "choose_purpose",
    "choose_visual_goal",
    "continuity_score",
    "group_units",
]
