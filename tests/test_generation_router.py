"""Stage 7 — the generation router.

Caching, selection, retry, fallback and budget enforcement are implemented once
here so that every provider behaves identically. These tests are what keep that
true.

The last two classes are about money leaving the building. The router is the
only place every paid call passes through, so it is where a request with no cost
ceiling is refused and where a tenant's spending limit is enforced — both of
which used to be enforced nowhere at all.
"""

from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.testing import (
    FailingProvider,
    StubImageGenerationProvider,
)
from vtv.contracts.base import Budget, IdPrefix, ObjectRef, TimeSpan, new_id
from vtv.contracts.errors import (
    DegradationReason,
    ErrorCode,
    PolicyViolation,
    ProviderError,
    ProviderRefused,
    Status,
    VTVError,
)
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    ImageParams,
    SpeechToTextParams,
)
from vtv.contracts.scene import Scene, SceneGraph, ScenePurpose, VisualGoal
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.timeline import NarrationTrack, ProgrammaticClipSource
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.contracts.visual_language import TypographySpec
from vtv.contracts.visual_plan import (
    ImageGenerationRequirements,
    ProgrammaticRequirements,
    SceneVisualPlan,
    VisualDirective,
    VisualPlan,
    VisualStrategy,
)
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.generation import CIRCUIT_THRESHOLD, GenerationRouter
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth


def run(coro):
    return asyncio.run(coro)


def image_request(prompt: str = "a silicon die, macro", **kwargs) -> GenerationRequest:
    return GenerationRequest(
        organisation_id=SYSTEM_ORGANISATION_ID,
        kind=GenerationKind.IMAGE, params=ImageParams(prompt=prompt), **kwargs
    )


class _Priced:
    """A provider that succeeds, at a stated price."""

    def __init__(self, name: str, cost: float, latency: float = 1.0) -> None:
        self.name = name
        self.cost = cost
        self.latency = latency
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            unit_cost_usd=self.cost,
            typical_latency_seconds=self.latency,
            data_policy=DataPolicy(
                retains_input=False, trains_on_input=False, dpa_in_place=True
            ),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate_image(self, request: GenerationRequest):
        from vtv.contracts.generation import GenerationResult

        self.calls += 1
        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model="test",
            structured_output={"ok": True},
            cost_usd=self.cost,
            latency_ms=int(self.latency * 1000),
        )


class Caching(unittest.TestCase):
    def setUp(self) -> None:
        self.events = EventSink()
        self.router = GenerationRouter(events=self.events)
        self.provider = _Priced("cheap", 0.01)
        self.router.register(self.provider, GenerationKind.IMAGE)

    def test_the_same_request_is_paid_for_once(self) -> None:
        first = run(self.router.generate(image_request()))
        second = run(self.router.generate(image_request()))
        self.assertEqual(self.provider.calls, 1)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.cost_usd, 0.0)
        self.assertAlmostEqual(self.router.ledger.total_usd, 0.01)

    def test_the_cache_hit_rate_is_visible(self) -> None:
        run(self.router.generate(image_request()))
        run(self.router.generate(image_request()))
        self.assertAlmostEqual(self.router.ledger.cache_hit_rate, 0.5)

    def test_a_different_prompt_is_a_different_request(self) -> None:
        run(self.router.generate(image_request("one")))
        run(self.router.generate(image_request("two")))
        self.assertEqual(self.provider.calls, 2)

    def test_a_failure_is_never_cached(self) -> None:
        # Caching a bad minute would turn it into a permanently broken shot.
        router = GenerationRouter(events=self.events)
        failing = FailingProvider()
        router.register(failing, GenerationKind.IMAGE)
        for _ in range(2):
            with self.assertRaises(VTVError):
                run(router.generate(image_request(max_attempts=1)))
        self.assertEqual(len(router.cache), 0)


