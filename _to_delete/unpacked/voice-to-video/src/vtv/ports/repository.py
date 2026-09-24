"""Persistence.

A deliberately small port. The system stores a handful of documents per project
and reads them back by id; it does not need an ORM, a query language, or a
migration framework in the core.

What it does need is the guarantee in `docs/STORAGE_POLICY.md`: a saved project
keeps its *reasoning* — transcript, understanding, scene graph, visual plan,
timeline — so the video can be rebuilt without storing the video.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from vtv.contracts.project import Project


@runtime_checkable
class ProjectRepository(Protocol):
    """Stores projects and the documents they produced."""

    async def save_project(self, project: Project) -> None: ...

    async def get_project(self, project_id: str) -> Project | None: ...

    async def list_projects(
        self, *, owner_id: str | None = None, limit: int = 50
    ) -> list[Project]: ...

    async def delete_project(self, project_id: str) -> None:
        """Real deletion of the project and every document belonging to it.

        This is the operation behind a user's right to erasure, so it must
        remove rows rather than mark them.
        """
        ...

    async def put_document(
        self, *, project_id: str, kind: str, document_id: str, payload: dict[str, Any]
    ) -> None:
        """Store one derived document.

        ``kind`` is the document name from the contract (``transcript``,
        ``scene_graph``, …). Documents are versioned by insertion rather than
        overwritten, so an improved Visual Director does not erase the plan an
        old project was rendered from.
        """
        ...

    async def get_document(
        self, *, project_id: str, kind: str
    ) -> dict[str, Any] | None:
        """The most recent document of this kind for the project."""
        ...

    async def expired_projects(self, *, now: datetime) -> list[str]:
        """Temporary projects past their retention window."""
        ...


__all__ = ["ProjectRepository"]
