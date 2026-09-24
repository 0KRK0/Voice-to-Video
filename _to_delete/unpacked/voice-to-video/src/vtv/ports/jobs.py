"""Background work.

Transcription, generation and rendering are all far too slow to happen inside a
request. They run as jobs. This port keeps the queue technology — Redis today,
something else later — out of the pipeline code entirely.

The interface is deliberately smaller than any real queue's feature set. Enqueue,
observe, cancel. Anything richer (priorities per customer tier, fan-out, delayed
retries with jitter) belongs inside the adapter, where it can be tuned without
the pipeline knowing.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from vtv.contracts.base import Id, VTVModel
from vtv.contracts.errors import ErrorInfo, Status


class JobPriority(str, Enum):
    """Interactive work must not queue behind batch work.

    A user watching a spinner is ``INTERACTIVE``. Re-rendering an archived
    project at higher quality is ``BATCH``. Without this distinction the first
    large customer's backlog becomes every other user's latency.
    """

    INTERACTIVE = "interactive"
    STANDARD = "standard"
    BATCH = "batch"


class JobHandle(VTVModel):
    """A reference to enqueued work."""

    job_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    status: Status = Status.PENDING
    attempt: int = Field(default=1, ge=1)
    project_id: Id | None = None
    error: ErrorInfo | None = None


@runtime_checkable
class JobQueue(Protocol):
    """Enqueue and observe background work."""

    async def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        priority: JobPriority = JobPriority.STANDARD,
        #: Two enqueues with the same key are the same job. This is what makes a
        #: retried API call safe and stops a double-clicked button from paying
        #: for the same video twice.
        idempotency_key: str | None = None,
        delay_seconds: float = 0.0,
    ) -> JobHandle:
        ...

    async def status(self, job_id: str) -> JobHandle:
        ...

    async def cancel(self, job_id: str) -> None:
        """Cancel pending work. Running work is asked to stop; it may not."""
        ...


__all__ = ["JobHandle", "JobPriority", "JobQueue"]
