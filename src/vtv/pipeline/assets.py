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

import time
from collections.abc import Callable
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
from vtv.security.paths import tenant_key

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


def _once_each(
    pool: list[tuple[str, AssetCandidate]],
) -> list[tuple[str, AssetCandidate]]:
    """The same file, kept once, under the first query that found it.

    Several queries per shot means the same popular commons file comes back
    several times. Left in, it wastes a download attempt on a repeat — one real
    render fetched the same 404 twice in a row, burning two of its three
    attempts on one missing file — and it lets one file occupy several places in
    the ranking, crowding out the alternatives.

    The *first* query wins because queries are already in the reader's order of
    preference, so the earliest one is the most considered.
    """
    seen: set[str] = set()
    out: list[tuple[str, AssetCandidate]] = []
    for query, candidate in pool:
        if candidate.download_url in seen:
            continue
        seen.add(candidate.download_url)
        out.append((query, candidate))
    return out


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
    #: Ceiling on the whole search, across every provider and every query.
    #:
    #: The loop below is a product of two lists — providers × alternate queries —
    #: and each iteration may wait out an adapter's own HTTP timeout. Two
    #: providers and four queries at fifteen seconds each is two minutes spent
    #: before the ladder descends, which the user experiences as a regeneration
    #: that has hung. A per-request timeout cannot bound a nested loop; only a
    #: deadline on the loop can.
    deadline_seconds: float = 20.0
    #: Injected so a test can move the deadline without sleeping.
    clock: Callable[[], float] = time.monotonic
    #: Search results for this render, keyed by (provider, query, kind).
    #:
    #: Set by the caller when it wants the pool inspected before the ladder runs
    #: — see `product_jobs._judge_commons`, which searches every unit up front so
    #: one model call can judge the whole script. Without it the judge's search
    #: and the resolver's search would be the same HTTP request made twice.
    #:
    #: Per render, not per process: commons results change, and a cache that
    #: outlived a render would make "regenerate this visual" return the picture
    #: the user just rejected.
    cache: dict[tuple[str, str, str], list[AssetCandidate]] | None = None
    #: How many of the ranked candidates to try downloading before giving up.
    #:
    #: More than one because a commons entry can be indexed and still 404 — that
    #: happened three times in one real render — and the second-best relevant
    #: picture is a far better answer than no picture. Not many more than one
    #: because each attempt is a full-size download.
    download_attempts: int = 3

    async def resolve(
        self,
        *,
        organisation_id: str,
        project_id: str,
        scene_id: str,
        requirements: LicensedMediaRequirements,
        #: A candidate the selector already judged best for this shot.
        #:
        #: When present the search is skipped entirely and this is what gets
        #: stored. The licence is still re-checked — that gate is never
        #: bypassed, whoever chose — but the *relevance* decision has already
        #: been made by something that read the narration, and re-deriving it
        #: here with a lexical rule would discard the better answer. That is
        #: precisely what happened when the judge returned only a yes/no: the
        #: agent picked one photograph, the resolver ranked the pool again on
        #: word overlap, and shipped a different one.
        chosen: AssetCandidate | None = None,
    ) -> Asset | None:
        """Return a stored, cleared asset, or ``None`` if nothing is usable.

        ``None`` is a normal outcome, not an error. The caller descends its
        fallback ladder and draws the scene instead.
        """
        if chosen is not None:
            if not self._is_clear(chosen, requirements.constraints):
                self.events.emit(
                    EventName.ASSET_REJECTED,
                    project_id=project_id,
                    scene_id=scene_id,
                    data={"reason": "licence_not_clear", "stage": "preselected"},
                )
                return None
            asset = await self._store(organisation_id, project_id, scene_id, chosen)
            if asset is not None:
                self.events.emit(
                    EventName.ASSET_RESOLVED,
                    project_id=project_id,
                    scene_id=scene_id,
                    data={
                        "asset_id": asset.asset_id,
                        "stage": "judged",
                        "title": (chosen.title or "")[:120],
                    },
                )
                return asset
            # The judged pick will not download — a commons entry can be
            # indexed and still 404, and this one did, twice.
            #
            # We stop here rather than falling through to the search. Falling
            # through would rank the pool on word overlap and ship whatever
            # *that* preferred, which is a photograph the agent did not choose —
            # exactly the behaviour this parameter was added to remove, sneaking
            # back in on the failure path. It is how a line about "systems to
            # accomplish it" came to show a Space Force officer at a podium.
            #
            # The rungs below are a drawing and a generated image, both made for
            # this specific line. Either is a better answer than a photograph
            # nothing endorsed.
            self.events.emit(
                EventName.ASSET_REJECTED,
                project_id=project_id,
                scene_id=scene_id,
                data={
                    "reason": "judged_pick_unavailable",
                    "stage": "preselected",
                    "title": (chosen.title or "")[:120],
                },
            )
            return None

        queries = [requirements.query, *requirements.alternate_queries]
        started = self.clock()
        # Every clear candidate across every provider and every query, with the
        # query that found it. Collected rather than consumed, because a
        # candidate can only be called the best one if it was compared.
        pool: list[tuple[str, AssetCandidate]] = []
        for provider in self.providers:
            for query in queries:
                if self.clock() - started > self.deadline_seconds:
                    self.events.emit(
                        EventName.ASSET_REJECTED,
                        project_id=project_id,
                        scene_id=scene_id,
                        data={
                            "reason": "search_deadline",
                            "seconds": round(self.clock() - started, 2),
                            "collected": len(pool),
                        },
                    )
                    # Rank what was collected rather than discarding it. The
                    # deadline bounds the *search*; a good candidate found in
                    # the first two seconds is not made worse by the fourth
                    # query being slow.
                    return await self._best_of(
                        pool,
                        organisation_id=organisation_id,
                        project_id=project_id,
                        scene_id=scene_id,
                        requirements=requirements,
                    )
                try:
                    candidates = await self._search(provider, query, requirements)
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
                except Exception as error:
                    # Anything at all, not only a `VTVError`. This is a
                    # third-party API response being parsed, which is the one
                    # place in the system where an unexpected value is
                    # guaranteed rather than hypothetical.
                    #
                    # It was `except VTVError` alone, and a Wikimedia scan
                    # 16578 pixels wide raised `ValidationError` from
                    # `AssetDimensions` — not a `VTVError`, so it went past this
                    # handler, past the fallback ladder whose entire purpose is
                    # to descend when a rung fails, and killed the render job.
                    # Three attempts, dead-letter queue, "Something went wrong
                    # on our side." A photograph being unusually large is not a
                    # reason to lose someone's video.
                    #
                    # The narrow handler above is kept because a `VTVError`
                    # carries a code worth recording; this one records the type
                    # so a genuinely new failure is still visible in the log
                    # rather than absorbed silently.
                    self.events.emit(
                        EventName.ASSET_REJECTED,
                        project_id=project_id,
                        scene_id=scene_id,
                        data={
                            "provider": getattr(provider, "name", "unknown"),
                            "reason": "provider_raised",
                            "error": type(error).__name__,
                            "detail": str(error)[:300],
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
                    pool.append((query, candidate))

        return await self._best_of(
            _once_each(pool),
            organisation_id=organisation_id,
            project_id=project_id,
            scene_id=scene_id,
            requirements=requirements,
        )

    async def _best_of(
        self,
        pool: list[tuple[str, AssetCandidate]],
        *,
        organisation_id: str,
        project_id: str,
        scene_id: str,
        requirements: LicensedMediaRequirements,
    ) -> Asset | None:
        """Download the best candidate found, not the first one seen.

        ## What this replaced

        The loop above used to store-and-return inside itself, so the asset that
        ended up in the video was the first search hit with an acceptable
        licence that happened to download. On real data that is a coin toss: the
        commons are keyword-indexed over volunteer-written filenames, and one
        real render illustrated "person clicking on computer" with a photograph
        of a litter-collecting event at Hof railway station, and "systems to
        accomplish it" with a gold military rank insignia. Both were the first
        hit. Neither was ever compared with anything.

        ## Why collecting first is also cheaper

        The old loop downloaded as it went, so it paid for the bytes of every
        candidate it tried — including a 13 MB TIFF it kept and should not have.
        Search returns metadata; only the winner is fetched. Ranking before
        downloading turns *n* downloads into one.

        ## Why an unusable pool is `None` rather than the least-bad hit

        Because the ladder below this has four more rungs, two of which cost
        nothing and both of which are honest. A drawing of the idea beats a
        photograph of the wrong thing, and the entire defect being fixed here
        came from treating "we found bytes" as "we found a picture".
        """
        from vtv.pipeline.selection import Candidate as Judged
        from vtv.pipeline.selection import screen

        if not pool:
            return None

        by_key = {}
        judged: list[Judged] = []
        for index, (query, candidate) in enumerate(pool):
            key = f"c{index}"
            by_key[key] = candidate
            judged.append(
                Judged(
                    key=key,
                    title=candidate.title or "",
                    description=candidate.description or "",
                    query=query,
                    width=candidate.dimensions.width if candidate.dimensions else None,
                    height=candidate.dimensions.height if candidate.dimensions else None,
                    relevance=float(candidate.relevance)
                    if candidate.relevance is not None
                    else None,
                )
            )

        ranked = screen(judged)
        if not ranked:
            self.events.emit(
                EventName.ASSET_REJECTED,
                project_id=project_id,
                scene_id=scene_id,
                data={
                    "reason": "nothing_relevant",
                    "considered": len(pool),
                    # The best of a bad lot, so a reader of the log can see what
                    # was on offer rather than only that nothing was taken.
                    "closest": (judged[0].title or "")[:120],
                },
            )
            return None

        for item in ranked[: self.download_attempts]:
            asset = await self._store(
                organisation_id, project_id, scene_id, by_key[item.candidate.key]
            )
            if asset is not None:
                self.events.emit(
                    EventName.ASSET_RESOLVED,
                    project_id=project_id,
                    scene_id=scene_id,
                    data={
                        "asset_id": asset.asset_id,
                        "stage": "selected",
                        "score": item.score,
                        "considered": len(pool),
                        "why": item.reason[:200],
                    },
                )
                return asset
        return None

    async def _search(
        self, provider: object, query: str, requirements: LicensedMediaRequirements
    ) -> list[AssetCandidate]:
        """One search, through the render's cache when there is one."""
        key = (getattr(provider, "name", "unknown"), query, requirements.kind.value)
        if self.cache is not None and key in self.cache:
            return self.cache[key]
        found = await provider.search(  # type: ignore[attr-defined]
            query=query,
            kind=requirements.kind,
            constraints=requirements.constraints,
            limit=8,
        )
        if self.cache is not None:
            self.cache[key] = found
        return found

    async def pool_for(
        self,
        *,
        project_id: str,
        scene_id: str,
        requirements: LicensedMediaRequirements,
    ) -> list[tuple[str, AssetCandidate]]:
        """Search only: every clear candidate, with the query that found it.

        Downloads nothing. Exists so the whole script's candidates can be
        gathered and judged in one model call before any of them is fetched —
        the alternative being a judgement per shot, which is a model call per
        shot, which is the cost shape this system has twice had to remove.
        """
        pool: list[tuple[str, AssetCandidate]] = []
        started = self.clock()
        for provider in self.providers:
            for query in [requirements.query, *requirements.alternate_queries]:
                if self.clock() - started > self.deadline_seconds:
                    return pool
                try:
                    candidates = await self._search(provider, query, requirements)
                except Exception:
                    # Same reasoning as `resolve`: this parses a third-party
                    # response, and a search that fails is a shot without a
                    # photograph, never a render without a video.
                    continue
                pool.extend(
                    (query, candidate)
                    for candidate in candidates
                    if self._is_clear(candidate, requirements.constraints)
                )
        pool = _once_each(pool)
        del project_id, scene_id
        return pool

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
        self,
        organisation_id: str,
        project_id: str,
        scene_id: str,
        candidate: AssetCandidate,
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
            key=tenant_key(
                organisation_id,
                "projects",
                project_id,
                "assets",
                f"{asset.asset_id}{suffix}",
            ),
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
