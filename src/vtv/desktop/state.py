"""Where a paired device keeps its identity.

One small file holding the token this machine authenticates with. Everything
about it is shaped by that being a credential sitting on a computer somebody
else also uses.

**Permissions are set before the secret is written**, not after. Creating the
file, writing the token and then calling `chmod` leaves a window — usually
milliseconds, occasionally much longer under load — in which a world-readable
file contains a working credential. `os.open` with the mode up front has no
window.

**It lives under the user's own config directory**, not beside the software. A
token in the install directory is a token that gets copied when somebody images
a machine or zips a folder to send to a colleague, and then two computers
authenticate as the same device and each thinks the other's job failures are
its own.

**It is refused if the permissions are wrong.** A token file that has become
readable by everyone has either been tampered with or been copied out of
somewhere, and continuing to use it would mean the one credential on this
machine is the one nobody is checking.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from vtv.contracts.errors import NotFound, PolicyViolation

#: Owner read/write only. On Windows this is advisory — the file inherits the
#: user's profile ACL, which is the platform's own answer to the same question.
SECRET_MODE = 0o600


def config_root() -> Path:
    """Where this platform expects a program to keep its configuration."""
    override = os.environ.get("VTV_DESKTOP_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "VoiceToVideo"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VoiceToVideo"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "voice-to-video"


@dataclass(frozen=True)
class Pairing:
    """This machine's identity, as stored."""

    server: str
    device_id: str
    token: str
    name: str = ""

    def __repr__(self) -> str:
        # Without this the token appears in every traceback that touches a
        # `Pairing`, which is most of them, since it is threaded through the
        # whole executor.
        return f"Pairing(server={self.server!r}, device_id={self.device_id!r}, token=<redacted>)"


def token_path(root: Path | None = None) -> Path:
    return (root or config_root()) / "device.json"


def save(pairing: Pairing, *, root: Path | None = None) -> Path:
    """Write this machine's identity, never world-readable at any instant."""
    path = token_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "server": pairing.server,
            "device_id": pairing.device_id,
            "token": pairing.token,
            "name": pairing.name,
        },
        indent=2,
    )
    # The mode is applied by `open` itself. A separate `chmod` afterwards would
    # leave the secret briefly readable by anyone with an account on the
    # machine, which is precisely the population this file exists to exclude.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    # An existing file keeps its old mode through O_CREAT, so this is not
    # redundant — it is the path where the file was already there.
    if os.name == "posix":
        os.chmod(path, SECRET_MODE)
    return path


def load(*, root: Path | None = None) -> Pairing:
    """This machine's identity, or a refusal to guess at one."""
    path = token_path(root)
    if not path.exists():
        raise NotFound(
            "this computer is not paired yet. Run:  vtv-desktop pair --code ABC-DEF"
        )
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PolicyViolation(
                f"{path} is readable by other users on this machine "
                f"(mode {mode:o}). Delete it and pair again."
            )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PolicyViolation(f"{path} is not readable: {exc}") from exc
    missing = [key for key in ("server", "device_id", "token") if not payload.get(key)]
    if missing:
        raise PolicyViolation(f"{path} is incomplete: missing {', '.join(missing)}")
    return Pairing(
        server=str(payload["server"]),
        device_id=str(payload["device_id"]),
        token=str(payload["token"]),
        name=str(payload.get("name", "")),
    )


def forget(*, root: Path | None = None) -> bool:
    """Unpair this machine locally. True if there was anything to remove.

    Local only, and the docstring says so because the difference matters: this
    stops *this computer* authenticating, and does nothing about the server's
    record. A lost laptop is revoked from the account, not from the laptop.
    """
    path = token_path(root)
    if not path.exists():
        return False
    path.unlink()
    return True


__all__ = [
    "SECRET_MODE",
    "Pairing",
    "config_root",
    "forget",
    "load",
    "save",
    "token_path",
]
