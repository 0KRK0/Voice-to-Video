"""Why does the GPU painter disagree with the reference, scene by scene?

    python scripts/diagnose_gpu_painter.py
    python scripts/diagnose_gpu_painter.py --out var/gpu-diagnosis --photo

Writes, for every scene that differs: the CPU frame, the GPU frame, an amplified
difference image, and one JSON report with the measurements and a *classified
cause*. Run it, look at the PNGs, and send back `report.json`.

## Why classification rather than just numbers

"Worst 136 over 1230 pixels" says a scene is wrong. It does not say whether the
picture is upside down, two pixels left, blended against the wrong colour, or
sampled with a different kernel — and those are four different fixes. Each of
the checks below answers one of those questions by *transforming the GPU frame
and seeing whether the difference collapses*, which turns an argument into a
measurement.

That is how the first round was diagnosed. Eight scenes failed on a GTX 1650,
every one of them containing something vertically asymmetric and every passing
one symmetric — the signature of a mirrored framebuffer readback, confirmed by
a plate at y 11..50 differing across y 11..169.

## The one open question this is built to answer

The reference resizes photographs with LANCZOS; a graphics card samples with
its own filter. On flat colours that is identical, which is why the built-in
calibration still is flat — it isolates geometry from resampling. `--photo`
swaps in a real photograph, and the difference between the two runs is exactly
the cost of the sampler. If that alone exceeds the tolerance, the tolerance
policy needs a considered answer, not a looser number.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageChops, ImageDraw  # noqa: E402

from vtv.animation.engine import AnimationEngine, RenderSize  # noqa: E402
from vtv.animation.equivalence import (  # noqa: E402
    TOLERANCE,
    classify,
    compare,
    measure,
    scenes,
)
from vtv.animation.painter import CpuPainter  # noqa: E402
from vtv.animation.theme import Theme  # noqa: E402
from vtv.contracts.style import StyleProfile  # noqa: E402


def flat_still(_key: str) -> Image.Image:
    """Deliberately featureless: isolates geometry from resampling."""
    return Image.new("RGB", (200, 120), (90, 120, 200))


def detailed_still(_key: str) -> Image.Image:
    """Structure and fine detail, so a resampling difference shows up."""
    image = Image.new("RGB", (400, 260), (40, 60, 110))
    draw = ImageDraw.Draw(image)
    for x in range(0, 400, 7):
        draw.line([(x, 0), (x, 260)], fill=(210, 130, 50), width=2)
    for y in range(0, 260, 23):
        draw.line([(0, y), (400, y)], fill=(240, 240, 250), width=1)
    draw.ellipse([60, 40, 220, 170], outline=(255, 255, 255), width=3)
    return image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="var/gpu-diagnosis")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument(
        "--photo",
        action="store_true",
        help="use a detailed still, to expose resampling differences",
    )
    parser.add_argument(
        "--all", action="store_true", help="write images for matching scenes too"
    )
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    width, height = args.width, args.height
    stills = detailed_still if args.photo else flat_still

    theme = Theme.from_style(StyleProfile(), width=width, height=height)
    engine = AnimationEngine(StyleProfile())
    size = RenderSize(width, height)
    cpu = CpuPainter(theme=theme, engine=engine, size=size, stills=stills)

    report: dict = {
        "machine": platform.platform(),
        "frame": [width, height],
        "still": "detailed" if args.photo else "flat",
        "tolerance": TOLERANCE,
        "theme_background": list(theme.background),
        "scenes": {},
    }

    try:
        from vtv.adapters.render.gpu_painter import GpuPainter

        gpu = GpuPainter(theme=theme, engine=engine, size=size, stills=stills)
    except Exception as exc:
        print(f"The GPU painter could not start: {type(exc).__name__}: {exc}")
        print("That is the whole diagnosis — nothing else can be measured.")
        report["error"] = f"{type(exc).__name__}: {exc}"
        (out / "report.json").write_text(json.dumps(report, indent=2))
        return 1

    report["device"] = {
        key: str(value)
        for key, value in gpu.ctx.info.items()
        if key.startswith("GL_") and "EXTENSION" not in key
    }
    print(f"device : {report['device'].get('GL_RENDERER', '?')}")
    print(f"still  : {report['still']}\n")

    failed = 0
    for name, frame in scenes(width, height).items():
        try:
            a = cpu.paint(frame)
            b = gpu.paint(frame)
        except Exception as exc:
            report["scenes"][name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"  {name:22} ERROR  {type(exc).__name__}: {exc}")
            failed += 1
            continue

        verdict = compare(a, b, scene=name)
        entry = measure(a, b)
        entry["ok"] = verdict.ok
        if not verdict.ok:
            entry["likely_cause"] = classify(a, b)
            failed += 1
        report["scenes"][name] = entry

        if not verdict.ok or args.all:
            a.save(out / f"{name}.cpu.png")
            b.save(out / f"{name}.gpu.png")
            # Amplified, because a difference of 11 on a dark plate is
            # invisible at true scale and is still a defect.
            ImageChops.difference(a.convert("RGB"), b.convert("RGB")).point(
                lambda level: min(255, level * 8)
            ).save(out / f"{name}.diff.png")

        mark = "ok  " if verdict.ok else "FAIL"
        detail = "" if verdict.ok else "  " + "; ".join(entry["likely_cause"])
        print(
            f"  {name:22} {mark} worst {entry['max_per_channel']:>3} "
            f"outliers {entry['pixels_outside_tolerance']:>6}{detail}"
        )
        cpu.release()
        gpu.release()

    gpu.close()
    (out / "report.json").write_text(json.dumps(report, indent=2))
    total = len(report["scenes"])
    print(f"\n{total - failed}/{total} scenes match")
    print(f"images and report.json written to {out.resolve()}")
    if failed:
        print("Send back report.json and the .diff.png files.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
