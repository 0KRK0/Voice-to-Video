"""What this machine can actually do, and what it would be worth.

## Why this reports rather than switches

The obvious product move is a **Fast — GPU** button. It would be a lie today,
and the measurements are the reason.

Rendering divides into two jobs, and they were profiled on a real 1080p
timeline:

| | share of render time |
|---|---|
| composing frames (PIL, Python) | **77%** |
| encoding them (x264) | 23% |

Hardware encoders — NVENC, Quick Sync, VAAPI, VideoToolbox — replace the second
row. So the ceiling on a GPU encode switch is Amdahl's: even at zero cost it
takes a render to 77% of its current time, a **1.3× speedup**, and the button
would say "Fast" next to a number that barely moved.

Worse, it fights the thing that does work. Segments are drawn in a process
pool, one encoder each; consumer NVIDIA drivers cap concurrent NVENC sessions
(historically three to five), so eight parallel segments would either fail or
serialise on the GPU. The fast path and the hardware path are in tension, and
the fast path is the one carrying 77% of the work.

The composition side is where a GPU would genuinely pay — and it is not a
switch, it is a different renderer. Profiling showed most of that 77% was
redundant full-frame buffer work rather than inherently parallel pixel maths;
removing it took composition from 12 to 26 frames a second on unchanged
hardware, which is more than a hardware encoder could ever have given. What is
left is a real GPU workload, but reaching it means a compositor written against
a GPU API, not a flag passed to ffmpeg.

## So this module tells the truth and offers nothing

It reports what the machine has: cores, and which hardware encoders this ffmpeg
build can actually open. Nothing here decides anything — deciding *where* a job
runs is `contracts/execution.py`, and populating the registry of what can
actually run is `wiring.build`, because only the composition root knows which
backends were assembled. An adapter that reached for a renderer to answer
"what can this machine do" would be an adapter importing another adapter, which
is the coupling `test_architecture_boundaries` exists to catch — and did.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from vtv.adapters.media import ffmpeg
from vtv.contracts.render import MAX_RENDER_WORKERS

#: Hardware H.264 encoders, by the vendor a person would recognise.
#:
#: Listed by ffmpeg encoder name because that is the only thing that can be
#: *checked*. "Has an RTX 4060" is not answerable from inside a container; "this
#: ffmpeg exposes h264_nvenc" is, and it is the question that decides whether a
#: command would work.
_ENCODERS: tuple[tuple[str, str], ...] = (
    ("h264_nvenc", "NVIDIA NVENC"),
    ("h264_qsv", "Intel Quick Sync"),
    ("h264_vaapi", "VAAPI"),
    ("h264_videotoolbox", "Apple VideoToolbox"),
    ("h264_amf", "AMD AMF"),
)

#: Measured share of render time spent encoding rather than composing.
#:
#: The number that makes a hardware-encode switch not worth shipping. Kept here
#: beside the detection so that anyone reading "NVENC available" reads this in
#: the same breath.
ENCODE_SHARE = 0.23


@dataclass(frozen=True)
class Compute:
    """What is available, and what the honest speedup would be."""

    cores: int
    #: Hardware encoders this ffmpeg build lists *and* the OS will open.
    hardware_encoders: tuple[str, ...]
    #: Whether frame composition can run on a GPU. Always false today: there is
    #: no GPU compositor, and saying otherwise would make the whole report a
    #: sales sheet rather than a measurement.
    gpu_composition: bool = False

    @property
    def render_workers(self) -> int:
        return max(1, min(MAX_RENDER_WORKERS, self.cores))

    @property
    def hardware_encode_ceiling(self) -> float:
        """Best case speedup from moving *only* the encode to hardware.

        Amdahl over the measured split. Reported as a number precisely so that
        nobody has to take "not worth it" on trust.
        """
        return round(1.0 / (1.0 - ENCODE_SHARE), 2)

    def as_json(self) -> dict[str, object]:
        return {
            "cores": self.cores,
            "render_workers": self.render_workers,
            "hardware_encoders": list(self.hardware_encoders),
            "gpu_composition": self.gpu_composition,
            # Stated, not implied. A client showing "GPU available" without
            # this would be promising a speedup the architecture cannot give.
            "hardware_encode_ceiling": self.hardware_encode_ceiling,
            "note": (
                "Composition is ~77% of render time and runs on the CPU across "
                f"{self.render_workers} worker(s). Hardware encoders affect the "
                "remaining ~23% only."
            ),
        }


@lru_cache(maxsize=1)
def detect() -> Compute:
    """Ask ffmpeg what it has. Cached: it cannot change while we run.

    Listing an encoder is not the same as being able to *use* it — a build can
    advertise `h264_nvenc` on a machine with no NVIDIA driver, and the failure
    then arrives as a broken render rather than a missing feature. So each
    candidate is opened for real against one black frame, which costs a few
    milliseconds once per process and is the difference between a capability
    report and a guess.
    """
    cores = os.cpu_count() or 1
    if not ffmpeg.is_available():
        return Compute(cores=cores, hardware_encoders=())

    try:
        listed = subprocess.run(
            [ffmpeg.FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no ffmpeg
        return Compute(cores=cores, hardware_encoders=())

    working: list[str] = []
    for encoder, _label in _ENCODERS:
        if encoder not in listed:
            continue
        if _can_encode(encoder):
            working.append(encoder)
    return Compute(cores=cores, hardware_encoders=tuple(working))


def _can_encode(encoder: str) -> bool:
    """Encode one frame of black and see whether it comes out."""
    try:
        result = subprocess.run(
            [
                ffmpeg.FFMPEG, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
                "-c:v", encoder, "-frames:v", "1", "-f", "null", "-",
            ],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False
    return result.returncode == 0


def label_for(encoder: str) -> str:
    """The vendor name a person would recognise, for an encoder id."""
    return dict(_ENCODERS).get(encoder, encoder)


__all__ = ["ENCODE_SHARE", "Compute", "detect", "label_for"]
