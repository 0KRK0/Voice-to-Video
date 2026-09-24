"""Stage 6 — Asset intelligence.

Search, clear, fetch, store. The output is an `Asset` with complete provenance,
or nothing at all.

Two properties are enforced here rather than trusted.

**Rights are re-checked after search.** Adapters filter, and are supposed to, but
this is the last gate before media enters a video a customer may publish. A
second check costs microseconds and removes a whole class of "the adapter had a
bug" incidents.

**Fetching is not a free-form HTTP client.** The URL comes from a third-party API
response, which makes it attacker-influenced data. The `Fetcher` protocol keeps
the actual HTTP client in an adapter — `vtv.adapters.assets.http_fetcher` — where
vendor code belongs, and where its SSRF checks live (`docs/SECURITY.md`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from vtv.contracts.asset import (
    EXTERNAL_SOURCES,
    Asset,
    AssetKind,
    AssetSource,
)
from vtv.contracts.base import RetentionClass
from vtv.contracts.errors import (
    ErrorCode,
    NotFound,
    PolicyViolation,
    Status,
    VTVError,
)
from vtv.contracts.visual_plan import LicensedMediaRequirements, MediaSearchConstraints
from vtv.observability.events import EventName, EventSink
from vtv.ports.assets import AssetCandidate

#: A single asset larger than this is not worth the download.
MAX_ASSET_BYTES = 40 * 1024 * 1024

_CONTENT_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


class Fetcher(Protocol):
    """Retrieves the bytes behind a candidate's download URL."""

    async def fetch(self, url: str) -> tuple[bytes, str]: ...


class LocalFileFetcher:
    """Reads ``file://`` URLs. Used by the internal library."""

    def __init__(self, allowed_roots: list[Path] | None = None) -> None:
        self.allowed_roots = [Path(p).resolve() for p in (allowed_roots or [])]

    async def fetch(self, url: str) -> tuple[bytes, str]:
        parsed = urlparse(url)
        if parsed.scheme != "file":
            raise VTVError(f"unsupported scheme {parsed.scheme!r}", code=ErrorCode.ASSET_NOT_FOUND)
        path = Path(parsed.path).resolve()
        if self.allowed_roots and not any(
            str(path).startswith(str(root)) for root in self.allowed_roots
        ):
            raise PolicyViolation(f"file outside the permitted library roots: {path}")
        if not path.exists():
            raise NotFound(f"library asset missing: {path}")
        data = path.read_bytes()
        if len(data) > MAX_ASSET_BYTES:
            raise PolicyViolation("library asset exceeds the size limit")
        return data, _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


@dataclass
class AssetResolver:
    """Finds and stores usable media for a licensed-media directive."""

    storage: object  # StorageProvider
    events: EventSink
    providers: list[object] = field(default_factory=list)  # AssetSearchProvider
    fetcher: Fetcher | None = None

    async def resolve(
        self,
        *,
        project_id: str,
        scene_id: str,
        requirements: LicensedMediaRequirements,
    ) -> Asset | None:
        """Return a stored, cleared asset, or ``None`` if nothing is usable.

        ``None`` is a normal outcome, not an error. The caller descends its
        fallback ladder and draws the scene instead.
        """
        queries = [requirements.query, *requirements.alternate_queries]
        for provider in self.providers:
            for query in queries:
                try:
                    candidates = await provider.search(  # type: ignore[attr-defined]
                        query=query,
                        kind=requirements.kind,
                        constraints=requirements.constraints,
                        limit=8,
                    )
                except VTVError as error:
                    self.events.emit(
                        EventName.ASSET_REJECTED,
                        project_id=project_id,
                        scene_id=scene_id,
                        data={
                            "provider": getattr(provider, "name", "unknown"),
                            "reason": error.info.code.value,
                        },
                    )
                    continue

                for candidate in candidates:
                    if not self._is_clear(candidate, requirements.constraints):
                        self.events.emit(
                            EventName.ASSET_REJECTED,
                            project_id=project_id,
                            scene_id=scene_id,
                            data={
                                "source": candidate.provenance.source.value,
                                "reason": "licence_not_clear",
                                "spdx": candidate.provenance.license.spdx_id,
                            },
                        )
                        continue
                    asset = await self._store(project_id, scene_id, candidate)
                    if asset is not None:
                        return asset
        return None

    @staticmethod
    def _is_clear(
        candidate: AssetCandidate, constraints: MediaSearchConstraints
    ) -> bool:
        """The last gate. Unknown rights fail closed."""
        licence = candidate.provenance.license
        if candidate.provenance.source not in EXTERNAL_SOURCES and (
            candidate.provenance.source is not AssetSource.INTERNAL_LIBRARY
        ):
            return False
        if constraints.require_commercial_use and not licence.commercial_use.is_permitted:
            return False
        if constraints.require_modification and not licence.modification.is_permitted:
            return False
        return not (constraints.exclude_share_alike and licence.share_alike)

    async def _store(
        self, project_id: str, scene_id: str, candidate: AssetCandidate
    ) -> Asset | None:
        if self.fetcher is None:
            return None
        try:
            data, content_type = await self.fetcher.fetch(candidate.download_url)
        except VTVError as error:
            self.events.emit(
                EventName.ASSET_REJECTED,
                project_id=project_id,
                scene_id=scene_id,
                data={"reason": error.info.code.value, "stage": "fetch"},
            )
            return None

        asset = Asset(
            project_id=project_id,
            kind=candidate.kind,
            source=candidate.provenance.source,
            provenance=candidate.provenance,
            dimensions=candidate.dimensions,
            description=candidate.description or candidate.title,
            status=Status.PROCESSING,
        )
        suffix = _suffix_for(content_type, candidate.download_url)
        ref = await self.storage.put(  # type: ignore[attr-defined]
            key=f"projects/{project_id}/assets/{asset.asset_id}{suffix}",
            data=data,
            content_type=content_type,
            retention=RetentionClass.PROJECT,
        )
        asset.object = ref
        asset.status = Status.READY

        self.events.emit(
            EventName.ASSET_RESOLVED,
            project_id=project_id,
            scene_id=scene_id,
            data={
                "asset_id": asset.asset_id,
                "source": asset.source.value,
                "spdx": candidate.provenance.license.spdx_id,
                "attribution_required": candidate.provenance.license.attribution_required,
                "bytes": len(data),
            },
        )
        return asset


def _suffix_for(content_type: str, url: str) -> str:
    for suffix, mime in _CONTENT_TYPES.items():
        if mime == content_type.split(";")[0].strip():
            return suffix
    tail = Path(urlparse(url).path).suffix.lower()
    return tail if tail in _CONTENT_TYPES else ".bin"


def kind_for_content_type(content_type: str) -> AssetKind:
    if content_type.startswith("video/"):
        return AssetKind.VIDEO
    if content_type.startswith("audio/"):
        return AssetKind.AUDIO
    if content_type == "image/svg+xml":
        return AssetKind.VECTOR
    return AssetKind.IMAGE


__all__ = [
    "MAX_ASSET_BYTES",
    "AssetResolver",
    "Fetcher",
    "LocalFileFetcher",
    "kind_for_content_type",
]