class Selection(unittest.TestCase):
    def test_the_cheapest_capable_provider_wins(self) -> None:
        router = GenerationRouter(events=EventSink())
        expensive = _Priced("expensive", 0.20)
        cheap = _Priced("cheap", 0.02)
        router.register(expensive, GenerationKind.IMAGE)
        router.register(cheap, GenerationKind.IMAGE)
        result = run(router.generate(image_request()))
        self.assertEqual(result.provider, "cheap")
        self.assertEqual(expensive.calls, 0)

    def test_a_provider_over_budget_is_not_considered(self) -> None:
        router = GenerationRouter(events=EventSink())
        router.register(_Priced("expensive", 0.20), GenerationKind.IMAGE)
        with self.assertRaises(VTVError) as caught:
            run(router.generate(image_request(budget=Budget(max_cost_usd=0.05))))
        self.assertIs(caught.exception.info.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_a_provider_that_is_too_slow_is_not_considered(self) -> None:
        router = GenerationRouter(events=EventSink())
        router.register(_Priced("slow", 0.01, latency=120.0), GenerationKind.IMAGE)
        with self.assertRaises(VTVError):
            run(router.generate(image_request(budget=Budget(max_latency_seconds=10))))

    def test_a_hint_is_honoured_when_the_provider_is_eligible(self) -> None:
        router = GenerationRouter(events=EventSink())
        router.register(_Priced("cheap", 0.01), GenerationKind.IMAGE)
        router.register(_Priced("preferred", 0.05), GenerationKind.IMAGE)
        result = run(router.generate(image_request(provider_hint="preferred")))
        self.assertEqual(result.provider, "preferred")

    def test_unverified_providers_never_receive_user_voice(self) -> None:
        """The enforcement point for the privacy commitment in docs/SECURITY.md."""

        class Unvetted(_Priced):
            @property
            def capabilities(self) -> ProviderCapabilities:
                # Defaults: assumed to retain and train on input.
                return ProviderCapabilities(name=self.name, data_policy=DataPolicy())

            async def transcribe(self, request: GenerationRequest):
                raise AssertionError("must never be reached")

        from vtv.contracts.base import ObjectRef

        router = GenerationRouter(events=EventSink())
        router.register(Unvetted("unvetted", 0.0), GenerationKind.SPEECH_TO_TEXT)
        request = GenerationRequest(
            organisation_id=SYSTEM_ORGANISATION_ID,
            kind=GenerationKind.SPEECH_TO_TEXT,
            params=SpeechToTextParams(
                audio=ObjectRef(bucket="b", key="a.webm", content_type="audio/webm")
            ),
        )
        with self.assertRaises(VTVError) as caught:
            run(router.generate(request))
        self.assertIs(caught.exception.info.code, ErrorCode.PROVIDER_UNAVAILABLE)


class RetryAndFallback(unittest.TestCase):
    def test_a_transient_failure_is_retried(self) -> None:
        router = GenerationRouter(events=EventSink())
        failing = FailingProvider(error=ProviderError("upstream 503"))
        router.register(failing, GenerationKind.IMAGE)
        with self.assertRaises(VTVError):
            run(router.generate(image_request(max_attempts=3)))
        self.assertEqual(failing.calls, 3)

    def test_a_refusal_is_not_retried(self) -> None:
        # A content-policy refusal will refuse identically on a second attempt.
        router = GenerationRouter(events=EventSink())
        failing = FailingProvider(error=ProviderRefused("declined"))
        router.register(failing, GenerationKind.IMAGE)
        with self.assertRaises(VTVError):
            run(router.generate(image_request(max_attempts=3)))
        self.assertEqual(failing.calls, 1)

    def test_a_failing_provider_falls_through_to_a_healthy_one(self) -> None:
        router = GenerationRouter(events=EventSink())
        broken = FailingProvider(name="broken", error=ProviderRefused("declined"))
        working = _Priced("working", 0.05)
        router.register(broken, GenerationKind.IMAGE)
        router.register(working, GenerationKind.IMAGE)
        result = run(router.generate(image_request(max_attempts=1)))
        self.assertEqual(result.provider, "working")

    def test_a_persistently_failing_provider_is_taken_out_of_rotation(self) -> None:
        router = GenerationRouter(events=EventSink())
        broken = FailingProvider(name="broken", error=ProviderRefused("declined"))
        router.register(broken, GenerationKind.IMAGE)
        for index in range(CIRCUIT_THRESHOLD):
            with self.assertRaises(VTVError):
                run(router.generate(image_request(f"prompt {index}", max_attempts=1)))
        before = broken.calls
        with self.assertRaises(VTVError):
            run(router.generate(image_request("another prompt", max_attempts=1)))
        self.assertEqual(broken.calls, before, "circuit should be open")

    def test_no_provider_at_all_raises_rather_than_faking_success(self) -> None:
        # The whole point: an unavailable provider must degrade the *scene*, and
        # it can only do that if the router is honest about failing.
        router = GenerationRouter(events=EventSink())
        with self.assertRaises(VTVError) as caught:
            run(router.generate(image_request()))
        self.assertIs(caught.exception.info.code, ErrorCode.PROVIDER_UNAVAILABLE)


class Accounting(unittest.TestCase):
    def test_failures_are_recorded_with_zero_cost_but_counted(self) -> None:
        router = GenerationRouter(events=EventSink())
        router.register(
            FailingProvider(error=ProviderRefused("declined")), GenerationKind.IMAGE
        )
        with self.assertRaises(VTVError):
            run(router.generate(image_request(max_attempts=1)))
        self.assertEqual(router.ledger.total_usd, 0.0)
        self.assertEqual(router.ledger.failure_rate, 1.0)

    def test_spend_is_attributable_to_a_scene(self) -> None:
        router = GenerationRouter(events=EventSink())
        router.register(_Priced("cheap", 0.03), GenerationKind.IMAGE)
        scene_id = "scn_" + "a" * 24
        run(router.generate(image_request(scene_id=scene_id)))
        self.assertAlmostEqual(router.ledger.by_scene()[scene_id], 0.03)

    def test_events_narrate_every_attempt(self) -> None:
        events = EventSink()
        events.record = True
        router = GenerationRouter(events=events)
        router.register(_Priced("cheap", 0.01), GenerationKind.IMAGE)
        run(router.generate(image_request()))
        names = [event.name for event in events.recorded]
        self.assertIn(EventName.GENERATION_STARTED, names)
        self.assertIn(EventName.GENERATION_COMPLETED, names)


class _Denying:
    """A spend authoriser that says no, and counts what it was asked."""

    def __init__(self, error: VTVError | None = None) -> None:
        self.asked: list[tuple[str, float]] = []
        self.noted: list[tuple[str, float]] = []
        self.error = error or PolicyViolation(
            "provider spend for this tenant is 40 of a 40 USD monthly allowance",
            code=ErrorCode.QUOTA_EXCEEDED,
            user_message=(
                "This account has reached its monthly limit for AI generation "
                "spend. Upgrade your plan or wait for it to reset next month."
            ),
        )

    def authorise(self, *, organisation_id: str, amount_usd: float) -> None:
        self.asked.append((organisation_id, amount_usd))
        raise self.error

    def note_spend(self, *, organisation_id: str, amount_usd: float) -> None:
        self.noted.append((organisation_id, amount_usd))


class _Permitting(_Denying):
    def authorise(self, *, organisation_id: str, amount_usd: float) -> None:
        self.asked.append((organisation_id, amount_usd))


class NoCallIsMadeWithoutACostCeiling(unittest.TestCase):
    """A request the router cannot price is refused, not dispatched.

    Both budget checks in the router are `is not None`-guarded, so an unbounded
    budget disabled every one of them. The contract now fills a backstop ceiling
    during validation — but `model_copy`, `model_construct` and hand-built
    stand-ins all skip validation, so the router refuses as well. Belt and
    braces, because the braces are the half that protects call sites nobody has
    written yet.
    """

    def setUp(self) -> None:
        self.router = GenerationRouter(events=EventSink())
        self.provider = _Priced("cheap", 0.01)
        self.router.register(self.provider, GenerationKind.IMAGE)

    def unbudgeted(self) -> GenerationRequest:
        # model_copy does not re-run validators, which is exactly how an
        # unbounded budget gets past the contract in real code.
        return image_request().model_copy(update={"budget": Budget()})

    def test_a_budget_stripped_after_validation_is_refused(self) -> None:
        with self.assertRaises(VTVError) as caught:
            run(self.router.generate(self.unbudgeted()))
        self.assertIs(caught.exception.info.code, ErrorCode.BUDGET_EXCEEDED)

    def test_the_provider_is_never_reached(self) -> None:
        with self.assertRaises(VTVError):
            run(self.router.generate(self.unbudgeted()))
        self.assertEqual(self.provider.calls, 0)

    def test_the_refusal_names_the_remedy(self) -> None:
        with self.assertRaises(VTVError) as caught:
            run(self.router.generate(self.unbudgeted()))
        self.assertIn("Budget(max_cost_usd", str(caught.exception))
        self.assertIn("SceneVisualPlan.budget", str(caught.exception))

    def test_the_spend_authoriser_is_not_asked_about_an_unknown_amount(self) -> None:
        authoriser = _Permitting()
        router = GenerationRouter(events=EventSink(), spend_authoriser=authoriser)
        router.register(_Priced("cheap", 0.01), GenerationKind.IMAGE)
        with self.assertRaises(VTVError):
            run(router.generate(self.unbudgeted()))
        self.assertEqual(authoriser.asked, [], "'may I spend ?' has no safe yes")


class TenantSpendIsCheckedBeforeItIsSpent(unittest.TestCase):
    """`QuotaKind.PROVIDER_SPEND_USD` was recorded and never checked.

    It is described in billing/plans.py as "the circuit breaker that stops a
    runaway loop becoming a runaway bill", and a grep for a check against it
    returned nothing. The router is where the money moves, so it is where the
    breaker lives.
    """

    def setUp(self) -> None:
        self.authoriser = _Denying()
        self.router = GenerationRouter(
            events=EventSink(), spend_authoriser=self.authoriser
        )
        self.provider = _Priced("cheap", 0.01)
        self.router.register(self.provider, GenerationKind.IMAGE)

    def test_a_tenant_past_its_limit_never_reaches_a_provider(self) -> None:
        with self.assertRaises(VTVError) as caught:
            run(self.router.generate(image_request()))
        self.assertEqual(self.provider.calls, 0)
        self.assertIs(caught.exception.info.code, ErrorCode.QUOTA_EXCEEDED)

    def test_the_refusal_reaches_the_user_with_a_remedy(self) -> None:
        with self.assertRaises(VTVError) as caught:
            run(self.router.generate(image_request()))
        message = caught.exception.info.user_message or ""
        self.assertIn("Upgrade", message)
        self.assertNotIn("cheap", message, "no provider name in a user message")

    def test_the_amount_authorised_is_the_ceiling_this_call_could_cost(self) -> None:
        with self.assertRaises(VTVError):
            run(self.router.generate(image_request(budget=Budget(max_cost_usd=0.25))))
        self.assertEqual(self.authoriser.asked[-1][1], 0.25)

    def test_a_cache_hit_is_not_charged_against_the_limit(self) -> None:
        """Serving work already paid for must not be refused: it costs nothing,
        and refusing it degrades a shot for no saving."""
        permitting = _Permitting()
        router = GenerationRouter(events=EventSink(), spend_authoriser=permitting)
        router.register(_Priced("cheap", 0.01), GenerationKind.IMAGE)
        run(router.generate(image_request()))
        router.spend_authoriser = _Denying()
        cached = run(router.generate(image_request()))
        self.assertTrue(cached.from_cache)

    def test_what_a_call_actually_cost_is_reported_back(self) -> None:
        """Without this the next check is answered from a stale figure, and a
        loop inside one job passes the same check a thousand times."""
        permitting = _Permitting()
        router = GenerationRouter(events=EventSink(), spend_authoriser=permitting)
        router.register(_Priced("cheap", 0.03), GenerationKind.IMAGE)
        run(router.generate(image_request()))
        self.assertEqual(permitting.noted, [(SYSTEM_ORGANISATION_ID, 0.03)])

    def test_an_overspending_provider_is_still_reported_as_spend(self) -> None:
        """The work happened and the invoice will show it, so the breaker must
        see it even though the result was refused."""
        permitting = _Permitting()
        router = GenerationRouter(events=EventSink(), spend_authoriser=permitting)
        # Advertises 0.02, charges 0.90: the case the post-hoc budget check
        # exists for, and the one where the money is already gone.
        router.register(_Greedy("greedy", 0.02, charged=0.90), GenerationKind.IMAGE)
        with self.assertRaises(VTVError):
            run(router.generate(image_request(budget=Budget(max_cost_usd=0.10))))
        self.assertEqual(permitting.noted, [(SYSTEM_ORGANISATION_ID, 0.90)])

    def test_an_authoriser_that_cannot_answer_denies(self) -> None:
        """Fail closed: an unreachable meter is not consent."""

        class Broken(_Permitting):
            def authorise(self, *, organisation_id: str, amount_usd: float) -> None:
                raise sqlite3.OperationalError("database is locked")

        router = GenerationRouter(events=EventSink(), spend_authoriser=Broken())
        provider = _Priced("cheap", 0.01)
        router.register(provider, GenerationKind.IMAGE)
        with self.assertRaises(VTVError) as caught:
            run(router.generate(image_request()))
        self.assertIs(caught.exception.info.code, ErrorCode.QUOTA_EXCEEDED)
        self.assertEqual(provider.calls, 0)


class ARefusedSpendDegradesTheShotNotTheProject(unittest.TestCase):
    """Rule 8, through the composer.

    A tenant at their spend limit still gets a video: the generated shot is
    refused at the router, the ladder descends to typography, and the reason is
    recorded on the clip rather than raised at the project.
    """

    def setUp(self) -> None:
        self.graph = _one_scene_graph()
        self.plan = _image_then_typography(self.graph)

    def compose(self, router: GenerationRouter):  # type: ignore[no-untyped-def]
        composer = SceneComposer(
            storage=object(), events=EventSink(), router=router
        )
        return run(
            composer.compose(
                scene_graph=self.graph,
                visual_plan=self.plan,
                transcript=_transcript_of(self.graph),
                narration=NarrationTrack(
                    audio=ObjectRef(
                        bucket="b", key="n.wav", content_type="audio/wav"
                    ),
                    duration_seconds=5.0,
                ),
            )
        )

    def test_the_per_scene_budget_reaches_the_request(self) -> None:
        """SceneVisualPlan.budget was computed by the Director and read by
        nobody, so every generated shot arrived at the router unbudgeted."""
        recorder = _RecordingProvider()
        router = GenerationRouter(events=EventSink(), spend_authoriser=_Permitting())
        router.register(recorder, GenerationKind.IMAGE)
        self.compose(router)
        self.assertEqual(len(recorder.requests), 1)
        self.assertEqual(recorder.requests[0].budget.max_cost_usd, 0.4)
        self.assertEqual(recorder.requests[0].budget.max_latency_seconds, 180.0)

    def test_a_spend_refusal_becomes_a_plainer_shot(self) -> None:
        router = GenerationRouter(events=EventSink(), spend_authoriser=_Denying())
        router.register(_RecordingProvider(), GenerationKind.IMAGE)
        result = self.compose(router)
        clip = result.timeline.clips[0]
        self.assertIsInstance(clip.source, ProgrammaticClipSource)
        self.assertEqual(
            [step.reason for step in clip.degradation],
            [DegradationReason.BUDGET_EXCEEDED],
        )

    def test_the_project_still_renders(self) -> None:
        router = GenerationRouter(events=EventSink(), spend_authoriser=_Denying())
        router.register(_RecordingProvider(), GenerationKind.IMAGE)
        result = self.compose(router)
        self.assertEqual(result.timeline.placeholder_count, 0)
        self.assertEqual(result.timeline.coverage_gaps(), [])


class _Greedy(_Priced):
    """Quotes one price to the router and charges another on the invoice."""

    def __init__(self, name: str, advertised: float, *, charged: float) -> None:
        super().__init__(name, advertised)
        self.charged = charged

    async def generate_image(self, request: GenerationRequest):
        outcome = await super().generate_image(request)
        return outcome.model_copy(update={"cost_usd": self.charged})


class _RecordingProvider(_Priced):
    """Keeps the requests it was given, so the budget on them can be asserted."""

    def __init__(self) -> None:
        super().__init__("recorder", 0.01)
        self.requests: list[GenerationRequest] = []

    async def generate_image(self, request: GenerationRequest):
        self.requests.append(request)
        return await super().generate_image(request)


def _one_scene_graph() -> SceneGraph:
    return SceneGraph(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=new_id(IdPrefix.PROJECT),
        understanding_id=new_id(IdPrefix.ENTITY),
        transcript_id=new_id(IdPrefix.TRANSCRIPT),
        scenes=[
            Scene(
                index=0,
                span=TimeSpan.of(0.0, 5.0),
                narration="An idea with no photograph of it anywhere.",
                purpose=ScenePurpose.EXPLANATION,
                visual_goal=VisualGoal.ILLUSTRATE_ABSTRACT,
                visual_brief="an abstract idea",
                importance=0.4,
            )
        ],
        style=StyleProfile(),
    )


def _image_then_typography(graph: SceneGraph) -> VisualPlan:
    """The ladder the Director builds for an abstract scene, with its budget.

    0.4 is `max(0.02, project ceiling of 1.0) * importance 0.4` — the figure
    `RuleBasedVisualDirector.plan_scene` computes.
    """
    return VisualPlan(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=graph.project_id,
        scene_graph_id=graph.scene_graph_id,
        scene_plans=[
            SceneVisualPlan(
                scene_id=graph.scenes[0].scene_id,
                primary=VisualDirective(
                    strategy=VisualStrategy.GENERATED_IMAGE,
                    requirements=ImageGenerationRequirements(
                        prompt="an abstract idea, wide shot"
                    ),
                    rationale="no photographic referent exists for this",
                ),
                fallbacks=[
                    VisualDirective(
                        strategy=VisualStrategy.PROGRAMMATIC,
                        requirements=ProgrammaticRequirements(
                            spec=TypographySpec(headline="An idea")
                        ),
                        rationale="type always renders",
                    )
                ],
                budget=Budget(max_cost_usd=0.4, max_latency_seconds=180.0),
            )
        ],
    )


def _transcript_of(graph: SceneGraph) -> Transcript:
    return Transcript(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=graph.project_id,
        recording_id=new_id(IdPrefix.RECORDING),
        language="en",
        segments=[
            TranscriptSegment(span=scene.span, text=scene.narration)
            for scene in graph.scenes
        ],
        provider="test",
        model="test",
    )


class StubsAreIdentifiable(unittest.TestCase):
    """A stubbed artefact must be identifiable in the database forever."""

    def test_stub_results_carry_a_stub_provider_name(self) -> None:
        with TemporaryDirectory(prefix="vtv-test-stub-") as directory:
            storage = LocalStorageProvider(Path(directory))
            provider = StubImageGenerationProvider(storage=storage)
            router = GenerationRouter(events=EventSink())
            router.register(provider, GenerationKind.IMAGE)
            result = run(router.generate(image_request()))
        self.assertTrue(result.provider.startswith("stub-"))
        self.assertTrue(result.outputs)


if __name__ == "__main__":
    unittest.main()
