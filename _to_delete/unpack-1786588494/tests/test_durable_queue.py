"""Stages 27 and 28 — durability, retries and disaster recovery for jobs.

Every test here runs against a real SQLite file on disk. The restart tests do
the thing that actually matters: they throw the queue object away and build a
new one on the same path, which is the closest a single process can come to
proving that a crashed worker's backlog survives.

The concurrency test is the one that would catch the expensive bug. A queue that
lets two workers claim the same job renders the same video twice and bills for
both, and the only way to know it does not is to run two workers against one
database and count.
"""

from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from vtv.adapters.queue.durable import (
    DurableJobQueue,
    JobState,
    backoff_seconds,
    deterministic_jitter,
    no_jitter,
    postgres_schema,
)
from vtv.contracts.errors import Status, VTVError
from vtv.observability.events import EventSink
from vtv.ports.jobs import JobPriority, JobQueue


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class QueueTestCase(unittest.TestCase):
    """One temporary directory per test; the database file is shared within it."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-durable-")
        self.path = Path(self._dir.name) / "jobs.db"
        self.events = EventSink()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def queue(self, **kwargs: Any) -> DurableJobQueue:
        options: dict[str, Any] = {
            "events": self.events,
            # Tests must not wait real seconds for a retry.
            "base_backoff_seconds": 0.01,
            "max_backoff_seconds": 0.05,
            "jitter": no_jitter,
            "heartbeat_interval_seconds": 0.05,
        }
        options.update(kwargs)
        return DurableJobQueue(self.path, **options)


class SatisfiesThePort(QueueTestCase):
    def test_it_is_a_job_queue(self) -> None:
        """The point of the port is that wiring can swap the two implementations."""
        self.assertIsInstance(self.queue(), JobQueue)


class Durability(QueueTestCase):
    def test_a_job_survives_the_process_that_enqueued_it(self) -> None:
        """The whole reason this class exists.

        The first queue never runs the job — it is discarded, as a crashing
        process would be. A second queue on the same file must still find it.
        """
        first = self.queue()
        handle = run(first.enqueue(kind="render", payload={"n": 1}))
        del first

        second = self.queue()
        done: list[int] = []

        async def handler(payload: dict[str, Any]) -> None:
            done.append(int(payload["n"]))

        second.register("render", handler)
        run(second.drain(timeout=5.0))

        self.assertEqual(done, [1])
        self.assertIs(run(second.status(handle.job_id)).status, Status.READY)

    def test_work_interrupted_mid_flight_is_reclaimed(self) -> None:
        """A worker that dies holding a job must not strand it forever.

        Simulated by writing the row into the state a crash leaves behind — a
        stale `running` row claimed by a worker that no longer exists — because
        killing a real process is not available inside a unit test.
        """
        queue = self.queue(reclaim_after_seconds=0.0)
        handle = run(queue.enqueue(kind="render", payload={"n": 7}))

        with sqlite3.connect(self.path, isolation_level=None) as connection:
            connection.execute(
                "UPDATE jobs SET state = ?, claimed_by = ?, heartbeat_at = 0 "
                "WHERE job_id = ?",
                (JobState.RUNNING.value, "worker-that-died", handle.job_id),
            )

        recovered, dead = run(queue.recover())
        self.assertEqual((recovered, dead), (1, 0))

        seen: list[int] = []

        async def handler(payload: dict[str, Any]) -> None:
            seen.append(int(payload["n"]))

        queue.register("render", handler)
        run(queue.drain(timeout=5.0))
        self.assertEqual(seen, [7])

    def test_a_crashed_job_past_its_attempts_goes_straight_to_dead_letter(self) -> None:
        """Recovery must not resurrect a job that has already failed enough."""
        queue = self.queue(max_attempts=1, reclaim_after_seconds=0.0)
        handle = run(queue.enqueue(kind="render", payload={}))
        with sqlite3.connect(self.path, isolation_level=None) as connection:
            connection.execute(
                "UPDATE jobs SET state = ?, claimed_by = ?, heartbeat_at = 0, "
                "attempts = 1 WHERE job_id = ?",
                (JobState.RUNNING.value, "gone", handle.job_id),
            )

        recovered, dead = run(queue.recover())
        self.assertEqual((recovered, dead), (0, 1))
        self.assertEqual(len(run(queue.dead_letters())), 1)


class ExactlyOneWorkerRunsEachJob(QueueTestCase):
    def test_concurrent_workers_never_double_execute(self) -> None:
        """Two workers, one database, forty jobs, each run exactly once.

        The claim is a single guarded UPDATE, so SQLite arbitrates rather than
        this process. A `SELECT` followed by an `UPDATE` would pass a lazier
        test and fail this one.
        """
        queue = self.queue()
        expected = {f"job-{index}" for index in range(40)}
        for name in sorted(expected):
            run(queue.enqueue(kind="work", payload={"name": name}))

        executions: list[str] = []

        async def handler(payload: dict[str, Any]) -> None:
            # Yield inside the handler so the workers genuinely interleave.
            await asyncio.sleep(0)
            executions.append(str(payload["name"]))

        queue.register("work", handler)
        run(queue.drain(timeout=20.0, workers=4))

        self.assertEqual(len(executions), 40, "a job ran twice or not at all")
        self.assertEqual(set(executions), expected)

    def test_two_queue_objects_on_one_file_share_the_backlog(self) -> None:
        """Cross-instance claiming, which is what multi-process deployment is."""
        producer = self.queue()
        for index in range(10):
            run(producer.enqueue(kind="work", payload={"index": index}))

        counts = {"a": 0, "b": 0}

        def make(name: str) -> Any:
            async def handler(_: dict[str, Any]) -> None:
                await asyncio.sleep(0)
                counts[name] += 1

            return handler

        first = self.queue(recover_on_start=False)
        second = self.queue(recover_on_start=False)
        first.register("work", make("a"))
        second.register("work", make("b"))

        async def both() -> None:
            await asyncio.gather(
                first.drain(timeout=10.0), second.drain(timeout=10.0)
            )

        run(both())
        self.assertEqual(counts["a"] + counts["b"], 10)


class RetriesAndDeadLetters(QueueTestCase):
    def test_a_failing_job_retries_then_dead_letters_with_its_error(self) -> None:
        queue = self.queue(max_attempts=3)
        attempts: list[int] = []

        async def handler(_: dict[str, Any]) -> None:
            attempts.append(1)
            raise VTVError("the provider refused politely")

        queue.register("flaky", handler)
        handle = run(queue.enqueue(kind="flaky", payload={}))
        run(queue.drain(timeout=10.0))

        self.assertEqual(len(attempts), 3, "did not retry exactly max_attempts times")
        final = run(queue.status(handle.job_id))
        self.assertIs(final.status, Status.FAILED)

        letters = run(queue.dead_letters())
        self.assertEqual(len(letters), 1)
        self.assertIsNotNone(letters[0].error)
        assert letters[0].error is not None
        # The message must survive, or an operator has nothing to debug with.
        self.assertIn("refused", (letters[0].error.message or "").lower())

    def test_a_job_that_succeeds_on_its_second_attempt_is_not_dead_lettered(self) -> None:
        queue = self.queue(max_attempts=3)
        calls = {"n": 0}

        async def handler(_: dict[str, Any]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise VTVError("transient")

        queue.register("flaky", handler)
        handle = run(queue.enqueue(kind="flaky", payload={}))
        run(queue.drain(timeout=10.0))

        self.assertEqual(calls["n"], 2)
        self.assertIs(run(queue.status(handle.job_id)).status, Status.READY)
        self.assertEqual(run(queue.dead_letters()), [])

    def test_backoff_grows_and_is_capped(self) -> None:
        delays = [
            backoff_seconds(
                attempt,
                base=1.0,
                factor=2.0,
                cap=10.0,
            )
            for attempt in range(1, 7)
        ]
        self.assertEqual(delays[:4], [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(delays[4:], [10.0, 10.0])
        self.assertTrue(all(delay <= 10.0 for delay in delays))

    def test_jitter_is_deterministic_per_job(self) -> None:
        """Spread retries without making the schedule impossible to reason about."""
        first = deterministic_jitter("job-a", 2, 4.0)
        again = deterministic_jitter("job-a", 2, 4.0)
        other = deterministic_jitter("job-b", 2, 4.0)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        for value in (first, other):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 4.0)

    def test_replay_returns_a_dead_letter_to_the_queue(self) -> None:
        queue = self.queue(max_attempts=1)
        fail = {"yes": True}

        async def handler(_: dict[str, Any]) -> None:
            if fail["yes"]:
                raise VTVError("nope")

        queue.register("flaky", handler)
        handle = run(queue.enqueue(kind="flaky", payload={}))
        run(queue.drain(timeout=10.0))
        self.assertEqual(len(run(queue.dead_letters())), 1)

        fail["yes"] = False
        replayed = run(queue.replay(handle.job_id))
        self.assertIs(replayed.status, Status.PENDING)
        run(queue.drain(timeout=10.0))

        self.assertIs(run(queue.status(handle.job_id)).status, Status.READY)
        self.assertEqual(run(queue.dead_letters()), [])

    def test_replaying_a_live_job_is_refused(self) -> None:
        queue = self.queue()
        handle = run(queue.enqueue(kind="work", payload={}))
        with self.assertRaises(VTVError):
            run(queue.replay(handle.job_id))

    def test_replaying_an_unknown_job_is_refused(self) -> None:
        queue = self.queue()
        with self.assertRaises(VTVError):
            run(queue.replay("job_does_not_exist"))


class Idempotency(QueueTestCase):
    def test_the_same_key_returns_the_same_job(self) -> None:
        queue = self.queue()
        first = run(queue.enqueue(kind="work", payload={"a": 1}, idempotency_key="k"))
        second = run(queue.enqueue(kind="work", payload={"a": 2}, idempotency_key="k"))
        self.assertEqual(first.job_id, second.job_id)

    def test_idempotency_survives_a_restart(self) -> None:
        """The in-memory version of this check is worthless across processes.

        A double-clicked button that reaches two API workers must not pay for
        two renders, so the guarantee has to live in the database.
        """
        first = self.queue()
        original = run(
            first.enqueue(kind="render", payload={}, idempotency_key="project-42")
        )
        del first

        second = self.queue()
        repeat = run(
            second.enqueue(kind="render", payload={}, idempotency_key="project-42")
        )
        self.assertEqual(repeat.job_id, original.job_id)

        executed: list[int] = []

        async def handler(_: dict[str, Any]) -> None:
            executed.append(1)

        second.register("render", handler)
        run(second.drain(timeout=5.0))
        self.assertEqual(len(executed), 1, "the same work ran twice")

    def test_different_keys_are_different_jobs(self) -> None:
        queue = self.queue()
        first = run(queue.enqueue(kind="work", payload={}, idempotency_key="a"))
        second = run(queue.enqueue(kind="work", payload={}, idempotency_key="b"))
        self.assertNotEqual(first.job_id, second.job_id)


class Ordering(QueueTestCase):
    def test_interactive_work_does_not_queue_behind_batch(self) -> None:
        """A user watching a spinner must not wait on someone's bulk re-render."""
        queue = self.queue()
        run(queue.enqueue(kind="work", payload={"tag": "batch"},
                          priority=JobPriority.BATCH))
        run(queue.enqueue(kind="work", payload={"tag": "standard"},
                          priority=JobPriority.STANDARD))
        run(queue.enqueue(kind="work", payload={"tag": "interactive"},
                          priority=JobPriority.INTERACTIVE))

        order: list[str] = []

        async def handler(payload: dict[str, Any]) -> None:
            order.append(str(payload["tag"]))

        queue.register("work", handler)
        # One worker, so the order observed is the order claimed.
        run(queue.drain(timeout=10.0, workers=1))
        self.assertEqual(order, ["interactive", "standard", "batch"])

    def test_equal_priority_is_first_in_first_out(self) -> None:
        queue = self.queue()
        for index in range(5):
            run(queue.enqueue(kind="work", payload={"index": index}))

        order: list[int] = []

        async def handler(payload: dict[str, Any]) -> None:
            order.append(int(payload["index"]))

        queue.register("work", handler)
        run(queue.drain(timeout=10.0, workers=1))
        self.assertEqual(order, [0, 1, 2, 3, 4])

    def test_a_delayed_job_is_not_claimed_early(self) -> None:
        queue = self.queue()
        run(queue.enqueue(kind="work", payload={}, delay_seconds=30.0))
        queue.register("work", lambda _: asyncio.sleep(0))
        self.assertIsNone(run(queue.run_once()))


