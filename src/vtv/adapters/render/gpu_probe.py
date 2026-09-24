"""Whether this machine can actually composite frames on its graphics card.

## The rule this exists to enforce

**Hardware being present is not a capability.** A card can be installed and
driverless; installed with a driver too old for the shaders we compile;
installed inside a container with no render node; installed and already holding
all its memory for something else. Every one of those reports as "an NVIDIA
RTX 4070 is present", and every one of them fails at the first frame of a
sixty-minute render.

So this probe does not ask what hardware exists. It asks the graphics stack to
**initialise, compile our pipelines, and draw one calibration frame** — and it
compares that frame against the CPU painter's output for the same display list.
Only then does it report the card as usable.

That last comparison is the part that matters and the part most probes skip. A
driver that initialises and produces a *wrong* picture is worse than one that
refuses: the render succeeds, the file plays, and the captions are two pixels
off in a way nobody notices until a customer does.

## Why it is cached, and what invalidates it

Initialising a graphics device and compiling shaders costs a noticeable
fraction of a second, and doing it before every render would be that fraction
spent re-learning something that changes when a driver changes and at no other
time. The cache is keyed on a fingerprint that includes the reported adapter
and driver strings, so a driver update invalidates it and nothing else needs to.

## What this reports in an environment with no GPU

`available=False`, with a reason. That is the state in the cloud container this
was written in — no render node, no graphics library obtainable — and it is
also the state on a laptop with the wrong driver. The reason distinguishes them,
because "install the desktop engine" and "update your driver" are different
instructions.
"""

from __future__ import annotations

import hashlib
import os
import platform
from dataclasses import dataclass, field
from functools import lru_cache

#: Environment variable that turns the GPU path off regardless of hardware.
#:
#: For a support call, and for a machine where the card is needed by something
#: else. A switch that only ever *disables* is safe to expose; one that forces
#: a capability on is how a user ends up with a render that cannot start.
DISABLE = "VTV_DISABLE_GPU"

#: How much a calibration frame may differ from the CPU reference, per channel.
#:
#: Not zero, and deliberately so. Rasterisation on a graphics card is allowed to
#: differ from PIL in the last bit or two: filtering is implementation-defined,
#: and an edge pixel that lands on 127 rather than 128 is not a defect.
#:
#: Two is tight enough that a real mistake cannot hide in it — a plate in the
#: wrong place, a missing overlay, a transition running backwards all produce
#: differences in the hundreds — and loose enough that a correct implementation
#: on a different vendor's card is not rejected for being a different correct
#: implementation.
TOLERANCE = 2

#: How much of the frame may exceed the tolerance at all.
#:
#: Zero. A handful of pixels being wrong is how a subtly broken painter passes:
#: the mean stays low while a caption sits a pixel high on every frame of a
#: four-hour video. Every pixel is within tolerance, or the card is not used.
MAX_OUTLIERS = 0


@dataclass(frozen=True)
class GpuReport:
    """What was found, and — when nothing usable was — why not."""

    available: bool
    #: Why not. Empty when available. Written for a person: "no graphics
    #: library is installed" and "your driver is too old" lead to different
    #: actions and must not both read as "GPU unavailable".
    reason: str = ""
    vendor: str = ""
    device: str = ""
    driver: str = ""
    #: Graphics API actually used — "opengl", "vulkan", "metal", "d3d12".
    api: str = ""
    #: Usable device memory in bytes, when the API will say. `None` is
    #: honest; zero would be a claim.
    memory_bytes: int | None = None
    #: Largest square texture the device accepts. Bounds what can be composed
    #: without tiling, and 4K needs 3840.
    max_texture: int | None = None
    #: Worst per-channel difference from the CPU reference on the calibration
    #: frame. `None` when no frame was drawn.
    calibration_difference: int | None = None
    #: Hardware video encoders, reported separately on purpose: encoding is
    #: about 23% of render time and a card that can encode but not composite
    #: is not a compositing backend.
    hardware_encoders: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "available": self.available,
            "reason": self.reason,
            "vendor": self.vendor,
            "device": self.device,
            "driver": self.driver,
            "api": self.api,
            "memory_bytes": self.memory_bytes,
            "max_texture": self.max_texture,
            "calibration_difference": self.calibration_difference,
            "hardware_encoders": list(self.hardware_encoders),
        }

    @property
    def fingerprint(self) -> str:
        """Identity of this graphics configuration, for the profile cache."""
        raw = "|".join(
            (platform.platform(), self.vendor, self.device, self.driver, self.api)
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Attempt:
    """One backend's answer, so `probe` can report the most useful failure."""

    api: str
    reason: str = ""
    report: GpuReport | None = None


def _unavailable(reason: str) -> GpuReport:
    return GpuReport(available=False, reason=reason)


@lru_cache(maxsize=1)
def probe() -> GpuReport:
    """Ask the graphics stack to prove itself. Cached for the process.

    Never raises. A probe that throws is a probe that takes down the render it
    was supposed to protect, and every failure mode here — no library, no
    device, no driver, a shader that will not compile — is an ordinary answer
    rather than an error.
    """
    if os.environ.get(DISABLE, "").strip().lower() in {"1", "true", "yes", "on"}:
        return _unavailable(f"disabled by {DISABLE}")

    try:
        from vtv.adapters.render.gpu_painter import inspect_device
    except Exception as exc:  # pragma: no cover - import guard
        return _unavailable(f"the GPU painter could not be loaded: {exc}")

    try:
        report = inspect_device()
    except Exception as exc:
        # Driver crashes, missing shared libraries and context-creation
        # failures all land here, and all of them mean the same thing to a
        # caller: use the processor.
        return _unavailable(
            f"the graphics device could not be initialised: "
            f"{type(exc).__name__}: {exc}"
        )
    return report


def forget() -> None:
    """Drop the cached answer. For tests, and after a driver change."""
    probe.cache_clear()


def describe() -> str:
    """One line for a log or a support message."""
    found = probe()
    if not found.available:
        return f"GPU compositing unavailable: {found.reason}"
    return (
        f"GPU compositing available: {found.vendor} {found.device} "
        f"via {found.api} (driver {found.driver or 'unknown'})"
    )


__all__ = [
    "DISABLE",
    "MAX_OUTLIERS",
    "TOLERANCE",
    "GpuReport",
    "describe",
    "forget",
    "probe",
]
