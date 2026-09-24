"""P1-3 — parsing is inspected and bounded wherever it is entered from.

The audit found the bomb and mismatch defences living entirely in `api/app.py`.
That is one caller. The worker reaches ingestion from a queued job, and a
document arriving that way was parsed unexamined — so the protection covered the
path a reviewer looks at and not the path the bytes actually take.

It also found no bound on parsing itself. Every existing defence fires *before*
the parser: a size cap, magic-byte sniffing, a zip expansion-ratio check. None
of them constrain a well-formed file that takes four hours or twelve gigabytes,
which is the realistic attack — one upload, one worker gone, and the queue
backing up behind it.

Two fixes, both tested here:

* `IngestionService` runs `inspect_upload` on every byte, unconditionally
* parsing happens in a child process with a wall-clock kill and kernel memory
  limits, because you cannot interrupt a CPU-bound C extension from Python

The second point is why this is a subprocess rather than a `signal.alarm`: a
watchdog thread can observe an overrun but not stop it, and abandoning the
thread leaks it along with whatever it has allocated.
"""

from __future__ import annotations

import unittest
import zipfile
from io import BytesIO

from vtv.adapters.ingest.documents import ParserRegistry
from vtv.contracts.errors import ErrorCode, VTVError
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.ingest_sandbox import SandboxLimits, parse_in_sandbox
from vtv.observability.events import EventSink
from vtv.pipeline.ingestion import IngestionService

PROJECT = "prj_" + "a" * 24
GOOD = (
    b"# The Transistor\n\n"
    b"The transistor was invented in 1947 at Bell Labs.\n\n"
    b"It replaced the vacuum tube almost everywhere within twenty years.\n"
)


def service(**overrides: object) -> IngestionService:
    return IngestionService(
        registry=ParserRegistry(), events=EventSink(), **overrides  # type: ignore[arg-type]
    )


class InspectionHappensHereNotOnlyAtTheRoute(unittest.TestCase):
    """The guarantee moved to where every caller passes through."""

    def test_a_real_document_is_accepted(self) -> None:
        document, transcript, _context = service().ingest(
            GOOD,
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=PROJECT,
            filename="note.md",
        )
        self.assertTrue(document.blocks)
        self.assertTrue(transcript.segments)

    def test_a_declared_type_that_the_bytes_contradict_is_refused(self) -> None:
        """Format confusion, caught without the HTTP route being involved."""
        with self.assertRaises(VTVError) as caught:
            service().ingest(
                b"<html><body><p>not a pdf at all</p></body></html>",
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="report.pdf",
            )
        self.assertIs(caught.exception.info.code, ErrorCode.UPLOAD_REFUSED)

    def test_an_executable_is_never_parsed(self) -> None:
        with self.assertRaises(VTVError) as caught:
            service().ingest(
                b"\x7fELF\x02\x01\x01" + b"\x00" * 128,
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="report.docx",
            )
        self.assertIs(caught.exception.info.code, ErrorCode.UPLOAD_REFUSED)

    def test_an_empty_file_is_refused_before_anything_runs(self) -> None:
        with self.assertRaises(VTVError):
            service().ingest(
                b"",
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="empty.md",
            )

    def test_an_oversized_document_is_refused_by_the_document_ceiling(self) -> None:
        """Not the 200MB upload ceiling. Documents are text."""
        from vtv.security.uploads import MAX_DOCUMENT_BYTES

        with self.assertRaises(VTVError) as caught:
            service().ingest(
                b"#\n" + b"a" * (MAX_DOCUMENT_BYTES + 1),
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="huge.md",
            )
        self.assertIs(caught.exception.info.code, ErrorCode.UPLOAD_REFUSED)

    def test_a_zip_bomb_is_refused(self) -> None:
        """DOCX and PPTX are zips, so this is a live path, not a hypothetical."""
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"\x00" * (600 * 1024 * 1024))
        with self.assertRaises(VTVError) as caught:
            service().ingest(
                buffer.getvalue(),
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="bomb.docx",
            )
        self.assertIs(caught.exception.info.code, ErrorCode.UPLOAD_REFUSED)

    def test_a_refusal_is_announced_not_only_raised(self) -> None:
        """A tenant suddenly generating refusals is someone probing parsers."""
        seen: list[str] = []
        events = EventSink()
        events.subscribe(lambda event: seen.append(event.name.value))
        subject = IngestionService(registry=ParserRegistry(), events=events)

        with self.assertRaises(VTVError):
            subject.ingest(
                b"<html><body>x</body></html>",
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="report.pdf",
            )
        self.assertIn("document.refused", seen)

    def test_the_refusal_event_carries_no_document_content(self) -> None:
        """The reasons name shapes. The bytes are attacker-controlled."""
        captured: list[object] = []
        events = EventSink()
        events.subscribe(lambda event: captured.append(event.data))
        subject = IngestionService(registry=ParserRegistry(), events=events)

        secret = b"<html><body>PATIENT NAME ALICE SMITH</body></html>"
        with self.assertRaises(VTVError):
            subject.ingest(
                secret,
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="report.pdf",
            )
        self.assertNotIn("ALICE", str(captured))


