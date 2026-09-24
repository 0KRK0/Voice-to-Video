"""Our own asset library.

A real provider, not a stand-in: it searches media we already hold and are
already cleared to use. It is first in the resolution order for a reason —
reusing something is free, instant, already on-brand, and carries no licensing
question at all.

The library is a directory plus a JSON manifest, so it works identically on a
laptop and on object storage. It starts empty; assets accumulate as projects
fetch and generate them, which means the second project about the transistor is
cheaper than the first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vtv.contracts.asset import (
    AssetDimensions,
    AssetKind,
    AssetProvenance,
    AssetSource,
    License,
    Permission,
)
from vtv.contracts.visual_plan import MediaSearchConstraints
from vtv.ports.assets import AssetCandidate
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth

MANIFEST_NAME = "manifest.json"


@dataclass
class LibraryEntry:
    asset_id: str
    path: Path
    title: str
    tags: list[str]
    kind: AssetKind
    width: int | None = None
    height: int | None = None
    description: str | None = None

    def score(self, query: str) -> float:
        """Term overlap between the query and this entry's text.

        Deliberately simple and deliberately strict: a weak match returns a low
        score and the resolver moves on to a provider that might do better. A
        library that returns something for every query is worse than one that
        admits it has nothing.
        """
        terms = {t for t in query.lower().split() if len(t) > 2}
        if not terms:
            return 0.0
        haystack = " ".join([self.title, self.description or "", *self.tags]).lower()
        hits = sum(1 for term in terms if term in haystack)
        return hits / len(terms)


class LocalLibraryAssetSearchProvider:
    """Search a local directory of pre-cleared media."""

    #: A match below this is not worth showing; better to draw the scene.
    MIN_SCORE = 0.5

    def __init__(self, root: Path, *, name: str = "internal-library") -> None:
        self.root = Path(root)
        self.name = name
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            unit_cost_usd=0.0,
            unit="search",
            typical_latency_seconds=0.05,
            data_policy=DataPolicy(
                retains_input=False,
                trains_on_input=False,
                retention_days=0,
                region="local",
                dpa_in_place=True,
            ),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    def entries(self) -> list[LibraryEntry]:
        manifest = self.root / MANIFEST_NAME
        if not manifest.exists():
            return []
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        found: list[LibraryEntry] = []
        for raw in payload.get("assets", []):
            path = self.root / str(raw.get("file", ""))
            if not path.exists():
                continue
            found.append(
                LibraryEntry(
                    asset_id=str(raw.get("asset_id") or path.stem),
                    path=path,
                    title=str(raw.get("title") or path.stem),
                    tags=[str(t) for t in raw.get("tags", [])],
                    kind=AssetKind(raw.get("kind", "image")),
                    width=raw.get("width"),
                    height=raw.get("height"),
                    description=raw.get("description"),
                )
            )
        return found

    async def search(
        self,
        *,
        query: str,
        kind: AssetKind = AssetKind.IMAGE,
        constraints: MediaSearchConstraints,
        limit: int = 12,
    ) -> list[AssetCandidate]:
        scored = [
            (entry.score(query), entry)
            for entry in self.entries()
            if entry.kind is kind
        ]
        chosen = sorted(
            ((score, entry) for score, entry in scored if score >= self.MIN_SCORE),
            key=lambda item: -item[0],
        )[:limit]

        candidates: list[AssetCandidate] = []
        for score, entry in chosen:
            if (
                kind is AssetKind.IMAGE
                and entry.width
                and entry.height
                and (entry.width < constraints.min_width or entry.height < constraints.min_height)
            ):
                continue
            candidates.append(
                AssetCandidate(
                    provenance=AssetProvenance(
                        source=AssetSource.INTERNAL_LIBRARY,
                        source_id=entry.asset_id,
                        title=entry.title,
                        license=License(
                            spdx_id=None,
                            name="Internal library — owned or licensed outright",
                            commercial_use=Permission.ALLOWED,
                            modification=Permission.ALLOWED,
                            attribution_required=False,
                        ),
                    ),
                    kind=entry.kind,
                    download_url=entry.path.as_uri(),
                    dimensions=(
                        AssetDimensions(width=entry.width, height=entry.height)
                        if entry.width and entry.height
                        else None
                    ),
                    title=entry.title,
                    description=entry.description,
                    relevance=min(1.0, score),
                )
            )
        return candidates


__all__ = ["MANIFEST_NAME", "LibraryEntry", "LocalLibraryAssetSearchProvider"]
