"""The document parsing port.

One interface, many formats. A parser declares which content types it handles,
sniffs bytes to confirm, and returns a `SourceDocument` plus an honest list of
what it could not extract.

Parsers live in adapters because they import third-party libraries — pypdf,
python-docx, python-pptx, BeautifulSoup. The registry that chooses between them
is core, and knows none of those names.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from vtv.contracts.source import IngestionResult, InputKind


@runtime_checkable
class DocumentParser(Protocol):
    """Turns bytes of one format into a `SourceDocument`."""

    @property
    def name(self) -> str:
        """Stable identifier recorded on every document this parser produces."""
        ...

    @property
    def handles(self) -> tuple[InputKind, ...]:
        """Input kinds this parser can extract."""
        ...

    def sniff(self, data: bytes, *, filename: str | None = None) -> bool:
        """Whether these bytes really are this format.

        The declared content type and the file extension are both hints from
        the client and both are routinely wrong. Magic bytes are the answer,
        exactly as in audio capture.
        """
        ...

    def parse(
        self,
        data: bytes,
        *,
        project_id: str,
        filename: str | None = None,
        origin: str | None = None,
    ) -> IngestionResult:
        """Extract structure and text.

        Must not raise for a merely difficult document: a PDF with one
        unreadable page produces a warning and the other pages. Raise only when
        nothing usable can be extracted at all.
        """
        ...


__all__ = ["DocumentParser"]
