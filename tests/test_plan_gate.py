"""Stage 24 — the grounding gate is mandatory, not a director's habit.

The 2026-08-13 audit found that `LlmVisualDirector` bypassed grounding entirely
while being the director wiring selects in production. These tests exist to make
that class of defect impossible to reintroduce: they check the *gate*, and they
check it against a director that has never heard of grounding.

If someone adds a fourth director tomorrow and forgets about grounding, the
first test in this file still passes and the system is still safe. That is the
property being protected.
"""

from __future__ import annotations

import asyncio
import unittest

from vtv.contracts.base import Budget, IdPrefix, new_id
from vtv.contracts.errors import DegradationReason, Status
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.visual_language import (
    ChartKind,
    ChartSeries,
    ChartSpec,
    DataPoint,
    VisualPrimitive,
)
from vtv.contracts.visual_plan import (
    LicensedMediaRequirements,
    ProgrammaticRequirements,
    SceneVisualPlan,
    VisualDirective,
    VisualPlan,
    VisualStrategy,
)
from vtv.observability.events import EventSink
from vtv.pipeline.plan_gate import PlanGate
from vtv.pipeline.scenes import SceneEngine
from vtv.pipeline.text_entry import transcript_from_text
from vtv.pipeline.understanding import (
    HeuristicUnderstandingEngine,
    UnderstandingService,
)

