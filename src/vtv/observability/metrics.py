"""P1-6 — numbers an operator can page on.

The audit scored observability 38/100, and the specific gap was that everything
this system knew about itself was an *event*: a rich, structured record of one
thing that happened. Events are the right primitive for "explain this render"
and the wrong one for "is the queue growing", "what fraction of renders are
degraded", "which provider got slow at 04:12". Those need counters and
histograms, aggregated, cheap to scrape, and present whether or not anything
interesting happened.

## Prometheus text format, no dependency

`prometheus_client` is the obvious choice and is not installable here. The
exposition format is a documented, stable, line-oriented text format, so this
module implements it directly rather than pretending the requirement does not
exist. If the library becomes available, this is the seam it replaces: the
`Metrics` API is deliberately the same shape.

## Cardinality is a production incident waiting to happen

A metric labelled with a project id has one series per project. At ten thousand
projects that is ten thousand series per metric, and the scrape times out.

So labels here are **bounded by construction**: `MAX_LABEL_VALUES` per label
name, past which new values collapse into `"other"` rather than being recorded.
That is a real loss of detail and it is the correct trade — a metric that
degrades to `"other"` still answers "how many", while a metric that takes the
scraper down answers nothing. Detail belongs in events, which are already
per-project and already structured.

**Never label with:** project id, job id, user id, request id, filename, or
anything else unbounded. `tenant` is deliberately absent for the same reason;
per-tenant numbers come from the usage meter, which is a database and can hold
them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

#: Past this many distinct values for one label, further values become "other".
#: Chosen to comfortably cover every enum this system labels with — stage,
#: outcome, provider, error code — while stopping an unbounded one dead.
MAX_LABEL_VALUES = 64

#: Seconds. Covers a fast API request through to a long render, so both are
#: legible in the same histogram without a second metric.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.025, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0,
)

_LabelKey = tuple[tuple[str, str], ...]


@dataclass
class _Series:
    help_text: str
    kind: str
    values: dict[_LabelKey, float] = field(default_factory=dict)
    #: Histograms only: bucket counts and the running sum, per label set.
    buckets: dict[_LabelKey, list[float]] = field(default_factory=dict)
    sums: dict[_LabelKey, float] = field(default_factory=dict)
    counts: dict[_LabelKey, float] = field(default_factory=dict)
    bucket_bounds: tuple[float, ...] = DEFAULT_BUCKETS


class Metrics:
    """A tiny, thread-safe, bounded metrics registry.

    Thread-safe because the worker runs several jobs concurrently and the API
    serves requests on a thread pool; an unsynchronised `dict[...] += 1` loses
    increments under exactly the load where the number starts mattering.
    """

    def __init__(self, *, max_label_values: int = MAX_LABEL_VALUES) -> None:
        self._lock = threading.Lock()
        self._series: dict[str, _Series] = {}
        self._seen: dict[tuple[str, str], set[str]] = {}
        self._max_label_values = max_label_values

    # -- declaration ------------------------------------------------------

    def counter(self, name: str, help_text: str) -> None:
        self._declare(name, help_text, "counter")

    def gauge(self, name: str, help_text: str) -> None:
        self._declare(name, help_text, "gauge")

    def histogram(
        self, name: str, help_text: str, *, buckets: tuple[float, ...] | None = None
    ) -> None:
        self._declare(name, help_text, "histogram", buckets=buckets)

    def _declare(
        self,
        name: str,
        help_text: str,
        kind: str,
        *,
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        with self._lock:
            existing = self._series.get(name)
            if existing is not None:
                if existing.kind != kind:
                    raise ValueError(
                        f"metric {name!r} is already a {existing.kind}"
                    )
                return
            self._series[name] = _Series(
                help_text=help_text,
                kind=kind,
                bucket_bounds=buckets or DEFAULT_BUCKETS,
            )

    # -- recording --------------------------------------------------------

    def increment(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            series = self._series.get(name)
            if series is None or series.kind == "histogram":
                return
            series.values[key] = series.values.get(key, 0.0) + amount

    def set(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            series = self._series.get(name)
            if series is None or series.kind != "gauge":
                return
            series.values[key] = value

    def observe(self, name: str, seconds: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            series = self._series.get(name)
            if series is None or series.kind != "histogram":
                return
            counts = series.buckets.setdefault(
                key, [0.0] * len(series.bucket_bounds)
            )
            for index, bound in enumerate(series.bucket_bounds):
                if seconds <= bound:
                    counts[index] += 1
            series.sums[key] = series.sums.get(key, 0.0) + seconds
            series.counts[key] = series.counts.get(key, 0.0) + 1

    # -- cardinality ------------------------------------------------------

    def _key(self, name: str, labels: dict[str, Any]) -> _LabelKey:
        """Normalise labels, collapsing anything unbounded into "other"."""
        if not labels:
            return ()
        pairs: list[tuple[str, str]] = []
        for label, raw in sorted(labels.items()):
            value = _stringify(raw)
            with self._lock:
                seen = self._seen.setdefault((name, label), set())
                if value not in seen:
                    if len(seen) >= self._max_label_values:
                        value = "other"
                    else:
                        seen.add(value)
            pairs.append((label, value))
        return tuple(pairs)

    # -- exposition -------------------------------------------------------

    def render(self) -> str:
        """The Prometheus text exposition format.

        Deterministic ordering, so a diff between two scrapes is readable by a
        human debugging the exporter rather than the system.
        """
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._series):
                series = self._series[name]
                lines.append(f"# HELP {name} {series.help_text}")
                lines.append(f"# TYPE {name} {series.kind}")
                if series.kind == "histogram":
                    lines.extend(_render_histogram(name, series))
                else:
                    for key in sorted(series.values):
                        lines.append(
                            f"{name}{_render_labels(key)} "
                            f"{_number(series.values[key])}"
                        )
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict[str, dict[str, float]]:
        """A plain view, for tests and for the `/health` report."""
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for name, series in self._series.items():
                if series.kind == "histogram":
                    out[name] = {
                        _render_labels(key) or "{}": series.counts[key]
                        for key in series.counts
                    }
                else:
                    out[name] = {
                        _render_labels(key) or "{}": value
                        for key, value in series.values.items()
                    }
        return out


def _stringify(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    text = str(value) if value is not None else "none"
    # Label values reach a text format where a newline or a quote would break
    # the line. Escaped rather than refused: a broken metric is worse than a
    # slightly mangled label.
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:96]


def _render_labels(key: _LabelKey) -> str:
    if not key:
        return ""
    inner = ",".join(f'{name}="{value}"' for name, value in key)
    return "{" + inner + "}"


def _render_labels_with(key: _LabelKey, extra: tuple[str, str]) -> str:
    pairs = [*key, extra]
    inner = ",".join(f'{name}="{value}"' for name, value in pairs)
    return "{" + inner + "}"


def _render_histogram(name: str, series: _Series) -> list[str]:
    lines: list[str] = []
    for key in sorted(series.buckets):
        counts = series.buckets[key]
        for bound, count in zip(series.bucket_bounds, counts, strict=True):
            lines.append(
                f"{name}_bucket{_render_labels_with(key, ('le', _number(bound)))} "
                f"{_number(count)}"
            )
        lines.append(
            f"{name}_bucket{_render_labels_with(key, ('le', '+Inf'))} "
            f"{_number(series.counts.get(key, 0.0))}"
        )
        lines.append(f"{name}_sum{_render_labels(key)} {_number(series.sums[key])}")
        lines.append(
            f"{name}_count{_render_labels(key)} {_number(series.counts[key])}"
        )
    return lines


def _number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(round(value, 6))


# ---------------------------------------------------------------------------
# The metrics this system actually exports
# ---------------------------------------------------------------------------

def register_defaults(metrics: Metrics) -> Metrics:
    """Declare every series up front.

    Declared rather than created on first use, so a metric reads as zero
    instead of being absent before anything has happened. "No data" and "no
    errors" look identical on a dashboard, and only one of them is good news.
    """
    metrics.counter(
        "vtv_http_requests_total",
        "HTTP requests, by route template and status class.",
    )
    metrics.histogram(
        "vtv_http_request_duration_seconds", "HTTP request latency."
    )
    metrics.counter(
        "vtv_errors_total", "Domain errors raised, by stable error code."
    )

    metrics.counter("vtv_jobs_total", "Jobs finished, by kind and outcome.")
    metrics.histogram("vtv_job_duration_seconds", "Job execution time.")
    metrics.gauge(
        "vtv_queue_depth",
        "Jobs waiting or running. The number that says whether workers are "
        "keeping up.",
    )
    metrics.gauge("vtv_dead_letters", "Jobs that exhausted their retries.")

    metrics.counter(
        "vtv_renders_total",
        "Renders finished, by outcome — success, degraded or failed. A rising "
        "degraded share is the product failing quietly.",
    )
    metrics.counter(
        "vtv_degradations_total",
        "Individual fallback steps taken, by strategy descended from.",
    )
    metrics.counter(
        "vtv_grounding_refusals_total",
        "Specs refused for lack of evidence, by primitive.",
    )

    metrics.counter(
        "vtv_provider_calls_total", "Provider calls, by kind and outcome."
    )
    metrics.histogram(
        "vtv_provider_duration_seconds", "Provider latency, by kind."
    )
    metrics.counter(
        "vtv_provider_cost_usd_total", "Money spent with providers, by kind."
    )
    metrics.counter(
        "vtv_cache_lookups_total", "Generation cache lookups, hit or miss."
    )

    metrics.counter(
        "vtv_uploads_refused_total", "Uploads refused, by reason class."
    )
    metrics.counter(
        "vtv_rate_limited_total", "Requests refused by a rate limit, by scope."
    )
    metrics.counter(
        "vtv_quota_exceeded_total", "Requests refused for quota, by kind."
    )
    return metrics


__all__ = [
    "DEFAULT_BUCKETS",
    "MAX_LABEL_VALUES",
    "Metrics",
    "register_defaults",
]
