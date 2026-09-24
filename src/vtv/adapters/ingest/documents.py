"""Document parsers.

STATUS: **REAL IMPLEMENTATIONS — ALL RUN AND ARE TESTED.**

Six parsers covering the formats knowledge actually arrives in: plain text,
Markdown, PDF, Word, PowerPoint and HTML. Each preserves the structure its
format expressed, because that structure is information about meaning:

* a **heading** is a topic boundary the scene engine should respect;
* a **list** is an enumeration, which wants a different visual from prose;
* a **table** is data a chart can be built from directly;
* **speaker notes** on a slide are the narration the author already wrote;
* a **figure caption** describes an image rather than asserting a fact.

Flattening all of that to a string is the standard approach and it throws away
most of what makes a document easier to visualise than a transcript.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from vtv.contracts.errors import ErrorCode, ValidationFailed, VTVError
from vtv.contracts.language import Language
from vtv.contracts.source import (
    BlockKind,
    IngestionResult,
    InputKind,
    SourceBlock,
    SourceDocument,
    SourceLocation,
    TableData,
)
from vtv.security.uploads import MAX_DOCUMENT_BYTES as _MAX_DOCUMENT_BYTES

#: Refuse anything larger before parsing. Document parsers are a classic
#: denial-of-service surface: a 2 KB zip can expand into gigabytes.
#: One definition, in `security.uploads`. A parser and the service that
#: calls it disagreeing about the ceiling is how a file gets refused in one
#: place and accepted in another.
MAX_DOCUMENT_BYTES = _MAX_DOCUMENT_BYTES

#: Cap on extracted blocks. A malformed file can otherwise produce millions.
MAX_BLOCKS = 5000


def _guard(data: bytes) -> None:
    if not data:
        raise ValidationFailed("empty document", code=ErrorCode.SCHEMA_INVALID)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValidationFailed(
            f"document of {len(data)} bytes exceeds the {MAX_DOCUMENT_BYTES} limit"
        )


def _anchor_of(element: Any) -> str | None:
    """The element's HTML id, if it has one that is a plain string.

    BeautifulSoup returns a list for attributes it believes are multi-valued,
    so this normalises rather than passing whatever it found into a contract
    that expects a string.
    """
    value = element.get("id")
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, list) and value and isinstance(value[0], str):
        return str(value[0])[:200]
    return None


def _document(
    *,
    organisation_id: str,
    project_id: str,
    kind: InputKind,
    blocks: list[SourceBlock],
    parser: str,
    title: str | None = None,
    origin: str | None = None,
    metadata: dict[str, str] | None = None,
) -> SourceDocument:
    trimmed = blocks[:MAX_BLOCKS]
    text = "\n".join(block.text for block in trimmed if block.is_narratable)
    return SourceDocument(
        organisation_id=organisation_id,
        project_id=project_id,
        kind=kind,
        title=title,
        origin=origin,
        language=Language.from_text(text) if text else Language(),
        blocks=trimmed,
        metadata=metadata or {},
        parser=parser,
        status="ready",  # type: ignore[arg-type]
        extraction_confidence=None,
    )


# ---------------------------------------------------------------------------
# Plain text and Markdown
# ---------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_MD_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_MD_FENCE = re.compile(r"^\s*```")
_MD_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


@dataclass
class TextParser:
    """Plain text and Markdown.

    Markdown gets full structural treatment because it is the format people
    paste most, and because its structure is unambiguous — no heuristics
    needed to know that `##` is a heading.
    """

    name: str = "text-1.0"
    handles: tuple[InputKind, ...] = (InputKind.TEXT, InputKind.MARKDOWN)

    def sniff(self, data: bytes, *, filename: str | None = None) -> bool:
        try:
            data[:4096].decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def parse(
        self,
        data: bytes,
        *,
        organisation_id: str,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        _guard(data)
        text = data.decode("utf-8", errors="replace")
        is_markdown = bool(filename and filename.lower().endswith((".md", ".markdown")))
        if not is_markdown:
            is_markdown = bool(_MD_HEADING.search(text) or _MD_TABLE_ROW.search(text))

        blocks = self._markdown(text) if is_markdown else self._plain(text)
        title = next(
            (b.text for b in blocks if b.kind in {BlockKind.TITLE, BlockKind.HEADING}),
            None,
        )
        return IngestionResult(
            document=_document(
                organisation_id=organisation_id,
                project_id=project_id,
                kind=InputKind.MARKDOWN if is_markdown else InputKind.TEXT,
                blocks=blocks,
                parser=self.name,
                title=title,
                origin=origin or filename,
            ),
            source_bytes=len(data),
        )

    @staticmethod
    def _plain(text: str) -> list[SourceBlock]:
        blocks: list[SourceBlock] = []
        offset = 0
        for chunk in re.split(r"\n\s*\n", text):
            cleaned = chunk.strip()
            if cleaned:
                blocks.append(
                    SourceBlock(
                        kind=BlockKind.PARAGRAPH,
                        text=cleaned[:20000],
                        location=SourceLocation(offset=offset),
                    )
                )
            offset += len(chunk) + 2
        return blocks

    def _markdown(self, text: str) -> list[SourceBlock]:
        blocks: list[SourceBlock] = []
        paragraph: list[str] = []
        table_rows: list[list[str]] = []
        in_code = False
        code: list[str] = []
        offset = 0

        def flush_paragraph() -> None:
            if paragraph:
                blocks.append(
                    SourceBlock(
                        kind=BlockKind.PARAGRAPH,
                        text=" ".join(paragraph).strip()[:20000],
                        location=SourceLocation(offset=offset),
                    )
                )
                paragraph.clear()

        def flush_table() -> None:
            if len(table_rows) >= 2:
                blocks.append(
                    SourceBlock(
                        kind=BlockKind.TABLE,
                        text="",
                        table=TableData(
                            headers=table_rows[0][:32],
                            rows=[row[:32] for row in table_rows[1:]][:500],
                        ),
                        location=SourceLocation(offset=offset),
                    )
                )
            table_rows.clear()

        for line in text.splitlines():
            offset += len(line) + 1

            if _MD_FENCE.match(line):
                if in_code:
                    blocks.append(
                        SourceBlock(
                            kind=BlockKind.CODE,
                            text="\n".join(code)[:20000],
                            location=SourceLocation(offset=offset),
                        )
                    )
                    code.clear()
                else:
                    flush_paragraph()
                in_code = not in_code
                continue
            if in_code:
                code.append(line)
                continue

            if _MD_TABLE_RULE.match(line):
                continue
            table_match = _MD_TABLE_ROW.match(line)
            if table_match:
                flush_paragraph()
                table_rows.append(
                    [cell.strip() for cell in table_match.group(1).split("|")]
                )
                continue
            flush_table()

            heading = _MD_HEADING.match(line)
            if heading:
                flush_paragraph()
                level = len(heading.group(1))
                blocks.append(
                    SourceBlock(
                        kind=BlockKind.TITLE if level == 1 else BlockKind.HEADING,
                        text=heading.group(2).strip()[:500],
                        level=level,
                        location=SourceLocation(offset=offset),
                    )
                )
                continue

            quote = _MD_QUOTE.match(line)
            if quote:
                flush_paragraph()
                blocks.append(
                    SourceBlock(
                        kind=BlockKind.QUOTE,
                        text=quote.group(1).strip()[:4000],
                        location=SourceLocation(offset=offset),
                    )
                )
                continue

            item = _MD_LIST.match(line)
            if item:
                flush_paragraph()
                blocks.append(
                    SourceBlock(
                        kind=BlockKind.LIST_ITEM,
                        text=item.group(1).strip()[:4000],
                        location=SourceLocation(offset=offset),
                    )
                )
                continue

            if line.strip():
                paragraph.append(line.strip())
            else:
                flush_paragraph()

        flush_paragraph()
        flush_table()
        return blocks


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

@dataclass
class PdfParser:
    """PDF, via pypdf.

    The important honesty here is `extraction_confidence`: a scanned PDF has no
    text layer, and extracting nothing from it is not a bug but it *is*
    something the user must be told before we charge them for understanding an
    empty document. OCR belongs behind its own port and is not implemented.
    """

    name: str = "pdf-1.0"
    handles: tuple[InputKind, ...] = (InputKind.PDF,)

    def sniff(self, data: bytes, *, filename: str | None = None) -> bool:
        return data[:5] == b"%PDF-"

    def parse(
        self,
        data: bytes,
        *,
        organisation_id: str,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        _guard(data)
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VTVError("pypdf is required to read PDFs") from exc

        warnings: list[str] = []
        try:
            reader = PdfReader(io.BytesIO(data))
        except Exception as exc:
            raise ValidationFailed(f"unreadable PDF: {type(exc).__name__}") from exc

        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                raise ValidationFailed("the PDF is password protected") from None

        blocks: list[SourceBlock] = []
        offset = 0
        pages_with_text = 0
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                warnings.append(f"page {index} could not be read ({type(exc).__name__})")
                continue
            if text.strip():
                pages_with_text += 1
            for chunk, kind in self._segment(text):
                blocks.append(
                    SourceBlock(
                        kind=kind,
                        text=chunk[:20000],
                        location=SourceLocation(page=index, offset=offset),
                    )
                )
                offset += len(chunk)

        total_pages = max(1, len(reader.pages))
        confidence = pages_with_text / total_pages
        if confidence < 0.2:
            warnings.append(
                "Almost no text layer was found. This is probably a scanned "
                "document and needs OCR, which is not configured."
            )

        metadata: dict[str, str] = {}
        try:
            for key, value in (reader.metadata or {}).items():
                metadata[str(key).lstrip("/")[:64]] = str(value)[:500]
        except Exception:
            pass

        document = _document(
            organisation_id=organisation_id,
            project_id=project_id,
            kind=InputKind.PDF,
            blocks=blocks,
            parser=self.name,
            title=metadata.get("Title") or (filename or None),
            origin=origin or filename,
            metadata=metadata,
        )
        document.extraction_confidence = round(confidence, 3)
        return IngestionResult(
            document=document, warnings=warnings, source_bytes=len(data)
        )

    @staticmethod
    def _segment(text: str) -> list[tuple[str, BlockKind]]:
        """Split a page into blocks, guessing headings from shape.

        PDF has no semantic structure — it is positioned glyphs — so headings
        are inferred: a short line, title case or all caps, no terminal
        punctuation. Conservative on purpose; a missed heading costs little, a
        paragraph misread as a heading reads badly.
        """
        def looks_like_heading(line: str) -> bool:
            return (
                len(line) <= 80
                and not line.endswith((".", ",", ";", ":"))
                and (line.isupper() or line.istitle())
                and 1 <= len(line.split()) <= 12
            )

        out: list[tuple[str, BlockKind]] = []
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                out.append((" ".join(paragraph), BlockKind.PARAGRAPH))
                paragraph.clear()

        # Blank lines are the ideal paragraph separator, but many PDFs have
        # none — every line is its own text run. Falling back to sentence
        # boundaries keeps those documents from collapsing into one block.
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line:
                flush()
                continue
            if looks_like_heading(line):
                flush()
                out.append((line, BlockKind.HEADING))
                continue
            paragraph.append(line)
            if line.endswith((".", "!", "?")):
                flush()
        flush()
        return out


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------

@dataclass
class DocxParser:
    """Word documents, via python-docx.

    Word *does* carry semantic structure — paragraph styles name headings,
    lists and quotes — so unlike PDF nothing has to be guessed.
    """

    name: str = "docx-1.0"
    handles: tuple[InputKind, ...] = (InputKind.DOCX,)

    def sniff(self, data: bytes, *, filename: str | None = None) -> bool:
        # OOXML is a zip; the word/ entry distinguishes it from pptx and xlsx.
        if data[:2] != b"PK":
            return False
        return b"word/" in data[:8000] or bool(
            filename and filename.lower().endswith(".docx")
        )

    def parse(
        self,
        data: bytes,
        *,
        organisation_id: str,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        _guard(data)
        try:
            import docx
        except ImportError as exc:  # pragma: no cover
            raise VTVError("python-docx is required to read Word documents") from exc

        try:
            document = docx.Document(io.BytesIO(data))
        except Exception as exc:
            raise ValidationFailed(f"unreadable Word document: {type(exc).__name__}") from exc

        blocks: list[SourceBlock] = []
        offset = 0
        for paragraph in document.paragraphs:
            text = " ".join(paragraph.text.split())
            if not text:
                continue
            style = (paragraph.style.name if paragraph.style else "") or ""
            kind, level = self._classify(style)
            blocks.append(
                SourceBlock(
                    kind=kind,
                    text=text[:20000],
                    level=level,
                    location=SourceLocation(offset=offset, section=style[:200] or None),
                )
            )
            offset += len(text)

        for table in document.tables:
            rows = [
                [" ".join(cell.text.split())[:200] for cell in row.cells]
                for row in table.rows[:500]
            ]
            if not rows:
                continue
            blocks.append(
                SourceBlock(
                    kind=BlockKind.TABLE,
                    text="",
                    table=TableData(headers=rows[0][:32], rows=[r[:32] for r in rows[1:]]),
                    location=SourceLocation(offset=offset),
                )
            )

        properties = document.core_properties
        metadata = {
            key: str(value)[:500]
            for key, value in (
                ("title", properties.title),
                ("author", properties.author),
                ("subject", properties.subject),
            )
            if value
        }

        title = metadata.get("title") or next(
            (b.text for b in blocks if b.kind is BlockKind.TITLE), None
        )
        return IngestionResult(
            document=_document(
                organisation_id=organisation_id,
                project_id=project_id,
                kind=InputKind.DOCX,
                blocks=blocks,
                parser=self.name,
                title=title,
                origin=origin or filename,
                metadata=metadata,
            ),
            source_bytes=len(data),
        )

    @staticmethod
    def _classify(style: str) -> tuple[BlockKind, int | None]:
        lowered = style.lower()
        if lowered.startswith("title"):
            return BlockKind.TITLE, 1
        if lowered.startswith("heading"):
            digits = re.search(r"(\d)", lowered)
            return BlockKind.HEADING, int(digits.group(1)) if digits else 2
        if "quote" in lowered:
            return BlockKind.QUOTE, None
        if "list" in lowered:
            return BlockKind.LIST_ITEM, None
        if "caption" in lowered:
            return BlockKind.CAPTION, None
        return BlockKind.PARAGRAPH, None


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------

@dataclass
class PptxParser:
    """Slide decks, via python-pptx.

    Decks are the richest input the platform gets, because the author has
    already done the segmentation: one slide is one idea, the title is the
    point, and the speaker notes are frequently the narration written out. A
    deck plus its notes is very close to a finished storyboard.
    """

    name: str = "pptx-1.0"
    handles: tuple[InputKind, ...] = (InputKind.PPTX,)

    def sniff(self, data: bytes, *, filename: str | None = None) -> bool:
        if data[:2] != b"PK":
            return False
        return b"ppt/" in data[:8000] or bool(
            filename and filename.lower().endswith(".pptx")
        )

    def parse(
        self,
        data: bytes,
        *,
        organisation_id: str,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        _guard(data)
        try:
            from pptx import Presentation
        except ImportError as exc:  # pragma: no cover
            raise VTVError("python-pptx is required to read presentations") from exc

        try:
            deck = Presentation(io.BytesIO(data))
        except Exception as exc:
            raise ValidationFailed(f"unreadable presentation: {type(exc).__name__}") from exc

        blocks: list[SourceBlock] = []
        warnings: list[str] = []
        offset = 0

        for index, slide in enumerate(deck.slides, start=1):
            location = SourceLocation(slide=index, offset=offset)
            title_shape = None
            try:
                title_shape = slide.shapes.title
            except Exception:
                title_shape = None

            if title_shape is not None and title_shape.has_text_frame:
                text = " ".join(title_shape.text_frame.text.split())
                if text:
                    blocks.append(
                        SourceBlock(
                            kind=BlockKind.HEADING,
                            text=text[:500],
                            level=2,
                            location=location,
                        )
                    )
                    offset += len(text)

            for shape in slide.shapes:
                if shape is title_shape:
                    continue
                if getattr(shape, "has_table", False):
                    rows = [
                        [" ".join(cell.text.split())[:200] for cell in row.cells]
                        for row in shape.table.rows
                    ]
                    if rows:
                        blocks.append(
                            SourceBlock(
                                kind=BlockKind.TABLE,
                                text="",
                                table=TableData(
                                    headers=rows[0][:32],
                                    rows=[r[:32] for r in rows[1:]][:500],
                                ),
                                location=location,
                            )
                        )
                    continue
                if not getattr(shape, "has_text_frame", False):
                    continue
                for paragraph in shape.text_frame.paragraphs:
                    text = " ".join(run.text for run in paragraph.runs).strip()
                    text = " ".join(text.split())
                    if not text:
                        continue
                    kind = (
                        BlockKind.LIST_ITEM
                        if paragraph.level and paragraph.level > 0
                        else BlockKind.PARAGRAPH
                    )
                    blocks.append(
                        SourceBlock(kind=kind, text=text[:8000], location=location)
                    )
                    offset += len(text)

            # Speaker notes: usually the narration the author already wrote.
            try:
                if slide.has_notes_slide:
                    notes = " ".join(slide.notes_slide.notes_text_frame.text.split())
                    if notes:
                        blocks.append(
                            SourceBlock(
                                kind=BlockKind.SPEAKER_NOTE,
                                text=notes[:20000],
                                location=location,
                            )
                        )
            except Exception:
                warnings.append(f"slide {index}: notes could not be read")

        return IngestionResult(
            document=_document(
                organisation_id=organisation_id,
                project_id=project_id,
                kind=InputKind.PPTX,
                blocks=blocks,
                parser=self.name,
                title=next((b.text for b in blocks if b.kind is BlockKind.HEADING), None),
                origin=origin or filename,
                metadata={"slides": str(len(deck.slides))},
            ),
            warnings=warnings,
            source_bytes=len(data),
        )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@dataclass
class HtmlParser:
    """Web pages, via BeautifulSoup.

    Most of a web page is not content. Navigation, cookie banners, related
    links and footers all extract as perfectly good sentences and would be
    narrated as though the author had written them. The extractor therefore
    removes structural chrome first and prefers a `<main>` or `<article>`
    element when the page provides one.
    """

    name: str = "html-1.0"
    handles: tuple[InputKind, ...] = (InputKind.HTML, InputKind.WEB_URL)

    #: Elements that never contain the argument.
    DROP = (
        "script", "style", "nav", "header", "footer", "aside", "form",
        "noscript", "iframe", "svg", "button", "template",
    )

    def sniff(self, data: bytes, *, filename: str | None = None) -> bool:
        head = data[:2048].lower()
        return b"<html" in head or b"<!doctype html" in head or b"<body" in head

    def parse(
        self,
        data: bytes,
        *,
        organisation_id: str,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        _guard(data)
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise VTVError("beautifulsoup4 is required to read HTML") from exc

        soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "html.parser")
        for tag in soup(list(self.DROP)):
            tag.decompose()

        root = soup.find("main") or soup.find("article") or soup.body or soup
        title = (soup.title.string or "").strip() if soup.title else None

        blocks: list[SourceBlock] = []
        offset = 0
        for element in root.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "table", "figcaption"]
        ):
            if element.name == "table":
                rows = [
                    [" ".join(cell.get_text().split())[:200] for cell in row.find_all(["td", "th"])]
                    for row in element.find_all("tr")
                ]
                rows = [row for row in rows if row]
                if len(rows) >= 2:
                    blocks.append(
                        SourceBlock(
                            kind=BlockKind.TABLE,
                            text="",
                            table=TableData(headers=rows[0][:32], rows=[r[:32] for r in rows[1:]][:500]),
                            location=SourceLocation(offset=offset),
                        )
                    )
                continue

            text = " ".join(element.get_text().split())
            if not text or len(text) < 2:
                continue
            kind, level = self._classify(element.name)
            blocks.append(
                SourceBlock(
                    kind=kind,
                    text=text[:20000],
                    level=level,
                    location=SourceLocation(
                        offset=offset, anchor=_anchor_of(element)
                    ),
                )
            )
            offset += len(text)

        return IngestionResult(
            document=_document(
                organisation_id=organisation_id,
                project_id=project_id,
                kind=InputKind.HTML,
                blocks=blocks,
                parser=self.name,
                title=title,
                origin=origin or filename,
            ),
            source_bytes=len(data),
        )

    @staticmethod
    def _classify(tag: str) -> tuple[BlockKind, int | None]:
        if tag == "h1":
            return BlockKind.TITLE, 1
        if tag in {"h2", "h3", "h4", "h5", "h6"}:
            return BlockKind.HEADING, int(tag[1])
        if tag == "li":
            return BlockKind.LIST_ITEM, None
        if tag == "blockquote":
            return BlockKind.QUOTE, None
        if tag == "pre":
            return BlockKind.CODE, None
        if tag == "figcaption":
            return BlockKind.CAPTION, None
        return BlockKind.PARAGRAPH, None


# ---------------------------------------------------------------------------
# Tabular data
# ---------------------------------------------------------------------------

@dataclass
class DataParser:
    """CSV, TSV, spreadsheets and JSON.

    Data is the input type the Visual Director handles best, because a chart of
    real numbers is exactly the shot it wants to choose. Rows are preserved as
    a table rather than prose so the chart can be built from them directly.
    """

    name: str = "data-1.0"
    handles: tuple[InputKind, ...] = (
        InputKind.CSV,
        InputKind.SPREADSHEET,
        InputKind.JSON,
    )

    def sniff(self, data: bytes, *, filename: str | None = None) -> bool:
        lowered = (filename or "").lower()
        if lowered.endswith((".csv", ".tsv", ".json", ".xlsx")):
            return True
        if data[:2] == b"PK" and b"xl/" in data[:8000]:
            return True
        head = data[:512].lstrip()
        return head[:1] in (b"{", b"[")

    def parse(
        self,
        data: bytes,
        *,
        organisation_id: str,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        _guard(data)
        lowered = (filename or "").lower()
        if lowered.endswith(".xlsx") or (data[:2] == b"PK" and b"xl/" in data[:8000]):
            blocks, kind = self._spreadsheet(data)
        elif lowered.endswith(".json") or data[:512].lstrip()[:1] in (b"{", b"["):
            blocks, kind = self._json(data)
        else:
            blocks, kind = self._delimited(data)

        return IngestionResult(
            document=_document(
                organisation_id=organisation_id,
                project_id=project_id,
                kind=kind,
                blocks=blocks,
                parser=self.name,
                title=filename,
                origin=origin or filename,
            ),
            source_bytes=len(data),
        )

    @staticmethod
    def _delimited(data: bytes) -> tuple[list[SourceBlock], InputKind]:
        text = data.decode("utf-8", errors="replace")
        try:
            dialect: Any = csv.Sniffer().sniff(text[:4096])
        except csv.Error:
            dialect = csv.excel
        rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(row)]
        if not rows:
            raise ValidationFailed("no rows found in the data file")
        table = TableData(
            headers=[cell[:200] for cell in rows[0]][:32],
            rows=[[cell[:200] for cell in row][:32] for row in rows[1:]][:500],
        )
        return [SourceBlock(kind=BlockKind.TABLE, text="", table=table)], InputKind.CSV

    @staticmethod
    def _spreadsheet(data: bytes) -> tuple[list[SourceBlock], InputKind]:
        try:
            from openpyxl import load_workbook  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise VTVError("openpyxl is required to read spreadsheets") from exc
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        blocks: list[SourceBlock] = []
        for sheet in workbook.worksheets:
            rows = [
                ["" if cell is None else str(cell)[:200] for cell in row]
                for row in sheet.iter_rows(max_row=501, values_only=True)
            ]
            rows = [row for row in rows if any(cell.strip() for cell in row)]
            if len(rows) < 2:
                continue
            blocks.append(
                SourceBlock(
                    kind=BlockKind.TABLE,
                    text="",
                    table=TableData(
                        headers=rows[0][:32],
                        rows=[row[:32] for row in rows[1:]],
                        caption=sheet.title,
                    ),
                    location=SourceLocation(sheet=sheet.title[:128]),
                )
            )
        workbook.close()
        if not blocks:
            raise ValidationFailed("the spreadsheet contained no usable rows")
        return blocks, InputKind.SPREADSHEET

    @staticmethod
    def _json(data: bytes) -> tuple[list[SourceBlock], InputKind]:
        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ValidationFailed(f"invalid JSON: {exc.msg}") from exc

        # A list of flat objects is a table; anything else is described as text.
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            headers = list(payload[0])[:32]
            rows = [
                [str(item.get(key, ""))[:200] for key in headers]
                for item in payload[:500]
                if isinstance(item, dict)
            ]
            return (
                [
                    SourceBlock(
                        kind=BlockKind.TABLE,
                        text="",
                        table=TableData(headers=headers, rows=rows),
                    )
                ],
                InputKind.JSON,
            )
        return (
            [
                SourceBlock(
                    kind=BlockKind.PARAGRAPH,
                    text=json.dumps(payload, indent=2)[:20000],
                )
            ],
            InputKind.JSON,
        )


@dataclass
class ParserRegistry:
    """Chooses a parser by sniffing the bytes.

    Extension and declared content type are hints; the bytes decide. Order
    matters only where formats overlap — DOCX, PPTX and XLSX are all zips, so
    each sniffs for its own internal directory.
    """

    parsers: list[Any] = field(
        default_factory=lambda: [
            PdfParser(),
            DocxParser(),
            PptxParser(),
            DataParser(),
            HtmlParser(),
            TextParser(),
        ]
    )

    def for_bytes(self, data: bytes, *, filename: str | None = None) -> Any:
        for parser in self.parsers:
            try:
                if parser.sniff(data, filename=filename):
                    return parser
            except Exception:
                continue
        raise ValidationFailed(
            "unsupported document format",
            code=ErrorCode.SCHEMA_INVALID,
        )

    def parse(
        self,
        data: bytes,
        *,
        organisation_id: str,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        parser = self.for_bytes(data, filename=filename)
        result: IngestionResult = parser.parse(
            data,
            organisation_id=organisation_id,
            project_id=project_id,
            filename=filename,
            origin=origin,
        )
        return result

    def supported(self) -> list[str]:
        return sorted(
            {kind.value for parser in self.parsers for kind in parser.handles}
        )


__all__ = [
    "MAX_BLOCKS",
    "MAX_DOCUMENT_BYTES",
    "DataParser",
    "DocxParser",
    "HtmlParser",
    "ParserRegistry",
    "PdfParser",
    "PptxParser",
    "TextParser",
]
