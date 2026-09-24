"""Stage 21 — normalising any input into the pipeline.

A `SourceDocument` becomes a `Transcript`, which is the only thing Stages 3
onward have ever consumed. From that point a PDF and a recording are
indistinguishable, which is the payoff for making the pipeline a chain of
documents rather than a set of coupled services.

Two things are carried across that a naive "extract the text" step would lose.

**The author's own structure.** Headings become their own short segments, so the
scene engine sees a topic boundary exactly where the author put one. That is
better evidence than anything inferable from prose, and it is free.

**The data.** A table stays a table in `DocumentContext`, so the Visual Director
can build a chart from the actual rows. A spreadsheet turned into a paragraph is
a spreadsheet whose entire value has been discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vtv.contracts.base import IdPrefix, TimeSpan, new_id
from vtv.contracts.errors import Status, ValidationFailed
from vtv.contracts.language import Language
from vtv.contracts.source import (
    BlockKind,
    IngestionResult,
    SourceBlock,
    SourceDocument,
    TableData,
)
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.observability.events import EventName, EventSink

#: Words per minute of unhurried narration, used to give written input a
#: plausible spoken duration. A real recording always uses its measured timing;
#: this is only for text, and is labelled as synthetic on the transcript.
SPEAKING_WPM = 145.0

#: Beat after a heading, as a narrator would pause before a new section.
HEADING_PAUSE = 0.6
BLOCK_PAUSE = 0.3

#: A heading shorter than this is a label, not a sentence; it still gets its own
#: segment because it marks a boundary, but it gets a minimum duration so the
#: scene engine does not have to merge it away.
MIN_BLOCK_SECONDS = 1.2


@dataclass
class DocumentContext:
    """Structured material extracted alongside the narration.

    Passed to the Visual Director so that a table in the source becomes a chart
    in the video, and a figure in the source becomes the shot rather than
    something generated to replace it.
    """

    tables: list[TableData] = field(default_factory=list)
    figures: list[SourceBlock] = field(default_factory=list)
    outline: list[tuple[int, str]] = field(default_factory=list)
    #: Blocks that are apparatus rather than argument: captions, footnotes,
    #: speaker notes already narrated elsewhere. Kept for provenance.
    apparatus: list[SourceBlock] = field(default_factory=list)

    @property
    def chartable_tables(self) -> list[TableData]:
        return [table for table in self.tables if table.is_chartable]

    def is_empty(self) -> bool:
        return not (self.tables or self.figures or self.outline)


def transcript_from_source(
    document: SourceDocument, *, wpm: float = SPEAKING_WPM
) -> tuple[Transcript, DocumentContext]:
    """Turn a parsed document into a timed transcript plus its structure.

    Timings are synthesised at a speaking rate and the transcript is stamped
    ``provider="synthetic-document"`` so nothing downstream mistakes them for
    measured audio.
    """
    blocks, demoted = _choose_narration(document)
    if not blocks:
        raise ValidationFailed(
            "the document contained no readable content to narrate"
        )

    segments: list[TranscriptSegment] = []
    cursor = 0.0
    for block in blocks:
        text = " ".join(block.narration_text().split())
        if not text:
            continue
        words = max(1, len(text.split()))
        duration = max(MIN_BLOCK_SECONDS, (words / wpm) * 60.0)
        segments.append(
            TranscriptSegment(
                span=TimeSpan.of(round(cursor, 3), round(cursor + duration, 3)),
                text=text[:8000],
                # Structural role travels in the speaker field, which is the one
                # place the transcript contract already allows a label. It lets
                # the scene engine see "this was a heading" without the
                # transcript needing to know what a heading is.
                speaker=block.kind.value,
            )
        )
        cursor += duration + (
            HEADING_PAUSE
            if block.kind in {BlockKind.TITLE, BlockKind.HEADING}
            else BLOCK_PAUSE
        )

    if not segments:
        raise ValidationFailed("nothing narratable survived normalisation")

    transcript = Transcript(
        project_id=document.project_id,
        recording_id=new_id(IdPrefix.RECORDING),
        language=document.language.code,
        segments=segments,
        provider="synthetic-document",
        model=document.parser or "unknown",
        status=Status.READY,
    )

    context = DocumentContext(
        tables=document.tables(),
        figures=document.images(),
        outline=document.outline(),
        apparatus=[
            block
            for block in document.blocks
            if block.kind in {BlockKind.CAPTION, BlockKind.FOOTNOTE}
        ]
        + demoted,
    )
    return transcript, context


def _choose_narration(
    document: SourceDocument,
) -> tuple[list[SourceBlock], list[SourceBlock]]:
    """Decide which blocks are spoken and which are on-screen material.

    For a slide deck this is the single most valuable judgement in the whole
    ingestion path. A deck's bullet points are *on-screen text* — the author
    wrote them to be read, not spoken — and the speaker notes are the narration
    the author already wrote out. Narrating the bullets produces a video that
    reads its own captions aloud; narrating the notes produces the talk.

    So: on any slide that has speaker notes, the notes become the narration and
    the bullets are demoted to visual material. Slides without notes keep their
    text, because something has to be said.
    """
    notes_by_slide: dict[int, SourceBlock] = {
        block.location.slide: block
        for block in document.blocks
        if block.kind is BlockKind.SPEAKER_NOTE
        and block.location.slide is not None
        and block.text.strip()
    }
    if not notes_by_slide:
        return [block for block in document.blocks if block.is_narratable], []

    narration: list[SourceBlock] = []
    demoted: list[SourceBlock] = []
    seen_slides: set[int] = set()
    for block in document.blocks:
        slide = block.location.slide
        if block.kind is BlockKind.SPEAKER_NOTE:
            continue
        if slide is not None and slide in notes_by_slide:
            if block.kind in {BlockKind.TITLE, BlockKind.HEADING}:
                narration.append(block)
                if slide not in seen_slides:
                    seen_slides.add(slide)
                    narration.append(notes_by_slide[slide])
                continue
            demoted.append(block)
            continue
        if block.is_narratable:
            narration.append(block)

    # A slide whose notes were never reached (no title on the slide) still has
    # to be narrated.
    for slide, note in notes_by_slide.items():
        if slide not in seen_slides:
            narration.append(note)
    return narration, demoted


@dataclass
class IngestionService:
    """Bytes in, a transcript and its structure out."""

    registry: object  # ParserRegistry
    events: EventSink

    def ingest(
        self,
        data: bytes,
        *,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
        language: str | None = None,
    ) -> tuple[SourceDocument, Transcript, DocumentContext]:
        result: IngestionResult = self.registry.parse(  # type: ignore[attr-defined]
            data, project_id=project_id, filename=filename, origin=origin
        )
        document = result.document
        if language:
            document.language = Language.parse(language)

        transcript, context = transcript_from_source(document)

        self.events.emit(
            EventName.DOCUMENT_INGESTED,
            project_id=project_id,
            data={
                "source": "document",
                "kind": document.kind.value,
                "parser": document.parser,
                "blocks": len(document.blocks),
                "segments": len(transcript.segments),
                "tables": len(context.tables),
                "figures": len(context.figures),
                "language": document.language.code,
                "extraction_confidence": document.extraction_confidence,
                "warnings": len(result.warnings),
            },
        )
        return document, transcript, context

    def supported_formats(self) -> list[str]:
        return list(self.registry.supported())  # type: ignore[attr-defined]


def chart_series_from_table(table: TableData) -> tuple[list[str], list[float], str] | None:
    """Pull a labelled numeric series out of a table.

    Returns ``(labels, values, series_name)``. The first non-numeric column is
    the labels and the first numeric column is the values, which is the shape of
    virtually every table a human writes.

    Returns ``None`` rather than guessing when the table has no numeric column;
    a chart of invented numbers is worse than no chart at all.
    """
    if not table.headers or len(table.rows) < 2:
        return None

    # The first column is the label axis, by overwhelming convention — even
    # when it is numeric, because "Year | Devices" means years along the
    # bottom, not a chart of years. Values come from the first numeric column
    # after it.
    value_index: int | None = None
    values: list[float] = []
    for index in range(1, len(table.headers)):
        column = table.numeric_column(index)
        if column is not None:
            value_index = index
            values = column
            break

    if value_index is not None:
        labels = [
            row[0][:40] if row else "" for row in table.rows[: len(values)]
        ]
        return labels, values[: len(labels)], table.headers[value_index][:64]

    # Single numeric column: the rows are the series and the index labels it.
    first = table.numeric_column(0)
    if first:
        return (
            [str(number + 1) for number in range(len(first))],
            first,
            table.headers[0][:64],
        )
    return None


__all__ = [
    "BLOCK_PAUSE",
    "HEADING_PAUSE",
    "MIN_BLOCK_SECONDS",
    "SPEAKING_WPM",
    "DocumentContext",
    "IngestionService",
    "chart_series_from_table",
    "transcript_from_source",
]
