"""Stage 7 — the generation router.

Caching, selection, retry, fallback and budget enforcement are implemented once
here so that every provider behaves identically. These tests are what keep that
true.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.testing import (
    FailingProvider,
    StubImageGenerationProvider,
)
from vtv.contracts.base import Budget
from vtv.contracts.errors import (
    ErrorCode,
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
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.generation import CIRCUIT_THRESHOLD, GenerationRouter
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth


def run(coro):
    return asyncio.run(coro)


def image_request(prompt: str = "a silicon die, macro", **kwargs) -> GenerationRequest:
    return GenerationRequest(
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
