"""Stage 24 — the mandatory grounding gate.

This module exists because of a specific defect found by the 2026-08-13 audit,
and the shape of the fix matters more than the code.

**What was wrong.** Grounding was implemented *inside* `RuleBasedVisualDirector`.
`LlmVisualDirector` never called it, and the wiring makes the LLM director
primary whenever a text credential is configured. So the anti-hallucination
guarantee guarded the one path that cannot hallucinate — deterministic rules
over extracted quantities — and was absent from the one that certainly can.

**Why a gate rather than a fix.** Adding the call to the second director would
have left the same class of bug available to the third. A safety property that
each implementation must remember to invoke is not a safety property; it is a
convention with a good reputation. So the check moved out of the directors
entirely:

    scene graph ─▶ any director ─▶ VisualPlan ─▶ PlanGate ─▶ approved plan ─▶ compose

Directors *propose*. The gate *disposes*. It runs unconditionally in the
orchestrator, it does not know which director produced the plan, and a director
that has never heard of grounding is still subject to it.

**What the gate refuses.** Only programmatic visuals make precise claims, so
only they are checked (`docs/GROUNDING.md`). A fetched photograph or a generated
image asserts no number, date or place, and holding it to this standard would
refuse every illustrative shot in the product.

**What the gate guarantees on the way out.**

1. Every scene in the graph has a plan. A director that silently drops a scene
   gets one supplied, rather than the composer discovering a hole.
2. No approved directive carries an ungrounded spec.
3. A scene whose whole ladder was refused falls back to setting the narration as
   type — which by construction cannot misstate the source.
4. Every refusal is recorded as a `DegradationStep` and emitted as an event, so
   a missing chart is explained rather than mysterious.
"""

from __future__ import annotations

from dataclasses import dataclass

from vtv.contracts.base import Budget, Confidence
from vtv.contracts.errors import DegradationReason, DegradationStep, Status
from vtv.contracts.scene import Scene, SceneGraph
from vtv.contracts.semantics import Understanding
from vtv.contracts.visual_language import TypographySpec
from vtv.contracts.visual_plan import (
    ProgrammaticRequirements,
    SceneVisualPlan,
    VisualDirective,
    VisualPlan,
    VisualStrategy,
)
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.grounding import Evidence, check_spec
from vtv.pipeline.ingestion import DocumentContext


@dataclass(frozen=True)
class GateOutcome:
    """What the gate did, for the record and for the API."""

    plan: VisualPlan
    #: One entry per refused directive, in scene order.
    degradations: tuple[DegradationStep, ...] = ()
    #: Scenes the director failed to plan at all, which the gate supplied.
    supplied_scene_ids: tuple[str, ...] = ()
    #: Scenes whose entire ladder was refused and fell back to narration type.
    fully_refused_scene_ids: tuple[str, ...] = ()

    @property
    def refusals(self) -> int:
        return len(self.degradations)

    def as_dict(self) -> dict[str, object]:
        return {
            "refusals": self.refusals,
            "supplied_scenes": list(self.supplied_scene_ids),
            "fully_refused_scenes": list(self.fully_refused_scene_ids),
        }


