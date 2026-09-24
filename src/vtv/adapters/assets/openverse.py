"""Openverse and Wikimedia Commons asset search.

STATUS: **REAL IMPLEMENTATION — REQUIRES NETWORK ACCESS.**
Not executed in this environment (no outbound network). The licence-parsing
logic below *is* exercised by tests against recorded fixtures, because that is
the part that can quietly cost the company money if it is wrong.

The rule this adapter exists to enforce: **filtering happens here, not in the
caller.** Openverse knows what its licence fields mean; the Visual Director does
not and should never have to. A candidate whose rights cannot be established is
not returned at all — an empty result is a correct answer, and the Director will
fall back to drawing the scene itself.
"""

from __future__ import annotations

from typing import Any

from vtv.contracts.asset import (
    AssetDimensions,
    AssetKind,
    AssetProvenance,
    AssetSource,
    License,
    Permission,
)
from vtv.contracts.errors import ProviderError, TimeoutExceeded
from vtv.contracts.visual_plan import MediaSearchConstraints
from vtv.ports.assets import AssetCandidate
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth

#: SPDX identifier → (commercial use, modification, attribution, share-alike).
#: Anything not in this table is treated as UNKNOWN, which means unusable. Adding
#: a licence here is a deliberate legal decision, not a convenience.
LICENSE_TABLE: dict[str, tuple[Permission, Permission, bool, bool]] = {
    "CC0-1.0": (Permission.ALLOWED, Permission.ALLOWED, False, False),
    "PDM-1.0": (Permission.ALLOWED, Permission.ALLOWED, False, False),
    "CC-BY-4.0": (Permission.ALLOWED, Permission.ALLOWED, True, False),
    "CC-BY-3.0": (Permission.ALLOWED, Permission.ALLOWED, True, False),
    "CC-BY-2.0": (Permission.ALLOWED, Permission.ALLOWED, True, False),
    "CC-BY-SA-4.0": (Permission.ALLOWED, Permission.ALLOWED, True, True),
    "CC-BY-SA-3.0": (Permission.ALLOWED, Permission.ALLOWED, True, True),
    "CC-BY-SA-2.0": (Permission.ALLOWED, Permission.ALLOWED, True, True),
    # Explicitly denied rather than omitted, so the reason is recorded.
    "CC-BY-NC-4.0": (Permission.DENIED, Permission.ALLOWED, True, False),
    "CC-BY-NC-SA-4.0": (Permission.DENIED, Permission.ALLOWED, True, True),
    "CC-BY-ND-4.0": (Permission.ALLOWED, Permission.DENIED, True, False),
}

#: Sent on every request. Wikimedia's API policy requires a descriptive
#: User-Agent and answers 403 without one — which arrives here as
#: `provider_unavailable` and reads like an outage rather than a missing header.
USER_AGENT = (
    "VoiceToVideo/1.0 (openly-licensed media search; "
    "https://github.com/voice-to-video)"
)


#: What the contract allows for an attribution line.
#:
#: Wikimedia's `Artist` field is free-form wiki markup and is sometimes a whole
#: licence notice — one real render lost a candidate to
#: "This Photo was taken by … derivative or extract.", which is 1 400 characters
#: and raised `ValidationError` from `AssetProvenance`.
#:
#: Losing an otherwise-usable photograph because its credit is long is the wrong
#: trade: the credit is what we owe the photographer, and a truncated credit
#: still names them, while no photograph credits nobody.
_MAX_CREATOR_CHARS = 280


def _creator(raw: object) -> str | None:
    """An attribution line the contract will accept."""
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) <= _MAX_CREATOR_CHARS:
        return text
    return text[: _MAX_CREATOR_CHARS - 1].rstrip() + "…"