SCRIPT = (
    "Renewable energy reached 30 percent of generation in 2023. "
    "Ten years earlier it was 12 percent. "
    "The cost of solar fell by 80 percent over the same period."
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def chart_directive(*points: tuple[str, float]) -> VisualDirective:
    return VisualDirective(
        strategy=VisualStrategy.PROGRAMMATIC,
        requirements=ProgrammaticRequirements(
            spec=ChartSpec(
                kind=ChartKind.COLUMN,
                series=[
                    ChartSeries(
                        name="series",
                        points=[
                            DataPoint(label=label, value=value)
                            for label, value in points
                        ],
                    )
                ],
                preferred_duration=4.0,
            )
        ),
        rationale="a chart of the stated figures",
        confidence=0.9,
    )


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.events = EventSink()
        self.seen: list[str] = []
        self.events.subscribe(lambda event: self.seen.append(event.name.value))

        project_id = new_id(IdPrefix.PROJECT)
        transcript = transcript_from_text(SCRIPT, organisation_id=SYSTEM_ORGANISATION_ID, project_id=project_id)
        self.understanding = run(
            UnderstandingService(
                engine=HeuristicUnderstandingEngine(), events=self.events
            ).understand(transcript)
        )
        span = transcript.span
        assert span is not None
        self.graph = SceneEngine(events=self.events).build(
            transcript=transcript,
            understanding=self.understanding,
            style=StyleProfile(),
            total_duration=span.end,
        )
        self.gate = PlanGate(events=self.events)

    def plan_of(self, *directives: VisualDirective) -> VisualPlan:
        """A plan assigning the given directive to every scene, in order.

        Deliberately built by hand rather than by a director: the gate must not
        care who produced the plan, and a test that used a real director would
        be testing the director instead.
        """
        scene_plans = []
        for index, scene in enumerate(self.graph.scenes):
            scene_plans.append(
                SceneVisualPlan(
                    scene_id=scene.scene_id,
                    primary=directives[min(index, len(directives) - 1)],
                    fallbacks=[],
                    budget=Budget(max_cost_usd=0.5),
                    status=Status.PENDING,
                )
            )
        return VisualPlan(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=self.graph.project_id,
            scene_graph_id=self.graph.scene_graph_id,
            scene_plans=scene_plans,
            status=Status.READY,
        )

    def apply(self, plan: VisualPlan):  # type: ignore[no-untyped-def]
        return self.gate.apply(
            plan=plan, scene_graph=self.graph, understanding=self.understanding
        )


class TheGateCannotBeBypassed(GateTestCase):
    def test_a_fabricated_chart_from_any_director_is_refused(self) -> None:
        """The exact defect the audit found, now impossible.

        This plan did not come from a director that grounds. It came from a
        model that invented a number. The gate refuses it anyway.
        """
        outcome = self.apply(self.plan_of(chart_directive(("2030", 55.0), ("2040", 90.0))))

        self.assertGreater(outcome.refusals, 0)
        self.assertIn("grounding.refused", self.seen)
        for scene_plan in outcome.plan.scene_plans:
            spec = getattr(scene_plan.primary.requirements, "spec", None)
            if spec is not None:
                self.assertIsNot(
                    spec.primitive,
                    VisualPrimitive.CHART,
                    "a fabricated chart survived the gate",
                )

    def test_a_grounded_chart_survives(self) -> None:
        """The gate must not be a blanket refusal — that would be useless."""
        outcome = self.apply(self.plan_of(chart_directive(("2013", 12.0), ("2023", 30.0))))
        self.assertEqual(outcome.refusals, 0)
        self.assertEqual(outcome.fully_refused_scene_ids, ())
        primitives = {
            spec.primitive
            for scene_plan in outcome.plan.scene_plans
            if (spec := getattr(scene_plan.primary.requirements, "spec", None))
        }
        self.assertIn(VisualPrimitive.CHART, primitives)

    def test_a_refused_scene_falls_back_to_narration_type(self) -> None:
        """Refusal must degrade, never leave a hole (Rule 8)."""
        outcome = self.apply(self.plan_of(chart_directive(("2030", 55.0), ("2040", 90.0))))
        self.assertEqual(
            len(outcome.plan.scene_plans), len(self.graph.scenes)
        )
        for scene_plan in outcome.plan.scene_plans:
            self.assertIs(scene_plan.status, Status.READY)

    def test_a_scene_the_director_dropped_is_supplied(self) -> None:
        """A director that silently omits a scene must not produce a hole."""
        plan = self.plan_of(chart_directive(("2013", 12.0), ("2023", 30.0)))
        truncated = plan.model_copy(update={"scene_plans": plan.scene_plans[:1]})
        outcome = self.apply(truncated)

        self.assertEqual(len(outcome.plan.scene_plans), len(self.graph.scenes))
        self.assertEqual(
            len(outcome.supplied_scene_ids), len(self.graph.scenes) - 1
        )

    def test_every_refusal_is_recorded_as_a_degradation(self) -> None:
        outcome = self.apply(self.plan_of(chart_directive(("9999", 1234.0), ("8888", 4321.0))))
        self.assertTrue(outcome.degradations)
        for step in outcome.degradations:
            self.assertIs(step.reason, DegradationReason.SAFETY_REFUSED)
            self.assertEqual(step.to_strategy, "refused_ungrounded")

    def test_non_claiming_visuals_are_exempt(self) -> None:
        """A photograph asserts no number. Refusing it would refuse the product."""
        media = VisualDirective(
            strategy=VisualStrategy.LICENSED_MEDIA,
            requirements=LicensedMediaRequirements(query="solar panels on a roof"),
            rationale="an illustrative photograph of the subject",
            confidence=0.7,
        )
        outcome = self.apply(self.plan_of(media))
        self.assertEqual(outcome.refusals, 0)

    def test_the_gate_emits_an_approval_event(self) -> None:
        """An operator must be able to see that the gate ran at all."""
        self.apply(self.plan_of(chart_directive(("2013", 12.0), ("2023", 30.0))))
        self.assertIn("visual.plan.approved", self.seen)


class TheGateIsWiredIntoThePipeline(unittest.TestCase):
    def test_a_pipeline_cannot_be_constructed_without_a_gate(self) -> None:
        """`plan_gate` is a required field, so no deployment can omit it.

        This is the structural half of the fix. The gate being correct matters
        less than the gate being unavoidable.
        """
        import inspect

        from vtv.pipeline.orchestrator import Pipeline

        signature = inspect.signature(Pipeline)
        gate = signature.parameters["plan_gate"]
        self.assertIs(
            gate.default,
            inspect.Parameter.empty,
            "plan_gate has a default, so a pipeline can be built without one",
        )

    def test_the_wiring_supplies_one(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from vtv.config import Settings
        from vtv.wiring import build

        with TemporaryDirectory(prefix="vtv-gate-") as scratch:
            assembly = build(Settings(asset_search_endpoint="", storage_root=Path(scratch) / "storage"))
            self.assertIsInstance(assembly.pipeline.plan_gate, PlanGate)

    def test_no_director_imports_the_grounding_module(self) -> None:
        """Grounding is not a director's concern any more.

        If a director imports `check_spec` again, the temptation to call it
        there — and to forget it in the next director — has returned.
        """
        from pathlib import Path

        source = Path("src/vtv/pipeline/director.py").read_text(encoding="utf-8")
        self.assertNotIn("check_spec", source)
        self.assertNotIn("from vtv.pipeline.grounding", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
