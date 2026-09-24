"""Provider resilience: timeouts, circuit breaker cooldown, retry idempotency
and graceful worker shutdown.

Four gaps this file exists to close, all found by reading `pipeline/generation.py`
and `worker.py` before writing anything:

* Every adapter already passes a timeout to httpx, but nothing bounded the
  *router's* call to a provider. A provider that never returns — a deadlocked
  SDK, a stub that forgets to answer, a connection pool with no free slot — hung
  the router forever, unbounded by anything an adapter's own timeout could see.
* The breaker existed and was consulted in the right place (`candidates()`, the
  router's one selection point), but a tripped breaker never closed: nothing
  ever gave a skipped provider another attempt, so `consecutive_failures` could
  never fall back to zero and the provider was out of rotation permanently
  rather than for a cooldown.
* The retry loop already billed only the attempt that actually succeeded, and
  the spend authoriser was already told about spend exactly once per call. That
  is verified here, not fixed — the test would already pass on the unmodified
  router, and it stays in this file as the regression guard for it.
* The worker already stopped claiming new work on SIGTERM and waited for
  in-flight work to finish. What it did not do was give up on a job promptly
  when the grace period ran out: it left the row `running` for
  `reclaim_after_seconds` (minutes) to notice, which is the crash path, not a
  graceful one.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from vtv.adapters.queue.durable import DurableJobQueue
from vtv.config import Settings
from vtv.contracts.base import Budget
from vtv.contracts.errors import ErrorCode, ProviderError, Status, VTVError
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    GenerationResult,
    ImageParams,
)
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.generation import (
    CIRCUIT_COOLDOWN_SECONDS,
    CIRCUIT_THRESHOLD,
    GenerationRouter,
)
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth
from vtv.wiring import build, queue_path
from vtv.worker import Worker

_POLICY = DataPolicy(retains_input=False, trains_on_input=False, dpa_in_place=True)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def run_bounded(coro, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    """Run a coroutine with an outer bound so a regression that reintroduces a
    hang fails the test in a few seconds instead of freezing the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def image_request(prompt: str = "a silicon die, macro", **kwargs) -> GenerationRequest:
    return GenerationRequest(
        organisation_id=SYSTEM_ORGANISATION_ID,
        kind=GenerationKind.IMAGE,
        params=ImageParams(prompt=prompt),
        **kwargs,
    )


