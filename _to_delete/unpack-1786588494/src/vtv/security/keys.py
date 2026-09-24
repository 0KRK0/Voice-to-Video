"""API key minting and verification.

The design constraint is that a database dump must not be a list of working
credentials. So the secret exists in exactly one place — the HTTP response to
the call that created it — and what is stored is a digest.

Three details that are easy to get wrong and expensive to get wrong:

**Constant-time comparison.** `==` on a digest leaks its prefix through timing.
`hmac.compare_digest` does not. This is cheap insurance against an attack that
is genuinely practical against a network service.

**A public prefix.** Stored in the clear so a key is identifiable in a log, an
audit entry and a UI list without being recoverable. "Revoke the key starting
vtv_live_7f3a" is a usable instruction; "revoke one of your nine keys" is not.

**Enough entropy.** 32 bytes from `secrets.token_urlsafe`, which is 256 bits of
CSPRNG output. Not `random`, not a UUID, not a hash of the time.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from vtv.contracts.base import utc_now
from vtv.contracts.errors import PolicyViolation, ValidationFailed
from vtv.contracts.tenancy import ApiKey, Capability, Role

#: Marks the environment in the key itself, so a test key pasted into a
#: production config is visible to a human before it is rejected by a machine.
LIVE_PREFIX = "vtv_live"
TEST_PREFIX = "vtv_test"

#: Bytes of CSPRNG entropy behind every key.
SECRET_BYTES = 32

#: How much of the token is public. Long enough to identify a key among a
#: tenant's keys, far too short to brute-force the rest.
PREFIX_LENGTH = 4

_KEY_SHAPE = re.compile(r"^vtv_(live|test)_([A-Za-z0-9_-]{20,120})$")


@dataclass(frozen=True)
class ApiKeySecret:
    """A freshly minted key: the record to store and the secret to show once."""

    record: ApiKey
    #: The only time this value exists. Never logged, never persisted, never
    #: returned by any endpoint other than the one that created it.
    secret: str

    def __repr__(self) -> str:
        # A dataclass repr would put the secret in every traceback, log line and
        # debugger session that touches this object.
        return f"ApiKeySecret(record={self.record.api_key_id!r}, secret=<redacted>)"


def hash_secret(secret: str) -> str:
    """SHA-256 of a key secret, hex encoded.

    A plain hash rather than a password KDF, deliberately: an API key is 256
    bits of random, so there is no dictionary to attack and no work factor worth
    paying on every single request. A *user password* is the opposite case and
    must use Argon2 or bcrypt — see `docs/SECURITY.md`.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_secret(secret: str, digest: str) -> bool:
    """Constant-time check of a presented secret against a stored digest."""
    return hmac.compare_digest(hash_secret(secret), digest)


def parse_key(secret: str) -> tuple[str, str]:
    """Split a presented key into ``(environment, body)``.

    Raises rather than returning a sentinel: a malformed key is a request that
    should be refused at the edge, not a value that flows onward as ``None``.
    """
    match = _KEY_SHAPE.match(secret.strip())
    if match is None:
        raise ValidationFailed("that is not a valid API key")
    return match.group(1), match.group(2)


def public_prefix(secret: str) -> str:
    """The identifying, non-secret part of a key."""
    environment, body = parse_key(secret)
    return f"vtv_{environment}_{body[:PREFIX_LENGTH]}"


def mint_api_key(
    *,
    organisation_id: str,
    name: str,
    role: Role = Role.SERVICE,
    scopes: list[Capability] | None = None,
    created_by: str | None = None,
    expires_in_days: int | None = 365,
    live: bool = True,
) -> ApiKeySecret:
    """Create a key. The secret is returned once and never stored.

    An expiry is set by default. A credential that never expires is a credential
    that outlives the person who created it and the reason it existed, which is
    how long-forgotten keys end up in a breach report.
    """
    if not organisation_id:
        raise ValidationFailed("an API key must belong to an organisation")
    if role is Role.OWNER:
        # A machine credential that can delete the organisation and change
        # billing is not a credential anybody should be able to mint by
        # accident. Owners act as themselves.
        raise PolicyViolation("API keys may not hold the owner role")

    body = secrets.token_urlsafe(SECRET_BYTES)
    environment = LIVE_PREFIX if live else TEST_PREFIX
    secret = f"{environment}_{body}"

    expires_at: datetime | None = None
    if expires_in_days is not None:
        expires_at = utc_now() + timedelta(days=expires_in_days)

    record = ApiKey(
        organisation_id=organisation_id,
        name=name[:120],
        prefix=f"{environment}_{body[:PREFIX_LENGTH]}",
        secret_hash=hash_secret(secret),
        role=role,
        scopes=list(scopes or []),
        created_by=created_by,
        expires_at=expires_at,
    )
    return ApiKeySecret(record=record, secret=secret)


__all__ = [
    "LIVE_PREFIX",
    "PREFIX_LENGTH",
    "SECRET_BYTES",
    "TEST_PREFIX",
    "ApiKeySecret",
    "hash_secret",
    "mint_api_key",
    "parse_key",
    "public_prefix",
    "verify_secret",
]
