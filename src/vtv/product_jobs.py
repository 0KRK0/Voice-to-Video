"""Background handlers for the product layer.

Three jobs, and the reason each is a job rather than a request:

* **`revise_script`** calls a language model. Seconds, and money.
* **`regenerate_visual`** calls an image or video provider, or fetches an
  asset. Seconds to minutes, and more money.
* **`render_scope`** encodes video. Minutes, and CPU.

Everything else the editor does — moving a clip, locking a visual, choosing a
version — is synchronous, because it costs nothing and a user who dragged a clip
expects it to have moved.

## Idempotency is not optional here

These are the operations a retry can charge twice for. The queue deduplicates on
an idempotency key, and each handler is additionally written so that running it
twice converges: a revision that already exists is returned rather than
re-requested, and a regeneration checks the unit's version count before spending.

The pattern is the one billing already uses — reserve, do the work, settle
against a key — and it is here for the same reason: at-least-once delivery is
only safe when the effects are idempotent.

## Scoped rendering

`render_scope` is what makes disliking one visual cheap. It renders the region
the change touched and concatenates it into the existing output. A full render
remains available and is what an export uses.

**Status: the scoped path currently re-renders the whole timeline and records
the region it was asked for.** The region arithmetic, the boundary expansion and
the job plumbing are real and tested; the ffmpeg-level segment splice is not
implemented, and this module says so rather than letting the API imply a saving
that is not being made. `docs/FINAL_REPORT.md` carries it as remaining work.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

#: Document kinds, shared with `api/product.py`. Imported from there so the two
#: cannot drift; a mismatched string would silently write documents nothing
#: reads.
from vtv.api.product import (
    SCRIPT_DOC,
    TIMELINE_DOC,
    UNITS_DOC,
)
from vtv.contracts.base import Id, VTVModel
from vtv.contracts.errors import ErrorCode, NotFound, VTVError
from vtv.contracts.generation import GenerationKind
from vtv.contracts.project import Project
from vtv.contracts.render_scope import RenderRegion
from vtv.contracts.script import RevisionKind, Script
from vtv.contracts.tenancy import AuditAction, Principal
from vtv.contracts.tracks import EditTimeline
from vtv.contracts.visual_unit import RegenerationIntent, VisualUnit
from vtv.jobs import JobContext
from vtv.observability.events import EventName


class RevisePayload(VTVModel):
    organisation_id: Id
    project_id: Id
    kind: str
    block_ids: list[str] = []
    target_language: str | None = None
    based_on_version: int = 1


class RegeneratePayload(VTVModel):
    organisation_id: Id
    project_id: Id
    visual_unit_id: Id
    intent: str = RegenerationIntent.SAME_IDEA.value


class RenderScopePayload(VTVModel):
    organisation_id: Id
    project_id: Id
    region: dict[str, Any] = {}
    #: Where this render was routed, decided when the button was pressed.
    #:
    #: Never "auto": the server resolved that at the API, at the moment
    #: somebody could see the answer. A job that decided for itself, an hour
    #: later, would be answering a question about a different instant and
    #: routing somewhere the person was never shown.
    #:
    #: Defaults to "cloud" so a payload written before this field existed
    #: still runs, and runs the way it always did.
    execution: str = "cloud"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def run_revise_script(context: JobContext, payload: dict[str, Any]) -> str:
    """Ask a model for a revision and store it as a proposal.

    Stores a *proposal*. Nothing about the script changes here — the user
    accepts or rejects through the API, and that is the only path that touches
    `current_text`. A background job that edited someone's words while they were
    not looking would be the exact failure this design exists to prevent.
    """
    job = RevisePayload.model_validate(payload)
    # Re-check the tenant here, not only at the API. A worker reads its project
    # id from a payload, and a payload is not a credential — the other two
    # handlers check, and a handler that does not is the one an attacker with
    # any queue-write path would aim at.
    await _project(context, job.project_id, job.organisation_id)
    script = await _load(context, job.project_id, SCRIPT_DOC, Script)
    if script is None:
        raise NotFound("this project has no script to revise")

    if script.version != job.based_on_version:
        # The script moved while the job waited. Proposing against text the user
        # has already changed produces a diff they will not recognise.
        raise VTVError(
            "the script changed before this revision ran",
            code=ErrorCode.SCHEMA_INVALID,
            user_message="Your script changed — ask for the suggestion again.",
        )

    from vtv.pipeline.revision import RevisionService

    service = RevisionService(router=context.assembly.router, events=context.events)
    proposal = await service.propose(
        script,
        kind=RevisionKind(job.kind),
        block_ids=job.block_ids or None,
        target_language=job.target_language,
    )
    await context.repository.put_document(
        project_id=job.project_id,
        kind=f"revision:{proposal.revision_id}",
        document_id=proposal.revision_id,
        payload=json.loads(proposal.model_dump_json()),
    )
    return proposal.revision_id


async def run_regenerate_visual(context: JobContext, payload: dict[str, Any]) -> str:
    """Produce a new version of one visual, and nothing else.

    The units document is read, one entry is replaced, and it is written back.
    Every other unit is untouched — not re-planned, not re-fetched, not
    re-charged — which is the property the product's economics depend on.
    """
    job = RegeneratePayload.model_validate(payload)
    project = await _project(context, job.project_id, job.organisation_id)

    units = await _load_units(context, job.project_id)
    index = next(
        (i for i, u in enumerate(units) if u.visual_unit_id == job.visual_unit_id),
        None,
    )
    if index is None:
        raise NotFound("no such visual in this project")
    unit = units[index]

    script = await _load(context, job.project_id, SCRIPT_DOC, Script)
    narration = _narration_for(unit, script)

    from vtv.pipeline.regeneration import (
        GateValidator,
        RegenerationService,
    )

    service = RegenerationService(
        producer=_DirectorProducer(context=context, project=project),
        validator=GateValidator(
            evidence=_evidence_for(narration),
            visual_bible=await _load_bible(context, job.project_id),
        ),
        events=context.events,
    )
    timeline = await _load(context, job.project_id, TIMELINE_DOC, EditTimeline)
    result = await service.regenerate(
        unit,
        narration=narration,
        intent=RegenerationIntent(job.intent),
        timeline=timeline,
    )

    units[index] = result.unit
    await _store_units(context, job.project_id, units)

    # The timeline shows what the unit now shows. Rebuilt only for this clip:
    # replacing the whole timeline would discard every manual edit the user has
    # made to the others.
    if result.accepted and timeline is not None:
        timeline = _retarget(timeline, result.unit)
        await context.repository.put_document(
            project_id=job.project_id,
            kind=TIMELINE_DOC,
            document_id=timeline.edit_timeline_id,
            payload=json.loads(timeline.model_dump_json()),
        )

    context.audit.write(
        action=AuditAction.PROJECT_UPDATED,
        principal=Principal.system("regenerate", job.organisation_id),
        organisation_id=job.organisation_id,
        target=f"project:{job.project_id}",
        detail={
            "change": "visual_regenerated",
            "visual_unit_id": job.visual_unit_id,
            "intent": job.intent,
            "accepted": str(result.accepted).lower(),
        },
    )
    return job.visual_unit_id


async def _hand_to_a_device(
    context: JobContext, job: Any, project: Any, flattened: Any
) -> bool:
    """Everything up to the drawing happened here. The drawing happens there.

    This is the whole product idea in one function: **the cloud thinks, the
    customer's computer renders.** By this line the narration has been
    synthesised, unclaimed visuals have been sourced through the paid ladder,
    and the edit timeline has been flattened into a render contract. Every step
    that needed a credential is behind us, and what is left is pixels — which is
    the only part that is safe to send to a machine in somebody's spare room.

    ## Why the flattened timeline is written down

    Because a device is handed a *stored* `timeline` document; it is never given
    a queue payload full of megabytes of JSON, and it certainly cannot be handed
    the `EditTimeline`, which carries no narration samples and no sourced
    visuals. Flattening is a cloud act whose result has to outlive the job that
    produced it — a device claiming this work minutes later reads exactly what
    was written here.

    ## Why it can still end up in the cloud

    Because the machine that was online when the button was pressed may not be
    online now. `offer` refuses in that case, and the honest answer is to draw
    it here rather than to fail a render somebody asked for — so the refusal
    falls through to the normal path instead of being raised. What the person
    chose is a preference about where; it is not a preference for no video.
    """
    from vtv.contracts.base import IdPrefix, new_id
    from vtv.contracts.errors import PolicyViolation
    from vtv.contracts.render import RenderSettings
    from vtv.dispatch import DevicePool, Target

    await context.repository.put_document(
        project_id=job.project_id,
        kind=flattened.document_name,
        document_id=flattened.timeline_id,
        payload=json.loads(flattened.model_dump_json()),
    )
    project.timeline_id = flattened.timeline_id
    await context.repository.save_project(project)

    pool = DevicePool(
        queue=context.queue,
        repository=context.repository,
        storage=context.assembly.storage,
        directory=context.assembly.directory,
    )
    render_job_id = new_id(IdPrefix.RENDER_JOB)
    try:
        await pool.offer(
            project_id=job.project_id,
            organisation_id=job.organisation_id,
            render_job_id=render_job_id,
            settings=RenderSettings(aspect_ratio=project.style.aspect_ratio),
            execution=Target.DEVICE,
        )
    except PolicyViolation:
        # Every computer went offline between the button and this line. Say so
        # in the stream and let the caller draw it here; a render that silently
        # never happens is the one outcome worse than a slower one.
        context.events.emit(
            EventName.RENDER_STARTED,
            project_id=job.project_id,
            data={"execution": "cloud", "reason": "no device was still available"},
        )
        return False

    context.events.emit(
        EventName.RENDER_STARTED,
        project_id=job.project_id,
        data={
            "execution": "device",
            "clips": len(flattened.clips),
            "encode": "full_timeline",
            "source": "edit_timeline",
        },
    )
    # Returns without a render job document. The device writes one when it
    # finishes — see `DevicePool._record` — and writing an empty one here would
    # mean the Studio briefly showed a completed render with nothing behind it.
    return True


async def run_render_scope(context: JobContext, payload: dict[str, Any]) -> str:
    """Render from the *edited* timeline.

    Not from the input. Re-running the pipeline would rebuild the timeline and
    throw away every edit the user has made — the locks, the moves, the chosen
    versions — which is the opposite of what "render my project" means once
    there is an editor.

    So this flattens the `EditTimeline` into the render contract and hands that
    to the renderer. What the user arranged is what gets encoded.

    **The scope is bookkeeping, not yet a saving.** The region is validated,
    recorded, audited and reported, and the encode is full-timeline. The
    segment splice that would make a clip-scoped render genuinely cheaper is not
    built; saying so here is the whole reason this paragraph exists.
    """
    job = RenderScopePayload.model_validate(payload)
    project = await _project(context, job.project_id, job.organisation_id)
    region = RenderRegion.model_validate(job.region or {"scope": "full_project"})

    timeline = await _load(context, job.project_id, TIMELINE_DOC, EditTimeline)
    if timeline is None:
        raise NotFound("this project has no timeline to render")

    # Narration audio: the samples an edit timeline never carries. Either a
    # previous run produced it, or this one synthesises it from the script.
    # `timeline` may come back retimed against the voice that was actually
    # recorded — see `_narration_for_render` — and this render must use that
    # one, not the estimate-built one it was called with, or the file this
    # produces would still drift while only the *stored* timeline agreed with
    # the voice.
    narration, scene_graph_id, timeline = await _narration_for_render(
        context, project, timeline
    )

    # Anything the user has never directed still has no picture. Source it now,
    # through the same ladder the Regenerate menu uses — see
    # `_source_unclaimed_visuals` for why this is here and not in the API.
    timeline = await _source_unclaimed_visuals(context, project, timeline)

    from vtv.contracts.render import RenderSettings
    from vtv.pipeline.units import flatten

    flattened = flatten(
        timeline,
        narration=narration,
        style=project.style,
        aspect_ratio=project.style.aspect_ratio,
        scene_graph_id=scene_graph_id,
    )

    # Falls through to the cloud path when the hand-off could not happen, so
    # there is one encode in this function rather than two copies that drift.
    if job.execution == "device" and await _hand_to_a_device(
        context, job, project, flattened
    ):
        return job.project_id

    context.events.emit(
        EventName.RENDER_STARTED,
        project_id=job.project_id,
        data={
            "scope": region.scope.value,
            "start": region.start,
            "end": region.end,
            "clips": len(flattened.clips),
            # Named honestly. An operator reading this must not conclude that a
            # clip-scoped request encoded only a clip.
            "encode": "full_timeline",
            "source": "edit_timeline",
        },
    )

    # Persist the render's progress as it happens.
    #
    # ## Why this subscription exists
    #
    # `project.progress` was written once, when the render finished. Every
    # other view of the system — the projects list, a second browser tab, a
    # phone — therefore showed a four-hour render as 0% for four hours and then
    # 100%, and the only place the real number existed was the SSE stream of
    # the one tab that started it. A user who closed that tab had no way to
    # learn anything about their own job.
    #
    # Segments made the number worth persisting: it moves in steps a person can
    # see, and it is honest — a completed segment is a file on disk, not an
    # estimate. The throttle is because a four-hour render emits about twelve
    # hundred of these and a database write per segment is a lot of writes to
    # move a progress bar a tenth of a percent.
    async with _persisting_progress(context, project):
        job_result = await context.assembly.pipeline.renderer.render(
            timeline=flattened,
            settings=RenderSettings(
                aspect_ratio=project.style.aspect_ratio,
            ),
        )
    await context.repository.put_document(
        project_id=job.project_id,
        kind="render_job",
        document_id=job_result.render_job_id,
        payload=json.loads(job_result.model_dump_json()),
    )

    # Record which timeline this file encodes. Written after the render, not
    # before: a render that failed must not leave the project claiming to be up
    # to date with an edit it never encoded.
    if job_result.output is not None:
        project.rendered_timeline_version = timeline.version
        project.rendered_duration_seconds = job_result.duration_seconds
        # And what actually happened, on the project itself.
        #
        # Until now nothing in this lane wrote `status`, `stages` or
        # `total_cost_usd`. After a three-hour render that produced a file, the
        # stored project still read `status: pending`, every stage `pending`,
        # and `cost_usd: 0.0`. That is not cosmetic:
        #
        # * `Project.progress` is computed from the stages, so the Studio had
        #   no honest number to show and showed none.
        # * `current_stage` was the first stage, forever, on a finished video.
        # * The cost the user was quoted before the render could never be
        #   compared with the cost they were charged.
        #
        # Written from what the job returned rather than assumed: a render that
        # produced no file leaves all of this alone, which is why it is inside
        # this branch.
        _record_progress(project, job_result, spent=context.assembly.ledger)
        await context.repository.save_project(project)
    context.audit.write(
        action=AuditAction.RENDER_SUBMITTED,
        principal=Principal.system("render-scope", job.organisation_id),
        organisation_id=job.organisation_id,
        target=f"project:{job.project_id}",
        detail={
            "scope": region.scope.value,
            "timeline_version": str(timeline.version),
            "encode": "full_timeline",
        },
    )
    return str(job_result.render_job_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Where a synthesised voice track is kept between renders, so that rendering
#: twice does not pay a synthesiser twice.
NARRATION_DOC = "narration_track"

#: How the timings on a cached voice track were arrived at.
#:
#: Bumped when that changes in a way that makes an existing track wrong rather
#: than merely older. `"measured"` means each sentence was synthesised and
#: measured on its own; everything cached before that carries no marker at all
#: and is re-synthesised on the next render, because it is mistimed however well
#: it matches the words.
NARRATION_TIMING = "measured"

#: Where the project-level safety score is kept, for the export screen to read.
SAFETY_DOC = "safety_report"

#: Where the budget plan is kept — how many visuals the project could afford to
#: generate, and what it expects to spend.
BUDGET_DOC = "budget_plan"

#: How many visuals are sourced at once before a render. Four, because the work
#: behind each one is a third party's rate limit rather than our CPU, and hosted
#: image endpoints answer a burst of four and 429 a burst of twenty.
SOURCING_CONCURRENCY = 4

#: How much longer than the timeline's narration lane the real voice may be
#: before the two are considered out of agreement. One frame at 24fps.
_NARRATION_TOLERANCE = 0.042


async def _narration_for_render(
    context: JobContext, project: Project, timeline: EditTimeline
) -> tuple[Any, str, EditTimeline]:
    """The voice this timeline is drawn against, and the scene graph it belongs to.

    ## Why this exists

    Two lanes reach the renderer. The **pipeline lane** (speak, or upload a
    document) synthesises narration first and builds a timeline around it, so
    the audio is already on disk by the time anyone can edit. The **product
    lane** (paste a script, direct the visuals) builds a timeline from
    *estimated* speaking rates and never records a sound — and before this
    function existed it therefore had no render path at all: every export failed
    with "render the project once first", and there was no first render to do.
    That is not a missing button, it is a missing stage, and this is it.

    ## Why it is one function

    Narration is the clock. Every rule in this system about pictures not
    drifting away from the voice — `_deliverable` collapsing interior pacing
    time, the builder laying narration contiguously, `flatten` refusing an
    interior gap — assumes exactly one authoritative audio track per project.
    Producing it in more than one place is how a second, differently-timed
    answer gets in. So there is one place, and it is called from the render job.

    ## What it will not do

    It will not silently render a video whose voice is longer than the lane the
    editor drew for it. The renderer places one continuous file at one offset
    and cuts to the shorter stream, so an over-long voice is not a cosmetic
    mismatch — it is narration that stops mid-sentence with nothing in the
    output to say so. When the real audio does not fit the estimate, the
    measured timings are written back to the script, the audio is kept, and the
    job fails with a sentence naming the remedy. Re-planning then rebuilds the
    timeline from measurement rather than estimate and the next render fits.

    ## Why this also returns the timeline

    Measuring the real voice does not, on its own, close the gap between it and
    the timeline: `TimelineBuilder.build` laid every clip out from the
    *estimate*, and each block's small error over- or under-shoots the next, so
    the pictures and captions drift away from the voice a little more with
    every line even when the total length still looks right. `retime` is the
    fix — a piecewise-linear map from estimated time to measured time, applied
    to every clip boundary — and it has to run before this function's caller
    flattens the timeline for the renderer, not only before the next time
    someone opens the editor. So the timeline this returns is the one the
    caller must render: unchanged on every path that does not just measure a
    fresh voice, retimed and re-persisted on the one that does.
    """
    from vtv.contracts.base import IdPrefix, new_id
    from vtv.contracts.timeline import NarrationTrack
    from vtv.contracts.timeline import Timeline as RenderTimeline

    project_id = project.project_id

    # 1. A full pipeline render already produced one. That is the authority.
    rendered = await _load(context, project_id, "timeline", RenderTimeline)
    if rendered is not None:
        return rendered.narration, rendered.scene_graph_id, timeline

    scene_graph_id = project.scene_graph_id or new_id(IdPrefix.SCENE_GRAPH)

    script = await _load(context, project_id, SCRIPT_DOC, Script)
    if script is None or not any(block.is_narrated for block in script.blocks):
        raise VTVError(
            "this project has no narrated script to voice",
            code=ErrorCode.RENDER_FAILED,
            user_message=(
                "There is nothing to narrate yet. Write or record your script "
                "first, then render."
            ),
        )

    digest = _script_digest(script)

    # 2. A voice track from an earlier attempt, still describing this script.
    stored = await context.repository.get_document(
        project_id=project_id, kind=NARRATION_DOC
    )
    if (
        stored
        and stored.get("text_digest") == digest
        # A track synthesised before sentences were measured individually is
        # cached against the same words and is nonetheless mistimed: it was one
        # request, one total duration, and per-sentence timings scaled to fit.
        # Reusing it would mean the fix never reaches a project that has already
        # been rendered once, which is every project the user cares about.
        # Re-synthesising costs one more call, once, and is the only way the
        # captions can land on the voice.
        and stored.get("timing") == NARRATION_TIMING
    ):
        try:
            track = NarrationTrack.model_validate(stored["narration"])
        except (KeyError, ValueError):
            track = None
        if track is not None:
            _require_fits(track.duration_seconds, timeline)
            return track, str(stored.get("scene_graph_id") or scene_graph_id), timeline

    # 3. Synthesise. This is the step that costs money, and the digest above is
    #    what stops a retry paying for it again.
    service = getattr(context.assembly.pipeline, "narration", None)
    if service is None:
        raise VTVError(
            "this deployment has no narration service",
            code=ErrorCode.PROVIDER_UNAVAILABLE,
            user_message=(
                "This install cannot turn a written script into a video: no "
                "narration is configured."
            ),
        )

    from vtv.observability.trace import traced
    from vtv.pipeline.narration import transcript_from_script

    draft = transcript_from_script(
        script,
        organisation_id=project.organisation_id,
        project_id=project_id,
    )
    with traced(stage="narration"):
        spoken = await service.synthesise(
            draft, organisation_id=project.organisation_id, project_id=project_id
        )

    track = NarrationTrack(
        audio=spoken.audio, duration_seconds=spoken.duration_seconds
    )

    # Keep it before deciding whether it fits. The audio exists either way, and
    # a project that fails the fit check is about to be re-planned and rendered
    # again — charging for the same sentences twice would be the system's
    # mistake, not the user's.
    await context.repository.put_document(
        project_id=project_id,
        kind=NARRATION_DOC,
        document_id=NARRATION_DOC,
        payload={
            "text_digest": digest,
            "timing": NARRATION_TIMING,
            "scene_graph_id": scene_graph_id,
            "narration": json.loads(track.model_dump_json()),
            "provider": spoken.provider,
            "has_speech": spoken.has_speech,
            "degradations": [
                json.loads(step.model_dump_json()) for step in spoken.degradations
            ],
        },
    )

    # The measured truth, written back onto the script. Estimates built the
    # timeline; measurement is what a re-plan must use, and a re-plan that had
    # to guess again would land in exactly the same place.
    measured = _apply_measured(script, spoken.transcript)
    if measured is not None:
        await context.repository.put_document(
            project_id=project_id,
            kind=SCRIPT_DOC,
            document_id=measured.script_id,
            payload=json.loads(measured.model_dump_json()),
        )

        # And the timeline moved to agree with it. Without this the pictures
        # and captions stay exactly where the estimate put them while the
        # voice now plays at the measured times — the drift this whole
        # function's docstring describes. Persisted under the same document
        # kind the editor reads, so the Studio shows the same times the video
        # has rather than the estimate the last plan produced.
        from vtv.pipeline.units import retime

        retimed = retime(timeline, measured)
        if retimed is not timeline:
            timeline = retimed
            await context.repository.put_document(
                project_id=project_id,
                kind=TIMELINE_DOC,
                document_id=timeline.edit_timeline_id,
                payload=json.loads(timeline.model_dump_json()),
            )

    if project.scene_graph_id != scene_graph_id:
        project.scene_graph_id = scene_graph_id
        await context.repository.save_project(project)

    context.events.emit(
        EventName.NARRATION_SYNTHESISED,
        project_id=project_id,
        data={
            "lane": "product",
            "provider": spoken.provider,
            "has_speech": spoken.has_speech,
            "duration_seconds": round(spoken.duration_seconds, 3),
            "degraded": bool(spoken.degradations),
        },
    )

    _require_audible(spoken, context)
    _require_fits(spoken.duration_seconds, timeline)
    return track, scene_graph_id, timeline


def _script_digest(script: Script) -> str:
    """What the narrated words are, independent of ids, order fields or version.

    Two scripts with the same digest produce the same audio, which is the only
    property the cache above needs. Including a block id would invalidate a
    perfectly good voice track every time a line was split and rejoined.
    """
    import hashlib

    material = "".join(
        block.text.strip() for block in script.blocks if block.is_narrated
    )
    material = f"{script.language}{material}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


#: Progress must move this much before it is worth a database write.
#:
#: Two percent is about fifty writes over a whole render, whatever its length —
#: fine-grained enough that a watching user sees the bar move, coarse enough
#: that a four-hour job does not write twelve hundred rows to say so.
PROGRESS_STEP = 0.02


@contextlib.asynccontextmanager
async def _persisting_progress(context: JobContext, project: Project) -> Any:
    """Mirror render progress onto the project record while a render runs.

    Subscribes to the event stream rather than being called by the renderer,
    because the renderer has no repository and should not grow one: it reports
    what it is doing and this layer decides what is worth storing. That also
    means a renderer swapped for another implementation keeps this behaviour
    for free, as long as it emits the same event.

    Every failure here is swallowed. A progress bar that stops updating is a
    cosmetic problem; a render that dies because a progress write failed is the
    user's whole video.
    """
    import asyncio

    written = project.progress or 0.0
    pending = written

    def observe(event: Any) -> None:
        # Records only. The event sink is synchronous and is called from inside
        # the encode loop; a database round trip here would be a database round
        # trip per segment, on the critical path of the render.
        nonlocal pending
        if event.name is not EventName.RENDER_PROGRESS:
            return
        raw = event.data.get("progress")
        if isinstance(raw, int | float):
            pending = max(0.0, min(1.0, float(raw)))

    async def flush() -> None:
        nonlocal written
        while True:
            await asyncio.sleep(1.0)
            if pending - written >= PROGRESS_STEP:
                written = pending
                project.progress = pending
                with contextlib.suppress(Exception):
                    await context.repository.save_project(project)

    unsubscribe = context.events.subscribe(observe)
    writer = asyncio.create_task(flush())
    try:
        yield
    finally:
        writer.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await writer
        unsubscribe()


def _record_progress(project: Project, job_result: Any, spent: Any) -> None:
    """Mark the project finished and record what it cost.

    Every stage this lane actually performs is marked ready. The Studio-first
    lane does not capture or transcribe — a pasted script has no recording —
    so those are marked ready too rather than left pending forever: a stage
    that will never run must not hold `current_stage` at "capture" on a
    finished video.
    """
    from vtv.contracts.errors import Status

    for state in project.stages:
        state.status = Status.READY
    project.status = Status.READY

    # `total_usd` takes no argument — the ledger is per job. A ledger that
    # cannot answer is not a reason to lose a render that produced a file, so
    # this is suppressed rather than raised.
    with contextlib.suppress(Exception):
        project.total_cost_usd = round(float(spent.total_usd()), 6)


def _require_audible(spoken: Any, context: JobContext) -> None:
    """Refuse to encode a narrated video that has no narration.

    ## What this cost

    A forty-minute render ran for three hours and produced a file with a
    2 399-second AAC track measured at **-91 dB** — digital silence. Every
    visual was a title card. The job reported success, the cost was $0.00, and
    the user found out by watching it.

    Nothing was broken in the sense of raising: `NarrationService` has a silent
    fallback for when no synthesiser can be reached, it used it, and it recorded
    `has_speech: False` in the narration document *and* in the
    `narration.synthesised` event. Both were correct. Nothing read either.

    ## When it refuses, and when it only warns

    A deployment with no synthesiser configured is silent by construction and
    that may be deliberate — it warns. A deployment whose configured
    synthesiser returned silence is the failure this exists for, and it raises.

    ## Why this is a refusal and not a degradation

    The ladder degrades a *shot* — a photograph becomes a drawing becomes a
    title card, and the video is still the video. Silence is not a degraded
    narration; it is the absence of the thing the product makes. A user who
    wanted silent slides did not ask this system for them.

    And it is a refusal *here*, before the encode, because the encode is the
    expensive part. Three hours to discover that the first ten seconds were
    already wrong is the difference between a bug and an insult.
    """
    if getattr(spoken, "has_speech", True):
        return

    # Two very different situations produce a silent track, and only one is a
    # failure.
    #
    # A deployment with **no synthesiser configured** is silent by
    # construction. `/health` says `real_speech_synthesis: false`, the operator
    # chose that, and silent slides may be exactly what they want. Refusing
    # would break every such deployment — and every test that renders without
    # credentials — to prevent a problem they do not have.
    #
    # A deployment **with** a synthesiser that returned silence is the failure:
    # something was configured, it was asked, and nothing came back. That is
    # the case worth three hours of somebody's time.
    from vtv.contracts.generation import GenerationKind

    if not context.assembly.router.providers_for(GenerationKind.SPEECH):
        context.events.emit(
            EventName.NARRATION_SYNTHESISED,
            project_id=None,
            data={
                "has_speech": False,
                "alert": "no speech provider is configured; this video is mute",
            },
        )
        return

    raise VTVError(
        "narration synthesised to silence; refusing to encode a mute video",
        code=ErrorCode.PROVIDER_UNAVAILABLE,
        user_message=(
            "We could not produce the voice track for this video, so there is "
            "nothing to narrate it with and we stopped rather than render "
            "forty minutes of silence. This usually means the speech provider "
            "refused every request — check the API key and its quota, then "
            "render again."
        ),
    )


def _require_fits(duration: float, timeline: EditTimeline) -> None:
    """Refuse a voice longer than the lane drawn for it.

    The renderer places one continuous audio file at one offset and encodes to
    the shorter of the two streams. A voice 4 seconds longer than the pictures
    therefore ships as a video that stops talking mid-sentence — no error, no
    event, nothing in the file to say a sentence is missing. That is the one
    failure this product promises never to produce, so it is a raise.

    The other direction is fine and needs no check: a voice shorter than the
    pictures leaves deliberate visual time at the end, which `flatten` already
    measures and records as `fill_seconds`.
    """
    from vtv.contracts.tracks import TrackKind

    lane = timeline.track_of_kind(TrackKind.NARRATION)
    clips = sorted(lane.clips, key=lambda clip: clip.start) if lane else []
    if not clips:
        return
    available = round(clips[-1].end - clips[0].start, 3)
    if duration <= available + _NARRATION_TOLERANCE:
        return

    over = round(duration - available, 2)
    raise VTVError(
        f"narration is {duration:.3f}s but the timeline allows {available:.3f}s",
        code=ErrorCode.RENDER_FAILED,
        user_message=(
            f"The recorded voice is {over}s longer than this timeline was "
            "built for, so rendering it now would cut the narration short. "
            "Re-plan the visuals — the timings measured from the real audio "
            "have been saved, and the next render will fit."
        ),
    )


def _apply_measured(script: Script, transcript: Any) -> Script | None:
    """Copy measured segment timings onto the narrated blocks, in order.

    Positional, because `transcript_from_script` emits one segment per narrated
    block in order and the narration service only ever re-times what it was
    given — it never splits, merges or reorders. If a provider ever did, the
    length check below drops the write rather than attaching one line's timing
    to another line's text.
    """
    blocks = [block for block in script.blocks if block.is_narrated]
    segments = list(getattr(transcript, "segments", []))
    if len(blocks) != len(segments):
        return None

    # Both ends in one assignment. `ScriptBlock` validates on assignment and
    # refuses a half-set pair — a start with no end is a timing nobody can use —
    # so the copy carries the whole update rather than two statements that are
    # briefly, and invalidly, apart.
    narrated = {block.block_id for block in blocks}
    updated = []
    index = 0
    for block in script.blocks:
        if block.block_id not in narrated:
            updated.append(block.model_copy(deep=True))
            continue
        span = segments[index].span
        updated.append(
            block.model_copy(
                deep=True,
                update={
                    "measured_start": round(span.start, 3),
                    "measured_end": round(span.end, 3),
                    "timing_invalidated": False,
                },
            )
        )
        index += 1
    return script.model_copy(deep=True, update={"blocks": updated})


async def _project(context: JobContext, project_id: str, organisation_id: str) -> Project:
    project: Project | None = await context.repository.get_project(
        project_id, organisation_id=organisation_id
    )
    if project is None:
        raise NotFound("no such project")
    return project


async def _load(context: JobContext, project_id: str, kind: str, model: Any) -> Any:
    payload = await context.repository.get_document(project_id=project_id, kind=kind)
    if payload is None:
        return None
    try:
        return model.model_validate(payload)
    except ValueError:
        return None


async def _load_units(context: JobContext, project_id: str) -> list[VisualUnit]:
    payload = await context.repository.get_document(
        project_id=project_id, kind=UNITS_DOC
    )
    if not payload:
        return []
    out: list[VisualUnit] = []
    for item in payload.get("units", []):
        try:
            out.append(VisualUnit.model_validate(item))
        except ValueError:
            continue
    return out


async def _store_units(
    context: JobContext, project_id: str, units: list[VisualUnit]
) -> None:
    await context.repository.put_document(
        project_id=project_id,
        kind=UNITS_DOC,
        document_id=UNITS_DOC,
        payload={"units": [json.loads(u.model_dump_json()) for u in units]},
    )


async def _load_bible(context: JobContext, project_id: str) -> Any:
    from vtv.contracts.consistency import VisualBible

    return await _load(context, project_id, "visual_bible", VisualBible)


def _narration_for(unit: VisualUnit, script: Script | None) -> str:
    if script is None:
        return ""
    return " ".join(
        block.text
        for block_id in unit.script_block_ids
        if (block := script.block(block_id)) is not None
    )


def _evidence_for(narration: str) -> Any:
    """Evidence for the grounding gate, built from the narration alone.

    Narrow on purpose: a regenerated visual must be checked against what is
    actually said under it, not against the whole project. A chart that is
    supported by something said four minutes earlier is not supported *here*.
    """
    if not narration.strip():
        return None
    from vtv.pipeline.grounding import Evidence

    return Evidence.build(narration=narration)


def _retarget(timeline: EditTimeline, unit: VisualUnit) -> EditTimeline:
    """Point this unit's clips at its newly selected version.

    A thin wrapper over `pipeline.units.retarget`, which is now the single
    implementation shared with the two synchronous paths that change what a
    visual shows. There were two implementations of this and only one of them
    ran anywhere except a regeneration; keeping the wrapper preserves this
    module's call site while removing the second copy.
    """
    from vtv.pipeline.units import retarget

    return retarget(timeline, [unit])


class _DirectorProducer:
    """Produces a new visual version by climbing the real sourcing ladder.

    ## What this used to be

    A stub. It built a `TypographySpec` from the first sentence and returned it,
    unconditionally: the same answer for every intent in the menu, and the same
    answer whether or not an image provider was configured. Its rationale told
    the user "no image or video provider is configured" even when one was, and
    its docstring claimed the director's ladder ran here, which was a
    description of code that did not exist. Three of the four menu items —
    "Generate an image", "Generate a video", "Use a real source" — could not do
    what they said, and the fourth did the same thing as the other three.

    ## What it is now

    A thin adapter over `VisualSourcingService`, which reads what the narration
    is trying to show and climbs the ladder for it through the same
    `SceneComposer` the pipeline lane uses. The decision about *what* a visual
    should be still does not live here; only the translation between a
    `VisualUnit` and a scene does.
    """

    def __init__(self, *, context: JobContext, project: Project) -> None:
        self.context = context
        self.project = project

    async def produce(
        self,
        *,
        unit: VisualUnit,
        intent: RegenerationIntent,
        narration: str,
    ) -> Any:
        from vtv.contracts.visual_unit import VisualVersion

        if not narration.strip():
            raise VTVError(
                "there is no narration under this visual to work from",
                code=ErrorCode.VISUAL_PLANNING_FAILED,
                user_message="Add narration to this section first.",
            )

        sourced = await source_one_visual(
            self.context,
            self.project,
            unit=unit,
            narration=narration,
            intent=intent,
        )
        return VisualVersion(
            version=unit.next_version_number,
            strategy=sourced.strategy,
            object=sourced.object,
            asset_id=sourced.asset_id,
            spec=sourced.spec,
            intent=intent,
            rationale=sourced.rationale,
            attribution=sourced.attribution,
        )


async def _source_unclaimed_visuals(
    context: JobContext, project: Project, timeline: EditTimeline
) -> EditTimeline:
    """Give every undirected visual a picture, before the encode.

    ## Why this exists

    `VisualUnitPlanner.plan` groups a script into units and gives them spans. It
    does not give them versions — deciding what a unit *shows* costs money and
    time, and the planner runs inside an API request. So a project typed into
    the Studio arrives at the renderer with thirteen units and no pictures, and
    `flatten` turns a unit with nothing on it into a text clip. That is why a
    whole video came out as title cards even with an image key configured: not a
    provider problem, a stage that was never run.

    ## Why here rather than in the API

    Because it spends money and waits on the network, and this codebase's rule
    is that the API enqueues and workers perform. Doing it in `plan_units` would
    mean an HTTP request holding open while two search providers and an image
    model are polled, once per visual.

    ## What it will not touch

    A unit the user owns — locked, approved, or showing their own upload — and
    any unit that already has a usable version. Re-sourcing those would spend
    money to overwrite a decision somebody made, which is the one thing a
    regeneration must never do.

    A unit whose sourcing fails is left as it was rather than failing the
    render: the ladder's last rung is typography, so "failed" here means
    something structural, and a title card in one section is a better outcome
    than no video.
    """
    units = await _load_units(context, project.project_id)
    if not units:
        return timeline
    script = await _load(context, project.project_id, SCRIPT_DOC, Script)

    pending = [
        unit
        for unit in units
        if not unit.is_user_owned
        and not unit.shows_user_media
        and (unit.selected is None or not unit.selected.is_usable)
    ]
    if not pending:
        return timeline

    import asyncio

    from vtv.contracts.visual_unit import VisualUnitStatus, VisualVersion
    from vtv.observability.trace import traced
    from vtv.pipeline.units import retarget

    # Every line read in one model call rather than one call per visual.
    #
    # Thirteen shots used to mean thirteen round trips asking the same kind of
    # question, each re-sending the same instruction — so most of the tokens
    # paid for were the instruction, not the script. It also gives the model
    # the surrounding lines, which is the difference between "it may change
    # what it means to use a computer" being unsearchable and being obvious.
    narrations = [_narration_for(unit, script) for unit in pending]
    service = _sourcing_service(context)
    with traced(stage="read-intent"):
        concepts = await service.concepts.read_many(
            narrations,
            organisation_id=project.organisation_id,
            project_id=project.project_id,
        )
    by_unit = {
        unit.visual_unit_id: concept
        for unit, concept in zip(pending, concepts, strict=True)
    }

    # How many of these may be bought, decided once for the whole project
    # rather than one shot at a time. See `pipeline/allowance.py`: a per-shot
    # ceiling answers "may this cost that", and the question a user has is
    # "what will this video cost me", which only a plan over all the shots can
    # answer.
    #
    # This runs *before* the director so that the director is told the truth
    # about money the first time it is asked. Deciding first and budgeting
    # afterwards would mean directing every line as though it could be bought
    # and then demoting the ones that could not — two decisions per line, of
    # which the first is discarded.
    allowance = _allowance_for(context, project, pending, by_unit)
    await context.repository.put_document(
        project_id=project.project_id,
        kind=BUDGET_DOC,
        document_id=BUDGET_DOC,
        payload=allowance.as_json(),
    )
    if not allowance.is_unconstrained:
        context.events.emit(
            EventName.VISUAL_UNITS_PLANNED,
            project_id=project.project_id,
            data={
                "stage": "budget_plan",
                "permitted": len(allowance.permitted),
                "withheld": allowance.withheld,
                "projected_usd": allowance.projected_usd,
            },
        )

    # What kind of visual each line gets, decided before anything is fetched.
    # See `sourcing.Direction`. Costs nothing: rules over the reader's output
    # and the Draughtsman's, no network, no model.
    directions = await _direct_all(
        context, project, pending, by_unit, service, allowance, script
    )

    # Which lines the commons can actually illustrate, decided for the whole
    # script at once — and now asked only about the lines whose ladder can
    # reach a photograph at all. See `_judge_commons`.
    commons_picks = await _judge_commons(
        context, project, pending, by_unit, service, directions
    )

    # Concurrently, bounded. Each unit costs a commons search — up to
    # `AssetResolver.deadline_seconds` — and possibly an image generation, and
    # they are independent of one another. Sequentially, a thirteen-visual
    # project would spend several minutes before the encoder started, with the
    # user watching a render that had not begun. The bound is low because the
    # work behind it is somebody else's rate limit, not our CPU.
    gate = asyncio.Semaphore(SOURCING_CONCURRENCY)
    #: Error codes from shots that could not be sourced, for the all-failed
    #: check after the gather.
    failures: list[str] = []

    async def one(unit: VisualUnit) -> VisualUnit | None:
        narration = _narration_for(unit, script)
        if not narration.strip():
            return None
        async with gate:
            try:
                sourced = await source_one_visual(
                    context,
                    project,
                    unit=unit,
                    narration=narration,
                    intent=None,
                    service=service,
                    concept=by_unit.get(unit.visual_unit_id),
                    may_generate=allowance.may_generate(unit.visual_unit_id),
                    # Absent means "the selector did not judge this shot" —
                    # no providers, no candidates, or a failure — and the
                    # ladder proceeds as it always did. Present-and-None is a
                    # decision: no photograph is right for this line.
                    may_use_commons=(
                        unit.visual_unit_id not in commons_picks
                        or commons_picks[unit.visual_unit_id] is not None
                    ),
                    chosen_media=commons_picks.get(unit.visual_unit_id),
                    direction=directions.get(unit.visual_unit_id),
                )
            except Exception as raised:
                # `Exception`, not `VTVError`, and for the reason this whole
                # sourcing stage exists: one visual failing is a title card in
                # one section, and failing the render costs the user the entire
                # video. That trade is not close.
                #
                # It has already happened once the other way round. A Wikimedia
                # scan 16578 pixels wide raised `ValidationError` from
                # `AssetDimensions`, which is not a `VTVError`, so it escaped
                # every handler between the search adapter and the queue and
                # dead-lettered the render after three attempts. The user saw
                # "Something went wrong on our side." and no video.
                code = (
                    raised.info.code.value
                    if isinstance(raised, VTVError)
                    else type(raised).__name__
                )
                failures.append(code)
                context.events.emit(
                    EventName.VISUAL_UNIT_FAILED,
                    project_id=project.project_id,
                    data={
                        "visual_unit_id": unit.visual_unit_id,
                        "stage": "auto_source",
                        "code": code,
                        "detail": str(raised)[:300],
                    },
                )
                return None
        unit.add_version(
            VisualVersion(
                version=unit.next_version_number,
                strategy=sourced.strategy,
                object=sourced.object,
                asset_id=sourced.asset_id,
                spec=sourced.spec,
                rationale=sourced.rationale,
                attribution=sourced.attribution,
            )
        )
        unit.status = VisualUnitStatus.READY
        return unit

    changed = [
        unit for unit in await asyncio.gather(*(one(u) for u in pending)) if unit
    ]

    # One visual failing is a title card in one section. *Every* visual failing
    # the same way is a defect, and the handler above is deliberately broad
    # enough to hide it: a `TypeError` from a call site that forgot an argument
    # looks exactly like a provider being down, thirteen times.
    #
    # That has happened. `AssetResolver.resolve` was called without
    # `organisation_id` for months, so the commons rung raised `TypeError` on
    # every shot of every render, was swallowed per shot, and the product simply
    # never used the commons — with no failure anywhere that anyone would read.
    # Thirteen identical per-unit events are thirteen needles in a log; one
    # event saying "all thirteen, same error" is a bug report.
    if pending and not changed:
        context.events.emit(
            EventName.VISUAL_UNITS_PLANNED,
            project_id=project.project_id,
            data={
                "stage": "auto_source",
                "sourced": 0,
                "units": len(pending),
                "alert": "every visual failed to source",
                "codes": sorted(set(failures))[:4],
            },
        )

    if not changed:
        return timeline

    await _store_units(context, project.project_id, units)
    await _store_safety_report(context, project, units)
    context.events.emit(
        EventName.VISUAL_UNITS_PLANNED,
        project_id=project.project_id,
        data={"stage": "auto_source", "sourced": len(changed), "units": len(units)},
    )

    retargeted = retarget(timeline, changed)
    if retargeted is not timeline:
        timeline = retargeted
        await context.repository.put_document(
            project_id=project.project_id,
            kind=TIMELINE_DOC,
            document_id=timeline.edit_timeline_id,
            payload=json.loads(timeline.model_dump_json()),
        )
    return timeline


async def _store_safety_report(
    context: JobContext, project: Project, units: list[VisualUnit]
) -> None:
    """Score the whole project's material and write it where export can read it.

    Per-visual verdicts are enforcement; this is disclosure. A user about to
    publish needs to know before they press the button that four of their forty
    shots are title cards because we declined to look for pictures — otherwise
    they find out by watching, or never, and either way an editorial decision
    was made on their behalf in silence.

    Derived from the narration rather than from what was sourced. A shot that
    fell to type because the commons was empty is not the same as one that fell
    to type because we would not illustrate it, and only the narration can tell
    them apart.
    """
    from vtv.pipeline.visual_intent import concept_from_rules, report_for

    script = await _load(context, project.project_id, SCRIPT_DOC, Script)
    lines = [
        text
        for unit in units
        if (text := _narration_for(unit, script).strip())
    ]
    if not lines:
        return
    report = report_for([concept_from_rules(line) for line in lines])
    await context.repository.put_document(
        project_id=project.project_id,
        kind=SAFETY_DOC,
        document_id=SAFETY_DOC,
        payload=report.as_json(),
    )
    context.events.emit(
        EventName.VISUAL_UNITS_PLANNED,
        project_id=project.project_id,
        data={
            "stage": "safety_report",
            "total": report.total,
            "text_only": report.text_only,
            "refused": report.refused,
        },
    )


def _allowance_for(
    context: JobContext,
    project: Project,
    pending: list[VisualUnit],
    concepts: dict[str, Any],
) -> Any:
    """The project's plan for what it may buy.

    Priced from the provider's own declared cost — the dearest it could charge
    — rather than an average, because planning against an average puts the plan
    over budget exactly when the expensive case happens, which is the case
    worth planning for.

    With no project budget set the deployment ceiling is *not* substituted:
    that number is an operator's backstop against a runaway loop and it has
    never been a statement about one video. An unlimited allowance here means
    "nobody has stated a budget", and the router's per-call checks still apply.
    """
    from vtv.pipeline.allowance import Shot, UnlimitedAllowance, plan

    if project.budget_usd is None:
        return UnlimitedAllowance()

    price = _price_of_one_image(context, project.visual_fidelity)
    shots = [
        Shot(
            unit_id=unit.visual_unit_id,
            seconds=unit.span.duration if unit.span else 0.0,
            # A shot that could never generate must not consume a slot: it
            # costs nothing, and holding money back for it would withhold a
            # picture from a shot that could have used one.
            could_generate=bool(
                (concept := concepts.get(unit.visual_unit_id)) is None
                or concept.may_generate
            ),
        )
        for unit in pending
    ]
    return plan(shots, budget_usd=project.budget_usd, price_each_usd=price)


async def _direct_all(
    context: JobContext,
    project: Project,
    pending: list[VisualUnit],
    concepts: dict[str, Any],
    service: Any,
    allowance: Any,
    script: Script | None,
) -> dict[str, Any]:
    """Decide the kind of every visual, before any of them is fetched.

    ## The inversion

    The stages that cost money and time used to run first and be told
    afterwards whether they were wanted. A forty-minute essay searched the
    commons for all seven hundred and thirty-six of its lines, sent the
    candidates to a judge, and only then asked the director what each line
    should be — which for most of them was typography. The searching, the two
    providers per line, and the judge's whole input were work done to answer a
    question that had already been settled by the shape of the sentence.

    Now the director runs here, once, on everything. Downstream stages are
    given a decision rather than an opportunity.

    ## Why it is safe to do this eagerly

    Directing costs nothing. `Draughtsman.draw` is rules over one sentence and
    `treatment.decide` is a table of twelve rules over the reader's output —
    no network, no model, no money. Directing four hours of narration is
    arithmetic, which is the property that makes deciding-before-doing
    affordable at every length.

    ## What it emits

    A census. "The agent decides well" is not a claim anybody should accept
    without a number, so the event stream carries what was chosen, how much of
    it is free, and what the paid remainder is projected to cost — before a
    single provider has been called.
    """
    import asyncio

    from vtv.contracts.base import TimeSpan
    from vtv.observability.trace import traced
    from vtv.pipeline.treatment import Census

    if not pending:
        return {}

    gate = asyncio.Semaphore(SOURCING_CONCURRENCY)
    directions: dict[str, Any] = {}

    async def direct(unit: VisualUnit) -> None:
        concept = concepts.get(unit.visual_unit_id)
        if concept is None:
            return
        async with gate:
            try:
                directions[unit.visual_unit_id] = await service.direct(
                    unit_id=unit.visual_unit_id,
                    narration=_narration_for(unit, script) or concept.source_text,
                    concept=concept,
                    style=_style_for(project),
                    span=unit.span or TimeSpan.of(0.0, 4.0),
                    organisation_id=project.organisation_id,
                    project_id=project.project_id,
                    may_generate=allowance.may_generate(unit.visual_unit_id),
                )
            except Exception:
                # A line nobody directed is sourced exactly as it was before
                # this function existed: the ladder decides for itself. Every
                # failure here has to leave the render as good as it was.
                return

    with traced(stage="direct-visuals"):
        await asyncio.gather(*(direct(unit) for unit in pending))

    if directions:
        census = Census.of([d.decision for d in directions.values()])
        context.events.emit(
            EventName.VISUAL_UNITS_PLANNED,
            project_id=project.project_id,
            data={
                "stage": "direction",
                "directed": len(directions),
                "by_treatment": census.by_treatment,
                "free_fraction": round(census.free_fraction, 3),
                "wants_photograph": sum(
                    1 for d in directions.values() if d.wants_photograph
                ),
                # Searches this pass removed: lines that would have been sent to
                # two commons providers and a judge, and now are not.
                "searches_avoided": sum(
                    1 for d in directions.values() if not d.wants_photograph
                ),
            },
        )
    return directions


async def _judge_commons(
    context: JobContext,
    project: Project,
    pending: list[VisualUnit],
    concepts: dict[str, Any],
    service: Any,
    directions: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Which lines the commons can actually illustrate.

    ## The problem

    The commons rung always succeeds. There is always *a* photograph, and until
    now the one that reached the video was the first search hit with a clear
    licence — so "letting intelligent agents figure out the steps" was
    illustrated with a stranger's cropped headshot, and "systems to accomplish
    it" with a gold military rank insignia. Both were the first hit. Neither was
    ever compared with anything, and both looked, to every stage downstream,
    exactly like success.

    `AssetResolver` now ranks its candidates and rejects the ones that do not
    answer their own query, which removes most of that for nothing. What it
    cannot remove is the candidate that answers the query perfectly and is still
    the wrong picture: "Skills for the Future" is an exact match for
    `skills for the future` and is a photograph of a man on a conference panel.
    Judging that needs a reader, not a rule.

    ## Why it happens here rather than inside the ladder

    Because here is the only place that has the whole script. A judge called
    from inside the ladder is a model call per shot — thirteen calls to answer
    thirteen instances of the same question, the exact cost shape
    `ConceptReader.read_many` and the budget planner both exist to remove. One
    call for the script costs about two cents and is paid once.

    The searches this does are not extra work: they go through the resolver's
    per-render cache, so the ladder reuses the same responses rather than asking
    the same two APIs the same questions again.

    ## What it returns

    One entry per judged unit: the candidate the agent chose, or `None` for "no
    photograph is right for this line". A unit that is **absent** was not judged
    at all — no providers, no candidates, or a failure — and the ladder proceeds
    exactly as it did before this function existed. Every failure here has to
    leave the render as good as it was.

    Returning the pick rather than a yes/no matters more than it sounds. The
    first version returned a boolean; the resolver then re-ranked the pool on
    word overlap and shipped its own preference, so the agent's reading of the
    narration was computed, paid for, and discarded.
    """
    import asyncio

    from vtv.observability.trace import traced
    from vtv.pipeline.selection import Brief, Candidate, CandidateSelector

    resolver = getattr(context.assembly.pipeline.composer, "asset_resolver", None)
    if resolver is None or not getattr(resolver, "providers", None):
        return {}

    # One cache for this render. Shared with the ladder below, so a query asked
    # here is not asked again when the shot is actually sourced.
    resolver.cache = {}

    work: list[tuple[Any, list[Any]]] = []
    #: Selector key -> the real candidate, so a verdict can be turned back into
    #: something downloadable.
    by_key: dict[str, Any] = {}
    gate = asyncio.Semaphore(SOURCING_CONCURRENCY)

    async def gather(unit: VisualUnit) -> None:
        concept = concepts.get(unit.visual_unit_id)
        if concept is None or not getattr(concept, "may_generate", True):
            return
        # The gate. A line the director has already settled as typography, a
        # chart or a diagram has no photograph in its ladder, so searching two
        # providers for one and paying a judge to read the results answers a
        # question nobody asked. An *undirected* line — direction failed, or a
        # caller that does not direct — is judged exactly as it was before,
        # because a missing decision must never be read as a refusal.
        direction = (directions or {}).get(unit.visual_unit_id)
        if direction is not None and not direction.wants_photograph:
            return
        requirements = service.commons_requirements(concept)
        if requirements is None:
            return
        async with gate:
            try:
                pool = await resolver.pool_for(
                    project_id=project.project_id,
                    scene_id=unit.visual_unit_id,
                    requirements=requirements,
                )
            except Exception:
                return
        if not pool:
            return
        judged: list[Any] = []
        for index, (query, candidate) in enumerate(pool):
            key = f"{unit.visual_unit_id}:{index}"
            by_key[key] = candidate
            judged.append(
                Candidate(
                    key=key,
                    title=candidate.title or "",
                    description=candidate.description or "",
                    query=query,
                    width=candidate.dimensions.width if candidate.dimensions else None,
                    height=candidate.dimensions.height if candidate.dimensions else None,
                )
            )
        work.append(
            (
                Brief(
                    unit_id=unit.visual_unit_id,
                    sentence=concept.source_text,
                    subject=getattr(concept, "subject", ""),
                ),
                judged,
            )
        )

    with traced(stage="select-visuals"):
        await asyncio.gather(*(gather(unit) for unit in pending))
        if not work:
            return {}
        selector = CandidateSelector(
            router=context.assembly.router
            if context.assembly.router.providers_for(GenerationKind.TEXT)
            else None,
            events=context.events,
        )
        verdicts = await selector.select(
            work,
            organisation_id=project.organisation_id,
            project_id=project.project_id,
        )

    # The pick itself, not a yes/no.
    #
    # The first version returned `verdict.chosen is not None` — a boolean — and
    # threw the agent's actual choice away. The resolver then ranked the pool
    # again on word overlap and shipped whatever *that* preferred, which is how
    # a shot the agent had judged still came out as a stereoscopic card of
    # elephants. An agent whose answer is reduced to yes/no is not an agent.
    picks: dict[str, Any] = {}
    for unit_id, verdict in verdicts.items():
        picks[unit_id] = by_key.get(verdict.chosen) if verdict.chosen else None
    refused = sorted(unit for unit, pick in picks.items() if pick is None)
    if refused:
        context.events.emit(
            EventName.VISUAL_UNITS_PLANNED,
            project_id=project.project_id,
            data={
                "stage": "selection",
                "judged": len(verdicts),
                "no_photograph": len(refused),
                # Why, for the first few. A log that says only "8 of 13" cannot
                # be used to decide whether the floor is in the right place.
                "reasons": [
                    verdicts[unit].reason[:120] for unit in refused[:4]
                ],
            },
        )
    return picks


