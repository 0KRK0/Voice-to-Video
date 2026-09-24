"""The Visual Director's output contract, and the visual language it speaks."""

from __future__ import annotations

import unittest

from pydantic import TypeAdapter, ValidationError

from vtv.contracts import (
    ChartKind,
    ChartSeries,
    ChartSpec,
    CompositeLayer,
    CompositeRequirements,
    CostEstimate,
    DataPoint,
    ExistingAssetRequirements,
    ImageGenerationRequirements,
    LicensedMediaRequirements,
    NetworkEdge,
    NetworkNode,
    NetworkSpec,
    ProgrammaticRequirements,
    SceneVisualPlan,
    TimelineEvent,
    TimelineSpec,
    TypographySpec,
    VisualDirective,
    VisualPlan,
    VisualRequirements,
    VisualStrategy,
)
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID

SCENE_ID = "scn_aaaaaaaaaaaaaaaaaaaaaaaa"


def typography(headline: str = "A headline") -> VisualDirective:
    return VisualDirective(
        strategy=VisualStrategy.PROGRAMMATIC,
        requirements=ProgrammaticRequirements(spec=TypographySpec(headline=headline)),
        rationale="type always renders",
    )


class DirectivesAreInternallyConsistent(unittest.TestCase):
    def test_strategy_and_requirements_must_agree(self) -> None:
        with self.assertRaises(ValidationError):
            VisualDirective(
                strategy=VisualStrategy.GENERATED_VIDEO,
                requirements=ProgrammaticRequirements(
                    spec=TypographySpec(headline="mismatch")
                ),
                rationale="this should not be constructible",
            )

    def test_a_rationale_is_mandatory(self) -> None:
        # A decision without a reason cannot be reviewed, evaluated or improved.
        with self.assertRaises(ValidationError):
            VisualDirective(
                strategy=VisualStrategy.PROGRAMMATIC,
                requirements=ProgrammaticRequirements(
                    spec=TypographySpec(headline="x")
                ),
                rationale="",
            )


class RequirementsDeserialiseByDiscriminator(unittest.TestCase):
    """What arrives from a language model is JSON. It must land in the right type."""

    def test_each_strategy_round_trips(self) -> None:
        adapter = TypeAdapter(VisualRequirements)
        cases: list[object] = [
            ExistingAssetRequirements(query="a photograph"),
            ProgrammaticRequirements(spec=TypographySpec(headline="hello")),
            LicensedMediaRequirements(query="bell labs 1947"),
            ImageGenerationRequirements(prompt="a silicon die, macro"),
        ]
        for original in cases:
            payload = original.model_dump(mode="json")  # type: ignore[attr-defined]
            restored = adapter.validate_python(payload)
            self.assertEqual(type(restored), type(original))

    def test_an_unknown_strategy_is_rejected(self) -> None:
        adapter = TypeAdapter(VisualRequirements)
        with self.assertRaises(ValidationError):
            adapter.validate_python({"strategy": "vibes", "prompt": "something"})

    def test_a_hallucinated_field_is_rejected(self) -> None:
        adapter = TypeAdapter(VisualRequirements)
        with self.assertRaises(ValidationError):
            adapter.validate_python(
                {
                    "strategy": "generated_image",
                    "prompt": "a silicon die, macro",
                    "cinematography": "anamorphic",
                }
            )


class FallbackLaddersDegradeGracefully(unittest.TestCase):
    def test_worst_case_sums_the_whole_ladder(self) -> None:
        expensive = VisualDirective(
            strategy=VisualStrategy.GENERATED_IMAGE,
            requirements=ImageGenerationRequirements(prompt="an abstract idea"),
            rationale="no photographic referent exists",
            estimate=CostEstimate(usd=0.04, latency_seconds=12.0),
        )
        cheap = VisualDirective(
            strategy=VisualStrategy.LICENSED_MEDIA,
            requirements=LicensedMediaRequirements(query="silicon wafer"),
            rationale="a real photograph carries the same idea",
            estimate=CostEstimate(usd=0.0, latency_seconds=1.5),
        )
        plan = SceneVisualPlan(
            scene_id=SCENE_ID, primary=expensive, fallbacks=[cheap, typography()]
        )
        self.assertEqual(len(plan.ladder), 3)
        self.assertAlmostEqual(plan.worst_case_estimate.usd, 0.04)
        self.assertAlmostEqual(plan.worst_case_estimate.latency_seconds, 13.5)

    def test_a_fallback_must_offer_a_different_approach(self) -> None:
        generated = VisualDirective(
            strategy=VisualStrategy.GENERATED_IMAGE,
            requirements=ImageGenerationRequirements(prompt="something abstract"),
            rationale="first attempt",
        )
        with self.assertRaises(ValidationError):
            SceneVisualPlan(
                scene_id=SCENE_ID,
                primary=generated,
                fallbacks=[generated.model_copy()],
            )

    def test_repeating_a_cheap_strategy_is_allowed(self) -> None:
        # Two different drawings are a legitimate ladder; two attempts at the
        # same expensive generation are not.
        plan = SceneVisualPlan(
            scene_id=SCENE_ID,
            primary=typography("first"),
            fallbacks=[typography("simpler")],
        )
        self.assertEqual(len(plan.fallbacks), 1)


