"""An in-process job queue.

STATUS: **REAL IMPLEMENTATION — RUNS. NOT SUITABLE FOR PRODUCTION.**

Jobs run as asyncio tasks in the API process. That is correct for a single-node
development install and wrong for production for one specific reason: a restart
loses running work. The `JobQueue` port exists so that swapping in Redis or
another broker is a wiring change, not a rewrite.

Idempotency is implemented rather than promised: two enqueues with the same key
return the same handle, so a retried API call or a double-clicked button cannot
pay for the same render twice.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vtv.contracts.errors import ErrorCategory, ErrorCode, ErrorInfo, Status, VTVError
from vtv.observability.events import EventName, EventSink
from vtv.ports.jobs import JobHandle, JobPriority

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class InProcessJobQueue:
    """Runs jobs as background tasks, tracking their state."""

    events: EventSink
    handlers: dict[str, Handler] = field(default_factory=dict)
    _jobs: dict[str, JobHandle] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    _by_key: dict[str, str] = field(default_factory=dict)
    _results: dict[str, Any] = field(default_factory=dict)

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    async def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        priority: JobPriority = JobPriority.STANDARD,
        idempotency_key: str | None = None,
        delay_seconds: float = 0.0,
    ) -> JobHandle:
        if idempotency_key and idempotency_key in self._by_key:
            return self._jobs[self._by_key[idempotency_key]]

        handler = self.handlers.get(kind)
        if handler is None:
            raise VTVError(f"no handler registered for job kind {kind!r}")

        from vtv.contracts.base import IdPrefix, new_id

        job_id = new_id(IdPrefix.RENDER_JOB if kind == "render" else IdPrefix.PROJECT)
        handle = JobHandle(
            job_id=job_id,
            kind=kind,
            status=Status.PENDING,
            project_id=payload.get("project_id"),
        )
        self._jobs[job_id] = handle
        if idempotency_key:
            self._by_key[idempotency_key] = job_id

        self.events.emit(
            EventName.JOB_ENQUEUED,
            project_id=handle.project_id,
            data={"job_id": job_id, "kind": kind, "priority": priority.value},
        )

        async def run() -> None:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            handle.status = Status.PROCESSING
            self.events.emit(
                EventName.JOB_STARTED,
                project_id=handle.project_id,
                data={"job_id": job_id, "kind": kind},
            )
            try:
                self._results[job_id] = await handler(payload)
            except asyncio.CancelledError:
                handle.status = Status.FAILED
                handle.error = ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category=ErrorCategory.INTERNAL,
                    message="job cancelled",
                )
                raise
            except VTVError as error:
                handle.status = Status.FAILED
                handle.error = error.info
                self.events.emit(
                    EventName.JOB_FAILED,
                    project_id=handle.project_id,
                    data={"job_id": job_id, "code": error.info.code.value},
                )
                return
            except Exception as error:
                handle.status = Status.FAILED
                handle.error = ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category=ErrorCategory.INTERNAL,
                    message=f"{type(error).__name__}: {error}"[:2000],
                    user_message="Something went wrong on our side.",
                )
                self.events.emit(
                    EventName.JOB_FAILED,
                    project_id=handle.project_id,
                    data={"job_id": job_id, "code": "internal_error"},
                )
                return
            handle.status = Status.READY
            self.events.emit(
                EventName.JOB_COMPLETED,
                project_id=handle.project_id,
                data={"job_id": job_id, "kind": kind},
            )

        self._tasks[job_id] = asyncio.create_task(run(), name=f"vtv-job-{job_id}")
        return handle

    async def status(self, job_id: str) -> JobHandle:
        handle = self._jobs.get(job_id)
        if handle is None:
            raise VTVError(f"unknown job {job_id}", code=ErrorCode.ASSET_NOT_FOUND)
        return handle

    def result(self, job_id: str) -> Any:
        return self._results.get(job_id)

    async def cancel(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def drain(self, timeout: float = 300.0) -> None:
        """Wait for every job to finish. Used by tests and by shutdown."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)


__all__ = ["Handler", "InProcessJobQueue"]
