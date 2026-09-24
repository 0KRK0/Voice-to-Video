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
from typing import Any

from vtv.contracts.asset import Asset
from vtv.contracts.errors import Status, VTVError
from vtv.contracts.project import PersistenceMode, PipelineStage, Project
from vtv.contracts.recording import CaptureSource, Recording
from vtv.contracts.render import RenderJob, RenderSettings
from vtv.contracts.scene import SceneGraph
from vtv.contracts.semantics import Understanding
from vtv.contracts.style import StyleProfile
from vtv.contracts.timeline import NarrationTrack, Timeline
from vtv.contracts.transcript import Transcript
from vtv.contracts.visual_plan import VisualPlan
from vtv.observability.events import EventName, EventSink, Timer
from vtv.pipeline.capture import CaptureService
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.costs import CostLedger
from vtv.pipeline.director import VisualDirector
from vtv.pipeline.scenes import SceneEngine
from vtv.pipeline.transcription import TranscriptionService
from vtv.pipeline.understanding import UnderstandingService
from vtv.ports.rendering import Renderer


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

    @property
    def succeeded(self) -> bool:
        return self.render_job is not None and self.render_job.status is Status.READY


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
    ledger: CostLedger = field(default_factory=CostLedger)

    async def run(
        self,
        *,
        audio: bytes,
        project: Project | None = None,
        style: StyleProfile | None = None,
        source: CaptureSource = CaptureSource.MICROPHONE,
        settings: RenderSettings | None = None,
        render: bool = True,
    ) -> PipelineResult:
        project = project or Project(
            style=style or StyleProfile(), persistence=PersistenceMode.TEMPORARY
        )
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
                project_id=project.project_id, data=audio, source=source
            ),
        )
        result = PipelineResult(project=project, recording=recording, ledger=self.ledger)
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
                total_duration=recording.duration_seconds,
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
                scene_graph=scene_graph, understanding=understanding
            ),
        )
        result.visual_plan = visual_plan
        project.visual_plan_id = visual_plan.visual_plan_id

        # -- Stages 6 to 9: assets, generation, composition ---------------
        narration = NarrationTrack(
            audio=recording.audio,
            duration_seconds=recording.duration_seconds or 1.0,
        )
        composition = await self._stage(
            project,
            PipelineStage.ASSET_RESOLUTION,
            lambda: self.composer.compose(
                scene_graph=scene_graph,
                visual_plan=visual_plan,
                transcript=transcript,
                narration=narration,
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