@dataclass
class PlanGate:
    """The single point at which a visual plan becomes renderable.

    Constructed once in `wiring.py` and called once in the orchestrator. It is
    deliberately not injectable as `None`: a deployment that could switch the
    gate off would eventually be a deployment that had.
    """

    events: EventSink

    def apply(
        self,
        *,
        plan: VisualPlan,
        scene_graph: SceneGraph,
        understanding: Understanding,
        context: DocumentContext | None = None,
        evidence: Evidence | None = None,
    ) -> GateOutcome:
        """Approve a plan, refusing anything the source does not support.

        ``evidence`` may be supplied by a caller that has already built it;
        otherwise it is built here. Building it here is the safe default —
        a caller that forgets is the failure mode this module exists to remove.
        """
        proof = evidence or self.evidence_for(
            scene_graph=scene_graph, understanding=understanding, context=context
        )

        by_scene = {item.scene_id: item for item in plan.scene_plans}
        approved: list[SceneVisualPlan] = []
        degradations: list[DegradationStep] = []
        supplied: list[str] = []
        fully_refused: list[str] = []

        for scene in scene_graph.scenes:
            scene_plan = by_scene.get(scene.scene_id)
            if scene_plan is None:
                # A director dropped a scene. Supply one rather than let the
                # composer meet a hole it cannot explain.
                supplied.append(scene.scene_id)
                approved.append(self._narration_plan(scene))
                self.events.emit(
                    EventName.GROUNDING_REFUSED,
                    project_id=plan.project_id,
                    data={
                        "scene_id": scene.scene_id,
                        "reason": "the director produced no plan for this scene",
                    },
                )
                continue

            kept, refused = self._filter(scene, scene_plan, proof, plan.project_id)
            degradations.extend(refused)

            if not kept:
                fully_refused.append(scene.scene_id)
                approved.append(self._narration_plan(scene, budget=scene_plan.budget))
                continue

            approved.append(
                scene_plan.model_copy(
                    update={
                        "primary": kept[0],
                        "fallbacks": kept[1:],
                        "status": Status.READY,
                    }
                )
            )

        gated = plan.model_copy(
            update={"scene_plans": approved, "status": Status.READY}
        )

        self.events.emit(
            EventName.PLAN_APPROVED,
            project_id=plan.project_id,
            data={
                "scenes": len(approved),
                "refusals": len(degradations),
                "supplied": len(supplied),
                "fully_refused": len(fully_refused),
                "strategy_mix": gated.strategy_mix(),
            },
        )
        return GateOutcome(
            plan=gated,
            degradations=tuple(degradations),
            supplied_scene_ids=tuple(supplied),
            fully_refused_scene_ids=tuple(fully_refused),
        )

    @staticmethod
    def evidence_for(
        *,
        scene_graph: SceneGraph,
        understanding: Understanding,
        context: DocumentContext | None = None,
    ) -> Evidence:
        """Everything the source asserts, assembled once per project.

        The narration is the primary evidence — it is literally what the source
        says. Omitting it once caused a correctly labelled chart to be refused,
        which the evaluation corpus caught; it is therefore supplied here rather
        than left to each caller.
        """
        return Evidence.build(
            narration=" ".join(scene.narration for scene in scene_graph.scenes),
            understanding=understanding,
            tables=list(context.tables) if context else None,
        )

    # -- internals --------------------------------------------------------

    def _filter(
        self,
        scene: Scene,
        scene_plan: SceneVisualPlan,
        evidence: Evidence,
        project_id: str,
    ) -> tuple[list[VisualDirective], list[DegradationStep]]:
        kept: list[VisualDirective] = []
        refused: list[DegradationStep] = []

        for directive in scene_plan.ladder:
            spec = getattr(directive.requirements, "spec", None)
            if spec is None:
                # Not a claim-making visual. Photographs and generated imagery
                # are exempt by design, not by oversight.
                kept.append(directive)
                continue

            result = check_spec(spec, evidence)
            if result.is_acceptable:
                kept.append(directive)
                continue

            refused.append(
                DegradationStep(
                    from_strategy=directive.strategy.value,
                    to_strategy="refused_ungrounded",
                    # Declining is not a provider failing. It is us saying no.
                    reason=DegradationReason.SAFETY_REFUSED,
                )
            )
            self.events.emit(
                EventName.GROUNDING_REFUSED,
                project_id=project_id,
                data={
                    "scene_id": scene.scene_id,
                    "primitive": spec.primitive.value,
                    "strategy": directive.strategy.value,
                    "reason": result.reason(),
                    "unsupported": list(result.unsupported[:6]),
                },
            )
        return kept, refused

    @staticmethod
    def _narration_directive(scene: Scene) -> VisualDirective:
        """The one visual that can never be ungrounded: the speaker's words."""
        headline = " ".join(scene.narration.split())[:120] or "…"
        return VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(
                spec=TypographySpec(
                    headline=headline,
                    preferred_duration=max(2.0, min(scene.duration, 10.0)),
                )
            ),
            rationale=(
                "Every richer visual made a claim the source did not support, so "
                "the narration is set as type instead — it cannot misstate itself."
            ),
            confidence=Confidence(0.6),
        )

    def _narration_plan(
        self, scene: Scene, *, budget: Budget | None = None
    ) -> SceneVisualPlan:
        return SceneVisualPlan(
            scene_id=scene.scene_id,
            primary=self._narration_directive(scene),
            fallbacks=[],
            budget=budget or Budget(),
            status=Status.READY,
        )


__all__ = ["GateOutcome", "PlanGate"]