class Cancellation(QueueTestCase):
    def test_a_pending_job_is_cancelled_outright(self) -> None:
        queue = self.queue()
        handle = run(queue.enqueue(kind="work", payload={}))
        run(queue.cancel(handle.job_id))

        ran: list[int] = []

        async def handler(_: dict[str, Any]) -> None:
            ran.append(1)

        queue.register("work", handler)
        self.assertIsNone(run(queue.run_once()))
        self.assertEqual(ran, [])
        # The port's vocabulary has no CANCELLED, so it surfaces as FAILED with
        # a cancellation error; the precise state stays in the row for stats().
        final = run(queue.status(handle.job_id))
        self.assertIs(final.status, Status.FAILED)
        self.assertIsNotNone(final.error)
        self.assertEqual(
            run(queue.stats()).get(JobState.CANCELLED.value), 1
        )

    def test_cancelling_an_unknown_job_is_refused(self) -> None:
        queue = self.queue()
        with self.assertRaises(VTVError):
            run(queue.cancel("job_nope"))


class Limits(QueueTestCase):
    def test_an_oversized_payload_is_refused(self) -> None:
        """Bound what goes in the row rather than discovering it at read time."""
        queue = self.queue(max_payload_bytes=256)
        with self.assertRaises(VTVError):
            run(queue.enqueue(kind="work", payload={"blob": "x" * 4096}))

    def test_a_payload_that_will_not_serialise_is_refused(self) -> None:
        queue = self.queue()
        with self.assertRaises(VTVError):
            run(queue.enqueue(kind="work", payload={"when": object()}))

    def test_status_of_an_unknown_job_is_refused(self) -> None:
        queue = self.queue()
        with self.assertRaises(VTVError):
            run(queue.status("job_missing"))


