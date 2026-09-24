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
import logging
import sys
import time
from collections.abc import Callable
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
    NARRATION_SYNTHESISED = "narration.synthesised"
    UNDERSTANDING_STARTED = "understanding.started"
    UNDERSTANDING_COMPLETED = "understanding.completed"
    SCENES_CREATED = "scene.created"
    VISUAL_PLAN_CREATED = "visual.plan.created"
    VISUAL_BIBLE_BUILT = "visual.bible.built"
    GROUNDING_REFUSED = "grounding.refused"
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

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

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


def configure_logging(level: str = "info") -> None:
    """Structured-ish logging. Formatted messages for humans, events for machines."""
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
    "json_log_handler",
]
