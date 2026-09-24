"""Path and storage-key safety.

Two distinct attacks, two distinct defences, deliberately not shared code
because the correct answer differs.

**Filesystem paths.** `../../etc/passwd` inside a filename. The defence is to
resolve the candidate and confirm it is still inside the root, *after*
normalisation, because `a/../../b` only becomes visibly dangerous once resolved.
Checking the raw string for `..` catches the naive attempt and misses URL
encoding, Unicode normalisation and symlinks.

**Storage keys.** An object store key is not a path — it is an opaque string
with a naming convention — but a key that starts with `/` or contains `..`
breaks tooling, and a key derived from user input can collide across tenants.
So keys are validated against a strict shape rather than resolved.

Both refuse rather than sanitise. Silently rewriting `../../secret` into
`secret` produces a request that succeeds against the wrong object, which is
worse than an error.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from pathlib import Path

from vtv.contracts.errors import PolicyViolation, ValidationFailed

#: Storage keys are ASCII, slash-separated, and boring on purpose.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,511}$")

#: Filenames that mean something to an operating system rather than to a user.
_RESERVED_NAMES = frozenset(
    {
        "con", "prn", "aux", "nul",
        *(f"com{n}" for n in range(1, 10)),
        *(f"lpt{n}" for n in range(1, 10)),
    }
)

MAX_FILENAME_LENGTH = 200


def safe_join(root: Path, *parts: str) -> Path:
    """Join user-influenced parts onto ``root``, refusing anything that escapes.

    The check is on the *resolved* path, so `a/../../b`, an absolute component
    and a symlink pointing outside the root are all caught by the same test.
    """
    if not parts:
        raise ValidationFailed("nothing to join")

    base = root.resolve()
    candidate = base
    for part in parts:
        if not part:
            raise ValidationFailed("empty path component")
        # An absolute component would silently discard everything before it,
        # which is how `Path("/safe") / "/etc/passwd"` becomes `/etc/passwd`.
        if Path(part).is_absolute() or part.startswith(("/", "\\")):
            raise PolicyViolation(f"absolute path component refused: {part!r}")
        if "\x00" in part:
            raise PolicyViolation("null byte in path")
        candidate = candidate / part

    resolved = candidate.resolve()
    # `is_relative_to` compares resolved paths, which is what makes this
    # symlink-safe: a link inside the root pointing out resolves out.
    if resolved != base and not resolved.is_relative_to(base):
        raise PolicyViolation("path escapes its root")
    return resolved


def safe_storage_key(key: str) -> str:
    """Validate an object-storage key. Returns it unchanged or raises.

    Not a sanitiser. A key that has to be rewritten to be safe is a key the
    caller constructed wrongly, and rewriting it hides the bug while pointing
    the request at a different object.
    """
    if not key or not _SAFE_KEY.match(key):
        raise PolicyViolation("unsafe storage key")
    # Normalisation must be a no-op. If it is not, the key contained `..`, a
    # doubled slash or a trailing dot segment, any of which resolves to a
    # different object than it appears to.
    if posixpath.normpath(key) != key:
        raise PolicyViolation("storage key is not in normal form")
    if key.endswith("/"):
        raise PolicyViolation("storage key must not name a directory")
    return key


def tenant_key(organisation_id: str, *parts: str) -> str:
    """Build a storage key that is unambiguously inside one tenant's namespace.

    Every object the platform writes goes through this. Tenant isolation in
    storage is a prefix convention, and a convention nobody can bypass is one
    that lives in a function rather than in a style guide.
    """
    safe_storage_key(organisation_id)
    key = "/".join(["orgs", organisation_id, *(_component(part) for part in parts)])
    return safe_storage_key(key)


def _component(part: str) -> str:
    if not part or "/" in part or part in {".", ".."}:
        raise PolicyViolation(f"unsafe key component: {part!r}")
    return part


def safe_filename(name: str, *, fallback: str = "upload") -> str:
    """A filename safe to write to disk and to put in a Content-Disposition.

    This one *does* rewrite, because a filename is a label rather than a
    locator: the bytes are addressed by storage key, and the name is only what
    the user sees when they download. Refusing an upload because its name has an
    emoji would be user-hostile for no security gain.
    """
    cleaned = unicodedata.normalize("NFKD", name).strip().replace("\x00", "")
    # Take the last component only: the browser sends what the user's own OS
    # produced, and on some platforms that is a full path.
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._ \-]", "_", cleaned).strip(" .")
    cleaned = re.sub(r"_{3,}", "__", cleaned)[:MAX_FILENAME_LENGTH]

    stem = cleaned.rsplit(".", 1)[0].lower() if cleaned else ""
    if not cleaned or stem in _RESERVED_NAMES:
        return fallback
    return cleaned


__all__ = [
    "MAX_FILENAME_LENGTH",
    "safe_filename",
    "safe_join",
    "safe_storage_key",
    "tenant_key",
]
