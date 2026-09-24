"""Outbound request safety — SSRF defence.

The platform fetches URLs that came from somewhere it does not control: a media
URL inside a provider's JSON response, a page a user asked to summarise, an
image referenced by a document. Every one of those is attacker-influenced, and
a server that will fetch an arbitrary URL on request is a proxy into its own
private network.

The classic target is `169.254.169.254`, the cloud instance-metadata endpoint,
which on an unhardened instance hands out credentials to anything that asks.
Also in scope: `localhost` (the admin interface nobody exposed on purpose),
RFC1918 space (everything else in the VPC), and `file://` (the local disk).

What actually stops it, in order of how often each is missed:

1. **Scheme allowlist.** `http`/`https` only. `file:`, `gopher:`, `ftp:` and
   `data:` have no business here.
2. **Resolve, then check every address.** A hostname is not an address. An
   attacker controls DNS for their own domain and can point it at 127.0.0.1.
   Every address the name resolves to must be public, not just the first.
3. **Re-check on every redirect.** A public URL that 302s to the metadata
   endpoint defeats a check performed only on the original URL. This is the
   single most commonly missed step.
4. **Refuse credentials and odd ports.** `http://user:pass@host` and port 22
   are not media fetches.

DNS rebinding — where the name resolves to a public address during the check
and a private one during the fetch — is *not* fully solved by validation. The
real fix is to connect to the validated address directly rather than re-resolve,
which requires control of the socket. :meth:`UrlGuard.resolved_addresses`
exposes what was validated so an HTTP client that supports pinning can use it;
until one is wired in, this is a documented residual risk rather than a solved
problem.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

from vtv.contracts.errors import PolicyViolation, ValidationFailed

#: The only schemes worth fetching.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Ports a media or page fetch plausibly uses. Everything else is somebody
#: probing the internal network through us.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})

#: Never resolvable to anything we should talk to, whatever DNS says.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

MAX_URL_LENGTH = 2048


def is_public_address(address: str) -> bool:
    """Whether an IP literal is routable on the public internet.

    Covers every non-public category the standard library knows about, plus
    IPv4-mapped IPv6 (`::ffff:127.0.0.1`), which is the standard way to smuggle
    a loopback address past a check that only understands IPv4.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False

    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return is_public_address(str(parsed.ipv4_mapped))
        # 6to4 and Teredo embed an IPv4 address that could be private.
        if parsed.sixtofour is not None:
            return is_public_address(str(parsed.sixtofour))
        if parsed.teredo is not None:
            return is_public_address(str(parsed.teredo[1]))

    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )


@dataclass
class UrlGuard:
    """Validates a URL before anything fetches it.

    ``allowed_hosts`` empty means "any public host", which is right for a
    user-supplied page to summarise. A non-empty set is an allowlist, which is
    right for provider media: that set is small and known, and an allowlist
    cannot be walked around the way a blocklist can.
    """

    allowed_hosts: frozenset[str] = frozenset()
    allowed_schemes: frozenset[str] = ALLOWED_SCHEMES
    allowed_ports: frozenset[int] = ALLOWED_PORTS
    require_https: bool = True
    #: Injected so tests can exercise the private-address branch without DNS.
    resolver: object = None
    _validated: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def check(self, url: str) -> str:
        """Validate a URL. Returns it unchanged, or raises."""
        if not url or len(url) > MAX_URL_LENGTH:
            raise ValidationFailed("URL is missing or implausibly long")

        parsed = urlparse(url.strip())
        scheme = (parsed.scheme or "").lower()
        if scheme not in self.allowed_schemes:
            raise PolicyViolation(f"refusing scheme {scheme or 'none'!r}")
        if self.require_https and scheme != "https":
            raise PolicyViolation("refusing a non-HTTPS fetch")

        if parsed.username or parsed.password:
            # Credentials in a URL are either a mistake or an attempt to reach
            # something that requires them, and neither belongs in a media fetch.
            raise PolicyViolation("refusing a URL carrying credentials")

        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise ValidationFailed("URL has no host")
        if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
            raise PolicyViolation(f"refusing host {host!r}")

        port = parsed.port or (443 if scheme == "https" else 80)
        if port not in self.allowed_ports:
            raise PolicyViolation(f"refusing port {port}")

        if self.allowed_hosts and host not in self.allowed_hosts:
            raise PolicyViolation(f"host {host!r} is not an allowed source")

        addresses = self._resolve(host)
        if not addresses:
            raise ValidationFailed(f"cannot resolve {host}")
        for address in addresses:
            if not is_public_address(address):
                # Naming the address rather than just the host is what makes an
                # attempted metadata-endpoint fetch legible in the log.
                raise PolicyViolation(
                    f"{host} resolves to non-public address {address}"
                )

        self._validated[url] = tuple(addresses)
        return url

    def check_redirect(self, original: str, location: str) -> str:
        """Validate a redirect target with the same rules as the original.

        Called by the fetching adapter on every hop. A guard applied once, to
        the URL the caller supplied, protects nothing: the attacker's own server
        answers with a 302 to wherever it likes.
        """
        del original
        return self.check(location)

    def resolved_addresses(self, url: str) -> tuple[str, ...]:
        """The addresses this URL validated against.

        Exposed so a client that can pin a connection to an address avoids the
        re-resolution window that DNS rebinding exploits.
        """
        return self._validated.get(url, ())

    def _resolve(self, host: str) -> list[str]:
        # An IP literal needs no DNS, and passing one to getaddrinfo would be a
        # round trip to learn what we already have.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return [host]

        if self.resolver is not None:
            return list(self.resolver(host))  # type: ignore[operator]
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return []
        return [str(info[4][0]) for info in infos]


__all__ = [
    "ALLOWED_PORTS",
    "ALLOWED_SCHEMES",
    "BLOCKED_HOSTNAMES",
    "MAX_URL_LENGTH",
    "UrlGuard",
    "is_public_address",
]
