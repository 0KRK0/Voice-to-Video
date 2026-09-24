"""The loop: ask for work, do it, say how it went, repeat.

## What it is built to survive

Not "errors" in general — specific, ordinary things that happen to a computer
under somebody's desk and never happen to a server in a rack:

* **The wifi goes.** Polling failures are not failures. The loop backs off and
  keeps asking, because a machine that stopped when a router rebooted is a
  machine somebody uninstalls.
* **The lid closes mid-render.** The job's lease expires and the server gives
  the work to somebody else. On waking, this device finds its claim gone and
  simply asks for the next thing. Nothing needs to notice it was asleep.
* **The card stops working.** Hardware is re-measured on every claim, so a
  driver that broke overnight makes this machine a CPU renderer at its next
  poll rather than a machine that fails every GPU job it is given.
* **The device is revoked.** That is terminal and the loop *stops*, loudly. A
  revoked device that kept polling would sit there forever while its owner
  believed they had removed it.

## Why the loop owns so little

Because everything that can be somewhere else already is. Claiming, leases and
recovery live in the queue; drawing lives in the renderer; correctness lives in
the equivalence harness. What is left here is scheduling and the decision about
which failures are worth stopping for — which is genuinely all this should be.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from vtv.contracts.devices import Completion, DeviceHardware
from vtv.contracts.errors import PolicyViolation
from vtv.contracts.execution import ExecutionTarget
from vtv.desktop.cache import AssetCache
from vtv.desktop.client import (
    IDLE_POLL_SECONDS,
    DeviceClient,
    backoff,
    idle_interval,
)
from vtv.desktop.executor import JobExecutor


@dataclass
class Agent:
    """A paired computer, offering itself for work until told to stop."""

    client: DeviceClient
    workspace: Path
    device_id: str
    cache: AssetCache | None = None
    workers: int | None = None
    idle_seconds: float = IDLE_POLL_SECONDS
    #: Injected for tests; production measures the machine.
    detect: object = None

    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    #: Counted rather than logged-and-forgotten, so `run` can report what a
    #: session actually did when it ends.
    completed: int = 0
    failed: int = 0

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.cache is None:
            self.cache = AssetCache(self.workspace / "assets")

    def stop(self) -> None:
        """Finish the job in hand, then exit. Safe from a signal handler."""
        self._stop.set()

    def announce(self, outcome: Completion, *, accepted: bool) -> None:
        """Say what happened, and **which hardware drew it**.

        The counters alone were the whole report, so a person watching this
        window could not answer the one question the entire feature exists to
        make answerable: did my graphics card actually do that? The device is
        the only thing that knows — the server is told, and until now threw it
        away — so it says so here, per job, on the machine somebody is looking
        at.
        """
        if not outcome.ok:
            print(f"  failed: {outcome.reason}", flush=True)
            if outcome.elsewhere:
                print("  handed back for another machine to try", flush=True)
            return

        drew = outcome.backends or {}
        where = ", ".join(
            f"{count} on {ExecutionTarget(target).label}"
            if target in {item.value for item in ExecutionTarget}
            else f"{count} on {target}"
            for target, count in sorted(drew.items())
        )
        print(
            f"  done in {outcome.render_seconds:.0f}s — "
            f"{outcome.duration_seconds:.0f}s of video, "
            f"{outcome.output_bytes / 1e6:.1f} MB"
            + (f" — {where}" if where else ""),
            flush=True,
        )
        if not accepted:
            # Not an error, and worth saying plainly rather than hiding: the
            # lease expired while this machine was drawing and the job belongs
            # to somebody else now.
            print(
                "  the server had already given this job to another machine, "
                "so this result was discarded",
                flush=True,
            )

    def _hardware(self) -> DeviceHardware:
        if self.detect is not None:
            return self.detect()  # type: ignore[operator]
        from vtv.desktop.hardware import detect

        return detect()

    async def _sleep(self, seconds: float) -> None:
        """Wait, but wake immediately if asked to stop.

        A plain `asyncio.sleep` would make Ctrl-C take up to a full poll
        interval to be noticed, which reads as a program that has hung.
        """
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def run(self, *, max_jobs: int | None = None) -> int:
        """Work until stopped. Returns how many jobs were completed.

        `max_jobs` is for tests and for a one-shot "render this and quit" mode;
        `None` is the normal case of a machine left running.
        """
        misses = 0
        # Where the ladder's current rung lives. Reset to the floor on every
        # job, so a machine that is being used stays responsive and only one
        # that has been quiet for minutes settles into the wide interval.
        idle = self.idle_seconds
        while not self._stop.is_set():
            if max_jobs is not None and self.completed + self.failed >= max_jobs:
                break

            try:
                hardware = self._hardware()
                assignment = await self.client.claim(hardware)
            except PolicyViolation:
                # Revoked or unpaired: terminal, and it must not be retried.
                # Raised onward so the CLI can say so and exit non-zero rather
                # than leaving a process that looks alive and does nothing.
                raise
            except Exception:
                # Everything else is the network being the network. Back off and
                # keep asking; there is nothing here worth stopping for.
                misses += 1
                await self._sleep(backoff(min(misses, 6)))
                continue

            if assignment is None:
                misses = 0
                # Three inputs, in order of authority. The server's hint wins
                # when it is there, because it knows something this machine
                # cannot — that somebody is watching a screen right now. Failing
                # that the interval widens one step, and it starts over at the
                # floor the moment there is work.
                hurry = getattr(self.client, "hurry_seconds", None)
                idle = hurry if hurry is not None else idle_interval(
                    idle, floor=self.idle_seconds
                )
                await self._sleep(idle)
                continue

            idle = self.idle_seconds
            misses = 0
            executor = JobExecutor(
                client=self.client,
                cache=self.cache,  # type: ignore[arg-type]
                workspace=self.workspace / "jobs",
                hardware=hardware,
                device_id=self.device_id,
                workers=self.workers,
            )
            outcome = await executor.execute(assignment)
            # `finish` retries hard: the work is done and a lost completion
            # means a finished render nobody knows about. If even that fails,
            # the lease expires and the job is drawn again — wasteful, and the
            # correct wasteful.
            accepted = await self.client.finish(outcome)
            self.announce(outcome, accepted=accepted)
            if outcome.ok:
                self.completed += 1
            else:
                self.failed += 1

            # Keep the cache inside its budget between jobs rather than during
            # one: sweeping mid-render could evict a photograph the running job
            # is still drawing from.
            if self.cache is not None:
                self.cache.sweep()

        return self.completed


__all__ = ["Agent"]
