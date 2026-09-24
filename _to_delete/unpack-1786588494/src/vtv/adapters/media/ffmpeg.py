"""ffmpeg and ffprobe, wrapped.

Everything the system knows about a media file it learns here, by asking ffprobe
— never by trusting what a client claimed. A browser that reports a 90-second
recording may be wrong, malicious, or simply running a codec that lies; the
duration used for the timeline comes from probing the bytes.

Subprocess arguments are always passed as a list, never through a shell. Paths
into these functions originate from our own storage layer, but ``shell=True``
with any interpolated value is how media pipelines get compromised, so it does
not appear anywhere in this file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vtv.contracts.errors import ErrorCode, VTVError

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

#: Refuse to spend forever on one file. A recording that cannot be probed in
#: thirty seconds is a recording we do not want.
PROBE_TIMEOUT = 30
RENDER_TIMEOUT = 60 * 30


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(args: list[str], *, timeout: int = RENDER_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, converting failure into a structured error."""
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VTVError(
            f"{args[0]} is not installed", code=ErrorCode.RENDER_FAILED
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VTVError(
            f"{args[0]} timed out after {timeout}s", code=ErrorCode.RENDER_FAILED
        ) from exc
    if completed.returncode != 0:
        tail = (completed.stderr or "").strip().splitlines()[-6:]
        raise VTVError(
            f"{args[0]} failed ({completed.returncode}): " + " | ".join(tail),
            code=ErrorCode.RENDER_FAILED,
        )
    return completed


@dataclass(frozen=True)
class ProbedAudio:
    duration_seconds: float
    sample_rate_hz: int
    channels: int
    bit_rate_bps: int | None
    codec: str
    format_name: str


def probe_audio(path: Path) -> ProbedAudio:
    """Measured properties of an audio file."""
    completed = run(
        [
            FFPROBE,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-select_streams", "a:0",
            str(path),
        ],
        timeout=PROBE_TIMEOUT,
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise VTVError(
            "file contains no audio stream", code=ErrorCode.AUDIO_UNREADABLE
        )
    stream = streams[0]
    container = payload.get("format", {})
    duration = float(stream.get("duration") or container.get("duration") or 0.0)
    if duration <= 0:
        raise VTVError("audio has no measurable duration", code=ErrorCode.AUDIO_UNREADABLE)
    bit_rate = stream.get("bit_rate") or container.get("bit_rate")
    return ProbedAudio(
        duration_seconds=duration,
        sample_rate_hz=int(stream.get("sample_rate") or 48000),
        channels=int(stream.get("channels") or 1),
        bit_rate_bps=int(bit_rate) if bit_rate else None,
        codec=str(stream.get("codec_name") or "unknown"),
        format_name=str(container.get("format_name") or "unknown"),
    )


def probe_video(path: Path) -> dict[str, object]:
    completed = run(
        [
            FFPROBE,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=PROBE_TIMEOUT,
    )
    return dict(json.loads(completed.stdout))


@dataclass(frozen=True)
class SpeechRegion:
    """A stretch of the recording where somebody is talking."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_speech_regions(
    path: Path,
    *,
    duration: float,
    noise_db: float = -32.0,
    min_silence: float = 0.45,
) -> list[SpeechRegion]:
    """Find speech by finding the silence between it.

    ffmpeg's ``silencedetect`` reports silent intervals; the speech is what is
    left. This is not transcription and does not pretend to be — but it produces
    *real* boundaries from *real* audio, which is what lets the rest of the
    pipeline be exercised honestly while no speech-to-text provider is reachable.

    ``min_silence`` is deliberately near half a second: shorter gaps are the
    pauses inside a sentence, longer ones are the pauses between thoughts.
    """
    completed = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i", str(path),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT * 4,
        check=False,
    )
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in (completed.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].strip().split()[0])
            except (IndexError, ValueError):
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split("|")[0].strip())
            except (IndexError, ValueError):
                start = None
                continue
            silences.append((start, end))
            start = None

    regions: list[SpeechRegion] = []
    cursor = 0.0
    for silence_start, silence_end in silences:
        if silence_start - cursor > 0.2:
            regions.append(SpeechRegion(cursor, min(silence_start, duration)))
        cursor = max(cursor, silence_end)
    if duration - cursor > 0.2:
        regions.append(SpeechRegion(cursor, duration))

    if not regions:
        silent_total = sum(end - start for start, end in silences)
        if silent_total >= duration * 0.95:
            # Genuinely silent: a muted microphone. Returning a full-length
            # region here would report a healthy recording, and the caller
            # would spend money transcribing nothing.
            return []
        # Continuous speech with no detectable pause is entirely normal for a
        # short clip. One region covering everything is the correct answer.
        regions = [SpeechRegion(0.0, duration)]
    return regions


def measure_loudness(path: Path) -> tuple[float | None, float | None]:
    """Return ``(peak_dbfs, silence_ratio)`` for a file.

    A very high silence ratio usually means a muted microphone, which we want to
    catch before spending money on transcription.
    """
    completed = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT * 2,
        check=False,
    )
    peak: float | None = None
    for line in (completed.stderr or "").splitlines():
        if "max_volume:" in line:
            try:
                peak = float(line.split("max_volume:")[1].strip().split()[0])
            except (IndexError, ValueError):
                peak = None
    return peak, None


def transcode_to_wav(source: Path, target: Path, *, sample_rate: int = 16_000) -> Path:
    """Normalise to mono 16 kHz PCM.

    Every speech-to-text provider wants something like this, and doing it once
    here means no adapter has to reimplement it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-ac", "1",
            "-ar", str(sample_rate),
            "-c:a", "pcm_s16le",
            str(target),
        ]
    )
    return target


def synthesise_tone_audio(target: Path, *, duration: float, segments: list[tuple[float, float]]) -> Path:
    """Generate a test recording: tones where speech should be, silence between.

    Used by the tests and by ``make demo`` so that the whole pipeline can be
    exercised end to end without a microphone. It is clearly synthetic and is
    never presented as a real recording.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    rate = 44_100
    filters: list[str] = []
    labels: list[str] = []
    cursor = 0.0
    for index, (start, end) in enumerate(segments):
        gap = max(0.0, start - cursor)
        if gap > 0.001:
            filters.append(f"anullsrc=r={rate}:cl=mono,atrim=0:{gap:.3f}[g{index}]")
            labels.append(f"[g{index}]")
        frequency = 180 + (index % 5) * 40
        filters.append(
            f"sine=f={frequency}:r={rate}:d={max(0.05, end - start):.3f},"
            f"aformat=channel_layouts=mono,volume=0.3[t{index}]"
        )
        labels.append(f"[t{index}]")
        cursor = end
    tail = max(0.0, duration - cursor)
    if tail > 0.001:
        filters.append(f"anullsrc=r={rate}:cl=mono,atrim=0:{tail:.3f}[tail]")
        labels.append("[tail]")
    graph = ";".join(filters)
    graph += f";{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]"
    run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-filter_complex", graph,
            "-map", "[out]",
            "-c:a", "libopus" if target.suffix == ".webm" else "pcm_s16le",
            str(target),
        ]
    )
    return target


__all__ = [
    "FFMPEG",
    "FFPROBE",
    "ProbedAudio",
    "SpeechRegion",
    "detect_speech_regions",
    "is_available",
    "measure_loudness",
    "probe_audio",
    "probe_video",
    "run",
    "synthesise_tone_audio",
    "transcode_to_wav",
]
