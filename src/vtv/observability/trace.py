"""A local, opt-in record of every paid provider call.

## Why this is not the event stream

`observability/events.py` says, without exception, that identifiers, durations,
counts and statuses may travel in an event and that **transcript text, prompts
and user content may not**. That rule is from `docs/SECURITY.md` and it is
right: events are fanned out to logs, metrics and eventually a transport, and
user content does not belong in any of them.

But "which prompt did we pay four cents for" is exactly the question an operator
has when a 76-second video costs two dollars, and it cannot be answered without
the prompt. So this is a *separate* facility with different rules and a
different destination:

* **off by default**, enabled per deployment with `VTV_TRACE_PROVIDER_CALLS`;
* **refused outright in production**, because a file of user prompts on a
  server is a data-protection incident waiting for a disk to be imaged;
* **written to one local file**, never to the event stream, never to a metric,
  never to the audit log;
* **truncated** — a prompt excerpt, not a transcript.

The file is `var/provider-calls.jsonl` by default. It contains user content by
design. Treat it like a heap dump: useful, local, and not to be shipped.

## What it answers

Three questions, in this order of usefulness:

1. **How many calls did one render make, and to whom?** Sixty-four requests for
   a fourteen-visual video is a number that needs explaining, and the
   explanation is a list.
2. **What did each one cost, and was it a retry?** A job that fails after doing
   paid work and is retried three times pays three times. The `attempt` field
   is how that becomes visible rather than mysterious.
3. **What exactly did we send?** Half of what an adapter does is build a
   request nobody has ever read.

`python -m vtv.trace_report` renders the file as a table.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Prompts are excerpted, not stored whole. Enough to recognise the shot.
PROMPT_EXCERPT_CHARS = 240

#: Per-call context — which job, which visual, which stage. A `ContextVar`
#: rather than an attribute because visuals are sourced concurrently: four
#: `asyncio` tasks each need their own context, and a shared attribute would
#: attribute every call to whichever task set it last.
#: `None` rather than `{}` as the default: a mutable default on a `ContextVar`
#: is shared by every context that never set one, so a single stray mutation
#: would leak across tasks — exactly the bug the `ContextVar` is here to avoid.
_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "vtv_trace_context", default=None
)


@contextlib.contextmanager
def traced(**fields: Any) -> Iterator[None]:
    """Attach context to every provider call made inside this block.

    Nests: an inner block adds to the outer one rather than replacing it, so a
    call can carry both `job=render_scope` and `unit=vun_…` without either
    caller knowing about the other.
    """
    merged = {
        **(_CONTEXT.get() or {}),
        **{k: v for k, v in fields.items() if v is not None},
    }
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def context() -> dict[str, Any]:
    return dict(_CONTEXT.get() or {})


@dataclass
class CallRecord:
    """One provider call, as it will be written."""

    kind: str
    provider: str
    model: str | None
    attempt: int
    outcome: str
    cost_usd: float
    latency_ms: int
    from_cache: bool = False
    organisation_id: str | None = None
    project_id: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "at": round(time.time(), 3),
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "from_cache": self.from_cache,
            "organisation_id": self.organisation_id,
            "project_id": self.project_id,
            "request": self.request,
            "response": self.response,
        }
        if self.error:
            payload["error"] = self.error[:400]
        payload.update(context())
        return payload


class ProviderTrace:
    """Appends one JSON line per provider call, and tallies as it goes.

    Thread-safe because the worker runs an event loop per process but adapters
    may hand work to a thread pool, and a half-written line in a debug file is
    worse than no debug file.
    """

    def __init__(self, path: Path, *, echo: bool = True) -> None:
        self.path = Path(path)
        self.echo = echo
        self._lock = threading.Lock()
        self._calls = 0
        self._spend = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- recording --------------------------------------------------------

    def record(self, call: CallRecord) -> None:
        line = json.dumps(call.as_json(), ensure_ascii=False, default=str)
        with self._lock:
            self._calls += 1
            self._spend += max(0.0, call.cost_usd)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        if self.echo:
            print(self._line(call), flush=True)

    @staticmethod
    def _line(call: CallRecord) -> str:
        """One readable line. This is what someone watching the worker sees."""
        marks = {"ok": "·", "cached": "=", "failed": "×", "refused": "!", "blocked": "⊘"}
        mark = marks.get(call.outcome, "?")
        money = f"${call.cost_usd:.4f}" if call.cost_usd else "     free"
        where = context()
        unit = where.get("unit") or where.get("stage") or ""
        detail = call.request.get("summary") or call.error or ""
        attempt = f" try{call.attempt}" if call.attempt > 1 else ""
        return (
            f"  {mark} {call.kind:<14} {call.provider:<16} {money:>9} "
            f"{call.latency_ms:>6}ms{attempt}  {unit:<26} {str(detail)[:70]}"
        )

    # -- tallies ----------------------------------------------------------

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def spend_usd(self) -> float:
        return round(self._spend, 6)

    @contextlib.contextmanager
    def job(self, kind: str, job_id: str) -> Iterator[None]:
        """Wrap one job, and print what it spent when it ends.

        The totals are deltas, so a worker running two jobs concurrently still
        reports each one's own consumption — approximately. Exact per-job
        attribution lives in the file; this line exists so that a person
        watching the terminal sees the number without reading anything.
        """
        started_calls, started_spend = self._calls, self._spend
        started_at = time.monotonic()
        if self.echo:
            say(f"\n▸ {kind} {job_id}")
        with traced(job=job_id, job_kind=kind):
            try:
                yield
            finally:
                calls = self._calls - started_calls
                spend = self._spend - started_spend
                seconds = time.monotonic() - started_at
                if self.echo:
                    say(
                        f"▪ {kind} {job_id} — {calls} provider call(s), "
                        f"${spend:.4f}, {seconds:.1f}s\n"
                    )


class NullTrace:
    """The disabled trace. Same shape, records nothing, costs nothing.

    A null object rather than `None` checks at the call sites, because the call
    sites are the router's hot path and an `if self.trace is not None` there is
    one more thing a future edit can get wrong.
    """

    calls = 0
    spend_usd = 0.0

    def record(self, call: CallRecord) -> None:
        return None

    @contextlib.contextmanager
    def job(self, kind: str, job_id: str) -> Iterator[None]:
        with traced(job=job_id, job_kind=kind):
            yield


def say(text: str) -> None:
    """Print, and never let printing be the reason a job failed.

    This is instrumentation. It exists to describe work, and a description that
    can destroy what it describes is worse than no description at all.

    It did exactly that. The line above starts with `▸` (U+25B8), which does not
    exist in Windows' cp1252 — the encoding Python gives a redirected stream
    there unless told otherwise. So on Windows, with the worker's output going
    to a log file and provider tracing switched on, **every job died in ten
    milliseconds with `UnicodeEncodeError` before drawing a frame**, and the
    only trace of it was the word `internal_error`.

    The retry made it worse in the way these things always do: three attempts,
    three identical instant failures, dead-lettered. A render that was never
    attempted, reported as a render that could not be done.

    Falls back to ASCII rather than staying silent, because the line is still
    worth having with a `?` in it.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        with contextlib.suppress(Exception):
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:  # noqa: BLE001 - a closed pipe, a full disk, anything
        pass


def excerpt(text: object, limit: int = PROMPT_EXCERPT_CHARS) -> str:
    """A prompt, shortened, on one line."""
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def build_trace(settings: object) -> ProviderTrace | NullTrace:
    """The trace this deployment should have.

    Refuses in production rather than warning about it. A file of user prompts
    is not something to leave to a deployment checklist.
    """
    enabled = bool(getattr(settings, "trace_provider_calls", False))
    if not enabled or bool(getattr(settings, "is_production", False)):
        return NullTrace()
    configured = getattr(settings, "trace_path", "") or ""
    root = getattr(settings, "storage_root", Path("./var/storage"))
    path = Path(configured) if configured else Path(root).parent / "provider-calls.jsonl"
    return ProviderTrace(path, echo=os.environ.get("VTV_TRACE_QUIET") != "1")


__all__ = [
    "PROMPT_EXCERPT_CHARS",
    "CallRecord",
    "NullTrace",
    "ProviderTrace",
    "build_trace",
    "context",
    "excerpt",
    "traced",
]