def _price_of_one_image(context: JobContext, fidelity: Any) -> float:
    """What one generated image costs this project, at its chosen fidelity."""
    from vtv.contracts.generation import GenerationKind
    from vtv.pipeline.allowance import price_of_one_image

    return price_of_one_image(
        context.assembly.router.providers_for(GenerationKind.IMAGE), fidelity
    )


def _sourcing_service(context: JobContext) -> Any:
    """The ladder, wired from what this deployment actually has.

    Built per call rather than held on the assembly because it is cheap and
    because holding it would mean a worker that started before a provider was
    configured kept a service that could not reach it.
    """
    from vtv.pipeline.drawing import Draughtsman
    from vtv.pipeline.sourcing import VisualSourcingService
    from vtv.pipeline.visual_intent import ConceptReader

    assembly = context.assembly
    router = assembly.router
    # The reader only uses the router when a text provider is registered; with
    # none it falls back to rules, which still produce searchable queries.
    reader = ConceptReader(
        events=context.events,
        router=router if router.providers_for(GenerationKind.TEXT) else None,
    )
    return VisualSourcingService(
        composer=assembly.pipeline.composer,
        concepts=reader,
        events=context.events,
        # Rung one. Costs nothing and needs no provider, so it is always
        # supplied — a deployment with no credentials at all still draws.
        draughtsman=Draughtsman(events=context.events),
    )


