"""Stage 21 — the universal input representation.

Voice is the wedge. The platform is knowledge to visuals, and knowledge arrives
as a PDF, a deck, a page, a spreadsheet or a paragraph at least as often as it
arrives as speech.

Rather than build a second pipeline for documents, everything normalises into a
`SourceDocument`: an ordered list of `SourceBlock`s carrying text plus the
structure the format expressed — a heading is a heading, a list is a list, a
table is a table. That structure is *information about meaning* and throwing it
away to get a flat string is the most common mistake in document ingestion.

The document then becomes a `Transcript`, which is the only interface Stages 3
onward have ever known. A PDF and a recording take the identical path from that
point, which is the whole argument for the pipeline being a chain of documents.

Provenance is preserved throughout. Every block knows its page, slide or line,
so a claim in the finished video can be traced back to page 14 of the source —
which is what Stage 24's grounding needs, and what an enterprise customer will
ask for.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Confidence,
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import Status
from vtv.contracts.language import Language


class InputKind(str, Enum):
    """What the user gave us."""

    VOICE = "voice"
    TEXT = "text"
    MARKDOWN = "markdown"
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    HTML = "html"
    WEB_URL = "web_url"
    SPREADSHEET = "spreadsheet"
    CSV = "csv"
    JSON = "json"
    IMAGE = "image"
    VIDEO = "video"


class BlockKind(str, Enum):
    """The structural role a piece of content played in its source.

    This is the part that survives from the original format and that a flat
    text extraction destroys. A `HEADING` marks a topic boundary the scene
    engine should respect; a `TABLE` is data a chart can be built from; a
    `CAPTION` describes a figure rather than asserting a fact.
    """

    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    QUOTE = "quote"
    TABLE = "table"
    CAPTION = "caption"
    CODE = "code"
    FOOTNOTE = "footnote"
    SPEAKER_NOTE = "speaker_note"
    FIGURE = "figure"
    METADATA = "metadata"


#: Blocks that carry the argument. Everything else is apparatus — page numbers,
#: footers, figure labels — and is kept for provenance but not narrated.
NARRATABLE: frozenset[BlockKind] = frozenset(
    {
        BlockKind.TITLE,
        BlockKind.HEADING,
        BlockKind.PARAGRAPH,
        BlockKind.LIST_ITEM,
        BlockKind.QUOTE,
        BlockKind.CODE,
        BlockKind.TABLE,
    }
)


class SourceLocation(VTVModel):
    """Where in the original a block came from.

    Kept so that a claim in the finished video can be traced to page 14, or to
    slide 3's speaker notes. Enterprise customers ask for this, and Stage 24's
    grounding needs it.
    """

    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    sheet: str | None = Field(default=None, max_length=128)
    section: str | None = Field(default=None, max_length=200)
    #: Character offset into the extracted plain text of the whole document.
    offset: int | None = Field(default=None, ge=0)
    #: URL fragment or anchor, for web sources.
    anchor: str | None = Field(default=None, max_length=200)

    def describe(self) -> str:
        parts: list[str] = []
        if self.page:
            parts.append(f"page {self.page}")
        if self.slide:
            parts.append(f"slide {self.slide}")
        if self.sheet:
            parts.append(f"sheet {self.sheet}")
        if self.section:
            parts.append(self.section)
        return ", ".join(parts) or "document"


class TableData(VTVModel):
    """A table, kept as a table.

    Flattening a table into prose destroys the one thing that makes it useful:
    a chart can be built directly from rows and columns, and cannot be built
    from "the first column says 2019 and the second says forty-two percent".
    """

    headers: list[str] = Field(default_factory=list, max_length=32)
    rows: list[list[str]] = Field(default_factory=list, max_length=500)
    caption: str | None = Field(default=None, max_length=400)

    @property
    def is_chartable(self) -> bool:
        """Whether a chart could plausibly be built from this.

        Needs at least one label column, one numeric column, and two rows —
        below that there is nothing to compare.
        """
        if len(self.rows) < 2 or not self.headers:
            return False
        return any(self.numeric_column(index) for index in range(len(self.headers)))

    def numeric_column(self, index: int) -> list[float] | None:
        """The column's values as numbers, or ``None`` if it is not numeric."""
        values: list[float] = []
        for row in self.rows:
            if index >= len(row):
                return None
            cleaned = (
                row[index].strip().replace(",", "").replace("%", "").replace("$", "")
            )
            if not cleaned:
                return None
            try:
                values.append(float(cleaned))
            except ValueError:
                return None
        return values or None

    def to_text(self) -> str:
        """A prose rendering, for the narration path."""
        lines = []
        if self.caption:
            lines.append(self.caption)
        if self.headers:
            lines.append(" | ".join(self.headers))
        for row in self.rows[:20]:
            lines.append(" | ".join(row))
        return "\n".join(lines)