def dimensions_or_none(width: object, height: object) -> AssetDimensions | None:
    """Dimensions when they fit the contract, `None` when they do not.

    ## Why this is not just `AssetDimensions(...)`

    It was, and a single photograph took down a whole render. Wikimedia hosts
    scans far larger than any screen: one result came back 16578 pixels wide,
    `AssetDimensions` caps width at 16384, and pydantic raised `ValidationError`
    — which is not a `VTVError`, so it went straight past the resolver's
    handler, past the ladder that exists to descend on exactly this, and killed
    the `render_scope` job. Three attempts, then the dead-letter queue, and what
    the user saw was "Something went wrong on our side."

    The image was fine. Only the *metadata* failed a sanity bound, and metadata
    is used for ranking, not for rendering — the fetch is bounded separately by
    `MAX_ASSET_BYTES`. So an out-of-range measurement is dropped and the
    candidate survives, which is the proportionate response to a number being
    bigger than expected.
    """
    try:
        return AssetDimensions(width=int(width), height=int(height))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


#: Openverse reports licences as ``(license, version)`` pairs in lowercase.
def spdx_from_openverse(license_name: str, version: str | None) -> str | None:
    name = (license_name or "").strip().lower()
    if not name:
        return None
    if name in {"cc0", "zero"}:
        return "CC0-1.0"
    if name in {"pdm", "publicdomain"}:
        return "PDM-1.0"
    if not version:
        return None
    return f"CC-{name.upper()}-{version}"


def license_from_spdx(spdx: str | None, url: str | None = None) -> License:
    """Build a `License`, defaulting to unusable when the identifier is unknown."""
    if spdx and spdx in LICENSE_TABLE:
        commercial, modification, attribution, share_alike = LICENSE_TABLE[spdx]
        return License(
            spdx_id=spdx,
            name=spdx,
            url=url,
            commercial_use=commercial,
            modification=modification,
            attribution_required=attribution,
            share_alike=share_alike,
        )
    # Unknown identifier: record what we saw, permit nothing.
    return License(
        spdx_id=spdx,
        name=spdx or "unrecognised licence",
        url=url,
        commercial_use=Permission.UNKNOWN,
        modification=Permission.UNKNOWN,
        attribution_required=True,
    )


