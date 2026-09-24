"""Upload inspection — treating every uploaded byte as hostile.

The threat model is the one the brief states: assume users upload malicious
files. Concretely, what arrives at this endpoint may be a zip bomb wearing a
.docx extension, an SVG containing a script, an HTML file that claims to be a
PDF, a 4KB file that decompresses to 40GB, or a real PDF with a payload aimed
at whatever library opens it.

What this module does, and — just as important — what it does not.

**Does: identify by content.** The declared content type and the extension are
both hints from the client and both are routinely wrong, sometimes deliberately.
Magic bytes decide.

**Does: refuse polyglots and mismatches.** A file whose bytes say HTML while its
name says PDF is not a confused user; it is an attempt to get one parser's input
through another parser's front door.

**Does: bound decompression.** Office formats are zips. A zip's own header
declares the uncompressed size, so the ratio can be checked *before* extracting
anything, which is the only safe order.

**Does: strip active content from SVG.** SVG is a document format that executes
script, which makes it the most dangerous "image" on the web.

**Does NOT: detect malware.** There is no scanner here and there is none in this
environment. `UploadPolicy.require_malware_scan` exists so a deployment can
demand one, and with no scanner wired in it *refuses the upload* rather than
waving it through. Saying "clean" without looking would be worse than useless.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from enum import Enum

from vtv.contracts.errors import PolicyViolation, ValidationFailed

#: Absolute ceiling on any single upload, checked before anything is parsed.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

#: A zip whose declared uncompressed size exceeds its compressed size by more
#: than this is a decompression bomb. Real documents sit far below it; the
#: classic 42.zip is around a million to one.
MAX_COMPRESSION_RATIO = 120.0
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000


class ContentClass(str, Enum):
    """What the bytes actually are, as determined by inspection."""

    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"
    DOCUMENT = "document"
    ARCHIVE = "archive"
    TEXT = "text"
    UNKNOWN = "unknown"


#: (magic prefix, offset, media type, class). Order matters only where one
#: signature is a prefix of another.
_SIGNATURES: tuple[tuple[bytes, int, str, ContentClass], ...] = (
    (b"%PDF-", 0, "application/pdf", ContentClass.DOCUMENT),
    (b"\x89PNG\r\n\x1a\n", 0, "image/png", ContentClass.IMAGE),
    (b"\xff\xd8\xff", 0, "image/jpeg", ContentClass.IMAGE),
    (b"GIF87a", 0, "image/gif", ContentClass.IMAGE),
    (b"GIF89a", 0, "image/gif", ContentClass.IMAGE),
    (b"RIFF", 0, "audio/wav", ContentClass.AUDIO),
    (b"OggS", 0, "audio/ogg", ContentClass.AUDIO),
    (b"fLaC", 0, "audio/flac", ContentClass.AUDIO),
    (b"\x1a\x45\xdf\xa3", 0, "video/webm", ContentClass.VIDEO),
    (b"ftyp", 4, "video/mp4", ContentClass.VIDEO),
    (b"ID3", 0, "audio/mpeg", ContentClass.AUDIO),
    (b"PK\x03\x04", 0, "application/zip", ContentClass.ARCHIVE),
    (b"\x1f\x8b", 0, "application/gzip", ContentClass.ARCHIVE),
    (b"BZh", 0, "application/x-bzip2", ContentClass.ARCHIVE),
    (b"\x37\x7a\xbc\xaf\x27\x1c", 0, "application/x-7z-compressed", ContentClass.ARCHIVE),
    (b"\xd0\xcf\x11\xe0", 0, "application/x-ole-storage", ContentClass.DOCUMENT),
)

#: Inside a zip, these paths identify which Office format it is.
_OFFICE_MARKERS: tuple[tuple[str, str], ...] = (
    ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
)

#: Executable formats. Never accepted, whatever the extension claims.
_EXECUTABLE_SIGNATURES: tuple[bytes, ...] = (
    b"MZ",            # Windows PE
    b"\x7fELF",       # Linux
    b"\xca\xfe\xba\xbe",  # Mach-O fat / Java class
    b"\xcf\xfa\xed\xfe",  # Mach-O
    b"#!",            # shell script
)

_HTML_SNIFF = re.compile(rb"<\s*(!doctype\s+html|html|head|body|script|iframe)", re.I)
_SVG_SNIFF = re.compile(rb"<\s*svg[\s>]", re.I)
_SVG_ACTIVE = re.compile(
    rb"<\s*script|<\s*foreignObject|\son\w+\s*=|javascript:|<\s*use[^>]+xlink:href\s*=\s*[\"']\s*http",
    re.I,
)


@dataclass(frozen=True)
class UploadVerdict:
    """What inspection concluded. Immutable, so it cannot drift after the check."""

    accepted: bool
    media_type: str
    content_class: ContentClass
    size_bytes: int
    reasons: tuple[str, ...] = ()
    #: True when the bytes disagree with the declared type or extension.
    mismatched: bool = False
    #: Set when the file was rewritten to be safe, e.g. a sanitised SVG.
    sanitised: bool = False

    def raise_if_rejected(self) -> None:
        if not self.accepted:
            raise PolicyViolation("; ".join(self.reasons) or "upload refused")


@dataclass
class UploadPolicy:
    """What this endpoint will accept."""

    max_bytes: int = MAX_UPLOAD_BYTES
    allowed_classes: frozenset[ContentClass] = frozenset(
        {ContentClass.AUDIO, ContentClass.VIDEO, ContentClass.IMAGE,
         ContentClass.DOCUMENT, ContentClass.TEXT}
    )
    allowed_media_types: frozenset[str] = frozenset()
    #: Refuse when the bytes do not match the declared type. On by default:
    #: a mismatch is far more often an attack than a mistake.
    reject_mismatch: bool = True
    #: Set true in a deployment that has a scanner. With no scanner supplied the
    #: upload is REFUSED, never silently accepted as clean.
    require_malware_scan: bool = False
    #: Injected scanner: bytes in, list of findings out. Empty means clean.
    scanner: object = None
    extra_signatures: tuple[tuple[bytes, int, str, ContentClass], ...] = field(
        default_factory=tuple
    )


def sniff(data: bytes) -> tuple[str, ContentClass]:
    """Identify content from its bytes alone.

    Text is the last resort rather than the first guess: everything binary is
    matched first, so a file that merely happens to start with printable bytes
    is not mistaken for a text document.
    """
    if not data:
        return "application/octet-stream", ContentClass.UNKNOWN

    for signature in _EXECUTABLE_SIGNATURES:
        if data.startswith(signature):
            return "application/x-executable", ContentClass.UNKNOWN

    for magic, offset, media_type, content_class in _SIGNATURES:
        if data[offset : offset + len(magic)] == magic:
            if media_type == "application/zip":
                return _identify_zip(data)
            if media_type == "audio/wav" and data[8:12] not in (b"WAVE", b"AVI "):
                continue
            return media_type, content_class

    head = data[:4096]
    if _SVG_SNIFF.search(head):
        return "image/svg+xml", ContentClass.IMAGE
    if _HTML_SNIFF.search(head):
        return "text/html", ContentClass.TEXT

    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream", ContentClass.UNKNOWN
    return "text/plain", ContentClass.TEXT


def _identify_zip(data: bytes) -> tuple[str, ContentClass]:
    """Distinguish an Office document from a plain archive.

    Reads the central directory only — names, not contents — so a malicious
    archive is never extracted in order to be identified.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist()[:MAX_ARCHIVE_ENTRIES])
    except (zipfile.BadZipFile, OSError):
        return "application/zip", ContentClass.ARCHIVE

    for marker, media_type in _OFFICE_MARKERS:
        if marker in names:
            return media_type, ContentClass.DOCUMENT
    return "application/zip", ContentClass.ARCHIVE


