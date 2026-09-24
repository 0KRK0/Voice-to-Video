"""The script service — where all four ways in become one thing.

A platform that has a voice pipeline and a script pipeline and a document
pipeline has three products that will drift apart. This module is the
convergence point:

```
voice     → transcript ─┐
typed     ───────────────┼→  Script  →  ScriptBlocks  →  VisualUnits  →  Timeline
document  → knowledge ──┘
```

Everything downstream of `Script` is shared. That is the architectural
requirement, and the reason it is a *service* producing a *document* rather than
a branch inside the orchestrator: a branch is where the three paths would start
to differ, one small special case at a time.

## What this will not do

**It will not rewrite the user's words.** In script mode the supplied text is
the narration, exactly as given. `from_text` splits it into blocks and estimates
timing; it does not improve, condense, correct or re-order. Enhancement exists —
see `vtv.pipeline.revision` — and it produces a *proposal* the user accepts.

The distinction matters more than it sounds. A system that quietly improves your
grammar has decided it writes better than you do, and the first time it changes
a technical term into something plausible-but-wrong, it has published that under
your name.

## Blocks, paragraphs and sentences

Splitting is on sentences, but paragraph boundaries are preserved as a hint:
a blank line in the source is a signal from the author that a new idea starts,
and the grouping step downstream uses it. Sentence-level blocks with paragraph
awareness gives the user a line they can point at and the director a hint about
where the cuts belong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from vtv.contracts.base import IdPrefix, TimeSpan, new_id
from vtv.contracts.errors import ValidationFailed
from vtv.contracts.script import (
    BlockStatus,
    Script,
    ScriptBlock,
    ScriptOrigin,
    ScriptVersion,
    estimate_seconds,
)
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.observability.events import EventName, EventSink

#: A blank line, or two. The author's own paragraph break.
_PARAGRAPH = re.compile(r"\n\s*\n+")

#: Titles and abbreviations whose full stop does not end a sentence. A closed
#: list rather than a cleverer heuristic: the failure mode of a heuristic here
#: is a line the user does not recognise as theirs, and "Dr." is not a sentence
#: is a fact rather than a guess.
_ABBREVIATIONS = frozenset(
    ["mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "rev", "hon", "gen", "col", "lt", "sgt", "capt", "vs", "etc", "eg", "ie", "cf", "approx", "no", "fig", "figs", "vol", "vols", "ch", "chs", "pp", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"]
)

#: Sentence-ish. Deliberately conservative: over-splitting produces a line the
#: user cannot recognise as theirs, which is worse than a slightly long block.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")

#: The word immediately before a candidate split point.
_TRAILING_WORD = re.compile(r"([A-Za-z]+)\.\s*$")

#: Beyond this a "sentence" is almost certainly an unpunctuated paragraph, and
#: leaving it whole makes one visual carry a minute of narration.
MAX_BLOCK_CHARS = 400

#: Below this, a "sentence" is a fragment — an initial, a stray "Mr." — and it
#: belongs with its neighbour rather than as a line of its own.
MIN_BLOCK_CHARS = 3


def split_blocks(text: str) -> list[tuple[str, bool]]:
    """Split text into `(sentence, starts_paragraph)` pairs.

    The paragraph flag is the author's own signal about where an idea begins.
    Carrying it through is most of what makes grouping produce cuts a human
    would have made.
    """
    out: list[tuple[str, bool]] = []
    for paragraph in _PARAGRAPH.split(text.strip()):
        cleaned = paragraph.strip()
        if not cleaned:
            continue
        sentences = _split_sentences(cleaned)
        merged = _merge_fragments(sentences)
        for index, sentence in enumerate(merged):
            for piece in _wrap(sentence):
                out.append((piece, index == 0))
    return out


def _split_sentences(text: str) -> list[str]:
    """Split on sentence ends, rejoining where the full stop was an abbreviation.

    The regex alone cuts "Dr. Babbage designed engines." into two, because a
    capital letter after a full stop is the only signal it has. Rejoining on a
    known abbreviation is the smallest correct fix; a statistical splitter would
    be more accurate on average and less predictable at the moment a user asks
    why their line was cut in half.
    """
    parts = [part.strip() for part in _SENTENCE.split(text.strip()) if part.strip()]
    if len(parts) < 2:
        return parts

    joined: list[str] = [parts[0]]
    for part in parts[1:]:
        trailing = _TRAILING_WORD.search(joined[-1])
        if trailing and trailing.group(1).lower() in _ABBREVIATIONS:
            joined[-1] = f"{joined[-1]} {part}"
        else:
            joined.append(part)
    return joined


def _merge_fragments(sentences: list[str]) -> list[str]:
    """Fold too-short pieces into their neighbour.

    "Dr." and "1947." split as sentences and are not. A minimum length is a
    blunt rule and it is the one that misfires least — a mis-merged pair reads
    as one slightly long line, while a mis-split one reads as a bug.
    """
    merged: list[str] = []
    for sentence in sentences:
        too_short = len(sentence) < MIN_BLOCK_CHARS
        previous_too_short = bool(merged) and len(merged[-1]) < MIN_BLOCK_CHARS
        if merged and (too_short or previous_too_short):
            merged[-1] = f"{merged[-1]} {sentence}"
        else:
            merged.append(sentence)
    return merged


def _wrap(sentence: str) -> list[str]:
    """Break an over-long sentence on clause boundaries, then on words.

    Only reached for text with no sentence punctuation at all — transcripts of
    fast speech, mostly. Splitting on commas first keeps the pieces readable;
    splitting on words is the last resort and produces something ugly, which is
    correct: it should look like what it is.
    """
    if len(sentence) <= MAX_BLOCK_CHARS:
        return [sentence]

    pieces: list[str] = []
    current = ""
    for clause in re.split(r"(?<=[,;:])\s+", sentence):
        candidate = f"{current} {clause}".strip()
        if len(candidate) > MAX_BLOCK_CHARS and current:
            pieces.append(current)
            current = clause
        else:
            current = candidate
    if current:
        pieces.append(current)

    out: list[str] = []
    for piece in pieces:
        while len(piece) > MAX_BLOCK_CHARS:
            cut = piece.rfind(" ", 0, MAX_BLOCK_CHARS) or MAX_BLOCK_CHARS
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return out


#: Paired Markdown emphasis. Only pairs, and only around non-space text, so a
#: lone asterisk in "2 * 3" or an underscore in a file name is left alone.
_EMPHASIS = re.compile(
    r"(?<!\w)(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)", re.DOTALL
)


def strip_emphasis(text: str) -> str:
    """Remove Markdown emphasis markers, keeping every word.

    People paste from somewhere. A script containing `**computation**` was
    carried through verbatim, which meant the asterisks were drawn into the
    video's captions, shown in the script panel, and — worse — handed to
    speech synthesis, which reads them aloud. The video said "asterisk
    asterisk computation asterisk asterisk".

    This is not the rewriting the product promises never to do. Nothing is
    rephrased, reordered or removed except the markers themselves: `**x**`
    becomes `x`. The words are the user's; the asterisks were a formatting
    convention from wherever the text was written, and this medium has no way
    to honour them.

    Deliberately conservative. Only *paired* markers hugging non-space text
    match, so `2 * 3`, `a_b_c` and a stray asterisk survive untouched — a
    stripper that guessed would eventually eat a real character out of
    somebody's sentence.
    """
    previous = None
    current = text
    # Repeated because emphasis nests: `**bold _and_ italic**` needs two passes.
    while current != previous:
        previous = current
        current = _EMPHASIS.sub(r"\2", current)
    return current



@dataclass
class ScriptService:
    """Builds and maintains the script document.

    Deliberately has no provider and no storage: constructing a script is pure
    text work, and keeping it that way means the whole of script mode up to the
    first visual costs nothing and cannot fail for an external reason.
    """

    events: EventSink = field(default_factory=EventSink)

    # -- construction -----------------------------------------------------

    def from_text(
        self,
        text: str,
        *,
        organisation_id: str,
        project_id: str,
        language: str = "en",
        origin: ScriptOrigin = ScriptOrigin.AUTHORED,
    ) -> Script:
        """The user's own words, split into addressable lines and nothing else.

        `source_text` and `current_text` start identical. They diverge only
        when the user accepts a revision, which is the only path that changes
        `current_text` at all.
        """
        cleaned = strip_emphasis(text.strip())
        if not cleaned:
            raise ValidationFailed("a script needs some text")

        blocks: list[ScriptBlock] = []
        cursor = 0
        for order, (sentence, _starts_paragraph) in enumerate(split_blocks(cleaned)):
            # Offsets into the *source*, found by scanning forward. Forward-only
            # so a repeated sentence maps to its own occurrence rather than to
            # the first one.
            found = cleaned.find(sentence, cursor)
            start = found if found >= 0 else None
            end = (found + len(sentence)) if found >= 0 else None
            if found >= 0:
                cursor = found + len(sentence)
            blocks.append(
                ScriptBlock(
                    order=order,
                    text=sentence,
                    source_start=start,
                    source_end=end,
                    estimated_seconds=estimate_seconds(sentence),
                )
            )

        script = Script(
            organisation_id=organisation_id,
            project_id=project_id,
            origin=origin,
            language=language,
            source_text=cleaned,
            current_text=cleaned,
            blocks=blocks,
            history=[ScriptVersion(version=1, text=cleaned)],
        )
        self.events.emit(
            EventName.SCRIPT_CREATED,
            project_id=project_id,
            data={
                "origin": origin.value,
                "blocks": len(blocks),
                "words": script.word_count,
                "estimated_seconds": script.estimated_duration_seconds,
                "language": language,
            },
        )
        return script

    def from_transcript(
        self,
        transcript: Transcript,
        *,
        organisation_id: str,
        project_id: str,
    ) -> Script:
        """A spoken recording becomes an editable script with real timings.

        This is what makes "automatic mode converts to director mode" true
        rather than aspirational: after the AI has built the story, the user
        gets the same `Script` object they would have got by pasting one, with
        the timings already measured from their own voice.

        `origin` is `SPOKEN`, which is not cosmetic — it is what later makes the
        system offer "re-record or synthesise?" instead of silently shipping
        audio that no longer matches the words.
        """
        blocks: list[ScriptBlock] = []
        for segment in transcript.segments:
            text = segment.text.strip()
            if not text:
                continue
            blocks.append(
                ScriptBlock(
                    # Ordered by position among the blocks we kept, not among
                    # the segments: a provider that emits an empty segment must
                    # not leave a hole in the ordering.
                    order=len(blocks),
                    text=text,
                    estimated_seconds=estimate_seconds(text),
                    measured_start=segment.span.start,
                    measured_end=segment.span.end,
                    status=BlockStatus.DRAFT,
                )
            )

        if not blocks:
            raise ValidationFailed("that recording produced no speech to edit")

        text = "\n\n".join(item.text for item in blocks)
        script = Script(
            organisation_id=organisation_id,
            project_id=project_id,
            origin=ScriptOrigin.SPOKEN,
            language=transcript.language,
            source_text=text,
            current_text=text,
            blocks=blocks,
            history=[ScriptVersion(version=1, text=text)],
        )
        self.events.emit(
            EventName.SCRIPT_CREATED,
            project_id=project_id,
            data={
                "origin": ScriptOrigin.SPOKEN.value,
                "blocks": len(blocks),
                "words": script.word_count,
                "measured_seconds": script.measured_duration_seconds,
                "language": transcript.language,
            },
        )
        return script

    # -- conversion -------------------------------------------------------

    def to_transcript(self, script: Script) -> Transcript:
        """The script as a transcript, so the existing pipeline can consume it.

        This is the convergence, made concrete. Understanding, scene planning
        and the visual director already take a `Transcript`; giving them one
        built from the script means script mode is not a second pipeline, it is
        the same pipeline entered from a different door.

        Timings are measured where they exist and estimated where they do not,
        laid end to end. A gap between blocks is not inserted here — that is a
        pacing decision, and pacing happens after the visuals are planned.
        """
        segments: list[TranscriptSegment] = []
        cursor = 0.0
        for block in script.narrated_blocks:
            if block.measured_start is not None and block.measured_end is not None:
                span = TimeSpan.of(block.measured_start, block.measured_end)
                cursor = max(cursor, block.measured_end)
            else:
                span = TimeSpan.of(
                    round(cursor, 3), round(cursor + block.estimated_seconds, 3)
                )
                cursor += block.estimated_seconds
            segments.append(TranscriptSegment(span=span, text=block.text))

        if not segments:
            raise ValidationFailed("this script has nothing narratable in it")

        return Transcript(
            organisation_id=script.organisation_id,
            project_id=script.project_id,
            recording_id=new_id(IdPrefix.RECORDING),
            language=script.language,
            segments=segments,
            # Named so a script-derived transcript is never mistaken for one a
            # speech provider produced. The audit's standing lesson: a synthetic
            # artefact that looks real is worse than one that admits it.
            provider="script",
            model=f"blocks-{script.version}",
        )

    # -- editing ----------------------------------------------------------

    def replace_block_text(
        self, script: Script, *, block_id: str, text: str
    ) -> Script:
        """Edit one line directly.

        The manual counterpart to an accepted revision. Marks timing stale
        rather than recomputing it, because recomputing an estimate over a
        block whose *measured* timing came from a recording would replace a
        fact with a guess.
        """
        block = script.block(block_id)
        if block is None:
            raise ValidationFailed("no such line in this script")
        cleaned = text.strip()
        if not cleaned:
            raise ValidationFailed("a line cannot be empty; mute it instead")
        if cleaned == block.text:
            return script

        was_measured = block.measured_start is not None
        block.text = cleaned
        block.estimated_seconds = estimate_seconds(cleaned)
        block.timing_invalidated = True
        script.current_text = self._render(script)
        script.record_version()
        if was_measured and script.origin is ScriptOrigin.SPOKEN:
            # The audio still says the old words. Never resolved silently —
            # the user picks re-record or synthesis.
            script.diverged_from_recording = True

        self.events.emit(
            EventName.SCRIPT_EDITED,
            project_id=script.project_id,
            data={
                "block_id": block_id,
                "version": script.version,
                "diverged_from_recording": script.diverged_from_recording,
            },
        )
        return script

    def set_block_status(
        self, script: Script, *, block_id: str, status: BlockStatus
    ) -> Script:
        """Mute or un-mute one line.

        `current_text` is re-rendered either way, and the version advances.
        An earlier version re-rendered only on mute, so un-muting left
        `current_text` permanently short of the line it had just restored:
        `blocks` said the line was narrated and `current_text` — the field whose
        job is to be "what the video will actually narrate" — disagreed. It also
        skipped the version bump, which let a proposal computed against the old
        text still pass `accept`'s version check.
        """
        block = script.block(block_id)
        if block is None:
            raise ValidationFailed("no such line in this script")
        if block.status is status:
            return script
        block.status = status
        script.current_text = self._render(script)
        script.record_version()
        return script

    def _render(self, script: Script) -> str:
        """The narration as one string. Blank line between blocks."""
        return "\n\n".join(item.text for item in script.narrated_blocks)


__all__ = [
    "MAX_BLOCK_CHARS",
    "MIN_BLOCK_CHARS",
    "ScriptService",
    "split_blocks",
]
