"""P1-6 — an operator can answer "why did this customer's video fail?".

The audit scored observability 38/100 and put the finding as a question, which
is the right way to score it: an on-call engineer could not follow one render
across an HTTP request, a queue and a worker, because those three wrote logs
with no field in common. Everything the system knew about itself was an *event*
— excellent for explaining one render after you have found it, useless for
finding it, and useless for "is this affecting one customer or all of them".

Three things are tested here:

1. **Correlation crosses the process boundary.** There is no implicit
   propagation between processes, so the trace travels in the job payload. This
   is the specific gap the audit described.
2. **Metrics exist, and are bounded.** A metric labelled with a project id has
   one series per project, and at ten thousand projects the scrape times out —
   so the registry collapses unbounded labels into `"other"` rather than
   growing without limit. That is a real loss of detail and the correct trade.
3. **Nothing observable is a disclosure channel.** `/metrics` is scraped
   without a credential. Every series must therefore be aggregate: labelled by
   route, kind, outcome or error code, never by tenant, project or user.
"""

from __future__ import annotations

import asyncio
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from vtv.api.app import create_app
from vtv.config import Settings
from vtv.observability.bridge import correlated_log_handler, metrics_handler
from vtv.observability.context import (
    bind,
    correlated,
    current,
    current_fields,
    sanitise,
)
from vtv.observability.events import EventName, EventSink
from vtv.observability.metrics import MAX_LABEL_VALUES, Metrics, register_defaults
from vtv.wiring import build


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class CorrelationTravels(unittest.TestCase):
    def test_a_block_has_a_trace_and_the_outside_does_not(self) -> None:
        self.assertIsNone(current())
        with correlated() as correlation:
            self.assertEqual(current(), correlation)
            self.assertTrue(correlation.trace_id)
        self.assertIsNone(current())

    def test_a_nested_block_keeps_the_outer_trace(self) -> None:
        """A worker adding a job id must not start a second trace."""
        with (
            correlated(trace_id="trace-1", request_id="req-1"),
            correlated(job_id="job-9") as inner,
        ):
            self.assertEqual(inner.trace_id, "trace-1")
            self.assertEqual(inner.request_id, "req-1")
            self.assertEqual(inner.job_id, "job-9")

    def test_it_survives_an_await(self) -> None:
        """`contextvars`, not a parameter threaded through forty functions."""

        async def inner() -> str | None:
            await asyncio.sleep(0)
            correlation = current()
            return correlation.trace_id if correlation else None

        async def outer() -> str | None:
            with correlated(trace_id="trace-across-await"):
                return await inner()

        self.assertEqual(run(outer()), "trace-across-await")

    def test_a_field_learned_later_can_be_bound(self) -> None:
        """The tenant is known only after authentication, inside the block."""
        with correlated(trace_id="t"):
            bind(organisation_id="org_abc")
            self.assertEqual(current_fields()["organisation_id"], "org_abc")

    def test_binding_never_overwrites_what_is_already_set(self) -> None:
        with correlated(trace_id="t", organisation_id="org_real"):
            bind(organisation_id="org_attacker")
            self.assertEqual(current_fields()["organisation_id"], "org_real")

    def test_unset_fields_are_omitted_rather_than_null(self) -> None:
        """An empty field on every line is noise on every line."""
        with correlated(trace_id="t"):
            self.assertEqual(current_fields(), {"trace_id": "t"})


class AnInboundIdentifierIsHostile(unittest.TestCase):
    """`X-Request-Id` is attacker-controlled and reaches logs and metrics."""

    def test_a_reasonable_identifier_is_honoured(self) -> None:
        self.assertEqual(sanitise("req-abc.123_X"), "req-abc.123_X")

    def test_a_log_forging_attempt_is_refused(self) -> None:
        for evil in ("a\nlevel=ERROR fake", 'a" injected="', "a\rb", "a\x00b"):
            with self.subTest(evil):
                self.assertIsNone(sanitise(evil))

    def test_an_absurdly_long_identifier_is_refused_not_truncated(self) -> None:
        """Truncating produces an id that matches nothing on either side."""
        self.assertIsNone(sanitise("a" * 4096))

    def test_a_refused_identifier_means_we_mint_our_own(self) -> None:
        with correlated(trace_id=sanitise("a\nb")) as correlation:
            self.assertTrue(correlation.trace_id)
            self.assertNotIn("\n", correlation.trace_id)