async def source_one_visual(
    context: JobContext,
    project: Project,
    *,
    unit: VisualUnit,
    narration: str,
    intent: RegenerationIntent | None,
    service: Any | None = None,
    concept: Any | None = None,
    may_generate: bool = True,
    may_use_commons: bool = True,
    chosen_media: Any | None = None,
    direction: Any | None = None,
) -> Any:
    """Source one unit's visual, under this project's remaining ceiling.

    Shared by the regeneration path and the render path, so "what a visual may
    cost" has one answer rather than two that drift apart.
    """
    from vtv.contracts.base import Budget, TimeSpan

    settings = context.assembly.settings
    span = unit.span or TimeSpan.of(0.0, 4.0)
    ceiling = project.budget_usd
    if ceiling is None:
        ceiling = settings.max_project_cost_usd
    # Reused when the caller already built one — a whole-script render builds it
    # once, so the batch read below happens once rather than per visual.
    service = service if service is not None else _sourcing_service(context)
    return await service.source(
        concept=concept,
        may_generate=may_generate,
        # One place, so no caller has to remember. `source_one_visual` is the
        # single door into the ladder from the product lane — the render path
        # and the regeneration path both come through here — which makes it the
        # only place the project's chosen fidelity can be attached without
        # relying on two call sites staying in step.
        fidelity=project.visual_fidelity,
        may_use_commons=may_use_commons,
        chosen_media=chosen_media,
        direction=direction,
        narration=narration,
        intent=intent,
        style=_style_for(project),
        organisation_id=project.organisation_id,
        project_id=project.project_id,
        unit_id=unit.visual_unit_id,
        span=span,
        budget=Budget(
            max_cost_usd=_shot_ceiling(context, project, ceiling, may_generate),
            max_latency_seconds=180.0,
        ),
    )


