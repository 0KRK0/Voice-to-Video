"""Stage 27 — the worker process.

`python -m vtv.worker`

This exists because of the audit finding that rendering ran inside the API's
event loop. A thirty-second render stalled every request on that process,
including the health check the load balancer uses to decide whether the process
is alive — so the system's response to load was to be marked unhealthy and
restarted, losing the render.

The split is the fix, and it is deliberately blunt:

* **The API may only enqueue.** It registers no handlers, so it cannot run work
  even by accident. `create_app` no longer imports the renderer.
* **The worker may only consume.** It serves no HTTP.
* **The queue is the only thing they share**, plus the database and object
  store they both address by reference.

Delivery is **at-least-once with idempotent effects**, not exactly-once. A
worker can complete a handler and die before the row is settled, and the job
will be redelivered; that is unavoidable without a distributed transaction
across the queue and the pipeline's side effects. What makes it safe is that
every effect converges: usage settlement is keyed on the render job id, the
output object is addressed by content, and persistence is an upsert.

Shutdown is cooperative. On SIGTERM the loop stops claiming new work and waits
for in-flight jobs, so a rolling deploy drains rather than abandons. A job that
still has not finished when the grace period runs out is handed back to the
queue explicitly (`DurableJobQueue.release_claimed`) rather than left `running`
for `reclaim_after_seconds` to notice — a deploy knows immediately that it is
giving up on the row, so the queue is told immediately, and the job is
available to the next worker in seconds rather than the minutes that window is
tuned for a genuine, silent crash. A worker that is killed outright (SIGKILL,
an OOM) gets no chance to run this path at all, which is exactly what
`reclaim_after_seconds` and `recover()` exist to catch — the same eventual
outcome, on the timer built for the case where nobody said anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vtv.adapters.queue.durable import RECLAIM_AFTER_SECONDS, DurableJobQueue
from vtv.adapters.repository.sqlite import SqliteProjectRepository
from vtv.config import Settings
from vtv.jobs import HANDLERS, JobContext, JobKind, SweepPayload
from vtv.observability.bridge import correlated_log_handler
from vtv.observability.context import correlated
from vtv.observability.events import EventName, configure_logging
from vtv.observability.trace import NullTrace
from vtv.wiring import Assembly, build, queue_path, repository_path

#: How often the worker asks for work when the queue was empty. Short enough
#: that a user does not notice, long enough that an idle fleet is not a
#: self-inflicted database load test.
IDLE_POLL_SECONDS = 0.25

#: How often abandoned work is reclaimed and expired reservations released.
#: Both are cheap; running them often means a crash costs seconds of quota
#: rather than the reservation TTL.
MAINTENANCE_INTERVAL_SECONDS = 30.0

#: Retention runs on a schedule here rather than behind an HTTP endpoint.
RETENTION_INTERVAL_SECONDS = 3600.0

#: How long a rolling deploy waits for in-flight work before giving up on it.
SHUTDOWN_GRACE_SECONDS = 120.0


@dataclass
class Worker:
    """Claims jobs, runs them, and shuts down without losing any."""

    settings: Settings
    assembly: Assembly
    repository: SqliteProjectRepository
    queue: DurableJobQueue
    concurrency: int = 2
    #: How long shutdown waits for in-flight jobs before giving up on them.
    #: A field rather than always reading the module constant so a test can
    #: shrink it and exercise the "gave up" branch in real time instead of
    #: waiting out `SHUTDOWN_GRACE_SECONDS` for real.
    shutdown_grace_seconds: float = SHUTDOWN_GRACE_SECONDS
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)
    _running: int = 0

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        assembly: Assembly | None = None,
        concurrency: int = 2,
    ) -> Worker:
        settings = settings or Settings.from_env()
        assembly = assembly or build(settings, log_events=True)
        repository = SqliteProjectRepository(repository_path(settings))
        queue = DurableJobQueue(
            queue_path(settings),
            events=assembly.events,
            max_attempts=3,
            # A render is minutes of work, so the reclaim window has to exceed
            # it or a healthy worker's job is stolen while it is still running.
            # The constant rather than the literal, because the API opens the
            # same queue and the two must agree.
            reclaim_after_seconds=RECLAIM_AFTER_SECONDS,
            heartbeat_interval_seconds=15.0,
        )
        worker = cls(
            settings=settings,
            assembly=assembly,
            repository=repository,
            queue=queue,
            concurrency=concurrency,
        )
        worker.register()
        return worker

    # -- registration -----------------------------------------------------

    def register(self) -> None:
        """Bind every job kind to its handler.

        The worker registers all of them. If a deployment should not run a kind
        of work, it runs a worker that does not register it — the queue leaves
        unclaimed kinds visibly waiting in `stats()` rather than failing them.
        """
        context = self.context()
        for kind, handler in HANDLERS.items():
            self.queue.register(kind, _bind(kind, handler, context))

    def _device_pool(self) -> Any:
        """The pool, built from what this worker already has.

        Constructed per call rather than held: it is a dataclass of four
        references with no state of its own, and a worker that cached one would
        be a second place to remember when the wiring changes.
        """
        from vtv.dispatch import DevicePool

        return DevicePool(
            queue=self.queue,
            repository=self.repository,
            storage=self.assembly.storage,
            directory=self.assembly.directory,
        )

    def context(self) -> JobContext:
        scratch = Path(self.settings.storage_root).parent / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        return JobContext(
            assembly=self.assembly,
            repository=self.repository,
            usage=self.assembly.usage,
            audit=self.assembly.audit,
            events=self.assembly.events,
            scratch=scratch,
            queue=self.queue,
        )

    # -- lifecycle --------------------------------------------------------

    async def run_forever(self) -> None:
        """The worker's whole life."""
        # The capabilities this worker actually has, printed at startup.
        #
        # The worker is a separate process reading the same configuration, and
        # nothing forces the two to agree. That is not hypothetical: a
        # transcription credential and its data-policy assertion were added to
        # `.env`, the API was restarted and reported `real_transcription: true`
        # — and the worker, still running from before the edit, refused every
        # recording with "no provider available for speech_to_text". `/health`
        # describes the API's providers; it says nothing about the process that
        # does the work, and the two disagreed for an hour with no way to see
        # it. This line is that way.
        self.assembly.events.emit(
            EventName.JOB_STARTED,
            project_id=None,
            data={
                "worker_id": self.queue.worker_id,
                "concurrency": self.concurrency,
                "kinds": sorted(HANDLERS),
                "capabilities": self.assembly.capabilities.as_dict(),
            },
        )
        # Reclaim anything a previous worker died holding, before taking new
        # work. Doing it first means a crash-restart loop still makes progress.
        recovered, dead = await self.queue.recover()
        if recovered or dead:
            self.assembly.events.emit(
                EventName.JOB_ENQUEUED,
                project_id=None,
                data={"recovered": recovered, "dead_lettered": dead},
            )

        # Recorded up front, not read off `_consume`'s frames, so shutdown can
        # address a consumer's claim without depending on that task still being
        # in a state where it would tell us. `run_once`'s own `worker_id`
        # default falls back to `self.queue.worker_id` (no suffix) when none is
        # passed, so consumers must always be given one of these explicitly —
        # otherwise a release here would not match what a claim was made under.
        consumer_worker_ids = [
            f"{self.queue.worker_id}#{index}" for index in range(max(1, self.concurrency))
        ]
        tasks = [
            asyncio.create_task(
                self._consume(worker_id), name=f"vtv-consumer-{index}"
            )
            for index, worker_id in enumerate(consumer_worker_ids)
        ]
        tasks.append(
            asyncio.create_task(
                self._correlated_maintenance(), name="vtv-maintenance"
            )
        )
        tasks.append(asyncio.create_task(self._retention(), name="vtv-retention"))

        try:
            await self._stopping.wait()
        finally:
            deadline = self.shutdown_grace_seconds
            while self._running and deadline > 0:
                step = min(0.5, deadline)
                await asyncio.sleep(step)
                deadline -= step
            if self._running:
                # The grace period ran out with a job still claimed. Give it
                # back explicitly instead of leaving it `running` for
                # `reclaim_after_seconds` to notice — see
                # `DurableJobQueue.release_claimed` for why that is safe even
                # though the handler task itself may keep running a little
                # longer after this.
                for worker_id in consumer_worker_ids:
                    with contextlib.suppress(Exception):
                        released = await self.queue.release_claimed(worker_id)
                        if released:
                            self.assembly.events.emit(
                                EventName.JOB_ENQUEUED,
                                project_id=None,
                                data={
                                    "worker_id": worker_id,
                                    "released_by_shutdown": released,
                                },
                            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        """Stop claiming. In-flight work is allowed to finish."""
        self._stopping.set()

    async def _consume(self, worker_id: str) -> None:
        while not self._stopping.is_set():
            try:
                self._running += 1
                claimed = await self.queue.run_once(worker_id=worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A handler that raises is already recorded by the queue. This
                # catches a failure in the claiming machinery itself, which must
                # not take the consumer down with it.
                self.assembly.events.emit(
                    EventName.JOB_FAILED,
                    project_id=None,
                    data={"worker_id": worker_id, "error": type(error).__name__},
                )
                claimed = None
                await asyncio.sleep(1.0)
            finally:
                self._running -= 1

            if claimed is None:
                await asyncio.sleep(IDLE_POLL_SECONDS)

    async def _correlated_maintenance(self) -> None:
        """Maintenance under its own trace.

        Periodic work has no enqueuing request to inherit from, so it gets a
        fresh trace per process. Without one, a reservation expiry that goes
        wrong appears in the logs as an orphan line with nothing to join it to.
        """
        with correlated(job_id=f"maintenance:{self.queue.worker_id}"):
            await self._maintenance()

    async def _maintenance(self) -> None:
        """Reclaim abandoned work and release abandoned quota."""
        while not self._stopping.is_set():
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
            with contextlib.suppress(Exception):
                await self.queue.recover()
            with contextlib.suppress(Exception):
                # Renders sent to a computer that never turned up. Distinct from
                # `recover`, which reclaims work a machine *took* and abandoned;
                # this is for work nobody ever claimed, which the queue is
                # perfectly happy to hold pending forever.
                await self._device_pool().escalate(
                    older_than_seconds=self.settings.device_stranded_seconds
                )
            with contextlib.suppress(Exception):
                # P0-7. This is the caller `expire_reservations` never had, and
                # its absence meant a crashed render held quota until the TTL.
                released = await asyncio.to_thread(
                    self.assembly.usage.expire_reservations
                )
                if released:
                    self.assembly.events.emit(
                        EventName.JOB_COMPLETED,
                        project_id=None,
                        data={"reservations_released": released},
                    )

    async def _retention(self) -> None:
        """Cross-tenant retention, on a schedule, under a system principal."""
        while not self._stopping.is_set():
            await asyncio.sleep(RETENTION_INTERVAL_SECONDS)
            with contextlib.suppress(Exception):
                await self.queue.enqueue(
                    kind=JobKind.RETENTION_SWEEP.value,
                    payload=SweepPayload(
                        older_than_hours=self.settings.temporary_retention_hours
                    ).model_dump(mode="json"),
                    idempotency_key=None,
                )


def _bind(kind: str, handler: Any, context: JobContext) -> Any:
    """Turn a two-argument handler into the one-argument shape the queue wants.

    Also the one place every job passes through, which makes it the right place
    to open a trace scope: with `VTV_TRACE_PROVIDER_CALLS` on, each job prints
    the provider calls it made and what they cost, and every call inside it is
    tagged with the job id in `var/provider-calls.jsonl`. With the trace off
    this is a null object and costs nothing.
    """
    trace = getattr(context.assembly, "trace", None) or NullTrace()

    async def run(payload: dict[str, Any]) -> Any:
        job_id = str(payload.get("job_id") or payload.get("project_id") or "-")
        with trace.job(kind, job_id):
            return await handler(context, payload)

    return run


async def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    concurrency = 2
    for index, item in enumerate(argv):
        if item == "--concurrency" and index + 1 < len(argv):
            concurrency = max(1, int(argv[index + 1]))

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    worker = Worker.create(settings, concurrency=concurrency)
    worker.assembly.events.subscribe(correlated_log_handler())

    loop = asyncio.get_running_loop()
    for received in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            # Not available on every platform; the worker still runs, it just
            # cannot drain gracefully there.
            loop.add_signal_handler(received, worker.stop)

    await worker.run_forever()
    return 0


def cli() -> None:  # pragma: no cover - process entrypoint
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":  # pragma: no cover
    cli()


__all__ = [
    "IDLE_POLL_SECONDS",
    "MAINTENANCE_INTERVAL_SECONDS",
    "RETENTION_INTERVAL_SECONDS",
    "SHUTDOWN_GRACE_SECONDS",
    "Worker",
    "cli",
    "main",
]