class Observability(QueueTestCase):
    def test_stats_count_every_state(self) -> None:
        queue = self.queue(max_attempts=1)

        async def ok(_: dict[str, Any]) -> None:
            return None

        async def bad(_: dict[str, Any]) -> None:
            raise VTVError("no")

        queue.register("ok", ok)
        queue.register("bad", bad)
        run(queue.enqueue(kind="ok", payload={}))
        run(queue.enqueue(kind="bad", payload={}))
        cancelled = run(queue.enqueue(kind="ok", payload={"x": 1}))
        run(queue.cancel(cancelled.job_id))
        run(queue.drain(timeout=10.0))

        stats = run(queue.stats())
        self.assertEqual(stats.get(JobState.SUCCEEDED.value), 1)
        self.assertEqual(stats.get(JobState.DEAD_LETTER.value), 1)
        self.assertEqual(stats.get(JobState.CANCELLED.value), 1)

    def test_lifecycle_events_are_emitted(self) -> None:
        seen: list[str] = []
        self.events.subscribe(lambda event: seen.append(event.name.value))
        queue = self.queue()

        async def handler(_: dict[str, Any]) -> None:
            return None

        queue.register("work", handler)
        run(queue.enqueue(kind="work", payload={}))
        run(queue.drain(timeout=5.0))

        for name in ("job.enqueued", "job.started", "job.completed"):
            self.assertIn(name, seen)

    def test_postgres_schema_is_offered_for_the_real_deployment(self) -> None:
        """SQLite is the single-node story. The migration must not be guesswork.

        This asserts the DDL exists and names the same columns; it has NOT been
        executed against a PostgreSQL server in this environment.
        """
        ddl = postgres_schema()
        for column in ("job_id", "state", "priority", "attempts", "idempotency_key"):
            self.assertIn(column, ddl)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
