"""The local trace: what it records, and what it refuses to.

## Why this exists

A user rendered a 76-second video and was charged two dollars across 64
requests, with no way to find out which calls those were. The cost ledger and
the event stream both had pieces of the answer and neither could show the
prompt, because `observability/events.py` forbids user content in an event —
correctly, since events reach logs, metrics and eventually a transport.

So the trace is a separate facility with different rules, and these tests pin
down both halves of that: it records enough to answer the question, and it
refuses to exist in production.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.testing import FailingProvider, StubImageGenerationProvider
from vtv.config import Settings
from vtv.contracts.base import Budget
from vtv.contracts.errors import VTVError
from vtv.contracts.generation import GenerationKind, GenerationRequest, ImageParams
from vtv.contracts.style import AspectRatio
from vtv.observability.events import EventSink
from vtv.observability.trace import NullTrace, ProviderTrace, build_trace, traced
from vtv.pipeline.generation import GenerationRouter

ORG = "org_0000000000000000000000"
PROJECT = "prj_0000000000000000000001"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TraceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-trace-")
        root = Path(self._dir.name)
        self.path = root / "provider-calls.jsonl"
        self.storage = LocalStorageProvider(root=root / "s", signing_key="k")
        self.trace = ProviderTrace(self.path, echo=False)
        self.router = GenerationRouter(events=EventSink(), trace=self.trace)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def request(self, prompt: str = "An empty meeting room at dawn.") -> GenerationRequest:
        return GenerationRequest(
            organisation_id=ORG,
            project_id=PROJECT,
            kind=GenerationKind.IMAGE,
            params=ImageParams(prompt=prompt, aspect_ratio=AspectRatio.LANDSCAPE_16_9),
            budget=Budget(max_cost_usd=1.0),
        )

    def lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]


class ItRecordsEnoughToAnswerTheQuestion(TraceCase):
    def test_a_successful_call_is_written_with_its_cost_and_prompt(self) -> None:
        self.router.register(
            StubImageGenerationProvider(storage=self.storage), GenerationKind.IMAGE
        )
        run(self.router.generate(self.request()))

        rows = self.lines()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "image")
        self.assertEqual(row["outcome"], "ok")
        self.assertGreater(row["cost_usd"], 0)
        self.assertEqual(row["project_id"], PROJECT)
        self.assertIn("meeting room", row["request"]["prompt"])

    def test_a_cache_hit_is_recorded_as_free_rather_than_omitted(self) -> None:
        """Sixty-four calls of which forty were cached is a different problem
        from sixty-four calls that all cost money."""
        self.router.register(
            StubImageGenerationProvider(storage=self.storage), GenerationKind.IMAGE
        )
        run(self.router.generate(self.request()))
        run(self.router.generate(self.request()))

        rows = self.lines()
        self.assertEqual([r["outcome"] for r in rows], ["ok", "cached"])
        self.assertEqual(rows[1]["cost_usd"], 0.0)
        self.assertTrue(rows[1]["from_cache"])

    def test_a_failure_records_the_attempt_and_the_vendors_message(self) -> None:
        self.router.register(FailingProvider(), GenerationKind.IMAGE)
        with self.assertRaises(VTVError):
            run(self.router.generate(self.request()))

        rows = self.lines()
        self.assertTrue(rows)
        self.assertTrue(all(r["outcome"] in {"failed", "refused"} for r in rows))
        self.assertTrue(any(r.get("error") for r in rows))

    def test_context_attributes_a_call_to_a_visual_and_a_job(self) -> None:
        """A flat list of forty calls cannot show that eleven were one shot."""
        self.router.register(
            StubImageGenerationProvider(storage=self.storage), GenerationKind.IMAGE
        )
        with self.trace.job("render_scope", "job_1"), traced(unit="vun_abc"):
            run(self.router.generate(self.request()))

        row = self.lines()[0]
        self.assertEqual(row["job"], "job_1")
        self.assertEqual(row["job_kind"], "render_scope")
        self.assertEqual(row["unit"], "vun_abc")

    def test_concurrent_visuals_do_not_borrow_each_others_context(self) -> None:
        """Four shots are sourced at once. An attribute would cross the wires."""
        self.router.register(
            StubImageGenerationProvider(storage=self.storage), GenerationKind.IMAGE
        )

        async def one(unit: str, prompt: str) -> None:
            with traced(unit=unit):
                await self.router.generate(self.request(prompt))

        async def both() -> None:
            await asyncio.gather(
                one("vun_a", "a lighthouse"), one("vun_b", "a harbour")
            )

        run(both())
        by_unit = {r["unit"]: r["request"]["prompt"] for r in self.lines()}
        self.assertEqual(by_unit["vun_a"], "a lighthouse")
        self.assertEqual(by_unit["vun_b"], "a harbour")


class TheReportReadsIt(TraceCase):
    def test_it_totals_by_kind_and_provider(self) -> None:
        from vtv.trace_report import load, summarise

        self.router.register(
            StubImageGenerationProvider(storage=self.storage), GenerationKind.IMAGE
        )
        run(self.router.generate(self.request("one")))
        run(self.router.generate(self.request("two")))

        text = summarise(load(self.path))
        self.assertIn("WHERE THE MONEY WENT", text)
        self.assertIn("stub-image", text)
        self.assertIn("2 provider call(s)", text)

    def test_an_empty_or_missing_file_is_said_plainly(self) -> None:
        from vtv.trace_report import load, summarise

        self.assertIn("empty", summarise([]))
        with self.assertRaises(SystemExit):
            load(self.path.parent / "nothing.jsonl")


class ItRefusesToExistInProduction(unittest.TestCase):
    """A file of user prompts on a server is an incident waiting for a disk image."""

    def _settings(self, **over: object) -> Settings:
        with TemporaryDirectory(prefix="vtv-cfg-") as scratch:
            root = Path(scratch)
            fields: dict[str, object] = {
                "storage_root": root / "storage",
                "database_url": f"sqlite:///{root / 'v.db'}",
                "signing_key": "test-signing-key-not-a-real-secret",
            }
            fields.update(over)
            # A literal keyword, not part of the dict: the guard in
            # `TheSuiteNeverReachesTheInternet` reads the source and a
            # `**fields` splat is opaque to it. Satisfying the checker rather
            # than working around it is the point — the next person to copy
            # this helper inherits the guard.
            return Settings(asset_search_endpoint="", **fields)  # type: ignore[arg-type]

    def test_off_by_default(self) -> None:
        self.assertIsInstance(build_trace(self._settings()), NullTrace)

    def test_on_when_asked_for_in_development(self) -> None:
        trace = build_trace(self._settings(trace_provider_calls=True, env="development"))
        self.assertIsInstance(trace, ProviderTrace)

    def test_refused_in_production_even_when_asked_for(self) -> None:
        trace = build_trace(self._settings(trace_provider_calls=True, env="production"))
        self.assertIsInstance(trace, NullTrace)

    def test_the_null_trace_still_carries_job_context(self) -> None:
        """So the same `with` blocks work whether or not tracing is on."""
        from vtv.observability.trace import context

        with NullTrace().job("render_scope", "job_9"):
            self.assertEqual(context()["job"], "job_9")


class PromptsStayOutOfTheEventStream(unittest.TestCase):
    """The rule the trace exists to avoid breaking."""

    def test_no_generation_event_carries_prompt_text(self) -> None:
        events = EventSink()
        events.record = True
        with TemporaryDirectory(prefix="vtv-ev-") as scratch:
            storage = LocalStorageProvider(root=Path(scratch), signing_key="k")
            router = GenerationRouter(events=events, trace=NullTrace())
            router.register(
                StubImageGenerationProvider(storage=storage), GenerationKind.IMAGE
            )
            run(
                router.generate(
                    GenerationRequest(
                        organisation_id=ORG,
                        project_id=PROJECT,
                        kind=GenerationKind.IMAGE,
                        params=ImageParams(
                            prompt="a distinctive phrase nobody would log",
                            aspect_ratio=AspectRatio.LANDSCAPE_16_9,
                        ),
                        budget=Budget(max_cost_usd=1.0),
                    )
                )
            )
        blob = json.dumps([e.model_dump(mode="json") for e in events.recorded])
        self.assertNotIn("distinctive phrase", blob)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