class CompositesAreBounded(unittest.TestCase):
    def test_a_composite_may_not_contain_a_composite(self) -> None:
        with self.assertRaises(ValidationError):
            CompositeRequirements(
                layers=[
                    CompositeLayer(
                        requirements={  # type: ignore[arg-type]
                            "strategy": "composite",
                            "layers": [],
                        }
                    ),
                    CompositeLayer(
                        requirements=ProgrammaticRequirements(
                            spec=TypographySpec(headline="x")
                        )
                    ),
                ]
            )

    def test_layers_must_fit_inside_the_frame(self) -> None:
        with self.assertRaises(ValidationError):
            CompositeLayer(
                requirements=ProgrammaticRequirements(
                    spec=TypographySpec(headline="x")
                ),
                bounds=(0.6, 0.0, 0.8, 1.0),
            )


class TheVisualLanguageValidatesItself(unittest.TestCase):
    def test_a_pie_chart_takes_one_series(self) -> None:
        two = [
            ChartSeries(name="a", points=[DataPoint(label="x", value=1)]),
            ChartSeries(name="b", points=[DataPoint(label="x", value=2)]),
        ]
        with self.assertRaises(ValidationError):
            ChartSpec(kind=ChartKind.PIE, series=two)
        self.assertIsNotNone(ChartSpec(kind=ChartKind.LINE, series=two))

    def test_timeline_events_must_be_chronological(self) -> None:
        with self.assertRaises(ValidationError):
            TimelineSpec(
                events=[
                    TimelineEvent(label="later", when="1967", sort_value=1967),
                    TimelineEvent(label="earlier", when="1947", sort_value=1947),
                ]
            )

    def test_network_edges_must_reference_declared_nodes(self) -> None:
        with self.assertRaises(ValidationError):
            NetworkSpec(
                nodes=[
                    NetworkNode(key="a", label="A"),
                    NetworkNode(key="b", label="B"),
                ],
                edges=[NetworkEdge(source="a", target="ghost")],
            )

    def test_specifications_carry_no_executable_content(self) -> None:
        # A model may choose and parameterise a visual. It may never hand us
        # something we would run or inject.
        forbidden = {"code", "html", "svg", "script", "javascript", "css", "template"}
        for spec_type in (TypographySpec, ChartSpec, TimelineSpec, NetworkSpec):
            fields = set(spec_type.model_fields)
            self.assertEqual(
                fields & forbidden,
                set(),
                f"{spec_type.__name__} exposes an executable-content field",
            )


class PlanLevelEconomics(unittest.TestCase):
    def test_strategy_mix_reveals_expensive_habits(self) -> None:
        plan = VisualPlan(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
            scene_graph_id="sgr_aaaaaaaaaaaaaaaaaaaaaaaa",
            scene_plans=[
                SceneVisualPlan(scene_id="scn_aaaaaaaaaaaaaaaaaaaaaaaa", primary=typography()),
                SceneVisualPlan(scene_id="scn_bbbbbbbbbbbbbbbbbbbbbbbb", primary=typography()),
                SceneVisualPlan(
                    scene_id="scn_cccccccccccccccccccccccc",
                    primary=VisualDirective(
                        strategy=VisualStrategy.GENERATED_VIDEO,
                        requirements={  # type: ignore[arg-type]
                            "strategy": "generated_video",
                            "prompt": "a city on mars, floating",
                        },
                        rationale="motion itself carries the meaning here",
                        estimate=CostEstimate(usd=1.80, latency_seconds=180),
                    ),
                ),
            ],
        )
        self.assertEqual(
            plan.strategy_mix(), {"programmatic": 2, "generated_video": 1}
        )
        self.assertAlmostEqual(plan.expected_cost_usd, 1.80)

    def test_one_plan_per_scene(self) -> None:
        with self.assertRaises(ValidationError):
            VisualPlan(
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
                scene_graph_id="sgr_aaaaaaaaaaaaaaaaaaaaaaaa",
                scene_plans=[
                    SceneVisualPlan(scene_id=SCENE_ID, primary=typography()),
                    SceneVisualPlan(scene_id=SCENE_ID, primary=typography("again")),
                ],
            )


if __name__ == "__main__":
    unittest.main()
