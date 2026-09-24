"""P1-3 — document parsing runs somewhere it can be killed.

Document parsers are the largest hostile-input surface this system has. A PDF, a
DOCX and a PPTX are all *programs* in a weak sense: nested object graphs with
back-references, compressed streams whose expansion the header can lie about,
and structures that make a naive traversal quadratic. `pypdf`, `python-docx` and
`python-pptx` are not written to be adversary-resistant, and reading their source
to convince yourself otherwise is not a security strategy.

The existing defences — a byte cap, magic-byte sniffing, a zip expansion-ratio
check — all happen *before* parsing. They stop a file that is obviously wrong.
They do nothing about a well-formed file that takes four hours or twelve
gigabytes, which is the interesting attack: one upload, one worker gone, and the
queue backing up behind it.

## Why a subprocess, and not a timeout

There is no way to interrupt a CPU-bound C extension from Python. `signal.alarm`
does not fire until the interpreter regains control; a watchdog thread can only
*observe* the overrun, and abandoning the thread leaks it along with whatever
memory it has allocated. A "timeout" implemented that way is a log line, not a
control.

A child process can be killed. `resource.setrlimit` in the child makes the
memory ceiling the kernel's problem rather than ours, `RLIMIT_CPU` covers a busy
loop that ignores wall-clock, and `SIGKILL` covers everything else. This is the
only implementation of "bounded parsing" that is true when the parser is
hostile, so it is the one that is here.

## What this is NOT

**It is not a security sandbox.** The child runs as the same user, with the same
filesystem and the same network. A parser with a genuine remote-code-execution
bug is not contained by this — it is contained by the container's non-root user,
by seccomp, and by the network policy, none of which this module provides.

What this *does* contain is resource exhaustion: time, memory, and output size.
That is the realistic threat from a document, and it is worth stating the limit
plainly rather than letting the word "sandbox" imply more than it delivers.

`docs/SECURITY.md` records the residual risk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from vtv.contracts.errors import ErrorCode, ValidationFailed, VTVError
from vtv.contracts.source import IngestionResult

#: Wall-clock ceiling for one document. Generous for a real 200-page PDF and
#: nowhere near enough for a decompression bomb that survived the ratio check.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Address-space ceiling. Chosen so a legitimate large PDF fits comfortably and
#: an allocation spiral hits `MemoryError` in the child rather than the OOM
#: killer in the pod — the difference between one refused upload and a worker
#: restart that loses every job in flight.
DEFAULT_MEMORY_BYTES = 1024 * 1024 * 1024

#: A parser that emits more structure than this has found a pathological input;
#: an unbounded result would move the exhaustion from the child to the parent.
DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class SandboxLimits:
    """What one parse may consume before it is stopped."""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def cpu_seconds(self) -> int:
        """A CPU ceiling slightly under the wall clock.

        Below the wall-clock limit on purpose: a process that is genuinely
        spinning should be stopped by `RLIMIT_CPU` — which the kernel enforces
        even if the parent is descheduled — rather than by the parent's timer.
        """
        return max(1, int(self.timeout_seconds) - 1)


def parse_in_sandbox(
    data: bytes,
    *,
    organisation_id: str,
    project_id: str,
    filename: str | None = None,
    origin: str | None = None,
    limits: SandboxLimits | None = None,
) -> IngestionResult:
    """Parse a document in a child process that cannot outlive its limits.

    Raises `ValidationFailed` when the child is killed or fails. That is the
    correct category: from the system's point of view a document that cannot be
    parsed within a sane budget is a bad document, not an internal fault, and
    it must not be retried — a redelivery would consume the budget again.
    """
    limits = limits or SandboxLimits()
    header = json.dumps(
        {
            "organisation_id": organisation_id,
            "project_id": project_id,
            "filename": filename,
            "origin": origin,
            "memory_bytes": limits.memory_bytes,
            "cpu_seconds": limits.cpu_seconds(),
            "max_output_bytes": limits.max_output_bytes,
        }
    ).encode("utf-8")

    # Length-prefixed header, then the raw bytes. Not JSON-with-base64: a 50MB
    # document would become 67MB of base64 and be parsed twice.
    payload = len(header).to_bytes(8, "big") + header + data

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "vtv.ingest_sandbox"],
            input=payload,
            capture_output=True,
            timeout=limits.timeout_seconds,
            env=_child_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationFailed(
            f"the document did not parse within {limits.timeout_seconds:.0f}s",
            code=ErrorCode.SCHEMA_INVALID,
        ) from error
    except (OSError, ValueError) as error:  # pragma: no cover - platform
        raise VTVError(
            "could not start the parsing sandbox",
            code=ErrorCode.INTERNAL_ERROR,
        ) from error

    if completed.returncode != 0:
        raise ValidationFailed(
            _reason_for(completed.returncode, completed.stderr),
            code=ErrorCode.SCHEMA_INVALID,
        )

    if len(completed.stdout) > limits.max_output_bytes:
        raise ValidationFailed(
            "the document produced an implausible amount of structure",
            code=ErrorCode.SCHEMA_INVALID,
        )

    try:
        return IngestionResult.model_validate_json(completed.stdout)
    except ValueError as error:
        raise ValidationFailed(
            "the document could not be read", code=ErrorCode.SCHEMA_INVALID
        ) from error


def _reason_for(returncode: int, stderr: bytes) -> str:
    """A message for the user that names the limit, never the internals.

    Parser stack traces routinely contain file paths and library versions, and
    a document is attacker-controlled, so `stderr` is used only to classify —
    it is never forwarded.
    """
    if returncode < 0:
        signal_number = -returncode
        if signal_number == 9:
            return "the document exceeded the memory allowed for parsing"
        if signal_number == 24:  # SIGXCPU
            return "the document exceeded the time allowed for parsing"
        return "parsing the document was stopped by the system"
    if b"MemoryError" in stderr:
        return "the document exceeded the memory allowed for parsing"
    return "the document could not be read"


def _child_environment() -> dict[str, str]:
    """A minimal environment for the child.

    Credentials are removed: a parser has no business reaching a provider, and
    a library that decides to "helpfully" fetch a remote resource should not
    find an API key waiting for it. `PYTHONPATH` is preserved because the child
    has to be able to import this package.
    """
    keep = {"PATH", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL", "TMPDIR", "HOME"}
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in keep or (name.startswith("PYTHON") and "VTV" not in name)
    }
    # Hardening that costs nothing: no user site-packages, no .pyc writing, and
    # no random hash seed differences between parent and child.
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


# ---------------------------------------------------------------------------
# Child entrypoint
# ---------------------------------------------------------------------------

def _apply_limits(memory_bytes: int, cpu_seconds: int) -> None:
    """Ask the kernel to enforce what Python cannot.

    Best-effort by design: a platform without `resource` still gets the
    wall-clock kill from the parent, which is the limit that matters most.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - not POSIX
        return

    for name, limit in (
        ("RLIMIT_AS", memory_bytes),
        ("RLIMIT_CPU", cpu_seconds),
        # No core dumps: a crash on hostile input should not write the
        # document's contents to disk outside the tenant's namespace.
        ("RLIMIT_CORE", 0),
    ):
        constant = getattr(resource, name, None)
        if constant is None:  # pragma: no cover - platform
            continue
        try:
            _soft, hard = resource.getrlimit(constant)
            ceiling = limit if hard in (resource.RLIM_INFINITY,) else min(limit, hard)
            resource.setrlimit(constant, (ceiling, hard))
        except (ValueError, OSError):  # pragma: no cover - platform
            continue


def main() -> int:  # pragma: no cover - exercised through subprocess
    raw = sys.stdin.buffer.read()
    if len(raw) < 8:
        return 2
    header_length = int.from_bytes(raw[:8], "big")
    header: dict[str, Any] = json.loads(raw[8 : 8 + header_length])
    data = raw[8 + header_length :]

    _apply_limits(
        int(header.get("memory_bytes", DEFAULT_MEMORY_BYTES)),
        int(header.get("cpu_seconds", int(DEFAULT_TIMEOUT_SECONDS))),
    )

    from vtv.adapters.ingest.documents import ParserRegistry

    result = ParserRegistry().parse(
        data,
        organisation_id=str(header["organisation_id"]),
        project_id=str(header["project_id"]),
        filename=header.get("filename"),
        origin=header.get("origin"),
    )
    sys.stdout.buffer.write(result.model_dump_json().encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MEMORY_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "SandboxLimits",
    "parse_in_sandbox",
]