class ParsingIsBounded(unittest.TestCase):
    def test_a_document_parses_normally_inside_the_sandbox(self) -> None:
        result = parse_in_sandbox(
            GOOD,
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=PROJECT,
            filename="note.md",
        )
        self.assertTrue(result.document.blocks)
        self.assertEqual(result.document.organisation_id, SYSTEM_ORGANISATION_ID)

    def test_the_result_crosses_the_process_boundary_intact(self) -> None:
        """Structure, not just text: tables are the expensive part to extract."""
        source = (
            b"# Growth\n\n"
            b"| Year | Users |\n|---|---|\n| 2019 | 100 |\n| 2024 | 900 |\n"
        )
        result = parse_in_sandbox(
            source,
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id=PROJECT,
            filename="growth.md",
        )
        tables = result.document.tables()
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].headers, ["Year", "Users"])

    def test_an_overrun_is_stopped_rather_than_observed(self) -> None:
        """The property a thread-based timeout cannot provide.

        A one-second ceiling against a parser that will not finish: the child is
        killed and the caller gets a domain error, not a hung worker.
        """
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            # Deeply nested, highly repetitive XML: cheap to build, expensive to
            # walk, and structurally valid so nothing earlier refuses it.
            archive.writestr(
                "word/document.xml",
                b"<w:document><w:body>" + b"<w:p><w:r><w:t>x</w:t></w:r></w:p>" * 200_000
                + b"</w:body></w:document>",
            )
            archive.writestr("[Content_Types].xml", b"<Types/>")

        with self.assertRaises(VTVError) as caught:
            parse_in_sandbox(
                buffer.getvalue(),
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="slow.docx",
                limits=SandboxLimits(timeout_seconds=1.0),
            )
        self.assertIs(caught.exception.info.code, ErrorCode.SCHEMA_INVALID)

    def test_a_memory_ceiling_is_asked_of_the_kernel(self) -> None:
        """Not enforced in Python, which cannot enforce it."""
        with self.assertRaises(VTVError):
            parse_in_sandbox(
                GOOD,
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="note.md",
                # Too small for an interpreter to even start. The point is that
                # the limit is real and applied, not that this number is sane.
                limits=SandboxLimits(memory_bytes=1024 * 1024),
            )

    def test_a_failure_message_never_leaks_parser_internals(self) -> None:
        """Stack traces carry file paths and library versions."""
        with self.assertRaises(VTVError) as caught:
            parse_in_sandbox(
                b"%PDF-1.4\nnot really a pdf",
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="broken.pdf",
            )
        message = str(caught.exception)
        for leak in ("Traceback", "site-packages", "/home/", ".py:"):
            with self.subTest(leak):
                self.assertNotIn(leak, message)

    def test_the_child_does_not_inherit_provider_credentials(self) -> None:
        """A parser has no business reaching a provider.

        A library that decides to fetch a remote resource must not find an API
        key waiting for it.
        """
        from vtv.ingest_sandbox import _child_environment

        environment = _child_environment()
        self.assertFalse([name for name in environment if name.startswith("VTV_")])

    def test_the_cpu_ceiling_sits_below_the_wall_clock(self) -> None:
        """So a spinning parse is stopped by the kernel, not by our timer."""
        limits = SandboxLimits(timeout_seconds=30.0)
        self.assertLess(limits.cpu_seconds(), limits.timeout_seconds)
        self.assertGreaterEqual(limits.cpu_seconds(), 1)


class TheSandboxIsOnByDefault(unittest.TestCase):
    def test_the_service_sandboxes_unless_told_otherwise(self) -> None:
        self.assertTrue(service().sandboxed)

    def test_wiring_does_not_switch_it_off(self) -> None:
        """The whole class of defect the audit found is "correct code, not

        reached". A default that production overrides is the same shape.
        """
        import inspect

        from vtv import wiring

        source = inspect.getsource(wiring)
        self.assertNotIn("sandboxed=False", source)

    def test_the_unsandboxed_path_still_inspects(self) -> None:
        """Turning off the resource bound must not turn off the content check."""
        with self.assertRaises(VTVError) as caught:
            service(sandboxed=False).ingest(
                b"<html><body>x</body></html>",
                organisation_id=SYSTEM_ORGANISATION_ID,
                project_id=PROJECT,
                filename="report.pdf",
            )
        self.assertIs(caught.exception.info.code, ErrorCode.UPLOAD_REFUSED)


class WhatThisDoesNotProtectAgainst(unittest.TestCase):
    """Stated as a test so the limitation is not quietly forgotten.

    The child runs as the same user with the same filesystem and network. This
    bounds resource exhaustion — time, memory, output size — and nothing else.
    Containment of a genuine parser RCE comes from the container's non-root
    user and its network policy, neither of which this module provides.
    """

    def test_the_module_says_so_in_its_own_documentation(self) -> None:
        import vtv.ingest_sandbox as module

        doc = module.__doc__ or ""
        self.assertIn("not a security sandbox", doc.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