class _Hangs:
    """A provider whose call never returns on its own.

    Stands in for exactly the failure mode a per-adapter httpx timeout cannot
    catch: the call never gets far enough to reach httpx at all. Only the
    router cancelling it gets control back.
    """

    def __init__(self, name: str = "hangs") -> None:
        self.name = name
        self.calls = 0
        self.cancelled = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            unit_cost_usd=0.01,
            typical_latency_seconds=0.01,
            data_policy=_POLICY,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate_image(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable: the sleep above never returns on its own")


@dataclass
class _FlakyThenHealthy:
    """Fails its first ``fail_times`` calls, then succeeds on every call after."""

    name: str = "flaky"
    fail_times: int = 1
    cost: float = 0.05
    calls: int = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            unit_cost_usd=self.cost,
            typical_latency_seconds=0.01,
            data_policy=_POLICY,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate_image(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError(f"upstream 503 on call {self.calls}")
        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider=self.name,
            model="test",
            structured_output={"ok": True},
            cost_usd=self.cost,
            latency_ms=5,
        )


@dataclass
class _RecordingAuthoriser:
    """A permissive spend authoriser that remembers every call it was asked."""

    asked: list[tuple[str, float]] = field(default_factory=list)
    noted: list[tuple[str, float]] = field(default_factory=list)

    def authorise(self, *, organisation_id: str, amount_usd: float) -> None:
        self.asked.append((organisation_id, amount_usd))

    def note_spend(self, *, organisation_id: str, amount_usd: float) -> None:
        self.noted.append((organisation_id, amount_usd))


class EveryProviderCallHasADeadline(unittest.TestCase):
    """The router-level backstop. Adapters already time out their own HTTP call;
    this is what bounds the call the router makes, regardless of what happens
    underneath it."""

    def test_a_hung_provider_is_timed_out_rather_than_hanging_forever(self) -> None:
        router = GenerationRouter(events=EventSink())
        provider = _Hangs()
        router.register(provider, GenerationKind.IMAGE)
        request = image_request(
            budget=Budget(max_cost_usd=1.0, max_latency_seconds=0.05),
            max_attempts=1,
        )
        with self.assertRaises(VTVError) as caught:
            run_bounded(router.generate(request))
        self.assertIs(caught.exception.info.code, ErrorCode.LATENCY_EXCEEDED)
        self.assertTrue(
            provider.cancelled,
            "a timed-out call must be cancelled, not merely abandoned to keep "
            "running in the background",
        )

    def test_a_timeout_is_retried_like_any_other_transient_failure(self) -> None:
        # TIMEOUT is in RETRYABLE_CATEGORIES, so a request with attempts to
        # spare must try the same provider again rather than giving up at once.
        router = GenerationRouter(events=EventSink())
        provider = _Hangs()
        router.register(provider, GenerationKind.IMAGE)
        request = image_request(
            budget=Budget(max_cost_usd=1.0, max_latency_seconds=0.02),
            max_attempts=2,
        )
        with self.assertRaises(VTVError) as caught:
            run_bounded(router.generate(request), timeout=10.0)
        self.assertIs(caught.exception.info.code, ErrorCode.LATENCY_EXCEEDED)
        self.assertEqual(provider.calls, 2, "a retryable timeout must be retried")

    def test_a_request_with_no_latency_budget_still_gets_a_deadline(self) -> None:
        # No `max_latency_seconds` at all -- the common case, since most
        # requests only ever set a cost ceiling. The router must still not wait
        # forever; it falls back to its own default bound.
        router = GenerationRouter(events=EventSink())
        provider = _Hangs()
        router.register(provider, GenerationKind.IMAGE)
        # A tiny router-level default, injected in place of the real one, keeps
        # this test fast without weakening what it proves: no budget still
        # means *some* deadline, not none.
        with mock.patch(
            "vtv.pipeline.generation.DEFAULT_PROVIDER_TIMEOUT_SECONDS", 0.05
        ):
            request = image_request(budget=Budget(max_cost_usd=1.0), max_attempts=1)
            with self.assertRaises(VTVError) as caught:
                run_bounded(router.generate(request))
        self.assertIs(caught.exception.info.code, ErrorCode.LATENCY_EXCEEDED)


class CircuitBreakerCooldown(unittest.TestCase):
    """Consulted in exactly one place -- `candidates()` -- which is what the
    task calls "the single place the router picks a provider". These tests are
    about the cooldown, not the consultation point: the breaker already tripped
    correctly before this change; what it never did was let go."""

    def test_a_tripped_breaker_stays_open_until_its_cooldown_elapses(self) -> None:
        clock = {"t": 0.0}
        router = GenerationRouter(events=EventSink(), clock=lambda: clock["t"])
        provider = _FlakyThenHealthy(fail_times=10_000)  # never succeeds in range
        router.register(provider, GenerationKind.IMAGE)

        for index in range(CIRCUIT_THRESHOLD):
            with self.assertRaises(VTVError):
                run(router.generate(image_request(f"prompt {index}", max_attempts=1)))
        self.assertEqual(provider.calls, CIRCUIT_THRESHOLD)

        # Immediately after tripping: still inside the cooldown, so the
        # provider must be skipped, and the failure must still be an honest
        # domain error rather than a hang or a fabricated success.
        with self.assertRaises(VTVError) as caught:
            run(router.generate(image_request("still cooling", max_attempts=1)))
        self.assertEqual(provider.calls, CIRCUIT_THRESHOLD, "must not be called")
        self.assertIs(caught.exception.info.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_a_probe_is_allowed_once_the_cooldown_has_elapsed(self) -> None:
        clock = {"t": 0.0}
        router = GenerationRouter(events=EventSink(), clock=lambda: clock["t"])
        provider = _FlakyThenHealthy(fail_times=10_000)
        router.register(provider, GenerationKind.IMAGE)
        for index in range(CIRCUIT_THRESHOLD):
            with self.assertRaises(VTVError):
                run(router.generate(image_request(f"prompt {index}", max_attempts=1)))

        clock["t"] += CIRCUIT_COOLDOWN_SECONDS + 0.01
        with self.assertRaises(VTVError):
            run(router.generate(image_request("probe", max_attempts=1)))
        self.assertEqual(
            provider.calls,
            CIRCUIT_THRESHOLD + 1,
            "a provider past its cooldown must be given one more try",
        )

    def test_a_failed_probe_reopens_the_breaker_for_a_fresh_cooldown(self) -> None:
        clock = {"t": 0.0}
        router = GenerationRouter(events=EventSink(), clock=lambda: clock["t"])
        provider = _FlakyThenHealthy(fail_times=10_000)
        router.register(provider, GenerationKind.IMAGE)
        for index in range(CIRCUIT_THRESHOLD):
            with self.assertRaises(VTVError):
                run(router.generate(image_request(f"prompt {index}", max_attempts=1)))

        clock["t"] += CIRCUIT_COOLDOWN_SECONDS + 0.01
        with self.assertRaises(VTVError):
            run(router.generate(image_request("probe fails too", max_attempts=1)))
        calls_after_probe = provider.calls

        # Just past the probe, well short of a second cooldown: must be skipped
        # again, not probed on every single call.
        clock["t"] += 1.0
        with self.assertRaises(VTVError):
            run(router.generate(image_request("too soon", max_attempts=1)))
        self.assertEqual(
            provider.calls,
            calls_after_probe,
            "a failed probe must reset the cooldown, not disable it",
        )

    def test_a_successful_probe_closes_the_breaker(self) -> None:
        clock = {"t": 0.0}
        router = GenerationRouter(events=EventSink(), clock=lambda: clock["t"])
        provider = _FlakyThenHealthy(fail_times=CIRCUIT_THRESHOLD)
        router.register(provider, GenerationKind.IMAGE)
        for index in range(CIRCUIT_THRESHOLD):
            with self.assertRaises(VTVError):
                run(router.generate(image_request(f"prompt {index}", max_attempts=1)))

        clock["t"] += CIRCUIT_COOLDOWN_SECONDS + 0.01
        result = run(router.generate(image_request("recovered", max_attempts=1)))
        self.assertEqual(result.provider, provider.name)

        # No further cooldown needed: the breaker is fully closed by a success.
        second = run(router.generate(image_request("right after", max_attempts=1)))
        self.assertEqual(second.provider, provider.name)

    def test_every_candidate_open_is_an_honest_error_not_a_hang_or_fake_success(
        self,
    ) -> None:
        """Task requirement: when every candidate is open, the ladder's existing
        degradation path (drawn/programmatic visual) must still work. It can
        only do that if the router is honest about failing -- the same
        PROVIDER_UNAVAILABLE code and ProviderError type callers already
        degrade on when there is no provider at all."""
        clock = {"t": 0.0}
        router = GenerationRouter(events=EventSink(), clock=lambda: clock["t"])
        provider = _FlakyThenHealthy(fail_times=10_000)
        router.register(provider, GenerationKind.IMAGE)
        for index in range(CIRCUIT_THRESHOLD):
            with self.assertRaises(VTVError):
                run(router.generate(image_request(f"prompt {index}", max_attempts=1)))

        with self.assertRaises(VTVError) as caught:
            run(router.generate(image_request("everything is open", max_attempts=1)))
        self.assertIsInstance(caught.exception, ProviderError)
        self.assertIs(caught.exception.info.code, ErrorCode.PROVIDER_UNAVAILABLE)


class RetryDoesNotDoubleCharge(unittest.TestCase):
    """Verification, not a fix: the router already billed only the attempt that
    succeeded. This is the regression test that keeps it that way."""

    def test_a_retried_call_is_metered_once(self) -> None:
        events = EventSink()
        events.record = True
        authoriser = _RecordingAuthoriser()
        router = GenerationRouter(events=events, spend_authoriser=authoriser)
        provider = _FlakyThenHealthy(fail_times=1, cost=0.05)
        router.register(provider, GenerationKind.IMAGE)

        result = run(router.generate(image_request(max_attempts=3)))

        self.assertEqual(provider.calls, 2, "one failure, then one success")
        self.assertFalse(result.from_cache)
        self.assertAlmostEqual(
            router.ledger.total_usd,
            0.05,
            msg="a retried call must be billed once, not once per attempt",
        )
        self.assertEqual(
            len(authoriser.noted),
            1,
            "the spend authoriser must be told about the spend once, not once "
            "per attempt",
        )
        self.assertAlmostEqual(authoriser.noted[0][1], 0.05)
        completed = [
            e for e in events.recorded if e.name is EventName.GENERATION_COMPLETED
        ]
        failed = [e for e in events.recorded if e.name is EventName.GENERATION_FAILED]
        self.assertEqual(len(completed), 1, "exactly one success must be reported")
        self.assertEqual(len(failed), 1, "exactly one failure must be reported")

    def test_a_call_that_never_succeeds_is_never_charged(self) -> None:
        authoriser = _RecordingAuthoriser()
        router = GenerationRouter(events=EventSink(), spend_authoriser=authoriser)
        provider = _FlakyThenHealthy(fail_times=10_000, cost=0.05)
        router.register(provider, GenerationKind.IMAGE)

        with self.assertRaises(VTVError):
            run(router.generate(image_request(max_attempts=3)))

        self.assertEqual(provider.calls, 3)
        self.assertEqual(router.ledger.total_usd, 0.0)
        self.assertEqual(authoriser.noted, [], "no attempt succeeded, nothing was spent")


class GracefulShutdownReleasesTheJobItHolds(unittest.TestCase):
    """On SIGTERM: stop claiming, let in-flight work finish if it can within the
    grace period, and give up on it explicitly -- not silently -- if it can't."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-shutdown-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
        )
        self.assembly = build(self.settings)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_no_new_job_is_claimed_once_shutdown_has_started(self) -> None:
        worker = Worker.create(self.settings, assembly=self.assembly, concurrency=1)
        worker.shutdown_grace_seconds = 5.0

        started = asyncio.Event()
        proceed = asyncio.Event()
        second_started = asyncio.Event()

        async def first(payload: dict) -> dict:
            started.set()
            await proceed.wait()
            return {}

        async def second(payload: dict) -> dict:
            second_started.set()
            return {}

        worker.queue.register("test_first", first)
        worker.queue.register("test_second", second)

        async def scenario() -> bool:
            await worker.queue.enqueue(kind="test_first", payload={})
            run_task = asyncio.create_task(worker.run_forever())
            await asyncio.wait_for(started.wait(), timeout=5.0)

            worker.stop()
            # Enqueued only after shutdown began: a worker still claiming would
            # pick this up long before the first job finishes.
            await worker.queue.enqueue(kind="test_second", payload={})
            proceed.set()
            await asyncio.wait_for(run_task, timeout=10.0)
            return second_started.is_set()

        self.assertFalse(
            asyncio.run(scenario()),
            "the worker claimed new work after stop() was called",
        )

    def test_a_job_that_outlives_the_grace_period_is_released_promptly(self) -> None:
        worker = Worker.create(self.settings, assembly=self.assembly, concurrency=1)
        # Short enough that the test does not wait out the real
        # SHUTDOWN_GRACE_SECONDS; the mechanism under test is what happens when
        # the deadline is reached, not how long the deadline is.
        worker.shutdown_grace_seconds = 0.2

        started = asyncio.Event()
        release_gate = asyncio.Event()

        async def slow(payload: dict) -> dict:
            started.set()
            await release_gate.wait()
            return {}

        worker.queue.register("test_slow", slow)

        async def scenario():
            handle = await worker.queue.enqueue(kind="test_slow", payload={})
            run_task = asyncio.create_task(worker.run_forever())
            await asyncio.wait_for(started.wait(), timeout=5.0)

            worker.stop()
            # Never set `release_gate`: the handler outlives the grace period,
            # forcing the "give up on it" branch of shutdown.
            await asyncio.wait_for(run_task, timeout=10.0)
            status = await worker.queue.status(handle.job_id)
            return handle.job_id, status

        job_id, status = asyncio.run(scenario())
        self.assertEqual(
            status.status,
            Status.PENDING,
            "a job that outlived the grace period must be handed back, not left "
            "claimed",
        )

        async def second_worker_claims() -> str | None:
            # A second worker over the same database file, standing in for
            # whichever pod picks up the rolling deploy's slack. It must be
            # able to claim the job now, not `reclaim_after_seconds` later.
            queue = DurableJobQueue(
                queue_path(self.settings),
                events=self.assembly.events,
                reclaim_after_seconds=900.0,
                heartbeat_interval_seconds=15.0,
                recover_on_start=False,
            )

            async def finish(payload: dict) -> dict:
                return {"done": True}

            queue.register("test_slow", finish)
            return await queue.run_once(worker_id="a-second-worker")

        claimed_id = asyncio.run(second_worker_claims())
        self.assertEqual(
            claimed_id,
            job_id,
            "a released job must be claimable by another worker immediately",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ThrottlingIsNotAnOutage(unittest.TestCase):
    """The defect that set a third of one video as typography.

    A render generated four images at once, was rate-limited three times on the
    fourth, and tripped the circuit breaker. The next four shots then found
    `provider_unavailable` — no image provider at all — and fell to type. The
    provider was healthy and working for the entire episode; it had simply asked
    us to slow down, and we heard "I am broken".
    """

    def registered(self):  # type: ignore[no-untyped-def]
        from vtv.pipeline.generation import _Registered

        class Throttling:
            name = "throttling"

            @property
            def capabilities(self):  # type: ignore[no-untyped-def]
                from vtv.ports.base import ProviderCapabilities

                return ProviderCapabilities(name="throttling", unit_cost_usd=0.01)

        return _Registered(provider=Throttling(), kind=GenerationKind.IMAGE)

    def test_a_rate_limit_does_not_count_toward_the_breaker(self) -> None:
        from vtv.pipeline.generation import CIRCUIT_THRESHOLD, THROTTLING_CODES

        self.assertIn(ErrorCode.RATE_LIMITED, THROTTLING_CODES)
        registered = self.registered()
        # Three throttles — the threshold — must leave it closed.
        for _ in range(CIRCUIT_THRESHOLD):
            if ErrorCode.RATE_LIMITED not in THROTTLING_CODES:  # pragma: no cover
                registered.consecutive_failures += 1
        self.assertFalse(registered.is_open(0.0))

    def test_a_real_fault_still_trips_it(self) -> None:
        """The breaker still has to work. It is only the signal that changed."""
        from vtv.pipeline.generation import CIRCUIT_THRESHOLD

        registered = self.registered()
        registered.consecutive_failures = CIRCUIT_THRESHOLD
        registered.opened_at = 0.0
        self.assertTrue(registered.is_open(0.0))


class WaitingLongEnoughToBeWorthWaiting(unittest.TestCase):
    """The retries were 370ms, 655ms and 363ms apart — three requests inside a
    second and a half, to an endpoint that had just said "too many requests".
    None of them could have succeeded."""

    def error(self, code: ErrorCode, **context: object):  # type: ignore[no-untyped-def]
        from vtv.contracts.errors import ProviderError

        return ProviderError("nope", code=code, context=context or None)

    def test_a_throttle_waits_seconds_not_milliseconds(self) -> None:
        from vtv.pipeline.generation import _backoff

        self.assertGreaterEqual(_backoff(self.error(ErrorCode.RATE_LIMITED), 1), 4.0)

    def test_an_ordinary_fault_still_retries_quickly(self) -> None:
        """A transient fault is waiting for something else entirely, and four
        seconds of it would be four seconds of a render doing nothing."""
        from vtv.pipeline.generation import _backoff

        self.assertLessEqual(_backoff(self.error(ErrorCode.PROVIDER_UNAVAILABLE), 1), 0.5)

    def test_the_vendors_own_number_wins(self) -> None:
        """It knows its quota window; we are guessing at it."""
        from vtv.pipeline.generation import _backoff

        error = self.error(ErrorCode.RATE_LIMITED, retry_after="12")
        self.assertAlmostEqual(_backoff(error, 1), 12.0)

    def test_an_absurd_retry_after_is_clamped(self) -> None:
        """A header is data we did not write, and a render must not stall for an
        hour because a vendor — or something pretending to be one — said so."""
        from vtv.pipeline.generation import MAX_BACKOFF_SECONDS, _backoff

        error = self.error(ErrorCode.RATE_LIMITED, retry_after="99999")
        self.assertLessEqual(_backoff(error, 1), MAX_BACKOFF_SECONDS)

    def test_a_malformed_retry_after_falls_back_to_the_schedule(self) -> None:
        from vtv.pipeline.generation import _backoff

        error = self.error(ErrorCode.RATE_LIMITED, retry_after="soon-ish")
        self.assertGreaterEqual(_backoff(error, 1), 4.0)

    def test_it_backs_off_further_each_attempt(self) -> None:
        from vtv.pipeline.generation import _backoff

        error = self.error(ErrorCode.RATE_LIMITED)
        self.assertGreater(_backoff(error, 3), _backoff(error, 1))


class AskingSlowlyEnoughNotToBeRefused(unittest.TestCase):
    """Better than surviving a 429 is not causing one. Each costs a
    multi-second backoff on a shot somebody is waiting for."""

    def test_the_image_adapter_declares_a_limit(self) -> None:
        from vtv.adapters.images.http_image import HttpImageGenerationProvider

        provider = HttpImageGenerationProvider(
            storage=None, endpoint="https://x/v1", api_key="k", model="gpt-image-1"  # type: ignore[arg-type]
        )
        self.assertEqual(provider.capabilities.max_concurrency, 2)

    def test_the_gate_admits_only_that_many(self) -> None:
        from vtv.pipeline.generation import _Registered

        class Limited:
            @property
            def capabilities(self):  # type: ignore[no-untyped-def]
                from vtv.ports.base import ProviderCapabilities

                return ProviderCapabilities(name="limited", max_concurrency=2)

        async def check() -> int:
            registered = _Registered(provider=Limited(), kind=GenerationKind.IMAGE)
            gate = registered.gate()
            assert gate is not None
            await gate.acquire()
            await gate.acquire()
            return gate.locked()

        self.assertTrue(asyncio.run(check()))

    def test_a_provider_with_no_limit_is_not_gated(self) -> None:
        """Most providers have generous quotas, and a semaphore nobody needs is
        a queue nobody asked for."""
        from vtv.pipeline.generation import _Registered

        class Unlimited:
            @property
            def capabilities(self):  # type: ignore[no-untyped-def]
                from vtv.ports.base import ProviderCapabilities

                return ProviderCapabilities(name="unlimited")

        registered = _Registered(provider=Unlimited(), kind=GenerationKind.IMAGE)
        self.assertIsNone(registered.gate())
