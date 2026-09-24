"""Turning a timeline into a file.

The renderer port is the narrowest interface in the system, on purpose. It takes
a fully-resolved timeline and settings, and it returns a finished job. It is
given no access to AI providers, no ability to search for assets, and no say in
what anything should look like (Rule 13).

Remotion is the first implementation. This port exists so that it is a choice
rather than a commitment: a second implementation — a headless compositor, a
GPU-backed encoder, a different framework entirely — can be introduced without a
single change above this line.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from vtv.contracts.render import RenderJob, RenderSettings
from vtv.contracts.timeline import Timeline


@runtime_checkable
class Renderer(Protocol):
    """Compose a timeline into a video file."""

    async def render(
        self,
        *,
        timeline: Timeline,
        settings: RenderSettings,
    ) -> RenderJob:
        """Render to completion and return the finished job.

        The implementation is responsible for resolving every object reference
        in the timeline through the storage port, and for failing loudly if one
        cannot be read — a render that silently substitutes a black frame for a
        missing asset is worse than one that fails.
        """
        ...

    async def render_progress(self, render_job_id: str) -> AsyncIterator[RenderJob]:
        """Stream progress updates for a running render.

        Rendering takes long enough that the user must be shown real progress,
        and honest progress requires the renderer to report it rather than the
        UI simulating it.
        """
        ...

    async def cancel(self, render_job_id: str) -> None:
        """Stop a running render. Idempotent."""
        ...


__all__ = ["Renderer"]
