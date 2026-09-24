"""Does the graphics card survive a real render, and is falling back safe?

    python scripts/verify_gpu_render.py
    python scripts/verify_gpu_render.py --seconds 90 --fail-at 2

The equivalence harness compares twenty-three frames. This renders a whole
video three times and compares the files, which is a different question: a
painter can be pixel-correct on a synthetic frame and still exhaust video
memory, leak textures across segments, or produce a segment ffmpeg will not
concatenate.

## The three runs

1. **CPU only.** The reference, and the timing baseline.
2. **GPU only**, strictly — no fallback in the chain, so a device failure is a
   failed render rather than a quiet return to the processor. That is the point:
   a run that silently fell back would report a GPU render that never happened.
3. **GPU with a device loss injected at segment `--fail-at`.** The first
   segments are drawn on the card, the rest on the processor, in one render.

## What run 3 actually proves

That the segments the fallback drew on the processor are **byte-identical** to
the ones a processor-only render drew — compared with `framemd5`, which hashes
decoded frames rather than the container, so two files made a second apart do
not differ for having different timestamps in them.

This is the claim worth making, and it is narrower than "the fallback is
correct". A GPU segment and a CPU segment are *not* identical — they differ by
up to the equivalence tolerance, which is what the tolerance is for. What must
not happen is a fallback that produces something different from an ordinary CPU
render: a half-initialised context, a stale texture, a frame index off by one
where the handover happened. That is a real failure mode, it would be invisible
in a finished video, and `framemd5` catches it exactly.

## Why `--workers 1`

Bit-identity is the measurement, so the pool is removed rather than trusted.
Parallel encoding was already proven frame-identical separately; mixing the two
questions would mean a failure here could be either.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import time
from asyncio import run
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw

from vtv.adapters.media import ffmpeg
from vtv.adapters.render.cpu_backend import (
    CpuRenderBackend,
    GpuRenderBackend,
)
from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
from vtv.adapters.render.segments import Segment
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import TimeSpan
from vtv.contracts.errors import Status
from vtv.contracts.execution import (
    ExecutionPolicy,
    ExecutionTarget,
    Registry,
)
from vtv.contracts.render import RenderSettings
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.timeline import (
    AssetClipSource,
    CameraMotion,
    NarrationTrack,
    ProgrammaticClipSource,
    Timeline,
    Transition,
    TransitionKind,
    VisualClip,
)
from vtv.contracts.visual_language import TypographySpec
from vtv.ports.rendering import SegmentOutcome

SHOT = 6.0
_KINDS = (TransitionKind.DISSOLVE, TransitionKind.WIPE, TransitionKind.PUSH)
RESOLUTIONS = {"1080p": (1920, 1080), "720p": (1280, 720)}


# -- the harness ----------------------------------------------------------


@dataclass
class Observed:
    """A backend that records what it drew, and can be told to fail.

    Wraps rather than replaces, so the thing under test is the real backend.
    A stub that merely *looked* like a GPU backend would prove that the
    fallback logic works, which is not in doubt, rather than that this card
    hands over cleanly, which is.
    """

    inner: object
    keep: Path
    label: str
    fail_from: int | None = None
    drew: dict[int, str] = field(default_factory=dict)
    refused: list[int] = field(default_factory=list)

    @property
    def target(self) -> ExecutionTarget:
        return self.inner.target  # type: ignore[attr-defined]

    def capabilities(self) -> object:
        return self.inner.capabilities()  # type: ignore[attr-defined]

    def render_segment(
        self, scratch: str, index: int, start_frame: int, end_frame: int
    ) -> SegmentOutcome:
        if self.fail_from is not None and index >= self.fail_from:
            # `elsewhere=True` because a device that has gone away is exactly
            # the case the chain exists for. A backend reporting a missing
            # asset would say False and stop the descent.
            self.refused.append(index)
            return SegmentOutcome.failed(
                f"segment {index}: simulated device loss", elsewhere=True
            )
        outcome = self.inner.render_segment(  # type: ignore[attr-defined]
            scratch, index, start_frame, end_frame
        )
        if outcome.ok:
            self.drew[index] = self.label
            name = Segment(index, start_frame, end_frame).name
            self.keep.mkdir(parents=True, exist_ok=True)
            source = Path(scratch) / name
            if source.exists():
                # Copied out because a successful render deletes its scratch
                # directory, and the segments are the evidence.
                shutil.copy2(source, self.keep / name)
        return outcome


def framemd5(path: Path) -> list[str]:
    """Hash the decoded frames, not the file.

    An mp4 carries creation timestamps, so two byte-identical renderings made a
    second apart are different files. `framemd5` compares what a viewer would
    see, which is the thing that must not change.
    """
    result = subprocess.run(
        [ffmpeg.FFMPEG, "-v", "error", "-i", str(path), "-an", "-f", "framemd5", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-400:])
    return [
        line
        for line in result.stdout.decode().splitlines()
        if line and not line.startswith("#")
    ]


# -- the material ---------------------------------------------------------


def photograph(width: int, height: int) -> bytes:
    """A still with detail that rings, saved as PNG.

    Fine stripes and saturated white, deliberately: soft material cannot tell a
    correct resampler from an incorrect one, which is how a real defect
    survived a full round of hardware testing on this project.
    """
    image = Image.new("RGB", (width * 2, height * 2), (8, 10, 14))
    draw = ImageDraw.Draw(image)
    for x in range(0, width * 2, 14):
        draw.line([(x, 0), (x, height * 2)], fill=(255, 255, 255), width=3)
    for y in range(0, height * 2, 46):
        draw.line([(0, y), (width * 2, y)], fill=(0, 0, 0), width=2)
    draw.ellipse(
        [width // 4, height // 4, width, height], outline=(255, 200, 60), width=9
    )
    draw.rectangle([60, 60, 400, 300], fill=(255, 255, 255))
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_timeline(seconds: float, storage: object, root: Path) -> Timeline:
    """Mixed content: typography, photographs, camera moves and transitions.

    Alternating rather than grouped, so every segment boundary has a chance of
    landing inside a shot of either kind and every transition is between two
    different sorts of frame. A video that was all photographs would exercise
    one path and prove the other untested.
    """
    audio_path = root / "narration.wav"
    ffmpeg.synthesise_tone_audio(
        audio_path, duration=seconds, segments=[(0.5, seconds - 0.5)]
    )
    audio = run(
        storage.put(  # type: ignore[attr-defined]
            key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/p/narration.wav",
            data=audio_path.read_bytes(),
            content_type="audio/wav",
        )
    )
    picture = run(
        storage.put(  # type: ignore[attr-defined]
            key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/p/photo.png",
            data=photograph(960, 540),
            content_type="image/png",
        )
    )

    shots = max(2, int(seconds / SHOT))
    motions = [CameraMotion.KEN_BURNS, CameraMotion.PAN_RIGHT, CameraMotion.ZOOM_IN]
    clips = []
    for index in range(shots):
        start, end = index * SHOT, min((index + 1) * SHOT, seconds)
        if start >= seconds:
            break
        common = {
            "scene_id": f"scn_{index:024d}",
            "span": TimeSpan.of(start, end),
            # Every kind the painter mixes, and a cut to open on. Cycled so a
            # segment boundary lands in a different sort of transition each
            # time round rather than always the same one.
            "transition_in": Transition(
                kind=TransitionKind.CUT if index == 0 else _KINDS[index % len(_KINDS)],
                duration_seconds=0.0 if index == 0 else 0.8,
            ),
        }
        if index % 2:
            clips.append(
                VisualClip(
                    source=AssetClipSource(
                        asset_id="ast_" + "b" * 24,
                        object=picture,
                        attribution="Test pattern, public domain",
                    ),
                    camera_motion=motions[index % len(motions)],
                    **common,
                )
            )
        else:
            clips.append(
                VisualClip(
                    source=ProgrammaticClipSource(
                        spec=TypographySpec(headline=f"Section {index}")
                    ),
                    **common,
                )
            )
    return Timeline(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id="prj_" + "a" * 24,
        scene_graph_id="sgr_" + "a" * 24,
        narration=NarrationTrack(audio=audio, duration_seconds=seconds),
        style=StyleProfile(captions_enabled=False),
        clips=clips,
        captions=[],
        status=Status.READY,
    )


# -- the runs -------------------------------------------------------------


@dataclass
class Run:
    name: str
    seconds: float
    status: str
    bytes_out: int
    keep: Path
    drew: dict[int, str]
    refused: list[int]
    error: str = ""


def render_once(
    *,
    name: str,
    timeline: Timeline,
    settings: RenderSettings,
    root: Path,
    gpu: bool,
    fallback: bool,
    fail_from: int | None,
) -> Run:
    """One whole render, into its own working directory.

    Its own directory on purpose: a scratch directory is named by *what is being
    rendered*, so two runs of the same timeline would share one and the second
    would resume from the first's segments instead of drawing them. That is the
    right behaviour for a retry and it would make this measurement meaningless.
    """
    keep = root / f"keep-{name}"
    registry = Registry()
    observers: list[Observed] = []

    if gpu:
        watcher = Observed(
            inner=GpuRenderBackend(), keep=keep, label="gpu", fail_from=fail_from
        )
        registry.register(ExecutionTarget.LOCAL_GPU, watcher)
        observers.append(watcher)
    if not gpu or fallback:
        watcher = Observed(inner=CpuRenderBackend(where=ExecutionTarget.LOCAL_CPU), keep=keep, label="cpu")
        registry.register(ExecutionTarget.LOCAL_CPU, watcher)
        observers.append(watcher)

    policy = (
        ExecutionPolicy.exactly(ExecutionTarget.LOCAL_GPU, strict=not fallback)
        if gpu
        else ExecutionPolicy.exactly(ExecutionTarget.LOCAL_CPU, strict=True)
    )

    renderer = FfmpegRenderer(
        storage=LocalStorageProvider(root / "storage"),
        events=_silent(),
        workdir=root / f"work-{name}",
        workers=1,
        backends=registry,
        policy=policy,
    )
    started = time.perf_counter()
    status, size, error = "failed", 0, ""
    try:
        job = run(renderer.render(timeline=timeline, settings=settings))
        status = job.status.value
        size = job.output.size_bytes if job.output else 0
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    drew: dict[int, str] = {}
    refused: list[int] = []
    for watcher in observers:
        drew.update(watcher.drew)
        refused += watcher.refused
    return Run(name, elapsed, status, size, keep, drew, sorted(refused), error)


def _silent() -> object:
    from vtv.observability.events import EventSink

    return EventSink()


def compare_segments(one: Run, two: Run, indices: list[int]) -> tuple[int, list[str]]:
    """Byte-for-byte on decoded frames, for the segments both runs produced."""
    problems: list[str] = []
    checked = 0
    for index in indices:
        name = f"seg-{index:05d}.mp4"
        a, b = one.keep / name, two.keep / name
        if not a.exists() or not b.exists():
            problems.append(f"segment {index}: missing from {one.name if not a.exists() else two.name}")
            continue
        if framemd5(a) != framemd5(b):
            problems.append(f"segment {index}: frames differ between {one.name} and {two.name}")
        checked += 1
    return checked, problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=48.0)
    parser.add_argument("--resolution", choices=sorted(RESOLUTIONS), default="1080p")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--fail-at",
        type=int,
        default=1,
        help="segment index where the card is made to disappear",
    )
    args = parser.parse_args()

    width, height = RESOLUTIONS[args.resolution]
    print(f"machine    : {platform.platform()}")
    print(f"video      : {args.seconds:.0f}s of {width}x{height} at {args.fps}fps, mixed content")

    from vtv.adapters.render.gpu_probe import describe, probe

    found = probe()
    print(f"gpu        : {describe()}")
    if not found.available:
        print("\nNo usable graphics device, so there is nothing to verify.")
        print("Diagnose with:  python scripts/diagnose_gpu_painter.py --photo")
        return 1

    settings = RenderSettings(frame_rate=args.fps)
    with TemporaryDirectory(prefix="vtv-verify-") as directory:
        root = Path(directory)
        storage = LocalStorageProvider(root / "storage")
        timeline = build_timeline(args.seconds, storage, root)
        print(f"clips      : {len(timeline.clips)} shots, alternating typography and photographs")

        runs = {}
        for name, gpu, fallback, fail_from in (
            ("cpu", False, False, None),
            ("gpu", True, False, None),
            ("fallback", True, True, args.fail_at),
        ):
            print(f"\nrendering: {name} ...", flush=True)
            outcome = render_once(
                name=name, timeline=timeline, settings=settings, root=root,
                gpu=gpu, fallback=fallback, fail_from=fail_from,
            )
            runs[name] = outcome
            if outcome.error:
                print(f"  FAILED  {outcome.error}")
            else:
                ratio = outcome.seconds / max(0.001, args.seconds)
                fps = (args.seconds * args.fps) / max(0.001, outcome.seconds)
                print(
                    f"  {outcome.status:<10} {outcome.seconds:7.1f}s wall  "
                    f"{ratio:5.2f}x realtime  {fps:6.1f} fps  "
                    f"{outcome.bytes_out / 1e6:6.2f} MB"
                )
                drawn = outcome.drew
                on_gpu = sorted(i for i, w in drawn.items() if w == "gpu")
                on_cpu = sorted(i for i, w in drawn.items() if w == "cpu")
                print(f"  segments  gpu={len(on_gpu)} cpu={len(on_cpu)} fell back={len(outcome.refused)}")

        print("\n--- 3. mixed-content render ---")
        both = [runs["cpu"], runs["gpu"]]
        if any(r.error or r.status != "ready" for r in both):
            print("  FAIL: a render did not complete; see above")
            return 2
        speedup = runs["cpu"].seconds / max(0.001, runs["gpu"].seconds)
        print(f"  both renders completed. end-to-end speedup {speedup:.2f}x")
        print("  (end to end, so it includes encoding, which is the same x264 either way)")

        print("\n--- 4. fallback equivalence ---")
        fall = runs["fallback"]
        if fall.error or fall.status != "ready":
            print("  FAIL: the fallback render did not complete")
            return 3
        if not fall.refused:
            print("  INCONCLUSIVE: no segment was refused, so nothing fell back")
            return 4
        after = sorted(i for i, w in fall.drew.items() if w == "cpu")
        before = sorted(i for i, w in fall.drew.items() if w == "gpu")
        checked, problems = compare_segments(fall, runs["cpu"], after)
        print(
            f"  the card was lost at segment {args.fail_at}: "
            f"{len(before)} drawn on it, {len(after)} on the processor"
        )
        print(f"  comparing those {checked} processor-drawn segments against the cpu-only render")
        for line in problems:
            print(f"    {line}")
        if problems or not checked:
            print("  FAIL: a fallback segment is not what a cpu-only render produces")
            return 5
        print("  identical by framemd5. Falling back mid-render changes nothing.")

    print("\nAll four B2 checks have now been run on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
