"""Stage 16 — The evaluation harness.

Runs a corpus of scripts through the intelligence stages and scores the result.
Rendering is skipped by default: the questions evaluation asks — did we
understand it, did we group it well, did we choose the right visual, is it
factually grounded — are all answered before a single frame is drawn, and
skipping the render makes the whole corpus run in seconds instead of an hour.

The corpus is deliberately small and hand-written. Ten cases whose correct
handling somebody has actually thought about are worth more than a thousand
scraped ones, especially while the metrics themselves are still being calibrated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.evaluation import metrics as m
from vtv.evaluation.corpus import EvaluationCase, default_corpus
from vtv.observability.events import EventSink
from vtv.pipeline.captions import CaptionBuilder
from vtv.pipeline.costs import CostLedger
from vtv.pipeline.director import RuleBasedVisualDirector, VisualDirector
from vtv.pipeline.scenes import SceneEngine
from vtv.pipeline.text_entry import transcript_from_text
from vtv.pipeline.understanding import (
    HeuristicUnderstandingEngine,
    UnderstandingEngine,
)


@dataclass
class CaseResult:
    case: EvaluationCase
    evaluation: m.Evaluation
    expectations: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.evaluation.passed and all(ok for _, ok, _ in self.expectations)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.evaluation.as_dict(),
            "case": self.case.name,
            "passed": self.passed,
            "expectations": [
                {"name": name, "passed": ok, "detail": detail}
                for name, ok, detail in self.expectations
            ],
        }


@dataclass
class HarnessReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def aggregate(self) -> dict[str, float]:
        """Mean of every metric across the corpus.

        The aggregate is what gets tracked release over release; the per-case
        detail is what gets debugged when it moves.
        """
        totals: dict[str, list[float]] = {}
        for result in self.results:
            for metric in result.evaluation.metrics:
                totals.setdefault(metric.name, []).append(metric.value)
        return {
            name: round(sum(values) / len(values), 4)
            for name, values in sorted(totals.items())
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "cases": len(self.results),
            "failed": [r.case.name for r in self.results if not r.passed],
            "aggregate": self.aggregate(),
            "results": [result.as_dict() for result in self.results],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


@dataclass
class EvaluationHarness:
    """Scores the intelligence stages against a corpus."""

    understanding: UnderstandingEngine = field(
        default_factory=HeuristicUnderstandingEngine
    )
    director: VisualDirector | None = None
    events: EventSink = field(default_factory=EventSink)

    async def run_case(self, case: EvaluationCase) -> CaseResult:
        director = self.director or RuleBasedVisualDirector(events=self.events)
        scenes = SceneEngine(events=self.events)

        transcript = transcript_from_text(
            case.script,
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id="prj_" + "e" * 24,
        )
        duration = transcript.span.end if transcript.span else 0.0
        understanding = await self.understanding.understand(transcript)
        scene_graph = scenes.build(
            transcript=transcript,
            understanding=understanding,
            style=case.style or StyleProfile(),
            total_duration=duration,
        )
        visual_plan = await director.direct(
            scene_graph=scene_graph, understanding=understanding
        )
        cues = CaptionBuilder().build(transcript, limit=duration)

        evaluation = m.Evaluation(label=case.name)
        evaluation.add(
            m.transcript_coverage(transcript, duration),
            m.understanding_density(understanding),
            m.entity_grounding(understanding),
            m.scene_compression(understanding, scene_graph),
            m.narration_coverage(scene_graph, duration),
            m.scene_duration_health(scene_graph),
            m.drawn_share(visual_plan),
            m.generation_share(visual_plan),
            m.fallback_readiness(visual_plan),
            m.rationale_quality(visual_plan),
            m.factual_safety(understanding, visual_plan),
            m.cost_efficiency(CostLedger(), duration),
        )

        expectations = case.check(
            understanding=understanding,
            scene_graph=scene_graph,
            visual_plan=visual_plan,
            captions=cues,
        )
        return CaseResult(case=case, evaluation=evaluation, expectations=expectations)

    async def run(self, corpus: list[EvaluationCase] | None = None) -> HarnessReport:
        cases = corpus if corpus is not None else default_corpus()
        report = HarnessReport()
        for case in cases:
            report.results.append(await self.run_case(case))
        return report


async def main(output: Path | None = None) -> int:
    """``python -m vtv.evaluation.harness`` — run the corpus and print the score."""
    report = await EvaluationHarness().run()
    text = report.to_json()
    if output:
        output.write_text(text, encoding="utf-8")
    aggregate = report.aggregate()
    width = max(len(name) for name in aggregate) if aggregate else 10
    print(f"{'metric'.ljust(width)}  value")
    for name, value in aggregate.items():
        print(f"{name.ljust(width)}  {value}")
    print()
    print(f"cases: {len(report.results)}  passed: {report.passed}")
    if not report.passed:
        for result in report.results:
            if result.passed:
                continue
            print(f"  {result.case.name}:")
            for metric in result.evaluation.failures:
                print(f"    metric {metric.name} = {metric.value:.3f} (target {metric.target}) {metric.detail}")
            for name, ok, detail in result.expectations:
                if not ok:
                    print(f"    expectation {name}: {detail}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    import asyncio
    import sys

    raise SystemExit(asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)))


__all__ = ["CaseResult", "EvaluationHarness", "HarnessReport", "main"]
