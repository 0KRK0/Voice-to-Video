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
import contextlib
import os
import re
import time
from dataclasses import dataclass, field
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

from vtv.adapters.media import compute
from vtv.adapters.queue.durable import RECLAIM_AFTER_SECONDS, DurableJobQueue
from vtv.adapters.repository.sqlite import SqliteProjectRepository
from vtv.animation.engine import AnimationEngine, RenderSize
from vtv.api import devices as devices_api
from vtv.api import media as media_api
from vtv.api import product
from vtv.billing.plans import QuotaKind
from vtv.config import Settings
from vtv.contracts.base import IdPrefix, RetentionClass, new_id, utc_now
from vtv.contracts.consistency import VisualBible
from vtv.contracts.errors import (
    ErrorCategory,
    ErrorCode,
    Status,
    ValidationFailed,
    VTVError,
)
from vtv.contracts.generation import GenerationKind, VisualFidelity
from vtv.contracts.project import PersistenceMode, Project
from vtv.contracts.render import RenderJob, RenderQuality
from vtv.contracts.scene import SceneGraph
from vtv.contracts.source import SourceDocument
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
from vtv.contracts.timeline import AssetClipSource, ProgrammaticClipSource, Timeline
from vtv.contracts.visual_plan import VisualPlan
from vtv.dispatch import DevicePool
from vtv.jobs import JobKind, RenderPayload
from vtv.observability.context import correlated, sanitise
from vtv.observability.events import Event
from vtv.pipeline.allowance import price_of_one_image
from vtv.pipeline.captions import to_vtt
from vtv.pipeline.orchestrator import RunOutcome
from vtv.retention import RetentionService, expiry_for
from vtv.security.authz import NotAuthenticated
from vtv.security.keys import mint_api_key
from vtv.security.limits import (
    LOGIN_LIMIT,
    PLAN_LIMITS,
    RENDER_LIMIT,
    RequestBounds,
    limit_key,
)
from vtv.security.paths import safe_filename, tenant_key
from vtv.security.uploads import ContentClass, UploadPolicy, inspect_upload
from vtv.wiring import (
    Assembly,
    build,
    execution_registry,
    queue_path,
    repository_path,
)

WEB_ROOT = Path(__file__).resolve().parents[3] / "apps" / "web"
#: The built editor. `apps/web/dist` after `tsc && node scripts/build.mjs`.
WEB_DIST = WEB_ROOT / "dist"

#: A content-addressed asset name, e.g. `main.7ee75bfe0c.js`. These may be
#: cached forever, because a change to the file changes the name.
_FINGERPRINTED = re.compile(r"\.[0-9a-f]{10}\.(?:js|css)$")

#: Sent on every response, from the middleware, so no route can be added
#: without them.
#:
#: `frame-ancestors` is here rather than in the page's meta tag because
#: browsers ignore it there — a clickjacking defence declared in a meta element
#: looks present and is not. `X-Frame-Options` accompanies it for the handful
#: of clients that still only understand the older header.
#:
#: `nosniff` matters more than it looks: this application serves user uploads,
#: and content-type sniffing is what turns an uploaded file into an executed
#: one.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), geolocation=(), payment=()"),
)

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
class ProjectArtifacts:
    """Everything a read endpoint needs, loaded from durable storage.

    This replaced an in-memory `dict[str, PipelineResult]` that was the only
    source for video, storyboard and captions. That dict died with the process
    and was invisible to every other replica, so a restart made finished videos
    unreachable and a two-pod deployment returned 404 at random.

    Documents are the source of truth. The worker writes them; any API replica
    reads them; nothing is held between requests.
    """

    project: Project
    render_job: RenderJob | None = None
    timeline: Timeline | None = None
    scene_graph: SceneGraph | None = None
    visual_plan: VisualPlan | None = None
    source_document: SourceDocument | None = None
    #: P1-1. Read back so the storyboard can show what is locked, and so a
    #: revision does not silently discard the user's own decisions.
    visual_bible: VisualBible | None = None

    @property
    def is_rendered(self) -> bool:
        return (
            self.render_job is not None
            and self.render_job.status is Status.READY
            and self.render_job.output is not None
        )


