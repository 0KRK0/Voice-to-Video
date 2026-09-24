"""The golden path.

Every stage, wired together, from a recording to a finished MP4:

    capture → transcribe → understand → scenes → direct → compose → render

Two things this module is careful about.

**Stage state is honest.** Each stage is marked `PROCESSING` before it runs and
`READY` or `FAILED` after, with its cost and duration recorded. The user sees
real progress and a failure names the stage that failed.

**Failure blast radius matches the stage.** Capture, transcription and
understanding are fatal — there is nothing to proceed with. Everything after is
per-scene, and degrades (`docs/ERROR_MODEL.md`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vtv.contracts.asset import Asset
from vtv.contracts.consistency import VisualBible
from vtv.contracts.errors import DegradationStep, ErrorCode, Status, VTVError
from vtv.contracts.project import PersistenceMode, PipelineStage, Project
from vtv.contracts.recording import (
    AudioFormat,
    AudioProperties,
    CaptureSource,
    Recording,
)
from vtv.contracts.render import RenderJob, RenderSettings
from vtv.contracts.scene import SceneGraph
from vtv.contracts.semantics import Understanding
from vtv.contracts.source import SourceDocument
from vtv.contracts.style import StyleProfile
from vtv.contracts.timeline import NarrationTrack, Timeline
from vtv.contracts.transcript import Transcript
from vtv.contracts.visual_plan import VisualPlan
from vtv.observability.events import EventName, EventSink, Timer
from vtv.pipeline.capture import CaptureService
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.consistency import ConsistencyEngine
from vtv.pipeline.costs import CostLedger
from vtv.pipeline.director import VisualDirector
from vtv.pipeline.ingestion import DocumentContext, transcript_from_source
from vtv.pipeline.narration import NarrationService
from vtv.pipeline.plan_gate import GateOutcome, PlanGate
from vtv.pipeline.scenes import SceneEngine
from vtv.pipeline.transcription import TranscriptionService
from vtv.pipeline.understanding import UnderstandingService
from vtv.ports.rendering import Renderer


class RunOutcome(str, Enum):
    """What actually happened, in the vocabulary a client can act on.

    Before the 2026-08-13 audit a run reported `ready` whether or not it had a
    voice track, so a caller checking status shipped a silent video believing it
    had succeeded. `ready` must mean what the product promises.
    """

    #: Everything the product promises was delivered.
    SUCCESS = "success"
    #: A watchable video exists, but something promised is missing — no voice,
    #: or every visual fell to the last rung of its ladder. Deliverable, and the
    #: user must be told before they publish it.
    DEGRADED = "degraded"
    #: Some scenes are usable and some are not.
    PARTIAL = "partial"
    #: Failed, and worth trying again with the same inputs.
    RETRYABLE_FAILURE = "retryable_failure"
    #: Failed, and retrying changes nothing.
    PERMANENT_FAILURE = "permanent_failure"
    CANCELLED = "cancelled"

    @property
    def is_deliverable(self) -> bool:
        """Whether there is a video the user can watch."""
        return self in {RunOutcome.SUCCESS, RunOutcome.DEGRADED, RunOutcome.PARTIAL}


#: Fraction of scenes that may fall back to bare narration type before the run
#: is reported DEGRADED. Above this, the video is technically complete and
#: visually empty, which the user needs to know before publishing it.
MAX_BARE_FALLBACK_RATIO = 0.5


@dataclass
class PipelineResult:
    """Everything one run produced. Persisted together, regenerable from it."""

    project: Project
    recording: Recording
    transcript: Transcript | None = None
    understanding: Understanding | None = None
    scene_graph: SceneGraph | None = None
    visual_plan: VisualPlan | None = None
    timeline: Timeline | None = None
    render_job: RenderJob | None = None
    assets: list[Asset] = field(default_factory=list)
    ledger: CostLedger | None = None

    #: Stage 24. What the mandatory grounding gate refused, and why.
    gate_outcome: GateOutcome | None = None
    #: Stage 23. Every decision that must hold across scenes, so a re-render
    #: reproduces the video the user approved rather than one resembling it.
    visual_bible: VisualBible | None = None
    #: Present when the run started from a document rather than a recording.
    #: Kept so a claim in the finished video can be traced back to page 14, and
    #: so the Visual Director can chart a table the author already provided.
    source_document: SourceDocument | None = None
    document_context: DocumentContext | None = None
    #: False when the narration track is silence. Surfaced to the user rather
    #: than left to be discovered by pressing play.
    narration_has_speech: bool = True
    #: Project-level fallbacks taken. Per-scene degradations live on the plan.
    degradations: list[DegradationStep] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """A file was produced. NOT the same as the product being delivered.

        Kept narrow on purpose and no longer used to report status to a client:
        `outcome` is the answer to "did this work", and it considers narration
        and degradation. See `docs/ERROR_MODEL.md`.
        """
        return self.render_job is not None and self.render_job.status is Status.READY

    @property
    def outcome(self) -> RunOutcome:
        """The honest status.

        Narration is part of the promise, so a mute render is `DEGRADED` rather
        than `SUCCESS` — a distinction the API surfaces and the frontend shows.
        """
        if self.render_job is None:
            return RunOutcome.PERMANENT_FAILURE
        if self.render_job.status is not Status.READY:
            retryable = bool(
                self.render_job.error and self.render_job.error.retryable
            )
            return (
                RunOutcome.RETRYABLE_FAILURE
                if retryable
                else RunOutcome.PERMANENT_FAILURE
            )

        if not self.narration_has_speech:
            return RunOutcome.DEGRADED

        bare = len(self.gate_outcome.fully_refused_scene_ids) if self.gate_outcome else 0
        scenes = len(self.scene_graph.scenes) if self.scene_graph else 0
        if scenes and bare / scenes > MAX_BARE_FALLBACK_RATIO:
            return RunOutcome.PARTIAL
        if bare or self.degradations:
            return RunOutcome.DEGRADED
        return RunOutcome.SUCCESS

    @property
    def degradation_summary(self) -> list[str]:
        """Why the outcome is not SUCCESS, in words a user can read."""
        reasons: list[str] = []
        if not self.narration_has_speech:
            reasons.append(
                "No speech synthesiser was available, so this video has no voice track."
            )
        if self.gate_outcome and self.gate_outcome.fully_refused_scene_ids:
            reasons.append(
                f"{len(self.gate_outcome.fully_refused_scene_ids)} scene(s) fell back "
                "to plain text because the source did not support a richer visual."
            )
        if self.gate_outcome and self.gate_outcome.refusals:
            reasons.append(
                f"{self.gate_outcome.refusals} visual(s) were refused for making a "
                "claim the source did not contain."
            )
        return reasons


@dataclass
class Pipeline:
    """The whole system, assembled."""

    capture: CaptureService
    transcription: TranscriptionService
    understanding: UnderstandingService
    scenes: SceneEngine
    director: VisualDirector
    composer: SceneComposer
    renderer: Renderer
    events: EventSink
    #: Stage 24. NOT optional. Every visual plan passes through this before
    #: composition, whichever director produced it. A pipeline that could be
    #: constructed without a gate would eventually be constructed without one.
    plan_gate: PlanGate
    ledger: CostLedger = field(default_factory=CostLedger)
    #: Optional. Required only for input that arrives without a voice.
    narration: NarrationService | None = None
    #: Stage 23. Optional: without it every scene is planned independently and
    #: a recurring entity may look different each time it appears.
    consistency: ConsistencyEngine | None = None

    async def run(
        self,
        *,
        audio: bytes,
        project: Project | None = None,
        organisation_id: str | None = None,
        style: StyleProfile | None = None,
        source: CaptureSource = CaptureSource.MICROPHONE,
        settings: RenderSettings | None = None,
        render: bool = True,
        #: P1-1. An approved Visual Bible from an earlier run of this project.
        #: Carrying it forward is what makes a re-render reproduce the video the
        #: user already saw: every locked binding survives, and the consistency
        #: engine adds to it rather than starting again.
        visual_bible: VisualBible | None = None,
    ) -> PipelineResult:
        project = project or _new_project(organisation_id, style)
        if style is not None:
            project.style = style
        settings = settings or RenderSettings(aspect_ratio=project.style.aspect_ratio)

        self.events.emit(
            EventName.PROJECT_CREATED,
            project_id=project.project_id,
            data={"persistence": project.persistence.value},
        )

        # -- Stage 1: capture ---------------------------------------------
        recording = await self._stage(
            project,
            PipelineStage.CAPTURE,
            lambda: self.capture.capture(
                organisation_id=project.organisation_id,
                project_id=project.project_id,
                data=audio,
                source=source,
            ),
        )
        result = PipelineResult(
            project=project,
            recording=recording,
            ledger=self.ledger,
            visual_bible=visual_bible,
        )
        if recording.status is not Status.READY:
            self._fail(project, PipelineStage.CAPTURE, recording.error)
            return result
        project.recording_id = recording.recording_id

        # -- Stage 2: transcription ---------------------------------------
        transcript = await self._stage(
            project,
            PipelineStage.TRANSCRIPTION,
            lambda: self.transcription.transcribe(recording),
        )
        result.transcript = transcript
        project.transcript_id = transcript.transcript_id

        return await self._continue(
            result,
            transcript=transcript,
            narration=NarrationTrack(
                audio=recording.audio,
                duration_seconds=recording.duration_seconds or 1.0,
            ),
            settings=settings,
            render=render,
        )

    async def run_from_document(
        self,
        *,
        document: SourceDocument,
        project: Project | None = None,
        organisation_id: str | None = None,
        style: StyleProfile | None = None,
        settings: RenderSettings | None = None,
        render: bool = True,
        #: P1-1. An approved Visual Bible from an earlier run of this project.
        #: Carrying it forward is what makes a re-render reproduce the video the
        #: user already saw: every locked binding survives, and the consistency
        #: engine adds to it rather than starting again.
        visual_bible: VisualBible | None = None,
    ) -> PipelineResult:
        """Stage 21 — the same golden path, entered from written input.

        A document has no voice, so the first two stages are replaced rather
        than skipped: ingestion stands in for capture and transcription, and a
        narration track is synthesised so the timeline still has its clock.
        Stage 3 onward is byte-for-byte the same code as the voice path, which
        is the entire payoff of making every stage consume a document.

        Requires a `NarrationService`. Without one this raises rather than
        producing a video with no audio track, because a silent MP4 that nobody
        chose is a bug, and a silent MP4 that the fallback ladder chose is a
        recorded degradation.
        """
        if self.narration is None:
            raise VTVError(
                "document input requires a narration service",
                code=ErrorCode.PROVIDER_UNAVAILABLE,
                user_message=(
                    "This deployment cannot turn documents into video yet: no "
                    "narration is configured."
                ),
            )

        project = project or _new_project(organisation_id, style)
        if style is not None:
            project.style = style
        settings = settings or RenderSettings(aspect_ratio=project.style.aspect_ratio)

        self.events.emit(
            EventName.PROJECT_CREATED,
            project_id=project.project_id,
            data={"persistence": project.persistence.value, "input": "document"},
        )

        # -- Stages 1 and 2, document form --------------------------------
        draft, context = await self._stage(
            project,
            PipelineStage.CAPTURE,
            lambda: transcript_from_source(document),
            is_async=False,
        )
        spoken = await self._stage(
            project,
            PipelineStage.TRANSCRIPTION,
            lambda: self.narration.synthesise(  # type: ignore[union-attr]
                draft,
                organisation_id=project.organisation_id,
                project_id=project.project_id,
            ),
        )
        transcript = spoken.transcript
        recording = _synthetic_recording(
            organisation_id=project.organisation_id,
            project_id=project.project_id,
            audio=spoken.audio,
            duration=spoken.duration_seconds,
            language=document.language.code,
        )
        project.recording_id = recording.recording_id
        project.transcript_id = transcript.transcript_id

        result = PipelineResult(
            project=project,
            recording=recording,
            transcript=transcript,
            ledger=self.ledger,
            visual_bible=visual_bible,
            source_document=document,
            document_context=context,
            narration_has_speech=spoken.has_speech,
            degradations=list(spoken.degradations),
        )
        return await self._continue(
            result,
            transcript=transcript,
            narration=NarrationTrack(
                audio=spoken.audio, duration_seconds=spoken.duration_seconds
            ),
            settings=settings,
            render=render,
        )

    async def _continue(
        self,
        result: PipelineResult,
        *,
        transcript: Transcript,
        narration: NarrationTrack,
        settings: RenderSettings,
        render: bool,
    ) -> PipelineResult:
        """Stages 3 to 10 — identical for every kind of input."""
        project = result.project

        # -- Stage 3: understanding ---------------------------------------
        understanding = await self._stage(
            project,
            PipelineStage.UNDERSTANDING,
            lambda: self.understanding.understand(transcript),
        )
        result.understanding = understanding
        project.understanding_id = understanding.understanding_id

        # -- Stage 4: scenes ----------------------------------------------
        scene_graph = await self._stage(
            project,
            PipelineStage.SCENE_PLANNING,
            lambda: self.scenes.build(
                transcript=transcript,
                understanding=understanding,
                style=project.style,
                total_duration=narration.duration_seconds,
            ),
            is_async=False,
        )
        result.scene_graph = scene_graph
        project.scene_graph_id = scene_graph.scene_graph_id

        # -- Stage 5: visual direction ------------------------------------
        visual_plan = await self._stage(
            project,
            PipelineStage.VISUAL_DIRECTION,
            lambda: self.director.direct(
                scene_graph=scene_graph,
                understanding=understanding,
                context=result.document_context,
            ),
        )
        # -- Stage 24: the mandatory grounding gate ------------------------
        # Runs for every director, including ones written after this line. The
        # director proposes; the gate disposes.
        outcome = self.plan_gate.apply(
            plan=visual_plan,
            scene_graph=scene_graph,
            understanding=understanding,
            context=result.document_context,
        )
        visual_plan = outcome.plan
        result.gate_outcome = outcome
        result.degradations.extend(outcome.degradations)

        result.visual_plan = visual_plan
        project.visual_plan_id = visual_plan.visual_plan_id

        # -- Stage 23: visual consistency ---------------------------------
        # After direction, because it binds what the Director chose; before
        # composition, because that is where the bindings are applied.
        if self.consistency is not None:
            bible = self.consistency.build(
                scene_graph=scene_graph,
                understanding=understanding,
                style=project.style,
                existing=result.visual_bible,
            )
            self.consistency.review(bible=bible, scene_graph=scene_graph)
            result.visual_bible = bible

        # -- Stages 6 to 9: assets, generation, composition ---------------
        composition = await self._stage(
            project,
            PipelineStage.ASSET_RESOLUTION,
            lambda: self.composer.compose(
                scene_graph=scene_graph,
                visual_plan=visual_plan,
                transcript=transcript,
                narration=narration,
                # P1-1. Built one stage above and, until now, thrown away.
                visual_bible=result.visual_bible,
            ),
        )
        project.stage(PipelineStage.COMPOSITION).status = Status.READY
        result.timeline = composition.timeline
        result.assets = composition.assets
        project.timeline_id = composition.timeline.timeline_id

        # -- Stage 10: render ---------------------------------------------
        if render:
            render_job = await self._stage(
                project,
                PipelineStage.RENDERING,
                lambda: self.renderer.render(
                    timeline=composition.timeline, settings=settings
                ),
            )
            result.render_job = render_job
            project.render_job_id = render_job.render_job_id
        else:
            project.stage(PipelineStage.RENDERING).status = Status.PENDING

        project.total_cost_usd = self.ledger.total_usd
        project.status = (
            Status.READY if project.current_stage is None else Status.PROCESSING
        )
        # The honest answer, written where it persists. A client that reads
        # `status == ready` and ships a mute video is a client we misled.
        project.outcome = result.outcome.value
        project.degradation_notes = result.degradation_summary[:16]
        project.narration_has_speech = result.narration_has_speech
        if result.render_job is not None and result.render_job.duration_seconds:
            project.rendered_duration_seconds = result.render_job.duration_seconds
        return result

    # -- stage plumbing ---------------------------------------------------

    async def _stage(
        self,
        project: Project,
        stage: PipelineStage,
        run: Callable[[], Any],
        *,
        is_async: bool = True,
    ) -> Any:
        state = project.stage(stage)
        state.status = Status.PROCESSING
        timer = Timer()
        try:
            value = await run() if is_async else run()
        except VTVError as error:
            state.status = Status.FAILED
            state.error = error.info
            self.events.emit(
                EventName.STAGE_FAILED,
                project_id=project.project_id,
                duration_ms=timer.elapsed_ms,
                data={"stage": stage.value, "code": error.info.code.value},
            )
            project.status = Status.FAILED
            raise
        state.status = Status.READY
        state.cost_usd = 0.0
        return value

    @staticmethod
    def _fail(project: Project, stage: PipelineStage, error: object) -> None:
        state = project.stage(stage)
        state.status = Status.FAILED
        state.error = error  # type: ignore[assignment]
        project.status = Status.FAILED


__all__ = ["Pipeline", "PipelineResult"]


def _synthetic_recording(
    *,
    organisation_id: str,
    project_id: str,
    audio: object,
    duration: float,
    language: str,
) -> Recording:
    """A `Recording` describing a synthesised narration track.

    Everything downstream expects one, and the timeline is built from its
    duration. The source is `DOCUMENT`, never `MICROPHONE`, so no report can
    ever count this as a user's voice.
    """
    return Recording(
        organisation_id=organisation_id,
        project_id=project_id,
        source=CaptureSource.DOCUMENT,
        format=AudioFormat.WAV_PCM,
        audio=audio,  # type: ignore[arg-type]
        properties=AudioProperties(
            duration_seconds=round(duration, 3),
            sample_rate_hz=44_100,
            channels=1,
        ),
        declared_language=language[:16],
        status=Status.READY,
    )


def _new_project(
    organisation_id: str | None, style: StyleProfile | None
) -> Project:
    """Create a project, refusing to create an ownerless one.

    Every project belongs to exactly one tenant. Before the 2026-08-13 audit
    this field was optional and the API read a null owner as "unowned, so
    anyone may read it". Requiring it here means the pipeline cannot
    manufacture the fail-open case even when called directly by a script, a
    worker or a test.
    """
    if not organisation_id:
        raise VTVError(
            "a pipeline run needs either a project or an organisation_id",
            code=ErrorCode.PERMISSION_DENIED,
            user_message="This request is not associated with an organisation.",
        )
    return Project(
        organisation_id=organisation_id,
        style=style or StyleProfile(),
        persistence=PersistenceMode.TEMPORARY,
    )