class MetricsAreBounded(unittest.TestCase):
    def setUp(self) -> None:
        self.metrics = Metrics()

    def test_a_counter_counts(self) -> None:
        self.metrics.counter("vtv_test_total", "help")
        self.metrics.increment("vtv_test_total", route="/health")
        self.metrics.increment("vtv_test_total", route="/health")
        self.assertIn('vtv_test_total{route="/health"} 2', self.metrics.render())

    def test_a_histogram_reports_buckets_a_sum_and_a_count(self) -> None:
        self.metrics.histogram("vtv_test_seconds", "help")
        self.metrics.observe("vtv_test_seconds", 0.3)
        self.metrics.observe("vtv_test_seconds", 7.0)
        rendered = self.metrics.render()
        self.assertIn("vtv_test_seconds_count 2", rendered)
        self.assertIn("vtv_test_seconds_sum 7.3", rendered)
        self.assertIn('le="+Inf"', rendered)

    def test_an_unbounded_label_collapses_instead_of_growing(self) -> None:
        """The production incident this prevents.

        One series per project means the scrape times out at scale. Collapsing
        to "other" loses detail and keeps the metric answering "how many";
        per-project detail belongs in events, which are already per-project.
        """
        self.metrics.counter("vtv_test_total", "help")
        for index in range(MAX_LABEL_VALUES * 3):
            self.metrics.increment("vtv_test_total", project=f"prj_{index}")
        rendered = self.metrics.render()
        self.assertIn('project="other"', rendered)
        self.assertLessEqual(
            rendered.count("vtv_test_total{"), MAX_LABEL_VALUES + 1
        )

    def test_declared_metrics_read_as_zero_before_anything_happens(self) -> None:
        """"No data" and "no errors" must not look identical on a dashboard."""
        rendered = register_defaults(Metrics()).render()
        self.assertIn("# TYPE vtv_errors_total counter", rendered)
        self.assertIn("# TYPE vtv_renders_total counter", rendered)

    def test_a_label_value_cannot_break_the_exposition_format(self) -> None:
        self.metrics.counter("vtv_test_total", "help")
        self.metrics.increment("vtv_test_total", route='a"b\nc')
        rendered = self.metrics.render()
        # One sample line, not two: a raw newline would split it and corrupt
        # every metric after it in the scrape.
        samples = [line for line in rendered.splitlines() if line.startswith("vtv_")]
        self.assertEqual(len(samples), 1, samples)
        self.assertIn(r"\"", samples[0])

    def test_redeclaring_a_metric_with_a_different_type_is_refused(self) -> None:
        self.metrics.counter("vtv_test_total", "help")
        with self.assertRaises(ValueError):
            self.metrics.histogram("vtv_test_total", "help")


class EventsBecomeMetrics(unittest.TestCase):
    """Derived from the event stream so there is one thing to keep correct."""

    def setUp(self) -> None:
        self.metrics = register_defaults(Metrics())
        self.events = EventSink()
        self.events.subscribe(metrics_handler(self.metrics))

    def test_a_degraded_render_is_counted_as_degraded(self) -> None:
        self.events.emit(
            EventName.RENDER_COMPLETED, project_id="p", data={"outcome": "degraded"}
        )
        self.assertIn(
            'vtv_renders_total{outcome="degraded"} 1', self.metrics.render()
        )

    def test_provider_spend_accumulates(self) -> None:
        for _ in range(3):
            self.events.emit(
                EventName.GENERATION_COMPLETED,
                project_id="p",
                cost_usd=0.02,
                duration_ms=250,
                data={"kind": "image"},
            )
        rendered = self.metrics.render()
        self.assertIn('vtv_provider_cost_usd_total{kind="image"} 0.06', rendered)
        self.assertIn('vtv_provider_duration_seconds_count{kind="image"} 3', rendered)

    def test_a_cache_hit_is_distinguished_from_a_call(self) -> None:
        self.events.emit(
            EventName.GENERATION_COMPLETED,
            project_id="p",
            data={"kind": "image", "cached": True},
        )
        rendered = self.metrics.render()
        self.assertIn('vtv_cache_lookups_total{outcome="hit"} 1', rendered)

    def test_a_grounding_refusal_is_visible_as_a_number(self) -> None:
        """A rising refusal rate is a model getting worse, quietly."""
        self.events.emit(
            EventName.GROUNDING_REFUSED, project_id="p", data={"primitive": "chart"}
        )
        self.assertIn(
            'vtv_grounding_refusals_total{primitive="chart"} 1',
            self.metrics.render(),
        )

    def test_an_event_with_no_metric_is_harmless(self) -> None:
        self.events.emit(EventName.RENDER_PROGRESS, project_id="p", data={})
        self.assertTrue(self.metrics.render())


class LogsCarryTheTrace(unittest.TestCase):
    def test_a_line_written_inside_a_trace_names_it(self) -> None:
        stream = StringIO()
        events = EventSink()
        events.subscribe(correlated_log_handler(stream))
        with correlated(trace_id="trace-77", organisation_id="org_abc"):
            events.emit(EventName.RENDER_STARTED, project_id="p", data={})
        written = stream.getvalue()
        self.assertIn("trace-77", written)
        self.assertIn("org_abc", written)

    def test_the_correlation_cannot_be_forged_by_event_data(self) -> None:
        """An event carrying its own `trace_id` must not masquerade as one."""
        stream = StringIO()
        events = EventSink()
        events.subscribe(correlated_log_handler(stream))
        with correlated(trace_id="real"):
            events.emit(
                EventName.RENDER_STARTED, project_id="p", data={"trace_id": "fake"}
            )
        import json

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["correlation"]["trace_id"], "real")


