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
from vtv.contracts.generation import VisualFidelity
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
    #:
    #: Not "kept forever". Every plan carries a `max_retention_days` ceiling —
    #: see `vtv.retention` — and a saved project is stamped with an expiry from
    #: it. A storage commitment with no upper bound is a cost curve with no
    #: upper bound, and it was the reason that field existed and was never read.
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
    #: The tenant that owns this project (Stage 25).
    #:
    #: Mandatory since the 2026-08-13 audit. It was previously optional, and
    #: `owned_project` read that as "no owner, so anyone may have it" — which
    #: made every project created outside the API route cross-tenant readable.
    #: A nullable tenant column on a multi-tenant table is a fail-open waiting
    #: for its first caller, so the column is now required and the migration
    #: backfills the rows that predate it.
    organisation_id: Id
    #: The individual who created it, within that organisation.
    owner_id: str | None = Field(default=None, max_length=128)

    title: str | None = Field(default=None, max_length=200)
    style: StyleProfile = Field(default_factory=StyleProfile)

    #: What this project may spend on providers, in US dollars. `None` means
    #: "use the deployment's ceiling" — `VTV_MAX_PROJECT_COST_USD`.
    #:
    #: ## Why the project owns this and not only the deployment
    #:
    #: The deployment ceiling is an operator's backstop against a runaway loop.
    #: It is the wrong instrument for the question a *user* has, which is "how
    #: much is this video going to cost me", and it cannot answer it: one
    #: number governs a thirty-second explainer and an hour-long lecture
    #: identically, so the same setting is either absurdly loose for one or
    #: impossible for the other.
    #:
    #: An hour of video is roughly six hundred and fifty shots. At a quarter of
    #: a dollar each that is $163, which is not a number anyone should discover
    #: after the fact. Stating a budget up front is what lets the planner decide
    #: how many shots may be bought and how many must be drawn or found free —
    #: and the ladder was built for exactly that decision.
    budget_usd: float | None = Field(default=None, ge=0.0, le=10_000.0)

    #: How good the generated visuals should be. `None` means the deployment's
    #: configured tier (`VTV_IMAGE_GENERATION_QUALITY`) applies.
    #:
    #: This sits next to the budget because the two are one decision. The budget
    #: says how much may be spent; the fidelity says what one shot costs, and
    #: therefore how many shots the budget buys. The same $2 is a hundred and
    #: twenty-five draft shots or eight fine ones — a sixteen-fold difference in
    #: how much of the video is illustrated rather than set as type.
    #:
    #: Neither number is useful without the other, which is why the planner
    #: takes both and why the studio shows the two together with the count they
    #: produce.
    visual_fidelity: VisualFidelity | None = None

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
    #: The honest result of the last run: `success`, `degraded`, `partial` or a
    #: failure. Stored on the project so it survives a restart and is identical
    #: whichever API replica answers. `status` alone says a file exists;
    #: this says whether the product was delivered.
    outcome: str | None = Field(default=None, max_length=32)
    #: Human-readable reasons the outcome is not `success`. Shown to the user
    #: before they publish, which is the entire point of tracking it.
    degradation_notes: list[str] = Field(default_factory=list, max_length=16)
    #: False when the finished video has no voice track.
    narration_has_speech: bool = True
    total_cost_usd: UsdAmount = 0.0
    #: How long the last successful render is. Written here as well as on the
    #: render job so that a project *list* can show a length without loading a
    #: document per row — fifty projects is fifty extra reads for one column.
    rendered_duration_seconds: float | None = Field(default=None, ge=0.0)
    #: The edit timeline version the last successful render encoded.
    #:
    #: This is the fact behind "timeline changed since this render". The editor
    #: had no access to it and used `timeline.version > 1` as a stand-in, which
    #: lights up after the first edit anybody ever makes and never goes dark
    #: again — including immediately after a fresh render. A warning that is
    #: always on is not a warning.
    rendered_timeline_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _stages_are_complete_and_ordered(self) -> Project:
        stages = [state.stage for state in self.stages]
        if stages != list(STAGE_ORDER):
            raise ValueError(
                "project stages must be present exactly once, in canonical order"
            )
        # A saved project *may* carry an expiry, and in a real deployment
        # always does: the plan's retention ceiling. This invariant used to
        # forbid it outright, on the reading that "saved" meant "forever" —
        # which is what made `Plan.max_retention_days` unenforceable, and why it
        # sat unread on all five plans.
        #
        # Nothing replaces it here. "Every project has an expiry" is a *policy*
        # that needs the tenant's plan to evaluate, and a contract that cannot
        # see the plan cannot enforce it; `vtv.retention.expiry_for` is where it
        # lives, and `tests/test_retention.py` is where it is proven.
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