@dataclass
class ApiState:
    """Everything the routes need. One object, injected, never a global."""

    assembly: Assembly
    repository: SqliteProjectRepository
    queue: DurableJobQueue
    #: Bounded ring of recent events per project, so a browser that connects
    #: late still sees what it missed. Best-effort and process-local by
    #: design: it is a convenience for a live viewer, never a source of truth,
    #: and every fact it carries is also persisted.
    event_log: dict[str, list[Event]] = field(default_factory=dict)
    #: Set once the readiness probe has seen an empty migration backlog. Only
    #: ever latches from False to True: outstanding migrations can be resolved
    #: while this process runs (the migrate job finishes), but a schema cannot
    #: become out of date without a new release, which is a new process.
    migrations_ready: bool = False

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
    repository = SqliteProjectRepository(repository_path(settings))
    # P0-5. The durable queue is the production queue. The API registers NO
    # handlers, so it cannot execute a job even by accident — the split that
    # the audit found missing is enforced by what this process knows how to do,
    # not by a convention about which function is called where.
    queue = DurableJobQueue(
        queue_path(settings),
        events=assembly.events,
        # Explicitly, and the same value the worker uses. Taking the
        # default here meant the API reclaimed after sixty seconds and the
        # worker after fifteen minutes — the same job, two answers,
        # depending on which process looked.
        reclaim_after_seconds=RECLAIM_AFTER_SECONDS,
    )
    state = ApiState(assembly=assembly, repository=repository, queue=queue)
    # P1-7. One retention implementation, shared with the worker. There used
    # to be two, written independently, and only one applied a tenant scope.
    retention = RetentionService(
        repository=repository,
        storage=assembly.storage,
        temporary_hours=settings.temporary_retention_hours,
        # Same reason as `jobs.run_retention_sweep`: the tier resolves the
        # byte-retention ceiling, and without it there is no ceiling.
        directory=assembly.directory,
    )
    assembly.events.subscribe(state.record)

    # -- jobs -------------------------------------------------------------
    #
    # There are none here. Handlers live in `vtv.jobs` and are registered only
    # by `vtv.worker`. Before the 2026-08-13 audit `run_pipeline` was a closure
    # in this function and rendering happened in this process's event loop.

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
        assembly.authorizer.check(
            principal,
            capability,
            organisation_id=organisation_id or principal.organisation_id,
            resource=resource,
        )
        return principal

    async def owned_project(request: Request, principal: Principal) -> Project | None:
        """Fetch a project only if this principal's tenant owns it.

        The scope goes into the query, not into an `if` after it. Before the
        2026-08-13 audit this compared in Python and treated a null owner as
        "unowned, therefore yours", which made every project created outside
        this route readable by every tenant. A query that cannot match is not
        something a caller can forget.

        A `None` for "not yours" and a `None` for "does not exist" are the same
        answer on purpose: distinguishing them confirms the id to an attacker
        who is guessing.
        """
        if principal.organisation_id is None:
            return None
        return await repository.get_project(
            request.path_params["project_id"],
            organisation_id=principal.organisation_id,
        )

    async def load_artifacts(
        project: Project,
    ) -> ProjectArtifacts:
        """Rebuild what a read endpoint needs from persisted documents."""

        async def document(kind: str, model: Any) -> Any:
            payload = await repository.get_document(
                project_id=project.project_id, kind=kind
            )
            if payload is None:
                return None
            try:
                return model.model_validate(payload)
            except VTVError:  # pragma: no cover - defensive
                return None
            except ValueError:
                # A document written by an older schema. Losing one artefact is
                # better than failing the whole read, and the drift is visible
                # in the schema-export test rather than here.
                return None

        return ProjectArtifacts(
            project=project,
            render_job=await document("render_job", RenderJob),
            timeline=await document("timeline", Timeline),
            scene_graph=await document("scene_graph", SceneGraph),
            visual_plan=await document("visual_plan", VisualPlan),
            source_document=await document("source_document", SourceDocument),
            visual_bible=await document("visual_bible", VisualBible),
        )


    # -- observability ----------------------------------------------------
    #
    # Bound to a local name so the closures below capture a definitely-built
    # assembly rather than the optional parameter.
    metrics = assembly.metrics

    class ObservabilityMiddleware:
        """P1-6. One trace per request, and the numbers a dashboard needs.

        Middleware rather than a decorator on each route, because the audit's
        finding was that instrumentation applied per-handler is instrumentation
        with a hole shaped like the next handler someone adds.

        Three things happen here and nowhere else:

        * a `trace_id` is established, honouring an inbound `X-Request-Id` so a
          caller's own tracing joins up with ours, and returned in the response
          so a user reporting a problem can quote it;
        * latency and status are counted, labelled by the **route template**
          rather than the path, because `/v1/projects/{project_id}` is one
          series and `/v1/projects/prj_…` is one series per project;
        * the correlation is torn down afterwards, so nothing leaks into the
          next request on the same worker thread.
        """

        def __init__(self, app: Any) -> None:
            self.app = app
            # Endpoint object to route template, built once. Starlette records
            # `scope["endpoint"]` when it matches, but not the pattern — and the
            # pattern is the whole point: `/v1/projects/{project_id}` is one
            # metric series, `/v1/projects/prj_…` is one per project.
            self.templates = {
                id(route.endpoint): route.path
                for route in routes
                if isinstance(route, Route)
            }

        def _template(self, scope: Any) -> str:
            endpoint = scope.get("endpoint")
            if endpoint is None:
                # No match: a 404, and the path that produced it is unbounded.
                return "unmatched"
            return self.templates.get(id(endpoint), "unmatched")

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            inbound = sanitise(headers.get("x-request-id"))
            started = time.perf_counter()
            status_holder = {"code": 500}

            async def send_wrapper(message: Any) -> None:
                if message["type"] == "http.response.start":
                    status_holder["code"] = int(message["status"])
                    message.setdefault("headers", [])
                    message["headers"] = [
                        *message["headers"],
                        (b"x-request-id", correlation.trace_id.encode("latin-1")),
                        *SECURITY_HEADERS,
                    ]
                await send(message)

            with correlated(trace_id=inbound, request_id=inbound) as correlation:
                try:
                    await self.app(scope, receive, send_wrapper)
                finally:
                    # Recorded in `finally` so a request that raised is counted.
                    # An error rate computed only from responses that arrived is
                    # an error rate that hides the worst failures.
                    template = self._template(scope)
                    elapsed = time.perf_counter() - started
                    status = status_holder["code"]
                    metrics.increment(
                        "vtv_http_requests_total",
                        route=template,
                        status=f"{status // 100}xx",
                    )
                    metrics.observe(
                        "vtv_http_request_duration_seconds", elapsed, route=template
                    )

    async def metrics_endpoint(request: Request) -> Response:
        """Prometheus scrape target.

        Unauthenticated, like the health probes, because a scraper has no
        credential — and, like them, it must disclose nothing about *whose*
        data this is. Every series here is labelled by route, kind, outcome or
        error code; none is labelled by tenant, project or user. That is a
        cardinality decision first and a disclosure decision second, and it
        happens to be the right answer for both.

        Queue depth is sampled at scrape time rather than tracked, because a
        gauge maintained by increments drifts the moment a process dies holding
        a job.
        """
        del request
        try:
            stats = await state.queue.stats()
            for label, value in stats.items():
                metrics.set("vtv_queue_depth", float(value), state=label)
            metrics.set("vtv_dead_letters", float(stats.get("dead_letter", 0)))
        except Exception:
            pass
        return PlainTextResponse(
            metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # -- routes -----------------------------------------------------------

    async def index(_: Request) -> Response:
        """Serve the built editor.

        The application serves its own frontend, which is not an incidental
        convenience: same-origin means there is no CORS configuration to get
        wrong, no preflight on every API call, and no second host to secure.
        A split deployment is possible — the client takes a base URL — but this
        is the arrangement the product is built for.
        """
        page = WEB_DIST / "index.html"
        if not page.exists():
            # Names the command that exists. This said `make web`, which is not
            # a target in the Makefile and never has been — and it is the first
            # thing a new operator sees, so it was the one error message in the
            # system guaranteed to be read by somebody with no other context.
            return HTMLResponse(
                "<h1>Voice to Video</h1>"
                "<p>The editor is not built. Run "
                "<code>npm --prefix apps/web run build</code> "
                "(needs Node 20+ and a global <code>typescript</code>).</p>",
                status_code=503,
            )
        return HTMLResponse(
            page.read_text(encoding="utf-8"),
            # The one file that must not be cached: it is what points at the
            # fingerprinted assets, and a stale copy pins the whole app to a
            # previous deploy.
            headers={"Cache-Control": "no-store"},
        )

    async def web_asset(request: Request) -> Response:
        """Serve a fingerprinted asset, or the shell for a client-side route.

        Two jobs in one handler because they share the traversal check. A path
        that resolves outside the build directory is refused before it is
        touched — `resolve()` first, compare second, because a check performed
        on the unresolved string is a check `../` walks straight past.
        """
        name = request.path_params["path"]

        # Anything but a page request is a 404, whatever the path looks like.
        # This fallback exists to hand a *navigation* to the client-side router;
        # answering a POST with an HTML shell would tell a caller that a
        # non-existent endpoint accepted their body.
        if request.method not in {"GET", "HEAD"}:
            return PlainTextResponse("not found", status_code=404)

        target = (WEB_DIST / name).resolve()
        root = WEB_DIST.resolve()

        if target.is_file() and (target == root or root in target.parents):
            fingerprinted = _FINGERPRINTED.search(target.name) is not None
            return FileResponse(
                target,
                headers={
                    "Cache-Control": (
                        "public, max-age=31536000, immutable"
                        if fingerprinted
                        else "public, max-age=300"
                    )
                },
            )

        # Not a file. If it looks like an asset request, it is a 404; if it
        # looks like a page, the client-side router owns it and gets the shell.
        if "." in name.rsplit("/", 1)[-1]:
            return PlainTextResponse("not found", status_code=404)
        return await index(request)

    async def static_file(request: Request) -> Response:
        """The legacy `/static/{name}` path, kept so old links still resolve."""
        name = request.path_params["name"]
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
                # What this machine can actually do, measured rather than
                # advertised. Deliberately a report and not a switch — see
                # `adapters/media/compute.py` for why a "GPU" render mode would
                # be a 1.3× ceiling attached to a button labelled "Fast".
                "compute": {
                    **compute.detect().as_json(),
                    # Every execution target, with a straight answer about
                    # each — including the ones that do not work and why. An
                    # interface listing one option cannot explain why there are
                    # not four, and "why can't I use my graphics card" is a
                    # support ticket either way. See `contracts/execution.py`.
                    "targets": [
                        item.as_json()
                        for item in execution_registry(settings).available()
                    ],
                },
            }
        )

    async def health_live(_: Request) -> Response:
        """Can this process answer at all?

        Deliberately checks nothing else. A liveness probe that touches the
        database restarts every replica when the database has a bad minute,
        which converts a degradation into an outage. The only correct response
        to "alive but its dependencies are down" is to stop sending it traffic
        — that is what readiness is for.
        """
        return JSONResponse({"status": "alive"})

    async def health_ready(_: Request) -> Response:
        """Should this process receive traffic?

        Checks the dependencies a request actually needs, cheaply, and reports
        which one failed. Returns 503 so a load balancer removes this replica
        rather than serving errors from it.
        """
        checks: dict[str, str] = {}

        try:
            await state.repository.ping()
            checks["repository"] = "ok"
        except Exception:
            checks["repository"] = "unavailable"

        try:
            await state.queue.stats()
            checks["queue"] = "ok"
        except Exception:
            checks["queue"] = "unavailable"

        try:
            assembly.storage.writable()
            checks["storage"] = "ok"
        except Exception:
            checks["storage"] = "unavailable"

        # A deployment that has not run its migrations is not ready. Serving
        # from a schema the code does not expect is how a partially-applied
        # migration becomes a data-corruption incident instead of a 503.
        #
        # Memoised once it passes. Migrations do not become outstanding again
        # within a process's lifetime — a new version is a new process — and a
        # probe firing every ten seconds across four replicas should not take a
        # write lock on the shared database forever.
        if state.migrations_ready:
            checks["migrations"] = "ok"
        else:
            try:
                from vtv.migrate import migrators

                outstanding = [
                    item.version
                    for migrator in migrators(settings)
                    for item in migrator.pending()
                ]
                checks["migrations"] = "ok" if not outstanding else "pending"
                state.migrations_ready = not outstanding
            except Exception:
                checks["migrations"] = "unavailable"

        ready = all(value == "ok" for value in checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    def _budget_of(body: dict[str, object]) -> float | None:
        """The project's stated budget, or `None` for the deployment's ceiling."""
        raw = body.get("budget_usd")
        if raw is None or raw == "":
            return None
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValidationFailed(
                f"budget_usd must be a number, not {type(raw).__name__}",
                user_message="That budget is not a number.",
            ) from error

    def _spend_settings(project: Project) -> dict[str, Any]:
        """What the user chose about money, and what it buys.

        Returned by `GET`, `PATCH` and nothing else — one shape, so the studio
        cannot show one thing after saving and another after a reload.

        `price_usd` is the part that makes the two settings legible together. A
        budget alone answers nothing: $2 is a hundred and twenty-five pictures
        at draft and eight at fine, and a user who cannot see that is choosing
        between two numbers with no idea what either does. Sending the price of
        one shot at each tier lets the studio say "about 125 pictures" while the
        slider is still moving, without a round trip per keystroke.
        """
        prices = {
            tier.value: round(
                price_of_one_image(
                    assembly.router.providers_for(GenerationKind.IMAGE), tier
                ),
                4,
            )
            for tier in VisualFidelity
        }
        return {
            "project_id": project.project_id,
            # Returned here as well as by `GET /v1/projects/{id}` because this
            # is the response to the rename, and a client that has to make a
            # second request to see what its own write did will eventually show
            # the old name.
            "title": project.title,
            "budget_usd": project.budget_usd,
            "visual_fidelity": (
                project.visual_fidelity.value if project.visual_fidelity else None
            ),
            "price_usd": prices,
        }

    def _fidelity_of(body: dict[str, object]) -> VisualFidelity | None:
        """The project's chosen visual fidelity, or `None` for the deployment's.

        An unknown word is a 400 rather than a silent fallback: "fidelity: hi"
        falling through to the cheapest tier would look exactly like the feature
        not working, and the user would have no way to tell which.
        """
        raw = body.get("visual_fidelity")
        if raw is None or raw == "":
            return None
        try:
            return VisualFidelity(raw)
        except ValueError as error:
            allowed = ", ".join(tier.value for tier in VisualFidelity)
            raise ValidationFailed(
                f"visual_fidelity must be one of {allowed}",
                user_message="That is not a picture quality we offer.",
            ) from error

    async def create_project(request: Request) -> Response:
        principal = guard(request, Capability.PROJECT_CREATE)
        organisation_id = principal.organisation_id
        if organisation_id is None:  # pragma: no cover - guard() forbids this
            raise NotAuthenticated("this request has no organisation")
        body = await _json_body(request)
        style = StyleProfile(
            style=VisualStyle(body.get("style", "explainer")),
            aspect_ratio=AspectRatio(body.get("aspect_ratio", "16:9")),
            direction=body.get("direction"),
        )
        project = Project(
            title=body.get("title"),
            style=style,
            # What this project may spend. Omitted means the deployment's
            # ceiling applies, which is what every project did before this
            # field existed. Validated by the contract — a negative or absurd
            # budget is a 400 here rather than a surprise at render time.
            budget_usd=_budget_of(body),
            # What one shot may cost, which together with the budget decides
            # how many shots are pictures rather than type. Omitted means the
            # deployment's configured tier.
            visual_fidelity=_fidelity_of(body),
            persistence=PersistenceMode(body.get("persistence", "temporary")),
            organisation_id=organisation_id,
            owner_id=principal.subject,
        )
        # P1-7. `Plan.max_retention_days` existed on all five plans and was
        # never read: a free trial and an enterprise contract expired on the
        # same global clock, and a persistent project never expired at all.
        # The plan ceiling now applies to both.
        project.expires_at = expiry_for(
            project,
            tier=assembly.directory.tier_of(organisation_id),
            temporary_hours=settings.temporary_retention_hours,
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
        organisation_id = principal.organisation_id
        if organisation_id is None:  # pragma: no cover - guard() forbids this
            raise NotAuthenticated("this request has no organisation")
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
            organisation_id=organisation_id,
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

        # Transcription is metered separately because it is billed separately:
        # every plan declares a transcribed-minutes allowance roughly double its
        # rendered one, precisely because speech is sent for transcription
        # whether or not a render follows. Nothing recorded it until now, so a
        # tenant could exhaust a 20-minute free allowance without the meter
        # moving. The same estimate serves both — it is the source audio's
        # length, which is the transcription figure exactly and the rendered
        # figure approximately — and both are settled against a measurement in
        # `jobs.py`.
        transcription_quota, transcription_reservation = assembly.usage.reserve(
            organisation_id=organisation_id,
            kind=QuotaKind.TRANSCRIBED_MINUTES,
            quantity=estimate,
        )
        if not transcription_quota.allowed:
            # Give the render hold back rather than leaving it for the TTL: this
            # request is not going to happen, and an hour of a free plan's ten
            # minutes is a large fraction of it.
            if reservation:
                assembly.usage.release(reservation)
            return JSONResponse(
                {
                    "error": {
                        "code": "quota_exceeded",
                        "message": (
                            transcription_quota.reason or "plan allowance exhausted"
                        ),
                    }
                },
                status_code=402,
            )

        # P0-8. The input goes to durable object storage under the tenant's own
        # prefix, and the job carries the key. A local temp path would tie the
        # job to the machine that accepted it, which is exactly what stopped
        # this system from having workers.
        stored = await assembly.storage.put(
            key=tenant_key(
                organisation_id, "projects", project_id, "input", "recording"
            ),
            data=data,
            content_type=verdict.media_type,
            retention=RetentionClass.EPHEMERAL,
        )

        # Development-only: a supplied script lets the aligning transcriber run
        # when no speech-to-text credential is configured. It is refused in
        # production, where a real provider must be present.
        script = form.get("script")
        payload = RenderPayload(
            project_id=project_id,
            organisation_id=organisation_id,
            input_key=stored.key,
            input_content_type=verdict.media_type,
            quality=RenderQuality(str(form.get("quality") or "preview")),
            frame_rate=int(str(form.get("frame_rate") or 24)),
            reservation_id=reservation,
            transcription_reservation_id=transcription_reservation,
            script=str(script) if script and not settings.is_production else None,
        )
        handle = await queue.enqueue(
            kind=JobKind.RENDER_RECORDING.value,
            payload=payload.model_dump(mode="json"),
            idempotency_key=f"render:{project_id}",
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
        organisation_id = principal.organisation_id
        if organisation_id is None:  # pragma: no cover - guard() forbids this
            raise NotAuthenticated("this request has no organisation")
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
            organisation_id=organisation_id,
            kind=QuotaKind.DOCUMENTS,
            requested=1.0,
        )
        if not quota.allowed:
            return JSONResponse(
                {"error": {"code": "quota_exceeded", "message": quota.reason or ""}},
                status_code=402,
            )

        # A document render costs minutes of video too, so it reserves the same
        # allowance a recording does. Estimated from the narration a document
        # of this size produces; settled against the measurement afterwards.
        render_quota, reservation = assembly.usage.reserve(
            organisation_id=organisation_id,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=max(0.5, len(data) / 40_000),
        )
        if not render_quota.allowed:
            return JSONResponse(
                {
                    "error": {
                        "code": "quota_exceeded",
                        "message": render_quota.reason or "plan allowance exhausted",
                    }
                },
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

        stored = await assembly.storage.put(
            key=tenant_key(
                organisation_id, "projects", project_id, "input", "document"
            ),
            data=data,
            content_type=document_verdict.media_type,
            retention=RetentionClass.EPHEMERAL,
        )

        document_payload = RenderPayload(
            project_id=project_id,
            organisation_id=organisation_id,
            input_key=stored.key,
            input_content_type=document_verdict.media_type,
            filename=safe_filename(str(filename)) if filename else None,
            language=str(form.get("language") or "") or None,
            quality=RenderQuality(str(form.get("quality") or "preview")),
            frame_rate=int(str(form.get("frame_rate") or 24)),
            reservation_id=reservation,
        )
        handle = await queue.enqueue(
            kind=JobKind.RENDER_DOCUMENT.value,
            payload=document_payload.model_dump(mode="json"),
            idempotency_key=f"document:{project_id}",
        )
        assembly.usage.record(
            organisation_id=organisation_id,
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

    async def list_projects(request: Request) -> Response:
        """Every project this tenant owns, newest first.

        Scoped by putting the organisation into the query rather than filtering
        after it, the same way `owned_project` is. The repository has taken an
        `organisation_id` since the P0-3 remediation; until now nothing over
        HTTP called it, so a user with fifty projects had no way to see them.
        """
        principal = guard(request, Capability.PROJECT_READ)
        organisation_id = principal.organisation_id
        if organisation_id is None:  # pragma: no cover - guard() forbids this
            raise NotAuthenticated("this request has no organisation")

        try:
            limit = max(1, min(200, int(request.query_params.get("limit", "50"))))
        except ValueError:
            raise ValidationFailed("`limit` must be a number") from None

        projects = await repository.list_projects(
            organisation_id=organisation_id, limit=limit
        )
        return JSONResponse(
            {
                "projects": [
                    {
                        "project_id": item.project_id,
                        "title": item.title,
                        "status": item.status.value,
                        "progress": item.progress,
                        "outcome": item.outcome,
                        # The honest one-line state the list column shows. Not
                        # derived client-side: "ready with warnings" and "ready"
                        # are different facts and only the server knows which.
                        "state": _project_state(item),
                        "duration_seconds": item.rendered_duration_seconds,
                        "current_stage": (
                            item.current_stage.value if item.current_stage else None
                        ),
                        "style": item.style.style.value,
                        "aspect_ratio": item.style.aspect_ratio.value,
                        "cost_usd": item.total_cost_usd,
                        "created_at": _iso(item.created_at),
                        "updated_at": _iso(item.updated_at),
                        "expires_at": _iso(item.expires_at),
                    }
                    for item in projects
                ]
            }
        )

    async def job_status(request: Request) -> Response:
        """What one job is actually doing.

        The frontend polls this for every queued operation. It reports the
        queue's own record — not a guess, not a synthetic percentage. A job the
        queue has never heard of is a 404 rather than an optimistic "pending",
        because a client that polls a fabricated id forever is worse than one
        that is told immediately.
        """
        principal = guard(request, Capability.PROJECT_READ)
        job_id = request.path_params["job_id"]
        try:
            handle = await queue.status(job_id)
        except VTVError:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        # A job carries the project it belongs to; a caller may only see jobs
        # for a project their tenant owns. Without this, a job id — which is
        # returned to whoever enqueued it — would read across tenants.
        if handle.project_id is not None:
            owned = await repository.get_project(
                project_id=handle.project_id,
                organisation_id=principal.organisation_id,
            )
            if owned is None:
                return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        return JSONResponse(
            {
                "job_id": handle.job_id,
                "kind": handle.kind,
                "status": handle.status.value,
                "attempt": handle.attempt,
                "project_id": handle.project_id,
                "error": (
                    {
                        "code": handle.error.code.value,
                        "message": handle.error.user_message
                        or "Something went wrong.",
                        "retryable": handle.error.retryable,
                        # The engineer-facing message, in development only.
                        #
                        # `user_message` is deliberately incapable of naming a
                        # provider, a prompt or a stack frame — that rule is
                        # right and stays. But it meant a developer running this
                        # on their own laptop saw "Something went wrong on our
                        # side." and had nowhere to go: the real exception was
                        # written to `last_error` in the queue database and
                        # shown to nobody. Diagnosing your own bug should not
                        # require a SQL client.
                        #
                        # Gated on `is_production`, so a deployed system still
                        # tells a customer only what a customer should hear.
                        **(
                            {"detail": handle.error.message}
                            if not settings.is_production and handle.error.message
                            else {}
                        ),
                    }
                    if handle.error
                    else None
                ),
            }
        )

    async def render_history(request: Request) -> Response:
        """Every render this project has produced, newest first.

        Read from the document history rather than a separate table: each render
        writes a `render_job` document, and the repository has kept every
        version of every document since the beginning. The history was there;
        nothing had asked for it.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        history = await repository.document_history(
            project_id=project.project_id, kind="render_job", limit=25
        )
        renders: list[dict[str, Any]] = []
        seen: set[str] = set()
        for payload in history:
            try:
                job = RenderJob.model_validate(payload)
            except ValueError:
                continue
            if job.render_job_id in seen:
                continue
            seen.add(job.render_job_id)
            renders.append(
                {
                    "render_job_id": job.render_job_id,
                    "status": job.status.value,
                    "progress": job.progress,
                    "quality": job.settings.quality.value,
                    "frame_rate": job.settings.frame_rate,
                    "duration_seconds": job.duration_seconds,
                    "size_bytes": (
                        job.output.size_bytes if job.output is not None else None
                    ),
                    "has_output": job.output is not None,
                    "has_captions": job.captions_output is not None,
                    "created_at": _iso(job.created_at),
                    "updated_at": _iso(job.updated_at),
                }
            )
        # What the material scored, and who is responsible for it. Sent with
        # the render list because this is the screen a person is on when they
        # decide to publish, and a disclosure they have to go looking for is
        # not a disclosure. `None` when nothing has been sourced yet.
        safety = await repository.get_document(
            project_id=project.project_id, kind="safety_report"
        )
        return JSONResponse(
            {
                "renders": renders,
                # Only the newest render's bytes are addressable: `/video`
                # serves the current one. Said plainly rather than offering
                # download links that would 404.
                "downloadable_render_job_id": (
                    renders[0]["render_job_id"]
                    if renders and renders[0]["has_output"]
                    else None
                ),
                "safety": safety or None,
            }
        )

    async def patch_project(request: Request) -> Response:
        """Change what a project may spend.

        The one project-level setting a user needs to reach after creation, and
        the reason it is a `PATCH` rather than part of the create body alone: a
        budget is a decision people revise once they have seen what a render
        costs, and re-creating the project to change it would throw away the
        script.

        Deliberately narrow. This is not a general project editor — style and
        aspect ratio are decided at creation because changing them invalidates
        every visual already sourced, and a partial update that quietly did
        that would be worse than no endpoint.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await _json_body(request)
        changed: list[str] = []
        if "title" in body:
            # Renaming. A project names itself from its script's opening
            # sentence (`pipeline/naming.py`); this is how the user disagrees
            # with that, and once they have, nothing derives over the top of
            # it. Blank clears the name, which lets a project be re-derived on
            # its next script upload rather than being stuck with a bad guess.
            raw = body.get("title")
            if raw is not None and not isinstance(raw, str):
                raise ValidationFailed(
                    f"title must be text, not {type(raw).__name__}",
                    user_message="A project name has to be text.",
                )
            title = (raw or "").strip()[:200]
            project.title = title or None
            changed.append("title")
        if "budget_usd" in body:
            # Validated by the contract, so a negative or absurd budget is a
            # 400 here rather than a surprise when the render plans against it.
            project.budget_usd = _budget_of(body)
            changed.append("budget")
        if "visual_fidelity" in body:
            project.visual_fidelity = _fidelity_of(body)
            changed.append("fidelity")
        if changed:
            await repository.save_project(project)
            assembly.audit.write(
                action=AuditAction.PROJECT_UPDATED,
                principal=principal,
                target=f"project:{project.project_id}",
                detail={
                    "change": "+".join(changed),
                    "budget_usd": str(project.budget_usd),
                    "visual_fidelity": (
                        project.visual_fidelity.value
                        if project.visual_fidelity
                        else ""
                    ),
                },
                ip_address=_client_ip(request),
            )
        return JSONResponse(_spend_settings(project))

    async def get_project(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        artifacts = await load_artifacts(project)
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
            # The project's real style and shape. Sent because the editor was
            # printing the word "Documentary" for every project regardless —
            # a label that is right by coincidence for some projects and wrong
            # for the rest is worse than no label.
            "style": project.style.style.value,
            "aspect_ratio": project.style.aspect_ratio.value,
            # The spend settings, which `PATCH` writes and nothing read back.
            # Without these the studio's budget field was write-only: a user set
            # a budget, reloaded, and saw an empty box — indistinguishable from
            # the setting having been dropped.
            **_spend_settings(project),
        }
        # P1-4. `outcome` is the honest answer; `status` only says a file
        # exists. A client that ships on `status == ready` alone would publish a
        # mute video believing it succeeded, which is what this distinction
        # exists to prevent.
        payload["outcome"] = project.outcome
        payload["deliverable"] = bool(
            project.outcome
            and RunOutcome(project.outcome).is_deliverable
        )
        if project.degradation_notes:
            payload["degradations"] = list(project.degradation_notes)
        payload["narration"] = {"has_speech": project.narration_has_speech}

        if artifacts.is_rendered:
            payload["video_url"] = f"/v1/projects/{project_id}/video"
            payload["captions_url"] = f"/v1/projects/{project_id}/captions.vtt"
            payload["storyboard_url"] = f"/v1/projects/{project_id}/storyboard"
            assert artifacts.render_job is not None
            payload["duration_seconds"] = artifacts.render_job.duration_seconds
            # Which timeline the file on disk actually encodes. The editor
            # compares this against the timeline it is showing to decide
            # whether the player is history. `None` for a render that predates
            # this field, and the editor treats that as "cannot tell" rather
            # than as "stale" — an unfounded warning is worse than none.
            payload["rendered_timeline_version"] = project.rendered_timeline_version
        if artifacts.source_document is not None:
            document = artifacts.source_document
            payload["source"] = {
                "kind": document.kind.value,
                "origin": document.origin,
                "parser": document.parser,
                "blocks": len(document.blocks),
                "extraction_confidence": document.extraction_confidence,
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
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        result = await load_artifacts(project)
        if result.scene_graph is None:
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
                "thesis": result.scene_graph.narrative.thesis,
                "outcome": project.outcome,
                "strategy_mix": (
                    result.visual_plan.strategy_mix() if result.visual_plan else {}
                ),
                "cost_usd": project.total_cost_usd,
                # P1-1. What is held constant across this project, and what the
                # system noticed going wrong. Exposed because the Visual Bible is
                # only useful as a *control*: a user who cannot see what is
                # locked cannot decide to change it.
                "consistency": {
                    "bindings": [
                        {
                            "name": binding.canonical_name,
                            "kind": binding.kind.value,
                            "colour": binding.colour,
                            "locked": binding.is_locked,
                            "scenes": len(binding.used_in_scenes),
                        }
                        for binding in (
                            result.visual_bible.bindings if result.visual_bible else []
                        )
                    ],
                    "palette": (
                        result.timeline.entity_colours if result.timeline else {}
                    ),
                    "issues": [
                        {
                            "entity": issue.entity_name,
                            "problem": issue.problem,
                            "severity": issue.severity,
                        }
                        for issue in (
                            result.visual_bible.issues if result.visual_bible else []
                        )
                    ],
                },
                "scenes": scenes,
            }
        )

    async def thumbnail(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await owned_project(request, principal)
        if project is None:
            return PlainTextResponse("not found", status_code=404)
        scene_id = request.path_params["scene_id"]
        result = await load_artifacts(project)
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
        project = await owned_project(request, principal)
        if project is None:
            return PlainTextResponse("not found", status_code=404)
        artifacts = await load_artifacts(project)
        if not artifacts.is_rendered:
            return PlainTextResponse("not found", status_code=404)
        assert artifacts.render_job is not None and artifacts.render_job.output is not None
        path = assembly.storage.path_for(artifacts.render_job.output)
        if not path.exists():
            return PlainTextResponse("expired", status_code=410)
        return FileResponse(path, media_type="video/mp4", filename=f"{project_id}.mp4")

    async def captions(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await owned_project(request, principal)
        if project is None:
            return PlainTextResponse("not found", status_code=404)
        artifacts = await load_artifacts(project)
        if artifacts.timeline is not None:
            return PlainTextResponse(
                to_vtt(artifacts.timeline.captions), media_type="text/vtt"
            )

        # A project authored as a script rather than spoken has no `timeline`
        # document — the editing lane keeps its own under `edit_timeline` — so
        # this returned 404 for every project made the way the Studio makes
        # them, while the render history beside it advertised
        # `has_captions: true`. The render *did* produce captions; only this
        # endpoint could not find them. Serve the ones baked into the render the
        # customer downloaded, which are in any case the captions that match it.
        job = artifacts.render_job
        if job is not None and job.captions_output is not None:
            path = assembly.storage.path_for(job.captions_output)
            if not path.exists():
                return PlainTextResponse("expired", status_code=410)
            return PlainTextResponse(
                path.read_text(encoding="utf-8"), media_type="text/vtt"
            )
        return PlainTextResponse("not found", status_code=404)

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
        project = await owned_project(request, principal)
        if project is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        scene_id = request.path_params["scene_id"]
        artifacts = await load_artifacts(project)
        visual_plan = artifacts.visual_plan
        if visual_plan is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        body = await _json_body(request)
        plan = visual_plan.plan_for(scene_id)
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

        # A new version rather than a mutation: `put_document` appends, so the
        # plan the video was rendered from is still readable afterwards. Two
        # concurrent edits therefore produce two versions rather than a torn
        # one, and the latest wins on read.
        revised = visual_plan.model_copy(
            update={
                "scene_plans": [
                    updated if item.scene_id == scene_id else item
                    for item in visual_plan.scene_plans
                ]
            }
        )
        await repository.put_document(
            project_id=project_id,
            kind="visual_plan",
            document_id=revised.visual_plan_id,
            payload=json.loads(revised.model_dump_json()),
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

    async def whoami(request: Request) -> Response:
        """Who this request is, and what it is allowed to do.

        The interface needs this to stop offering actions the caller cannot
        perform. It was offering them: the Studio drew a Delete link beside
        every project, a key minted with the default `service` role does not
        hold `project:delete`, and the server correctly answered 403 every
        single time. The server was right and the button should not have been
        there — "never trust the frontend for authorization" is about what the
        server *enforces*, and says nothing about the frontend advertising work
        it cannot do.

        Requires no capability beyond being authenticated, because a principal
        asking what it may do is not itself a privileged question, and gating it
        would leave exactly the callers who need the answer unable to get it.
        """
        principal = authenticate(request)
        return JSONResponse(
            {
                "subject": principal.subject,
                "kind": principal.kind.value,
                "organisation_id": principal.organisation_id,
                "role": principal.role.value if principal.role else None,
                "capabilities": sorted(
                    capability.value for capability in principal.capabilities
                ),
            }
        )

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
        # `enforced` and `note` per quota deliberately stay. They are the
        # opposite of margin: not something we know and they do not, but
        # something we knew and were not saying. Four of these quotas were
        # rendered here as `used: 0` beside a real limit while nothing counted
        # them, which reads as headroom rather than as an unmeasured number.
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
        # Keys are a *level*: revoking one has to give the allowance back, so
        # the count comes from the directory rather than from a period-scoped
        # sum of creation events. This used to call `usage.check()` and then
        # `del` the verdict — a line that read like a check and enforced
        # nothing — beside a hand-rolled comparison that did the real work.
        # `check_level` is that comparison with somewhere to live.
        quota = assembly.usage.check_level(
            organisation_id=organisation_id,
            kind=QuotaKind.API_KEYS,
            current=len(existing),
        )
        if not quota.allowed:
            return JSONResponse(
                {
                    "error": {
                        "code": "quota_exceeded",
                        "message": (
                            quota.reason
                            or f"your plan allows {quota.limit:g} active keys"
                        ),
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

    async def media_upload(request: Request) -> Response:
        """Receive an object at a signed, single-purpose upload URL.

        The counterpart to `media`, and it exists because a device has no
        storage credentials: `signed_upload_url` mints a URL that reaches
        exactly one key, expires, and carries a byte ceiling, and this is what
        honours it.

        Every limit in that URL is enforced here rather than trusted:

        * the signature, before anything else is read;
        * the bucket and key from the *claims*, never from the path — a request
          whose path disagrees with what was signed is refused rather than
          reconciled;
        * the byte ceiling, while streaming, so an oversized body is cut off
          rather than written and then measured;
        * and the write goes to a `.part` first, so a connection that drops
          half way leaves nothing that looks like a finished render.
        """
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

        ref = ObjectRef(
            bucket=bucket,
            key=key,
            content_type=request.headers.get("content-type", "application/octet-stream"),
        )
        target = assembly.storage.path_for(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        ceiling = claims.get("m")
        limit = int(ceiling) if ceiling is not None else None

        partial = target.with_suffix(target.suffix + ".part")
        written = 0
        try:
            with partial.open("wb") as handle:
                async for chunk in request.stream():
                    written += len(chunk)
                    if limit is not None and written > limit:
                        # Cut off mid-stream rather than written and then
                        # measured: the point of a ceiling is that the bytes
                        # never reach the disk.
                        handle.close()
                        partial.unlink(missing_ok=True)
                        return PlainTextResponse("too large", status_code=413)
                    handle.write(chunk)
            os.replace(partial, target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        # How long it may live, taken from the signed token rather than decided
        # here. Every other write reaches storage through `put`, which requires
        # a retention class; this is the one path where bytes arrive without
        # passing through it, and it was landing them on disk with none — which
        # means the sweep would never delete them. Every video a customer's own
        # computer rendered was being kept forever by a system whose stated
        # design is that it does not become a storage company.
        retention = claims.get("r")
        if retention is not None:
            from vtv.contracts.base import RetentionClass

            with contextlib.suppress(Exception):
                await assembly.storage.classify(key, RetentionClass(str(retention)))
        return JSONResponse({"bytes": written, "key": key}, status_code=201)

    async def sweep(request: Request) -> Response:
        """Retention sweep for **one tenant**, by an authorised caller.

        This route previously had no `guard()` at all and deleted projects and
        storage objects across every tenant. "Internal" is not an access
        control: the HTTP boundary is hostile whatever the path prefix says.

        It now requires `PROJECT_DELETE`, operates only inside the caller's own
        organisation, and is audited. The scheduled cross-tenant retention job
        runs in the worker under a system principal, where it belongs, and is
        not reachable over HTTP at all.
        """
        principal = guard(request, Capability.PROJECT_DELETE, cost=10.0)
        organisation_id = principal.organisation_id or ""

        reports = await retention.sweep(organisation_id=organisation_id)
        orphans = await retention.sweep_orphans(
            organisation_id=organisation_id,
            older_than_seconds=settings.temporary_retention_hours * 3600,
        )
        objects = sum(report.objects_deleted for report in reports) + len(orphans)

        assembly.audit.write(
            action=AuditAction.DATA_DELETED,
            principal=principal,
            target=f"organisation:{organisation_id}",
            detail={
                "projects_deleted": str(len(reports)),
                "objects_deleted": str(objects),
                "trigger": "api",
            },
            ip_address=_client_ip(request),
        )
        return JSONResponse(
            {"projects_deleted": len(reports), "objects_deleted": objects}
        )

    async def delete_project(request: Request) -> Response:
        """Delete one project — records **and** bytes.

        P1-7. There was no such route at all: a customer could create a project
        and had no way to remove it, which makes "delete my data" a support
        ticket. Deletion is storage-first (see `vtv.retention`), scoped to the
        caller's own tenant in both halves, idempotent, and audited.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request,
            Capability.PROJECT_DELETE,
            resource=f"project:{project_id}",
            cost=2.0,
        )
        organisation_id = principal.organisation_id
        if organisation_id is None:  # pragma: no cover - guard() forbids this
            raise NotAuthenticated("this request has no organisation")

        report = await retention.delete_project(
            organisation_id=organisation_id, project_id=project_id
        )
        assembly.audit.write(
            action=AuditAction.PROJECT_DELETED,
            principal=principal,
            target=f"project:{project_id}",
            detail={
                "objects_deleted": str(report.objects_deleted),
                "already_absent": str(report.already_absent).lower(),
            },
            ip_address=_client_ip(request),
        )
        # 200 with a body rather than 204: the caller needs to know whether
        # anything was actually removed, and a bare 204 cannot say.
        return JSONResponse(
            {
                "project_id": project_id,
                "deleted": report.records_deleted > 0,
                "objects_deleted": report.objects_deleted,
                "already_absent": report.already_absent,
            }
        )

    # One pool, shared. The device routes ask it for work and the render
    # button asks it where a render should go; two instances would be two
    # answers to "is one of my computers available", from two modules, in
    # the same request.
    pool = DevicePool(
        queue=queue,
        repository=repository,
        storage=assembly.storage,
        directory=assembly.directory,
    )

    routes = [
        Route("/", index),
        Route("/static/{name}", static_file),
        Route("/health", health),
        # Three endpoints, three questions. `/health` is the human-facing
        # capability report; the other two are for orchestrators and must not
        # be merged, because "restart me" and "stop routing to me" are
        # different remedies for different failures.
        Route("/health/live", health_live),
        Route("/health/ready", health_ready),
        Route("/metrics", metrics_endpoint),
        Route("/v1/projects", list_projects),
        Route("/v1/projects", create_project, methods=["POST"]),
        Route("/v1/projects/{project_id}", get_project),
        Route("/v1/projects/{project_id}", patch_project, methods=["PATCH"]),
        Route(
            "/v1/projects/{project_id}", delete_project, methods=["DELETE"]
        ),
        Route("/v1/projects/{project_id}/recordings", upload_recording, methods=["POST"]),
        Route("/v1/projects/{project_id}/documents", upload_document, methods=["POST"]),
        Route("/v1/projects/{project_id}/storyboard", storyboard),
        Route("/v1/projects/{project_id}/scenes/{scene_id}/thumbnail.png", thumbnail),
        Route("/v1/projects/{project_id}/scenes/{scene_id}/revise", revise_scene, methods=["POST"]),
        Route("/v1/projects/{project_id}/video", video),
        Route("/v1/projects/{project_id}/captions.vtt", captions),
        Route("/v1/projects/{project_id}/events", events_stream),
        Route("/v1/projects/{project_id}/renders", render_history),
        Route("/v1/jobs/{job_id}", job_status),
        Route("/v1/visualize", visualize, methods=["POST"]),
        Route("/v1/me", whoami),
        Route("/v1/usage", usage),
        Route("/v1/audit", audit_log),
        Route("/v1/api-keys", list_api_keys),
        Route("/v1/api-keys", create_api_key, methods=["POST"]),
        Route("/v1/api-keys/{api_key_id}", revoke_api_key, methods=["DELETE"]),
        # Order matters: the upload route is more specific and must be matched
        # before the download route's `{bucket}` swallows the literal "upload".
        Route("/media/upload/{bucket}/{key:path}", media_upload, methods=["PUT"]),
        Route("/media/{bucket}/{key:path}", media),
        # Not "/internal/…". A path prefix is not an access control, and naming
        # it as a tenant operation stops anyone reading it as one.
        Route("/v1/retention/sweep", sweep, methods=["POST"]),
        # The product surface — script, visual units, timeline, preview, scoped
        # render. A separate module, but not a separate application: it is
        # handed this app's `guard`, `owned_project`, repository and queue, so
        # there is exactly one authentication path and one tenancy boundary.
        *product.routes(
            pool=pool,
            guard=guard,
            owned_project=owned_project,
            repository=repository,
            assembly=assembly,
            queue=queue,
            client_ip=_client_ip,
            json_body=_json_body,
        ),
        # Local execution — a customer's own computers, pairing and claiming
        # work. Mounted the same way, and it gets its own `pool` because that is
        # the only object allowed to turn a queued job into something a device
        # may see: signed URLs for exactly this job's assets, and nothing else.
        *devices_api.routes(
            guard=guard,
            owned_project=owned_project,
            json_body=_json_body,
            client_ip=_client_ip,
            assembly=assembly,
            pool=pool,
        ),
        # The media surface — the user's own files. Mounted the same way and
        # for the same reason: a second module, not a second application.
        *media_api.routes(
            guard=guard,
            owned_project=owned_project,
            repository=repository,
            assembly=assembly,
            client_ip=_client_ip,
            json_body=_json_body,
        ),
        # Last, and deliberately so. This matches anything the API did not,
        # which is exactly what a client-side router needs and exactly what
        # would shadow every endpoint above if it came first.
        #
        # Every method, not just GET. Starlette answers a path that matches a
        # route but not its methods with 405, so a GET-only fallback turned
        # `POST /internal/retention/sweep` — a route that was *removed* because
        # it was unauthenticated — into "method not allowed", which reads to a
        # scanner as "this endpoint exists, find the right verb". A deleted
        # endpoint must answer 404, so the handler decides rather than the
        # router.
        Route(
            "/{path:path}",
            web_asset,
            methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        ),
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
            # Outermost, so a request refused by CORS is still counted and a
            # trace exists for a failure that happens inside CORS itself.
            Middleware(ObservabilityMiddleware),
            Middleware(
                CORSMiddleware,
                allow_origins=settings.allowed_origins,
                # DELETE is here because `/v1/api-keys/{id}` accepts it. A
                # preflight that omits a method the API serves fails in a
                # browser and nowhere else, which is the worst place to find it.
                allow_methods=["GET", "POST", "DELETE"],
                allow_headers=["*"],
            )
        ],
    )
    app.state.vtv = state
    return app


# ---------------------------------------------------------------------------
# The deployed ASGI object
# ---------------------------------------------------------------------------

class _LazyApplication:
    """`uvicorn vtv.api.app:application`, built on the first request.

    Deferred rather than constructed at import time for two reasons. Building
    the assembly opens databases and creates directories, and doing that as an
    import side effect means `python -m vtv.migrate`, `python -m vtv.worker` and
    every test that merely imports this module would pay for — and create — an
    API's worth of state.

    The second reason matters more in production: an exception during wiring
    raised at import time is reported by uvicorn as a failure to load the
    application, with no server to answer the health probe that would say so.
    Raised on first request, it is a 500 with a logged cause, and the liveness
    probe still answers.
    """

    def __init__(self) -> None:
        self._app: Starlette | None = None

    def _resolve(self) -> Starlette:
        if self._app is None:
            self._app = create_app()
        return self._app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._resolve()(scope, receive, send)


#: The production entrypoint. Referenced by the Dockerfile's CMD and by
#: `deploy/docker-compose.yml`; both are checked by `tests/test_deployment.py`
#: so the container cannot start referring to a name that no longer exists.
application = _LazyApplication()


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


#: Largest JSON request body any endpoint accepts. Generous for a script, a
#: batch of timeline operations or a settings change, and small enough that a
#: client cannot use a JSON field as free object storage.
#:
#: Without a cap here, `PATCH /timeline` would take an arbitrarily large
#: animation `spec` and persist it into the relational store — the one place
#: this system deliberately keeps blobs out of.
MAX_JSON_BODY_BYTES = 1_000_000


async def _json_body(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_JSON_BODY_BYTES:
        raise ValidationFailed(
            f"request body is larger than {MAX_JSON_BODY_BYTES} bytes",
            user_message="That request is too large.",
        )
    raw = await request.body()
    if len(raw) > MAX_JSON_BODY_BYTES:
        # Checked again after reading: `Content-Length` is a claim, and a
        # chunked request does not carry one at all.
        raise ValidationFailed(
            f"request body is larger than {MAX_JSON_BODY_BYTES} bytes",
            user_message="That request is too large.",
        )
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _project_state(project: Project) -> str:
    """The one line a project list shows, and it must not flatter.

    `status` alone says a file exists. `outcome` says whether it is worth
    watching, and the two disagree in exactly the case that matters: a render
    that finished with three failed visuals is `READY` and is not *ready*.
    Deriving this server-side means the list, the card and the export banner
    cannot each decide differently.
    """
    if project.status is Status.FAILED:
        return "Render failed"
    if project.status is Status.PROCESSING:
        stage: str = (
            str(project.current_stage.value).replace("_", " ")
            if project.current_stage
            else "working"
        )
        return stage[:1].upper() + stage[1:]
    if project.status is not Status.READY:
        return str(project.status.value).replace("_", " ").capitalize()

    outcome = project.outcome
    if outcome and outcome != RunOutcome.SUCCESS.value:
        if RunOutcome(outcome).is_deliverable:
            return "Ready with warnings"
        return "Failed"
    return "Ready"


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


__all__ = ["MAX_UPLOAD_BYTES", "WEB_DIST", "WEB_ROOT", "ApiState", "create_app"]
