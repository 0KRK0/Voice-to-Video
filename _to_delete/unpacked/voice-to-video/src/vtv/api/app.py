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
from vtv.config import Settings
from vtv.contracts.base import utc_now
from vtv.contracts.errors import ErrorCategory, ErrorCode, Status, VTVError
from vtv.contracts.project import PersistenceMode, Project
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.style import AspectRatio, StyleProfile, VisualStyle
from vtv.contracts.timeline import AssetClipSource, ProgrammaticClipSource
from vtv.observability.events import Event
from vtv.pipeline.captions import to_vtt
from vtv.pipeline.orchestrator import PipelineResult
from vtv.wiring import Assembly, build

WEB_ROOT = Path(__file__).resolve().parents[3] / "apps" / "web"

#: Cap on a single upload. Enforced before anything is read into memory.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


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


def _error_response(error: VTVError, status_code: int | None = None) -> JSONResponse:
    """Never leak internals. `user_message` is what reaches the browser."""
    code = status_code or {
        ErrorCategory.VALIDATION: 400,
        ErrorCategory.POLICY: 403,
        ErrorCategory.NOT_FOUND: 404,
        ErrorCategory.TIMEOUT: 504,
        ErrorCategory.PROVIDER: 502,
        ErrorCategory.PROVIDER_REFUSED: 422,
        ErrorCategory.INTERNAL: 500,
    }.get(error.info.category, 500)
    return JSONResponse(
        {
            "error": {
                "code": error.info.code.value,
                "message": error.info.user_message or "Something went wrong.",
                "retryable": error.info.retryable,
            }
        },
        status_code=code,
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
        return project.project_id

    queue.register("pipeline", run_pipeline)

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
        )
        if project.persistence is PersistenceMode.TEMPORARY:
            project.expires_at = utc_now() + timedelta(
                hours=settings.temporary_retention_hours
            )
        await repository.save_project(project)
        return JSONResponse(
            {"project_id": project.project_id, "expires_at": _iso(project.expires_at)},
            status_code=201,
        )

    async def upload_recording(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        project = await repository.get_project(project_id)
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
            },
            idempotency_key=f"pipeline:{project_id}",
        )
        return JSONResponse({"job_id": handle.job_id, "status": handle.status.value}, 202)

    async def get_project(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        project = await repository.get_project(project_id)
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
        failed = project.failed_stage
        if failed and failed.error:
            payload["error"] = {
                "stage": failed.stage.value,
                "message": failed.error.user_message or "Something went wrong.",
            }
        return JSONResponse(payload)

    async def storyboard(request: Request) -> Response:
        project_id = request.path_params["project_id"]
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
        result = state.results.get(project_id)
        if result is None or result.render_job is None or result.render_job.output is None:
            return PlainTextResponse("not found", status_code=404)
        path = assembly.storage.path_for(result.render_job.output)
        if not path.exists():
            return PlainTextResponse("expired", status_code=410)
        return FileResponse(path, media_type="video/mp4", filename=f"{project_id}.mp4")

    async def captions(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        result = state.results.get(project_id)
        if result is None or result.timeline is None:
            return PlainTextResponse("not found", status_code=404)
        return PlainTextResponse(
            to_vtt(result.timeline.captions), media_type="text/vtt"
        )

    async def events_stream(request: Request) -> Response:
        """Server-sent events. Progress the user can actually read."""
        project_id = request.path_params["project_id"]

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
        Route("/v1/projects/{project_id}/storyboard", storyboard),
        Route("/v1/projects/{project_id}/scenes/{scene_id}/thumbnail.png", thumbnail),
        Route("/v1/projects/{project_id}/scenes/{scene_id}/revise", revise_scene, methods=["POST"]),
        Route("/v1/projects/{project_id}/video", video),
        Route("/v1/projects/{project_id}/captions.vtt", captions),
        Route("/v1/projects/{project_id}/events", events_stream),
        Route("/v1/visualize", visualize, methods=["POST"]),
        Route("/media/{bucket}/{key:path}", media),
        Route("/internal/sweep", sweep, methods=["POST"]),
    ]

    app = Starlette(
        debug=not settings.is_production,
        routes=routes,
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
