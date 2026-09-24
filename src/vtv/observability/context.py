"""P1-6 — the identifiers that make one request traceable end to end.

The audit's finding was operational, and phrased as a question an on-call
engineer cannot answer: *"why did this customer's video fail?"* Answering it
required reading the source, because a render crosses an HTTP request, a queue,
a worker, several providers and a renderer, and nothing carried an identifier
across those boundaries. Logs from the API and logs from the worker described
the same failure and had no field in common.

## What a correlation identifier is for

Not for the happy path. For the moment a customer says "it was broken at about
four o'clock" and you have four replicas, two workers, twelve providers and no
idea which. One identifier, present on every line, turns that into a query.

Three fields, deliberately:

* **`request_id`** — one HTTP request. Generated at the edge, or taken from an
  inbound `X-Request-Id` so a caller's own tracing joins up with ours.
* **`trace_id`** — the whole causal chain, including the queued job the request
  produced and everything that job did. This is the one that survives the
  process boundary, and the one the audit was really missing.
* **`organisation_id`** — the tenant. On every line so that "is this affecting
  one customer or all of them" is answerable in one query rather than by
  joining against the database during an incident.

## Why `contextvars` and not a parameter

Threading a context object through forty functions is the version of this that
gets abandoned halfway and leaves a gap exactly where the interesting failure
is. `contextvars` propagates across `await` and into `asyncio.to_thread`, which
covers every boundary inside one process; the queue payload carries it across
processes explicitly, because implicit propagation across a process boundary is
not a thing that exists.

**This is not ambient authority.** Nothing here is ever read to make a security
decision — the tenant used for authorisation comes from the `Principal`, and the
tenant used for storage comes from the document. This is for logs and metrics
only, and the distinction matters: a correlation field is attacker-influenced
(an inbound header) and must never be trusted for anything but labelling.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from vtv.contracts.base import IdPrefix, new_id

#: An inbound identifier is attacker-controlled: it reaches logs, metric labels
#: and possibly a downstream system. Bounded and restricted to characters that
#: cannot forge a log line or blow up a metrics cardinality budget.
_SAFE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


@dataclass(frozen=True)
class Correlation:
    """Who and what this unit of work belongs to."""

    trace_id: str
    request_id: str | None = None
    organisation_id: str | None = None
    #: Set on a worker, so a log line says which process handled the job.
    job_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        """Only the fields that are set. An empty field is noise in every line."""
        fields = {
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "organisation_id": self.organisation_id,
            "job_id": self.job_id,
        }
        return {name: value for name, value in fields.items() if value}


_CURRENT: contextvars.ContextVar[Correlation | None] = contextvars.ContextVar(
    "vtv_correlation", default=None
)


def current() -> Correlation | None:
    """The correlation for the work in hand, or `None` outside any."""
    return _CURRENT.get()


def current_fields() -> dict[str, str]:
    """Correlation as log fields. Empty outside a correlated unit of work."""
    correlation = _CURRENT.get()
    return correlation.as_dict() if correlation else {}


def sanitise(value: str | None) -> str | None:
    """Accept an inbound identifier, or refuse it.

    Refusing rather than truncating: a caller sending a 4KB `X-Request-Id` is
    not making a typo, and silently shortening it produces an identifier that
    matches nothing on their side either.
    """
    if value is None:
        return None
    candidate = value.strip()
    return candidate if _SAFE.match(candidate) else None


def new_trace_id() -> str:
    return new_id(IdPrefix.GENERATION)


@contextmanager
def correlated(
    *,
    trace_id: str | None = None,
    request_id: str | None = None,
    organisation_id: str | None = None,
    job_id: str | None = None,
) -> Iterator[Correlation]:
    """Run a block under a correlation, restoring the previous one after.

    Fields inherit from any enclosing correlation rather than replacing it, so
    a worker that sets `job_id` inside a trace keeps the trace.
    """
    parent = _CURRENT.get()
    correlation = Correlation(
        trace_id=trace_id or (parent.trace_id if parent else new_trace_id()),
        request_id=request_id or (parent.request_id if parent else None),
        organisation_id=organisation_id
        or (parent.organisation_id if parent else None),
        job_id=job_id or (parent.job_id if parent else None),
    )
    token = _CURRENT.set(correlation)
    try:
        yield correlation
    finally:
        _CURRENT.reset(token)


def bind(**fields: str | None) -> Correlation | None:
    """Add fields to the current correlation in place.

    For the case where the identifier is not known when the block opens — the
    tenant, for instance, is known only after authentication, which happens
    inside the request's own correlation.
    """
    correlation = _CURRENT.get()
    if correlation is None:
        return None
    updated = replace(
        correlation,
        **{
            name: value
            for name, value in fields.items()
            if value and getattr(correlation, name, None) is None
        },
    )
    _CURRENT.set(updated)
    return updated


__all__ = [
    "Correlation",
    "bind",
    "correlated",
    "current",
    "current_fields",
    "new_trace_id",
    "sanitise",
]
