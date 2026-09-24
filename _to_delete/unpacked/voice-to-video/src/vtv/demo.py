"""``make demo`` — the golden path, end to end, with no microphone.

Synthesises a recording whose pauses match a script, runs it through every
stage, and writes a real MP4. It exists so that anyone can verify the claim
"speech goes in, a coherent video comes out" on a fresh checkout in under a
minute, without a browser, a credential or a network connection.

The audio is tones, not speech, and the script is supplied rather than
recognised. Both facts are printed. This is a demonstration of the *pipeline*,
not of transcription.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from vtv.adapters.media import ffmpeg
from vtv.config import Settings
from vtv.contracts.recording import Recording
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.style import StyleProfile, VisualStyle
from vtv.wiring import build

SCRIPT = (
    "Let me tell you about the single most important invention of the twentieth century. "
    "The transistor was invented in 1947 at Bell Labs. "
    "It was smaller and far more efficient than the vacuum tubes that came before it. "
    "A vacuum tube was the size of a light bulb, but a transistor could be smaller than a grain of rice. "
    "Within twenty years transistors had replaced vacuum tubes almost everywhere. "
    "Everything you are holding right now is built out of them."
)

#: Where the speaker pauses. These become the transcript's real segment
#: boundaries, because they are detected from the audio rather than assumed.
PAUSES = [(0.3, 5.5), (6.2, 11.4), (12.1, 18.0), (18.8, 25.0), (25.8, 32.0), (32.8, 39.5)]
DURATION = 40.0


async def run(output_dir: Path, *, script: str = SCRIPT, quality: str = "preview") -> int:
    if not ffmpeg.is_available():
        print("ffmpeg and ffprobe are required for the demo", file=sys.stderr)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "recording.wav"
    print(f"synthesising a {DURATION:.0f}s recording with {len(PAUSES)} spoken stretches")
    ffmpeg.synthesise_tone_audio(audio_path, duration=DURATION, segments=PAUSES)

    assembly = build(
        Settings(
            storage_root=output_dir / "storage",
            asset_search_endpoint="",  # offline
        ),
        workdir=output_dir / "work",
    )
    print("capabilities:", assembly.capabilities.as_dict())

    original = assembly.pipeline.capture.capture

    async def capture(**kwargs: Any) -> Recording:
        recording: Recording = await original(**kwargs)
        assembly.scripts[recording.audio.key] = script
        return recording

    assembly.pipeline.capture.capture = capture  # type: ignore[method-assign]

    result = await assembly.pipeline.run(
        audio=audio_path.read_bytes(),
        style=StyleProfile(style=VisualStyle.DOCUMENTARY),
        settings=RenderSettings(quality=RenderQuality(quality), frame_rate=24),
    )

    if not result.succeeded or result.render_job is None or result.timeline is None:
        print("the pipeline did not produce a video", file=sys.stderr)
        return 1

    assert result.understanding is not None and result.scene_graph is not None
    assert result.visual_plan is not None and result.render_job.output is not None

    print()
    print(f"  topic            {result.understanding.topic}")
    print(
        f"  structure        {len(result.transcript.segments)} segments"  # type: ignore[union-attr]
        f" -> {len(result.understanding.units)} units"
        f" -> {len(result.scene_graph.scenes)} scenes"
    )
    print(f"  visuals          {result.visual_plan.strategy_mix()}")
    print(f"  captions         {len(result.timeline.captions)} cues")
    print(f"  placeholders     {result.timeline.placeholder_count}")
    print(f"  uncovered        {len(result.timeline.coverage_gaps())} gaps")
    print(f"  cost             ${result.ledger.total_usd:.4f}")  # type: ignore[union-attr]
    print(f"  render time      {result.render_job.render_seconds:.1f}s")

    path = assembly.storage.path_for(result.render_job.output)
    final = output_dir / "demo.mp4"
    final.write_bytes(path.read_bytes())
    print()
    print(f"video: {final}  ({final.stat().st_size / 1_000_000:.1f} MB)")
    print(
        "note: the audio is synthesised tones and the words came from a supplied "
        "script, not from speech recognition. Everything after transcription is real."
    )
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("./var/demo"))
    parser.add_argument("--quality", default="preview", choices=["preview", "standard", "high"])
    parser.add_argument("--script", type=Path, help="A text file to narrate instead.")
    args = parser.parse_args(argv)
    script = args.script.read_text(encoding="utf-8") if args.script else SCRIPT
    return asyncio.run(run(args.out, script=script, quality=args.quality))


if __name__ == "__main__":
    raise SystemExit(cli())
