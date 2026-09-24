"""How fast is this machine, actually.

## Why a measurement rather than a lookup table

The tempting way to choose a backend is by name: an RTX 4070 is present,
therefore use it. That is wrong often enough to matter. A card can be present
and driverless; present and already saturated by whatever else the user has
open; present and genuinely slower than eight CPU cores on a typography-heavy
video, where almost nothing is being sampled and the work is text layout rather
than pixel maths. Hardware names describe what was bought, not what is
available.

So: compose a small number of real frames on each backend and time it. The
comparison is between numbers from the same machine, the same minute, the same
kind of content — which is the only comparison that means anything.

## Why it is cached, and what invalidates it

A calibration is a few hundred milliseconds. Doing it before every render would
be a few hundred milliseconds of every render, spent re-learning something that
changes when hardware changes and at no other time. So it is cached against a
fingerprint of the machine, and the fingerprint is deliberately coarse: core
count, frame size, and the set of backends. A driver update will not invalidate
it; nor will it need to, because the number it produces is used to *order* two
backends rather than to promise anyone a duration.

## What it deliberately does not do

Promise a render time. "Estimated: 5 minutes" from a 300-millisecond sample is
a number with error bars wider than the estimate, on a video whose content it
has not seen. What this supports is the honest question — *is this backend
materially faster than that one* — and the answer only has to be right about the
ordering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

#: Frames to compose when timing a backend.
#:
#: Enough to get past the first-frame costs — font loading, the vignette's
#: 154-pixel Gaussian, an import — and few enough that the whole calibration is
#: shorter than the render it informs.
SAMPLE_FRAMES = 24

#: How much faster one backend must be before it is worth preferring.
#:
#: Ten percent. Below that the difference is inside the noise of a
#: quarter-second sample, and switching hardware on that evidence buys nothing
#: while adding a class of failure — a driver, a memory ceiling, a fallback —
#: that the slower path does not have. A tie goes to the incumbent.
MATERIAL_GAIN = 1.10


@dataclass(frozen=True)
class Measurement:
    """One backend's throughput, on this machine, on real frames."""

    target: str
    frames_per_second: float
    frames: int

    def as_json(self) -> dict[str, object]:
        return {
            "target": self.target,
            "frames_per_second": round(self.frames_per_second, 1),
            "frames": self.frames,
        }


#: Measurements already taken, keyed by machine fingerprint.
_CACHE: dict[str, list[Measurement]] = {}


def fingerprint(*, cores: int, width: int, height: int, targets: tuple[str, ...]) -> str:
    return f"{cores}:{width}x{height}:{'|'.join(sorted(targets))}"


def compose_rate(context: object, *, frames: int = SAMPLE_FRAMES) -> float:
    """Frames a second this context composes, measured now.

    Takes a `RenderContext` rather than a backend, because composition is the
    77% and it is what differs between a CPU and a GPU implementation. Timing a
    whole segment would fold in the encoder, which is the part that does not
    change.
    """
    compose = context.compose  # type: ignore[attr-defined]
    fps = getattr(context, "fps", 30) or 30
    # One frame first, untimed. The first call pays for font loading, the
    # vignette mask and a Gaussian blur the size of the frame — real costs, but
    # paid once per render, and counting them here would report a machine as
    # several times slower than it is.
    compose(0.0)
    started = time.perf_counter()
    for index in range(frames):
        compose(index / fps)
    elapsed = time.perf_counter() - started
    return frames / elapsed if elapsed > 0 else float("inf")


def better(measurements: list[Measurement], incumbent: str) -> str:
    """Which target to prefer, given what was measured.

    Returns `incumbent` unless something else is materially faster. Written
    this way round on purpose: the default is to keep doing what works, and the
    burden is on the alternative to earn the switch.
    """
    if not measurements:
        return incumbent
    ranked = sorted(measurements, key=lambda m: m.frames_per_second, reverse=True)
    best = ranked[0]
    current = next(
        (m for m in measurements if m.target == incumbent), None
    )
    if current is None or best.target == incumbent:
        return best.target if current is None else incumbent
    if best.frames_per_second >= current.frames_per_second * MATERIAL_GAIN:
        return best.target
    return incumbent


def remember(key: str, measurements: list[Measurement]) -> None:
    _CACHE[key] = list(measurements)


def recall(key: str) -> list[Measurement] | None:
    return _CACHE.get(key)


def forget() -> None:
    """Drop every cached measurement. For tests, and for a hardware change."""
    _CACHE.clear()


def report(scratch: Path | None = None) -> dict[str, object]:
    """Everything measured so far, for `/health` and for support.

    Empty is the normal state on a fresh process and says so rather than
    inventing a number: an unmeasured backend and a slow one are different
    facts, and reporting the first as the second would have the selector
    refusing hardware it has simply never tried.
    """
    del scratch
    return {
        "measured": [
            measurement.as_json()
            for values in _CACHE.values()
            for measurement in values
        ],
        "sample_frames": SAMPLE_FRAMES,
        "material_gain": MATERIAL_GAIN,
    }


__all__ = [
    "MATERIAL_GAIN",
    "SAMPLE_FRAMES",
    "Measurement",
    "better",
    "compose_rate",
    "fingerprint",
    "forget",
    "recall",
    "remember",
    "report",
]
