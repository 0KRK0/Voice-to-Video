"""The product API: script, visual units, timeline, preview, scoped render.

Everything the future editor needs, and nothing it does not. Mounted by
`api/app.py` through `routes()`, which takes the guard and the state it already
built — so these endpoints share one authentication path, one rate limiter, one
audit log and one tenancy boundary with the rest of the surface. A second
security architecture for the new routes is how the first one develops a hole.

## Shape

Reads are synchronous and cheap: a script, a unit list, a timeline are
kilobytes, and a user pressing a key should not wait on a queue.

Writes divide in two:

* **Structural edits** — replace a line, move a clip, lock a visual — are
  synchronous. They touch no provider, cost nothing, and a user who moved a clip
  expects it to have moved before their hand leaves the mouse.
* **Anything that spends money or time** — propose a revision, regenerate a
  visual, render — is a job. The endpoint validates, enqueues, and returns a job
  id.

That line is drawn by cost and latency, not by tidiness. An editor where
dragging a clip round-trips through a queue feels broken however correct it is.

## Idempotency

Every expensive operation takes an `Idempotency-Key` header. Regenerating a
visual twice because a phone lost its connection must produce one generation and
one charge, and the durable queue already enforces that on a key — this layer
only has to supply a good one.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from vtv.contracts.errors import (
    ErrorCode,
    NotFound,
    PolicyViolation,
    ValidationFailed,
    VTVError,
)
from vtv.contracts.pacing import PacingMode, PacingPlan
from vtv.contracts.project import Project
from vtv.contracts.render_scope import RenderRegion, RenderScope
from vtv.contracts.scale import MAX_SCRIPT_CHARS as _MAX_SCRIPT_CHARS
from vtv.contracts.scale import ceiling_chars, describe
from vtv.contracts.script import (
    RevisionKind,
    RevisionProposal,
    RevisionStatus,
    Script,
)
from vtv.contracts.tenancy import AuditAction, Capability, Principal
from vtv.contracts.tracks import ClipSourceKind, EditTimeline, TrackKind
from vtv.contracts.visual_unit import RegenerationIntent, VisualUnit
from vtv.pipeline import naming
from vtv.pipeline.editing import TimelineEditor, TimelineOperation
from vtv.pipeline.pacing import PaceableUnit, PacingPlanner
from vtv.pipeline.scripting import ScriptService
from vtv.pipeline.units import TimelineBuilder, VisualUnitPlanner
from vtv.security.limits import limit_key
from vtv.security.paths import is_tenant_key

#: Longest script we accept in one request.
#:
#: Imported rather than declared, from `contracts/scale.py`, which is the one
#: place that says how long a project may be. It was 400 000 here and 20 000 in
#: the narration service, and the smaller one won silently — a thirty-minute
#: script was accepted by this endpoint's predecessor and then truncated, and
#: later refused outright with no usable message.
MAX_SCRIPT_CHARS = _MAX_SCRIPT_CHARS

#: The document kinds this API stores. Named here so the persistence helper and
#: the reader cannot disagree about a string.
SCRIPT_DOC = "script"
UNITS_DOC = "visual_units"
TIMELINE_DOC = "edit_timeline"
PACING_DOC = "pacing_plan"


def routes(
    *,
    guard: Callable[..., Principal],
    owned_project: Callable[[Request, Principal], Awaitable[Project | None]],
    repository: Any,
    assembly: Any,
    queue: Any,
    #: The device pool, so the render button can choose where it runs. The
    #: same instance the device routes use — one object, so "is a computer
    #: available" cannot be answered two ways by two modules.
    pool: Any,
    client_ip: Callable[[Request], str],
    json_body: Callable[[Request], Awaitable[dict[str, Any]]],
) -> list[Route]:
    """Build the product routes over the application's existing plumbing.

    Dependencies are passed in rather than imported so this module cannot
    acquire its own authentication, its own rate limiter or its own idea of who
    owns a project — the three things that must have exactly one implementation.
    """
    scripts = ScriptService(events=assembly.events)
    planner = VisualUnitPlanner(events=assembly.events)
    builder = TimelineBuilder(events=assembly.events)
    editor = TimelineEditor()
    pacer = PacingPlanner()

    # -- persistence ------------------------------------------------------

    async def store(project_id: str, kind: str, document: Any) -> None:
        """Persist a product document beside the pipeline's own.

        The same document table, so a project is one thing to load, one thing
        to delete and one thing to retain. A second store for the editor's
        documents would be a second thing to remember in `RetentionService`.
        """
        payload = (
            json.loads(document.model_dump_json())
            if hasattr(document, "model_dump_json")
            else document
        )
        await repository.put_document(
            project_id=project_id,
            kind=kind,
            document_id=getattr(document, f"{kind}_id", kind),
            payload=payload,
        )

    async def load(project_id: str, kind: str, model: Any) -> Any:
        payload = await repository.get_document(project_id=project_id, kind=kind)
        if payload is None:
            return None
        try:
            return model.model_validate(payload)
        except ValueError:
            # Written by an older schema. One missing artefact beats refusing
            # to open the project; the drift is visible in the schema test.
            return None

    async def load_units(project_id: str) -> list[VisualUnit]:
        payload = await repository.get_document(project_id=project_id, kind=UNITS_DOC)
        if not payload:
            return []
        items = payload.get("units", []) if isinstance(payload, dict) else []
        out: list[VisualUnit] = []
        for item in items:
            try:
                out.append(VisualUnit.model_validate(item))
            except ValueError:
                continue
        return out

    async def store_units(project_id: str, units: list[VisualUnit]) -> None:
        """Store the units, and make the timeline show what they now show.

        The two writes are one operation. Storing units without repointing the
        timeline is how "switch a version" became a change the inspector agreed
        with and the render ignored — the renderer flattens the timeline and
        never reads a unit, so a clip left pointing at the previous version is
        what the customer's file actually contains.

        Doing it here rather than at each call site is the point: three
        endpoints change what a visual shows, only one of them remembered, and
        a fourth written next year would have to remember too.
        """
        await repository.put_document(
            project_id=project_id,
            kind=UNITS_DOC,
            document_id=UNITS_DOC,
            payload={"units": [json.loads(u.model_dump_json()) for u in units]},
        )
        await _sync_timeline(project_id, units)

    async def _sync_timeline(project_id: str, units: list[VisualUnit]) -> None:
        from vtv.pipeline.units import retarget

        timeline = await load(project_id, TIMELINE_DOC, EditTimeline)
        if timeline is None:
            return
        repointed = retarget(timeline, units)
        # `retarget` returns the same object when nothing needed changing, so a
        # no-op does not bump the version and turn every other editor's next
        # save into a conflict nobody caused.
        if repointed is not timeline:
            await store(project_id, TIMELINE_DOC, repointed)

    async def _resolve_media(
        operations: list[TimelineOperation], project: Project
    ) -> list[TimelineOperation]:
        """Turn `media_asset_id` into a real object reference, server-side.

        The library is loaded once for the whole batch, not once per operation:
        a client placing twelve files should cost one read.

        Three things are settled here and cannot be settled anywhere else:

        * **Tenancy.** The asset is looked up in *this project's* library, so an
          id from another tenant is simply not found. There is no key to check
          because the caller never supplied one.
        * **Usability.** A file still uploading, or one refused by inspection,
          cannot reach the timeline. Otherwise a render would fail minutes later
          on a clip pointing at nothing.
        * **What kind of clip it is.** Derived from the asset, not from the
          request, so a caller cannot describe an audio file as programmatic and
          get it drawn.
        """
        wanted = {op.media_asset_id for op in operations if op.media_asset_id}
        if not wanted:
            return operations

        from vtv.api.media import MEDIA_DOC
        from vtv.contracts.media import MediaLibrary

        library = await load(project.project_id, MEDIA_DOC, MediaLibrary)
        resolved: list[TimelineOperation] = []
        for operation in operations:
            if not operation.media_asset_id:
                resolved.append(operation)
                continue
            asset = library.asset(operation.media_asset_id) if library else None
            if asset is None:
                raise NotFound(
                    f"no asset {operation.media_asset_id} in this project",
                    user_message="That file is not in this project's library.",
                )
            if not asset.is_usable or asset.object is None:
                raise ValidationFailed(
                    f"asset {asset.media_asset_id} is {asset.status.value}",
                    user_message="That file is not ready to use yet.",
                )
            resolved.append(
                operation.model_copy(
                    update={
                        "source_kind": ClipSourceKind.OBJECT,
                        "object": json.loads(asset.object.model_dump_json()),
                        "media_asset_id": None,
                        "text": operation.text or asset.filename,
                    }
                )
            )
        return resolved

    async def require_project(request: Request, principal: Principal) -> Project:
        project = await owned_project(request, principal)
        if project is None:
            # `NotFound` carries the NOT_FOUND *category*, which is what maps to
            # 404. A bare `VTVError` with a not-found *code* is categorised
            # INTERNAL and answers 500 — which would turn every cross-tenant
            # probe into a server error and every real 404 into a page nobody
            # can act on.
            raise NotFound(
                "no such project",
                user_message="We could not find that project.",
            )
        return project

    # -- script -----------------------------------------------------------

    async def create_script(request: Request) -> Response:
        """Start script mode: the user's words become the narration.

        Nothing is rewritten here. The text is split into addressable lines and
        stored exactly as supplied; `source_text` keeps it verbatim for the life
        of the project.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        body = await json_body(request)

        text = str(body.get("text", ""))
        # The deployment's ceiling, or the engineering one, whichever is
        # smaller. An operator sets `VTV_MAX_PROJECT_MINUTES` to give a free
        # tier fifteen minutes; nobody can set it to give themselves ten hours.
        limit = ceiling_chars(
            getattr(assembly.settings, "max_project_minutes", None)
        )
        if len(text) > limit:
            # `user_message` matters more than usual here. Without it this
            # refusal reached the studio as "Something in that request did not
            # look right" — which told a user pasting a thirty-minute narration
            # nothing at all: not that length was the problem, not what the
            # limit was, not how far over they were.
            raise ValidationFailed(
                f"a script may be at most {limit:,} characters; "
                f"this one is {len(text):,}",
                user_message=(
                    f"This script is {describe(len(text))} of narration. A "
                    f"single project can hold {describe(limit)} — about "
                    f"{limit:,} characters, and this one is {len(text):,}. "
                    "Split it into parts and render them separately."
                ),
            )
        script = scripts.from_text(
            text,
            organisation_id=project.organisation_id,
            project_id=project.project_id,
            language=str(body.get("language", "en"))[:16],
        )
        await store(project.project_id, SCRIPT_DOC, script)
        # A project names itself from its own opening sentence. `ensure` never
        # overwrites a name the user typed, so this is safe on every upload
        # rather than only on the first — see `pipeline/naming.py`.
        if naming.ensure(project, text):
            await repository.save_project(project)
        assembly.audit.write(
            action=AuditAction.PROJECT_UPDATED,
            principal=principal,
            target=f"project:{project.project_id}",
            detail={"change": "script_created", "blocks": str(len(script.blocks))},
            ip_address=client_ip(request),
        )
        return JSONResponse(_script_view(script), status_code=201)

    async def get_script(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        if script is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        return JSONResponse(_script_view(script))

    async def update_block(request: Request) -> Response:
        """Edit one line directly. Synchronous: it costs nothing."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        if script is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await json_body(request)
        script = scripts.replace_block_text(
            script,
            block_id=request.path_params["block_id"],
            text=str(body.get("text", "")),
        )
        await store(project.project_id, SCRIPT_DOC, script)

        units = await load_units(project.project_id)
        stale = [
            unit.visual_unit_id
            for unit in units
            if {
                block.block_id
                for block in script.blocks
                if block.timing_invalidated
            }.intersection(unit.script_block_ids)
        ]
        return JSONResponse(
            {
                **_script_view(script),
                # Never silently stale. The editor shows these as needing
                # attention, which is the whole reason the flag exists.
                "timing_invalidated_units": stale,
            }
        )

    async def propose_revision(request: Request) -> Response:
        """Ask for an enhancement. Returns a proposal; changes nothing."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request,
            Capability.PROJECT_UPDATE,
            resource=f"project:{project_id}",
            cost=5.0,
        )
        project = await require_project(request, principal)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        if script is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await json_body(request)
        try:
            kind = RevisionKind(str(body.get("kind", "")))
        except ValueError:
            raise ValidationFailed(
                "unknown revision kind; see the API reference for the list"
            ) from None

        payload = {
            "organisation_id": project.organisation_id,
            "project_id": project.project_id,
            "kind": kind.value,
            "block_ids": [str(b) for b in body.get("block_ids", [])][:200],
            "target_language": (
                str(body["target_language"])[:16]
                if body.get("target_language")
                else None
            ),
            "based_on_version": script.version,
        }
        handle = await queue.enqueue(
            kind="revise_script",
            payload=payload,
            # Every field that shapes the proposal, not just the kind. Proposing
            # does not advance `script.version`, so "translate to French" and
            # "translate to Spanish" against the same script would otherwise
            # produce one key — and the second caller would be handed the first
            # one's job and read back the wrong proposal.
            idempotency_key=_idempotency(
                request,
                project.organisation_id,
                _derived("revise", payload),
                operation="revise",
            ),
        )
        return JSONResponse({"job_id": handle.job_id}, status_code=202)

    async def get_revision(request: Request) -> Response:
        """Read a proposal without deciding on it.

        The endpoint the diff dialogue is built from. Without it the product's
        central promise — "we show you the change before we make it" — had no
        transport: a client could accept a proposal it was unable to display.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        proposal = await _load_proposal(
            repository, project.project_id, request.path_params["revision_id"]
        )
        if proposal is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        return JSONResponse(_proposal_view(proposal, script))

    async def decide_revision(request: Request) -> Response:
        """Accept or reject a proposal. The only path that changes the script."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        if script is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        revision_id = request.path_params["revision_id"]
        proposal = await _load_proposal(repository, project.project_id, revision_id)
        if proposal is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await json_body(request)
        accepted = bool(body.get("accept", False))

        from vtv.pipeline.revision import RevisionService

        service = RevisionService(router=None, events=assembly.events)
        if accepted:
            script = service.accept(script, proposal)
            await store(project.project_id, SCRIPT_DOC, script)
        else:
            service.reject(script, proposal)
        await _store_proposal(repository, project.project_id, proposal)

        assembly.audit.write(
            action=AuditAction.PROJECT_UPDATED,
            principal=principal,
            target=f"project:{project.project_id}",
            detail={
                "change": "revision_accepted" if accepted else "revision_rejected",
                "revision_id": revision_id,
                "kind": proposal.kind.value,
            },
            ip_address=client_ip(request),
        )
        return JSONResponse(
            {
                "revision_id": revision_id,
                "status": proposal.status.value,
                "script": _script_view(script),
            }
        )

    # -- visual units -----------------------------------------------------

    async def plan_units(request: Request) -> Response:
        """Group the script into visuals, preserving everything the user owns."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        if script is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await json_body(request)
        mode = _pacing_mode(body.get("pacing"))
        existing = await load_units(project.project_id)
        units = planner.plan(script, existing=existing, mode=mode)
        await store_units(project.project_id, units)

        plan = _pace(pacer, script, units, body.get("target_seconds"), mode)
        await store(project.project_id, PACING_DOC, plan)

        timeline = builder.build(
            script=script,
            units=units,
            pacing=plan,
            organisation_id=project.organisation_id,
            project_id=project.project_id,
            existing=await load(project.project_id, TIMELINE_DOC, EditTimeline),
        )
        await store(project.project_id, TIMELINE_DOC, timeline)

        return JSONResponse(
            {
                "units": [_unit_view(unit) for unit in units],
                "pacing": _pacing_view(plan),
                "timeline": _timeline_summary(timeline),
            }
        )

    async def get_units(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        units = await load_units(project.project_id)
        return JSONResponse({"units": [_unit_view(unit) for unit in units]})

    async def regenerate_unit(request: Request) -> Response:
        """Regenerate one visual. Costs money, so it is a job."""
        project_id = request.path_params["project_id"]
        unit_id = request.path_params["unit_id"]
        principal = guard(
            request,
            Capability.PROJECT_UPDATE,
            resource=f"project:{project_id}",
            cost=10.0,
        )
        project = await require_project(request, principal)
        units = await load_units(project.project_id)
        unit = next((u for u in units if u.visual_unit_id == unit_id), None)
        if unit is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        if unit.locked:
            # Refused here as well as in the service. Two checks, because this
            # one gives the user a 403 with a sentence they can act on before a
            # job is queued and a provider is paid.
            return JSONResponse(
                {
                    "error": {
                        "code": "permission_denied",
                        "message": (
                            f"Visual {unit.index + 1} is locked. Unlock it first "
                            "if you want it regenerated."
                        ),
                        "retryable": False,
                    }
                },
                status_code=403,
            )

        body = await json_body(request)
        try:
            intent = RegenerationIntent(
                str(body.get("intent", RegenerationIntent.SAME_IDEA.value))
            )
        except ValueError:
            raise ValidationFailed("unknown regeneration intent") from None

        handle = await queue.enqueue(
            kind="regenerate_visual",
            payload={
                "organisation_id": project.organisation_id,
                "project_id": project.project_id,
                "visual_unit_id": unit_id,
                "intent": intent.value,
            },
            idempotency_key=_idempotency(
                request,
                project.organisation_id,
                # Versioned *and* intent-bearing. The version number alone is
                # not enough: it only advances once the previous job has
                # written the unit back, so two different intents queued in
                # quick succession would collide and the second would silently
                # never run.
                _derived(
                    "regen",
                    project.project_id,
                    unit_id,
                    unit.next_version_number,
                    intent.value,
                ),
                operation="regenerate",
            ),
        )
        return JSONResponse(
            {"job_id": handle.job_id, "visual_unit_id": unit_id}, status_code=202
        )

    async def set_unit_state(request: Request) -> Response:
        """Approve, lock, unlock, or choose a version. All free and instant."""
        project_id = request.path_params["project_id"]
        unit_id = request.path_params["unit_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        units = await load_units(project.project_id)
        index = next(
            (i for i, u in enumerate(units) if u.visual_unit_id == unit_id), None
        )
        if index is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        from vtv.contracts.visual_unit import VisualUnitStatus
        from vtv.pipeline.regeneration import RegenerationService

        service = RegenerationService(
            producer=_UnavailableProducer(),
            validator=_PassthroughValidator(),
            events=assembly.events,
        )
        body = await json_body(request)
        unit = units[index]

        if "locked" in body:
            unit = service.set_locked(unit, locked=bool(body["locked"]))
        if body.get("approved"):
            if unit.locked:
                raise ValidationFailed("that visual is already locked")
            unit = unit.model_copy(deep=True)
            unit.status = VisualUnitStatus.APPROVED
        if body.get("version_id"):
            unit = service.select_version(
                unit, version_id=str(body["version_id"])
            ).unit

        units[index] = unit
        await store_units(project.project_id, units)
        assembly.audit.write(
            action=AuditAction.PROJECT_UPDATED,
            principal=principal,
            target=f"project:{project.project_id}",
            detail={
                "change": "visual_unit_state",
                "visual_unit_id": unit_id,
                "locked": str(unit.locked).lower(),
                "status": unit.status.value,
            },
            ip_address=client_ip(request),
        )
        return JSONResponse(_unit_view(unit))

    # -- timeline ---------------------------------------------------------

    async def get_timeline(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        timeline = await load(project.project_id, TIMELINE_DOC, EditTimeline)
        if timeline is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        units = await load_units(project.project_id)
        return JSONResponse(_timeline_view(timeline, units, script))

    async def edit_timeline(request: Request) -> Response:
        """Apply operations. Synchronous, atomic, and version-checked."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        timeline = await load(project.project_id, TIMELINE_DOC, EditTimeline)
        if timeline is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await json_body(request)
        raw = body.get("operations")
        if not isinstance(raw, list) or not raw:
            raise ValidationFailed("send at least one operation")
        if len(raw) > 200:
            raise ValidationFailed("apply at most 200 operations at a time")

        try:
            operations = [TimelineOperation.model_validate(item) for item in raw]
        except PydanticValidationError as error:
            # A malformed operation is the caller's mistake, not ours. Without
            # this it escapes as a raw pydantic error, misses the API's
            # `VTVError` handler and returns 500 for a bad request.
            raise ValidationFailed(
                f"that operation is not valid: {_first_pydantic_message(error)}"
            ) from None
        # `force` is never honoured from the wire. Overriding a user's lock is
        # an operator action with an audit record, not a flag a client sets.
        operations = [op.model_copy(update={"force": False}) for op in operations]
        operations = await _resolve_media(operations, project)
        for operation in operations:
            _check_operation_payloads(
                operation,
                project.organisation_id,
                getattr(assembly.storage, "bucket", ""),
            )

        expected = body.get("expected_version")
        result = editor.apply_all(
            timeline,
            operations,
            expected_version=int(expected) if expected is not None else None,
        )
        await store(project.project_id, TIMELINE_DOC, result.timeline)

        return JSONResponse(
            {
                "version": result.timeline.version,
                "changed_clip_ids": list(result.changed_clip_ids),
                "affected_unit_ids": list(result.affected_unit_ids),
                "warnings": list(result.warnings),
                "duration": result.timeline.duration,
            }
        )

    # -- pacing, preview and render ---------------------------------------

    async def set_pacing(request: Request) -> Response:
        """Choose a mode and a target. Returns the plan; applies it to the timeline."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        script = await load(project.project_id, SCRIPT_DOC, Script)
        units = await load_units(project.project_id)
        if script is None or not units:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await json_body(request)
        mode = _pacing_mode(body.get("pacing"))
        plan = _pace(pacer, script, units, body.get("target_seconds"), mode)
        await store(project.project_id, PACING_DOC, plan)

        timeline = builder.build(
            script=script,
            units=units,
            pacing=plan,
            organisation_id=project.organisation_id,
            project_id=project.project_id,
            existing=await load(project.project_id, TIMELINE_DOC, EditTimeline),
        )
        await store(project.project_id, TIMELINE_DOC, timeline)
        return JSONResponse(
            {"pacing": _pacing_view(plan), "timeline": _timeline_summary(timeline)}
        )

    async def preview(request: Request) -> Response:
        """What is on screen at a moment, and what produced it.

        A metadata endpoint, not a rendering one. The editor scrubs constantly
        and rendering a frame per scrub would be absurd; what it actually needs
        is "which clip, which unit, which line, which source", which is a
        database read.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        timeline = await load(project.project_id, TIMELINE_DOC, EditTimeline)
        if timeline is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        units = {u.visual_unit_id: u for u in await load_units(project.project_id)}
        script = await load(project.project_id, SCRIPT_DOC, Script)

        raw = request.query_params.get("at")
        try:
            at = max(0.0, float(raw)) if raw is not None else 0.0
        except ValueError:
            raise ValidationFailed("`at` must be a number of seconds") from None

        clip = timeline.clip_at(at, kind=TrackKind.VISUAL)
        unit = units.get(clip.visual_unit_id or "") if clip else None
        lines = (
            [
                {"block_id": b, "text": (script.block(b).text if script and script.block(b) else "")}
                for b in (unit.script_block_ids if unit else [])
            ]
            if unit
            else []
        )
        return JSONResponse(
            {
                "at": round(at, 3),
                "duration": timeline.duration,
                "clip": _clip_view(clip) if clip else None,
                "visual_unit": _unit_view(unit) if unit else None,
                "script_lines": lines,
            }
        )

    async def render_scoped(request: Request) -> Response:
        """Render the project, or only the part that changed."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request,
            Capability.PROJECT_UPDATE,
            resource=f"project:{project_id}",
            cost=20.0,
        )
        project = await require_project(request, principal)
        timeline = await load(project.project_id, TIMELINE_DOC, EditTimeline)
        if timeline is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await json_body(request)
        region = _region_from(body, timeline)

        # Where this render runs. Resolved here, at the moment the button was
        # pressed, and carried in the payload — not decided later by whichever
        # process happens to pick the job up.
        #
        # The decision is made *now* rather than in the handler because "is one
        # of my computers available" is a question about this instant. A job
        # that resolved it an hour later, after sitting in a queue, would answer
        # a question nobody asked and route somewhere the person never saw.
        from vtv.dispatch import Target

        wanted = str(body.get("execution") or Target.AUTO.value).strip().lower()
        try:
            plan = pool.plan(principal.organisation_id or "", Target(wanted))
        except ValueError:
            return JSONResponse(
                {
                    "error": {
                        "code": "schema_invalid",
                        "message": f"unknown execution {wanted!r}",
                    },
                    "expected": [target.value for target in Target],
                },
                status_code=400,
            )

        assembly.limiter.require(
            limit_key("render", principal.organisation_id, principal.subject),
            cost=1.0 if region.is_full else 0.2,
        )
        # So a computer that has settled into a slow poll is already being told
        # to hurry by the time the job exists.
        assembly.directory.nudge(principal.organisation_id or "")
        handle = await queue.enqueue(
            kind="render_scope",
            payload={
                "organisation_id": project.organisation_id,
                "project_id": project.project_id,
                "region": json.loads(region.model_dump_json()),
                "execution": plan.target.value,
            },
            idempotency_key=_idempotency(
                request,
                project.organisation_id,
                # The whole region, not just its start. `expand` clamps the
                # start to zero, so 0→10 and 0→20 at the same timeline version
                # shared a key and the second render silently never ran.
                _derived(
                    "render",
                    project.project_id,
                    timeline.version,
                    json.loads(region.model_dump_json()),
                ),
                operation="render",
            ),
        )
        return JSONResponse(
            {
                "job_id": handle.job_id,
                "scope": region.scope.value,
                "start": region.start,
                "end": region.end,
                # How much of the project this region covers. Named for what
                # it is, not for a saving it does not deliver.
                #
                # This field was called the same thing and commented "what the
                # user saves by not re-rendering everything" — while
                # `run_render_scope` states plainly that the encode is
                # full-timeline and the segment splice is not built. Two
                # documents in one repository, one honest, and the dishonest one
                # was the customer-facing API response.
                "fraction_of_project": (
                    1.0
                    if region.is_full or not timeline.duration
                    else round((region.duration or 0.0) / timeline.duration, 3)
                ),
                # What the encode will actually do. The same word the job emits
                # in its event, so an operator reading a log and a client
                # reading a response cannot reach different conclusions.
                "encode": "full_timeline",
                "saves_time": False,
                # What was actually chosen, and the sentence explaining it.
                # Never the word the client sent: "auto" echoed back tells an
                # editor nothing it can show a person.
                "execution": plan.target.value,
                "execution_reason": plan.reason,
            },
            status_code=202,
        )

    return [
        Route("/v1/projects/{project_id}/script", create_script, methods=["POST"]),
        Route("/v1/projects/{project_id}/script", get_script),
        Route(
            "/v1/projects/{project_id}/script/blocks/{block_id}",
            update_block,
            methods=["PATCH"],
        ),
        Route(
            "/v1/projects/{project_id}/script/revisions",
            propose_revision,
            methods=["POST"],
        ),
        Route(
            "/v1/projects/{project_id}/script/revisions/{revision_id}",
            get_revision,
        ),
        Route(
            "/v1/projects/{project_id}/script/revisions/{revision_id}",
            decide_revision,
            methods=["POST"],
        ),
        Route("/v1/projects/{project_id}/visual-units", get_units),
        Route(
            "/v1/projects/{project_id}/visual-units", plan_units, methods=["POST"]
        ),
        Route(
            "/v1/projects/{project_id}/visual-units/{unit_id}",
            set_unit_state,
            methods=["PATCH"],
        ),
        Route(
            "/v1/projects/{project_id}/visual-units/{unit_id}/regenerate",
            regenerate_unit,
            methods=["POST"],
        ),
        Route("/v1/projects/{project_id}/timeline", get_timeline),
        Route(
            "/v1/projects/{project_id}/timeline", edit_timeline, methods=["PATCH"]
        ),
        Route("/v1/projects/{project_id}/pacing", set_pacing, methods=["POST"]),
        Route("/v1/projects/{project_id}/preview", preview),
        Route(
            "/v1/projects/{project_id}/render", render_scoped, methods=["POST"]
        ),
    ]


# ---------------------------------------------------------------------------
# Views. One place that decides what leaves the building.
# ---------------------------------------------------------------------------

def _script_view(script: Script) -> dict[str, Any]:
    return {
        "script_id": script.script_id,
        "version": script.version,
        "origin": script.origin.value,
        "language": script.language,
        "estimated_seconds": script.estimated_duration_seconds,
        "measured_seconds": script.measured_duration_seconds,
        "word_count": script.word_count,
        "has_stale_timing": script.has_stale_timing,
        # The flag that forces a product decision instead of shipping audio
        # that says something other than the captions.
        "diverged_from_recording": script.diverged_from_recording,
        "blocks": [
            {
                "block_id": block.block_id,
                "order": block.order,
                "text": block.text,
                "status": block.status.value,
                "estimated_seconds": block.estimated_seconds,
                "start": block.measured_start,
                "end": block.measured_end,
                "visual_unit_id": block.visual_unit_id,
                "timing_invalidated": block.timing_invalidated,
            }
            for block in script.blocks
        ],
    }


def _proposal_view(
    proposal: RevisionProposal, script: Script | None
) -> dict[str, Any]:
    """Everything the diff dialogue needs, and nothing about how it was made.

    `provider` and `model` are deliberately absent: which vendor produced a
    suggestion is internal, and naming it in a response is how a customer ends
    up depending on it.
    """
    stale = script is not None and script.version != proposal.based_on_version
    return {
        "revision_id": proposal.revision_id,
        "kind": proposal.kind.value,
        "target_language": proposal.target_language,
        # `superseded` is computed against the *current* script rather than read
        # from the stored status, because the script can move after the proposal
        # was written and nothing goes back to re-stamp it.
        "status": (
            RevisionStatus.SUPERSEDED.value
            if stale and proposal.status is RevisionStatus.PROPOSED
            else proposal.status.value
        ),
        "based_on_version": proposal.based_on_version,
        "script_version": script.version if script else None,
        "estimated_duration_before": proposal.estimated_duration_before,
        "estimated_duration_after": proposal.estimated_duration_after,
        "duration_delta_seconds": proposal.duration_delta_seconds,
        "changes": [
            {
                "block_id": change.block_id,
                "original": change.original,
                "proposed": change.proposed,
                "reason": change.reason,
                "is_change": change.is_change,
            }
            for change in proposal.changes
        ],
    }


def _unit_view(unit: VisualUnit) -> dict[str, Any]:
    selected = unit.selected
    return {
        "visual_unit_id": unit.visual_unit_id,
        "index": unit.index,
        "status": unit.status.value,
        "locked": unit.locked,
        "detail": unit.detail,
        "script_block_ids": list(unit.script_block_ids),
        "start": unit.span.start if unit.span else None,
        "end": unit.span.end if unit.span else None,
        "deliverable": unit.is_deliverable,
        "selected_version_id": selected.version_id if selected else None,
        "versions": [
            {
                "version_id": version.version_id,
                "version": version.version,
                "strategy": version.strategy.value,
                "intent": version.intent.value if version.intent else None,
                "grounding": version.grounding.value,
                "consistency": version.consistency.value,
                "usable": version.is_usable,
                "rationale": version.rationale,
                # Where the picture came from, in the same four words the media
                # library uses. Sent rather than inferred: `existing_asset`
                # covers both a user's upload and a library asset, and those
                # carry opposite answers to the licence question.
                "origin": version.origin,
                "user_owned": version.user_owned,
                # The credit line the licence obliges the finished video to
                # carry. Shown in the inspector so a user can see *before*
                # publishing that this shot comes with an obligation, rather
                # than discovering it from the video or not at all.
                "attribution": version.attribution,
                # Per-version, because "regenerating cost me four times" is a
                # question users ask and are entitled to an answer to.
                "cost_usd": version.cost_usd,
            }
            for version in unit.versions
        ],
    }


def _clip_view(clip: Any) -> dict[str, Any]:
    return {
        "clip_id": clip.clip_id,
        "track_id": clip.track_id,
        "visual_unit_id": clip.visual_unit_id,
        "start": clip.start,
        "end": clip.end,
        "duration": clip.duration,
        "source_kind": clip.source_kind.value,
        "locked": clip.locked,
        "label": clip.label,
        "transition_in": clip.transition_in.value,
        "transition_out": clip.transition_out.value,
        # Linear, 0 to 1, as stored. The editor converts to decibels for
        # display; the conversion belongs there and not here, because this is
        # the number `set_gain` writes and a view that reported decibels while
        # the operation took a ratio is a round trip nobody can debug.
        "gain": clip.gain,
    }


def _timeline_view(
    timeline: EditTimeline, units: list[VisualUnit], script: Script | None
) -> dict[str, Any]:
    by_unit = {unit.visual_unit_id: unit for unit in units}
    return {
        "edit_timeline_id": timeline.edit_timeline_id,
        "version": timeline.version,
        "duration": timeline.duration,
        "target_seconds": timeline.target_seconds,
        "tracks": [
            {
                "track_id": track.track_id,
                "kind": track.kind.value,
                "name": track.name,
                "muted": track.muted,
                "locked": track.locked,
                "exclusive": track.is_exclusive,
                "derived": track.is_derived,
                "gaps": [{"start": a, "end": b} for a, b in track.gaps()],
                "clips": [_clip_view(clip) for clip in track.clips],
            }
            for track in timeline.tracks
        ],
        # The link table the editor needs to jump between panels. Materialised
        # here rather than derived client-side so that "click the line, seek the
        # video" cannot be implemented two ways that disagree.
        "links": [
            {
                "visual_unit_id": clip.visual_unit_id,
                "clip_id": clip.clip_id,
                "start": clip.start,
                "end": clip.end,
                "script_block_ids": list(
                    by_unit[clip.visual_unit_id].script_block_ids
                )
                if clip.visual_unit_id in by_unit
                else [],
            }
            for track in timeline.tracks
            if track.kind is TrackKind.VISUAL
            for clip in track.clips
        ],
        "script_version": script.version if script else None,
    }


def _timeline_summary(timeline: EditTimeline) -> dict[str, Any]:
    return {
        "edit_timeline_id": timeline.edit_timeline_id,
        "version": timeline.version,
        "duration": timeline.duration,
        "clips": sum(len(track.clips) for track in timeline.tracks),
        "tracks": [track.kind.value for track in timeline.tracks],
    }


def _pacing_view(plan: PacingPlan) -> dict[str, Any]:
    return {
        "mode": plan.profile.mode.value,
        "verdict": plan.verdict.value,
        "narration_seconds": plan.narration_seconds,
        "target_seconds": plan.target_seconds,
        "planned_seconds": plan.planned_seconds,
        "shortfall_seconds": plan.shortfall_seconds,
        "overrun_seconds": plan.overrun_seconds,
        "needs_user_decision": plan.needs_user_decision,
        "message": plan.message,
        "fill": [
            {
                "strategy": item.strategy.value,
                "seconds": item.seconds,
                "visual_unit_id": item.visual_unit_id,
            }
            for item in plan.allocations
        ],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pacing_mode(raw: Any) -> PacingMode:
    if raw is None:
        return PacingMode.NATURAL
    try:
        return PacingMode(str(raw))
    except ValueError:
        raise ValidationFailed(
            f"unknown pacing mode; choose one of "
            f"{', '.join(m.value for m in PacingMode if m is not PacingMode.CUSTOM)}"
        ) from None


def _pace(
    pacer: PacingPlanner,
    script: Script,
    units: list[VisualUnit],
    target: Any,
    mode: PacingMode,
) -> PacingPlan:
    blocks = {block.block_id: block for block in script.blocks}
    paceable = [
        PaceableUnit(
            visual_unit_id=unit.visual_unit_id,
            narration_seconds=round(
                sum(
                    blocks[b].duration_seconds
                    for b in unit.script_block_ids
                    if b in blocks
                ),
                3,
            ),
            user_owned=unit.is_user_owned,
        )
        for unit in units
    ]
    target_seconds: float | None = None
    if target is not None:
        try:
            target_seconds = max(0.0, float(target))
        except (TypeError, ValueError):
            raise ValidationFailed("`target_seconds` must be a number") from None
    return pacer.plan(
        units=paceable,
        narration_seconds=script.measured_duration_seconds
        or script.estimated_duration_seconds,
        target_seconds=target_seconds,
        mode=mode,
    )


def _region_from(body: dict[str, Any], timeline: EditTimeline) -> RenderRegion:
    """Build a render region from a request, refusing anything incoherent."""
    raw = str(body.get("scope", RenderScope.FULL_PROJECT.value))
    try:
        scope = RenderScope(raw)
    except ValueError:
        raise ValidationFailed("unknown render scope") from None

    if scope is RenderScope.FULL_PROJECT:
        return RenderRegion(scope=scope)

    if scope is RenderScope.CLIP:
        clip_id = str(body.get("clip_id", ""))
        found = timeline.clip(clip_id)
        if found is None:
            raise ValidationFailed("no such clip")
        _track, clip = found
        return RenderRegion(
            scope=scope,
            start=clip.start,
            end=clip.end,
            clip_ids=[clip.clip_id],
            visual_unit_ids=[clip.visual_unit_id] if clip.visual_unit_id else [],
        ).expand(limit=timeline.duration)

    if scope is RenderScope.SCENE:
        unit_id = str(body.get("visual_unit_id", ""))
        clips = timeline.clips_for_unit(unit_id)
        if not clips:
            raise ValidationFailed("that visual has nothing on the timeline")
        return RenderRegion(
            scope=scope,
            start=min(c.start for c in clips),
            end=max(c.end for c in clips),
            clip_ids=[c.clip_id for c in clips],
            visual_unit_ids=[unit_id],
        ).expand(limit=timeline.duration)

    try:
        start = float(body.get("start", 0.0))
        end = float(body["end"])
    except (KeyError, TypeError, ValueError):
        raise ValidationFailed("a range render needs a start and an end") from None
    return RenderRegion(
        scope=RenderScope.RANGE, start=max(0.0, start), end=end
    ).expand(limit=timeline.duration)


def _idempotency(
    request: Request, organisation_id: str, fallback: str, *, operation: str
) -> str:
    """The caller's key if they sent one, otherwise a derived one.

    **Always namespaced by tenant.** The queue's uniqueness index is global, so
    an un-namespaced client key is a cross-tenant collision: two organisations
    both sending `Idempotency-Key: retry-1` would deduplicate against each
    other, and the second one would be handed the *first one's* job id. That is
    both a correctness bug and a leak of another tenant's work.

    A derived key is weaker than a supplied one — two genuinely different
    requests collide if they are identical in every field the derivation
    considers — so every derivation above hashes the whole request-shaping
    payload rather than a subset. A client that wants precision sends the
    header.
    """
    supplied = request.headers.get("idempotency-key", "").strip()
    if supplied and len(supplied) <= 128 and supplied.isprintable():
        # Namespaced by *operation* as well as tenant. The queue's uniqueness
        # index is on the key alone and does not consider the job kind, so a
        # client that reuses one key across two different calls — which is
        # exactly what a naive "one key per user action" client does — would
        # otherwise have its second call silently deduplicated against the
        # first and be handed a job of the wrong kind to poll.
        return f"client:{organisation_id}:{operation}:{supplied}"
    return f"{organisation_id}:{fallback}"


def _first_pydantic_message(error: PydanticValidationError) -> str:
    """One readable sentence from a pydantic failure, naming no internals."""
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg", "")).strip()
        if message:
            return f"{location}: {message}" if location else message
    return "the request did not match the expected shape"


def _check_operation_payloads(
    operation: TimelineOperation, organisation_id: str, bucket: str
) -> None:
    """Refuse a storage reference belonging to somebody else, or a bad spec.

    A timeline operation may carry an `object` — that is how "use this asset"
    works — and a `spec`, which is an animation the renderer will draw. Both are
    client-supplied and both are stored and acted on much later, so both are
    validated here rather than at the point of use.

    **The object.** Storage keys are predictable within a tenant, and the
    storage provider enforces the tenant namespace on *write* but not on read.
    Without this a principal who learned another organisation's key could splice
    that organisation's asset into their own render, and the only trace would be
    in the rendered video. The bucket is checked too: with per-tenant buckets a
    key check alone leaves the same hole one field over.

    **The spec.** Validated now so a malformed animation is a 400 the client can
    fix, rather than a crash inside a render job an hour later — the operation
    model keeps it as a loose `dict` so this layer can decide, and this is that
    decision.
    """
    if operation.object is not None:
        payload = operation.object
        if not isinstance(payload, dict) or not payload:
            raise ValidationFailed("an object reference cannot be empty")
        if str(payload.get("bucket", "")) not in {"", bucket}:
            raise PolicyViolation(
                "that object is in a bucket this organisation does not use",
                code=ErrorCode.PERMISSION_DENIED,
                user_message="That file is not one of yours.",
            )
        if not is_tenant_key(str(payload.get("key", "")), organisation_id):
            raise PolicyViolation(
                "that object does not belong to this organisation",
                code=ErrorCode.PERMISSION_DENIED,
                user_message="That file is not one of yours.",
            )

    if operation.spec is not None:
        from pydantic import TypeAdapter

        from vtv.contracts.visual_language import AnimationSpec

        try:
            TypeAdapter(AnimationSpec).validate_python(operation.spec)
        except PydanticValidationError as error:
            raise ValidationFailed(
                f"that animation is not valid: {_first_pydantic_message(error)}"
            ) from None


def _derived(prefix: str, *parts: Any) -> str:
    """A derived idempotency key covering every field that shapes the request.

    Hashed rather than concatenated so that adding a field to an operation
    cannot silently produce a key that collides with an older one, and so that
    a long list of block ids does not produce an unbounded key.
    """
    material = json.dumps(parts, sort_keys=True, default=str)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


async def _load_proposal(
    repository: Any, project_id: str, revision_id: str
) -> RevisionProposal | None:
    payload = await repository.get_document(
        project_id=project_id, kind=f"revision:{revision_id}"
    )
    if payload is None:
        return None
    try:
        return RevisionProposal.model_validate(payload)
    except ValueError:
        return None


async def _store_proposal(
    repository: Any, project_id: str, proposal: RevisionProposal
) -> None:
    await repository.put_document(
        project_id=project_id,
        kind=f"revision:{proposal.revision_id}",
        document_id=proposal.revision_id,
        payload=json.loads(proposal.model_dump_json()),
    )


class _UnavailableProducer:
    """Stands in where a producer is structurally unreachable.

    `set_unit_state` uses `RegenerationService` for its locking and
    version-selection rules and never regenerates. Passing a producer that
    raises makes that structural rather than a comment — if this code ever grew
    a path that regenerated, it would fail loudly here instead of quietly
    spending money on a request that was meant to be free.
    """

    async def produce(self, **_: Any) -> Any:
        raise VTVError(
            "this endpoint does not generate; use the regenerate endpoint",
            code=ErrorCode.INTERNAL_ERROR,
        )


class _PassthroughValidator:
    """No verdict, because nothing new was produced to have a verdict about."""

    def validate(self, **_: Any) -> tuple[Any, Any, str]:
        from vtv.contracts.visual_unit import ConsistencyStatus, GroundingStatus

        return GroundingStatus.NOT_APPLICABLE, ConsistencyStatus.NOT_APPLICABLE, ""


__all__ = [
    "MAX_SCRIPT_CHARS",
    "PACING_DOC",
    "SCRIPT_DOC",
    "TIMELINE_DOC",
    "UNITS_DOC",
    "routes",
]
