"""Phase E — find out what breaks when the videos get long.

    python scripts/verify_long_render.py                    # 5, 30, 60, 120, 240
    python scripts/verify_long_render.py --minutes 5,30
    python scripts/verify_long_render.py --minutes 240 --resume

The longest video this project has ever rendered end to end is **150 seconds**.
Everything above that is a guess, and the guesses that matter are not about
whether the renderer is correct — that is settled — but about what accumulates:
memory per segment, handles, disk, and whether ffmpeg will concatenate a
thousand files as happily as it concatenates thirteen.

## How it is built to be left alone

It runs shortest first and writes its report after **every** rung, so a machine
that dies in hour six still leaves five hours of evidence on disk. A rung that
fails is recorded and the next one starts anyway, because "240 broke" is worth
knowing even when 120 did too. Nothing is held only in memory and nothing waits
for a person.

## What it actually checks

Wall-clock and the ratio to realtime are the headline, but they are not the
finding. These are:

**The output is as long as it claims.** A four-hour render that quietly
produces forty minutes is the failure mode that a progress tally cannot see and
a person watching a log would believe. Every rung ends with `ffprobe` on the
real file and a comparison against the duration the timeline asked for.

**Memory does not climb with segment count.** Sampled from the desktop process
throughout, so a leak of a few megabytes per segment — invisible at thirteen
segments, fatal at twelve hundred — shows up as a slope rather than a crash.

**Disk high-water, not disk at the end.** A delivered job deletes its own
scratch directory, so measuring afterwards always finds zero.

Graphics memory is sampled too where `nvidia-smi` exists, because Phase F needs
a VRAM ceiling and this run is going to spend the hours anyway.

## What it does not do

It does not judge whether the video is any good. That is Phase G, and it needs
people.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_recovery import ROOT, Scene, Segments, walk  # noqa: E402

#: How often to sample memory, disk and segment count. Cheap enough to leave
#: running for four hours, frequent enough that a rung lasting a minute still
#: produces a curve rather than two points.
SAMPLE_SECONDS = 2.0

#: Shots stay six seconds up to an hour. Past that the timeline document grows
#: into thousands of clips, which is a different test — document size — wearing
#: this one's clothes. Twelve seconds keeps the clip count near two thousand at
#: four hours while leaving segmentation exactly as it is.
LONG_SHOT_SECONDS = 12.0
LONG_SHOT_ABOVE_MINUTES = 60.0

#: Where `LocalStorageProvider` keeps retention metadata. It mirrors the
#: key path, so it contains files with video names that are not videos.
META_DIR = ".vtv-meta"

try:  # Optional: it is not a dependency of the project.
    import psutil
except ImportError:  # pragma: no cover - depends on the machine
    psutil = None  # type: ignore[assignment]


def _nvidia_smi() -> int | None:
    """Graphics memory in use, in MB. None when there is no NVIDIA tool."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001 - a diagnostic must never be the failure
        return None


