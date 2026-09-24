"""Finding media we are allowed to use.

The port is deliberately narrow, and it carries one strong guarantee: **an
adapter must not return a candidate whose rights it cannot establish.** Filtering
happens inside the adapter, against the source's own licence metadata, before
anything reaches our code. An adapter that cannot determine a licence returns
fewer results; it never returns a result with ``Permission.UNKNOWN`` and hopes
the caller checks.

That places the burden where the knowledge is. The Openverse adapter understands
Openverse's licence fields; the Visual Director should not have to.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from vtv.contracts.asset import AssetDimensions, AssetKind, AssetProvenance
from vtv.contracts.base import Confidence, VTVModel
from vtv.contracts.visual_plan import MediaSearchConstraints
from vtv.ports.base import Provider


class AssetCandidate(VTVModel):
    """A search hit, not yet downloaded and not yet chosen."""

    provenance: AssetProvenance
    kind: AssetKind
    #: Where the full-resolution file can be fetched from. Downloading happens
    #: only after a candidate is selected, so a search costs nothing but a query.
    download_url: str = Field(min_length=1, max_length=2048)
    preview_url: str | None = Field(default=None, max_length=2048)
    dimensions: AssetDimensions | None = None
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    #: The source's own relevance score, normalised. Rankings from different
    #: providers are not comparable, so this is a hint, not a decision.
    relevance: Confidence | None = None


@runtime_checkable
class AssetSearchProvider(Provider, Protocol):
    """Search one media source for usable material."""

    async def search(
        self,
        *,
        query: str,
        kind: AssetKind = AssetKind.IMAGE,
        constraints: MediaSearchConstraints,
        limit: int = 12,
    ) -> list[AssetCandidate]:
        """Return candidates that satisfy ``constraints``.

        Every returned candidate must carry complete provenance, including a
        licence that positively permits the requested uses. Returning an empty
        list is a correct and expected answer; the Visual Director will fall
        back to drawing the scene itself.
        """
        ...


__all__ = ["AssetCandidate", "AssetSearchProvider"]
