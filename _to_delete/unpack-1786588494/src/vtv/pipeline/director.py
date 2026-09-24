"""Stage 5 — The Visual Director.

Scene in, `VisualPlan` out. This is the reasoning layer the company is built on:
for every idea the speaker expressed, decide how it should be shown.

`RuleBasedVisualDirector` is a complete, deterministic implementation. It is not
a placeholder for a model — it is the baseline, and on a lot of material it is
also the right answer. When somebody says "the population went from one billion
to eight billion", a chart of those two numbers is not an approximation of the
best visual, it *is* the best visual, and no amount of model capability improves
on it. Rules also give us three things a model cannot: they are free, they are
instant, and they are the same every time, which is what makes the evaluation
system in Stage 16 able to measure anything at all.

`LlmVisualDirector` will beat it on the harder half — abstract ideas, tone,
metaphor, knowing when a photograph carries more than a diagram. It produces the
same contract and falls back to the rules when it is unavailable or wrong.

Nothing in this module fetches, generates or renders anything. It returns
decisions with reasons, estimates and fallback ladders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from vtv.contracts.base import Budget, Confidence
from vtv.contracts.errors import Status, ValidationFailed, VTVError
from vtv.contracts.generation import GenerationKind, GenerationRequest, TextParams
from vtv.contracts.scene import Scene, SceneGraph, ScenePurpose, VisualGoal
from vtv.contracts.semantics import (
    Entity,
    EntityType,
    Quantity,
    SemanticIntent,
    SemanticUnit,
    Understanding,
)
from vtv.contracts.source import TableData
from vtv.contracts.style import StyleProfile
from vtv.contracts.visual_language import (
    AnimationSpec,
    CameraMotion,
    ChartKind,
    ChartSeries,
    ChartSpec,
    ComparisonSide,
    ComparisonSpec,
    DataPoint,
    Emphasis,
    MapMarker,
    MapSpec,
    NetworkEdge,
    NetworkNode,
    NetworkSpec,
    TimelineEvent,
    TimelineSpec,
    TypographySpec,
)
from vtv.contracts.visual_plan import (
    CostEstimate,
    ImageGenerationRequirements,
    LicensedMediaRequirements,
    ProgrammaticRequirements,
    SceneVisualPlan,
    VisualDirective,
    VisualPlan,
    VisualStrategy,
)
from vtv.observability.events import EventName, EventSink
from vtv.pipeline import gazetteer
from vtv.pipeline.grounding import Evidence, check_spec
from vtv.pipeline.ingestion import DocumentContext, chart_series_from_table

DIRECTOR_VERSION = "rules-1.0"
LLM_DIRECTOR_VERSION = "llm-1.0"

#: Default estimates used when the router has not supplied live capability data.
#: Deliberately conservative on the generative side: over-estimating the cost of
#: generation biases the Director towards drawing, which is the bias we want.
DEFAULT_ESTIMATES: dict[VisualStrategy, CostEstimate] = {
    VisualStrategy.EXISTING_ASSET: CostEstimate(usd=0.0, latency_seconds=0.1),
    VisualStrategy.PROGRAMMATIC: CostEstimate(usd=0.0, latency_seconds=0.4),
    VisualStrategy.LICENSED_MEDIA: CostEstimate(usd=0.0, latency_seconds=2.0),
    VisualStrategy.GENERATED_IMAGE: CostEstimate(usd=0.04, latency_seconds=14.0),
    VisualStrategy.GENERATED_VIDEO: CostEstimate(usd=1.20, latency_seconds=150.0),
    VisualStrategy.COMPOSITE: CostEstimate(usd=0.04, latency_seconds=14.0),
}

#: Generated video is only reached for when motion itself carries the meaning
#: *and* the scene is important enough to deserve it.
MOTION_WORDS = re.compile(
    r"\b(flow|flows|flowing|crash|crashes|spin|spinning|rotate|rotating|orbit|"
    r"orbiting|explode|exploding|collapse|collapsing|grow|growing|erupt|"
    r"erupting|swirl|swirling|drift|drifting|pour|pouring|race|racing)\b",
    re.I,
)

_COMPARISON_SPLIT = re.compile(
    r"\s+(?:than|versus|vs\.?|whereas|but|while|compared\s+to|unlike)\s+", re.I
)


class VisualDirector(Protocol):
    """Decides how each scene should be shown."""

    version: str

    async def direct(
        self,
        *,
        scene_graph: SceneGraph,
        understanding: Understanding,
        context: DocumentContext | None = None,
    ) -> VisualPlan: ...


# ---------------------------------------------------------------------------
# Rule-based director
# ---------------------------------------------------------------------------

@dataclass
class RuleBasedVisualDirector:
    """Deterministic visual decisions from meaning."""

    events: EventSink
    estimates: dict[VisualStrategy, CostEstimate] = field(
        default_factory=lambda: dict(DEFAULT_ESTIMATES)
    )
    #: Ceiling for the whole project. Checked against the worst case — the sum
    #: of every fallback ladder — not against primaries alone.
    budget: Budget = field(default_factory=lambda: Budget(max_cost_usd=1.0))
    #: Allow the most expensive strategy at all. Off by default: video
    #: generation has to be switched on deliberately, per project.
    allow_video_generation: bool = False
    version: str = DIRECTOR_VERSION

    async def direct(
        self,
        *,
        scene_graph: SceneGraph,
        understanding: Understanding,
        context: DocumentContext | None = None,
    ) -> VisualPlan:
        # A table the author already wrote beats anything inferable from the
        # prose around it: the numbers are exact, attributed and free.
        tables = _match_tables_to_scenes(scene_graph, context)
        # Stage 24. Everything the source actually asserts, built once. Any
        # drawn visual claiming something outside this is refused below.
        evidence = Evidence.build(
            # The narration is the primary evidence: it is literally what the
            # source says. Omitting it here caused a correct chart to be
            # refused, which the evaluation corpus caught.
            narration=" ".join(scene.narration for scene in scene_graph.scenes),
            understanding=understanding,
            tables=list(context.tables) if context else None,
        )
        plans = [
            self.plan_scene(
                scene,
                understanding,
                scene_graph.style,
                source_table=tables.get(scene.scene_id),
                evidence=evidence,
            )
            for scene in scene_graph.scenes
        ]
        plans = self._apply_budget(plans, understanding)

        plan = VisualPlan(
            project_id=scene_graph.project_id,
            scene_graph_id=scene_graph.scene_graph_id,
            scene_plans=plans,
            budget=self.budget,
            status=Status.READY,
        )
        self.events.emit(
            EventName.VISUAL_PLAN_CREATED,
            project_id=scene_graph.project_id,
            data={
                "director": self.version,
                "scenes": len(plans),
                "strategy_mix": plan.strategy_mix(),
                "expected_cost_usd": round(plan.expected_cost_usd, 4),
                "worst_case_cost_usd": round(plan.worst_case_cost_usd, 4),
            },
        )
        return plan

    # -- per scene --------------------------------------------------------

    def plan_scene(
        self,
        scene: Scene,
        understanding: Understanding,
        style: StyleProfile,
        *,
        source_table: TableData | None = None,
        evidence: Evidence | None = None,
    ) -> SceneVisualPlan:
        units = [
            unit
            for unit in (understanding.unit_by_id(i) for i in scene.semantic_unit_ids)
            if unit is not None
        ]
        entities = [
            entity
            for entity in (understanding.entity_by_id(i) for i in scene.entity_ids)
            if entity is not None
        ]
        quantities = [q for unit in units for q in unit.quantities]

        primary = self._primary(
            scene, units, entities, quantities, style, understanding, source_table
        )
        fallbacks = self._fallbacks(primary, scene, entities, style)

        # Stage 24 — the grounding gate. A drawn visual that asserts something
        # the source did not say is refused and the ladder descends. This runs
        # after the ladder is built so there is always somewhere to descend to.
        primary, fallbacks = self._ground(primary, fallbacks, scene, evidence)
        return SceneVisualPlan(
            scene_id=scene.scene_id,
            primary=primary,
            fallbacks=fallbacks,
            budget=Budget(
                max_cost_usd=round(
                    max(0.02, self.budget.max_cost_usd or 1.0) * scene.importance, 4
                )
                if self.budget.max_cost_usd
                else None,
                max_latency_seconds=180.0,
            ),
            status=Status.READY,
        )

    def _primary(
        self,
        scene: Scene,
        units: list[SemanticUnit],
        entities: list[Entity],
        quantities: list[Quantity],
        style: StyleProfile,
        understanding: Understanding,
        source_table: TableData | None = None,
    ) -> VisualDirective:
        goal = scene.visual_goal
        spec: AnimationSpec | None

        # 0. A table the source document already contained. Its numbers are
        #    exact and attributable to a page, which is strictly better evidence
        #    than quantities recovered from prose — so it outranks them.
        if source_table is not None:
            spec = self._chart_from_table(scene, source_table)
            if spec is not None:
                return self._programmatic(
                    spec,
                    "The source document contains this table. Charting the "
                    "author's own figures invents nothing and can be cited.",
                    confidence=0.95,
                )

        # 1. Numbers the speaker actually said. A chart of stated values is
        #    exact; a generated image of "a lot of people" is an expensive
        #    approximation of something we already know precisely.
        if goal in {VisualGoal.SHOW_QUANTITY, VisualGoal.SHOW_CHANGE_OVER_TIME} and len(quantities) >= 2:
            spec = self._chart(scene, quantities)
            if spec is not None:
                return self._programmatic(
                    spec,
                    "The speaker stated the numbers. A chart is exact where "
                    "generation would only approximate.",
                    confidence=0.92,
                )

        # 2. Dates → a timeline, in the speaker's own phrasing.
        if goal is VisualGoal.SHOW_CHANGE_OVER_TIME:
            spec = self._timeline(scene, entities, units)
            if spec is not None:
                return self._programmatic(
                    spec,
                    "A span of time with stated endpoints. A timeline is the "
                    "exact visual form of that sentence and invents nothing.",
                    confidence=0.88,
                )

        # 3. A comparison is a two-sided shot, drawn.
        if goal is VisualGoal.SHOW_CONTRAST:
            spec = self._comparison(scene, entities, units, understanding)
            if spec is not None:
                return self._programmatic(
                    spec,
                    "The point being made is the difference between two things. "
                    "Holding them side by side states it directly.",
                    confidence=0.9,
                )

        # 4. Structure, process and causation are graphs. Relations were
        #    extracted as data precisely so they could be drawn as edges.
        if goal in {
            VisualGoal.SHOW_STRUCTURE,
            VisualGoal.SHOW_PROCESS,
            VisualGoal.SHOW_CAUSE_EFFECT,
        }:
            spec = self._network(scene, entities, units, understanding)
            if spec is not None:
                return self._programmatic(
                    spec,
                    "The relationships are the meaning here, so they are drawn "
                    "as a diagram rather than described in an image.",
                    confidence=0.85,
                )

        # 5. Places we can actually locate.
        if goal is VisualGoal.SHOW_PLACE:
            spec = self._map(entities)
            if spec is not None:
                return self._programmatic(
                    spec,
                    "The scene is about where something is, and the location is "
                    "one we can place accurately.",
                    confidence=0.87,
                )

        # 6. Real, specific things deserve real photography. Generating a
        #    likeness of a real person or a real historical object invents
        #    history; a licensed photograph does not.
        if goal is VisualGoal.SHOW_ENTITY:
            subject = self._photographable(entities)
            if subject is not None:
                return VisualDirective(
                    strategy=VisualStrategy.LICENSED_MEDIA,
                    requirements=LicensedMediaRequirements(
                        query=subject.search_name,
                        alternate_queries=self._alternate_queries(subject, entities),
                        must_depict=subject.search_name,
                        camera_motion=CameraMotion.KEN_BURNS,
                    ),
                    rationale=(
                        f"{subject.search_name} is a real subject. Photography is "
                        "cheaper and more truthful than an invented likeness."
                    ),
                    estimate=self.estimates[VisualStrategy.LICENSED_MEDIA],
                    confidence=0.8,
                )

        # 7. Abstract closing ideas with no photographic referent. The one place
        #    generation earns its cost — and only when the scene matters.
        if goal is VisualGoal.ILLUSTRATE_ABSTRACT and scene.importance >= 0.6:
            if self.allow_video_generation and self._wants_motion(scene, units):
                return VisualDirective(
                    strategy=VisualStrategy.GENERATED_VIDEO,
                    requirements={  # type: ignore[arg-type]
                        "strategy": "generated_video",
                        "prompt": self._image_prompt(scene, entities, style),
                        "aspect_ratio": style.aspect_ratio.value,
                        "duration_seconds": min(10.0, max(2.0, scene.duration)),
                        "motion_description": "the motion described in the narration",
                        "depicts_reality": False,
                    },
                    rationale=(
                        "The narration describes movement, and movement is what "
                        "carries the meaning. A still cannot do this."
                    ),
                    estimate=self.estimates[VisualStrategy.GENERATED_VIDEO],
                    confidence=0.6,
                )
            return VisualDirective(
                strategy=VisualStrategy.GENERATED_IMAGE,
                requirements=ImageGenerationRequirements(
                    prompt=self._image_prompt(scene, entities, style),
                    negative_prompt="text, watermark, logos, distorted anatomy",
                    aspect_ratio=style.aspect_ratio,
                    camera_motion=CameraMotion.ZOOM_IN,
                    depicts_reality=False,
                ),
                rationale=(
                    "An abstract idea with no specific photographic referent. "
                    "This is where generation buys something real."
                ),
                estimate=self.estimates[VisualStrategy.GENERATED_IMAGE],
                confidence=0.7,
            )

        # 8. Everything else is a statement, and statements are type.
        return self._programmatic(
            self._typography(scene, entities, style),
            "A statement with no separate referent. Well-set moving type "
            "communicates it better than a literal picture of its subject.",
            confidence=0.75,
        )

    # -- fallback ladder --------------------------------------------------

    def _fallbacks(
        self,
        primary: VisualDirective,
        scene: Scene,
        entities: list[Entity],
        style: StyleProfile,
    ) -> list[VisualDirective]:
        """Every rung must be a genuinely different approach, ending in one
        that cannot fail."""
        ladder: list[VisualDirective] = []

        if primary.strategy is VisualStrategy.GENERATED_VIDEO:
            ladder.append(
                VisualDirective(
                    strategy=VisualStrategy.GENERATED_IMAGE,
                    requirements=ImageGenerationRequirements(
                        prompt=self._image_prompt(scene, entities, style),
                        aspect_ratio=style.aspect_ratio,
                        camera_motion=CameraMotion.ZOOM_IN,
                    ),
                    rationale="A still with camera motion carries most of the idea "
                    "at a fraction of the cost and latency.",
                    estimate=self.estimates[VisualStrategy.GENERATED_IMAGE],
                    confidence=0.6,
                )
            )

        if primary.strategy in {
            VisualStrategy.GENERATED_VIDEO,
            VisualStrategy.GENERATED_IMAGE,
        }:
            ladder.append(
                VisualDirective(
                    strategy=VisualStrategy.LICENSED_MEDIA,
                    requirements=LicensedMediaRequirements(
                        query=self._search_query(scene, entities),
                        camera_motion=CameraMotion.KEN_BURNS,
                    ),
                    rationale="A real photograph of the same idea, if one exists "
                    "under an acceptable licence.",
                    estimate=self.estimates[VisualStrategy.LICENSED_MEDIA],
                    confidence=0.55,
                )
            )

        if primary.strategy is VisualStrategy.LICENSED_MEDIA:
            ladder.append(
                VisualDirective(
                    strategy=VisualStrategy.PROGRAMMATIC,
                    requirements=ProgrammaticRequirements(
                        spec=self._typography(scene, entities, style)
                    ),
                    rationale="If nothing correctly licensed exists, state the "
                    "fact as type rather than inventing an image of a real thing.",
                    estimate=self.estimates[VisualStrategy.PROGRAMMATIC],
                    confidence=0.6,
                )
            )
            return ladder

        # Every ladder terminates in typography, which always renders.
        if primary.strategy is not VisualStrategy.PROGRAMMATIC or not isinstance(
            primary.requirements, ProgrammaticRequirements
        ) or primary.requirements.spec.primitive.value != "typography":
            ladder.append(self._typography_directive(scene, entities, style))
        return ladder

    def _typography_directive(
        self, scene: Scene, entities: list[Entity], style: StyleProfile
    ) -> VisualDirective:
        return VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(
                spec=self._typography(scene, entities, style)
            ),
            rationale="Guaranteed terminal fallback: type always renders.",
            estimate=self.estimates[VisualStrategy.PROGRAMMATIC],
            confidence=0.5,
        )

    # -- budget -----------------------------------------------------------

    def _apply_budget(
        self, plans: list[SceneVisualPlan], understanding: Understanding
    ) -> list[SceneVisualPlan]:
        """Demote expensive primaries until the project fits its ceiling.

        Least important scenes are demoted first, which is the whole reason
        scenes carry an importance score. The alternative — refusing to plan, or
        silently overspending — is worse than a slightly plainer supporting shot.
        """
        ceiling = self.budget.max_cost_usd
        if ceiling is None:
            return plans

        def worst_case(current: list[SceneVisualPlan]) -> float:
            return sum(plan.worst_case_estimate.usd for plan in current)

        working = list(plans)
        order = sorted(
            range(len(working)),
            key=lambda i: (
                working[i].primary.estimate.usd,
                -i,
            ),
            reverse=True,
        )
        for index in order:
            if worst_case(working) <= ceiling:
                break
            plan = working[index]
            if plan.primary.estimate.usd <= 0.0 or not plan.fallbacks:
                continue
            demoted = plan.fallbacks[0]
            working[index] = plan.model_copy(
                update={
                    "primary": demoted,
                    "fallbacks": plan.fallbacks[1:],
                }
            )
        return working

    # -- spec builders ----------------------------------------------------

    def _ground(
        self,
        primary: VisualDirective,
        fallbacks: list[VisualDirective],
        scene: Scene,
        evidence: Evidence | None,
    ) -> tuple[VisualDirective, list[VisualDirective]]:
        """Refuse any drawn visual whose claims do not trace to the source.

        Only programmatic visuals are checked, and deliberately so: they are
        the ones that state numbers, dates and places with the authority of a
        diagram. A fetched photograph or a generated image makes no precise
        quantitative claim, and holding it to this standard would refuse every
        illustrative shot in the product.
        """
        if evidence is None:
            return primary, fallbacks

        ladder = [primary, *fallbacks]
        kept: list[VisualDirective] = []
        for directive in ladder:
            spec = getattr(directive.requirements, "spec", None)
            if spec is None:
                kept.append(directive)
                continue
            result = check_spec(spec, evidence)
            if result.is_acceptable:
                kept.append(directive)
                continue
            self.events.emit(
                EventName.GROUNDING_REFUSED,
                project_id=None,
                data={
                    "scene_id": scene.scene_id,
                    "primitive": spec.primitive.value,
                    "reason": result.reason(),
                    "unsupported": list(result.unsupported[:6]),
                },
            )

        if not kept:
            # Everything was refused. Rather than draw nothing, fall back to
            # setting the narration itself — which is, by construction, exactly
            # what the source says.
            safe = self._typography_from_narration(scene)
            return safe, []
        return kept[0], kept[1:]

    def _typography_from_narration(self, scene: Scene) -> VisualDirective:
        """The one visual that can never be ungrounded: the speaker's words."""
        headline = " ".join(scene.narration.split())[:120] or "…"
        return self._programmatic(
            TypographySpec(
                headline=headline,
                preferred_duration=max(2.0, min(scene.duration, 10.0)),
            ),
            "Every richer visual made a claim the source did not support, so "
            "the narration is set as type instead — it cannot misstate itself.",
            confidence=0.6,
        )

    def _programmatic(
        self, spec: AnimationSpec, rationale: str, *, confidence: Confidence
    ) -> VisualDirective:
        return VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(spec=spec),
            rationale=rationale,
            estimate=self.estimates[VisualStrategy.PROGRAMMATIC],
            confidence=confidence,
        )

    @staticmethod
    def _chart(scene: Scene, quantities: list[Quantity]) -> ChartSpec | None:
        points: list[DataPoint] = []
        for index, quantity in enumerate(quantities[:8]):
            label = _short_label(quantity) or f"value {index + 1}"
            points.append(DataPoint(label=label, value=quantity.value))
        if len(points) < 2:
            return None
        units = {q.unit for q in quantities if q.unit}
        # Two values of the same thing read as a change; more read as a series.
        kind = ChartKind.COLUMN if len(points) <= 4 else ChartKind.LINE
        return ChartSpec(
            kind=kind,
            title=None,
            y_label=next(iter(units)) if len(units) == 1 else None,
            series=[ChartSeries(name=scene.visual_goal.value, points=points)],
            preferred_duration=max(2.0, min(scene.duration, 12.0)),
            emphasis=Emphasis.STRONG if scene.importance > 0.7 else Emphasis.NORMAL,
        )

    @staticmethod
    def _chart_from_table(scene: Scene, table: TableData) -> ChartSpec | None:
        """A chart built from a table in the source, or nothing.

        Returns ``None`` rather than guessing whenever the table has no column
        that is unambiguously numeric. A chart of misread values is worse than
        no chart, because it looks authoritative.
        """
        series = chart_series_from_table(table)
        if series is None:
            return None
        labels, values, name = series
        points = [
            DataPoint(label=(label or f"value {index + 1}")[:60], value=value)
            for index, (label, value) in enumerate(zip(labels, values, strict=False))
        ][:12]
        if len(points) < 2:
            return None
        return ChartSpec(
            kind=ChartKind.COLUMN if len(points) <= 5 else ChartKind.LINE,
            title=(table.caption or None) and str(table.caption)[:120],
            y_label=name[:64] or None,
            series=[ChartSeries(name=name[:64] or "series", points=points)],
            preferred_duration=max(2.5, min(scene.duration, 12.0)),
            emphasis=Emphasis.STRONG if scene.importance > 0.7 else Emphasis.NORMAL,
        )

    @staticmethod
    def _timeline(
        scene: Scene, entities: list[Entity], units: list[SemanticUnit]
    ) -> TimelineSpec | None:
        dated = [e for e in entities if e.type is EntityType.DATE and e.name.isdigit()]
        events: list[TimelineEvent] = []
        for entity in dated:
            year = int(entity.name)
            label = _sentence_containing(units, entity.name) or scene.visual_brief
            events.append(
                TimelineEvent(
                    label=_trim(label, 110),
                    when=entity.name,  # verbatim: never invent precision
                    sort_value=float(year),
                )
            )
        if not events:
            return None
        events.sort(key=lambda event: event.sort_value)
        # Deduplicate identical years, keeping the first label.
        unique: list[TimelineEvent] = []
        seen: set[float] = set()
        for event in events:
            if event.sort_value in seen:
                continue
            seen.add(event.sort_value)
            unique.append(event)
        return TimelineSpec(
            events=unique[:12],
            preferred_duration=max(2.0, min(scene.duration, 12.0)),
            emphasis=Emphasis.NORMAL,
        )

    @staticmethod
    def _comparison(
        scene: Scene,
        entities: list[Entity],
        units: list[SemanticUnit],
        understanding: Understanding,
    ) -> ComparisonSpec | None:
        """Two sides, each titled by whatever the speaker was contrasting.

        The hard case is the ordinary one: "it was smaller than the vacuum
        tubes". One side names a subject and the other is a pronoun. Rather than
        titling a panel "this", the unnamed side takes the recording's topic —
        which is what the pronoun refers to, and is almost always right.
        """
        source = next(
            (u for u in units if u.intent is SemanticIntent.COMPARISON), None
        )
        if source is None:
            return None
        halves = _COMPARISON_SPLIT.split(source.text, maxsplit=1)
        if len(halves) != 2:
            return None

        named = [
            entity
            for entity in entities
            if entity.type
            in {
                EntityType.CONCEPT,
                EntityType.TECHNOLOGY,
                EntityType.PRODUCT,
                EntityType.OTHER,
                EntityType.PERSON,
                EntityType.ORGANIZATION,
            }
        ]

        def title_for(half: str, taken: set[str]) -> str | None:
            lowered = half.lower()
            for entity in named:
                name = entity.search_name
                if name.lower() in lowered and name not in taken:
                    return name
            return None

        taken: set[str] = set()
        right_title = title_for(halves[1], taken)
        if right_title:
            taken.add(right_title)
        left_title = title_for(halves[0], taken)

        # The pronoun side inherits the subject of the recording.
        topic = (understanding.topic or "").strip()
        if not left_title:
            left_title = topic or "this"
        if not right_title:
            right_title = topic if topic and topic != left_title else "before"
        if left_title == right_title:
            return None

        left_points = [_trim(_strip_leading_pronoun(halves[0]), 90)]
        right_points = [_trim(halves[1], 90)]

        return ComparisonSpec(
            left=ComparisonSide(
                title=_trim(left_title, 60).capitalize(),
                points=[p for p in left_points if p][:4],
            ),
            right=ComparisonSide(
                title=_trim(right_title, 60).capitalize(),
                points=[p for p in right_points if p][:4],
            ),
            connector="versus",
            preferred_duration=max(2.0, min(scene.duration, 16.0)),
            emphasis=Emphasis.STRONG if scene.importance > 0.7 else Emphasis.NORMAL,
        )

    @staticmethod
    def _network(
        scene: Scene,
        entities: list[Entity],
        units: list[SemanticUnit],
        understanding: Understanding,
    ) -> NetworkSpec | None:
        if len(entities) < 2:
            return None
        nodes = [
            NetworkNode(key=f"n{index}", label=_trim(entity.search_name, 60))
            for index, entity in enumerate(entities[:8])
        ]
        index_by_entity = {
            entity.entity_id: f"n{index}" for index, entity in enumerate(entities[:8])
        }
        edges: list[NetworkEdge] = []
        # Real relations first: these are the edges the semantic layer found.
        for unit in units:
            for relation_id in unit.relation_ids:
                relation = understanding.relation_by_id(relation_id)
                if relation is None:
                    continue
                source = index_by_entity.get(relation.subject_id)
                target = index_by_entity.get(relation.object_id)
                if source and target and source != target:
                    edges.append(
                        NetworkEdge(
                            source=source,
                            target=target,
                            label=relation.predicate.replace("_", " "),
                        )
                    )
        if not edges:
            # No extracted relation: chain the subjects in the order they were
            # spoken. A sequence is a weaker claim than a labelled relation, so
            # the edges carry no label — we are not asserting *how* they relate.
            edges = [
                NetworkEdge(source=f"n{i}", target=f"n{i + 1}", label=None)
                for i in range(len(nodes) - 1)
            ]
        return NetworkSpec(
            nodes=nodes,
            edges=edges[:24],
            layout="layered" if scene.visual_goal is VisualGoal.SHOW_PROCESS else "force",
            animation="propagate"
            if scene.visual_goal is VisualGoal.SHOW_CAUSE_EFFECT
            else "build",
            preferred_duration=max(2.0, min(scene.duration, 14.0)),
        )

    @staticmethod
    def _map(entities: list[Entity]) -> MapSpec | None:
        markers: list[MapMarker] = []
        scopes: list[str] = []
        for entity in entities:
            located = gazetteer.lookup(entity.search_name)
            if located is None:
                continue
            latitude, longitude, scope = located
            markers.append(
                MapMarker(
                    label=_trim(entity.search_name, 60),
                    latitude=latitude,
                    longitude=longitude,
                )
            )
            scopes.append(scope)
        if not markers:
            return None
        return MapSpec(
            markers=markers[:12],
            scope=gazetteer.widest_scope(scopes),  # type: ignore[arg-type]
            connect_markers=len(markers) > 1,
        )

    @staticmethod
    def _typography(
        scene: Scene, entities: list[Entity], style: StyleProfile
    ) -> TypographySpec:
        headline = _headline(scene.narration)
        highlight = [
            entity.search_name
            for entity in entities[:3]
            if entity.search_name.lower() in headline.lower()
        ]
        subline = None
        if len(scene.narration) > len(headline) + 12:
            subline = _trim(scene.narration[len(headline) :].strip(" .,"), 160) or None
        return TypographySpec(
            headline=headline,
            subline=subline,
            highlight=highlight[:8],
            reveal="word_by_word",
            preferred_duration=max(1.5, min(scene.duration, 14.0)),
            emphasis=(
                Emphasis.STRONG
                if scene.purpose in {ScenePurpose.OPENING, ScenePurpose.CONCLUSION}
                else Emphasis.NORMAL
            ),
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _photographable(entities: list[Entity]) -> Entity | None:
        ranked = [
            entity
            for entity in entities
            if entity.type
            in {
                EntityType.PERSON,
                EntityType.ORGANIZATION,
                EntityType.PRODUCT,
                EntityType.LOCATION,
                EntityType.WORK,
                EntityType.EVENT,
            }
        ]
        return max(ranked, key=lambda e: e.salience) if ranked else None

    @staticmethod
    def _alternate_queries(subject: Entity, entities: list[Entity]) -> list[str]:
        """Cheap insurance against one unlucky search."""
        others = [
            entity.search_name
            for entity in entities
            if entity.entity_id != subject.entity_id and entity.type is not EntityType.DATE
        ]
        alternates = [f"{subject.search_name} {other}" for other in others[:2]]
        dates = [e.name for e in entities if e.type is EntityType.DATE]
        if dates:
            alternates.append(f"{subject.search_name} {dates[0]}")
        return alternates[:4]

    @staticmethod
    def _search_query(scene: Scene, entities: list[Entity]) -> str:
        if entities:
            return " ".join(entity.search_name for entity in entities[:2])[:300]
        return _trim(scene.visual_brief, 120)

    @staticmethod
    def _image_prompt(
        scene: Scene, entities: list[Entity], style: StyleProfile
    ) -> str:
        subject = (
            ", ".join(entity.search_name for entity in entities[:3])
            or _trim(scene.narration, 120)
        )
        return (
            f"{subject}. {_trim(scene.visual_brief, 160)} "
            f"{style.as_prompt_fragment()}, no text, no watermark"
        )[:2000]

    @staticmethod
    def _wants_motion(scene: Scene, units: list[SemanticUnit]) -> bool:
        text = " ".join(unit.text for unit in units) or scene.narration
        return bool(MOTION_WORDS.search(text)) and scene.importance >= 0.7


def _short_label(quantity: Quantity) -> str | None:
    parts = [quantity.at, quantity.of_what]
    for part in parts:
        if part:
            words = part.split()
            if words:
                return _trim(" ".join(words[:4]), 40)
    if quantity.unit:
        return _trim(quantity.unit, 40)
    return None


def _sentence_containing(units: list[SemanticUnit], needle: str) -> str | None:
    for unit in units:
        if needle in unit.text:
            return unit.proposition
    return None


_LEADING_PRONOUN = re.compile(r"^(?:it|they|this|that|these|those|he|she)\s+(?:was|were|is|are|had|has)\s+", re.I)


def _strip_leading_pronoun(text: str) -> str:
    """"It was smaller and far more efficient" becomes "smaller and far more
    efficient" — the panel title already says what "it" is."""
    return _LEADING_PRONOUN.sub("", text.strip()).strip() or text.strip()


def _headline(text: str, limit: int = 110) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0]
    return cut or cleaned[:limit]