class SourceBlock(VTVModel):
    """One structural piece of a source document."""

    block_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SEGMENT))
    kind: BlockKind
    text: str = Field(default="", max_length=20000)
    #: Heading depth, 1 being the most important.
    level: int | None = Field(default=None, ge=1, le=6)
    location: SourceLocation = Field(default_factory=SourceLocation)
    table: TableData | None = None
    #: An embedded image, already stored. Figures in a source document are
    #: assets we are usually permitted to reuse, and are far better than
    #: anything we could generate for the same passage.
    image: ObjectRef | None = None
    language: Language | None = None

    @model_validator(mode="after")
    def _kind_matches_payload(self) -> SourceBlock:
        if self.kind is BlockKind.TABLE and self.table is None and not self.text:
            raise ValueError("a table block must carry table data or text")
        if self.kind is not BlockKind.TABLE and self.table is not None:
            raise ValueError("only a table block may carry table data")
        return self

    @property
    def is_narratable(self) -> bool:
        return self.kind in NARRATABLE and bool(self.text.strip() or self.table)

    def narration_text(self) -> str:
        if self.table is not None:
            return self.table.to_text()
        return self.text


class SourceDocument(RootDocument, Timestamped):
    """Any input, normalised.

    Everything the platform accepts — a recording, a PDF, a deck, a page, a
    spreadsheet — becomes one of these, and one of these becomes a `Transcript`.
    Stages 3 through 30 never learn which it was.
    """

    document_name = "source_document"

    source_document_id: Id = Field(default_factory=lambda: new_id(IdPrefix.TRANSCRIPT))
    #: The tenant this belongs to. Carried on every project-scoped document so
    #: that a service deep in the pipeline can name the storage namespace it is
    #: allowed to write to without an ambient lookup — `LocalStorageProvider`
    #: refuses any key outside `orgs/<organisation_id>/`.
    organisation_id: Id
    project_id: Id

    kind: InputKind
    title: str | None = Field(default=None, max_length=500)
    #: Where it came from: a filename, a URL, a recording id.
    origin: str | None = Field(default=None, max_length=2048)
    language: Language = Field(default_factory=Language)
    blocks: list[SourceBlock] = Field(default_factory=list, max_length=5000)

    #: Author, creation date and so on, as the format reported them. Kept
    #: verbatim: it is provenance, and parsing it into our own fields would lose
    #: whatever we failed to anticipate.
    metadata: dict[str, str] = Field(default_factory=dict)
    #: Parser name and version, so a bad extraction can be attributed and
    #: re-run when the parser improves.
    parser: str | None = Field(default=None, max_length=64)
    status: Status = Status.PENDING
    #: How much of the source we believe we extracted. A scanned PDF with no
    #: text layer scores near zero, and that is worth knowing before spending
    #: money understanding an empty document.
    extraction_confidence: Confidence | None = None

    @property
    def text(self) -> str:
        """The narratable content as one string."""
        return "\n\n".join(
            block.narration_text() for block in self.blocks if block.is_narratable
        ).strip()

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def blocks_of(self, kind: BlockKind) -> list[SourceBlock]:
        return [block for block in self.blocks if block.kind is kind]

    def tables(self) -> list[TableData]:
        return [block.table for block in self.blocks if block.table is not None]

    def images(self) -> list[SourceBlock]:
        return [block for block in self.blocks if block.image is not None]

    def outline(self) -> list[tuple[int, str]]:
        """Headings with their depth — the document's own structure.

        The scene engine uses this: an author's section boundaries are a better
        signal of topic change than anything we could infer from the prose.
        """
        return [
            (block.level or 1, block.text)
            for block in self.blocks
            if block.kind in {BlockKind.TITLE, BlockKind.HEADING} and block.text
        ]

    def block_at_offset(self, offset: int) -> SourceBlock | None:
        """The block covering a character offset, for tracing a claim back."""
        for block in self.blocks:
            start = block.location.offset
            if start is None:
                continue
            if start <= offset < start + len(block.text):
                return block
        return None


class IngestionResult(VTVModel):
    """What a parser produced, including what it could not do."""

    document: SourceDocument
    #: Problems that did not stop extraction: an unreadable page, a skipped
    #: embedded object. Surfaced to the user rather than silently swallowed.
    warnings: list[str] = Field(default_factory=list, max_length=64)
    #: Bytes of the original, for the record.
    source_bytes: int | None = Field(default=None, ge=0)


__all__ = [
    "NARRATABLE",
    "BlockKind",
    "IngestionResult",
    "InputKind",
    "SourceBlock",
    "SourceDocument",
    "SourceLocation",
    "TableData",
]
