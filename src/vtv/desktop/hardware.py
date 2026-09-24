"""What this machine actually has, measured on this machine.

## Why every field is observed

Because the alternative is a device that tells the server it has a graphics
card, is sent graphics work, and fails it — and the customer experiences that as
the product being broken rather than as their driver being broken.

So `gpu_verified` is not "a card was found". It is "this card drew the
twenty-three calibration scenes and matched the reference painter, on this
machine, just now" — the same bar `gpu_probe` applies before the renderer will
use a card in-process. There is one definition of "the GPU works here" and this
module does not get a second opinion.

## Why it is re-read on every poll and not just at pairing

Hardware changes underneath a paired device. A driver is updated, a card is
replaced, an external GPU is unplugged, a laptop is undocked. A machine whose
card stopped verifying must stop being offered graphics work on its next poll,
not at its next pairing — which might be never, because pairing happens once.

The probe itself is cached for the process, so re-reading it costs nothing; what
this does re-read cheaply is whether the answer is still the same one.

## What is deliberately not here

Anything that needs a vendor SDK. `nvidia-smi`, `wmic`, ROCm, Metal queries —
each would give a richer answer on exactly one platform and nothing on the
others, and the field they would fill in most usefully (video memory) is already
reported by the graphics API when it knows and left `None` when it does not.
`None` is honest; a number scraped from a tool that may not be installed is a
number that is sometimes wrong.
"""

from __future__ import annotations

import os
import platform

from vtv.contracts.devices import DeviceHardware


def _memory_mb() -> int:
    """Physical memory, or 0 if this platform will not say.

    Zero rather than a guess. The server uses this to decide whether a machine
    can hold a 4K frame buffer plus an encoder, and a fabricated number there is
    worse than an absent one — an absent one means "do not assume", and a wrong
    one means "assume wrongly".
    """
    # POSIX, including Linux and macOS.
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and size > 0:
            return int(pages * size / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass

    # Windows, through the standard library rather than a vendor tool.
    try:
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.ullTotalPhys / (1024 * 1024))
    except Exception:
        pass
    return 0


def _cores() -> int:
    """Cores this process may actually use.

    `os.process_cpu_count` and not `os.cpu_count`: inside a container with a
    CPU quota, or under an affinity mask, the second number is the machine's and
    the first is ours. Reporting the machine's would have the server size work
    for cores this process is not allowed to run on.
    """
    counted = getattr(os, "process_cpu_count", None)
    if counted is not None:
        found = counted()
        if found:
            return int(found)
    return int(os.cpu_count() or 1)


def detect(*, probe_gpu: bool = True) -> DeviceHardware:
    """Everything the server needs to decide what to send this machine.

    `probe_gpu=False` exists for the pairing path on a machine the user has not
    asked to render on yet, and for tests: probing builds a graphics context and
    draws twenty-three frames, which is the right cost to pay once and the wrong
    cost to pay in a unit test that only cares about core counts.
    """
    hardware = DeviceHardware(
        platform=platform.platform()[:200],
        cpu_cores=_cores(),
        memory_mb=_memory_mb(),
    )
    if not probe_gpu:
        return hardware

    from vtv.adapters.render.gpu_probe import probe

    # Never raises, by contract — see `gpu_probe.probe`. A machine whose
    # graphics stack explodes during detection must still pair and still render
    # on its processor, because "no GPU" is a completely ordinary answer.
    found = probe()
    return hardware.model_copy(
        update={
            "gpu_name": f"{found.vendor} {found.device}".strip()[:200],
            "gpu_api": found.api[:40],
            "gpu_verified": bool(found.available),
            # The reason survives even when the card works, empty in that case.
            # A machine that reports "available" with a stale refusal attached
            # would be a machine somebody debugs for an hour.
            "gpu_reason": "" if found.available else found.reason[:400],
        }
    )


def describe(hardware: DeviceHardware) -> str:
    """Several lines, for a person setting a machine up.

    Longer than `DeviceHardware.summary` on purpose: that one goes in a list of
    devices, this one is what somebody reads when they are asking why their card
    is not being used.
    """
    lines = [
        f"machine   : {hardware.platform}",
        f"processor : {hardware.cpu_cores} cores",
        f"memory    : {hardware.memory_mb} MB" if hardware.memory_mb else
        "memory    : not reported by this platform",
    ]
    if hardware.gpu_verified:
        lines.append(f"graphics  : {hardware.gpu_name} ({hardware.gpu_api}) — verified")
    elif hardware.gpu_name:
        lines.append(f"graphics  : {hardware.gpu_name} — NOT usable: {hardware.gpu_reason}")
    else:
        lines.append(f"graphics  : none usable — {hardware.gpu_reason or 'no device found'}")
    lines.append(
        "will run  : " + ", ".join(target.label for target in hardware.targets)
    )
    return "\n".join(lines)


__all__ = ["describe", "detect"]
