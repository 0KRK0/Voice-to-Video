"""P1-1 — the Visual Bible changes the video.

The audit's finding was blunt: the Bible was built, reviewed, attached to the
pipeline result, and then **never consulted again**. Composition searched for a
fresh asset on every appearance of the same entity, and the renderer picked
colours off a categorical wheel. So Stage 23 — "visual consistency" — was true
of a data structure and false of every frame the customer saw.

Worse, it was not persisted, so a user who *locked* a binding ("keep this one")
lost that decision on the next render. A control that does not survive a
re-render is not a control; it is a button that lies.

These tests assert the three things that make the feature real:

1. a bound entity reuses **the same stored object**, not a fresh search
2. a locked colour reaches the pixels
3. an approved Bible survives a re-render, including its locks

Each is written against the seam the audit says matters — composition, the
Timeline, the renderer — rather than against `ConsistencyEngine`, which was
always correct and was never the problem.
"""

from __future__ import annotations

import asyncio
import unittest

from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.animation.theme import Theme
from vtv.contracts.base import IdPrefix, ObjectRef, TimeSpan, new_id
from vtv.contracts.consistency import (
    BindingKind,
    BindingSource,
    EntityBinding,
    VisualBible,
)
from vtv.contracts.scene import (
    Continuity,
    Scene,
    SceneGraph,
    ScenePurpose,
    VisualGoal,
)
from vtv.contracts.style import AspectRatio, StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.timeline import AssetClipSource, NarrationTrack, Timeline
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.contracts.visual_language import ChartKind, ChartSeries, ChartSpec, DataPoint
from vtv.contracts.visual_plan import (
    LicensedMediaRequirements,
    SceneVisualPlan,
    VisualDirective,
    VisualPlan,
    VisualStrategy,
)
from vtv.observability.events import EventSink
from vtv.pipeline.composition import SceneComposer

