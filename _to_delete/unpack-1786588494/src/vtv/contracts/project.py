"""The project: one run of the pipeline, and the state machine that governs it.

A ``Project`` is the object a user thinks they own. It holds no media and no
transcript text of its own — it holds *references*, plus the stage-by-stage state
of the pipeline. That makes it small, cheap to read, safe to log, and the natural
place to enforce two policies that are much harder to add later.

**Retention is a first-class choice, not a default that happens to us.** A
project is either ``TEMPORARY`` — processed, rendered, delivered, then swept —
or ``SAVED``. The user picks, and the storage layer honours it without needing to
understand the domain (Section 26, ``docs/STORAGE_POLICY.md``).

**Regeneration beats storage.** What we keep for a saved project is the
intermediate reasoning — transcript, understanding, scene graph, visual plan,
timeline — not gigabytes of finished MP4. Those documents are small, and from
them the video can be rebuilt at any resolution, in any aspect ratio, with any
one shot replaced. Keeping the reasoning rather than the pixels is both cheaper
and strictly more useful (Rule 12).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Id,
    IdPrefix,
    RootDocument,
    Timestamped,
    UsdAmount,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import ErrorInfo, Status
from vtv.contracts.style import StyleProfile


class PipelineStage(str, Enum):
    """The ordered stages every project passes through.

    Naming them lets progress be reported honestly ("understanding your
    recording" rather than a spinner), lets failures be attributed precisely, and
    gives the evaluation system its natural unit of measurement.
    """

    CAPTURE = "capture"
    TRANSCRIPTION = "transcription"
    UNDERSTANDING = "understanding"
    SCENE_PLANNING = "scene_planning"
    VISUAL_DIRECTION = "visual_direction"
    ASSET_RESOLUTION = "asset_resolution"
    COMPOSITION = "composition"
    RENDERING = "rendering"


#: Canonical order. Used to compute progress and to reject out-of-order updates.
STAGE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.CAPTURE,
    PipelineStage.TRANSCRIPTION,
    PipelineStage.UNDERSTANDING,
    PipelineStage.SCENE_PLANNING,
    PipelineStage.VISUAL_DIRECTION,
    PipelineStage.ASSET_RESOLUTION,
    PipelineStage.COMPOSITION,
    PipelineStage.RENDERING,
)


class PersistenceMode(str, Enum):
    """How long this project's data is kept."""

    #: Deleted once the user has their video. The default, and the honest one
    #: for a product built on people's voices.
    TEMPORARY = "temporary"
    #: Kept so the user can return, edit and re-render.
    SAVED = "saved"


class StageState(VTVModel):
    """Status of one pipeline stage."""

    stage: PipelineStage
    status: Status = Status.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: What this stage cost, so that per-stage unit economics are visible
    #: without reconstructing them from generation records.
    cost_usd: UsdAmount = 0.0
    error: ErrorInfo | None = None
    #: Human-readable note surfaced in the UI, e.g. "12 of 18 visuals ready".
    detail: str | None = Field(default=None, max_length=200)


class ProcessingConsent(VTVModel):
    """What the user has agreed to.

    Consent is stored per project and defaults to the most conservative setting.
    Nothing about a recording may be used to improve the product unless this
    says so explicitly, and the flag travels with the data so that a later
    training-data export cannot accidentally include material that was never
    offered (``docs/SECURITY.md``).
    """

    #: Allow retaining content to improve the system. Off unless chosen.
    improve_product: bool = False
    #: Allow human review of this project's content for quality purposes.
    human_review: bool = False
    accepted_at: datetime | None = None


class Project(RootDocument, Timestamped):
    """The root aggregate for one voice-to-video run."""

    document_name = "project"

    project_id: Id = Field(default_factory=lambda: new_id(IdPrefix.PROJECT))
    #: The tenant that owns this project (Stage 25). Optional only for projects
    #: created before tenancy existed and for the internal demo path; every
    #: request-created project carries one, and the API refuses to return a
    #: project whose organisation does not match the caller's.
    organisation_id: Id | None = None
    #: The individual who created it, within that organisation.
    owner_id: str | None = Field(default=None, max_length=128)

    title: str | None = Field(default=None, max_length=200)
    style: StyleProfile = Field(default_factory=StyleProfile)

    persistence: PersistenceMode = PersistenceMode.TEMPORARY
    consent: ProcessingConsent = Field(default_factory=ProcessingConsent)
    #: When temporary data is swept. Set by the storage policy, not by callers.
    expires_at: datetime | None = None

    # References to the documents this project produced. Each is written once by
    # its stage and then treated as immutable input by the next.
    recording_id: Id | None = None
    transcript_id: Id | None = None
    understanding_id: Id | None = None
    scene_graph_id: Id | None = None
    visual_plan_id: Id | None = None
    timeline_id: Id | None = None
    render_job_id: Id | None = None

    stages: list[StageState] = Field(
        default_factory=lambda: [StageState(stage=s) for s in STAGE_ORDER]
    )
    status: Status = Status.PENDING
    total_cost_usd: UsdAmount = 0.0

    @model_validator(mode="after")
    def _stages_are_complete_and_ordered(self) -> Project:
        stages = [state.stage for state in self.stages]
        if stages != list(STAGE_ORDER):
            raise ValueError(
                "project stages must be present exactly once, in canonical order"
            )
        if self.persistence is PersistenceMode.SAVED and self.expires_at is not None:
            raise ValueError("a saved project must not carry an expiry")
        return self

    def stage(self, stage: PipelineStage) -> StageState:
        return next(state for state in self.stages if state.stage is stage)

    @property
    def current_stage(self) -> PipelineStage | None:
        """The stage the project is working on, or ``None`` when finished."""
        for state in self.stages:
            if state.status is not Status.READY:
                return state.stage
        return None

    @property
    def progress(self) -> float:
        """Fraction of stages completed, for honest progress reporting."""
        done = sum(1 for state in self.stages if state.status is Status.READY)
        return done / len(self.stages)

    @property
    def failed_stage(self) -> StageState | None:
        return next(
            (state for state in self.stages if state.status is Status.FAILED), None
        )

    @property
    def is_regenerable(self) -> bool:
        """Whether the video could be rebuilt from what we still hold.

        This is the property that justifies not storing finished video for
        temporary projects, and the one the storage lifecycle must never break
        for saved ones.
        """
        return all(
            reference is not None
            for reference in (
                self.transcript_id,
                self.understanding_id,
                self.scene_graph_id,
                self.visual_plan_id,
            )
        )


__all__ = [
    "STAGE_ORDER",
    "PersistenceMode",
    "PipelineStage",
    "ProcessingConsent",
    "Project",
    "StageState",
]
