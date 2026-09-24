"""P1-6 — events become metrics, and every log line carries its trace.

The system already emitted a rich event for everything interesting. What it did
not do was *aggregate*: there was no way to ask "what fraction of renders are
degraded this hour" without reading every event, and no way to ask it at all
from a dashboard.

Rather than instrument every call site a second time — which drifts, because the
two instrumentations are edited by different changes — this subscribes to the
existing event stream and derives the counters from it. One place to read, one
place to change, and a new event automatically reaches the metric it belongs to
or is visibly missing from this table.

The trade is honest: metrics derived from events inherit the events' coverage.
An operation that emits no event produces no metric. That is a real limitation
and it is the reason `metrics_handler` is a *supplement* to direct
instrumentation at the HTTP and job boundaries, not a replacement for it.
"""

from __future__ import annotations

import sys
from typing import Any

from vtv.observability.context import current_fields
from vtv.observability.events import Event, EventHandler, EventName
from vtv.observability.metrics import Metrics


def metrics_handler(metrics: Metrics) -> EventHandler:
    """Derive counters and histograms from the event stream."""

    def handle(event: Event) -> None:
        name = event.name
        data = event.data

        if name is EventName.GENERATION_COMPLETED:
            metrics.increment(
                "vtv_provider_calls_total",
                kind=data.get("kind"),
                outcome="cached" if data.get("cached") else "success",
            )
            _observe_duration(metrics, event, kind=data.get("kind"))
            if event.cost_usd:
                metrics.increment(
                    "vtv_provider_cost_usd_total",
                    float(event.cost_usd),
                    kind=data.get("kind"),
                )
            metrics.increment(
                "vtv_cache_lookups_total",
                outcome="hit" if data.get("cached") else "miss",
            )
        elif name is EventName.GENERATION_FAILED:
            metrics.increment(
                "vtv_provider_calls_total",
                kind=data.get("kind"),
                outcome="failure",
            )
            _observe_duration(metrics, event, kind=data.get("kind"))
            metrics.increment("vtv_errors_total", code=data.get("code", "unknown"))

        elif name is EventName.DEGRADED:
            metrics.increment(
                "vtv_degradations_total", strategy=data.get("from_strategy")
            )
        elif name is EventName.GROUNDING_REFUSED:
            metrics.increment(
                "vtv_grounding_refusals_total", primitive=data.get("primitive")
            )
        elif name is EventName.DOCUMENT_REFUSED:
            metrics.increment("vtv_uploads_refused_total", reason="inspection")

        elif name is EventName.RENDER_COMPLETED:
            metrics.increment(
                "vtv_renders_total", outcome=data.get("outcome", "success")
            )
            _observe(metrics, "vtv_job_duration_seconds", event, kind="render")
        elif name is EventName.RENDER_FAILED:
            metrics.increment("vtv_renders_total", outcome="failed")
            metrics.increment("vtv_errors_total", code=data.get("code", "unknown"))

        elif name is EventName.JOB_COMPLETED:
            metrics.increment(
                "vtv_jobs_total", kind=data.get("kind"), outcome="success"
            )
            _observe(metrics, "vtv_job_duration_seconds", event, kind=data.get("kind"))
        elif name is EventName.JOB_FAILED:
            metrics.increment(
                "vtv_jobs_total",
                kind=data.get("kind"),
                outcome="dead_letter" if data.get("dead_letter") else "failure",
            )
            metrics.increment("vtv_errors_total", code=data.get("code", "unknown"))

        elif name is EventName.STAGE_FAILED:
            metrics.increment("vtv_errors_total", code=data.get("code", "unknown"))

    return handle


def _observe(metrics: Metrics, name: str, event: Event, **labels: Any) -> None:
    if event.duration_ms is None:
        return
    metrics.observe(name, event.duration_ms / 1000.0, **labels)


def _observe_duration(metrics: Metrics, event: Event, **labels: Any) -> None:
    _observe(metrics, "vtv_provider_duration_seconds", event, **labels)


def correlated_log_handler(stream: Any = None) -> EventHandler:
    """One JSON object per line, with the trace fields merged in.

    The audit's actual complaint: an operator could not follow one render across
    the API, the queue and the worker, because no field was common to all three.
    Every line this emits carries `trace_id`, so the answer to "what happened to
    this video" is one query.
    """
    target = stream or sys.stdout

    def handle(event: Event) -> None:
        payload = event.model_dump(mode="json")
        fields = current_fields()
        if fields:
            # Under a reserved key rather than merged flat, so an event that
            # happens to carry a `request_id` in its own data cannot silently
            # overwrite the correlation — or be mistaken for it.
            payload["correlation"] = fields
        print(_dumps(payload), file=target, flush=True)

    return handle


def _dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


__all__ = ["correlated_log_handler", "metrics_handler"]