PROJECT = new_id(IdPrefix.PROJECT)
BOUND_OBJECT = ObjectRef(
    bucket="vtv-media",
    key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/{PROJECT}/assets/bell-labs.jpg",
    content_type="image/jpeg",
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def colours(image) -> set[tuple[int, int, int]]:  # type: ignore[no-untyped-def]
    """Every distinct RGB value in a frame."""
    counted = image.convert("RGB").getcolors(1 << 20) or []
    return {value for _count, value in counted}


class CountingResolver:
    """Records how often it was asked and finds nothing.

    ``None`` is a normal outcome for an asset search — the caller descends its
    ladder — so this exercises the real degradation path rather than raising
    something composition would have to special-case.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, **_: object) -> None:
        self.calls += 1
        return None


def scene_graph(names: list[str]) -> SceneGraph:
    scenes = [
        Scene(
            index=index,
            span=TimeSpan.of(index * 5.0, index * 5.0 + 5.0),
            narration=f"A sentence about {name}.",
            purpose=ScenePurpose.EXPLANATION,
            visual_goal=VisualGoal.SHOW_ENTITY,
            visual_brief=f"show {name}",
            continuity=Continuity(),
        )
        for index, name in enumerate(names)
    ]
    return SceneGraph(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=PROJECT,
        understanding_id=new_id(IdPrefix.ENTITY),
        transcript_id=new_id(IdPrefix.TRANSCRIPT),
        scenes=scenes,
        style=StyleProfile(),
    )


def plan_for(graph: SceneGraph, query: str) -> VisualPlan:
    return VisualPlan(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=PROJECT,
        scene_graph_id=graph.scene_graph_id,
        scene_plans=[
            SceneVisualPlan(
                scene_id=scene.scene_id,
                primary=VisualDirective(
                    strategy=VisualStrategy.LICENSED_MEDIA,
                    requirements=LicensedMediaRequirements(query=query),
                    rationale="the subject is a real, photographable thing",
                ),
                # No fallbacks. The ladder must not rescue a failed lookup here,
                # or "the resolver was never called" would be indistinguishable
                # from "the resolver failed and typography covered for it".
                fallbacks=[],
            )
            for scene in graph.scenes
        ],
    )


def transcript_of(graph: SceneGraph) -> Transcript:
    return Transcript(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=PROJECT,
        recording_id=new_id(IdPrefix.RECORDING),
        language="en",
        segments=[
            TranscriptSegment(span=scene.span, text=scene.narration)
            for scene in graph.scenes
        ],
        provider="test",
        model="test",
    )


def bible_binding_an_asset(name: str) -> VisualBible:
    bible = VisualBible(
        organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT
    )
    bible.bind(
        EntityBinding(
            canonical_name=name,
            kind=BindingKind.ASSET,
            asset=BOUND_OBJECT,
            note="Bell Labs, 1947",
        )
    )
    return bible


class ABoundEntityReusesItsObject(unittest.TestCase):
    """The half of consistency that no amount of searching can provide.

    Two searches for "Bell Labs" return two different photographs. The viewer
    reads that as two different places.
    """

    def setUp(self) -> None:
        self.graph = scene_graph(["Bell Labs", "Bell Labs", "Bell Labs"])
        self.plan = plan_for(self.graph, "Bell Labs")
        self.resolver = CountingResolver()
        self.composer = SceneComposer(
            storage=object(),
            events=EventSink(),
            asset_resolver=self.resolver,
        )

    def compose(self, bible: VisualBible | None):  # type: ignore[no-untyped-def]
        return run(
            self.composer.compose(
                scene_graph=self.graph,
                visual_plan=self.plan,
                transcript=transcript_of(self.graph),
                narration=NarrationTrack(audio=BOUND_OBJECT, duration_seconds=15.0),
                visual_bible=bible,
            )
        )

    def test_every_appearance_uses_the_identical_object(self) -> None:
        result = self.compose(bible_binding_an_asset("Bell Labs"))
        keys = {
            clip.source.object.key
            for clip in result.timeline.clips
            if isinstance(clip.source, AssetClipSource)
        }
        self.assertEqual(keys, {BOUND_OBJECT.key})
        self.assertEqual(len(result.timeline.clips), 3)

    def test_the_resolver_is_not_consulted_at_all(self) -> None:
        """No fetch, no second licence check, no second payment."""
        self.compose(bible_binding_an_asset("Bell Labs"))
        self.assertEqual(self.resolver.calls, 0)

    def test_an_alias_binds_as_well_as_the_canonical_name(self) -> None:
        bible = VisualBible(
            organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT
        )
        bible.bind(
            EntityBinding(
                canonical_name="Bell Telephone Laboratories",
                aliases=["Bell Labs"],
                kind=BindingKind.ASSET,
                asset=BOUND_OBJECT,
            )
        )
        result = self.compose(bible)
        self.assertTrue(
            all(
                isinstance(clip.source, AssetClipSource)
                for clip in result.timeline.clips
            )
        )

    def test_use_is_recorded_against_the_binding(self) -> None:
        """So continuity can be audited, and the storyboard can show coverage."""
        bible = bible_binding_an_asset("Bell Labs")
        self.compose(bible)
        binding = bible.binding_for("Bell Labs", kind=BindingKind.ASSET)
        assert binding is not None
        self.assertEqual(len(binding.used_in_scenes), 3)

    def test_a_binding_with_no_object_still_searches(self) -> None:
        """A colour lock is not an asset. Inventing one would be worse."""
        bible = VisualBible(
            organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT
        )
        bible.bind(
            EntityBinding(
                canonical_name="Bell Labs",
                kind=BindingKind.COLOUR,
                colour="#3366cc",
            )
        )
        result = self.compose(bible)
        self.assertEqual(self.resolver.calls, 3)
        self.assertTrue(result.timeline.clips)

    def test_without_a_bible_nothing_changes(self) -> None:
        """The feature is additive: no Bible, no new behaviour."""
        self.compose(None)
        self.assertEqual(self.resolver.calls, 3)


class TheColourLockReachesThePixels(unittest.TestCase):
    """The audit's `_persist` finding, followed all the way to the frame."""

    def test_the_timeline_carries_the_locked_palette(self) -> None:
        graph = scene_graph(["Mars"])
        bible = VisualBible(
            organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT
        )
        bible.bind(
            EntityBinding(
                canonical_name="Mars",
                kind=BindingKind.COLOUR,
                colour="#c1440e",
            )
        )
        composer = SceneComposer(storage=object(), events=EventSink())
        result = run(
            composer.compose(
                scene_graph=graph,
                visual_plan=VisualPlan(
                    organisation_id=SYSTEM_ORGANISATION_ID,
                    project_id=PROJECT,
                    scene_graph_id=graph.scene_graph_id,
                    scene_plans=[],
                ),
                transcript=transcript_of(graph),
                narration=NarrationTrack(audio=BOUND_OBJECT, duration_seconds=5.0),
                visual_bible=bible,
            )
        )
        self.assertEqual(result.timeline.entity_colours, {"mars": "#c1440e"})

    def test_a_theme_resolves_a_locked_name_to_its_colour(self) -> None:
        theme = Theme.from_style(
            StyleProfile(),
            width=640,
            height=360,
            entity_colours={"Mars": "#c1440e"},
        )
        fallback = theme.series_colour(0)
        self.assertEqual(theme.colour_for("mars", fallback)[:3], (0xC1, 0x44, 0x0E))
        self.assertEqual(theme.colour_for("MARS", fallback)[:3], (0xC1, 0x44, 0x0E))

    def test_an_unlocked_name_keeps_the_categorical_colour(self) -> None:
        """Most series are not entities. "Revenue" must stay itself."""
        theme = Theme.from_style(
            StyleProfile(), width=640, height=360, entity_colours={"Mars": "#c1440e"}
        )
        fallback = theme.series_colour(1)
        self.assertEqual(theme.colour_for("Revenue", fallback), fallback)

    def test_no_locks_means_exactly_the_previous_behaviour(self) -> None:
        theme = Theme.from_style(StyleProfile(), width=640, height=360)
        fallback = theme.series_colour(0)
        self.assertEqual(theme.colour_for("Mars", fallback), fallback)

    def test_the_drawn_frame_actually_contains_the_locked_colour(self) -> None:
        """The end of the chain. Everything above is plumbing until this holds."""
        spec = ChartSpec(
            kind=ChartKind.COLUMN,
            series=[
                ChartSeries(
                    name="Mars",
                    points=[DataPoint(label="2019", value=10.0),
                            DataPoint(label="2024", value=40.0)],
                )
            ],
        )
        size = RenderSize.for_aspect(AspectRatio.LANDSCAPE_16_9, scale=0.25)

        locked = AnimationEngine(
            StyleProfile(), entity_colours={"Mars": "#c1440e"}
        ).still(spec, size=size)
        plain = AnimationEngine(StyleProfile()).still(spec, size=size)

        target = (0xC1, 0x44, 0x0E)
        self.assertIn(target, colours(locked), "the locked colour never appeared")
        self.assertNotIn(
            target, colours(plain), "the colour was there without the lock"
        )

    def test_the_renderer_reads_the_palette_off_the_timeline(self) -> None:
        """Not off the Bible. The Timeline is what is persisted.

        A re-render a month later must produce the same colours even if the
        Bible has since moved on, which is only true if the decision travels
        with the document rather than being looked up again.
        """
        timeline = Timeline(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=PROJECT,
            scene_graph_id=new_id(IdPrefix.SCENE_GRAPH),
            narration=NarrationTrack(audio=BOUND_OBJECT, duration_seconds=5.0),
            entity_colours={"mars": "#c1440e"},
        )
        engine = AnimationEngine(
            timeline.style, entity_colours=timeline.entity_colours
        )
        theme = engine.theme_for(RenderSize.for_aspect(timeline.aspect_ratio))
        self.assertEqual(
            theme.colour_for("Mars", theme.accent)[:3], (0xC1, 0x44, 0x0E)
        )


class TheBibleSurvivesARerender(unittest.TestCase):
    def test_a_locked_binding_is_not_overwritten(self) -> None:
        """The user pressed "keep this". That has to mean something."""
        bible = VisualBible(
            organisation_id=SYSTEM_ORGANISATION_ID, project_id=PROJECT
        )
        bible.bind(
            EntityBinding(
                canonical_name="Mars",
                kind=BindingKind.COLOUR,
                colour="#c1440e",
                # A lock is a *source*, not a flag: only a user or a brand
                # decision is final, and an automatic one never is.
                source=BindingSource.USER,
            )
        )
        bible.bind(
            EntityBinding(
                canonical_name="Mars", kind=BindingKind.COLOUR, colour="#000000"
            )
        )
        binding = bible.binding_for("Mars", kind=BindingKind.COLOUR)
        assert binding is not None
        self.assertEqual(binding.colour, "#c1440e")

    def test_the_pipeline_accepts_an_existing_bible(self) -> None:
        """The seam the worker uses to carry an approved Bible forward."""
        import inspect

        from vtv.pipeline.orchestrator import Pipeline

        for name in ("run", "run_from_document"):
            with self.subTest(name):
                signature = inspect.signature(getattr(Pipeline, name))
                self.assertIn("visual_bible", signature.parameters)

    def test_the_worker_loads_the_approved_bible(self) -> None:
        """Persisting it was only half the fix; reading it back is the rest."""
        import inspect

        from vtv import jobs

        source = inspect.getsource(jobs)
        self.assertIn("_approved_bible", source)
        self.assertIn('kind="visual_bible"', source)
        for entry in ("run_render_recording", "run_render_document"):
            with self.subTest(entry):
                body = inspect.getsource(getattr(jobs, entry))
                self.assertIn("visual_bible=await _approved_bible", body)

    def test_the_api_reads_it_back_for_the_storyboard(self) -> None:
        from vtv.api.app import ProjectArtifacts

        self.assertIn("visual_bible", ProjectArtifacts.__dataclass_fields__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