class OpenverseAssetSearchProvider:
    """Search Openverse for openly-licensed media."""

    def __init__(
        self,
        *,
        endpoint: str = "https://api.openverse.org/v1",
        api_key: str | None = None,
        # Eight seconds, not fifteen. This adapter is called several times
        # inside one ladder rung — once per alternate query, for each of two
        # providers — and the user is waiting on the whole rung, not on any one
        # request. `AssetResolver.deadline_seconds` bounds the rung; this bounds
        # a single hung connection within it.
        timeout_seconds: float = 8.0,
        name: str = "openverse",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.name = name
        self._failures = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            unit_cost_usd=0.0,
            unit="search",
            typical_latency_seconds=2.0,
            # Only a search query leaves the building, never user audio.
            data_policy=DataPolicy(
                retains_input=False, trains_on_input=False, dpa_in_place=True
            ),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(consecutive_failures=self._failures)

    async def search(
        self,
        *,
        query: str,
        kind: AssetKind = AssetKind.IMAGE,
        constraints: MediaSearchConstraints,
        limit: int = 12,
    ) -> list[AssetCandidate]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("httpx is required for Openverse search") from exc

        path = "images" if kind is AssetKind.IMAGE else "audio"
        # Ask the API to filter too. Belt and braces: the response is re-checked
        # below, because a provider-side filter is a convenience, not a promise.
        licences = "cc0,pdm,by,by-sa" if not constraints.exclude_share_alike else "cc0,pdm,by"
        params: dict[str, Any] = {
            "q": query,
            "page_size": min(limit * 2, 40),
            "license": licences,
            "license_type": "commercial,modification",
            "mature": "false",
        }
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    f"{self.endpoint}/{path}/", params=params, headers=headers
                )
        except Exception as exc:
            self._failures += 1
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutExceeded("openverse search timed out") from exc
            raise ProviderError(f"openverse search failed: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            self._failures += 1
            raise ProviderError(f"openverse returned {response.status_code}")
        self._failures = 0
        return self.parse(response.json(), constraints=constraints, limit=limit, kind=kind)

    def parse(
        self,
        payload: dict[str, Any],
        *,
        constraints: MediaSearchConstraints,
        limit: int,
        kind: AssetKind = AssetKind.IMAGE,
    ) -> list[AssetCandidate]:
        """Map a response to candidates, dropping anything we cannot clear.

        Separated from the HTTP call so it can be tested against fixtures with no
        network — this is the function whose correctness has legal consequences.
        """
        candidates: list[AssetCandidate] = []
        for item in payload.get("results") or []:
            url = item.get("url")
            identifier = item.get("id")
            if not url or not identifier:
                continue

            spdx = spdx_from_openverse(item.get("license"), item.get("license_version"))
            licence = license_from_spdx(spdx, item.get("license_url"))
            if constraints.require_commercial_use and not licence.commercial_use.is_permitted:
                continue
            if constraints.require_modification and not licence.modification.is_permitted:
                continue
            if constraints.exclude_share_alike and licence.share_alike:
                continue

            width = item.get("width")
            height = item.get("height")
            if kind is AssetKind.IMAGE:
                if not width or not height:
                    continue  # unknown size: cannot guarantee it will not be soft
                if width < constraints.min_width or height < constraints.min_height:
                    continue

            candidates.append(
                AssetCandidate(
                    provenance=AssetProvenance(
                        source=AssetSource.OPENVERSE,
                        source_id=str(identifier),
                        original_url=item.get("foreign_landing_url") or url,
                        title=(item.get("title") or None),
                        creator=_creator(item.get("creator")),
                        creator_url=item.get("creator_url"),
                        license=licence,
                        raw_metadata={
                            key: str(item.get(key))
                            for key in ("license", "license_version", "source", "provider")
                            if item.get(key) is not None
                        },
                    ),
                    kind=kind,
                    download_url=url,
                    preview_url=item.get("thumbnail"),
                    dimensions=dimensions_or_none(width, height),
                    title=item.get("title"),
                    description=item.get("description"),
                )
            )
            if len(candidates) >= limit:
                break
        return candidates


class WikimediaAssetSearchProvider(OpenverseAssetSearchProvider):
    """Wikimedia Commons search.

    Commons hosts material under many different licences, so "found on Commons"
    tells you nothing about rights — every item's own licence governs. The
    response shape differs from Openverse but the clearing rules are identical,
    so only `parse` is overridden.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("endpoint", "https://commons.wikimedia.org/w/api.php")
        kwargs.setdefault("name", "wikimedia-commons")
        super().__init__(**kwargs)

    async def search(
        self,
        *,
        query: str,
        kind: AssetKind = AssetKind.IMAGE,
        constraints: MediaSearchConstraints,
        limit: int = 12,
    ) -> list[AssetCandidate]:
        """MediaWiki's `action=query` API, which is nothing like Openverse's.

        ## Why this had to be overridden and never was

        This class overrode `parse` alone, and inherited a `search` that builds
        `{endpoint}/images/?q=…` with Openverse's query parameters. Against
        `https://commons.wikimedia.org/w/api.php` that produces

            https://commons.wikimedia.org/w/api.php/images/?q=…

        which is not an endpoint. MediaWiki answers 301, the redirect lands on
        HTML, and `response.json()` raises `JSONDecodeError` — so **every
        Wikimedia search this system has ever made has failed**, silently,
        behind an `asset.rejected` event that read like an outage.

        Inheriting the HTTP call and overriding only the parsing was the
        mistake: the response shape and the *request* shape are one decision,
        and splitting them across a subclass boundary let the parser be
        correct for a request that was never sent.

        MediaWiki wants a generator search over the File namespace, asking for
        `imageinfo` with `extmetadata` — which is where the licence lives, and
        which is what `parse` below has always expected.
        """
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderError("httpx is required for Wikimedia search") from exc

        if kind is not AssetKind.IMAGE:
            # Commons hosts video and audio, but `parse` reads image metadata
            # and nothing here has been checked against the others.
            return []

        params: dict[str, Any] = {
            "action": "query",
            "format": "json",
            "formatversion": "1",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            # Namespace 6 is File:. Without it the search returns articles,
            # which have no `imageinfo` and parse to nothing.
            "gsrnamespace": 6,
            "gsrlimit": min(max(limit, 1), 20),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            # A scaled rendition rather than the original. Commons hosts
            # scans tens of thousands of pixels wide, and downloading one to
            # fill a 1920-pixel frame is bytes nobody needs.
            "iiurlwidth": 1600,
        }
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=True
            ) as client:
                response = await client.get(self.endpoint, params=params, headers=headers)
        except Exception as exc:
            self._failures += 1
            if "timeout" in type(exc).__name__.lower():
                raise TimeoutExceeded("wikimedia search timed out") from exc
            raise ProviderError(
                f"wikimedia search failed: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            self._failures += 1
            raise ProviderError(f"wikimedia returned {response.status_code}")
        self._failures = 0
        return self.parse(response.json(), constraints=constraints, limit=limit, kind=kind)

    def parse(
        self,
        payload: dict[str, Any],
        *,
        constraints: MediaSearchConstraints,
        limit: int,
        kind: AssetKind = AssetKind.IMAGE,
    ) -> list[AssetCandidate]:
        candidates: list[AssetCandidate] = []
        pages = (payload.get("query") or {}).get("pages") or {}
        for page in pages.values():
            for info in page.get("imageinfo") or []:
                extmetadata = info.get("extmetadata") or {}

                def value(key: str, source: dict[str, Any] = extmetadata) -> str | None:
                    entry = source.get(key) or {}
                    return entry.get("value")

                spdx = _normalise_commons_licence(value("LicenseShortName"))
                licence = license_from_spdx(spdx, value("LicenseUrl"))
                if constraints.require_commercial_use and not licence.commercial_use.is_permitted:
                    continue
                if constraints.require_modification and not licence.modification.is_permitted:
                    continue
                if constraints.exclude_share_alike and licence.share_alike:
                    continue

                width, height = info.get("width"), info.get("height")
                if kind is AssetKind.IMAGE and (
                    not width
                    or not height
                    or width < constraints.min_width
                    or height < constraints.min_height
                ):
                    continue

                candidates.append(
                    AssetCandidate(
                        provenance=AssetProvenance(
                            source=AssetSource.WIKIMEDIA_COMMONS,
                            source_id=str(page.get("title") or info.get("url")),
                            original_url=info.get("descriptionurl"),
                            title=page.get("title"),
                            creator=_creator(_strip_html(value("Artist"))),
                            license=licence,
                            raw_metadata={
                                "LicenseShortName": str(value("LicenseShortName")),
                                "UsageTerms": str(value("UsageTerms")),
                            },
                        ),
                        kind=kind,
                        download_url=str(info.get("url")),
                        preview_url=info.get("thumburl"),
                        dimensions=dimensions_or_none(width, height),
                        title=page.get("title"),
                    )
                )
                if len(candidates) >= limit:
                    return candidates
        return candidates


def _normalise_commons_licence(short_name: str | None) -> str | None:
    if not short_name:
        return None
    text = short_name.strip().upper().replace(" ", "-")
    mapping = {
        "CC0": "CC0-1.0",
        "PUBLIC-DOMAIN": "PDM-1.0",
        "CC-BY-4.0": "CC-BY-4.0",
        "CC-BY-3.0": "CC-BY-3.0",
        "CC-BY-2.0": "CC-BY-2.0",
        "CC-BY-SA-4.0": "CC-BY-SA-4.0",
        "CC-BY-SA-3.0": "CC-BY-SA-3.0",
        "CC-BY-SA-2.0": "CC-BY-SA-2.0",
    }
    return mapping.get(text)


def _strip_html(value: str | None) -> str | None:
    if not value:
        return None
    import re

    return re.sub(r"<[^>]+>", "", value).strip() or None


__all__ = [
    "LICENSE_TABLE",
    "USER_AGENT",
    "OpenverseAssetSearchProvider",
    "WikimediaAssetSearchProvider",
    "dimensions_or_none",
    "license_from_spdx",
    "spdx_from_openverse",
]
