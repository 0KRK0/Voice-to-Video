"""The evaluation corpus.

Ten hand-written cases, each with an expectation somebody thought about. They
are short on purpose: a case exists to test one decision, and a five-minute
monologue tests everything at once and therefore nothing in particular.

Each case states what the system *should* conclude, in terms of the contract
rather than of an implementation. "This should become a chart" is a claim about
the product; "this should call `_chart`" would be a claim about the code, and
would have to be rewritten every time the code improved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from vtv.contracts.scene import SceneGraph, VisualGoal
from vtv.contracts.semantics import SemanticIntent, Understanding
from vtv.contracts.style import StyleProfile
from vtv.contracts.timeline import CaptionCue
from vtv.contracts.visual_language import VisualPrimitive
from vtv.contracts.visual_plan import VisualPlan

Check = Callable[..., tuple[str, bool, str]]


@dataclass
class EvaluationCase:
    name: str
    script: str
    style: StyleProfile | None = None
    checks: list[Check] = field(default_factory=list)

    def check(
        self,
        *,
        understanding: Understanding,
        scene_graph: SceneGraph,
        visual_plan: VisualPlan,
        captions: list[CaptionCue],
    ) -> list[tuple[str, bool, str]]:
        return [
            check(
                understanding=understanding,
                scene_graph=scene_graph,
                visual_plan=visual_plan,
                captions=captions,
            )
            for check in self.checks
        ]


# ---------------------------------------------------------------------------
# Reusable expectations
# ---------------------------------------------------------------------------

def uses_primitive(primitive: VisualPrimitive) -> Check:
    def check(*, visual_plan: VisualPlan, **_: object) -> tuple[str, bool, str]:
        found = [
            spec.primitive.value
            for spec in (
                getattr(plan.primary.requirements, "spec", None)
                for plan in visual_plan.scene_plans
            )
            if spec is not None
        ]
        return (
            f"uses_{primitive.value}",
            primitive.value in found,
            f"primitives chosen: {found or 'none'}",
        )

    return check


def has_goal(goal: VisualGoal) -> Check:
    def check(*, scene_graph: SceneGraph, **_: object) -> tuple[str, bool, str]:
        goals = [scene.visual_goal.value for scene in scene_graph.scenes]
        return (f"goal_{goal.value}", goal.value in goals, f"goals: {goals}")

    return check


def finds_entity(name: str) -> Check:
    def check(*, understanding: Understanding, **_: object) -> tuple[str, bool, str]:
        names = [entity.search_name.lower() for entity in understanding.entities]
        return (
            f"entity_{name}",
            any(name.lower() in candidate for candidate in names),
            f"entities: {names}",
        )

    return check


def finds_intent(intent: SemanticIntent) -> Check:
    def check(*, understanding: Understanding, **_: object) -> tuple[str, bool, str]:
        intents = [unit.intent.value for unit in understanding.units]
        return (
            f"intent_{intent.value}",
            intent.value in intents,
            f"intents: {intents}",
        )

    return check


def merges_to_fewer_than(limit: int) -> Check:
    """Rule 7, as an expectation rather than an aspiration."""

    def check(*, scene_graph: SceneGraph, **_: object) -> tuple[str, bool, str]:
        count = len(scene_graph.scenes)
        return (
            f"scenes_under_{limit}",
            count < limit,
            f"{count} scenes",
        )

    return check


def no_generation() -> Check:
    """This material should cost nothing to visualise."""

    def check(*, visual_plan: VisualPlan, **_: object) -> tuple[str, bool, str]:
        mix = visual_plan.strategy_mix()
        generated = mix.get("generated_image", 0) + mix.get("generated_video", 0)
        return ("no_generation", generated == 0, f"mix: {mix}")

    return check


def every_scene_planned() -> Check:
    def check(
        *, scene_graph: SceneGraph, visual_plan: VisualPlan, **_: object
    ) -> tuple[str, bool, str]:
        missing = [
            scene.scene_id
            for scene in scene_graph.scenes
            if visual_plan.plan_for(scene.scene_id) is None
        ]
        return ("every_scene_planned", not missing, f"{len(missing)} unplanned")

    return check


def captions_cover_everything() -> Check:
    def check(
        *, captions: list[CaptionCue], scene_graph: SceneGraph, **_: object
    ) -> tuple[str, bool, str]:
        span = scene_graph.span
        if span is None or not captions:
            return ("captions_present", False, "no captions")
        covered = sum(cue.span.duration for cue in captions)
        ratio = covered / span.duration
        return ("captions_cover", ratio >= 0.7, f"{ratio:.0%} of the video")

    return check


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

def default_corpus() -> list[EvaluationCase]:
    return [
        EvaluationCase(
            name="quantity-change",
            script=(
                "The world population grew from one billion people to eight billion people. "
                "That happened in a little over two hundred years. "
                "Almost all of that growth came after industrialisation."
            ),
            checks=[
                uses_primitive(VisualPrimitive.CHART),
                no_generation(),
                every_scene_planned(),
                captions_cover_everything(),
            ],
        ),
        EvaluationCase(
            name="historical-event",
            script=(
                "The transistor was invented in 1947 at Bell Labs. "
                "It was smaller and far more efficient than the vacuum tubes that came before it. "
                "A vacuum tube was the size of a light bulb, but a transistor could be smaller than a grain of rice. "
                "Within twenty years transistors had replaced vacuum tubes almost everywhere."
            ),
            checks=[
                finds_entity("transistor"),
                finds_entity("1947"),
                finds_intent(SemanticIntent.COMPARISON),
                uses_primitive(VisualPrimitive.COMPARISON),
                merges_to_fewer_than(6),
                no_generation(),
            ],
        ),
        EvaluationCase(
            name="definition",
            script=(
                "A blockchain is a distributed ledger. "
                "Every participant keeps a copy, and every copy has to agree. "
                "That is what makes it hard to tamper with."
            ),
            checks=[
                finds_intent(SemanticIntent.DEFINITION),
                finds_entity("blockchain"),
                every_scene_planned(),
            ],
        ),
        EvaluationCase(
            name="process",
            script=(
                "First the water is heated to just under boiling. "
                "Then the ground coffee is added and left to steep. "
                "Finally the grounds are pressed to the bottom and the coffee is poured."
            ),
            checks=[
                finds_intent(SemanticIntent.PROCESS),
                has_goal(VisualGoal.SHOW_PROCESS),
                no_generation(),
            ],
        ),
        EvaluationCase(
            name="causation",
            script=(
                "Because the vacuum tubes were fragile, early computers failed constantly. "
                "Engineers spent more time replacing tubes than running programs. "
                "That is why the transistor mattered so much."
            ),
            checks=[
                finds_intent(SemanticIntent.CAUSATION),
                every_scene_planned(),
            ],
        ),
        EvaluationCase(
            name="place",
            script=(
                "The conference was held in Geneva. "
                "Delegates travelled there from Tokyo, London and Nairobi. "
                "It was the first time all three regions had agreed on anything."
            ),
            checks=[
                finds_entity("geneva"),
                every_scene_planned(),
            ],
        ),
        EvaluationCase(
            name="pure-statement",
            script=(
                "This changed everything about how we build things. "
                "Nothing was ever quite the same afterwards."
            ),
            checks=[
                uses_primitive(VisualPrimitive.TYPOGRAPHY),
                no_generation(),
            ],
        ),
        EvaluationCase(
            name="numbers-and-units",
            script=(
                "Renewable energy reached 30 percent of generation in 2023. "
                "Ten years earlier it was 12 percent. "
                "The cost of solar fell by 80 percent over the same period."
            ),
            checks=[
                uses_primitive(VisualPrimitive.CHART),
                no_generation(),
                every_scene_planned(),
            ],
        ),
        EvaluationCase(
            name="filler-heavy",
            script=(
                "So, um, yeah. "
                "Okay so what I wanted to talk about is how sleep affects memory. "
                "Right, um, so the brain consolidates memories during deep sleep. "
                "You know."
            ),
            checks=[
                merges_to_fewer_than(4),
                every_scene_planned(),
            ],
        ),
        EvaluationCase(
            name="single-sentence",
            script="Photosynthesis turns sunlight into sugar.",
            checks=[
                every_scene_planned(),
                captions_cover_everything(),
            ],
        ),
    ]


__all__ = [
    "Check",
    "EvaluationCase",
    "captions_cover_everything",
    "default_corpus",
    "every_scene_planned",
    "finds_entity",
    "finds_intent",
    "has_goal",
    "merges_to_fewer_than",
    "no_generation",
    "uses_primitive",
]