def _tree_bytes(root: Path) -> int:
    total = 0
    for path in walk(root):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _probe_seconds(path: Path) -> float | None:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path),
            ],
            capture_output=True, text=True, timeout=120,
        )
        return float(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


@dataclass
class Rung:
    """One length, and everything measured about it."""

    minutes: float
    ok: bool = False
    note: str = ""
    seed_seconds: float = 0.0
    render_seconds: float = 0.0
    realtime_ratio: float = 0.0
    segments: int = 0
    drawn_cpu: int = 0
    drawn_gpu: int = 0
    peak_rss_mb: float | None = None
    rss_slope_mb_per_segment: float | None = None
    peak_workspace_mb: float = 0.0
    peak_vram_mb: int | None = None
    output_mb: float = 0.0
    output_seconds: float | None = None
    duration_error_seconds: float | None = None
    samples: int = 0
    curve: list[tuple[int, float]] = field(default_factory=list)


def _tally(line: str) -> tuple[int, int]:
    """Read `1 on This computer, 10 on This computer's graphics card`."""
    cpu = gpu = 0
    for count, where in re.findall(r"(\d+) on ([^,]+)", line):
        if "graphics" in where:
            gpu += int(count)
        else:
            cpu += int(count)
    return cpu, gpu


def _slope(curve: list[tuple[int, float]]) -> float | None:
    """Megabytes of resident memory gained per finished segment.

    A least-squares fit rather than last-minus-first, because the first sample
    lands during start-up and the last during the concatenation, and both are
    outliers that would make an honest curve look alarming and an alarming one
    look fine.
    """
    points = [(x, y) for x, y in curve if x > 0]
    xs = [float(x) for x, _ in points]
    ys = [y for _, y in points]
    # A slope over five segments is noise wearing a number's clothes — it came
    # out at -4 MB/segment on a one-minute smoke run, which would read as memory
    # being *released* per segment rather than as too few points to say anything.
    # Below this it reports nothing, which is the honest answer.
    if len(points) < 20 or len(set(xs)) < 6:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    bottom = sum((x - mx) ** 2 for x in xs)
    if bottom <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / bottom


def _newest_video(storage: Path) -> Path | None:
    """The finished render, from the **server's** storage.

    Deliberately not the device's job directory: that is deleted the moment the
    render is delivered, so it is empty exactly when the answer is yes.

    ## Two things this has to step around

    **The retention sidecar.** `LocalStorageProvider` records an object's
    retention class in a tree that *mirrors the key path*, so beside every
    `renders/rnd_….mp4` there is a `.vtv-meta/…/renders/rnd_….mp4` holding
    twenty-six bytes of metadata. Both match `*.mp4` and both were written in
    the same instant, so picking "newest" picked the sidecar about half the
    time and reported that ffprobe could not read the output. It could not: it
    was reading a retention class.

    **Narration and assets are `.mp4`-adjacent.** Choosing the largest file
    would work today and is the wrong rule; the render lives under `renders/`
    and that is what identifies it.
    """
    found = [
        path
        for path in walk(storage, "*.mp4")
        if path.is_file()
        and META_DIR not in path.parts
        and "renders" in path.parts
    ]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _sample(scene: Scene, rung: Rung, watched: Any) -> float:
    """One reading of everything worth watching. Returns resident megabytes."""
    found = Segments.under(scene.workspace)
    rung.segments = max(rung.segments, len(found.seen))
    rung.peak_workspace_mb = max(
        rung.peak_workspace_mb, _tree_bytes(scene.workspace) / 1e6
    )
    vram = _nvidia_smi()
    if vram is not None:
        rung.peak_vram_mb = max(rung.peak_vram_mb or 0, vram)

    if watched is None:
        return 0.0
    try:
        # The children are the ffmpeg processes doing the encoding, and leaving
        # them out would report a renderer that uses almost no memory while the
        # machine swaps.
        rss = watched.memory_info().rss
        for child in watched.children(recursive=True):
            try:
                rss += child.memory_info().rss
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001 - it exited between two calls
        return 0.0
    rung.curve.append((len(found.seen), rss / 1e6))
    return rss / 1e6


def run_rung(minutes: float, args: argparse.Namespace, port: int) -> Rung:
    rung = Rung(minutes=minutes)
    seconds = minutes * 60.0
    scene = Scene(port=port, keep=args.keep)
    print(f"\n{'=' * 64}\n  {minutes:g} minutes\n{'=' * 64}\n", flush=True)

    try:
        scene.start_server()

        started = time.monotonic()
        shot = LONG_SHOT_SECONDS if minutes > LONG_SHOT_ABOVE_MINUTES else 6.0
        project = scene.seed(
            seconds,
            "--narration-format", "webm",
            "--shot-seconds", str(shot),
        )
        rung.seed_seconds = time.monotonic() - started
        print(f"seeded in {rung.seed_seconds:.0f}s: {project} ({shot:g}s shots)", flush=True)

        scene.pair()
        running = scene.start_desktop("--max-jobs", "1")
        time.sleep(2.0)
        scene.queue(project)

        watched = psutil.Process(running.pid) if psutil is not None else None
        peak_rss = 0.0
        started = time.monotonic()
        last_report = started

        while running.poll() is None:
            if time.monotonic() - started > args.timeout_hours * 3600:
                scene.kill_desktop()
                rung.note = f"gave up after {args.timeout_hours}h"
                break

            # Every sample, inside one guard.
            #
            # Instrumentation must never be able to fail the thing it is
            # instrumenting, and this block was bare inside the loop. A
            # delivered job deletes its own scratch directory, so the walk
            # sampling that directory is by definition still inside it when it
            # goes — and one `FileNotFoundError` out of a `rglob` loop header
            # ended a **240-minute render** as a failure. Twelve hundred
            # segments drawn correctly over six hours, delivered, and reported
            # as broken by the measurement tidying up behind itself.
            try:
                peak_rss = max(peak_rss, _sample(scene, rung, watched))
            except Exception:  # noqa: BLE001
                # A sample lost is a point missing from a curve. That is all it
                # should ever cost.
                pass

            rung.samples += 1
            now = time.monotonic()
            if now - last_report >= args.report_every:
                done = now - started
                print(
                    f"    {done / 60:6.1f} min in — {rung.segments:>4} segments, "
                    f"{rung.peak_workspace_mb:7.0f} MB scratch"
                    + (f", {peak_rss:6.0f} MB rss" if watched else "")
                    + (f", {rung.peak_vram_mb} MB vram" if rung.peak_vram_mb else ""),
                    flush=True,
                )
                last_report = now
            time.sleep(SAMPLE_SECONDS)

        rung.render_seconds = time.monotonic() - started
        rung.realtime_ratio = seconds / rung.render_seconds if rung.render_seconds else 0.0
        rung.peak_rss_mb = round(peak_rss, 1) if watched else None
        slope = _slope(rung.curve)
        rung.rss_slope_mb_per_segment = round(slope, 3) if slope is not None else None
        # The curve itself is thousands of points at four hours; the slope and
        # the peak are the findings, and the report has to stay readable.
        rung.curve = rung.curve[:: max(1, len(rung.curve) // 40)]

        output = (running.stdout.read() if running.stdout else "") or ""
        done = [line for line in output.splitlines() if "done in" in line]
        if done:
            rung.drawn_cpu, rung.drawn_gpu = _tally(done[0])
            print(f"\n    {done[0].strip()}", flush=True)

        video = _newest_video(Path(scene.env["VTV_STORAGE_ROOT"]))
        if video is None:
            rung.note = rung.note or "no video reached storage"
            print(f"    FAIL: {rung.note}")
            print(output[-1500:])
            return rung

        rung.output_mb = video.stat().st_size / 1e6
        rung.output_seconds = _probe_seconds(video)
        if rung.output_seconds is None:
            rung.note = "ffprobe could not read the output"
            return rung

        rung.duration_error_seconds = round(rung.output_seconds - seconds, 3)
        # A tenth of a percent, or a second, whichever is kinder. Frame-rate
        # rounding at the last segment is real and is not a defect; forty
        # minutes missing from four hours is.
        tolerance = max(1.0, seconds * 0.001)
        rung.ok = abs(rung.duration_error_seconds) <= tolerance
        if not rung.ok:
            rung.note = (
                f"output is {rung.output_seconds:.1f}s, timeline asked for "
                f"{seconds:.1f}s"
            )
        return rung
    except Exception as error:  # noqa: BLE001 - one rung must not end the run
        rung.note = f"{type(error).__name__}: {error}"
        return rung
    finally:
        scene.close()


def write_report(rungs: list[Rung], path: Path, args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "machine": {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "psutil": psutil is not None,
                    "nvidia_smi": shutil.which("nvidia-smi") is not None,
                },
                "rungs": [asdict(r) for r in rungs],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    lines = [
        "# Phase E — long-video validation",
        "",
        f"`{platform.platform()}`, Python {platform.python_version()}.",
        "",
    ]
    if psutil is None:
        lines += [
            "> Memory was **not** measured: `psutil` is not installed. Install it "
            "and re-run if the memory question matters — the timings below are "
            "unaffected.",
            "",
        ]
    lines += [
        "| Length | Result | Render | vs realtime | Segments | CPU/GPU | Peak RSS | MB/segment | Peak scratch | Peak VRAM | Output | Duration error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rungs:
        lines.append(
            f"| {r.minutes:g} min "
            f"| {'PASS' if r.ok else 'FAIL'} "
            f"| {r.render_seconds / 60:.1f} min "
            f"| {r.realtime_ratio:.2f}x "
            f"| {r.segments} "
            f"| {r.drawn_cpu}/{r.drawn_gpu} "
            f"| {f'{r.peak_rss_mb:.0f} MB' if r.peak_rss_mb else '—'} "
            f"| {r.rss_slope_mb_per_segment if r.rss_slope_mb_per_segment is not None else '—'} "
            f"| {r.peak_workspace_mb:.0f} MB "
            f"| {f'{r.peak_vram_mb} MB' if r.peak_vram_mb else '—'} "
            f"| {r.output_mb:.0f} MB "
            f"| {f'{r.duration_error_seconds:+.2f}s' if r.duration_error_seconds is not None else '—'} |"
        )
    lines.append("")
    for r in rungs:
        if r.note:
            lines.append(f"- **{r.minutes:g} min** — {r.note}")
    lines += [
        "",
        "## Reading the columns that matter",
        "",
        "**MB/segment** is the least-squares slope of resident memory against "
        "finished segments. Near zero is what a renderer that releases what it "
        "allocates looks like. A few megabytes is invisible at thirteen segments "
        "and is a gigabyte by twelve hundred, which is the whole reason for "
        "measuring at length rather than inferring from a short run. A dash "
        "means too few segments to fit a line through; short rungs will not "
        "answer this question and do not pretend to.",
        "",
        "**Duration error** is the output measured by `ffprobe` against what the "
        "timeline asked for. This is the column that catches a render which "
        "reports every segment complete and produces a file that stops early.",
        "",
        "**Peak scratch** is a high-water mark taken during the run. Measuring "
        "afterwards always reads zero, because a delivered job removes its own "
        "directory.",
        "",
        f"Run with `--minutes {args.minutes}`.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--minutes", default="5,30,60,120,240")
    parser.add_argument("--port", type=int, default=8971)
    parser.add_argument("--report", default=str(ROOT / "var" / "phase-e-report.md"))
    parser.add_argument("--report-every", type=float, default=120.0,
                        help="seconds between progress lines")
    parser.add_argument("--timeout-hours", type=float, default=8.0,
                        help="give up on a single rung after this long")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    lengths = [float(part) for part in args.minutes.split(",") if part.strip()]
    report = Path(args.report)
    rungs: list[Rung] = []

    print(f"Phase E — {', '.join(f'{m:g}' for m in lengths)} minutes")
    print(f"Report written after every rung to {report}")
    if psutil is None:
        print("psutil is not installed, so memory will not be measured.")
    print()

    for index, minutes in enumerate(lengths):
        rungs.append(run_rung(minutes, args, args.port + index))
        # After every rung, not at the end. Eight hours of work must not be
        # contingent on the ninth hour going well.
        write_report(rungs, report, args)
        print(f"\n  {minutes:g} min: {'PASS' if rungs[-1].ok else 'FAIL'}"
              f"{' — ' + rungs[-1].note if rungs[-1].note else ''}", flush=True)

    print("\n" + "=" * 64)
    for r in rungs:
        print(f"  {r.minutes:>5g} min   {'PASS' if r.ok else 'FAIL'}   "
              f"{r.render_seconds / 60:6.1f} min   {r.realtime_ratio:.2f}x realtime")
    print("=" * 64)
    print(f"\n{report}")
    return 0 if all(r.ok for r in rungs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
