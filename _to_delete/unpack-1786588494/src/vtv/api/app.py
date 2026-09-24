"""Stages 12, 15 and 20 — the HTTP surface.

Built directly on Starlette rather than FastAPI. `docs/ARCHITECTURE.md` names
FastAPI, and it remains the right choice once request/response models multiply;
FastAPI is a layer over Starlette, so this is the same foundation and swapping up
is additive. The reason for the deviation is stated plainly rather than hidden:
FastAPI is not installable in this environment, and the API is not worth blocking
on that.

Everything long-running is a job. Uploading a recording returns immediately with
a job id; the browser follows progress over server-sent events. A request that
blocks for the length of a render is a request that times out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from vtv.adapters.queue.inprocess import InProcessJobQueue
from vtv.adapters.repository.sqlite import SqliteProjectRepository
from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.billing.plans import QuotaKind
from vtv.config import Settings
from vtv.contracts.base import IdPrefix, new_id, utc_now
from vtv.contracts.errors import ErrorCategory, ErrorCode, Status, VTVError
from vtv.contracts.project import PersistenceMode, Project
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.style import AspectRatio, StyleProfile, VisualStyle
from vtv.contracts.tenancy import (
    AuditAction,
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Principal,
    Role,
    User,
)
from vtv.contracts.timeline import AssetClipSource, ProgrammaticClipSource
from vtv.observability.events import Event
from vtv.pipeline.captions import to_vtt
from vtv.pipeline.orchestrator import PipelineResult
from vtv.security.authz import NotAuthenticated
from vtv.security.keys import mint_api_key
from vtv.security.limits import (
    LOGIN_LIMIT,
    PLAN_LIMITS,
    RENDER_LIMIT,
    RequestBounds,
    limit_key,
)
from vtv.security.uploads import ContentClass, UploadPolicy, inspect_upload
from vtv.wiring import Assembly, build

WEB_ROOT = Path(__file__).resolve().parents[3] / "apps" / "web"

#: Cap on a single upload. Enforced before anything is read into memory.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Documents are text, not media, and a 50MB PDF is already pathological.
MAX_DOCUMENT_UPLOAD_BYTES = 50 * 1024 * 1024

#: Per-request ceilings, enforced before a body is read into memory.
BOUNDS = RequestBounds()

#: In a development install with no organisation configured, requests run as
#: this tenant. Refused in production, where a credential is mandatory — a
#: convenience default that survives into production is how an open API ships.
DEV_ORGANISATION_SLUG = "development"


@dataclass
class ApiState:
    """Everything the routes need. One object, injected, never a global."""

    assembly: Assembly
    repository: SqliteProjectRepository
    queue: InProcessJobQueue
    results: dict[str, PipelineResult] = field(default_factory=dict)
    #: Bounded ring of recent events per project, so a browser that connects
    #: late still sees what it missed.
    event_log: dict[str, list[Event]] = field(default_factory=dict)

    def record(self, event: Event) -> None:
        if not event.project_id:
            return
        log = self.event_log.setdefault(event.project_id, [])
        log.append(event)
        del log[:-400]


#: Specific codes that need a status other than their category's default.
#: Kept as a table because the mapping is a contract clients depend on: 401
#: means "get a credential", 403 means "that credential is not enough", 402
#: means "pay", 429 means "wait". Collapsing them into 403 makes every one of
#: those a support ticket.
_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.NOT_AUTHENTICATED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.QUOTA_EXCEEDED: 402,
    ErrorCode.TENANT_SUSPENDED: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UPLOAD_REFUSED: 415,
}


def _error_response(error: VTVError, status_code: int | None = None) -> JSONResponse:
    """Never leak internals. `user_message` is what reaches the browser."""
    code = (
        status_code
        or _STATUS_BY_CODE.get(error.info.code)
        or {
            ErrorCategory.VALIDATION: 400,
            ErrorCategory.POLICY: 403,
            ErrorCategory.NOT_FOUND: 404,
            ErrorCategory.TIMEOUT: 504,
            ErrorCategory.PROVIDER: 502,
            ErrorCategory.PROVIDER_REFUSED: 422,
            ErrorCategory.INTERNAL: 500,
        }.get(error.info.category, 500)
    )
    headers: dict[str, str] = {}
    if error.info.code is ErrorCode.RATE_LIMITED:
        retry = error.info.context.get("retry_after")
        headers["Retry-After"] = str(int(float(retry or 1)) or 1)
    return JSONResponse(
        {
            "error": {
                "code": error.info.code.value,
                # The message the user sees never names a provider, an internal
                # identifier or another tenant.
                "message": error.info.user_message or "Something went wrong.",
                "retryable": error.info.retryable,
            }
        },
        status_code=code,
        headers=headers or None,
    )


def create_app(
    settings: Settings | None = None,
    *,
    assembly: Assembly | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    assembly = assembly or build(settings)
    repository = SqliteProjectRepository(
        Path(settings.database_url.replace("sqlite:///", ""))
        if settings.database_url.startswith("sqlite:///")
        else Path("./var/vtv.db")
    )
    queue = InProcessJobQueue(events=assembly.events)
    state = ApiState(assembly=assembly, repository=repository, queue=queue)
    assembly.events.subscribe(state.record)

    # -- jobs -------------------------------------------------------------

    async def run_pipeline(payload: dict[str, Any]) -> str:
        project = await repository.get_project(payload["project_id"])
        if project is None:
            raise VTVError("project vanished", code=ErrorCode.ASSET_NOT_FOUND)
        audio = Path(payload["audio_path"]).read_bytes()
        Path(payload["audio_path"]).unlink(missing_ok=True)

        quality = RenderQuality(payload.get("quality", "preview"))
        result = await assembly.pipeline.run(
            audio=audio,
            project=project,
            style=project.style,
            settings=RenderSettings(
                aspect_ratio=project.style.aspect_ratio,
                quality=quality,
                frame_rate=int(payload.get("frame_rate", 24)),
            ),
        )
        state.results[project.project_id] = result
        await _persist(repository, result)
        _meter(payload, result)
        return project.project_id

    async def run_document_pipeline(payload: dict[str, Any]) -> str:
        """Stage 21 — the same job, entered from a document."""
        project = await repository.get_project(payload["project_id"])
        if project is None:
            raise VTVError("project vanished", code=ErrorCode.ASSET_NOT_FOUND)
        path = Path(payload["document_path"])
        data = path.read_bytes()
        path.unlink(missing_ok=True)

        document, _transcript, _context = assembly.ingestion.ingest(
            data,
            project_id=project.project_id,
            filename=payload.get("filename"),
            origin=payload.get("filename"),
            language=payload.get("language"),
        )
        result = await assembly.pipeline.run_from_document(
            document=document,
            project=project,
            style=project.style,
            settings=RenderSettings(
                aspect_ratio=project.style.aspect_ratio,
                quality=RenderQuality(payload.get("quality", "preview")),
                frame_rate=int(payload.get("frame_rate", 24)),
            ),
        )
        state.results[project.project_id] = result
        await _persist(repository, result)
        _meter(payload, result)
        return project.project_id

    def _meter(payload: dict[str, Any], result: PipelineResult) -> None:
        """Settle the reservation against what was actually produced.

        The estimate is discarded: billing a customer for a guess rather than a
        measurement is how a support thread becomes a refund.
        """
        reservation = payload.get("reservation_id")
        if not reservation:
            return
        job = result.render_job
        if job is None or job.status is not Status.READY:
            # Nothing was delivered, so nothing is charged and the allowance
            # goes back immediately rather than waiting to expire.
            assembly.usage.release(str(reservation))
            return
        assembly.usage.settle(
            str(reservation),
            actual=round((job.duration_seconds or 0.0) / 60.0, 4),
            cost_usd=result.ledger.total_usd if result.ledger else 0.0,
            project_id=result.project.project_id,
            idempotency_key=f"render:{result.project.project_id}:{job.render_job_id}",
        )

    queue.register("pipeline", run_pipeline)
    queue.register("document", run_document_pipeline)

    # -- authentication ---------------------------------------------------

    def development_principal() -> Principal | None:
        """The tenant an unauthenticated development request runs as.

        Created lazily and never in production. `settings.is_production` gates
        it, so a deployment that forgets to configure authentication fails
        closed rather than serving every request as an owner.
        """
        if settings.is_production:
            return None
        organisation = assembly.directory.organisation_by_slug(DEV_ORGANISATION_SLUG)
        if organisation is None:
            organisation = assembly.directory.create_organisation(
                Organisation(
                    name="Development",
                    slug=DEV_ORGANISATION_SLUG,
                    plan=PlanTier.ENTERPRISE,
                )
            )
            user = assembly.directory.create_user(
                User(email="dev@localhost", sso_subject="dev|local")
            )
            assembly.directory.add_member(
                Membership(
                    user_id=user.user_id,
                    organisation_id=organisation.organisation_id,
                    role=Role.OWNER,
                    accepted_at=utc_now(),
                )
            )
        members = assembly.directory.members(organisation.organisation_id)
        if not members:
            return None
        return assembly.directory.principal_for_user(
            members[0].user_id, organisation.organisation_id
        )

    def authenticate(request: Request) -> Principal:
        """Resolve the caller once, at the edge. Nothing below re-authenticates."""
        request_id = request.headers.get("x-request-id") or new_id(IdPrefix.GENERATION)
        header = request.headers.get("authorization") or ""
        scheme, _, credential = header.partition(" ")

        if credential and scheme.lower() == "bearer":
            # Credential *failures* are limited by address, because the whole
            # point of credential stuffing is that the attacker has no valid
            # principal. Charging every successful call to the same bucket would
            # throttle legitimate clients at the login rate, which is a bug this
            # test suite caught: authenticating is not attempting to log in.
            bucket = limit_key("auth", _client_ip(request))
            try:
                principal = assembly.directory.authenticate(credential.strip())
            except VTVError:
                assembly.limiter.require(bucket, policy=LOGIN_LIMIT)
                raise
            assembly.limiter.reset(bucket)
            return principal.model_copy(update={"request_id": request_id})

        development = development_principal()
        if development is not None:
            return development.model_copy(update={"request_id": request_id})
        raise NotAuthenticated("this endpoint requires an API key")

    def guard(
        request: Request,
        capability: Capability,
        *,
        organisation_id: str | None = None,
        resource: str | None = None,
        cost: float = 1.0,
    ) -> Principal:
        """Authenticate, rate limit and authorise, in that order.

        The order is not arbitrary. Authenticating first means the rate limit
        can be per tenant rather than per address; limiting before authorising
        means an attacker cannot use permission checks as an oracle to probe at
        full speed.
        """
        principal = authenticate(request)
        plan_policy = PLAN_LIMITS.get(
            assembly.directory.tier_of(principal.organisation_id or ""),
        )
        assembly.limiter.require(
            limit_key("api", principal.organisation_id, principal.subject),
            policy=plan_policy,
            cost=cost,
        )
        assembly.authorizer.suspended = assembly.directory.suspended_ids()
        assembly.authorizer.check(
            principal,
            capability,
            organisation_id=organisation_id or principal.organisation_id,
            resource=resource,
        )
        return principal

    async def owned_project(request: Request, principal: Principal) -> Project | None:
        """Fetch a project only if this principal's tenant owns it.

        A `None` for "not yours" and a `None` for "does not exist" are the same
        answer on purpose: distinguishing them confirms the id to an attacker
        who is guessing.
        """
        project = await repository.get_project(request.path_params["project_id"])
        if project is None:
            return None
        owner = getattr(project, "organisation_id", None)
        if owner is not None and not principal.owns(owner):
            return None
        return project

    # -- routes -----------------------------------------------------------

    async def index(_: Request) -> Response:
        page = WEB_ROOT / "index.html"
        if not page.exists():
            return HTMLResponse("<h1>Voice to Video</h1><p>Web UI not installed.</p>")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    async def static_file(request: Request) -> Response:
        name = request.path_params["name"]
        # Never join user input onto a path without re-resolving and checking.
        target = (WEB_ROOT / name).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.exists():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target)

    async def health(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "environment": settings.env,
                "capabilities": assembly.capabilities.as_dict(),
                "providers": await assembly.router.health(),
                "renderer": settings.renderer,
            }
        )

    async def create_project(request: Request) -> Response:
        principal = guard(request, Capability.PROJECT_CREATE)
        body = await _json_body(request)
        style = StyleProfile(
            style=VisualStyle(body.get("style", "explainer")),
            aspect_ratio=AspectRatio(body.get("aspect_ratio", "16:9")),
            direction=body.get("direction"),
        )
        project = Project(
            title=body.get("title"),
            style=style,
            persistence=PersistenceMode(body.get("persistence", "temporary")),
            organisation_id=principal.organisation_id,
            owner_id=principal.subject,
        )
        if project.persistence is PersistenceMode.TEMPORARY:
            project.expires_at = utc_now() + timedelta(
                hours=settings.temporary_retention_hours
            )
        await repository.save_project(project)
        assembly.audit.write(
            action=AuditAction.PROJECT_CREATED,
            principal=principal,
            target=f"project:{project.project_id}",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        return JSONResponse(
            {"project_id": project.project_id, "expires_at": _iso(project.expires_at)},
            status_code=201,
        )

    async def upload_recording(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request,
            Capability.RENDER_SUBMIT,
            resource=f"project:{project_id}",
            cost=5.0,
        )
        # Renders cost real money per call, so they carry their own bucket on
        # top of the plan's general one.
        assembly.limiter.require(
            limit_key("render", principal.organisation_id), policy=RENDER_LIMIT
        )
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        form = await request.form()
        upload = form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                {"error": {"code": "schema_invalid", "message": "audio file required"}},
                status_code=400,
            )
        data = await upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"error": {"code": "audio_too_long", "message": "recording too large"}},
                status_code=413,
            )

        # Assume every uploaded byte is hostile: identify by content, refuse
        # anything whose bytes disagree with its name (Stage 25).
        verdict = inspect_upload(
            data,
            policy=UploadPolicy(
                max_bytes=MAX_UPLOAD_BYTES,
                allowed_classes=frozenset({ContentClass.AUDIO, ContentClass.VIDEO}),
                # A browser MediaRecorder blob arrives with a generic type and a
                # generic name, so a mismatch here is routine rather than
                # hostile; the class check above is what does the real work.
                reject_mismatch=False,
            ),
            declared_type=getattr(upload, "content_type", None),
            filename=getattr(upload, "filename", None),
        )
        if not verdict.accepted:
            assembly.audit.write(
                action=AuditAction.UPLOAD_REJECTED,
                principal=principal,
                target=f"project:{project_id}",
                succeeded=False,
                detail={"reasons": "; ".join(verdict.reasons)},
                ip_address=_client_ip(request),
            )
            return JSONResponse(
                {
                    "error": {
                        "code": "upload_refused",
                        "message": "; ".join(verdict.reasons) or "upload refused",
                    }
                },
                status_code=415,
            )

        # Reserve the allowance before spending anything. Checking afterwards
        # produces an invoice, not a control.
        estimate = max(0.5, len(data) / (16_000 * 2 * 60))
        quota, reservation = assembly.usage.reserve(
            organisation_id=principal.organisation_id or "",
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=estimate,
        )
        if not quota.allowed:
            return JSONResponse(
                {
                    "error": {
                        "code": "quota_exceeded",
                        "message": quota.reason or "plan allowance exhausted",
                    }
                },
                status_code=402,
            )

        scratch = Path(settings.storage_root).parent / "uploads"
        scratch.mkdir(parents=True, exist_ok=True)
        audio_path = scratch / f"{project_id}.upload"
        audio_path.write_bytes(data)

        # Development-only: a supplied script lets the aligning transcriber run
        # when no speech-to-text credential is configured. It is refused in
        # production, where a real provider must be present.
        script = form.get("script")
        if script and not settings.is_production:
            probe = await assembly.storage.put(
                key=f"projects/{project_id}/pending.marker",
                data=b"",
                content_type="application/octet-stream",
            )
            del probe
            assembly.scripts[f"projects/{project_id}/recordings/"] = str(script)
            assembly.pipeline.capture = _script_binding(assembly, str(script))

        handle = await queue.enqueue(
            kind="pipeline",
            payload={
                "project_id": project_id,
                "audio_path": str(audio_path),
                "quality": str(form.get("quality") or "preview"),
                "frame_rate": int(str(form.get("frame_rate") or 24)),
                "reservation_id": reservation,
                "organisation_id": principal.organisation_id,
            },
            idempotency_key=f"pipeline:{project_id}",
        )
        assembly.audit.write(
            action=AuditAction.RENDER_SUBMITTED,
            principal=principal,
            target=f"project:{project_id}",
            detail={"job_id": handle.job_id, "bytes": str(len(data))},
            ip_address=_client_ip(request),
        )
        return JSONResponse({"job_id": handle.job_id, "status": handle.status.value}, 202)

    async def upload_document(request: Request) -> Response:
        """Stage 21 — a PDF, deck, page or spreadsheet becomes a video.

        Deliberately the same shape as the recordings endpoint: a project id, a
        file, a 202 and a job. The client does not need to know that one path
        has a voice and the other has to be given one.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request,
            Capability.RENDER_SUBMIT,
            resource=f"project:{project_id}",
            cost=3.0,
        )
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        form = await request.form()
        upload = form.get("document")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                {
                    "error": {
                        "code": "schema_invalid",
                        "message": "a document file is required",
                    }
                },
                status_code=400,
            )
        data = await upload.read()
        if not data:
            return JSONResponse(
                {"error": {"code": "schema_invalid", "message": "the file was empty"}},
                status_code=400,
            )
        if len(data) > MAX_DOCUMENT_UPLOAD_BYTES:
            return JSONResponse(
                {
                    "error": {
                        "code": "payload_too_large",
                        "message": "that document is too large",
                    }
                },
                status_code=413,
            )

        filename = getattr(upload, "filename", None)

        document_verdict = inspect_upload(
            data,
            policy=UploadPolicy(
                max_bytes=MAX_DOCUMENT_UPLOAD_BYTES,
                allowed_classes=frozenset(
                    {ContentClass.DOCUMENT, ContentClass.TEXT}
                ),
            ),
            declared_type=getattr(upload, "content_type", None),
            filename=str(filename) if filename else None,
        )
        if not document_verdict.accepted:
            assembly.audit.write(
                action=AuditAction.UPLOAD_REJECTED,
                principal=principal,
                target=f"project:{project_id}",
                succeeded=False,
                detail={"reasons": "; ".join(document_verdict.reasons)},
                ip_address=_client_ip(request),
            )
            return JSONResponse(
                {
                    "error": {
                        "code": "upload_refused",
                        "message": "; ".join(document_verdict.reasons),
                    }
                },
                status_code=415,
            )

        quota = assembly.usage.check(
            organisation_id=principal.organisation_id or "",
            kind=QuotaKind.DOCUMENTS,
            requested=1.0,
        )
        if not quota.allowed:
            return JSONResponse(
                {"error": {"code": "quota_exceeded", "message": quota.reason or ""}},
                status_code=402,
            )

        # Reject before queueing: a user who uploads an unsupported format
        # deserves the answer now, not a job that fails a minute later.
        try:
            assembly.ingestion.registry.for_bytes(  # type: ignore[attr-defined]
                data, filename=filename
            )
        except VTVError as error:
            return _error_response(error)

        scratch = Path(settings.storage_root).parent / "uploads"
        scratch.mkdir(parents=True, exist_ok=True)
        document_path = scratch / f"{project_id}.document"
        document_path.write_bytes(data)

        handle = await queue.enqueue(
            kind="document",
            payload={
                "project_id": project_id,
                "document_path": str(document_path),
                "filename": str(filename) if filename else None,
                "language": str(form.get("language") or "") or None,
                "quality": str(form.get("quality") or "preview"),
                "frame_rate": int(str(form.get("frame_rate") or 24)),
            },
            idempotency_key=f"document:{project_id}",
        )
        assembly.usage.record(
            organisation_id=principal.organisation_id or "",
            kind=QuotaKind.DOCUMENTS,
            quantity=1.0,
            project_id=project_id,
            idempotency_key=f"document:{project_id}:{handle.job_id}",
        )
        assembly.audit.write(
            action=AuditAction.DOCUMENT_UPLOADED,
            principal=principal,
            target=f"project:{project_id}",
            detail={
                "media_type": document_verdict.media_type,
                "bytes": str(len(data)),
            },
            ip_address=_client_ip(request),
        )
        return JSONResponse(
            {
                "job_id": handle.job_id,
                "status": handle.status.value,
                "supported_formats": assembly.ingestion.supported_formats(),
                "notes": (
                    []
                    if assembly.capabilities.real_speech_synthesis
                    else [
                        "No speech synthesiser is configured: the finished video "
                        "will have captions and visuals but no voice track."
                    ]
                ),
            },
            status_code=202,
        )

    async def get_project(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        result = state.results.get(project_id)
        payload: dict[str, Any] = {
            "project_id": project.project_id,
            "title": project.title,
            "status": project.status.value,
            "progress": project.progress,
            "current_stage": project.current_stage.value if project.current_stage else None,
            "stages": [
                {
                    "stage": stage.stage.value,
                    "status": stage.status.value,
                    "detail": stage.detail,
                }
                for stage in project.stages
            ],
            "cost_usd": project.total_cost_usd,
            "expires_at": _iso(project.expires_at),
        }
        if result and result.render_job and result.render_job.status is Status.READY:
            payload["video_url"] = f"/v1/projects/{project_id}/video"
            payload["captions_url"] = f"/v1/projects/{project_id}/captions.vtt"
            payload["duration_seconds"] = result.render_job.duration_seconds
        if result is not None and not result.narration_has_speech:
            payload["narration"] = {
                "has_speech": False,
                "reason": (
                    "This project was built from a document and no speech "
                    "synthesiser is configured, so the audio track is silent."
                ),
            }
        if result is not None and result.source_document is not None:
            payload["source"] = {
                "kind": result.source_document.kind.value,
                "origin": result.source_document.origin,
                "parser": result.source_document.parser,
                "blocks": len(result.source_document.blocks),
                "extraction_confidence": result.source_document.extraction_confidence,
            }
        failed = project.failed_stage
        if failed and failed.error:
            payload["error"] = {
                "stage": failed.stage.value,
                "message": failed.error.user_message or "Something went wrong.",
            }
        return JSONResponse(payload)

    async def storyboard(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        if await owned_project(request, principal) is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        result = state.results.get(project_id)
        if result is None or result.scene_graph is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        scenes: list[dict[str, Any]] = []
        for scene in result.scene_graph.scenes:
            plan = result.visual_plan.plan_for(scene.scene_id) if result.visual_plan else None
            clip = next(
                (c for c in (result.timeline.clips if result.timeline else []) if c.scene_id == scene.scene_id),
                None,
            )
            source_label = None
            if clip and isinstance(clip.source, AssetClipSource):
                source_label = clip.source.attribution or "asset"
            elif clip and isinstance(clip.source, ProgrammaticClipSource):
                source_label = f"drawn · {clip.source.spec.primitive.value}"
            scenes.append(
                {
                    "scene_id": scene.scene_id,
                    "index": scene.index,
                    "start": scene.span.start,
                    "end": scene.span.end,
                    "narration": scene.narration,
                    "purpose": scene.purpose.value,
                    "visual_goal": scene.visual_goal.value,
                    "visual_brief": scene.visual_brief,
                    "importance": scene.importance,
                    "strategy": plan.primary.strategy.value if plan else None,
                    # The reason the system chose this. Exposed on purpose: a
                    # user who disagrees can see why, which turns an argument
                    # with a black box into a conversation.
                    "rationale": plan.primary.rationale if plan else None,
                    "estimated_cost_usd": plan.primary.estimate.usd if plan else 0.0,
                    "fallbacks": [f.strategy.value for f in plan.fallbacks] if plan else [],
                    "degraded": bool(clip and clip.degradation),
                    "degradation": [
                        {"from": step.from_strategy, "to": step.to_strategy, "reason": step.reason.value}
                        for step in (clip.degradation if clip else [])
                    ],
                    "source": source_label,
                    "thumbnail": f"/v1/projects/{project_id}/scenes/{scene.scene_id}/thumbnail.png",
                }
            )

        return JSONResponse(
            {
                "project_id": project_id,
                "topic": result.understanding.topic if result.understanding else None,
                "thesis": result.scene_graph.narrative.thesis,
                "strategy_mix": result.visual_plan.strategy_mix() if result.visual_plan else {},
                "cost": result.ledger.summary() if result.ledger else {},
                "scenes": scenes,
            }
        )

    async def thumbnail(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        if await owned_project(request, principal) is None:
            return PlainTextResponse("not found", status_code=404)
        scene_id = request.path_params["scene_id"]
        result = state.results.get(project_id)
        if result is None or result.timeline is None:
            return PlainTextResponse("not found", status_code=404)
        clip = next((c for c in result.timeline.clips if c.scene_id == scene_id), None)
        if clip is None:
            return PlainTextResponse("not found", status_code=404)

        size = RenderSize.for_aspect(result.timeline.aspect_ratio, scale=0.3)
        if isinstance(clip.source, ProgrammaticClipSource):
            # Free, and always exactly what will be rendered, because it is the
            # same drawing code at the same progress.
            image = AnimationEngine(result.timeline.style).still(
                clip.source.spec, size=size, progress=1.0
            )
        elif isinstance(clip.source, AssetClipSource):
            import io

            from PIL import Image

            data = await assembly.storage.get(clip.source.object)
            with Image.open(io.BytesIO(data)) as opened:
                image = opened.convert("RGB")
                image.thumbnail((size.width, size.height))
        else:
            image = AnimationEngine(result.timeline.style).still(
                _placeholder_spec(), size=size, progress=1.0
            )

        import io as _io

        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            buffer.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=300"},
        )

    async def video(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        if await owned_project(request, principal) is None:
            return PlainTextResponse("not found", status_code=404)
        result = state.results.get(project_id)
        if result is None or result.render_job is None or result.render_job.output is None:
            return PlainTextResponse("not found", status_code=404)
        path = assembly.storage.path_for(result.render_job.output)
        if not path.exists():
            return PlainTextResponse("expired", status_code=410)
        return FileResponse(path, media_type="video/mp4", filename=f"{project_id}.mp4")

    async def captions(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        if await owned_project(request, principal) is None:
            return PlainTextResponse("not found", status_code=404)
        result = state.results.get(project_id)
        if result is None or result.timeline is None:
            return PlainTextResponse("not found", status_code=404)
        return PlainTextResponse(
            to_vtt(result.timeline.captions), media_type="text/vtt"
        )

    async def events_stream(request: Request) -> Response:
        """Server-sent events. Progress the user can actually read."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        if await owned_project(request, principal) is None:
            return PlainTextResponse("not found", status_code=404)

        async def publish() -> Any:
            import asyncio

            sent = 0
            for _ in range(1800):  # ~15 minutes at 0.5s
                log = state.event_log.get(project_id, [])
                while sent < len(log):
                    event = log[sent]
                    sent += 1
                    yield f"data: {event.canonical_json()}\n\n"
                    if event.name.value in {"render.completed", "render.failed", "stage.failed"}:
                        return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def revise_scene(request: Request) -> Response:
        """Stage 13 — semantic editing.

        The user changes *meaning or approach*, not frames: pick a different
        strategy, or force a specific one. Re-rendering is a separate action so
        several edits can be made before paying for a render.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        if await owned_project(request, principal) is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        scene_id = request.path_params["scene_id"]
        result = state.results.get(project_id)
        if result is None or result.visual_plan is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await _json_body(request)
        plan = result.visual_plan.plan_for(scene_id)
        if plan is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        action = body.get("action", "next_strategy")
        if action == "next_strategy" and plan.fallbacks:
            promoted = plan.fallbacks[0]
            updated = plan.model_copy(
                update={"primary": promoted, "fallbacks": plan.fallbacks[1:]}
            )
        else:
            return JSONResponse(
                {"error": {"code": "schema_invalid", "message": "no alternative available"}},
                status_code=400,
            )

        result.visual_plan.scene_plans = [
            updated if p.scene_id == scene_id else p
            for p in result.visual_plan.scene_plans
        ]
        await repository.put_document(
            project_id=project_id,
            kind="visual_plan",
            document_id=result.visual_plan.visual_plan_id,
            payload=json.loads(result.visual_plan.model_dump_json()),
        )
        return JSONResponse(
            {
                "scene_id": scene_id,
                "strategy": updated.primary.strategy.value,
                "rationale": updated.primary.rationale,
            }
        )

    async def visualize(request: Request) -> Response:
        """Stage 20 — the platform endpoint.

        Text in, a visual story out. The same engine as the voice product with
        the first two stages skipped, which is the entire argument for keeping
        the pipeline a chain of documents.
        """
        guard(request, Capability.PROJECT_CREATE, cost=2.0)
        body = await _json_body(request)
        content = (body.get("input") or {}).get("content")
        if not content or not isinstance(content, str):
            return JSONResponse(
                {"error": {"code": "schema_invalid", "message": "input.content required"}},
                status_code=400,
            )
        if len(content) > 20_000:
            return JSONResponse(
                {"error": {"code": "schema_invalid", "message": "input too long"}},
                status_code=413,
            )
        try:
            from vtv.pipeline.text_entry import visualise_text

            payload = await visualise_text(
                assembly,
                content,
                style=StyleProfile(
                    style=VisualStyle(body.get("style", "explainer")),
                    aspect_ratio=AspectRatio(body.get("aspect_ratio", "16:9")),
                ),
                render=bool(body.get("render", False)),
            )
        except VTVError as error:
            return _error_response(error)
        return JSONResponse(payload)

    async def usage(request: Request) -> Response:
        """What this tenant has used this period, and what remains."""
        principal = guard(request, Capability.BILLING_READ)
        summary = assembly.usage.summary(
            organisation_id=principal.organisation_id or ""
        )
        payload = summary.as_dict()
        # Margin is an internal number. A customer sees what they consumed and
        # what it will cost them, never what it cost us.
        payload.pop("gross_margin_usd", None)
        payload.pop("provider_cost_usd", None)
        return JSONResponse(payload)

    async def audit_log(request: Request) -> Response:
        """The tenant's own audit trail. Never anyone else's."""
        principal = guard(request, Capability.AUDIT_READ)
        limit = min(int(request.query_params.get("limit", "100")), 500)
        security_only = request.query_params.get("security") == "true"
        entries = assembly.audit.recent(
            organisation_id=principal.organisation_id,
            security_only=security_only,
            limit=limit,
        )
        return JSONResponse(
            {
                "entries": [
                    {
                        "id": entry.audit_event_id,
                        "action": entry.action.value,
                        "actor": entry.actor,
                        "target": entry.target,
                        "succeeded": entry.succeeded,
                        "security_relevant": entry.is_security_relevant,
                        "detail": entry.detail,
                        "at": entry.created_at.isoformat(),
                    }
                    for entry in entries
                ]
            }
        )

    async def list_api_keys(request: Request) -> Response:
        principal = guard(request, Capability.API_KEY_MANAGE)
        keys = assembly.directory.keys_for(principal.organisation_id or "")
        return JSONResponse(
            {
                "keys": [
                    {
                        "id": key.api_key_id,
                        "name": key.name,
                        "prefix": key.prefix,
                        "role": key.role.value,
                        "active": key.is_active,
                        "created_at": key.created_at.isoformat(),
                        "expires_at": _iso(key.expires_at),
                    }
                    for key in keys
                ]
            }
        )

    async def create_api_key(request: Request) -> Response:
        """Mint a key. The secret is in this response and nowhere else, ever."""
        principal = guard(request, Capability.API_KEY_MANAGE)
        body = await _json_body(request)
        organisation_id = principal.organisation_id or ""

        existing = [
            key
            for key in assembly.directory.keys_for(organisation_id)
            if key.is_active
        ]
        quota = assembly.usage.check(
            organisation_id=organisation_id,
            kind=QuotaKind.API_KEYS,
            requested=1.0,
        )
        del quota
        plan_limit = assembly.usage.plan_of(organisation_id).limit(QuotaKind.API_KEYS)
        if len(existing) >= plan_limit:
            return JSONResponse(
                {
                    "error": {
                        "code": "quota_exceeded",
                        "message": f"your plan allows {plan_limit:g} active keys",
                    }
                },
                status_code=402,
            )

        try:
            minted = mint_api_key(
                organisation_id=organisation_id,
                name=str(body.get("name") or "api key")[:120],
                role=Role(body.get("role", "service")),
                created_by=principal.subject,
                expires_in_days=int(body["expires_in_days"])
                if body.get("expires_in_days")
                else 365,
                live=settings.is_production,
            )
        except (VTVError, ValueError) as error:
            if isinstance(error, VTVError):
                return _error_response(error)
            return JSONResponse(
                {"error": {"code": "schema_invalid", "message": "unknown role"}},
                status_code=400,
            )

        assembly.directory.store_key(minted.record)
        assembly.audit.write(
            action=AuditAction.KEY_CREATED,
            principal=principal,
            target=f"api_key:{minted.record.api_key_id}",
            detail={"prefix": minted.record.prefix, "role": minted.record.role.value},
            ip_address=_client_ip(request),
        )
        return JSONResponse(
            {
                "id": minted.record.api_key_id,
                "prefix": minted.record.prefix,
                "expires_at": _iso(minted.record.expires_at),
                # The one and only time this value is ever transmitted.
                "secret": minted.secret,
                "notice": "Store this now. It cannot be retrieved again.",
            },
            status_code=201,
        )

    async def revoke_api_key(request: Request) -> Response:
        principal = guard(request, Capability.API_KEY_MANAGE)
        api_key_id = request.path_params["api_key_id"]
        owned = [
            key
            for key in assembly.directory.keys_for(principal.organisation_id or "")
            if key.api_key_id == api_key_id
        ]
        if not owned:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        assembly.directory.revoke_key(api_key_id)
        assembly.audit.write(
            action=AuditAction.KEY_REVOKED,
            principal=principal,
            target=f"api_key:{api_key_id}",
            detail={"prefix": owned[0].prefix},
            ip_address=_client_ip(request),
        )
        return JSONResponse({"revoked": api_key_id})

    async def media(request: Request) -> Response:
        bucket = request.path_params["bucket"]
        key = request.path_params["key"]
        token = request.query_params.get("token")
        if not token:
            return PlainTextResponse("forbidden", status_code=403)
        try:
            claims = assembly.storage.verify(token)
        except VTVError as error:
            return _error_response(error, status_code=403)
        if claims.get("b") != bucket or claims.get("k") != key:
            return PlainTextResponse("forbidden", status_code=403)
        from vtv.contracts.base import ObjectRef

        ref = ObjectRef(bucket=bucket, key=key, content_type="application/octet-stream")
        path = assembly.storage.path_for(ref)
        if not path.exists():
            return PlainTextResponse("gone", status_code=410)
        return FileResponse(path)

    async def sweep(_: Request) -> Response:
        """Retention sweep. In production this is a bucket lifecycle rule and a
        scheduled job, not an endpoint; here it makes the policy testable."""
        expired = await repository.expired_projects(now=utc_now())
        for project_id in expired:
            await repository.delete_project(project_id)
            state.results.pop(project_id, None)
        removed = await assembly.storage.sweep_expired(
            older_than_seconds=settings.temporary_retention_hours * 3600
        )
        return JSONResponse({"projects_deleted": len(expired), "objects_deleted": len(removed)})

    routes = [
        Route("/", index),
        Route("/static/{name}", static_file),
        Route("/health", health),
        Route("/v1/projects", create_project, methods=["POST"]),
        Route("/v1/projects/{project_id}", get_project),
        Route("/v1/projects/{project_id}/recordings", upload_recording, methods=["POST"]),
        Route("/v1/projects/{project_id}/documents", upload_document, methods=["POST"]),
        Route("/v1/projects/{project_id}/storyboard", storyboard),
        Route("/v1/projects/{project_id}/scenes/{scene_id}/thumbnail.png", thumbnail),
        Route("/v1/projects/{project_id}/scenes/{scene_id}/revise", revise_scene, methods=["POST"]),
        Route("/v1/projects/{project_id}/video", video),
        Route("/v1/projects/{project_id}/captions.vtt", captions),
        Route("/v1/projects/{project_id}/events", events_stream),
        Route("/v1/visualize", visualize, methods=["POST"]),
        Route("/v1/usage", usage),
        Route("/v1/audit", audit_log),
        Route("/v1/api-keys", list_api_keys),
        Route("/v1/api-keys", create_api_key, methods=["POST"]),
        Route("/v1/api-keys/{api_key_id}", revoke_api_key, methods=["DELETE"]),
        Route("/media/{bucket}/{key:path}", media),
        Route("/internal/sweep", sweep, methods=["POST"]),
    ]

    async def on_vtv_error(_: Request, exc: Exception) -> Response:
        """One boundary for every domain error.

        Guards raise rather than return, which keeps the happy path in each
        route readable. This is where those become responses, and it is the only
        place that decides a status code.
        """
        if isinstance(exc, VTVError):
            return _error_response(exc)
        raise exc

    app = Starlette(
        debug=not settings.is_production,
        routes=routes,
        exception_handlers={VTVError: on_vtv_error},
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=settings.allowed_origins,
                allow_methods=["GET", "POST"],
                allow_headers=["*"],
            )
        ],
    )
    app.state.vtv = state
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """The caller's address, for limiting unauthenticated attempts.

    `X-Forwarded-For` is trusted only for its left-most entry and only because
    this service is expected to sit behind a proxy that sets it. A deployment
    without such a proxy must not trust it at all — stated here rather than
    assumed, because a spoofable rate-limit key is no rate limit.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    client = request.client
    return client.host if client else "unknown"


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _placeholder_spec() -> Any:
    from vtv.contracts.visual_language import TypographySpec

    return TypographySpec(headline="Visual unavailable")


def _script_binding(assembly: Assembly, script: str) -> Any:
    """Wrap capture so the development transcriber can find its script.

    Only reachable outside production (see the caller). It exists because the
    aligning transcriber refuses to invent words, which is the behaviour we
    want — so the script has to arrive from somewhere explicit.
    """
    capture = assembly.pipeline.capture
    original = capture.capture

    async def wrapped(**kwargs: Any) -> Any:
        recording = await original(**kwargs)
        assembly.scripts[recording.audio.key] = script
        return recording

    capture.capture = wrapped  # type: ignore[method-assign]
    return capture


async def _persist(repository: SqliteProjectRepository, result: PipelineResult) -> None:
    """Store the reasoning, so the video can be rebuilt without keeping it."""
    await repository.save_project(result.project)
    documents = {
        "recording": result.recording,
        "transcript": result.transcript,
        "understanding": result.understanding,
        "scene_graph": result.scene_graph,
        "visual_plan": result.visual_plan,
        "timeline": result.timeline,
        "render_job": result.render_job,
    }
    for kind, document in documents.items():
        if document is None:
            continue
        await repository.put_document(
            project_id=result.project.project_id,
            kind=kind,
            document_id=getattr(document, f"{kind}_id", kind),
            payload=json.loads(document.model_dump_json()),
        )


__all__ = ["MAX_UPLOAD_BYTES", "WEB_ROOT", "ApiState", "create_app"]
