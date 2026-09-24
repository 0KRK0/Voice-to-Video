"""Measure render speed against real time, at a given worker count.

    python scripts/bench_render.py --seconds 60 --workers 1
    python scripts/bench_render.py --seconds 60 --workers 4

Prints the ratio the user actually feels: how many seconds of wall clock one
second of finished video costs. A forty-minute video measured at 4.0 before
segments existed, which is why this script does.

Typography only, so nothing is downloaded and the number is about composition
and encoding rather than about somebody's network.
"""

from __future__ import annotations

import argparse
import time
from asyncio import run
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.media import ffmpeg
from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import TimeSpan
from vtv.contracts.errors import Status
from vtv.contracts.render import RenderSettings
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.timeline import (
    NarrationTrack,
    ProgrammaticClipSource,
    Timeline,
    VisualClip,
)
from vtv.contracts.visual_language import TypographySpec
from vtv.observability.events import EventSink

SHOT = 4.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    with TemporaryDirectory(prefix="vtv-bench-") as directory:
        root = Path(directory)
        storage = LocalStorageProvider(root / "storage")
        audio_path = root / "narration.wav"
        ffmpeg.synthesise_tone_audio(
            audio_path, duration=args.seconds, segments=[(0.5, args.seconds - 0.5)]
        )
        audio = run(
            storage.put(
                key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/p/narration.wav",
                data=audio_path.read_bytes(),
                content_type="audio/wav",
            )
        )
        shots = max(1, int(args.seconds / SHOT))
        timeline = Timeline(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id="prj_" + "a" * 24,
            scene_graph_id="sgr_" + "a" * 24,
            narration=NarrationTrack(audio=audio, duration_seconds=args.seconds),
            style=StyleProfile(captions_enabled=False),
            clips=[
                VisualClip(
                    scene_id=f"scn_{index:024d}",
                    span=TimeSpan.of(
                        index * SHOT,
                        min((index + 1) * SHOT, args.seconds),
                    ),
                    source=ProgrammaticClipSource(
                        spec=TypographySpec(headline=f"Idea number {index}")
                    ),
                )
                for index in range(shots)
                if index * SHOT < args.seconds
            ],
            captions=[],
            status=Status.READY,
        )

        started = time.perf_counter()
        job = run(
            FfmpegRenderer(
                storage=storage,
                events=EventSink(),
                workdir=root / "work",
                workers=args.workers,
            ).render(timeline=timeline, settings=RenderSettings(frame_rate=args.fps))
        )
        elapsed = time.perf_counter() - started

    print(
        f"{args.seconds:.0f}s of video | {args.workers} worker(s) | "
        f"{elapsed:.1f}s wall | {elapsed / args.seconds:.2f}x realtime | "
        f"{(args.seconds * args.fps) / elapsed:.1f} fps composed | {job.status.value}"
    )


if __name__ == "__main__":
    main()
