"""Rendering in segments: why an encode can be resumed, and parallelised.

## The failure this exists to fix

The renderer composed every frame of a video into one ffmpeg process. That is a
good design — transitions are free, captions are ours, audio cannot drift — and
it has one property that is unacceptable in a product people pay for:

    a forty-minute render, three hours in, at 97%
        ↓
    the power goes out
        ↓
    the worker restarts, reclaims the job
        ↓
    it encodes from frame zero

The expensive *thinking* already survived a crash: the job row is durable, the
narration is reused by digest, and sourced visuals are not re-bought. Only the
encode threw away everything. Three hours of a machine's life, discarded because
the output was one file that either existed or did not.

## The rule

**A frame range that has been encoded is a file on disk whose name says so.**

Not a progress counter, not a row in a table, not a flag someone must remember
to write. The segment is either at `seg-00042.mp4` — in which case it is
finished, because that name is only ever created by `os.replace` from a
`.part` file after ffmpeg exited zero — or it is not there and must be made.

This is the chokepoint. Resumption is not a feature that runs on restart; it is
the ordinary path. Every render asks the same question of every segment, "is
this already done", and a first attempt is simply the case where the answer is
always no. There is no separate resume code to leave untested and discover
broken during the one incident it was written for.

## Why the same split gives parallelism

A segment is a closed range of frames that depends on nothing outside itself:
the composer is a pure function of the timeline and a timestamp, so frame
100 000 can be drawn without having drawn frame 99 999. That makes segments the
natural unit of work for a process pool as well as the natural unit of
checkpointing — one decomposition, two problems, and the second one was the
user's most-felt complaint (a forty-minute video took four times its own length
to render, on one core, while the other cores idled).

Doing this with threads would not have worked: frame composition is PIL and
Python, and the GIL serialises exactly the part that is slow.

## Where the boundaries go

At **clip boundaries**, accumulating until a segment is about
`TARGET_SECONDS` long. Never mid-clip, for two reasons that are both about not
being clever:

* concat's stream copy wants segments an encoder produced whole, and a clip
  boundary is where the picture changes anyway, so the keyframe was going there;
* a segment that starts where a clip starts is one a person can reason about
  when a render goes wrong — "segment 12 is the shot about tariffs".

The arithmetic is done in **frame indices, not seconds**. `round(start * fps)`
per boundary, and each segment runs to the next boundary, so the segments
partition `range(total_frames)` exactly. Deriving each segment's length from its
own duration instead would round independently and lose or gain a frame per
segment — a two-hundred-segment video would end up seconds adrift of its audio,
which is the one failure this renderer's single-pass design was built to make
impossible.

## What invalidates a checkpoint

The scratch directory is named by a **fingerprint of what is being rendered** —
the timeline's clips, captions, style and narration, plus the settings — and not
by the render job's id. A retry of the same work therefore lands on the same
directory and finds its own segments; an edit to the timeline lands somewhere
else and shares nothing.

The fingerprint deliberately over-invalidates. It hashes the whole timeline with
only timestamps removed, so a field nobody thought about here still changes it.
Re-rendering something that did not need re-rendering costs time; reusing a
segment that no longer matches the timeline ships a video with the wrong picture
in it, and the user has no way to know. Those are not symmetrical risks.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: How long a segment should be, in seconds of finished video.
#:
#: The trade is checkpoint granularity against per-segment overhead. Twelve
#: seconds means a crash loses at most twelve seconds of encoding, a
#: forty-minute video splits into roughly two hundred pieces — enough to keep
#: eight cores fed to the last few seconds of the render — and the fixed cost of
#: starting an ffmpeg (about 30ms) stays under half a percent of the work it is
#: given. Segments are snapped outwards to clip boundaries, so a video of very
#: long shots gets fewer, longer segments and that is correct: a thirty-second
#: shot is one indivisible piece of drawing.
TARGET_SECONDS = 12.0

#: Delete abandoned scratch directories older than this.
#:
#: Segments are kept when a render fails — that is the entire point — so
#: something must eventually collect the ones belonging to renders nobody will
#: retry. A day is long enough that a user who comes back the next morning still
#: resumes, and short enough that a busy worker does not fill a disk with the
#: frames of videos that were abandoned last month.
STALE_AFTER_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Segment:
    """A closed range of frames, and the file that proves it was encoded."""

    index: int
    start_frame: int
    end_frame: int

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame

    @property
    def name(self) -> str:
        # Zero-padded so a directory listing and `sorted()` agree, and so the
        # concat list can be built by globbing without re-deriving the order.
        return f"seg-{self.index:05d}.mp4"

    def path(self, scratch: Path) -> Path:
        return scratch / self.name

    def partial(self, scratch: Path) -> Path:
        """Where ffmpeg writes before the segment counts as done.

        A crash mid-encode leaves this, never the real name. Nothing reads it
        and the next attempt overwrites it, so a half-written segment cannot be
        mistaken for a finished one — which is the whole guarantee.
        """
        return scratch / f"seg-{self.index:05d}.part.mp4"

    def done(self, scratch: Path) -> bool:
        path = self.path(scratch)
        return path.exists() and path.stat().st_size > 0


def plan(
    boundaries_seconds: list[float],
    *,
    fps: int,
    total_frames: int,
    target_seconds: float = TARGET_SECONDS,
) -> list[Segment]:
    """Split a video into segments at clip boundaries.

    `boundaries_seconds` is where each clip starts. The first is usually zero;
    anything at or beyond the end of the video is ignored, and the result always
    covers `range(total_frames)` exactly with no gap and no overlap — asserted
    below rather than assumed, because a lost frame here would show up as
    creeping audio desync in an hour-long video and be almost impossible to
    attribute back to this function.
    """
    if total_frames <= 0:
        return []

    cuts = [0]
    for seconds in sorted(set(boundaries_seconds)):
        frame = round(seconds * fps)
        if 0 < frame < total_frames and frame > cuts[-1]:
            cuts.append(frame)
    cuts.append(total_frames)

    minimum = max(1, round(target_seconds * fps))
    segments: list[Segment] = []
    start = 0
    for cut in cuts[1:]:
        if cut - start < minimum and cut != total_frames:
            # Keep accumulating clips until the segment is worth its overhead.
            continue
        segments.append(Segment(len(segments), start, cut))
        start = cut

    if not segments:
        segments = [Segment(0, 0, total_frames)]

    assert segments[0].start_frame == 0
    assert segments[-1].end_frame == total_frames
    assert all(
        a.end_frame == b.start_frame for a, b in zip(segments, segments[1:], strict=False)
    )
    return segments


def fingerprint(timeline: Any, settings: Any) -> str:
    """A name for "this exact video", stable across attempts.

    Two renders share segments if and only if they would draw the same frames.
    Timestamps are stripped because they change on every save and describe when
    a record was written, never what it looks like; everything else is included,
    including fields this module has never heard of.
    """

    def strip(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: strip(item)
                for key, item in value.items()
                if not key.endswith("_at")
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    payload = {
        "timeline": strip(timeline.model_dump(mode="json")),
        "settings": strip(settings.model_dump(mode="json")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def sweep(root: Path, *, older_than_seconds: float = STALE_AFTER_SECONDS) -> int:
    """Delete scratch directories nobody came back for.

    Never raises: this runs at the start of a render and a permission error on
    somebody else's leftovers must not stop a user's video from being made.
    """
    import shutil

    if not root.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:  # pragma: no cover - filesystem race
            continue
    return removed


# -- encoding one segment -------------------------------------------------

#: Loaded once per worker process and reused for every segment it is given.
#:
#: A pool worker is handed only a scratch path and a frame range; the timeline,
#: the settings and the asset manifest are read from `plan.json` on first use.
#: Passing them as arguments instead would pickle a whole timeline — captions,
#: clips and all — once per segment, which for a four-hour video is the same
#: megabytes sent two thousand times.
_LOADED: dict[str, Any] = {}


def _context(scratch: str) -> Any:
    from vtv.adapters.render.ffmpeg_renderer import RenderContext

    cached = _LOADED.get(scratch)
    if cached is None:
        cached = RenderContext.load(Path(scratch))
        _LOADED[scratch] = cached
    return cached


def context(scratch: str) -> Any:
    """The render context for this scratch directory, loaded once per process.

    Public because the router needs it too: deciding where a segment should run
    means asking what is in it, and what is in it is what this knows.
    """
    return _context(scratch)


def forget(scratch: str | Path) -> None:
    """Drop a finished render's cached context, and its painter's device.

    `_LOADED` exists so that two hundred segments share one parsed timeline
    instead of re-reading it two hundred times. Nothing ever removed an entry,
    which was harmless while every painter was pure Python and is not once one
    of them holds a graphics context: three renders in one process left three
    live contexts and their textures, and the fourth failed to allocate.

    Called by the renderer when a render ends, either way. A crash that skips
    it costs one context until the process exits, which is the same as before.
    """
    context = _LOADED.pop(str(scratch), None)
    if context is not None:
        with contextlib.suppress(Exception):
            context.close()


def encode_segment(
    scratch: str, index: int, start_frame: int, end_frame: int, painter: str = "cpu"
) -> str:
    """Draw and encode one frame range. Returns "" on success, else the reason.

    ## Why this returns a string instead of raising

    It runs in a pool worker, so whatever it raises has to survive pickling
    back to the parent. The project's errors carry structured payloads and
    ffmpeg's carry a few hundred bytes of stderr; a failure to *transport* the
    failure would surface as an opaque pool error and hide the actual cause,
    which is the worst possible outcome for the one path that only runs when
    something has already gone wrong. A string always makes it home, and the
    parent raises the real error where there is context to describe it.
    """
    from vtv.adapters.media import ffmpeg

    context = _context(scratch)
    try:
        context.use_painter(painter)
    except Exception as exc:
        # The backend asked for hardware that will not start. Reported rather
        # than quietly falling back here, because the caller decides where a
        # segment runs and a painter that substituted itself would make the
        # render record wrong about which hardware drew what.
        return f"segment {index}: the {painter} painter could not start: {exc}"
    segment = Segment(index, start_frame, end_frame)
    directory = Path(scratch)
    target = segment.partial(directory)

    width, height = context.settings.dimensions
    args = [
        ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", str(context.fps), "-i", "-",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(context.settings.quality.crf),
        "-pix_fmt", "yuv420p",
        # One thread each. Without this every worker's encoder spawns as many
        # threads as there are cores, so eight workers on an eight-core machine
        # ask for sixty-four and the scheduler spends its time context
        # switching instead of drawing. The parallelism is the pool, not x264.
        "-threads", "1",
        str(target),
    ]

    process = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    assert process.stdin is not None
    try:
        for frame_index in range(start_frame, end_frame):
            frame = context.compose(frame_index / context.fps)
            process.stdin.write(frame.tobytes())
    except BrokenPipeError:
        process.kill()
        process.wait()
        return f"segment {index}: ffmpeg closed the pipe"
    except Exception as exc:  # pragma: no cover - defensive
        process.kill()
        process.wait()
        return f"segment {index}: {type(exc).__name__}: {exc}"
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        stderr = b""
        if process.stderr is not None:
            stderr = process.stderr.read()
            process.stderr.close()
        code = process.wait()
        context.release()

    if code != 0 or not target.exists():
        return f"segment {index}: encode failed ({code}): " + (
            stderr.decode("utf-8", "replace").strip()[-300:]
        )

    # The rename is the commit. Until this line the segment does not exist as
    # far as any other process is concerned; after it, it is finished by
    # definition and a restart will skip it.
    os.replace(target, segment.path(directory))
    return ""


__all__ = [
    "STALE_AFTER_SECONDS",
    "TARGET_SECONDS",
    "Segment",
    "encode_segment",
    "fingerprint",
    "plan",
    "sweep",
]
