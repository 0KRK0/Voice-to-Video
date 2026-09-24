"""Stage 10 — Rendering a timeline to MP4 with ffmpeg.

STATUS: **REAL IMPLEMENTATION — RUNS AND PRODUCES A PLAYABLE FILE.**

The design is a single pass. For every frame time the composer asks the timeline
what should be on screen, draws it, and pipes the raw pixels into one ffmpeg
process that muxes the narration and encodes. There is no intermediate segment
per clip and no concat step.

That choice buys three things:

* **transitions are free** — a dissolve is two frames blended, not a filter graph;
* **captions and attributions are ours** — drawn with the same type system as
  everything else, rather than handed to a subtitle burner whose fonts and
  metrics we do not control;
* **it cannot desynchronise** — audio is muxed once, and every frame's timestamp
  is derived from the narration clock, so drift has nowhere to enter.

The renderer makes no creative decisions. Everything it draws was decided
upstream and is present in the `Timeline` (Rule 13).
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from vtv.adapters.media import ffmpeg
from vtv.animation.canvas import Canvas
from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.animation.theme import Theme, ease_in_out, ease_out_cubic, with_alpha
from vtv.contracts.base import ObjectRef, RetentionClass, TimeSpan
from vtv.contracts.errors import ErrorCode, RenderFailed, Status, VTVError
from vtv.contracts.render import RenderJob, RenderSettings
from vtv.contracts.timeline import (
    AssetClipSource,
    CaptionCue,
    FitPolicy,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    Timeline,
    TransitionKind,
    VisualClip,
)
from vtv.contracts.visual_language import CameraMotion
from vtv.observability.events import EventName, EventSink, Timer
from vtv.pipeline.captions import to_srt, to_vtt, wrap_caption

#: Ken Burns strength: how far the frame travels over a clip. Subtle on purpose
#: — anything more reads as a screensaver.
CAMERA_TRAVEL = 0.10


@dataclass
class _ResolvedClip:
    clip: VisualClip
    #: A still, already loaded, for image sources.
    still: Image.Image | None = None
    #: Directory of extracted frames, for video sources.
    frames: list[Path] | None = None
    frame_rate: float = 30.0


class FfmpegRenderer:
    """Composes and encodes a timeline. Implements the `Renderer` port."""

    def __init__(
        self,
        *,
        storage: object,
        events: EventSink,
        workdir: Path | None = None,
    ) -> None:
        self.storage = storage
        self.events = events
        self.workdir = workdir
        self._progress: dict[str, RenderJob] = {}

    # -- Renderer port ----------------------------------------------------

    async def render(
        self, *, timeline: Timeline, settings: RenderSettings
    ) -> RenderJob:
        job = RenderJob(
            project_id=timeline.project_id,
            timeline_id=timeline.timeline_id,
            settings=settings,
            status=Status.PROCESSING,
        )
        self._progress[job.render_job_id] = job

        renderable, problems = timeline.is_renderable()
        if not renderable:
            # Coverage gaps and missing captions are caught here rather than
            # discovered in the finished file.
            raise RenderFailed("timeline is not renderable: " + "; ".join(problems))

        self.events.emit(
            EventName.RENDER_STARTED,
            project_id=timeline.project_id,
            data={
                "render_job_id": job.render_job_id,
                "quality": settings.quality.value,
                "clips": len(timeline.clips),
                "duration_seconds": round(timeline.duration_seconds, 3),
            },
        )
        timer = Timer()

        root = Path(self.workdir) if self.workdir else Path(tempfile.mkdtemp(prefix="vtv-render-"))
        root.mkdir(parents=True, exist_ok=True)
        scratch = root / job.render_job_id
        scratch.mkdir(parents=True, exist_ok=True)

        try:
            output = await self._encode(timeline, settings, scratch, job)
            captions_ref = await self._write_captions(timeline, scratch)
        except VTVError:
            job.status = Status.FAILED
            self.events.emit(
                EventName.RENDER_FAILED,
                project_id=timeline.project_id,
                data={"render_job_id": job.render_job_id},
            )
            raise
        finally:
            if self.workdir is None:
                shutil.rmtree(scratch, ignore_errors=True)

        job.output = output
        job.captions_output = captions_ref
        job.duration_seconds = timeline.duration_seconds
        job.render_seconds = timer.elapsed_ms / 1000.0
        job.progress = 1.0
        job.status = Status.READY

        self.events.emit(
            EventName.RENDER_COMPLETED,
            project_id=timeline.project_id,
            duration_ms=timer.elapsed_ms,
            data={
                "render_job_id": job.render_job_id,
                "bytes": output.size_bytes,
                "realtime_ratio": round(
                    (timer.elapsed_ms / 1000.0) / max(0.001, timeline.duration_seconds), 2
                ),
            },
        )
        return job

    async def render_progress(self, render_job_id: str) -> AsyncIterator[RenderJob]:
        async def iterator() -> AsyncIterator[RenderJob]:
            job = self._progress.get(render_job_id)
            if job is not None:
                yield job

        return iterator()

    async def cancel(self, render_job_id: str) -> None:
        job = self._progress.get(render_job_id)
        if job is not None and not job.status.is_terminal:
            job.status = Status.FAILED

    # -- encoding ---------------------------------------------------------

    async def _encode(
        self,
        timeline: Timeline,
        settings: RenderSettings,
        scratch: Path,
        job: RenderJob,
    ) -> ObjectRef:
        width, height = settings.dimensions
        size = RenderSize(width, height)
        theme = Theme.from_style(timeline.style, width=width, height=height)
        engine = AnimationEngine(timeline.style)

        resolved = [await self._resolve(clip, scratch, settings.frame_rate) for clip in timeline.clips]
        audio_path = await self._narration_file(timeline, scratch)

        fps = settings.frame_rate
        total_frames = max(1, round(timeline.duration_seconds * fps))
        output_path = scratch / "render.mp4"

        args = [
            ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        ]
        if audio_path is not None:
            args += ["-i", str(audio_path)]
        args += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", str(settings.quality.crf),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ]
        if audio_path is not None:
            args += ["-c:a", "aac", "-b:a", f"{settings.audio_bitrate_kbps}k", "-shortest"]
        args.append(str(output_path))

        process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        assert process.stdin is not None

        try:
            for index in range(total_frames):
                t = index / fps
                frame = self._compose_frame(
                    timeline, resolved, theme, engine, size, t, settings
                )
                process.stdin.write(frame.tobytes())
                if index % max(1, total_frames // 20) == 0:
                    job.progress = round(index / total_frames, 3)
                    self.events.emit(
                        EventName.RENDER_PROGRESS,
                        project_id=timeline.project_id,
                        data={"render_job_id": job.render_job_id, "progress": job.progress},
                    )
        except BrokenPipeError as exc:
            process.kill()
            raise RenderFailed("ffmpeg closed the pipe during encoding") from exc
        finally:
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read().decode("utf-8", "replace")
                process.stderr.close()
            code = process.wait()

        if code != 0 or not output_path.exists():
            raise RenderFailed(f"encode failed ({code}): {stderr.strip()[-400:]}")

        stored: ObjectRef = await self.storage.put_file(  # type: ignore[attr-defined]
            key=f"projects/{timeline.project_id}/renders/{job.render_job_id}.mp4",
            source=output_path,
            content_type="video/mp4",
            retention=RetentionClass.EPHEMERAL,
        )
        return stored

    # -- frame composition ------------------------------------------------

    def _compose_frame(
        self,
        timeline: Timeline,
        resolved: list[_ResolvedClip],
        theme: Theme,
        engine: AnimationEngine,
        size: RenderSize,
        t: float,
        settings: RenderSettings,
    ) -> Image.Image:
        active = self._active(resolved, t)
        if active is None:
            base = Image.new("RGB", (size.width, size.height), theme.background[:3])
        else:
            base = self._draw_clip(active, theme, engine, size, t)

            # A dissolve is the previous clip's final frame blended under the
            # incoming one. Cheap, exact, and impossible to desynchronise.
            transition = active.clip.transition_in
            if transition.kind is not TransitionKind.CUT and transition.duration_seconds > 0:
                since = t - active.clip.span.start
                if 0 <= since < transition.duration_seconds:
                    previous = self._previous(resolved, active)
                    if previous is not None:
                        under = self._draw_clip(
                            previous, theme, engine, size, previous.clip.span.end - 0.001
                        )
                        base = Image.blend(
                            under, base, ease_in_out(since / transition.duration_seconds)
                        )

        canvas = Canvas(theme)
        canvas.image = base.convert("RGBA")
        canvas.draw = canvas.draw.__class__(canvas.image, "RGBA")

        if active is not None:
            source = active.clip.source
            if isinstance(source, AssetClipSource):
                if source.attribution and settings.include_attributions:
                    self._draw_attribution(canvas, source.attribution)
                if source.illustrative_label:
                    # Generated imagery of real subjects is labelled on screen.
                    self._draw_badge(canvas, "Illustrative")

        if settings.burn_in_captions and timeline.style.captions_enabled:
            cue = self._caption_at(timeline.captions, t)
            if cue is not None:
                self._draw_caption(canvas, cue, t)

        return canvas.to_rgb()

    def _draw_clip(
        self,
        resolved: _ResolvedClip,
        theme: Theme,
        engine: AnimationEngine,
        size: RenderSize,
        t: float,
    ) -> Image.Image:
        clip = resolved.clip
        local = max(0.0, min(clip.span.duration, t - clip.span.start))
        source = clip.source

        if isinstance(source, ProgrammaticClipSource):
            return engine.frame_at(
                source.spec, size=size, t=local, duration=clip.span.duration
            )

        if isinstance(source, PlaceholderClipSource):
            return self._draw_placeholder(theme, source.message)

        if resolved.still is not None:
            return self._draw_still(resolved.still, theme, clip, local)

        if resolved.frames:
            index = self._frame_index(resolved, local)
            with Image.open(resolved.frames[index]) as frame:
                canvas = Canvas(theme)
                canvas.paste_fitted(frame, (0, 0, theme.width, theme.height), cover=True)
                return canvas.to_rgb()

        return self._draw_placeholder(theme, "Visual unavailable")

    @staticmethod
    def _frame_index(resolved: _ResolvedClip, local: float) -> int:
        """Which extracted frame to show, honouring the clip's fit policy."""
        count = len(resolved.frames or [])
        if count == 0:
            return 0
        raw = int(local * resolved.frame_rate)
        fit = resolved.clip.fit
        if fit is FitPolicy.LOOP:
            return raw % count
        if fit is FitPolicy.SPEED_RAMP:
            fraction = local / max(0.001, resolved.clip.span.duration)
            return min(count - 1, int(fraction * count))
        # TRIM and HOLD_LAST both stop at the final frame; TRIM simply never
        # reaches it because the media is longer than the slot.
        return min(raw, count - 1)

    def _draw_still(
        self, still: Image.Image, theme: Theme, clip: VisualClip, local: float
    ) -> Image.Image:
        """A photograph with camera motion. A static image held for eight
        seconds reads as a broken video, so every still moves."""
        fraction = local / max(0.001, clip.span.duration)
        eased = ease_out_cubic(fraction) if clip.camera_motion is CameraMotion.KEN_BURNS else fraction
        travel = CAMERA_TRAVEL

        zoom = 1.0 + travel
        offset_x = offset_y = 0.0
        motion = clip.camera_motion
        if motion in {CameraMotion.ZOOM_IN, CameraMotion.KEN_BURNS}:
            zoom = 1.0 + travel * eased
        elif motion is CameraMotion.ZOOM_OUT:
            zoom = 1.0 + travel * (1 - eased)
        elif motion in {CameraMotion.PAN_LEFT, CameraMotion.PAN_RIGHT}:
            offset_x = travel * (eased if motion is CameraMotion.PAN_RIGHT else -eased)
        elif motion in {CameraMotion.PAN_UP, CameraMotion.PAN_DOWN}:
            offset_y = travel * (eased if motion is CameraMotion.PAN_DOWN else -eased)
        elif motion is CameraMotion.NONE:
            zoom = 1.0

        if motion is CameraMotion.KEN_BURNS:
            offset_x = travel * 0.35 * eased

        target_w = int(theme.width * zoom)
        target_h = int(theme.height * zoom)
        canvas = Canvas(theme)
        left = int((theme.width - target_w) / 2 + offset_x * theme.width)
        top = int((theme.height - target_h) / 2 + offset_y * theme.height)
        canvas.paste_fitted(still, (left, top, left + target_w, top + target_h), cover=True)
        return canvas.to_rgb()

    @staticmethod
    def _draw_placeholder(theme: Theme, message: str) -> Image.Image:
        canvas = Canvas(theme)
        font = theme.font("title")
        block = canvas.wrap(message, font, int(theme.safe_width * 0.7))
        canvas.rounded_rect(
            (
                theme.width / 2 - block.width / 2 - theme.scale(0.05),
                theme.height / 2 - block.height / 2 - theme.scale(0.04),
                theme.width / 2 + block.width / 2 + theme.scale(0.05),
                theme.height / 2 + block.height / 2 + theme.scale(0.04),
            ),
            theme.scale(0.02),
            outline=with_alpha(theme.muted, 0.6),
            width=2,
        )
        canvas.text_block(
            block,
            font,
            (int(theme.width / 2 - block.width / 2), int(theme.height / 2 - block.height / 2)),
            theme.muted,
            align="center",
        )
        return canvas.to_rgb()

    # -- overlays ---------------------------------------------------------

    @staticmethod
    def _caption_at(cues: list[CaptionCue], t: float) -> CaptionCue | None:
        for cue in cues:
            if cue.span.start <= t < cue.span.end:
                return cue
        return None

    def _draw_caption(self, canvas: Canvas, cue: CaptionCue, t: float) -> None:
        theme = canvas.theme
        font = theme.font("body")
        lines = wrap_caption(cue.text)
        widths = [canvas.measure(line, font)[0] for line in lines]
        ascent, descent = font.getmetrics()
        line_height = int((ascent + descent) * 1.16)
        pad_x, pad_y = theme.scale(0.022), theme.scale(0.014)
        box_w = max(widths) + pad_x * 2
        box_h = line_height * len(lines) + pad_y * 2
        x0 = (theme.width - box_w) / 2
        y0 = theme.height - theme.margin * 0.55 - box_h

        canvas.rounded_rect(
            (x0, y0, x0 + box_w, y0 + box_h),
            theme.scale(0.012),
            fill=with_alpha((0, 0, 0, 255), 0.55),
        )
        y = y0 + pad_y
        for line, width in zip(lines, widths, strict=True):
            canvas.text(
                ((theme.width - width) / 2, y), line, font, theme.foreground
            )
            y += line_height

    def _draw_attribution(self, canvas: Canvas, attribution: str) -> None:
        theme = canvas.theme
        font = theme.font("micro")
        text = attribution[:110]
        width = canvas.measure(text, font)[0]
        pad = theme.scale(0.01)
        x0 = theme.margin * 0.45
        y0 = theme.margin * 0.45
        canvas.rounded_rect(
            (x0, y0, x0 + width + pad * 2, y0 + theme.scale(0.032)),
            theme.scale(0.006),
            fill=with_alpha((0, 0, 0, 255), 0.45),
        )
        canvas.text(
            (x0 + pad, y0 + theme.scale(0.016)),
            text,
            font,
            with_alpha(theme.foreground, 0.85),
            anchor="lm",
        )

    def _draw_badge(self, canvas: Canvas, label: str) -> None:
        theme = canvas.theme
        font = theme.font("micro")
        width = canvas.measure(label, font)[0]
        pad = theme.scale(0.012)
        x1 = theme.width - theme.margin * 0.45
        y0 = theme.margin * 0.45
        canvas.rounded_rect(
            (x1 - width - pad * 2, y0, x1, y0 + theme.scale(0.032)),
            theme.scale(0.016),
            fill=with_alpha(theme.secondary, 0.9),
        )
        canvas.text(
            (x1 - width - pad, y0 + theme.scale(0.016)),
            label,
            font,
            (10, 10, 12, 255),
            anchor="lm",
        )

    # -- resolution -------------------------------------------------------

    @staticmethod
    def _active(resolved: list[_ResolvedClip], t: float) -> _ResolvedClip | None:
        for item in resolved:
            if item.clip.span.start <= t < item.clip.span.end:
                return item
        return resolved[-1] if resolved and t >= resolved[-1].clip.span.end else None

    @staticmethod
    def _previous(
        resolved: list[_ResolvedClip], current: _ResolvedClip
    ) -> _ResolvedClip | None:
        index = resolved.index(current)
        return resolved[index - 1] if index > 0 else None

    async def _resolve(
        self, clip: VisualClip, scratch: Path, fps: int
    ) -> _ResolvedClip:
        """Load whatever the clip needs before the frame loop starts.

        Doing this up front means the encode loop never blocks on I/O, which is
        what keeps the pipe fed and the encoder busy.
        """
        source = clip.source
        if not isinstance(source, AssetClipSource):
            return _ResolvedClip(clip=clip)

        data = await self.storage.get(source.object)  # type: ignore[attr-defined]
        content_type = source.object.content_type

        if content_type.startswith("image/"):
            import io

            with Image.open(io.BytesIO(data)) as opened:
                return _ResolvedClip(clip=clip, still=opened.convert("RGB").copy())

        if content_type.startswith("video/"):
            directory = scratch / f"frames-{clip.clip_id}"
            directory.mkdir(parents=True, exist_ok=True)
            source_file = directory / "source.mp4"
            source_file.write_bytes(data)
            ffmpeg.run(
                [
                    ffmpeg.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(source_file),
                    "-vf", f"fps={fps}",
                    str(directory / "frame-%05d.png"),
                ]
            )
            frames = sorted(directory.glob("frame-*.png"))
            if not frames:
                raise RenderFailed(f"no frames extracted from clip {clip.clip_id}")
            return _ResolvedClip(clip=clip, frames=frames, frame_rate=float(fps))

        raise RenderFailed(
            f"unsupported asset content type {content_type}",
        )

    async def _narration_file(self, timeline: Timeline, scratch: Path) -> Path | None:
        try:
            data = await self.storage.get(timeline.narration.audio)  # type: ignore[attr-defined]
        except VTVError:
            # A render with no audio is still a render. The user gets silent
            # visuals and a clear event rather than nothing at all.
            self.events.emit(
                EventName.RENDER_PROGRESS,
                project_id=timeline.project_id,
                data={"warning": "narration audio unavailable; rendering silent"},
            )
            return None
        path = scratch / "narration.bin"
        path.write_bytes(data)
        return path

    async def _write_captions(self, timeline: Timeline, scratch: Path) -> ObjectRef | None:
        """Always write a sidecar, whatever the burn-in setting.

        Accessibility must not depend on a render flag.
        """
        if not timeline.captions:
            return None
        srt = scratch / "captions.srt"
        srt.write_text(to_srt(timeline.captions), encoding="utf-8")
        vtt = scratch / "captions.vtt"
        vtt.write_text(to_vtt(timeline.captions), encoding="utf-8")
        captions: ObjectRef = await self.storage.put_file(  # type: ignore[attr-defined]
            key=f"projects/{timeline.project_id}/renders/{timeline.timeline_id}.vtt",
            source=vtt,
            content_type="text/vtt",
            retention=RetentionClass.PROJECT,
        )
        return captions


def clip_span_seconds(clip: VisualClip) -> TimeSpan:
    return clip.span


def ensure_ffmpeg() -> None:
    if not ffmpeg.is_available():
        raise VTVError(
            "ffmpeg and ffprobe are required for rendering",
            code=ErrorCode.RENDER_FAILED,
        )


__all__ = ["CAMERA_TRAVEL", "FfmpegRenderer", "ensure_ffmpeg"]
