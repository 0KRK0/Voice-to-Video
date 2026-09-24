"""Drawing one job, on this machine.

## What this is not

It is not a renderer. It is the twenty lines of arrangement that let the *real*
renderer run somewhere it has never run before, plus the error handling for a
machine that can be closed, unplugged or put to sleep halfway through.

`FfmpegRenderer` is used unmodified. The same `Timeline`, the same
`RenderSettings`, the same segment checkpointing, the same per-segment routing,
the same GPU painter that was verified against the reference frame by frame. A
video drawn on a customer's desktop and the same video drawn in the cloud differ
by no more than the equivalence tolerance, because the code that draws them is
the same code.

That is the whole reason the earlier work went where it did. A desktop executor
that reimplemented compositing would need its own correctness proof, its own
fallback rules and its own bugs; this one inherits all three.

## How the assets arrive without credentials

The device never holds storage credentials, and the renderer resolves assets
through a storage port. Both are satisfied by putting a `LocalStorageProvider`
in front of the job's own directory and placing each downloaded asset exactly
where that provider would look for its `ObjectRef`. The renderer then
materialises assets by the identical code path it uses in the cloud, and never
learns that it is somewhere else.

## Why the scratch directory is not a temporary one

Because a laptop closes. Segments are checkpoints — a segment on disk is
finished by definition — and a job directory that vanished on exit would throw
away every finished segment on a machine that has exactly one interesting
failure mode. Kept under the executor's own directory, keyed by the render job,
so an interrupted job resumes from the segments it already has.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from vtv.contracts.devices import (
    Assignment,
    Completion,
    DeviceHardware,
    ProgressReport,
)
from vtv.contracts.errors import TERMINAL_CATEGORIES, VTVError
from vtv.contracts.execution import ExecutionPolicy, ExecutionTarget, Registry
from vtv.desktop.cache import AssetCache
from vtv.desktop.client import DeviceClient
from vtv.observability.events import EventSink

#: How often the executor tells the server where it is. Comfortably inside the
#: lease, because a report is also what renews it: at a fifth of the window,
#: four consecutive reports have to be lost before the job is taken back.
REPORT_EVERY_SECONDS = 20.0


def _is_local_problem(error: VTVError) -> bool:
    """Whether this failure is this machine's rather than the job's.

    The remote form of `SegmentOutcome.elsewhere`, and the same distinction: a
    device out of disk or video memory has not made the job impossible and
    should hand it back, while a device sent a timeline referencing an asset
    that does not exist has, and every other machine will fail identically.

    Decided from the error's own category rather than from its message text.
    Matching on strings is how "out of memory" and "out of memoryX" end up on
    different code paths a year after somebody rewords a message.

    And from `TERMINAL_CATEGORIES` rather than a list of its own, because the
    queue already uses exactly that set to decide whether a failure is worth
    retrying at all. Two lists would be two answers to "is this job impossible
    or is this machine having a bad day", and they would diverge the first time
    somebody added a category to one of them.
    """
    return error.info.category not in TERMINAL_CATEGORIES


@dataclass
class JobExecutor:
    """One assignment, from bytes on the wire to a finished file uploaded."""

    client: DeviceClient
    cache: AssetCache
    workspace: Path
    hardware: DeviceHardware
    device_id: str
    #: Segments to draw at once. `None` lets the renderer size it from the
    #: machine, which is what a customer wants; a smaller number is what
    #: somebody who is also using the computer wants.
    workers: int | None = None
    events: EventSink = field(default_factory=EventSink)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    # -- assets ------------------------------------------------------------

    async def gather(self, assignment: Assignment, storage_root: Path) -> int:
        """Put every asset this job needs where the renderer will look for it.

        Returns bytes downloaded — zero when everything was already cached,
        which is the common case for a machine rendering the same project twice.
        """
        from vtv.adapters.storage.local import LocalStorageProvider

        downloaded = 0
        for handout in assignment.assets:
            target = LocalStorageProvider(
                storage_root, bucket=handout.object.bucket
            ).path_for(handout.object)
            target.parent.mkdir(parents=True, exist_ok=True)

            if not self.cache.holds(handout.sha256):
                partial = self.workspace / f"{handout.sha256}.part"
                try:
                    downloaded += await self.client.fetch(handout.url, partial)
                    # Verified inside `store`, before it becomes visible. A
                    # truncated download does not raise — it decodes to a grey
                    # band across the bottom of somebody's video — so the digest
                    # is the only thing standing between a bad connection and a
                    # ruined render.
                    self.cache.store(handout, partial)
                finally:
                    partial.unlink(missing_ok=True)
            self.cache.touch(handout.sha256)

            cached = self.cache.path_for(handout.sha256)
            if target.exists():
                target.unlink()
            try:
                # Hard link rather than copy: same bytes, no second copy of a
                # 200 MB photograph, and the cache still owns the original.
                target.hardlink_to(cached)
            except (OSError, AttributeError):
                shutil.copyfile(cached, target)
        return downloaded

    # -- rendering ---------------------------------------------------------

    def _registry(self) -> tuple[Registry, ExecutionPolicy]:
        """The backends this machine may use, and in what order.

        Built from measured hardware, so a graphics card that did not verify is
        simply absent rather than present-and-declined. The policy is AUTO
        because per-segment routing decides the rest: typography goes to the
        processor and photographs to the card, inside one video.
        """
        from vtv.adapters.render.cpu_backend import CpuRenderBackend, GpuRenderBackend

        registry = Registry()
        if ExecutionTarget.LOCAL_GPU in self.hardware.targets:
            registry.register(ExecutionTarget.LOCAL_GPU, GpuRenderBackend())
        registry.register(
            ExecutionTarget.LOCAL_CPU,
            CpuRenderBackend(where=ExecutionTarget.LOCAL_CPU),
        )
        return registry, ExecutionPolicy.auto()

    async def execute(self, assignment: Assignment) -> Completion:
        """Draw it, upload it, and say what happened. Never raises.

        Never raises because the caller is a loop that must keep running: an
        executor that died on a bad job would need a person to restart it, and
        the person is the one thing a machine under somebody's desk does not
        have. Every failure becomes a `Completion` the server can act on.
        """
        started = time.perf_counter()
        job_root = self.workspace / assignment.render_job_id
        job_root.mkdir(parents=True, exist_ok=True)
        storage_root = job_root / "storage"

        try:
            await self.gather(assignment, storage_root)
            output, backends = await self._render(assignment, job_root, storage_root)
            size = output.stat().st_size
            if assignment.can_upload:
                # Asked for here, with the file already on disk, rather than
                # carried in the assignment. The URL is good for an hour from
                # this moment instead of an hour from when the job was claimed
                # — which is the difference between a long render that delivers
                # and one that draws every frame and then throws it away.
                await self.client.upload(await self.client.upload_url(), output)
        except VTVError as exc:
            return Completion(
                render_job_id=assignment.render_job_id,
                device_id=self.device_id,
                ok=False,
                reason=str(exc)[:400],
                elsewhere=_is_local_problem(exc),
                render_seconds=time.perf_counter() - started,
            )
        except Exception as exc:  # the loop must survive anything
            # An unexpected exception is this machine's problem until proven
            # otherwise: handing the job back costs one retry elsewhere, while
            # marking it permanently failed costs the customer their render.
            return Completion(
                render_job_id=assignment.render_job_id,
                device_id=self.device_id,
                ok=False,
                reason=f"{type(exc).__name__}: {exc}"[:400],
                elsewhere=True,
                render_seconds=time.perf_counter() - started,
            )

        # Only now: the finished segments are no longer worth keeping, because
        # the thing they were checkpoints towards has been delivered.
        shutil.rmtree(job_root, ignore_errors=True)
        return Completion(
            render_job_id=assignment.render_job_id,
            device_id=self.device_id,
            ok=True,
            output_bytes=size,
            duration_seconds=assignment.timeline.duration_seconds,
            render_seconds=time.perf_counter() - started,
            backends=backends,
        )

    async def _render(
        self, assignment: Assignment, job_root: Path, storage_root: Path
    ) -> tuple[Path, dict[str, int]]:
        """The real renderer, on local files, with progress going back."""
        from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
        from vtv.adapters.storage.local import LocalStorageProvider

        bucket = (
            assignment.assets[0].object.bucket if assignment.assets else "vtv-local"
        )
        storage = LocalStorageProvider(storage_root, bucket=bucket)
        registry, policy = self._registry()
        renderer = FfmpegRenderer(
            storage=storage,
            events=self.events,
            # Kept, not temporary: segments are checkpoints and a closed laptop
            # is this machine's defining failure. The renderer finds them again
            # by fingerprint, so an interrupted job resumes rather than restarts.
            workdir=job_root / "work",
            workers=self.workers,
            backends=registry,
            policy=policy,
        )

        drawing = asyncio.create_task(
            renderer.render(
                timeline=assignment.timeline, settings=assignment.settings
            )
        )
        reporting = asyncio.create_task(
            self._report_while(assignment, job_root, drawing)
        )
        try:
            job = await drawing
        finally:
            reporting.cancel()
            # Awaited rather than abandoned: an un-awaited cancelled task logs a
            # warning on exit and, worse, may still be mid-request when the
            # process ends.
            await asyncio.gather(reporting, return_exceptions=True)

        if job.output is None:
            raise VTVError(f"the render produced no file: {job.error}")
        return storage.path_for(job.output), dict(job.backends or {})

    async def _report_while(
        self, assignment: Assignment, job_root: Path, drawing: asyncio.Task
    ) -> None:
        """Tell the server where we are, until the render stops.

        By **counting the segment files that exist on disk**, not by reading a
        number the renderer keeps in memory. That is the same rule the progress
        contract states and it is not pedantry: a segment on disk is finished by
        definition, and a count derived from anything else is a progress bar
        that can move while nothing is being produced.

        It also happens to be the only count that survives a resume. A job
        picked up again after a laptop was closed starts with segments already
        on disk, and a counter kept in memory would report it as starting from
        nothing.
        """
        scratch = self._scratch_of(assignment, job_root)
        plan = self._plan_of(assignment)
        total = len(plan)

        while not drawing.done():
            await asyncio.sleep(REPORT_EVERY_SECONDS)
            if drawing.done():
                return
            done = sum(1 for item in plan if item.done(scratch)) if scratch else 0
            await self.client.report(
                ProgressReport(
                    render_job_id=assignment.render_job_id,
                    device_id=self.device_id,
                    segments_done=min(done, total),
                    segments_total=total,
                    backends={},
                )
            )

    def _plan_of(self, assignment: Assignment) -> list:
        """The segments this job splits into. The same split the renderer makes.

        Derived from the timeline rather than asked for, because the renderer
        does not expose it and duplicating the *arithmetic* is safe in a way
        that duplicating the *drawing* would not be: if the two ever disagree,
        the worst outcome is a progress bar that stops short, and the tests
        below assert they do not.
        """
        from vtv.adapters.render import segments as seg

        timeline = assignment.timeline
        fps = assignment.settings.frame_rate
        return seg.plan(
            [clip.span.start for clip in timeline.clips],
            fps=fps,
            total_frames=max(1, round(timeline.duration_seconds * fps)),
        )

    def _scratch_of(self, assignment: Assignment, job_root: Path) -> Path | None:
        """Where the renderer is putting this job's segments.

        Named by *what is being rendered* — see `segments.fingerprint` — which
        is what makes a resumed job find the segments the interrupted one left.
        """
        from vtv.adapters.render import segments as seg

        try:
            mark = seg.fingerprint(assignment.timeline, assignment.settings)
        except Exception:
            return None
        return job_root / "work" / f"{assignment.timeline.timeline_id}-{mark}"


__all__ = ["REPORT_EVERY_SECONDS", "JobExecutor"]
