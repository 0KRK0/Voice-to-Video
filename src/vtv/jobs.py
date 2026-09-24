"""Stage 27 — the work the worker does, defined once.

Before the 2026-08-13 audit these handlers lived as closures inside
`create_app`, which is why rendering happened inside the API's event loop: the
only process that knew how to run a job was the one serving requests. One
thirty-second render stalled every other request on that process, including
health checks.

They live here now because they belong to neither side. The API imports this
module to know the *names* of the job kinds it may enqueue; the worker imports
it to know how to run them. Neither imports the other.

**Payloads are contracts.** A job payload outlives the process that created it —
that is the point of a durable queue — so it is a validated model rather than a
loose dict. A worker running last week's code must be able to reject next week's
payload rather than misread it.

**Handlers are idempotent.** The queue delivers at least once. A handler that is
re-run after a crash must converge on the same result, which is why usage
settlement carries an idempotency key and why the render output key is derived
from the job rather than from the clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field

from vtv.billing.plans import QuotaKind
from vtv.billing.usage import UsageMeter
from vtv.contracts.base import Id, VTVModel
from vtv.contracts.consistency import VisualBible
from vtv.contracts.errors import ErrorCode, NotFound, Status, VTVError
from vtv.contracts.project import Project
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.tenancy import AuditAction, Principal
from vtv.dispatch import CLOUD_KIND
from vtv.observability.events import EventName, EventSink
from vtv.pipeline.orchestrator import PipelineResult
from vtv.retention import RetentionService
from vtv.security.audit import AuditLog


class JobKind(str, Enum):
    """Every kind of background work. The API may enqueue only these."""

    #: A recording becomes a video.
    RENDER_RECORDING = "render_recording"
    #: A document becomes a video.
    RENDER_DOCUMENT = "render_document"
    #: Cross-tenant retention, run on a schedule by the worker under a system
    #: principal. Deliberately not reachable over HTTP (see `docs/SECURITY.md`).
    RETENTION_SWEEP = "retention_sweep"

    # The product layer. Each of these is a job for the same reason: it spends
    # money or minutes. Everything the editor does that is free is synchronous.
    #: A language model proposes a script revision. Produces a proposal; the
    #: script is unchanged until the user accepts it.
    REVISE_SCRIPT = "revise_script"
    #: One visual gets a new version. Exactly one — the economics of the
    #: product depend on disliking a shot being cheap.
    REGENERATE_VISUAL = "regenerate_visual"
    #: Render from the edited timeline, whole or in part.
    RENDER_SCOPE = "render_scope"

    #: A resolved timeline becomes a video, in the cloud.
    #:
    #: The same work `render_timeline` offers to a customer's own computer, run
    #: here instead. A **separate kind**, not a flag on the payload, because the
    #: queue hands out work by kind: one kind claimed by both a device and a
    #: cloud worker is two renderers racing for the same row, and whichever
    #: loses has already started drawing. The kind is the routing decision, made
    #: once, at the moment somebody presses the button.
    RENDER_TIMELINE_CLOUD = CLOUD_KIND


class RenderPayload(VTVModel):
    """What a render job needs to run, and nothing else.

    Note the absence of the audio bytes. Large inputs go to object storage and
    the job carries a reference: a queue row is not a blob store, and a payload
    that grows with the upload is a queue that fails on the biggest customer.
    """

    project_id: Id
    organisation_id: Id
    #: Storage key of the uploaded bytes. Durable, so a worker on another
    #: machine can fetch it, which a local temp path would not allow.
    input_key: str = Field(min_length=1, max_length=512)
    input_content_type: str = Field(default="application/octet-stream", max_length=128)
    filename: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, max_length=32)
    quality: RenderQuality = RenderQuality.PREVIEW
    frame_rate: int = Field(default=24, ge=24, le=60)
    #: Set when the API reserved quota up front. The handler settles it.
    reservation_id: str | None = Field(default=None, max_length=64)
    #: The transcription allowance, held separately. One field could not carry
    #: both: rendered minutes and transcribed minutes are settled against
    #: different measurements — the finished video's duration and the source
    #: audio's — and a run that transcribes and then fails to render must give
    #: one back while charging the other.
    transcription_reservation_id: str | None = Field(default=None, max_length=64)
    #: Development-only aligner script. Refused in production by the API.
    script: str | None = Field(default=None, max_length=20000)


class SweepPayload(VTVModel):
    """Retention for one tenant, or for all of them when run by the scheduler."""

    organisation_id: Id | None = None
    older_than_hours: float = Field(default=24.0, gt=0)


@dataclass
class JobContext:
    """Everything a handler needs, injected rather than reached for.

    A handler that could reach for a global would be a handler that behaves
    differently in the worker than in a test.
    """

    assembly: Any  # vtv.wiring.Assembly
    repository: Any  # SqliteProjectRepository
    usage: UsageMeter
    audit: AuditLog
    events: EventSink
    scratch: Path
    #: The queue this handler is running under, for work that produces more
    #: work. A Studio render bound for somebody's own computer is exactly
    #: that: the cloud does the part that needs credentials — narration,
    #: sourcing, flattening — and then hands the drawing to a device, which
    #: is a second job it must be able to enqueue.
    #:
    #: Optional because most handlers do not enqueue anything and the two
    #: test fixtures that build a context by hand should not have to invent
    #: a queue to exercise a handler that never touches one.
    queue: Any = None


async def _approved_bible(context: JobContext, project: Project) -> VisualBible | None:
    """The Visual Bible an earlier run of this project already approved.

    P1-1. Without this, every render rebuilt the Bible from scratch, so a
    binding a user had *locked* — pressed "keep this" on — was silently
    discarded on the next render. A control that does not survive a re-render
    is not a control.

    A document written by an older schema is ignored rather than fatal: losing
    the continuity of one project is much better than refusing to render it.
    """
    payload = await context.repository.get_document(
        project_id=project.project_id, kind="visual_bible"
    )
    if payload is None:
        return None
    try:
        return VisualBible.model_validate(payload)
    except ValueError:
        return None


async def run_render_recording(context: JobContext, payload: dict[str, Any]) -> str:
    """A recording becomes a video. Runs in the worker, never in the API."""
    job = RenderPayload.model_validate(payload)
    project = await _load_project(context, job)

    audio = await context.assembly.storage.get_by_key(job.input_key)
    if job.script and not context.assembly.settings.is_production:
        context.assembly.register_script_for_key(job.input_key, job.script)

    result = await context.assembly.pipeline.run(
        audio=audio,
        project=project,
        style=project.style,
        visual_bible=await _approved_bible(context, project),
        settings=RenderSettings(
            aspect_ratio=project.style.aspect_ratio,
            quality=job.quality,
            frame_rate=job.frame_rate,
        ),
    )
    await _finish(context, job, result)
    if result.succeeded:
        # Stage: the bridge to Studio. Deferred import for the reason
        # `_product_handlers` below gives one: `vtv.product_bridge` imports
        # `JobContext` from this module, so importing it at module scope would
        # close an import cycle that importing it here, after this module has
        # finished loading, does not.
        from vtv.product_bridge import derive_product_documents

        await derive_product_documents(context, result)
    return job.project_id


async def run_render_document(context: JobContext, payload: dict[str, Any]) -> str:
    """A document becomes a video, through the identical downstream path."""
    job = RenderPayload.model_validate(payload)
    project = await _load_project(context, job)

    data = await context.assembly.storage.get_by_key(job.input_key)
    document, _transcript, _document_context = context.assembly.ingestion.ingest(
        data,
        organisation_id=project.organisation_id,
        project_id=project.project_id,
        filename=job.filename,
        origin=job.filename,
        language=job.language,
    )
    result = await context.assembly.pipeline.run_from_document(
        document=document,
        project=project,
        style=project.style,
        visual_bible=await _approved_bible(context, project),
        settings=RenderSettings(
            aspect_ratio=project.style.aspect_ratio,
            quality=job.quality,
            frame_rate=job.frame_rate,
        ),
    )
    await _finish(context, job, result)
    return job.project_id


async def run_render_timeline_cloud(
    context: JobContext, payload: dict[str, Any]
) -> str:
    """Draw an already-resolved timeline here, because no computer of theirs will.

    ## Why this is the shortest handler in the file

    Everything expensive has already happened. By the time a timeline exists,
    the transcription is done, the language model has been paid, the assets have
    been found and licensed, and what remains is pixels. That is exactly why
    this work is safe to offer to a customer's own machine — and it is why the
    cloud version of it needs no providers, no credentials and no pipeline: it
    is the same last stage, run on a different processor.

    ## Why it does not reuse the device's storage key

    It does not upload; the renderer stores its own output and hands back an
    `ObjectRef`. Nothing downstream reconstructs a key to find a video —
    `render_job.output` is the only route — so the two execution paths are free
    to write wherever suits them.
    """
    from vtv.contracts.timeline import Timeline

    project_id = str(payload.get("project_id") or "")
    organisation_id = str(payload.get("organisation_id") or "")
    render_job_id = str(payload.get("render_job_id") or "")
    project = await context.repository.get_project(
        project_id, organisation_id=organisation_id
    )
    if project is None:
        raise NotFound(f"unknown project {project_id}")

    document = await context.repository.get_document(
        project_id=project_id, kind=Timeline.document_name
    )
    if document is None:
        raise NotFound(f"project {project_id} has no timeline to render")
    timeline = Timeline.model_validate(document)
    # The tenant check at the one place cloud rendering reads content, matching
    # the one in `DevicePool._assignment`. A worker is not a trusted caller
    # simply because it runs on our own hardware.
    if timeline.organisation_id != organisation_id:
        raise NotFound(f"unknown project {project_id}")

    job = await context.assembly.pipeline.renderer.render(
        timeline=timeline,
        settings=RenderSettings.model_validate(payload.get("settings") or {}),
    )
    # Recorded under the id the request was told about. The renderer mints its
    # own, and a customer who was handed one id at the button and finds another
    # in their render history has been given two names for one thing.
    job = job.model_copy(update={"render_job_id": render_job_id or job.render_job_id})
    await context.repository.put_document(
        project_id=project_id,
        kind=job.document_name,
        document_id=job.render_job_id,
        payload=json.loads(job.model_dump_json()),
    )
    project.render_job_id = job.render_job_id
    if job.duration_seconds:
        project.rendered_duration_seconds = job.duration_seconds
    await context.repository.save_project(project)
    return project_id


async def run_retention_sweep(context: JobContext, payload: dict[str, Any]) -> str:
    """Delete what has passed its retention.

    Runs under a system principal in the worker. The HTTP surface has a
    tenant-scoped equivalent; the cross-tenant form lives only here, because an
    unauthenticated endpoint that deletes every tenant's data is precisely the
    defect the audit found.
    """
    job = SweepPayload.model_validate(payload)
    principal = Principal.system("retention-sweep", job.organisation_id)

    # P1-7. One implementation, shared with the API's tenant-scoped route.
    # There used to be two, and only one of them applied a tenant scope.
    service = RetentionService(
        repository=context.repository,
        storage=context.assembly.storage,
        # The plan's `max_retention_days` is a ceiling on BYTES, not just on
        # rows. Without the directory the tier cannot be resolved, so no ceiling
        # is applied at all and the sweep keeps everything — deliberately fail-
        # closed, but it means the plan governs nothing until this is passed.
        directory=context.assembly.directory,
    )

    organisations = (
        [job.organisation_id]
        if job.organisation_id
        else context.assembly.directory.active_organisation_ids()
    )

    projects = 0
    objects = 0
    for organisation_id in organisations:
        for report in await service.sweep(organisation_id=organisation_id):
            projects += report.records_deleted
            objects += report.objects_deleted
        objects += len(
            await service.sweep_orphans(
                organisation_id=organisation_id,
                older_than_seconds=job.older_than_hours * 3600,
            )
        )

    context.audit.write(
        action=AuditAction.DATA_DELETED,
        principal=principal,
        organisation_id=job.organisation_id,
        target=f"organisation:{job.organisation_id or 'all'}",
        detail={
            "organisations": str(len(organisations)),
            "projects_deleted": str(projects),
            "objects_deleted": str(objects),
            "trigger": "scheduled",
        },
    )
    return f"{projects}:{objects}"


# ---------------------------------------------------------------------------
# Shared handler plumbing
# ---------------------------------------------------------------------------

def _prefix_for(organisation_id: str | None) -> str | None:
    if organisation_id is None:
        return None
    from vtv.security.paths import tenant_prefix

    return tenant_prefix(organisation_id)


async def _load_project(context: JobContext, job: RenderPayload) -> Project:
    """Load the project, scoped to the tenant named in the payload.

    The scope matters even here. A payload is data, and data can be wrong or
    forged; loading by id alone would let a malformed job touch another
    tenant's project.
    """
    project: Project | None = await context.repository.get_project(
        job.project_id, organisation_id=job.organisation_id
    )
    if project is None:
        raise NotFound(
            f"project {job.project_id} is not available to this tenant",
            code=ErrorCode.ASSET_NOT_FOUND,
        )
    return project


async def _finish(
    context: JobContext, job: RenderPayload, result: PipelineResult
) -> None:
    """Persist everything, settle usage, and drop the input.

    Order matters. Persistence first, because an artifact nobody can find is
    the same as no artifact. Settlement second, keyed so a re-delivered job
    bills once. The input is removed last, and only on success — a retry after
    a crash still needs it.
    """
    await persist_result(context.repository, result)
    _settle(context, job, result)
    _settle_transcription(context, job, result)
    _record_generated_assets(context, job, result)

    if result.succeeded:
        await context.assembly.storage.delete_by_key(job.input_key)

    context.events.emit(
        EventName.JOB_COMPLETED,
        project_id=result.project.project_id,
        data={
            "succeeded": result.succeeded,
            "outcome": result.outcome.value,
            "has_speech": result.narration_has_speech,
            "grounding_refusals": (
                result.gate_outcome.refusals if result.gate_outcome else 0
            ),
        },
    )


def _settle(context: JobContext, job: RenderPayload, result: PipelineResult) -> None:
    if not job.reservation_id:
        return
    render_job = result.render_job
    if render_job is None or render_job.status is not Status.READY:
        # Nothing was delivered, so nothing is charged. Releasing immediately
        # beats waiting for the TTL to reclaim it.
        context.usage.release(job.reservation_id)
        return
    context.usage.settle(
        job.reservation_id,
        actual=round((render_job.duration_seconds or 0.0) / 60.0, 4),
        cost_usd=result.ledger.total_usd if result.ledger else 0.0,
        project_id=result.project.project_id,
        # Derived from the render job, so a re-delivered message settles once.
        idempotency_key=f"render:{result.project.project_id}:{render_job.render_job_id}",
    )
    context.usage.record(
        organisation_id=job.organisation_id,
        kind=QuotaKind.PROVIDER_SPEND_USD,
        quantity=result.ledger.total_usd if result.ledger else 0.0,
        project_id=result.project.project_id,
        idempotency_key=f"spend:{result.project.project_id}:{render_job.render_job_id}",
    )


def _settle_transcription(
    context: JobContext, job: RenderPayload, result: PipelineResult
) -> None:
    """Charge transcription against the *measured* audio, not the estimate.

    `TRANSCRIBED_MINUTES` is declared on all five plans and was recorded
    nowhere, so `/v1/usage` reported every tenant as having used none of it. The
    API reserves an estimate from the uploaded byte count before the job is
    enqueued — that is what stops ten concurrent uploads each passing the same
    check — and this settles the reservation against the duration the capture
    stage actually measured.

    Nothing is charged when there is no transcript: a run that failed at capture
    sent no audio to a speech-to-text provider, and holding the allowance until
    the reservation's TTL expired would cost the tenant an hour of headroom for
    work that never happened.

    No ``cost_usd``. The speech-to-text spend is already in the run's ledger and
    is charged once, at the render settlement below; adding it here would double
    the provider cost on every margin report.
    """
    if not job.transcription_reservation_id:
        return
    if result.transcript is None or result.recording.status is not Status.READY:
        context.usage.release(job.transcription_reservation_id)
        return
    context.usage.settle(
        job.transcription_reservation_id,
        actual=round((result.recording.duration_seconds or 0.0) / 60.0, 4),
        project_id=result.project.project_id,
        # Derived from the recording, so a re-delivered message settles once.
        idempotency_key=(
            f"transcription:{result.project.project_id}:"
            f"{result.recording.recording_id}"
        ),
    )


def _record_generated_assets(
    context: JobContext, job: RenderPayload, result: PipelineResult
) -> None:
    """Meter the images and video this run actually bought.

    `GENERATED_ASSETS` is the quota whose Free-tier limit is zero, and until now
    nothing counted it, so `/v1/usage` showed every tenant at zero of their
    allowance whether they had generated two assets or two thousand.

    Counted from the cost ledger rather than from the number of scenes, because
    the ledger is what the invoice is reconciled against. Two exclusions, both
    of which a customer would argue for:

    * cache hits — a reused image was bought once, by the render that first
      asked for it; charging every later render for it would turn the cache
      from a saving into a cost;
    * zero-cost entries — a stub or local generator bought nothing from a
      provider, and this quota counts assets *bought*.

    This is the "record after" half, kept even now that the "check before" half
    is wired: `billing/usage.GeneratedAssetAllowance` is attached to
    `GenerationRouter.asset_authoriser` by `vtv.wiring.build` and refuses a
    call before it is dispatched, but the authoriser's own tally is held in
    memory and per process. This record is what a restarted process, a second
    replica and the customer's own invoice all agree on.
    """
    from vtv.contracts.generation import GenerationKind

    if result.ledger is None:
        return
    generated = sum(
        1
        for entry in result.ledger.entries
        if entry.kind in (GenerationKind.IMAGE, GenerationKind.VIDEO)
        and not entry.from_cache
        and entry.usd > 0
    )
    if generated == 0:
        return
    render_job = result.render_job
    # Keyed on the render job when there is one, so a re-delivered message
    # records once. A run that never produced a render job falls back to the
    # recording, which is new per attempt — a genuine re-run bought genuinely
    # new assets, so counting it again is the correct answer rather than a gap.
    handle = (
        render_job.render_job_id
        if render_job is not None
        else result.recording.recording_id
    )
    context.usage.record(
        organisation_id=job.organisation_id,
        kind=QuotaKind.GENERATED_ASSETS,
        quantity=float(generated),
        project_id=result.project.project_id,
        idempotency_key=f"assets:{result.project.project_id}:{handle}",
    )


async def persist_result(repository: Any, result: PipelineResult) -> None:
    """Store every document the run produced.

    This is what makes artifacts survive a restart, and it is why the API can
    serve a video it never rendered. The Visual Bible is included — it was
    omitted before the audit, which is why "re-render reproduces the approved
    video" was not true across processes.
    """
    await repository.save_project(result.project)
    documents = {
        "recording": result.recording,
        "transcript": result.transcript,
        "understanding": result.understanding,
        "scene_graph": result.scene_graph,
        "visual_plan": result.visual_plan,
        "visual_bible": result.visual_bible,
        "source_document": result.source_document,
        "timeline": result.timeline,
        "render_job": result.render_job,
    }
    import json

    for kind, document in documents.items():
        if document is None:
            continue
        await repository.put_document(
            project_id=result.project.project_id,
            kind=kind,
            document_id=getattr(document, f"{kind}_id", kind),
            payload=json.loads(document.model_dump_json()),
        )


#: Job kind to handler. The worker registers exactly this; the API registers
#: nothing, which is what stops it running work by accident.
def _product_handlers() -> dict[str, Any]:
    """The editor's handlers, imported late.

    `vtv.product_jobs` imports from `vtv.api.product` for its document-kind
    constants, and `vtv.api.app` imports from here. Importing at module scope
    would close that cycle; importing at call time breaks it without either
    module having to duplicate a string the other owns.
    """
    from vtv.product_jobs import HANDLERS as PRODUCT_HANDLERS

    return dict(PRODUCT_HANDLERS)


HANDLERS: dict[str, Any] = {
    JobKind.RENDER_RECORDING.value: run_render_recording,
    JobKind.RENDER_DOCUMENT.value: run_render_document,
    JobKind.RENDER_TIMELINE_CLOUD.value: run_render_timeline_cloud,
    JobKind.RETENTION_SWEEP.value: run_retention_sweep,
    **_product_handlers(),
}


def unavailable(kind: str) -> VTVError:
    return VTVError(
        f"no handler for job kind {kind!r}",
        code=ErrorCode.INTERNAL_ERROR,
        user_message="That kind of work is not available on this deployment.",
    )


__all__ = [
    "HANDLERS",
    "JobContext",
    "JobKind",
    "RenderPayload",
    "SweepPayload",
    "persist_result",
    "run_render_document",
    "run_render_recording",
    "run_render_timeline_cloud",
    "run_retention_sweep",
    "unavailable",
]
