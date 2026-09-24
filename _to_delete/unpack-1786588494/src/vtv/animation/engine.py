"""Stage 8 — The animation engine.

Turns a validated `AnimationSpec` into pixels. Two entry points:

* `still()` renders one frame — used for storyboard thumbnails, which are
  therefore free and always in sync with what will be rendered.
* `render_clip()` writes a real video file by streaming raw frames into ffmpeg.

Frames are streamed rather than written to disk as PNGs. A five-minute 30fps
video is nine thousand frames; writing and re-reading them costs more time than
drawing them, and fills the disk for no benefit.

Nothing in this module executes anything a model authored. It reads typed
fields off a validated spec and draws them (`docs/VISUAL_DIRECTOR.md`).
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from vtv.adapters.media import ffmpeg
from vtv.animation import primitives
from vtv.animation.canvas import Canvas
from vtv.animation.theme import Theme, ease_in_out
from vtv.contracts.errors import ErrorCode, VTVError
from vtv.contracts.style import AspectRatio, StyleProfile

#: Fraction of a clip spent easing in and out. The animation itself finishes
#: well before the end so the shot has a moment to be read.
LEAD_IN = 0.08
SETTLE_AT = 0.72


@dataclass(frozen=True)
class RenderSize:
    width: int
    height: int

    @classmethod
    def for_aspect(cls, aspect: AspectRatio, *, scale: float = 1.0) -> RenderSize:
        width, height = aspect.dimensions_1080
        # Even dimensions: every mainstream H.264 encoder requires them.
        return cls(int(width * scale) // 2 * 2, int(height * scale) // 2 * 2)


class AnimationEngine:
    """Draws animation specs, deterministically."""

    def __init__(self, style: StyleProfile | None = None) -> None:
        self.style = style or StyleProfile()

    def theme_for(self, size: RenderSize) -> Theme:
        return Theme.from_style(self.style, width=size.width, height=size.height)

    # -- single frames ----------------------------------------------------

    def still(
        self, spec: object, *, size: RenderSize, progress: float = 1.0
    ) -> Image.Image:
        """One frame, fully composed. Used for thumbnails and for tests."""
        theme = self.theme_for(size)
        canvas = Canvas(theme)
        primitives.draw(canvas, spec, progress)
        canvas.vignette(0.22)
        return canvas.to_rgb()

    def frame_at(
        self, spec: object, *, size: RenderSize, t: float, duration: float
    ) -> Image.Image:
        """The frame at time ``t`` seconds into a clip of ``duration`` seconds."""
        theme = self.theme_for(size)
        canvas = Canvas(theme)
        fraction = 0.0 if duration <= 0 else max(0.0, min(1.0, t / duration))
        # Animation completes at SETTLE_AT so the viewer has time to read the
        # finished frame rather than watching it arrive until the cut.
        progress = min(1.0, fraction / SETTLE_AT) if SETTLE_AT > 0 else 1.0
        primitives.draw(canvas, spec, progress)
        canvas.vignette(0.22)

        if fraction < LEAD_IN:
            canvas.fade(1.0 - ease_in_out(fraction / LEAD_IN))
        return canvas.to_rgb()

    # -- clips ------------------------------------------------------------

    def render_clip(
        self,
        spec: object,
        *,
        output: Path,
        size: RenderSize,
        duration: float,
        fps: int = 30,
        crf: int = 20,
    ) -> Path:
        """Render an animation spec to a silent video file.

        Frames are piped to ffmpeg as raw RGB. The pipe is closed and the
        process waited on even when drawing fails, so a broken spec cannot leave
        an orphaned encoder behind.
        """
        if duration <= 0:
            raise VTVError("clip duration must be positive", code=ErrorCode.RENDER_FAILED)
        output.parent.mkdir(parents=True, exist_ok=True)
        frame_count = max(1, round(duration * fps))

        process = subprocess.Popen(
            [
                ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-s", f"{size.width}x{size.height}",
                "-r", str(fps),
                "-i", "-",
                "-an",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                str(output),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None

        try:
            for index in range(frame_count):
                frame = self.frame_at(
                    spec, size=size, t=index / fps, duration=duration
                )
                process.stdin.write(frame.tobytes())
        except BrokenPipeError as exc:
            process.kill()
            raise VTVError(
                "ffmpeg closed the pipe while encoding an animation",
                code=ErrorCode.RENDER_FAILED,
            ) from exc
        finally:
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read().decode("utf-8", "replace")
                process.stderr.close()
            code = process.wait()

        if code != 0:
            raise VTVError(
                f"animation encode failed ({code}): {stderr.strip()[-400:]}",
                code=ErrorCode.RENDER_FAILED,
            )
        return output

    def render_still_file(
        self, spec: object, *, output: Path, size: RenderSize, progress: float = 1.0
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        self.still(spec, size=size, progress=progress).save(output, format="PNG")
        return output


__all__ = ["LEAD_IN", "SETTLE_AT", "AnimationEngine", "RenderSize"]
