"""Stage 11 — Captions.

Cues are built from the transcript's own timings, never estimated from word
counts. That is the whole reason word-level timing is carried all the way from
the speech provider through to here.

The constraints below are accessibility conventions, not preferences: roughly
forty characters a line, at most two lines, on screen long enough to be read.
Captions that flash past faster than reading speed are worse than none, because
they occupy the space where usable captions would have gone.
"""

from __future__ import annotations

from dataclasses import dataclass

from vtv.contracts.base import TimeSpan
from vtv.contracts.timeline import CaptionCue
from vtv.contracts.transcript import Transcript, TranscriptSegment, TranscriptWord

MAX_LINE_CHARS = 42
MAX_LINES = 2
MAX_CUE_CHARS = MAX_LINE_CHARS * MAX_LINES

#: Below this a cue cannot be read, however short the text.
MIN_CUE_SECONDS = 1.0
MAX_CUE_SECONDS = 6.0

#: Comfortable reading speed. Used to check a cue is on screen long enough.
CHARS_PER_SECOND = 17.0


def wrap_caption(text: str, width: int = MAX_LINE_CHARS) -> list[str]:
    """Wrap on word boundaries. Returns **every** line — never truncates.

    This used to end `return lines[:MAX_LINES]`, and that slice was throwing
    away the end of a great many sentences. A caption too long for two lines
    was drawn as its first two lines and the rest simply vanished: the video
    said "Computer Science is the study of how computers work, how software is
    built, and" and then moved on. The narration said the whole sentence. The
    `.srt` and `.vtt` files, which call this too, were cut in the same place —
    so the download a deaf viewer relies on was missing text nobody had been
    told about.

    Fitting is a *layout* decision and belongs to whoever is drawing: the
    renderer pages these lines two at a time across the cue's own span (see
    `caption_pages`), and the subtitle files emit them all, which is what both
    formats are for. Dropping words to make them fit is not a layout decision.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def caption_pages(
    text: str, *, width: int = MAX_LINE_CHARS, max_lines: int = MAX_LINES
) -> list[list[str]]:
    """The wrapped lines, grouped into screenfuls.

    A cue longer than the box holds becomes several pages shown one after
    another across the cue's own duration, rather than one page and silence.
    Always at least one page, so a caller can index page 0 without checking.
    """
    lines = wrap_caption(text, width)
    pages = [lines[at : at + max_lines] for at in range(0, len(lines), max_lines)]
    return pages or [[""]]


@dataclass
class CaptionBuilder:
    """Turns a transcript into readable, correctly-timed cues."""

    max_chars: int = MAX_CUE_CHARS
    min_seconds: float = MIN_CUE_SECONDS
    max_seconds: float = MAX_CUE_SECONDS

    def build(self, transcript: Transcript, *, limit: float | None = None) -> list[CaptionCue]:
        cues: list[CaptionCue] = []
        for segment in transcript.segments:
            cues.extend(self._split(segment))
        return self._tidy(cues, limit)

    def _split(self, segment: TranscriptSegment) -> list[CaptionCue]:
        """Break one segment into cue-sized pieces.

        With word timings the split lands on a real word boundary with a real
        timestamp. Without them the segment is divided proportionally — bounded
        by the segment's own true start and end, so error can never accumulate.
        """
        text = " ".join(segment.text.split())
        if len(text) <= self.max_chars and segment.span.duration <= self.max_seconds:
            return [
                CaptionCue(
                    span=segment.span,
                    text=text,
                    word_spans=[word.span for word in segment.words],
                )
            ]

        if segment.words:
            return self._split_on_words(segment)

        pieces = _chunk_text(text, self.max_chars)
        total = sum(len(piece) for piece in pieces) or 1
        cues: list[CaptionCue] = []
        cursor = segment.span.start
        for index, piece in enumerate(pieces):
            share = (len(piece) / total) * segment.span.duration
            end = segment.span.end if index == len(pieces) - 1 else cursor + share
            if end - cursor < 0.2:
                end = min(segment.span.end, cursor + 0.2)
            if end <= cursor:
                continue
            cues.append(CaptionCue(span=TimeSpan.of(cursor, end), text=piece))
            cursor = end
        return cues

    def _split_on_words(self, segment: TranscriptSegment) -> list[CaptionCue]:
        cues: list[CaptionCue] = []
        batch: list[TranscriptWord] = []

        def flush() -> None:
            if not batch:
                return
            start = batch[0].span.start
            end = batch[-1].span.end
            if end - start < 0.05:
                end = start + 0.05
            cues.append(
                CaptionCue(
                    span=TimeSpan.of(start, end),
                    text=" ".join(word.text for word in batch),
                    word_spans=[word.span for word in batch],
                )
            )
            batch.clear()

        for word in segment.words:
            candidate_length = sum(len(w.text) + 1 for w in batch) + len(word.text)
            candidate_duration = word.span.end - (batch[0].span.start if batch else word.span.start)
            if batch and (
                candidate_length > self.max_chars or candidate_duration > self.max_seconds
            ):
                flush()
            batch.append(word)
        flush()
        return cues

    def _tidy(self, cues: list[CaptionCue], limit: float | None) -> list[CaptionCue]:
        """Enforce ordering, minimum on-screen time and the recording's end.

        A cue is allowed to outlive its audio slightly — holding a caption a
        moment longer than the words is standard practice and reads better —
        but never past the next cue and never past the end of the video.
        """
        ordered = sorted(cues, key=lambda cue: cue.span.start)
        tidied: list[CaptionCue] = []
        for index, cue in enumerate(ordered):
            start = cue.span.start
            if tidied and start < tidied[-1].span.end:
                start = tidied[-1].span.end
            end = max(cue.span.end, start + self.min_seconds)
            following = ordered[index + 1].span.start if index + 1 < len(ordered) else None
            if following is not None:
                end = min(end, max(start + 0.2, following))
            if limit is not None:
                end = min(end, limit)
            if end - start < 0.15:
                continue
            words = [
                span
                for span in cue.word_spans
                if span.start >= start - 1e-6 and span.end <= end + 1e-6
            ]
            tidied.append(
                CaptionCue(span=TimeSpan.of(start, end), text=cue.text, word_spans=words)
            )
        return tidied


def _chunk_text(text: str, limit: int) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks or [text]


# ---------------------------------------------------------------------------
# Sidecar formats
# ---------------------------------------------------------------------------

def _timestamp(seconds: float, *, comma: bool) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    milliseconds = round((seconds - int(seconds)) * 1000)
    if milliseconds == 1000:  # rounding can tip a whole second
        whole += 1
        milliseconds = 0
    separator = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{whole:02d}{separator}{milliseconds:03d}"


def to_srt(cues: list[CaptionCue]) -> str:
    """SubRip. Universally supported, including by every video platform."""
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        lines = wrap_caption(cue.text)
        blocks.append(
            f"{index}\n"
            f"{_timestamp(cue.span.start, comma=True)} --> "
            f"{_timestamp(cue.span.end, comma=True)}\n"
            + "\n".join(lines)
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(cues: list[CaptionCue]) -> str:
    """WebVTT. What a browser `<track>` element wants."""
    blocks = ["WEBVTT", ""]
    for cue in cues:
        lines = wrap_caption(cue.text)
        blocks.append(
            f"{_timestamp(cue.span.start, comma=False)} --> "
            f"{_timestamp(cue.span.end, comma=False)}\n" + "\n".join(lines)
        )
        blocks.append("")
    return "\n".join(blocks)


__all__ = [
    "CHARS_PER_SECOND",
    "MAX_CUE_CHARS",
    "MAX_CUE_SECONDS",
    "MAX_LINES",
    "MAX_LINE_CHARS",
    "MIN_CUE_SECONDS",
    "CaptionBuilder",
    "caption_pages",
    "to_srt",
    "to_vtt",
    "wrap_caption",
]
