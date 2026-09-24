"""Drawing the idea instead of buying a picture of it.

## The gap this closes

The visual ladder's first rung is "draw it ourselves" — a chart, a timeline, a
diagram, a map, a two-sided comparison. It is free, instant, deterministic, and
it cannot invent a fact. `RuleBasedVisualDirector` knows how to choose among the
six primitives and how to build each spec, and `animation/` knows how to draw
them.

The Studio could reach none of it.

`VisualSourcingService` works from one stretch of narration and had no
`Understanding` to hand — no entities, no quantities, no visual goal — so the
only programmatic visual it could construct was a `TypographySpec` built from
the first sentence. Which is why **"Use animation" in the Regenerate menu
produced exactly what "Use typography" produced**, and why a line stating two
numbers became a title card rather than the chart the Director would have drawn
for it in the pipeline lane.

So this module is the missing adapter: narration in, a drawn spec out, by
running the *existing* understanding engine over one line and asking the
*existing* Director what it would do with the result.

## Why the heuristic engine and not the model

`HeuristicUnderstandingEngine` is rules over one sentence: no network, no cost,
no latency, no hallucination. Every question it answers here — are there two
numbers, is there a date range, are there two things being contrasted, is there
a place we can locate — is a question rules answer as well as a model does,
because they are questions about what the sentence *contains* rather than what
it means. The model's contribution to visual choice is already spent one layer
up, in `visual_intent`, where it decides what to *look for*.

That also means this rung stays available when nothing is configured, which is
the property that makes it the bottom of the ladder.

## What it will not do

It will not produce a spec whose claims are not in the sentence. It does not
need to try: the Director builds every spec from extracted quantities, dates and
entities, and `PlanGate` refuses anything that slips through. This module adds
no new way to state a fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from vtv.contracts.base import IdPrefix, TimeSpan, new_id
from vtv.contracts.errors import Status
from vtv.contracts.scene import Scene, ScenePurpose, VisualGoal
from vtv.contracts.semantics import EntityType, SemanticIntent, Understanding
from vtv.contracts.style import StyleProfile
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.contracts.visual_language import AnimationSpec
from vtv.contracts.visual_plan import ProgrammaticRequirements, VisualStrategy
from vtv.observability.events import EventSink
from vtv.pipeline.director import RuleBasedVisualDirector
from vtv.pipeline.understanding import HeuristicUnderstandingEngine


@dataclass
class DrawnVisual:
    """A spec the renderer can draw, and why this one."""

    spec: AnimationSpec
    rationale: str
    #: What the sentence was read as containing. For the inspector, and for
    #: anyone wondering why a line became a chart rather than a diagram.
    goal: VisualGoal

    @property
    def requirements(self) -> ProgrammaticRequirements:
        return ProgrammaticRequirements(spec=self.spec)

    @property
    def is_typography(self) -> bool:
        """Whether this is the terminal fallback rather than a real drawing.

        The caller needs to know: offering "Use animation" and delivering a
        title card is what made that menu item look broken, and a caller that
        cannot tell the difference will do it again.
        """
        return self.spec.primitive.value == "typography"


@dataclass
class Draughtsman:
    """Turns one line of narration into the best drawn visual it supports."""

    events: EventSink

    async def draw(
        self,
        narration: str,
        *,
        style: StyleProfile,
        organisation_id: str,
        project_id: str,
        duration: float = 4.0,
    ) -> DrawnVisual:
        """The Director's own choice, for one sentence.

        Always returns something: typography is the answer for a sentence with
        no numbers, no dates, no contrast, no relations and no place, and that
        is most sentences. `is_typography` is how the caller tells the two
        apart.
        """
        scene, understanding = await self._read(
            narration,
            organisation_id=organisation_id,
            project_id=project_id,
            duration=duration,
        )
        director = RuleBasedVisualDirector(events=self.events)
        plan = director.plan_scene(scene, understanding, style)

        # The Director may have chosen a rung this module cannot serve — a
        # photograph for a named subject, a generated image for an abstract
        # one. Those are other rungs of the ladder and other people's job; here
        # we take its drawn choice if it made one, and its typography if not.
        for directive in plan.ladder:
            if directive.strategy is not VisualStrategy.PROGRAMMATIC:
                continue
            requirements = directive.requirements
            if not isinstance(requirements, ProgrammaticRequirements):
                continue
            return DrawnVisual(
                spec=requirements.spec,
                rationale=directive.rationale,
                goal=scene.visual_goal,
            )

        # Unreachable in practice: every ladder terminates in typography. Kept
        # because "in practice" is where this codebase keeps finding its bugs.
        return DrawnVisual(
            spec=director._typography(scene, [], style),
            rationale="a statement with no separate referent, set as type",
            goal=scene.visual_goal,
        )

    # -- reading one line -------------------------------------------------

    async def _read(
        self,
        narration: str,
        *,
        organisation_id: str,
        project_id: str,
        duration: float,
    ) -> tuple[Scene, Understanding]:
        """One sentence, understood well enough for the Director to decide."""
        text = " ".join(narration.split())
        transcript = Transcript(
            recording_id=new_id(IdPrefix.RECORDING),
            organisation_id=organisation_id,
            project_id=project_id,
            language="en",
            segments=[
                TranscriptSegment(span=TimeSpan.of(0.0, max(0.5, duration)), text=text)
            ],
            provider="drawing",
            model="heuristic",
            status=Status.READY,
        )
        understanding = await HeuristicUnderstandingEngine().understand(transcript)

        scene = Scene(
            index=0,
            span=TimeSpan.of(0.0, max(0.5, duration)),
            narration=text[:2000],
            semantic_unit_ids=[unit.unit_id for unit in understanding.units],
            entity_ids=[entity.entity_id for entity in understanding.entities],
            purpose=ScenePurpose.EXPLANATION,
            visual_goal=_goal_for(understanding),
            visual_brief=text[:200],
            # High enough that the Director's `importance >= 0.6` gate for the
            # generative rung is not what decides this. We only ever take its
            # programmatic answer, and a low importance would push a
            # chartable sentence towards a cheaper rung for no reason.
            importance=0.7,
        )
        return scene, understanding


def _goal_for(understanding: Understanding) -> VisualGoal:
    """What this sentence is trying to show, from what the rules found."""
    quantities = [q for unit in understanding.units for q in unit.quantities]
    if len(quantities) >= 2:
        return VisualGoal.SHOW_QUANTITY

    dates = [
        entity
        for entity in understanding.entities
        if entity.type is EntityType.DATE
    ]
    if len(dates) >= 2:
        return VisualGoal.SHOW_CHANGE_OVER_TIME

    intents = {unit.intent for unit in understanding.units}
    if SemanticIntent.COMPARISON in intents:
        return VisualGoal.SHOW_CONTRAST
    if SemanticIntent.CAUSATION in intents:
        return VisualGoal.SHOW_CAUSE_EFFECT
    if SemanticIntent.PROCESS in intents:
        return VisualGoal.SHOW_PROCESS

    if understanding.relations:
        return VisualGoal.SHOW_STRUCTURE

    if any(entity.type is EntityType.LOCATION for entity in understanding.entities):
        return VisualGoal.SHOW_PLACE

    return VisualGoal.EMPHASISE_STATEMENT


__all__ = ["Draughtsman", "DrawnVisual"]
