"""Is this machine's graphics card actually faster at compositing than its cores?

    python scripts/bench_gpu_renderer.py
    python scripts/bench_gpu_renderer.py --resolution 4k --frames 200

Runs on the machine you want the answer about. It reports what it measured and
refuses to report anything it did not: with no usable device it prints the
reason and stops, rather than printing a zero that reads like a result.

## What it measures, and what it does not

**Composition**, in frames per second, per content kind — because the answer
differs by content and a single number would hide that. A typography-heavy
video has its frames produced by the animation engine on the processor and then
uploaded, so the card has little to do and the upload is new work. An
image-heavy video is transforms and compositing, which is what a card is for.

Encoding is timed separately and is not part of the comparison. It is about 23%
of render time on both paths and it is the same x264 either way.

## What "faster" has to mean here

`calibration.MATERIAL_GAIN` — ten percent. Below that the difference is inside
the noise of a short sample, and switching hardware on that evidence buys
nothing while adding a driver, a memory ceiling and a fallback path the slower
route does not have. A tie goes to the processor.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from vtv.animation.engine import AnimationEngine, RenderSize  # noqa: E402
from vtv.animation.equivalence import check, summarise  # noqa: E402
from vtv.animation.painter import CpuPainter  # noqa: E402
from vtv.animation.theme import Theme  # noqa: E402
from vtv.contracts.base import TimeSpan  # noqa: E402
from vtv.contracts.display import (  # noqa: E402
    Background,
    DisplayList,
    Drawn,
    Label,
    Mix,
    Picture,
    Plate,
)
from vtv.contracts.style import StyleProfile  # noqa: E402
from vtv.contracts.visual_language import TypographySpec  # noqa: E402
from vtv.pipeline.calibration import MATERIAL_GAIN  # noqa: E402

RESOLUTIONS = {"1080p": (1920, 1080), "720p": (1280, 720), "4k": (3840, 2160)}


def photograph(width: int, height: int) -> Image.Image:
    """Something with structure, so a wrong transform is visible."""
    image = Image.new("RGB", (width * 2, height * 2), (40, 60, 110))
    draw = ImageDraw.Draw(image)
    for x in range(0, width * 2, 80):
        draw.rectangle([x, 0, x + 40, height * 2], fill=(200, 120, 40))
    draw.ellipse([100, 100, 600, 500], fill=(240, 240, 250))
    return image


def workloads(width: int, height: int) -> dict[str, list[DisplayList]]:
    """The four content kinds, because the answer differs between them."""
    spec = TypographySpec(headline="A line of narration on screen")
    background = Background(colour=(14, 14, 18, 255))
    plate = Plate(
        box=(0.06 * width, 0.78 * height, 0.94 * width, 0.92 * height),
        radius=0.012 * width,
        fill=(0, 0, 0, 140),
    )
    label = Label(
        text="A caption line that a viewer reads",
        role="body",
        position=(0.09 * width, 0.81 * height),
        colour=(240, 240, 245, 255),
    )

    def picture(fraction: float) -> Picture:
        zoom = 1.0 + 0.10 * fraction
        w, h = width * zoom, height * zoom
        return Picture(
            key="photo",
            box=((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2),
        )

    def frame(*layers: object, **kw: object) -> DisplayList:
        return DisplayList(width=width, height=height, layers=tuple(layers), **kw)  # type: ignore[arg-type]

    steps = [index / 24 for index in range(24)]
    return {
        "typography": [
            frame(Drawn(spec=spec, seconds=t, duration=4.0)) for t in steps
        ],
        "images": [frame(picture(t)) for t in steps],
        "transitions": [
            DisplayList(
                width=width, height=height, layers=(picture(t),),
                beneath=frame(picture(0.0)), transition=Mix.BLEND, progress=t,
            )
            for t in steps
        ],
        "mixed": [
            DisplayList(
                width=width, height=height, layers=(picture(t),),
                overlays=(plate, label),
            )
            if index % 2
            else frame(background, Drawn(spec=spec, seconds=t, duration=4.0))
            for index, t in enumerate(steps)
        ],
    }


def rate(painter: object, frames: list[DisplayList], repeats: int) -> float:
    painter.paint(frames[0])  # warm: first call pays for fonts and pipelines
    started = time.perf_counter()
    count = 0
    for _ in range(repeats):
        for frame in frames:
            painter.paint(frame)
            count += 1
    elapsed = time.perf_counter() - started
    return count / elapsed if elapsed > 0 else float("inf")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolution", choices=sorted(RESOLUTIONS), default="1080p")
    parser.add_argument("--frames", type=int, default=96)
    args = parser.parse_args()

    width, height = RESOLUTIONS[args.resolution]
    print(f"machine    : {platform.platform()}")
    print(f"resolution : {width}x{height}")

    from vtv.adapters.render.gpu_probe import describe, probe

    found = probe()
    print(f"gpu        : {describe()}")
    if not found.available:
        # Two very different situations, and the first version of this said
        # "install a graphics library" to somebody who had a working GTX 1650
        # and a correctness failure. A message that names the wrong problem is
        # worse than no message.
        print()
        if "differ" in found.reason or "scenes" in found.reason:
            print("The device works. The pictures do not match the reference yet.")
            print("Speed is not the question until they do. Diagnose with:")
            print("    python scripts/diagnose_gpu_painter.py")
        else:
            print("No usable graphics device, so there is nothing to compare.")
            print("Install a graphics library and re-run:  pip install moderngl")
        return 1
    print(f"device     : {found.vendor} {found.device} ({found.api})")
    print(f"calibration: worst channel difference {found.calibration_difference}")

    theme = Theme.from_style(StyleProfile(), width=width, height=height)
    size = RenderSize(width, height)
    engine = AnimationEngine(StyleProfile())
    photo = photograph(width, height)

    def stills(_key: str) -> Image.Image:
        return photo

    from vtv.adapters.render.gpu_painter import GpuPainter

    cpu = CpuPainter(theme=theme, engine=engine, size=size, stills=stills)
    gpu = GpuPainter(theme=theme, engine=engine, size=size, stills=stills)

    print("\ncorrectness")
    differences = check(cpu, gpu, width=min(width, 640), height=min(height, 360))
    print(f"  {summarise(differences)}")
    if any(not d.ok for d in differences):
        print("\nThe pictures differ. Speed is not the question yet.")
        gpu.close()
        return 2

    repeats = max(1, args.frames // 24)
    print(f"\ncomposition ({repeats * 24} frames per workload)")
    print(f"  {'workload':<14}{'CPU fps':>10}{'GPU fps':>10}{'speedup':>10}  verdict")
    verdicts: dict[str, float] = {}
    for name, frames in workloads(width, height).items():
        on_cpu = rate(cpu, frames, repeats)
        on_gpu = rate(gpu, frames, repeats)
        speedup = on_gpu / on_cpu if on_cpu else 0.0
        verdicts[name] = speedup
        verdict = "GPU" if speedup >= MATERIAL_GAIN else "CPU (tie or slower)"
        print(f"  {name:<14}{on_cpu:>10.1f}{on_gpu:>10.1f}{speedup:>9.2f}x  {verdict}")
        cpu.release()
        gpu.release()

    gpu.close()
    best = max(verdicts.values(), default=0.0)
    print(
        f"\nVerdict: {'use the GPU' if best >= MATERIAL_GAIN else 'stay on the CPU'} "
        f"(best {best:.2f}x, threshold {MATERIAL_GAIN:.2f}x)"
    )
    print("Paste this output back and the result becomes the default policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