def _trim(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rsplit(" ", 1)[0] or cleaned[:limit]


# ---------------------------------------------------------------------------
# Model-backed director
# ---------------------------------------------------------------------------

_DIRECTOR_INSTRUCTION = """\
You are the Visual Director of a voice-to-video system.

For each scene you are given, decide how the idea should be shown, and return a
`visual_plan` document that validates against the schema you have been given.

Principles, in order:
1. If the speaker stated numbers, dates or relationships, draw them. A chart or
   timeline of stated values is exact; a generated image only approximates.
2. If the subject is a real person, place, organisation or object, prefer
   licensed photography over generating a likeness.
3. Use image generation only for ideas with no photographic referent.
4. Use video generation only when motion itself carries the meaning.
5. Every scene needs a fallback ladder ending in a typography shot.
6. Never assert anything the speaker did not say. Dates are quoted verbatim.

Return only JSON.
"""


@dataclass
class LlmVisualDirector:
    """Visual direction via a text generation provider, with a rules fallback."""

    provider: object  # TextGenerationProvider
    events: EventSink
    fallback: VisualDirector | None = None
    version: str = LLM_DIRECTOR_VERSION

    async def direct(
        self,
        *,
        scene_graph: SceneGraph,
        understanding: Understanding,
        context: DocumentContext | None = None,
    ) -> VisualPlan:
        request = GenerationRequest(
            project_id=scene_graph.project_id,
            kind=GenerationKind.TEXT,
            params=TextParams(
                instruction=_DIRECTOR_INSTRUCTION,
                input_json={
                    "style": scene_graph.style.model_dump(mode="json"),
                    "narrative": scene_graph.narrative.model_dump(mode="json"),
                    "scenes": [
                        {
                            "scene_id": scene.scene_id,
                            "start": scene.span.start,
                            "end": scene.span.end,
                            "narration": scene.narration,
                            "purpose": scene.purpose.value,
                            "visual_goal": scene.visual_goal.value,
                            "visual_brief": scene.visual_brief,
                            "importance": scene.importance,
                            "entities": [
                                {"name": e.search_name, "type": e.type.value}
                                for e in (
                                    understanding.entity_by_id(i)
                                    for i in scene.entity_ids
                                )
                                if e is not None
                            ],
                        }
                        for scene in scene_graph.scenes
                    ],
                },
                response_schema="visual_plan",
                temperature=0.3,
                max_output_tokens=16_000,
            ),
        )

        try:
            result = await self.provider.generate(request)  # type: ignore[attr-defined]
            if result.status is not Status.READY or not result.structured_output:
                raise ValidationFailed("visual director returned no output")
            payload = dict(result.structured_output)
            payload["project_id"] = scene_graph.project_id
            payload["scene_graph_id"] = scene_graph.scene_graph_id
            payload["status"] = Status.READY.value
            payload.pop("visual_plan_id", None)
            payload.pop("schema_version", None)
            plan = VisualPlan.model_validate(payload)
            self._require_every_scene(plan, scene_graph)
        except VTVError:
            if self.fallback is None:
                raise
            return await self.fallback.direct(
                scene_graph=scene_graph,
                understanding=understanding,
                context=context,
            )

        self.events.emit(
            EventName.VISUAL_PLAN_CREATED,
            project_id=scene_graph.project_id,
            cost_usd=result.cost_usd,
            data={
                "director": self.version,
                "provider": result.provider,
                "scenes": len(plan.scene_plans),
                "strategy_mix": plan.strategy_mix(),
            },
        )
        return plan

    @staticmethod
    def _require_every_scene(plan: VisualPlan, scene_graph: SceneGraph) -> None:
        planned = {scene_plan.scene_id for scene_plan in plan.scene_plans}
        missing = [
            scene.scene_id for scene in scene_graph.scenes if scene.scene_id not in planned
        ]
        if missing:
            raise ValidationFailed(
                f"visual plan omitted {len(missing)} scene(s); a scene with no "
                "plan would render as a hole"
            )



#: A table is only attached to a scene when the overlap in significant words is
#: at least this strong. Below it, the table is more likely about something
#: else on the page, and a chart of unrelated numbers beside unrelated narration
#: is worse than no chart.
TABLE_MATCH_THRESHOLD = 0.34


def _match_tables_to_scenes(
    scene_graph: SceneGraph, context: DocumentContext | None
) -> dict[str, TableData]:
    """Decide which scene, if any, each source table illustrates.

    Matching is on shared significant words between the narration and the
    table's caption, headers and row labels. It is deliberately conservative:
    each table goes to at most one scene, each scene takes at most one table,
    and anything below the threshold is dropped rather than placed somewhere
    plausible-looking.
    """
    if context is None or not context.chartable_tables:
        return {}

    scored: list[tuple[float, str, TableData]] = []
    for table in context.chartable_tables:
        table_words = _significant_words(
            " ".join(
                [table.caption or ""]
                + list(table.headers)
                + [row[0] for row in table.rows[:20] if row]
            )
        )
        if not table_words:
            continue
        for scene in scene_graph.scenes:
            scene_words = _significant_words(scene.narration)
            if not scene_words:
                continue
            overlap = len(table_words & scene_words) / len(table_words)
            if overlap >= TABLE_MATCH_THRESHOLD:
                scored.append((overlap, scene.scene_id, table))

    scored.sort(key=lambda item: item[0], reverse=True)
    chosen: dict[str, TableData] = {}
    used: set[int] = set()
    for _, scene_id, table in scored:
        if scene_id in chosen or id(table) in used:
            continue
        chosen[scene_id] = table
        used.add(id(table))
    return chosen


#: Words too common to indicate that two passages are about the same thing.
_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from", "had", "has", "have", "in", "into", "is", "it", "its", "of", "on", "or", "that", "the", "their", "there", "these", "this", "to", "was", "were", "which", "will", "with", "we", "you", "our", "your", "they", "he", "she"]
)


def _significant_words(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


__all__ = [
    "DEFAULT_ESTIMATES",
    "DIRECTOR_VERSION",
    "LLM_DIRECTOR_VERSION",
    "LlmVisualDirector",
    "RuleBasedVisualDirector",
    "VisualDirector",
]
