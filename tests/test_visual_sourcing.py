"""What each item in the Regenerate menu actually does.

Before this, `_DirectorProducer.produce` returned typography for every intent,
so all ten menu items had the same effect and three of them described something
the code could not do. These tests are the executable statement of what each one
now means — which rung it starts at, what it falls back to, and what it must
never reach.

The provider stubs count their calls. That is the whole point: "Use a real
source" is only correct if the image generator is *not* asked, and no assertion
about the returned object can show that.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.testing import (
    ScriptedTextGenerationProvider,
    StubImageGenerationProvider,
    StubVideoGenerationProvider,
)
from vtv.contracts.asset import (
    AssetKind,
    AssetProvenance,
    AssetSource,
    License,
    Permission,
)
from vtv.contracts.base import Budget, TimeSpan
from vtv.contracts.errors import VTVError
from vtv.contracts.generation import GenerationKind
from vtv.contracts.style import StyleProfile
from vtv.contracts.visual_plan import VisualStrategy
from vtv.contracts.visual_unit import RegenerationIntent
from vtv.observability.events import EventSink
from vtv.pipeline.assets import AssetResolver
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.generation import GenerationRouter
from vtv.pipeline.sourcing import VisualSourcingService
from vtv.pipeline.visual_intent import ConceptReader
from vtv.ports.assets import AssetCandidate

ORG = "org_0000000000000000000000"
PROJECT = "prj_0000000000000000000001"
UNIT = "vun_0000000000000000000002"

NARRATION = "Your AI checks your schedule and books the meeting for you."


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class Commons:
    """Openverse, stubbed. Counts what it was asked for."""

    name = "stub-commons"

    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.queries: list[str] = []

    async def search(self, *, query: str, kind, constraints, limit):  # type: ignore[no-untyped-def]
        del kind, constraints, limit
        self.queries.append(query)
        if self.empty:
            return []
        # Echoes the query. A real engine's *good* hits describe what was asked
        # for, and the resolver now ranks on that; a double that always
        # answered "An office meeting" was modelling only the bad hits.
        return [
            AssetCandidate(
                kind=AssetKind.IMAGE,
                title=f"A photograph of {query}",
                download_url="https://example.org/photo.jpg",
                provenance=AssetProvenance(
                    source=AssetSource.OPENVERSE,
                    source_id="stub-1",
                    original_url="https://example.org/photo",
                    title=f"A photograph of {query}",
                    creator="A. Photographer",
                    license=License(
                        spdx_id="CC-BY-4.0",
                        name="Creative Commons Attribution 4.0",
                        commercial_use=Permission.ALLOWED,
                        modification=Permission.ALLOWED,
                        attribution_required=True,
                    ),
                ),
            )
        ]


class Fetcher:
    async def fetch(self, url: str) -> tuple[bytes, str]:
        del url
        return b"\xff\xd8\xff\xe0jpeg-ish", "image/jpeg"


class SourcingCase(unittest.TestCase):
    """One assembled ladder, with every rung stubbed and counted."""

    commons_empty = False
    with_image = True
    with_video = True
    with_commons = True

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-sourcing-")
        self.storage = LocalStorageProvider(
            root=Path(self._dir.name), signing_key="test-key"
        )
        self.router = GenerationRouter(events=EventSink())
        self.image = StubImageGenerationProvider(storage=self.storage)
        self.video = StubVideoGenerationProvider(storage=self.storage)
        if self.with_image:
            self.router.register(self.image, GenerationKind.IMAGE)
        if self.with_video:
            self.router.register(self.video, GenerationKind.VIDEO)

        self.commons = Commons(empty=self.commons_empty)
        resolver = (
            AssetResolver(
                storage=self.storage,
                events=EventSink(),
                providers=[self.commons],
                fetcher=Fetcher(),
            )
            if self.with_commons
            else None
        )
        self.composer = SceneComposer(
            storage=self.storage,
            events=EventSink(),
            router=self.router,
            asset_resolver=resolver,
        )
        self.service = VisualSourcingService(
            composer=self.composer,
            concepts=ConceptReader(events=EventSink()),
            events=EventSink(),
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def source(  # type: ignore[no-untyped-def]
        self, intent=None, narration: str = NARRATION, ceiling: float = 0.25
    ):
        return run(
            self.service.source(
                narration=narration,
                intent=intent,
                style=StyleProfile(),
                organisation_id=ORG,
                project_id=PROJECT,
                unit_id=UNIT,
                span=TimeSpan.of(0.0, 4.0),
                budget=Budget(max_cost_usd=ceiling, max_latency_seconds=60.0),
            )
        )


class TheDefaultLadderSpendsNothingItDoesNotHaveTo(SourcingCase):
    def test_the_commons_is_searched_before_anything_is_generated(self) -> None:
        result = self.source()
        self.assertEqual(result.strategy, VisualStrategy.LICENSED_MEDIA)
        self.assertIsNotNone(result.object)
        self.assertTrue(self.commons.queries)
        self.assertEqual(self.image.calls, 0)
        self.assertEqual(self.video.calls, 0)

    def test_the_attribution_travels_with_the_photograph(self) -> None:
        """A CC-BY image without its credit line is a licence breach."""
        result = self.source()
        self.assertTrue(result.attribution)
        self.assertIn("Photographer", result.attribution or "")

    def test_the_search_is_for_something_photographable(self) -> None:
        """Not the sentence back. A stock library cannot search a sentence."""
        self.source()
        for query in self.commons.queries:
            self.assertNotIn(NARRATION, query)
            self.assertLess(len(query), 80)


class WhenTheCommonsHasNothing(SourcingCase):
    commons_empty = True

    def test_it_generates_an_image_and_says_that_is_what_it_did(self) -> None:
        result = self.source()
        self.assertEqual(result.strategy, VisualStrategy.GENERATED_IMAGE)
        self.assertEqual(self.image.calls, 1)
        self.assertIsNotNone(result.object)
        self.assertIn("generated", result.rationale.lower())

    def test_the_descent_is_recorded_rather_than_silent(self) -> None:
        result = self.source()
        self.assertTrue(result.degradations)
        self.assertEqual(result.degradations[0].from_strategy, "licensed_media")


class EachMenuItemDoesWhatItSays(SourcingCase):
    commons_empty = False

    def test_generate_an_image_generates_even_though_a_photograph_exists(self) -> None:
        result = self.source(RegenerationIntent.USE_GENERATED_IMAGE)
        self.assertEqual(result.strategy, VisualStrategy.GENERATED_IMAGE)
        self.assertEqual(self.image.calls, 1)
        self.assertEqual(self.commons.queries, [])

    def test_generate_a_video_reaches_the_video_provider(self) -> None:
        # A ceiling that can actually afford a clip. At the default project
        # ceiling one shot may spend $0.25 and a generated video is estimated at
        # $1.20, so the rung is refused before the call — which is tested in
        # `AShotCannotSpendMoreThanItHas` below and is the correct behaviour.
        result = self.source(RegenerationIntent.USE_GENERATED_VIDEO, ceiling=2.0)
        self.assertEqual(result.strategy, VisualStrategy.GENERATED_VIDEO)
        self.assertEqual(self.video.calls, 1)

    def test_use_a_real_source_never_asks_a_generator(self) -> None:
        result = self.source(RegenerationIntent.USE_REAL_SOURCE)
        self.assertEqual(result.strategy, VisualStrategy.LICENSED_MEDIA)
        self.assertEqual(self.image.calls, 0)
        self.assertEqual(self.video.calls, 0)

    def test_use_typography_neither_searches_nor_generates(self) -> None:
        result = self.source(RegenerationIntent.USE_TYPOGRAPHY)
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertIsNotNone(result.spec)
        self.assertEqual(self.commons.queries, [])
        self.assertEqual(self.image.calls, 0)

    def test_a_style_intent_changes_the_prompt_not_the_order(self) -> None:
        """"More cinematic" is a note to the generator, not a different ladder."""
        result = self.source(RegenerationIntent.MORE_CINEMATIC)
        self.assertEqual(result.strategy, VisualStrategy.LICENSED_MEDIA)
        self.assertTrue(self.commons.queries)


class WithNoProvidersAtAll(SourcingCase):
    with_image = False
    with_video = False
    with_commons = False

    def test_it_reaches_typography_rather_than_failing(self) -> None:
        result = self.source()
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertIsNotNone(result.spec)

    def test_asking_to_generate_still_produces_something(self) -> None:
        """A menu item that produces nothing is worse than one that degrades."""
        result = self.source(RegenerationIntent.USE_GENERATED_IMAGE)
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertTrue(result.is_usable)


class SafetyIsNotOptional(SourcingCase):
    commons_empty = False

    def test_material_we_will_not_illustrate_is_set_as_type(self) -> None:
        result = self.source(
            narration="The report described the torture and the mutilation in detail."
        )
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertEqual(self.commons.queries, [])
        self.assertEqual(self.image.calls, 0)
        self.assertIn("graphic violence", result.rationale)

    def test_asking_explicitly_to_generate_does_not_override_it(self) -> None:
        """The verdict is on the concept, so the intent cannot route around it."""
        result = self.source(
            RegenerationIntent.USE_GENERATED_IMAGE,
            narration="The report described the torture and the mutilation in detail.",
        )
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertEqual(self.image.calls, 0)

    def test_the_one_category_that_produces_nothing_at_all(self) -> None:
        with self.assertRaises(VTVError) as caught:
            self.source(narration="Explicit pornographic images of children.")
        self.assertIn("no visual", str(caught.exception).lower())
        self.assertEqual(self.commons.queries, [])
        self.assertEqual(self.image.calls, 0)

    def test_a_real_named_subject_is_photographed_not_invented(self) -> None:
        result = self.source(
            narration="The transistor was invented at Bell Labs in 1947."
        )
        self.assertEqual(result.strategy, VisualStrategy.LICENSED_MEDIA)
        self.assertEqual(self.image.calls, 0)

    def test_a_real_named_subject_falls_to_type_rather_than_being_generated(self) -> None:
        self.commons.empty = True
        result = self.source(
            narration="The transistor was invented at Bell Labs in 1947."
        )
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertEqual(self.image.calls, 0)

    def test_swearing_is_removed_from_the_query_and_not_from_the_script(self) -> None:
        line = "The fucking compiler crashed on the build server again."
        result = self.source(narration=line)
        self.assertEqual(result.strategy, VisualStrategy.LICENSED_MEDIA)
        self.assertTrue(self.commons.queries)
        for query in self.commons.queries:
            self.assertNotIn("fucking", query.lower())


class WhenAModelReadsTheIntent(SourcingCase):
    commons_empty = False

    def test_the_models_queries_are_used_and_the_rules_are_kept_behind_them(self) -> None:
        text = ScriptedTextGenerationProvider(
            responses=[
                {
                    "subject": "an empty meeting room",
                    "search_queries": ["empty conference room", "office calendar on a wall"],
                    "image_prompt": "An empty meeting room lit by morning light.",
                    "motion": "a slow push in",
                    "rationale": "the sentence is about a meeting being arranged for you",
                }
            ]
        )
        self.router.register(text, GenerationKind.TEXT)
        self.service.concepts = ConceptReader(events=EventSink(), router=self.router)

        result = self.source()
        self.assertEqual(self.commons.queries[0], "empty conference room")
        self.assertIn("meeting", result.rationale)

    def test_a_model_that_returns_unusable_shape_falls_back_to_rules(self) -> None:
        text = ScriptedTextGenerationProvider(responses=[{"subject": "", "search_queries": []}])
        self.router.register(text, GenerationKind.TEXT)
        self.service.concepts = ConceptReader(events=EventSink(), router=self.router)

        result = self.source()
        self.assertEqual(result.strategy, VisualStrategy.LICENSED_MEDIA)
        self.assertTrue(self.commons.queries)

    def test_a_model_is_not_asked_about_material_we_will_not_illustrate(self) -> None:
        """No round trip, and the text never reaches a vendor for no purpose."""
        text = ScriptedTextGenerationProvider(
            responses=[
                {
                    "subject": "x",
                    "search_queries": ["x"],
                    "image_prompt": "x",
                    "motion": "x",
                    "rationale": "x",
                }
            ]
        )
        self.router.register(text, GenerationKind.TEXT)
        self.service.concepts = ConceptReader(events=EventSink(), router=self.router)

        self.source(narration="The report described the torture in detail.")
        self.assertEqual(text.calls, 0)

    def test_a_model_that_proposes_something_disallowed_is_classified_again(self) -> None:
        """The verdict is recomputed over the model's own output, not the input."""
        text = ScriptedTextGenerationProvider(
            responses=[
                {
                    "subject": "a beheading",
                    "search_queries": ["graphic beheading footage"],
                    "image_prompt": "A beheading, photographed closely.",
                    "motion": "still",
                    "rationale": "asked for",
                }
            ]
        )
        self.router.register(text, GenerationKind.TEXT)
        self.service.concepts = ConceptReader(events=EventSink(), router=self.router)

        result = self.source(narration="The kingdom fell after the last of the wars.")
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertEqual(self.commons.queries, [])
        self.assertEqual(self.image.calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ALicensedPhotographCarriesItsCredit(SourcingCase):
    """A CC-BY image used without attribution is a licence breach.

    The renderer draws `AssetClipSource.attribution` and `Timeline.attributions`
    collects it — both of which existed before the commons rung worked, and
    neither of which the Studio's path could reach, because `VisualVersion` had
    nowhere to keep the credit between the search and the encode. It cost
    nothing while that rung was unreachable. It is the customer's legal exposure
    now that it is not.
    """

    def test_the_credit_survives_all_the_way_to_the_render_contract(self) -> None:
        from vtv.contracts.tracks import (
            ClipSourceKind,
            EditTimeline,
            TimelineClip,
            Track,
            TrackKind,
        )
        from vtv.contracts.visual_unit import VisualUnit, VisualVersion
        from vtv.pipeline.units import retarget

        sourced = self.source()
        self.assertTrue(sourced.attribution)

        unit = VisualUnit(
            organisation_id=ORG,
            project_id=PROJECT,
            index=0,
            script_block_ids=["blk_0000000000000000000003"],
            span=TimeSpan.of(0.0, 4.0),
        )
        unit.add_version(
            VisualVersion(
                version=1,
                strategy=sourced.strategy,
                object=sourced.object,
                asset_id=sourced.asset_id,
                attribution=sourced.attribution,
                rationale=sourced.rationale,
            )
        )

        track = Track(kind=TrackKind.VISUAL, name="Visuals")
        track.clips.append(
            TimelineClip(
                track_id=track.track_id,
                visual_unit_id=unit.visual_unit_id,
                start=0.0,
                end=4.0,
                source_kind=ClipSourceKind.EMPTY,
            )
        )
        narration = Track(kind=TrackKind.NARRATION, name="Narration")
        narration.clips.append(
            TimelineClip(
                track_id=narration.track_id,
                start=0.0,
                end=4.0,
                source_kind=ClipSourceKind.TEXT,
                text=NARRATION,
            )
        )
        timeline = EditTimeline(
            organisation_id=ORG,
            project_id=PROJECT,
            tracks=[track, narration],
        )

        repointed = retarget(timeline, [unit])
        clip = repointed.track_of_kind(TrackKind.VISUAL).clips[0]
        self.assertEqual(clip.attribution, sourced.attribution)

    def test_flatten_hands_the_credit_to_the_renderer(self) -> None:
        """`flatten` is the last place it can be dropped, and it was dropping it."""
        from vtv.contracts.base import ObjectRef
        from vtv.contracts.style import AspectRatio
        from vtv.contracts.timeline import AssetClipSource, NarrationTrack
        from vtv.contracts.tracks import (
            ClipSourceKind,
            EditTimeline,
            TimelineClip,
            Track,
            TrackKind,
        )
        from vtv.pipeline.units import flatten

        credit = "Photo by A. Photographer (CC BY 4.0)"
        visuals = Track(kind=TrackKind.VISUAL, name="Visuals")
        visuals.clips.append(
            TimelineClip(
                track_id=visuals.track_id,
                start=0.0,
                end=4.0,
                source_kind=ClipSourceKind.OBJECT,
                object=ObjectRef(bucket="b", key="k.jpg", content_type="image/jpeg"),
                attribution=credit,
            )
        )
        narration = Track(kind=TrackKind.NARRATION, name="Narration")
        narration.clips.append(
            TimelineClip(
                track_id=narration.track_id,
                start=0.0,
                end=4.0,
                source_kind=ClipSourceKind.TEXT,
                text=NARRATION,
            )
        )
        timeline = EditTimeline(
            organisation_id=ORG, project_id=PROJECT, tracks=[visuals, narration]
        )

        rendered = flatten(
            timeline,
            narration=NarrationTrack(
                audio=ObjectRef(bucket="b", key="a.wav", content_type="audio/wav"),
                duration_seconds=4.0,
            ),
            style=StyleProfile(),
            aspect_ratio=AspectRatio.LANDSCAPE_16_9,
            scene_graph_id="scg_0000000000000000000004",
        )
        source = rendered.clips[0].source
        self.assertIsInstance(source, AssetClipSource)
        self.assertEqual(source.attribution, credit)
        self.assertIn(credit, rendered.attributions())


class AShotCannotSpendMoreThanItHas(SourcingCase):
    """The estimate is checked before the call, not the invoice after it.

    `GenerationRouter` filters on `unit_cost_usd`, which for video is a price
    per *second*. A ten-cent second passes a twenty-five-cent ceiling and then
    bills eighty cents for an eight-second clip — the router raises
    `BudgetExceeded`, the ladder descends, and the user has paid for a result
    that was thrown away.
    """

    commons_empty = True

    def source_with(self, ceiling: float, intent=None):  # type: ignore[no-untyped-def]
        return run(
            self.service.source(
                narration=NARRATION,
                intent=intent,
                style=StyleProfile(),
                organisation_id=ORG,
                project_id=PROJECT,
                unit_id=UNIT,
                span=TimeSpan.of(0.0, 4.0),
                budget=Budget(max_cost_usd=ceiling, max_latency_seconds=60.0),
            )
        )

    def test_the_video_rung_is_not_attempted_under_a_small_ceiling(self) -> None:
        result = self.source_with(0.25, RegenerationIntent.SAME_IDEA)
        self.assertEqual(self.video.calls, 0)
        self.assertEqual(result.strategy, VisualStrategy.GENERATED_IMAGE)

    def test_asking_for_video_under_that_ceiling_says_so_rather_than_substituting(
        self,
    ) -> None:
        with self.assertRaises(VTVError) as caught:
            self.source_with(0.25, RegenerationIntent.USE_GENERATED_VIDEO)
        message = caught.exception.info.user_message or ""
        self.assertIn("VTV_MAX_PROJECT_COST_USD", message)
        self.assertEqual(self.video.calls, 0)

    def test_a_ceiling_that_can_afford_it_lets_it_through(self) -> None:
        result = self.source_with(2.0, RegenerationIntent.USE_GENERATED_VIDEO)
        self.assertEqual(result.strategy, VisualStrategy.GENERATED_VIDEO)
        self.assertEqual(self.video.calls, 1)

    def test_a_ceiling_below_everything_still_produces_type(self) -> None:
        """The last rung costs nothing, so there is always an answer."""
        result = self.source_with(0.001)
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertEqual(self.image.calls, 0)


class RungOneIsDrawing(SourcingCase):
    """The cheapest rung, and the one the Studio could not reach.

    `VisualSourcingService` had no `Understanding` to hand, so the only
    programmatic visual it could build was a `TypographySpec` from the first
    sentence. Two consequences the user saw: a line stating two numbers went to
    an image model at a quarter of a dollar to produce an *impression* of
    quantity, and "Use animation" in the Regenerate menu produced exactly what
    "Use typography" produced.
    """

    commons_empty = False

    def setUp(self) -> None:
        super().setUp()
        from vtv.pipeline.drawing import Draughtsman

        self.service.draughtsman = Draughtsman(events=EventSink())

    def test_a_sentence_with_two_numbers_is_charted_not_bought(self) -> None:
        result = self.source(narration="The population went from one billion to eight billion.")
        self.assertEqual(result.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertEqual((result.spec or {}).get("primitive"), "chart")
        self.assertEqual(self.image.calls, 0)
        self.assertEqual(self.commons.queries, [], "nothing was searched for either")

    def test_a_span_of_years_becomes_a_timeline(self) -> None:
        result = self.source(
            narration="Between 1947 and 1967 the transistor replaced the vacuum tube."
        )
        self.assertEqual((result.spec or {}).get("primitive"), "timeline")
        self.assertEqual(self.image.calls, 0)

    def test_a_comparison_is_held_side_by_side(self) -> None:
        result = self.source(narration="Solar is cheaper than coal now.")
        self.assertEqual((result.spec or {}).get("primitive"), "comparison")

    def test_an_abstract_line_still_reaches_the_paid_rungs(self) -> None:
        """Drawing first does not mean drawing always."""
        result = self.source(narration="Imagine waking up ten years from now.")
        self.assertEqual(result.strategy, VisualStrategy.LICENSED_MEDIA)

    def test_use_animation_now_differs_from_use_typography(self) -> None:
        line = "The population went from one billion to eight billion."
        drawn = self.source(RegenerationIntent.USE_ANIMATION, narration=line)
        typed = self.source(RegenerationIntent.USE_TYPOGRAPHY, narration=line)
        self.assertEqual((drawn.spec or {}).get("primitive"), "chart")
        self.assertEqual((typed.spec or {}).get("primitive"), "typography")

    def test_use_animation_falls_to_type_when_there_is_nothing_to_draw(self) -> None:
        """Honest rather than empty: most sentences support no drawing."""
        result = self.source(
            RegenerationIntent.USE_ANIMATION,
            narration="Imagine waking up ten years from now.",
        )
        self.assertEqual((result.spec or {}).get("primitive"), "typography")
        self.assertEqual(self.image.calls, 0)


class TheEditorsTransitionReachesTheRenderer(unittest.TestCase):
    """`flatten` dropped it, so every Studio render was hard cuts.

    `TimelineBuilder` sets `transition_in` on every clip after the first, and
    the renderer blends two frames to draw a dissolve — it has done both since
    before this test existed. The one line between them was missing, which is
    the same shape as the attribution field: present at both ends, dropped in
    the middle.
    """

    def test_a_dissolve_survives_the_flatten(self) -> None:
        from vtv.contracts.base import ObjectRef
        from vtv.contracts.style import AspectRatio
        from vtv.contracts.timeline import NarrationTrack, TransitionKind
        from vtv.contracts.tracks import (
            ClipSourceKind,
            EditTimeline,
            TimelineClip,
            Track,
            TrackKind,
        )
        from vtv.pipeline.units import flatten

        visuals = Track(kind=TrackKind.VISUAL, name="Visuals")
        for index in range(2):
            visuals.clips.append(
                TimelineClip(
                    track_id=visuals.track_id,
                    start=index * 3.0,
                    end=(index + 1) * 3.0,
                    source_kind=ClipSourceKind.TEXT,
                    text=f"shot {index}",
                    transition_in=(
                        TransitionKind.CUT if index == 0 else TransitionKind.DISSOLVE
                    ),
                    transition_in_seconds=0.0 if index == 0 else 0.4,
                )
            )
        narration = Track(kind=TrackKind.NARRATION, name="Narration")
        narration.clips.append(
            TimelineClip(
                track_id=narration.track_id, start=0.0, end=6.0,
                source_kind=ClipSourceKind.TEXT, text="two shots",
            )
        )
        rendered = flatten(
            EditTimeline(organisation_id=ORG, project_id=PROJECT, tracks=[visuals, narration]),
            narration=NarrationTrack(
                audio=ObjectRef(bucket="b", key="a.wav", content_type="audio/wav"),
                duration_seconds=6.0,
            ),
            style=StyleProfile(),
            aspect_ratio=AspectRatio.LANDSCAPE_16_9,
            scene_graph_id="scg_0000000000000000000004",
        )
        self.assertEqual(rendered.clips[0].transition_in.kind, TransitionKind.CUT)
        self.assertEqual(rendered.clips[1].transition_in.kind, TransitionKind.DISSOLVE)
        self.assertAlmostEqual(rendered.clips[1].transition_in.duration_seconds, 0.4)

    def test_a_zero_length_transition_is_a_cut(self) -> None:
        """A kind with no duration is not a transition, whatever it is called."""
        from vtv.contracts.timeline import TransitionKind
        from vtv.contracts.tracks import ClipSourceKind, TimelineClip, Track, TrackKind
        from vtv.pipeline.units import _transition_of

        track = Track(kind=TrackKind.VISUAL, name="V")
        clip = TimelineClip(
            track_id=track.track_id, start=0.0, end=2.0,
            source_kind=ClipSourceKind.TEXT, text="x",
            transition_in=TransitionKind.DISSOLVE, transition_in_seconds=0.0,
        )
        self.assertEqual(_transition_of(clip).kind, TransitionKind.CUT)
