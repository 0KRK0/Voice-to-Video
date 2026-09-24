"""Downloading remote media, safely.

STATUS: **REAL IMPLEMENTATION — REQUIRES NETWORK ACCESS.** The URL validation is
exercised by tests; the download itself cannot run in this environment.

This lives in an adapter rather than in the pipeline for the reason the whole
architecture exists: it imports an HTTP client, and vendor code belongs behind a
port. `tests/test_architecture_boundaries.py` enforces that, and caught this
file being in the wrong place.

The checks below are all SSRF defences. A media URL arrives inside a
third-party API response, which makes it attacker-influenced data: an allowlisted
hostname that resolves to 169.254.169.254 is the classic cloud-metadata attack,
and a redirect is the classic way around a naive check.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from vtv.contracts.errors import NotFound, PolicyViolation, VTVError
from vtv.pipeline.assets import MAX_ASSET_BYTES

#: Hosts we are willing to fetch media from. An allowlist rather than a
#: blocklist: the set of media sources we use is small and known, and a
#: blocklist can always be walked around.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "upload.wikimedia.org",
        "commons.wikimedia.org",
        "api.openverse.org",
        "openverse-api.onrender.com",
        "live.staticflickr.com",
        "farm1.staticflickr.com",
        "images.openverse.engineering",
    }
)


class HttpFetcher:
    """Downloads remote media, with SSRF protections.

    STATUS: real implementation; requires network access, unavailable here.
    """

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
        max_bytes: int = MAX_ASSET_BYTES,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    def check_url(self, url: str) -> str:
        """Validate a URL before fetching it. Raises on anything suspicious."""
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise PolicyViolation(f"refusing non-HTTPS media URL: {parsed.scheme}")
        host = (parsed.hostname or "").lower()
        if host not in self.allowed_hosts:
            raise PolicyViolation(f"host {host!r} is not an allowed media source")
        # Resolve and reject private space: a hostname on the allowlist that
        # resolves to 169.254.169.254 is the classic cloud-metadata attack.
        try:
            for info in socket.getaddrinfo(host, None):
                address = ipaddress.ip_address(info[4][0])
                if (
                    address.is_private
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_reserved
                ):
                    raise PolicyViolation(f"{host} resolves to a non-public address")
        except socket.gaierror as exc:
            raise NotFound(f"cannot resolve media host {host}") from exc
        return url

    async def fetch(self, url: str) -> tuple[bytes, str]:
        self.check_url(url)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VTVError("httpx is required to fetch remote media") from exc

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            # Redirects are disabled deliberately: following one would bypass
            # every check above.
            response = await client.get(url)
            if response.status_code >= 400:
                raise NotFound(f"media download returned {response.status_code}")
            content = response.content
            if len(content) > self.max_bytes:
                raise PolicyViolation("media exceeds the size limit")
            return content, response.headers.get("content-type", "application/octet-stream")


__all__ = ["DEFAULT_ALLOWED_HOSTS", "HttpFetcher"]