class TheHttpSurfaceIsInstrumented(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-observability-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
        )
        self.assembly = build(self.settings)
        self.client = TestClient(create_app(self.settings, assembly=self.assembly))

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def test_every_response_carries_a_request_id(self) -> None:
        """So a user reporting a problem can quote something searchable."""
        response = self.client.get("/health/live")
        self.assertTrue(response.headers.get("x-request-id"))

    def test_an_inbound_request_id_is_echoed_so_tracing_joins_up(self) -> None:
        response = self.client.get(
            "/health/live", headers={"X-Request-Id": "caller-123"}
        )
        self.assertEqual(response.headers["x-request-id"], "caller-123")

    def test_a_forged_request_id_is_replaced_not_echoed(self) -> None:
        response = self.client.get(
            "/health/live", headers={"X-Request-Id": 'x" fake="1'}
        )
        self.assertNotIn('"', response.headers["x-request-id"])

    def test_requests_are_counted_by_route_template_not_by_path(self) -> None:
        """`/v1/projects/{project_id}` is one series. The path is one per project."""
        for _ in range(3):
            project_id = self.client.post("/v1/projects", json={}).json()["project_id"]
            self.client.get(f"/v1/projects/{project_id}")

        rendered = self.client.get("/metrics").text
        self.assertIn('route="/v1/projects/{project_id}"', rendered)
        self.assertNotIn("prj_", rendered)

    def test_latency_is_recorded(self) -> None:
        self.client.get("/health/live")
        self.assertIn(
            "vtv_http_request_duration_seconds_count", self.client.get("/metrics").text
        )

    def test_the_scrape_endpoint_speaks_prometheus(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("# TYPE vtv_http_requests_total counter", response.text)

    def test_queue_depth_is_sampled_at_scrape_time(self) -> None:
        """A gauge maintained by increments drifts when a process dies."""
        self.assertIn("vtv_queue_depth", self.client.get("/metrics").text)

    def test_the_scrape_discloses_nothing_about_whose_data_this_is(self) -> None:
        """It is unauthenticated, like the probes, so it must be aggregate."""
        project_id = self.client.post(
            "/v1/projects", json={"title": "Secret Acquisition Plan"}
        ).json()["project_id"]
        self.client.get(f"/v1/projects/{project_id}")

        rendered = self.client.get("/metrics").text
        self.assertNotIn(project_id, rendered)
        self.assertNotIn("Secret Acquisition", rendered)
        self.assertNotIn("organisation_id", rendered)


class TheTraceCrossesTheQueue(unittest.TestCase):
    """The gap the audit actually described.

    There is no implicit propagation between processes, so the trace travels in
    the job payload — and must not become something the handler has to know
    about, because handlers validate their payload with `extra="forbid"`.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-queue-trace-")
        from vtv.adapters.queue.durable import DurableJobQueue

        self.queue = DurableJobQueue(
            Path(self._dir.name) / "queue.db", events=EventSink()
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_the_enqueuing_trace_is_seen_by_the_handler(self) -> None:
        seen: dict[str, object] = {}

        async def handler(payload: dict[str, object]) -> str:
            correlation = current()
            seen["trace_id"] = correlation.trace_id if correlation else None
            seen["job_id"] = correlation.job_id if correlation else None
            seen["payload"] = payload
            return "done"

        self.queue.register("demo", handler)
        with correlated(trace_id="trace-across-the-queue"):
            run(self.queue.enqueue(kind="demo", payload={"a": 1}))
        run(self.queue.drain(timeout=10.0))

        self.assertEqual(seen["trace_id"], "trace-across-the-queue")
        self.assertTrue(seen["job_id"])

    def test_the_handler_never_sees_the_correlation_key(self) -> None:
        """Handlers validate with `extra="forbid"`, and are right to."""
        seen: dict[str, object] = {}

        async def handler(payload: dict[str, object]) -> str:
            seen["payload"] = dict(payload)
            return "done"

        self.queue.register("demo", handler)
        with correlated(trace_id="t"):
            run(self.queue.enqueue(kind="demo", payload={"a": 1}))
        run(self.queue.drain(timeout=10.0))

        self.assertEqual(seen["payload"], {"a": 1})

    def test_a_job_enqueued_outside_a_trace_still_runs(self) -> None:
        ran: list[bool] = []

        async def handler(_: dict[str, object]) -> str:
            ran.append(True)
            return "done"

        self.queue.register("demo", handler)
        run(self.queue.enqueue(kind="demo", payload={"a": 1}))
        run(self.queue.drain(timeout=10.0))
        self.assertEqual(ran, [True])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
