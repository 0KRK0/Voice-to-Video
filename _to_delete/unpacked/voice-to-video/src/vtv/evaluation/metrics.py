"""Stage 16 — Evaluation metrics.

Tests stop quality regressing. Evaluation is how quality improves. They are
different activities and this module is the second one.

Every metric here is computed from artefacts the pipeline already produces, and
every one is deterministic — no model grades another model. That is a
limitation and it is stated: judgements like "is this visual beautiful" are not
measured here, and adding a model-graded rubric is future work. What *is*
measured is the set of properties that can be checked exactly, which turns out
to include most of the things that go wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vtv.contracts.scene import SceneGraph
from vtv.contracts.semantics import Understanding
from vtv.contracts.timeline import PlaceholderClipSource, Timeline
from vtv.contracts.transcript import Transcript
from vtv.contracts.visual_plan import VisualPlan, VisualStrategy
from vtv.pipeline.costs import CostLedger


@dataclass
class Metric:
    name: str
    value: float
    #: What "good" looks like. A metric with no target is an observation, not a
    #: goal, and is reported without a verdict.
    target: float | None = None
    higher_is_better: bool = True
    detail: str = ""

    @property
    def passing(self) -> bool | None:
        if self.target is None:
            return None
        return self.value >= self.target if self.higher_is_better else self.value <= self.target


@dataclass
class Evaluation:
    label: str
    metrics: list[Metric] = field(default_factory=list)

    def add(self, *metrics: Metric) -> None:
        self.metrics.extend(metrics)

    @property
    def failures(self) -> list[Metric]:
        return [m for m in self.metrics if m.passing is False]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "passed": self.passed,
            "metrics": [
                {
                    "name": m.name,
                    "value": round(m.value, 4),
                    "target": m.target,
                    "passing": m.passing,
                    "detail": m.detail,
                }
                for m in self.metrics
            ],
        }


# ---------------------------------------------------------------------------
# Individual measurements
# ---------------------------------------------------------------------------

def transcript_coverage(transcript: Transcript, duration: float) -> Metric:
    """How much of the recording produced words.

    A low value means the transcriber missed speech, which nothing downstream
    can recover from.
    """
    covered = sum(segment.span.duration for segment in transcript.segments)
    return Metric(
        name="transcript_coverage",
        value=min(1.0, covered / duration) if duration > 0 else 0.0,
        target=0.6,
        detail=f"{covered:.1f}s of {duration:.1f}s",
    )


def understanding_density(understanding: Understanding) -> Metric:
    """Meaningful units per minute of speech.

    Too low means the reading was shallow; absurdly high means it split on
    punctuation, which is Rule 7's failure mode.
    """
    span = understanding.span
    minutes = (span.duration / 60.0) if span else 0.0
    value = len(understanding.units) / minutes if minutes > 0 else 0.0
    return Metric(
        name="units_per_minute",
        value=value,
        target=None,
        detail=f"{len(understanding.units)} units",
    )


#: Below this many units a ratio is noise, not a measurement. Metrics report
#: their value but withhold a verdict — a two-sentence script cannot fail a
#: grouping target, and pretending otherwise would train us to ignore failures.
MIN_SAMPLE_UNITS = 4


def entity_grounding(understanding: Understanding) -> Metric:
    """Fraction of units that name at least one subject.

    Units with no entity are pronoun continuations and filler; a corpus where
    most units name nothing means entity extraction is failing.
    """
    visualisable = [u for u in understanding.units if u.is_visualisable]
    if not visualisable:
        return Metric(name="entity_grounding", value=0.0, target=None)
    grounded = sum(1 for u in visualisable if u.entity_ids)
    return Metric(
        name="entity_grounding",
        value=grounded / len(visualisable),
        target=0.3 if len(visualisable) >= MIN_SAMPLE_UNITS else None,
        detail=f"{grounded}/{len(visualisable)} units name a subject",
    )


def scene_compression(understanding: Understanding, scene_graph: SceneGraph) -> Metric:
    """Semantic units per scene.

    **The Rule 7 metric.** A value at or near 1.0 means the engine is producing
    one scene per idea per sentence — a slideshow. Above 1.2 means it is
    genuinely grouping.
    """
    if not scene_graph.scenes:
        return Metric(name="scene_compression", value=0.0, target=1.2)
    visualisable = [u for u in understanding.units if u.is_visualisable]
    return Metric(
        name="scene_compression",
        value=len(visualisable) / len(scene_graph.scenes),
        # Three sentences that are three separate ideas *should* be three
        # scenes. Grouping is only measurable once there is something to group.
        target=1.2 if len(visualisable) >= MIN_SAMPLE_UNITS else None,
        detail=f"{len(visualisable)} units → {len(scene_graph.scenes)} scenes",
    )


def narration_coverage(scene_graph: SceneGraph, duration: float) -> Metric:
    """Fraction of the recording covered by a scene. Gaps are silent failures."""
    if duration <= 0:
        return Metric(name="narration_coverage", value=0.0, target=0.99)
    gaps = sum(gap.duration for gap in scene_graph.gaps())
    covered = sum(scene.span.duration for scene in scene_graph.scenes)
    return Metric(
        name="narration_coverage",
        value=min(1.0, covered / duration),
        target=0.99,
        detail=f"{gaps:.2f}s of gaps",
    )


def scene_duration_health(scene_graph: SceneGraph) -> Metric:
    """Fraction of scenes long enough to land with a viewer."""
    if not scene_graph.scenes:
        return Metric(name="scene_duration_health", value=0.0, target=0.9)
    good = sum(1 for scene in scene_graph.scenes if 1.5 <= scene.duration <= 30.0)
    return Metric(
        name="scene_duration_health",
        value=good / len(scene_graph.scenes),
        target=0.9,
        detail=f"{good}/{len(scene_graph.scenes)} scenes within 1.5–30s",
    )


def drawn_share(visual_plan: VisualPlan) -> Metric:
    """Share of shots the system draws itself rather than buying.

    The economics of the company in one number (`docs/AI_PROVIDER_POLICY.md`).
    """
    if not visual_plan.scene_plans:
        return Metric(name="drawn_share", value=0.0, target=0.5)
    mix = visual_plan.strategy_mix()
    drawn = mix.get(VisualStrategy.PROGRAMMATIC.value, 0) + mix.get(
        VisualStrategy.EXISTING_ASSET.value, 0
    )
    return Metric(
        name="drawn_share",
        value=drawn / len(visual_plan.scene_plans),
        target=0.5,
        detail=str(mix),
    )


def generation_share(visual_plan: VisualPlan) -> Metric:
    """Share of shots that reach for a generative model. Lower is better."""
    if not visual_plan.scene_plans:
        return Metric(name="generation_share", value=0.0, target=0.3, higher_is_better=False)
    mix = visual_plan.strategy_mix()
    generated = mix.get(VisualStrategy.GENERATED_IMAGE.value, 0) + mix.get(
        VisualStrategy.GENERATED_VIDEO.value, 0
    )
    return Metric(
        name="generation_share",
        value=generated / len(visual_plan.scene_plans),
        target=0.3,
        higher_is_better=False,
        detail=str(mix),
    )


def fallback_readiness(visual_plan: VisualPlan) -> Metric:
    """Fraction of fallible shots that have a way down (Rule 8)."""
    fallible = [
        plan
        for plan in visual_plan.scene_plans
        if plan.primary.strategy
        in {
            VisualStrategy.GENERATED_IMAGE,
            VisualStrategy.GENERATED_VIDEO,
            VisualStrategy.LICENSED_MEDIA,
            VisualStrategy.EXISTING_ASSET,
        }
    ]
    if not fallible:
        return Metric(
            name="fallback_readiness", value=1.0, target=1.0, detail="no fallible shots"
        )
    ready = sum(1 for plan in fallible if plan.fallbacks)
    return Metric(
        name="fallback_readiness",
        value=ready / len(fallible),
        target=1.0,
        detail=f"{ready}/{len(fallible)} fallible shots have a ladder",
    )


def rationale_quality(visual_plan: VisualPlan) -> Metric:
    """Fraction of decisions that actually explain themselves.

    A rationale of four characters technically validates. This checks that
    decisions are reviewable rather than merely well-formed.
    """
    if not visual_plan.scene_plans:
        return Metric(name="rationale_quality", value=0.0, target=0.95)
    good = sum(
        1
        for plan in visual_plan.scene_plans
        if len(plan.primary.rationale.split()) >= 6
    )
    return Metric(
        name="rationale_quality",
        value=good / len(visual_plan.scene_plans),
        target=0.95,
        detail=f"{good}/{len(visual_plan.scene_plans)} decisions explained",
    )


def visual_completeness(timeline: Timeline) -> Metric:
    """Fraction of screen time showing something other than a placeholder."""
    if not timeline.clips or timeline.duration_seconds <= 0:
        return Metric(name="visual_completeness", value=0.0, target=0.95)
    broken = sum(
        clip.span.duration
        for clip in timeline.clips
        if isinstance(clip.source, PlaceholderClipSource)
    )
    return Metric(
        name="visual_completeness",
        value=1.0 - (broken / timeline.duration_seconds),
        target=0.95,
        detail=f"{timeline.placeholder_count} placeholder shot(s)",
    )


def caption_sync(timeline: Timeline, transcript: Transcript) -> Metric:
    """How much of the spoken audio has a caption over it.

    Computed by overlap, so a caption that drifts away from its words counts
    against the score even though it exists.
    """
    spoken = sum(segment.span.duration for segment in transcript.segments)
    if spoken <= 0:
        return Metric(name="caption_sync", value=0.0, target=0.85)
    covered = 0.0
    for segment in transcript.segments:
        for cue in timeline.captions:
            overlap = cue.span.intersection(segment.span)
            if overlap:
                covered += overlap.duration
    return Metric(
        name="caption_sync",
        value=min(1.0, covered / spoken),
        target=0.85,
        detail=f"{len(timeline.captions)} cues",
    )


def timeline_continuity(timeline: Timeline) -> Metric:
    """Fraction of the video with a visual on screen."""
    if timeline.duration_seconds <= 0:
        return Metric(name="timeline_continuity", value=0.0, target=1.0)
    gaps = sum(gap.duration for gap in timeline.coverage_gaps())
    return Metric(
        name="timeline_continuity",
        value=1.0 - min(1.0, gaps / timeline.duration_seconds),
        target=1.0,
        detail=f"{gaps:.2f}s uncovered",
    )


def factual_safety(understanding: Understanding, visual_plan: VisualPlan) -> Metric:
    """Charts and timelines must only use values the speaker actually stated.

    The check is exact: every numeric value in a chart spec and every year in a
    timeline spec has to appear in the understanding. A visual that contradicts
    the narration is the worst failure this system can produce, because it is
    confident and wrong.
    """
    stated_numbers = {
        round(quantity.value, 6)
        for unit in understanding.units
        for quantity in unit.quantities
    }
    stated_dates = {
        entity.name
        for entity in understanding.entities
        if entity.type.value == "date"
    }

    checked = 0
    grounded = 0
    for plan in visual_plan.scene_plans:
        spec = getattr(plan.primary.requirements, "spec", None)
        if spec is None:
            continue
        primitive = spec.primitive.value
        if primitive == "chart":
            for series in spec.series:
                for point in series.points:
                    checked += 1
                    if round(point.value, 6) in stated_numbers:
                        grounded += 1
        elif primitive == "timeline":
            for event in spec.events:
                checked += 1
                if event.when in stated_dates or any(
                    date in event.when for date in stated_dates
                ):
                    grounded += 1

    if checked == 0:
        return Metric(
            name="factual_safety", value=1.0, target=1.0, detail="no factual visuals"
        )
    return Metric(
        name="factual_safety",
        value=grounded / checked,
        target=1.0,
        detail=f"{grounded}/{checked} values traced to the narration",
    )


def cost_efficiency(ledger: CostLedger, duration: float) -> Metric:
    """Dollars per minute of finished video. Lower is better."""
    minutes = duration / 60.0 if duration > 0 else 1.0
    return Metric(
        name="usd_per_minute",
        value=ledger.total_usd / minutes,
        target=0.50,
        higher_is_better=False,
        detail=f"${ledger.total_usd:.4f} over {duration:.0f}s",
    )


__all__ = [
    "MIN_SAMPLE_UNITS",
    "Evaluation",
    "Metric",
    "caption_sync",
    "cost_efficiency",
    "drawn_share",
    "entity_grounding",
    "factual_safety",
    "fallback_readiness",
    "generation_share",
    "narration_coverage",
    "rationale_quality",
    "scene_compression",
    "scene_duration_health",
    "timeline_continuity",
    "transcript_coverage",
    "understanding_density",
    "visual_completeness",
]