def archive_is_safe(data: bytes) -> tuple[bool, str | None]:
    """Check a zip's declared expansion before anything is extracted.

    The header states each entry's uncompressed size, so the ratio is knowable
    without decompressing. Trusting the header is safe here because it is used
    only to *refuse*: a bomb that under-reports its size is still caught by the
    extractor's own byte budget, and a bomb that reports honestly is caught here.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                return False, f"archive has {len(infos)} entries"
            total = 0
            for info in infos:
                name = info.filename
                if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                    # Zip-slip: an entry that writes outside the extraction root.
                    return False, f"archive entry escapes its root: {name!r}"
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    return False, "archive expands beyond the allowed size"
            compressed = max(1, len(data))
            if total / compressed > MAX_COMPRESSION_RATIO:
                return False, (
                    f"archive expansion ratio {total / compressed:.0f}:1 exceeds "
                    f"{MAX_COMPRESSION_RATIO:.0f}:1"
                )
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"archive is unreadable: {type(exc).__name__}"
    return True, None


def sanitise_svg(data: bytes) -> bytes:
    """Strip active content from an SVG, or refuse it.

    SVG executes script, so an SVG upload is an HTML upload wearing an image's
    name. Rather than attempt to rewrite hostile markup — which is a losing game
    against a determined attacker — anything with active content is refused.
    """
    if _SVG_ACTIVE.search(data):
        raise PolicyViolation("this SVG contains active content and was refused")
    return data


def inspect_upload(
    data: bytes,
    *,
    policy: UploadPolicy | None = None,
    declared_type: str | None = None,
    filename: str | None = None,
) -> UploadVerdict:
    """Decide whether these bytes may enter the system.

    Order matters and is not arbitrary: size before parsing, identity before
    trust, structure before extraction. Each step is cheap relative to the one
    after it, and each protects the next from input it cannot handle.
    """
    policy = policy or UploadPolicy()
    reasons: list[str] = []

    if not data:
        return UploadVerdict(False, "application/octet-stream", ContentClass.UNKNOWN,
                             0, ("the file was empty",))
    if len(data) > policy.max_bytes:
        return UploadVerdict(
            False, "application/octet-stream", ContentClass.UNKNOWN, len(data),
            (f"file exceeds the {policy.max_bytes // (1024 * 1024)}MB limit",),
        )

    media_type, content_class = sniff(data)

    if media_type == "application/x-executable":
        return UploadVerdict(False, media_type, ContentClass.UNKNOWN, len(data),
                             ("executable files are never accepted",))

    mismatched = False
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared and not _compatible(declared, media_type):
        mismatched = True
        reasons.append(f"content is {media_type}, not the declared {declared}")
    if filename:
        expected = _type_for_extension(filename)
        if expected and not _compatible(expected, media_type):
            mismatched = True
            reasons.append(f"content is {media_type}, but the name says {expected}")

    if mismatched and policy.reject_mismatch:
        return UploadVerdict(False, media_type, content_class, len(data),
                             tuple(reasons), mismatched=True)

    if content_class not in policy.allowed_classes:
        return UploadVerdict(
            False, media_type, content_class, len(data),
            (*reasons, f"{content_class.value} files are not accepted here"),
            mismatched=mismatched,
        )
    if policy.allowed_media_types and media_type not in policy.allowed_media_types:
        return UploadVerdict(
            False, media_type, content_class, len(data),
            (*reasons, f"{media_type} is not accepted here"), mismatched=mismatched,
        )

    if data.startswith(b"PK\x03\x04"):
        safe, problem = archive_is_safe(data)
        if not safe:
            return UploadVerdict(False, media_type, content_class, len(data),
                                 (*reasons, problem or "unsafe archive"),
                                 mismatched=mismatched)

    sanitised = False
    if media_type == "image/svg+xml":
        sanitise_svg(data)
        sanitised = True

    if policy.require_malware_scan:
        if policy.scanner is None:
            # The honest answer. A deployment that demands scanning and has no
            # scanner must fail, not quietly decide everything is clean.
            return UploadVerdict(
                False, media_type, content_class, len(data),
                (*reasons,
                 "malware scanning is required but no scanner is configured"),
                mismatched=mismatched,
            )
        findings = list(policy.scanner(data))  # type: ignore[operator]
        if findings:
            return UploadVerdict(
                False, media_type, content_class, len(data),
                (*reasons, f"malware scan flagged: {', '.join(findings[:3])}"),
                mismatched=mismatched,
            )

    return UploadVerdict(True, media_type, content_class, len(data), tuple(reasons),
                         mismatched=mismatched, sanitised=sanitised)


#: Text subtypes that plain bytes cannot distinguish from each other. Markdown,
#: CSV and JSON are all "some UTF-8"; there is no magic number for markdown, and
#: refusing `text/markdown` because the bytes merely look like text is a false
#: positive that blocks a legitimate upload.
#:
#: The mismatch check exists to catch *format confusion* — HTML wearing a PDF's
#: name, an executable wearing an image's — not to police subtypes that are
#: genuinely indistinguishable. Narrowing it here keeps the attack it was built
#: for refused while letting real documents through.
_TEXTUAL = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
        "application/csv",
        "application/x-ndjson",
    }
)


def _compatible(declared: str, sniffed: str) -> bool:
    """Whether a declared type is an acceptable label for what the bytes are."""
    if declared == sniffed:
        return True
    # Both plain text of some flavour: the bytes cannot tell us more.
    if declared in _TEXTUAL and sniffed in _TEXTUAL:
        return True
    # A generic declaration commits to nothing, which is what a browser sends
    # for a MediaRecorder blob and for many drag-and-drop uploads.
    return declared in {"application/octet-stream", "binary/octet-stream"}


_EXTENSION_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".zip": "application/zip",
}


def _type_for_extension(filename: str) -> str | None:
    lowered = filename.lower()
    for extension, media_type in _EXTENSION_TYPES.items():
        if lowered.endswith(extension):
            return media_type
    return None


def require_acceptable(
    data: bytes,
    *,
    policy: UploadPolicy | None = None,
    declared_type: str | None = None,
    filename: str | None = None,
) -> UploadVerdict:
    """Inspect and raise on rejection, for call sites that want an exception."""
    verdict = inspect_upload(
        data, policy=policy, declared_type=declared_type, filename=filename
    )
    verdict.raise_if_rejected()
    return verdict


def guard_text(value: str | None, *, limit: int, field: str) -> str | None:
    """Bound a free-text field before it reaches storage or a prompt.

    Unbounded user text is a cost problem (it becomes tokens), a storage problem
    and an injection surface. Refusing is better than truncating: a silently
    truncated title is a bug report nobody can reproduce.
    """
    if value is None:
        return None
    if len(value) > limit:
        raise ValidationFailed(f"{field} exceeds {limit} characters")
    if "\x00" in value:
        raise ValidationFailed(f"{field} contains a null byte")
    return value


__all__ = [
    "MAX_ARCHIVE_ENTRIES",
    "MAX_COMPRESSION_RATIO",
    "MAX_DOCUMENT_BYTES",
    "MAX_UNCOMPRESSED_BYTES",
    "MAX_UPLOAD_BYTES",
    "ContentClass",
    "UploadPolicy",
    "UploadVerdict",
    "archive_is_safe",
    "guard_text",
    "inspect_upload",
    "require_acceptable",
    "sanitise_svg",
    "sniff",
]