def _shot_ceiling(
    context: JobContext, project: Project, budget_usd: float, may_generate: bool
) -> float:
    """The most one visual may cost.

    ## The quarter

    Per visual, not per project. Spending the whole budget on one shot would be
    a defensible reading of the word "budget" and a terrible product — one
    magnificent picture and twelve title cards. A single shot may have a
    quarter.

    ## Why the quarter is not the whole rule

    Because the quarter and the budget planner were two answers to one question,
    and they disagreed. `plan()` works out how many shots the budget can buy and
    permits exactly those; this ceiling then refused some of them anyway. At
    $0.25 a shot and a $0.25 budget the plan says "one picture, and here is
    which one" — and a flat quarter-of-budget ceiling of $0.0625 refused it at
    the router, so the user got no picture, no error, and a budget they never
    spent.

    So where the plan has already approved this shot, the ceiling is at least
    the price of the thing that was approved. The total is still bounded — by
    the plan, which is what a total should be bounded by — and the quarter goes
    back to being what it was meant to be: a guard against one shot eating the
    project, not a second budget quietly overriding the first.
    """
    quarter = round(max(0.02, budget_usd) * 0.25, 4)
    if not may_generate:
        return quarter
    return round(max(quarter, _price_of_one_image(context, project.visual_fidelity)), 4)


def _style_for(project: Project) -> Any:
    """The project's style, or the default one.

    A `StyleProfile` decides the aspect ratio a generated image is asked for, so
    getting it from the project rather than defaulting is what stops a 9:16
    project receiving 16:9 pictures.
    """
    from vtv.contracts.style import StyleProfile

    style = getattr(project, "style", None)
    return style if isinstance(style, StyleProfile) else StyleProfile()


HANDLERS: dict[str, Any] = {
    "revise_script": run_revise_script,
    "regenerate_visual": run_regenerate_visual,
    "render_scope": run_render_scope,
}


__all__ = [
    "HANDLERS",
    "RegeneratePayload",
    "RenderScopePayload",
    "RevisePayload",
    "run_regenerate_visual",
    "run_render_scope",
    "run_revise_script",
]
