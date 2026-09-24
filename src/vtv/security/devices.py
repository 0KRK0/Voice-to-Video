"""Minting the two credentials the local-execution layer needs.

A pairing code and a device token are different kinds of secret and are
deliberately built differently.

**The pairing code** is typed by a human, off one screen and into another. So it
is short, uppercase, and drawn from an alphabet with no character anybody
misreads: no ``O`` against ``0``, no ``I`` or ``1`` or ``L``. That costs
entropy — six characters of a 24-letter alphabet is about 27 bits — and the
cost is paid back by making the window ten minutes and the use single. Guessing
27 bits inside ten minutes against a rate limiter is not an attack anybody
mounts; asking a user to type 43 characters of base64 is a product nobody uses.

**The device token** is never typed by anybody. It is 256 bits of CSPRNG,
written to a file on the machine that redeemed the code, and presented on every
request thereafter. It follows exactly the `security/keys.py` design — public
prefix stored in the clear, digest for the rest, constant-time comparison —
because that design is right and having two of them would mean having two to
review.

## Why the token is not an API key

`mint_api_key` would work, and using it would be a mistake. An API key carries a
`Role` whose capabilities are the organisation's; a device must carry exactly
`DEVICE_EXECUTE`. Sharing the mint would mean one careless `role=` argument away
from a laptop in a coffee shop holding a credential that can create projects and
spend money. Different powers, different constructor, and the type system says
so.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from vtv.contracts.base import utc_now
from vtv.contracts.devices import PAIRING_TTL_SECONDS, Device, DeviceHardware, PairingCode
from vtv.contracts.errors import ValidationFailed

#: Marks a device token apart from an API key at a glance, in a log line or a
#: support ticket, before anything has tried to authenticate it.
DEVICE_PREFIX = "vtv_dev"

#: Bytes of CSPRNG entropy behind every device token.
TOKEN_BYTES = 32

#: How much of the token is public: enough to name one device among a tenant's
#: devices, far too little to brute-force the rest.
PREFIX_LENGTH = 4

#: No O/0, no I/1/L, no U — the characters people transcribe wrongly, and the
#: one that turns codes into words nobody wants read aloud in an office.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Six characters, shown grouped as ``ABC-DEF``. About 29 bits; see the module
#: docstring for why that is the right trade here and would not be elsewhere.
CODE_LENGTH = 6

_TOKEN_SHAPE = re.compile(r"^vtv_dev_([A-Za-z0-9_-]{20,120})$")
_CODE_SHAPE = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")


@dataclass(frozen=True)
class DeviceToken:
    """A freshly paired device: the record to store, the token to show once."""

    record: Device
    #: The only moment this value exists on the server. Returned by the pairing
    #: call, written to disk by the desktop, never persisted here and never
    #: logged.
    secret: str

    def __repr__(self) -> str:
        # A dataclass repr would put the token in every traceback and debugger
        # session that happens to touch this object.
        return f"DeviceToken(record={self.record.device_id!r}, secret=<redacted>)"


def hash_token(secret: str) -> str:
    """SHA-256 of a device token, hex encoded.

    A plain hash and not a password KDF, for the same reason as an API key: this
    is 256 bits of random with no dictionary behind it, and a work factor would
    be paid on every request a busy device makes while changing nothing about
    how hard it is to guess.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_token(secret: str, digest: str) -> bool:
    """Constant-time check of a presented token against a stored digest."""
    return hmac.compare_digest(hash_token(secret), digest)


def parse_token(secret: str) -> str:
    """The body of a presented device token, or a refusal.

    Raises rather than returning `None`: a malformed token is a request to
    refuse at the edge, not a value that flows onward for somebody to forget to
    check.
    """
    match = _TOKEN_SHAPE.match(secret.strip())
    if match is None:
        raise ValidationFailed("that is not a valid device token")
    return match.group(1)


def token_prefix(secret: str) -> str:
    """The identifying, non-secret part of a device token."""
    return f"{DEVICE_PREFIX}_{parse_token(secret)[:PREFIX_LENGTH]}"


def normalise_code(raw: str) -> str:
    """What the user typed, as the code actually is.

    People type spaces, hyphens and lowercase, and a pairing step that rejects
    ``abc-def`` for ``ABCDEF`` is a pairing step that generates support tickets.
    What it does *not* do is repair characters: mapping ``0`` to ``O`` would
    quietly widen the alphabet and shrink the search space, which is the wrong
    direction for a credential.
    """
    cleaned = re.sub(r"[\s-]", "", raw).upper()
    if not _CODE_SHAPE.match(cleaned):
        raise ValidationFailed("that is not a valid pairing code")
    return cleaned


def format_code(code: str) -> str:
    """``ABCDEF`` as ``ABC-DEF``, for showing on a screen."""
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


def mint_pairing_code(
    *,
    organisation_id: str,
    created_by: str | None = None,
    ttl_seconds: int = PAIRING_TTL_SECONDS,
) -> PairingCode:
    """A code for one computer, good for a few minutes and one use."""
    if not organisation_id:
        raise ValidationFailed("a pairing code must belong to an organisation")
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return PairingCode(
        code=code,
        organisation_id=organisation_id,
        created_by=created_by,
        expires_at=utc_now() + timedelta(seconds=ttl_seconds),
    )


def mint_device(
    *,
    organisation_id: str,
    name: str,
    hardware: DeviceHardware | None = None,
    paired_by: str | None = None,
) -> DeviceToken:
    """Create a device and its token. The token is returned once.

    No expiry, unlike an API key, and that is a considered difference rather
    than an oversight. An API key expires because it tends to be pasted into a
    config file and forgotten by everyone including the person who made it. A
    device is a physical object somebody owns: it appears in a list with a name
    and a last-seen time, and it stops working when its owner revokes it or
    when the machine is gone. An expiry would mean a customer's render farm
    silently going dark on a date nobody wrote down.
    """
    if not organisation_id:
        raise ValidationFailed("a device must belong to an organisation")
    if not name.strip():
        raise ValidationFailed("a device needs a name somebody will recognise")

    body = secrets.token_urlsafe(TOKEN_BYTES)
    secret = f"{DEVICE_PREFIX}_{body}"
    now = utc_now()
    record = Device(
        organisation_id=organisation_id,
        name=name.strip()[:120],
        hardware=hardware or DeviceHardware(),
        prefix=f"{DEVICE_PREFIX}_{body[:PREFIX_LENGTH]}",
        token_hash=hash_token(secret),
        paired_by=paired_by,
        paired_at=now,
        last_seen_at=now,
    )
    return DeviceToken(record=record, secret=secret)


__all__ = [
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "DEVICE_PREFIX",
    "PREFIX_LENGTH",
    "TOKEN_BYTES",
    "DeviceToken",
    "format_code",
    "hash_token",
    "mint_device",
    "mint_pairing_code",
    "normalise_code",
    "parse_token",
    "token_prefix",
    "verify_token",
]
