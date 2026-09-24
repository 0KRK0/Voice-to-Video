"""Password hashing.

`Directory.verify_password` used to raise `NotImplementedError`, and the audit
recorded the consequence plainly: no human could log in, because the only
working credential was an API key and no SSO integration existed. Refusing was
the right *behaviour* — a hand-rolled hash would have been worse — but shipping
without any usable option is not a product.

**What this uses and why.** PBKDF2-HMAC-SHA256 from `hashlib`, at 600,000
iterations. That is:

* in the standard library, so it works in every environment this runs in,
  including ones with no package index — which is why Argon2 was unavailable in
  the first place;
* the algorithm OWASP lists as acceptable when Argon2id and scrypt are not
  available, at the iteration count they recommend for SHA-256;
* FIPS-approved, which several enterprise buyers will ask about.

**What it is not.** Argon2id is better. It is memory-hard, so it resists GPU and
ASIC attack in a way PBKDF2 does not. When `argon2-cffi` is installed this
module uses it automatically and records `argon2id` in the hash prefix; the
verifier reads the prefix, so a deployment can install the library later and
existing PBKDF2 hashes keep working while new ones get the stronger algorithm.
That upgrade path is the reason the algorithm is stored *in* the hash rather
than in configuration.

**Format.** `algorithm$iterations$salt$digest`, all base64. Self-describing, so
verification never has to guess and the iteration count can rise over time
without invalidating anything already stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from vtv.contracts.errors import ValidationFailed

#: OWASP's recommendation for PBKDF2-HMAC-SHA256. Deliberately a constant that
#: can be raised: `needs_rehash` reports when a stored hash is below it, so
#: raising this number upgrades users on their next successful login.
PBKDF2_ITERATIONS = 600_000

SALT_BYTES = 16
DIGEST_BYTES = 32

#: Refuse to hash something absurd. A gigabyte "password" is an attack on the
#: KDF's cost, not a user with a long passphrase.
MAX_PASSWORD_LENGTH = 1024

#: Below this, a password is not protecting anything. Enforced at registration
#: rather than only advised, because advice is not a control.
MIN_PASSWORD_LENGTH = 12


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def argon2_available() -> bool:
    """Whether the stronger algorithm can be used in this deployment."""
    try:
        import argon2  # noqa: F401
    except ImportError:
        return False
    return True


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Derive a storable hash. Uses Argon2id when it is installed."""
    _guard(password)

    if argon2_available():  # pragma: no cover - depends on the environment
        from argon2 import PasswordHasher

        # The library's own encoding is already self-describing and carries its
        # parameters, so it is stored verbatim behind our prefix.
        return f"argon2id${PasswordHasher().hash(password)}"

    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=DIGEST_BYTES
    )
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash, in constant time.

    Returns `False` for a malformed hash rather than raising: a corrupt row
    must not be distinguishable from a wrong password, and it must not take the
    login endpoint down.
    """
    if not password or not stored:
        return False
    if len(password) > MAX_PASSWORD_LENGTH:
        return False

    algorithm, _, rest = stored.partition("$")

    if algorithm == "argon2id":  # pragma: no cover - depends on the environment
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import VerificationError

            return bool(PasswordHasher().verify(rest, password))
        except (ImportError, VerificationError, ValueError):
            return False

    if algorithm != "pbkdf2_sha256":
        return False

    try:
        raw_iterations, salt_value, digest_value = rest.split("$", 2)
        iterations = int(raw_iterations)
        salt = _unb64(salt_value)
        expected = _unb64(digest_value)
    except (ValueError, TypeError):
        return False
    if iterations < 1 or not salt or not expected:
        return False

    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str, *, iterations: int = PBKDF2_ITERATIONS) -> bool:
    """Whether this hash is weaker than what we would produce today.

    Checked after a *successful* login, which is the only moment the plaintext
    is available to re-derive from. That is how an iteration-count increase, or
    installing Argon2, upgrades an existing user without asking them to do
    anything.
    """
    algorithm, _, rest = stored.partition("$")
    if algorithm == "argon2id":
        return False
    if algorithm != "pbkdf2_sha256":
        return True
    if argon2_available():
        return True
    try:
        return int(rest.split("$", 1)[0]) < iterations
    except (ValueError, IndexError):
        return True


def _guard(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(
            f"a password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationFailed("that password is implausibly long")


@dataclass(frozen=True)
class PasswordPolicy:
    """What this deployment demands of a password.

    Length only, deliberately. Composition rules ("one uppercase, one symbol")
    reduce entropy in practice by pushing everyone to `Password1!`, and NIST
    stopped recommending them years ago. Length plus a breach list is the
    modern answer; the breach list needs a data file this environment cannot
    fetch, so `forbidden` is the seam where one is supplied.
    """

    minimum_length: int = MIN_PASSWORD_LENGTH
    #: Known-breached or trivially guessable passwords. Supplied by the
    #: deployment; empty here rather than a token list that implies coverage
    #: it does not have.
    forbidden: frozenset[str] = frozenset()

    def check(self, password: str) -> None:
        if len(password) < self.minimum_length:
            raise ValidationFailed(
                f"a password must be at least {self.minimum_length} characters"
            )
        if password.lower() in self.forbidden:
            raise ValidationFailed(
                "that password appears in a list of known-compromised passwords"
            )


__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "PBKDF2_ITERATIONS",
    "PasswordPolicy",
    "argon2_available",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
