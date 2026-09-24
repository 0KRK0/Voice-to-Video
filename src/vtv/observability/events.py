"""Structured pipeline events.

Every meaningful transition emits one of these. They are the substrate for
progress reporting today and for cost, latency and quality analysis later
(Stages 16 and 17), which is why they carry structured fields rather than
formatted strings.

The rule from ``docs/SECURITY.md`` applies without exception: identifiers,
durations, counts and statuses may travel in an event. Transcript text, prompts
and user content may not.
"""

from __future__ import annotations

import json
import contextlib
import logging
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from enum import Enum
from typing import Any

from pydantic import Field

from vtv.contracts.base import VTVModel, utc_now


class EventName(str, Enum):
    PROJECT_CREATED = "project.created"
    RECORDING_CREATED = "recording.created"
    RECORDING_PROBED = "recording.probed"
    TRANSCRIPTION_STARTED = "transcription.started"
    TRANSCRIPTION_COMPLETED = "transcription.completed"
    DOCUMENT_INGESTED = "document.ingested"
    #: P1-3. A document refused before parsing. Emitted rather than only raised,
    #: because a tenant suddenly generating a stream of refusals is the signal
    #: that someone is probing the parsers.
    DOCUMENT_REFUSED = "document.refused"

    # The product layer. A script is a document the user owns, so every change
    # to one is worth a record — "who changed my narration" must be answerable.
    SCRIPT_CREATED = "script.created"
    SCRIPT_EDITED = "script.edited"
    REVISION_PROPOSED = "script.revision.proposed"
    REVISION_ACCEPTED = "script.revision.accepted"
    REVISION_REJECTED = "script.revision.rejected"
    VISUAL_UNITS_PLANNED = "visual.units.planned"
    VISUAL_UNIT_REGENERATED = "visual.unit.regenerated"
    VISUAL_UNIT_LOCKED = "visual.unit.locked"
    VISUAL_UNIT_FAILED = "visual.unit.failed"
    TIMELINE_EDITED = "timeline.edited"
    PACING_PLANNED = "pacing.planned"
    NARRATION_SYNTHESISED = "narration.synthesised"
    UNDERSTANDING_STARTED = "understanding.started"
    UNDERSTANDING_COMPLETED = "understanding.completed"
    SCENES_CREATED = "scene.created"
    VISUAL_PLAN_CREATED = "visual.plan.created"
    VISUAL_BIBLE_BUILT = "visual.bible.built"
    GROUNDING_REFUSED = "grounding.refused"
    PLAN_APPROVED = "visual.plan.approved"
    ASSET_RESOLVED = "asset.resolved"
    ASSET_REJECTED = "asset.rejected"
    GENERATION_STARTED = "generation.started"
    GENERATION_COMPLETED = "generation.completed"
    GENERATION_FAILED = "generation.failed"
    DEGRADED = "visual.degraded"
    COMPOSITION_COMPLETED = "composition.completed"
    TIMELINE_CREATED = "timeline.created"
    RENDER_STARTED = "render.started"
    RENDER_PROGRESS = "render.progress"
    RENDER_COMPLETED = "render.completed"
    RENDER_FAILED = "render.failed"
    STAGE_FAILED = "stage.failed"
    JOB_ENQUEUED = "job.enqueued"
    JOB_STARTED = "job.started"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    AUDIT_RECORDED = "audit.recorded"


class Event(VTVModel):
    name: EventName
    project_id: str | None = None
    scene_id: str | None = None
    at: Any = Field(default_factory=utc_now)
    duration_ms: int | None = None
    cost_usd: float | None = None
    #: Structured, non-sensitive. Never user content.
    data: dict[str, Any] = Field(default_factory=dict)


EventHandler = Callable[[Event], None]


class EventSink:
    """Fan-out for events. In-process today; a transport later."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._recorded: list[Event] = []
        self.record = False

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register a handler, and return the way to remove it again.

        Subscription used to be permanent, which is correct for the handlers
        wired once at startup — metrics, logging — and a leak for anything
        scoped to one piece of work. A worker that renders projects all day and
        attaches a per-render handler accumulates one dead closure per render,
        each holding that render's project object alive, and each still being
        called on every subsequent event.

        Returning the remover rather than exposing an `unsubscribe(handler)`
        means the caller cannot get the pairing wrong, and a handler that was
        already removed can be removed again harmlessly.
        """
        self._handlers.append(handler)

        def remove() -> None:
            with suppress(ValueError):
                self._handlers.remove(handler)

        return remove

    def emit(self, name: EventName, **fields: Any) -> Event:
        event = Event(name=name, **fields)
        if self.record:
            self._recorded.append(event)
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                logging.getLogger("vtv.events").exception("event handler failed")
        return event

    @property
    def recorded(self) -> list[Event]:
        return list(self._recorded)

    def clear(self) -> None:
        self._recorded.clear()


def json_log_handler(stream: Any = None) -> EventHandler:
    """Emit events as one JSON object per line."""
    target = stream or sys.stdout

    def handle(event: Event) -> None:
        print(event.canonical_json(), file=target, flush=True)

    return handle


class Timer:
    """Wall-clock timing for a stage, in milliseconds."""

    def __init__(self) -> None:
        self.started = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


def tolerant_streams() -> None:
    """Make this process's output survive a console that cannot spell.

    Windows gives a redirected stream the cp1252 codec unless told otherwise,
    and cp1252 has no `▸`, no `—`, no `…`. Writing one raises
    `UnicodeEncodeError` — out of a `print`, in the middle of whatever was
    calling it.

    That is not theoretical. With provider tracing on and the worker's output
    going to a log file, every job on Windows died in ten milliseconds inside
    the trace line that announces it, before drawing a single frame, and the
    queue dead-lettered it after three identical instant failures. **A
    decorative character in a log message stopped a render from happening.**

    `errors="replace"` at the process boundary is the fix that generalises: no
    log line anywhere, now or later, can be the reason work does not happen.
    Individual call sites guarding themselves would be a rule every future
    `print` has to remember.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            # Absent on a stream that is not a `TextIOWrapper` — a test double,
            # a captured buffer. Those are not the streams with the problem.
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]


def configure_logging(level: str = "info") -> None:
    """Structured-ish logging. Formatted messages for humans, events for machines.

    Makes the streams tolerant first: logging is the most likely thing to emit a
    character the console cannot take, and the least acceptable thing to fail
    because of it.
    """
    tolerant_streams()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
        stream=sys.stderr,
    )


def as_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "Event",
    "EventHandler",
    "EventName",
    "EventSink",
    "Timer",
    "as_json",
    "configure_logging",
    "tolerant_streams",
    "json_log_handler",
]
