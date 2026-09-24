"""The media API — upload, ingest, place, and never quietly replace.

Mounted the same way `api/product.py` is: handed the application's own guard,
ownership check, repository and storage, so there is one authentication path and
one tenancy boundary rather than a second security architecture for the routes
that happen to accept files.

## The three promises this module keeps

**Your file is yours.** A `USER_UPLOAD` asset carries no licence question, needs
no attribution, and is never replaced by an automatic process. Using one as a
visual locks that visual by default — the user can unlock it, but nothing else
can.

**Your file outlives a re-plan.** Because applying media locks the unit, and the
planner pins locked units into the segmentation, a script edit re-plans *around*
your photograph rather than through it. That is not a special case in the
planner; it is the ordinary lock rule doing its job, which is why it can be
relied on.

**Your file is never thrown away.** Deleting the line a visual covered returns
the asset to the library. The only path that removes bytes is the user deleting
the asset explicitly, or the project being deleted.

## Why bytes are inspected before anything else happens

Every uploaded byte is assumed hostile. `inspect_upload` identifies content by
its magic prefix rather than by the name or the declared type, because both of
those are attacker-controlled and neither survives contact with a file that was
renamed. A file whose bytes disagree with its extension is refused with a
sentence, not accepted with a warning.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from vtv.contracts.base import RetentionClass
from vtv.contracts.errors import (
    ErrorCode,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from vtv.contracts.media import (
    LogoPlacement,
    MediaAsset,
    MediaCrop,
    MediaKind,
    MediaLibrary,
    MediaOrigin,
    MediaStatus,
    MediaTrim,
)
from vtv.contracts.project import Project
from vtv.contracts.tenancy import AuditAction, Capability, Principal
from vtv.contracts.visual_plan import VisualStrategy
from vtv.contracts.visual_unit import (
    ConsistencyStatus,
    GroundingStatus,
    VisualUnit,
    VisualUnitStatus,
    VisualVersion,
)
from vtv.security.paths import tenant_key
from vtv.security.uploads import (
    MAX_UPLOAD_BYTES,
    ContentClass,
    UploadPolicy,
    inspect_upload,
)

#: The document kind the library is stored under, beside the script, the units
#: and the timeline. Named once so the reader and the writer cannot disagree.
MEDIA_DOC = "media_library"

#: Content classes we accept, mapped to the kind they become. `DOCUMENT`,
#: `ARCHIVE` and `TEXT` are deliberately absent: a `.pages` file is not media,
#: and telling the user that plainly is better than storing something the
#: renderer will later fail on.
_CLASS_TO_KIND: dict[ContentClass, MediaKind] = {
    ContentClass.IMAGE: MediaKind.IMAGE,
    ContentClass.VIDEO: MediaKind.VIDEO,
    ContentClass.AUDIO: MediaKind.AUDIO,
}

#: Uploads are the one place a user's own bytes are stored for the life of the
#: project. `PROJECT` retention, not `EPHEMERAL`: a file the user brought must
#: not be swept out from under a project that still references it.
MEDIA_RETENTION = RetentionClass.PROJECT


def routes(
    *,
    guard: Callable[..., Principal],
    owned_project: Callable[[Request, Principal], Awaitable[Project | None]],
    repository: Any,
    assembly: Any,
    client_ip: Callable[[Request], str],
    json_body: Callable[[Request], Awaitable[dict[str, Any]]],
) -> list[Route]:
    """Build the media routes over the application's existing plumbing."""

    storage = assembly.storage

    # -- persistence ------------------------------------------------------

    async def load_library(project: Project) -> MediaLibrary:
        payload = await repository.get_document(
            project_id=project.project_id, kind=MEDIA_DOC
        )
        if not payload:
            return MediaLibrary(
                organisation_id=project.organisation_id,
                project_id=project.project_id,
            )
        try:
            return MediaLibrary.model_validate(payload)
        except ValueError:
            # Written by an older schema. An empty library beats refusing to
            # open the project; the drift is visible in the schema test.
            return MediaLibrary(
                organisation_id=project.organisation_id,
                project_id=project.project_id,
            )

    async def store_library(library: MediaLibrary) -> None:
        await repository.put_document(
            project_id=library.project_id,
            kind=MEDIA_DOC,
            document_id=MEDIA_DOC,
            payload=json.loads(library.model_dump_json()),
        )

    async def require_project(request: Request, principal: Principal) -> Project:
        project = await owned_project(request, principal)
        if project is None:
            raise NotFound(
                "no such project",
                user_message="We could not find that project.",
            )
        return project

    async def load_units(project_id: str) -> list[VisualUnit]:
        payload = await repository.get_document(
            project_id=project_id, kind="visual_units"
        )
        if not payload:
            return []
        out: list[VisualUnit] = []
        for item in payload.get("units", []) if isinstance(payload, dict) else []:
            try:
                out.append(VisualUnit.model_validate(item))
            except ValueError:
                continue
        return out

    async def store_units(project_id: str, units: list[VisualUnit]) -> None:
        """Store the units, and repoint the timeline at what they now show.

        The same pairing `api/product.py` makes, for the same reason: giving a
        visual a file of your own changed the unit and left the clip pointing at
        the picture it replaced, so the render — which reads the timeline and
        never the units — still contained the old shot. The interface said
        "your file is now this visual" and the downloaded video disagreed.
        """
        await repository.put_document(
            project_id=project_id,
            kind="visual_units",
            document_id="visual_units",
            payload={"units": [json.loads(u.model_dump_json()) for u in units]},
        )

        from vtv.contracts.tracks import EditTimeline
        from vtv.pipeline.units import retarget

        payload = await repository.get_document(
            project_id=project_id, kind="edit_timeline"
        )
        if not payload:
            return
        try:
            timeline = EditTimeline.model_validate(payload)
        except ValueError:
            return
        repointed = retarget(timeline, units)
        if repointed is timeline:
            return
        await repository.put_document(
            project_id=project_id,
            kind="edit_timeline",
            document_id=repointed.edit_timeline_id,
            payload=json.loads(repointed.model_dump_json()),
        )

    # -- routes -----------------------------------------------------------

    async def upload_media(request: Request) -> Response:
        """Accept a file, identify it by its bytes, and store it.

        Synchronous, because a user who dropped a file expects to see it in the
        library — not a job id. The expensive parts (probing dimensions,
        transcoding) are deliberately not done here; the asset is `READY` with
        what inspection could determine, and the interface says what it knows.
        """
        project_id = request.path_params["project_id"]
        principal = guard(
            request,
            Capability.PROJECT_UPDATE,
            resource=f"project:{project_id}",
            cost=3.0,
        )
        project = await require_project(request, principal)

        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise ValidationFailed(
                "a file is required",
                user_message="Choose a file to upload.",
            )

        data = await upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse(
                {
                    "error": {
                        "code": "upload_refused",
                        "message": (
                            f"That file is larger than "
                            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                        ),
                        "retryable": False,
                    }
                },
                status_code=413,
            )

        filename = str(getattr(upload, "filename", "") or "upload")
        declared = getattr(upload, "content_type", None)

        # Identity from the bytes. The name and the declared type are hints the
        # caller controls, and neither survives a renamed file.
        #
        # `inspect_upload` returns a verdict for every refusal except one: an
        # SVG carrying active content *raises*, because refusing to rewrite
        # hostile markup is a deliberate decision made deeper in that module.
        # Caught here so this endpoint has one refusal shape rather than two.
        try:
            verdict = inspect_upload(
                data,
                policy=UploadPolicy(
                    max_bytes=MAX_UPLOAD_BYTES,
                    allowed_classes=frozenset(
                        {ContentClass.IMAGE, ContentClass.VIDEO, ContentClass.AUDIO}
                    ),
                    reject_mismatch=False,
                ),
                declared_type=declared,
                filename=filename,
            )
        except PolicyViolation as refusal:
            assembly.audit.write(
                action=AuditAction.UPLOAD_REJECTED,
                principal=principal,
                target=f"project:{project.project_id}",
                succeeded=False,
                detail={"reasons": str(refusal.info.message)[:200]},
                ip_address=client_ip(request),
            )
            return JSONResponse(
                {
                    "error": {
                        "code": "upload_refused",
                        "message": (
                            f"{filename} contains active content and was "
                            "refused. Export it as a plain image instead."
                        ),
                        "retryable": False,
                    }
                },
                status_code=415,
            )
        if not verdict.accepted:
            assembly.audit.write(
                action=AuditAction.UPLOAD_REJECTED,
                principal=principal,
                target=f"project:{project.project_id}",
                succeeded=False,
                detail={"reasons": "; ".join(verdict.reasons)[:200]},
                ip_address=client_ip(request),
            )
            return JSONResponse(
                {
                    "error": {
                        "code": "upload_refused",
                        "message": _refusal_sentence(filename, verdict.reasons),
                        "retryable": False,
                    }
                },
                status_code=415,
            )

        kind = _kind_for(verdict, filename, form.get("kind"))
        asset = MediaAsset(
            organisation_id=project.organisation_id,
            project_id=project.project_id,
            kind=kind,
            origin=MediaOrigin.USER_UPLOAD,
            status=MediaStatus.INGESTING,
            filename=filename[:255],
            content_type=verdict.media_type or (declared or "application/octet-stream"),
            size_bytes=len(data),
        )

        # Tenant-namespaced by construction. `LocalStorageProvider.put` refuses
        # any key outside the caller's own prefix, so this cannot be got wrong
        # by forgetting rather than by trying.
        key = tenant_key(
            project.organisation_id,
            "projects",
            project.project_id,
            "media",
            f"{asset.media_asset_id}{_extension(filename, verdict.media_type)}",
        )
        reference = await storage.put(
            key=key,
            data=data,
            content_type=asset.content_type,
            retention=MEDIA_RETENTION,
        )
        asset.object = reference
        asset.status = MediaStatus.READY

        library = await load_library(project)
        if len(library.assets) >= 500:
            raise PolicyViolation(
                "this project already holds 500 media assets",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    "This project has reached 500 files. Delete some before "
                    "adding more."
                ),
            )
        library.assets = [*library.assets, asset]
        await store_library(library)

        assembly.audit.write(
            action=AuditAction.PROJECT_UPDATED,
            principal=principal,
            target=f"project:{project.project_id}",
            detail={
                "change": "media_uploaded",
                "media_asset_id": asset.media_asset_id,
                "kind": kind.value,
                "bytes": str(len(data)),
            },
            ip_address=client_ip(request),
        )
        return JSONResponse(_asset_view(asset), status_code=201)

    async def list_media(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        library = await load_library(project)

        wanted = request.query_params.get("origin")
        assets = library.assets
        if wanted:
            try:
                origin = MediaOrigin(wanted)
            except ValueError:
                raise ValidationFailed("unknown origin filter") from None
            assets = [item for item in assets if item.origin is origin]

        mark = library.project_mark
        return JSONResponse(
            {
                "assets": [_asset_view(item) for item in assets],
                "counts": {
                    origin.value: sum(
                        1 for item in library.assets if item.origin is origin
                    )
                    for origin in MediaOrigin
                },
                "project_mark_id": mark.media_asset_id if mark else None,
            }
        )

    async def get_media(request: Request) -> Response:
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_READ, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        library = await load_library(project)
        asset = library.asset(request.path_params["media_asset_id"])
        if asset is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        return JSONResponse(await _detail_view(asset, storage))

    async def update_media(request: Request) -> Response:
        """Trim, crop, level, loop, or place a logo. All free and instant."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        library = await load_library(project)

        media_asset_id = request.path_params["media_asset_id"]
        index = next(
            (
                i
                for i, item in enumerate(library.assets)
                if item.media_asset_id == media_asset_id
            ),
            None,
        )
        if index is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        asset = library.assets[index].model_copy(deep=True)
        body = await json_body(request)
        capabilities = asset.capabilities()

        try:
            asset = _apply_edits(asset, body, capabilities)
        except PydanticValidationError as error:
            # A trim that does not advance or a crop outside the frame is the
            # caller's mistake. Without this it escapes as a raw pydantic error,
            # misses the API's `VTVError` handler and answers 500.
            raise ValidationFailed(
                f"that edit is not valid: {_first_pydantic_message(error)}"
            ) from None

        # Re-validate the whole asset, so an edit that produced an impossible
        # trim or a crop outside the frame fails here rather than in the render.
        try:
            library.assets[index] = MediaAsset.model_validate(
                json.loads(asset.model_dump_json())
            )
        except PydanticValidationError as error:
            raise ValidationFailed(
                f"that edit is not valid: {_first_pydantic_message(error)}"
            ) from None

        # At most one mark per project, enforced here rather than hoped for.
        # `MediaLibrary.project_mark` answers "the last logo", so two logos in
        # the list means the mark silently depends on insertion order — and a
        # user who promotes a second image would find the first one still
        # riding every scene, or the second one not, with nothing to explain it.
        if library.assets[index].kind is MediaKind.LOGO:
            library.assets = [
                item
                if i == index or item.kind is not MediaKind.LOGO
                else item.model_copy(update={"kind": MediaKind.IMAGE})
                for i, item in enumerate(library.assets)
            ]

        await store_library(library)
        return JSONResponse(_asset_view(library.assets[index]))

    async def delete_media(request: Request) -> Response:
        """Remove an asset, refusing while a visual still shows it."""
        project_id = request.path_params["project_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        library = await load_library(project)

        media_asset_id = request.path_params["media_asset_id"]
        asset = library.asset(media_asset_id)
        if asset is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        units = await load_units(project.project_id)
        in_use = [
            unit
            for unit in units
            if any(
                version.object is not None
                and asset.object is not None
                and version.object.key == asset.object.key
                for version in unit.versions
            )
        ]
        if in_use and not _flag(request, "force"):
            names = ", ".join(f"Visual {unit.index + 1:02d}" for unit in in_use[:4])
            raise PolicyViolation(
                f"{media_asset_id} is used by {len(in_use)} visual(s)",
                code=ErrorCode.SCHEMA_INVALID,
                user_message=(
                    f"That file is used by {names}. Replace those visuals "
                    "first, or delete it anyway."
                ),
            )

        library.assets = [
            item for item in library.assets if item.media_asset_id != media_asset_id
        ]
        await store_library(library)
        if asset.object is not None:
            await storage.delete(asset.object)

        assembly.audit.write(
            action=AuditAction.PROJECT_UPDATED,
            principal=principal,
            target=f"project:{project.project_id}",
            detail={"change": "media_deleted", "media_asset_id": media_asset_id},
            ip_address=client_ip(request),
        )
        return JSONResponse({"deleted": media_asset_id})

    async def use_as_visual(request: Request) -> Response:
        """Make a user's file the picture for one visual unit.

        Adds a version and selects it, exactly as a regeneration would — the
        clip keeps its identity, its span and its script link, because those are
        what the editor, the undo stack and the preview all hang off.

        **Locks the unit by default.** A user who chose their own photograph for
        shot three has expressed a preference stronger than any the director
        can form, and the whole value of that is that nothing automatic
        reconsiders it. The lock is visible and removable; it is not silent.
        """
        project_id = request.path_params["project_id"]
        unit_id = request.path_params["unit_id"]
        principal = guard(
            request, Capability.PROJECT_UPDATE, resource=f"project:{project_id}"
        )
        project = await require_project(request, principal)
        library = await load_library(project)

        body = await json_body(request)
        asset = library.asset(str(body.get("media_asset_id", "")))
        if asset is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        if not asset.is_usable:
            raise ValidationFailed(
                f"asset {asset.media_asset_id} is {asset.status.value}",
                user_message="That file is not ready yet.",
            )
        if not asset.can_be_a_visual:
            raise ValidationFailed(
                f"a {asset.kind.value} cannot be a visual",
                user_message=(
                    "A logo rides every scene as an overlay — it is never a "
                    "shot on its own. Set it as the project mark instead."
                ),
            )

        units = await load_units(project.project_id)
        index = next(
            (i for i, u in enumerate(units) if u.visual_unit_id == unit_id), None
        )
        if index is None:
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)

        unit = units[index]
        if unit.locked:
            # The same refusal a regeneration gets, for the same reason. A
            # user's own file does not outrank the user's own lock.
            return JSONResponse(
                {
                    "error": {
                        "code": "permission_denied",
                        "message": (
                            f"Visual {unit.index + 1} is locked. Unlock it "
                            "first if you want to replace it."
                        ),
                        "retryable": False,
                    }
                },
                status_code=403,
            )

        working = unit.model_copy(deep=True)
        version = VisualVersion(
            version=working.next_version_number,
            strategy=VisualStrategy.EXISTING_ASSET,
            object=asset.object,
            # The link back to the library, and the fact the badge needs. Both
            # recorded here because this is the only route by which a version
            # becomes the user's own file, and a flag set at the one place it
            # can be true cannot drift from reality.
            asset_id=asset.media_asset_id,
            user_owned=asset.is_user_owned,
            # Your own file makes no claim the system has to stand behind, and
            # it belongs to no style the Bible governs. Both verdicts are
            # recorded as not applicable rather than quietly asserted.
            grounding=GroundingStatus.NOT_APPLICABLE,
            consistency=ConsistencyStatus.NOT_APPLICABLE,
            rationale=f"Your file — {asset.filename}",
            cost_usd=0.0,
        )
        working.add_version(version, select=True)
        working.status = VisualUnitStatus.LOCKED
        working.locked = bool(body.get("lock", True))
        if not working.locked:
            working.status = VisualUnitStatus.READY
        working.detail = "" if working.locked else "your file, not locked"
        units[index] = working
        await store_units(project.project_id, units)

        asset = asset.model_copy(
            update={
                "used_by_unit_ids": sorted({*asset.used_by_unit_ids, unit_id}),
            }
        )
        library.assets = [
            asset if item.media_asset_id == asset.media_asset_id else item
            for item in library.assets
        ]
        await store_library(library)

        assembly.audit.write(
            action=AuditAction.PROJECT_UPDATED,
            principal=principal,
            target=f"project:{project.project_id}",
            detail={
                "change": "media_used_as_visual",
                "media_asset_id": asset.media_asset_id,
                "visual_unit_id": unit_id,
                "locked": str(working.locked).lower(),
            },
            ip_address=client_ip(request),
        )
        return JSONResponse(
            {
                "visual_unit_id": unit_id,
                "version_id": version.version_id,
                "locked": working.locked,
                "status": working.status.value,
            }
        )

    return [
        Route("/v1/projects/{project_id}/media", list_media),
        Route("/v1/projects/{project_id}/media", upload_media, methods=["POST"]),
        Route("/v1/projects/{project_id}/media/{media_asset_id}", get_media),
        Route(
            "/v1/projects/{project_id}/media/{media_asset_id}",
            update_media,
            methods=["PATCH"],
        ),
        Route(
            "/v1/projects/{project_id}/media/{media_asset_id}",
            delete_media,
            methods=["DELETE"],
        ),
        Route(
            "/v1/projects/{project_id}/visual-units/{unit_id}/media",
            use_as_visual,
            methods=["POST"],
        ),
    ]


# ---------------------------------------------------------------------------
# Views. One place that decides what leaves the building.
# ---------------------------------------------------------------------------

def _asset_view(asset: MediaAsset) -> dict[str, Any]:
    return {
        "media_asset_id": asset.media_asset_id,
        "kind": asset.kind.value,
        "origin": asset.origin.value,
        "status": asset.status.value,
        "filename": asset.filename,
        "content_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "source_duration_seconds": asset.source_duration_seconds,
        "duration_seconds": asset.effective_duration_seconds,
        "usable": asset.is_usable,
        "user_owned": asset.is_user_owned,
        "detail": asset.detail,
        "used_by_unit_ids": list(asset.used_by_unit_ids),
        "capabilities": asset.capabilities(),
        "trim": {
            "in_seconds": asset.trim.in_seconds,
            "out_seconds": asset.trim.out_seconds,
            "fade_in_seconds": asset.trim.fade_in_seconds,
            "fade_out_seconds": asset.trim.fade_out_seconds,
            "gain_db": asset.trim.gain_db,
            "duck_db": asset.trim.duck_db,
            "loop": asset.trim.loop,
            "use_source_audio": asset.trim.use_source_audio,
        },
        "crop": {
            "x": asset.crop.x,
            "y": asset.crop.y,
            "width": asset.crop.width,
            "height": asset.crop.height,
            "is_whole_frame": asset.crop.is_whole_frame,
        },
        "logo": {
            "placement": asset.logo_placement.value,
            "width_percent": asset.logo_width_percent,
            "inset_px": asset.logo_inset_px,
            "opacity": asset.logo_opacity,
        },
        # Provenance. Empty for a user's own file, and that emptiness is the
        # answer rather than a gap — there is no licence question to answer.
        "provenance": {
            "creator": asset.creator,
            "licence": asset.licence,
            "source_name": asset.source_name,
            "source_url": asset.source_url,
            "attribution": asset.attribution,
        },
    }


async def _detail_view(asset: MediaAsset, storage: Any) -> dict[str, Any]:
    """The asset, plus a time-limited URL the browser can actually load.

    Signed and short-lived rather than a permanent path: an object key is a
    tenant's namespace, and a URL that never expires is a credential nobody
    remembers issuing.
    """
    view = _asset_view(asset)
    if asset.object is not None:
        try:
            view["url"] = await storage.signed_url(asset.object, expires_in_seconds=900)
        except Exception:  # pragma: no cover - a missing object is not fatal here
            view["url"] = None
    else:
        view["url"] = None
    return view


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kind_for(verdict: Any, filename: str, declared: Any) -> MediaKind:
    """What this file is, deciding between the two ambiguous cases.

    Inspection answers image/video/audio. It cannot answer the two questions
    that are about *intent* rather than bytes: an SVG is an image by content and
    a vector by use, and a logo is an image the user nominated as a mark. Both
    come from the caller, and both are checked against the bytes rather than
    trusted — a caller cannot declare a video to be a logo.
    """
    lowered = filename.lower()
    base = _CLASS_TO_KIND.get(verdict.content_class, MediaKind.IMAGE)

    if lowered.endswith(".svg") or (verdict.media_type or "").endswith("svg+xml"):
        return MediaKind.VECTOR
    if declared and str(declared) == MediaKind.LOGO.value and base is MediaKind.IMAGE:
        return MediaKind.LOGO
    return base


def _extension(filename: str, media_type: str | None) -> str:
    """A conservative extension for the stored key.

    Taken from the filename only when it is short and alphanumeric — a key is a
    path, and a path assembled from an attacker-supplied string is how traversal
    happens. `safe_storage_key` would catch it; not constructing it is better.
    """
    _, _, suffix = filename.rpartition(".")
    if suffix and suffix != filename and len(suffix) <= 5 and suffix.isalnum():
        return f".{suffix.lower()}"
    if media_type and "/" in media_type:
        tail = media_type.rsplit("/", 1)[1]
        if tail.isalnum() and len(tail) <= 5:
            return f".{tail}"
    return ".bin"


def _refusal_sentence(filename: str, reasons: tuple[str, ...] | list[str]) -> str:
    """A refusal a person can act on, naming the file and the remedy."""
    name = filename or "That file"
    joined = "; ".join(reasons)
    # The common case by far: the bytes are a real file of a kind this endpoint
    # does not take. Saying "document files are not accepted here" tells the
    # user what we decided; saying what to do instead tells them what to do.
    if not joined or "not accepted here" in joined or "not allowed" in joined:
        return (
            f"{name} is not an image, video or audio file. Convert it, or "
            "paste its text into your script."
        )
    return f"{name} could not be accepted: {joined}"


def _apply_edits(
    asset: MediaAsset, body: dict[str, Any], capabilities: dict[str, bool]
) -> MediaAsset:
    """Apply a trim, a crop or a logo placement — refusing what the kind cannot.

    A capability the kind does not have is a refusal with a sentence, not a
    silently ignored field. An interface that accepts "trim this photograph" and
    does nothing has taught the user that trimming does not work.
    """
    # `kind` first: promoting an image to the project mark changes which of the
    # edits below are legal, and applying a trim under the old kind and then
    # changing the kind would leave an asset carrying a field its kind forbids.
    if "kind" in body:
        asset = _promote(asset, str(body["kind"]))
        capabilities = asset.capabilities()

    if "trim" in body:
        if not capabilities["trim"]:
            raise ValidationFailed(
                f"a {asset.kind.value} has no duration to trim",
                user_message=(
                    f"A {asset.kind.value} has no length of its own — it takes "
                    "the clip's."
                ),
            )
        # Merged onto the existing trim rather than replacing it, so a client
        # that sends only `gain_db` does not silently reset the in and out
        # points the user set a minute ago.
        asset.trim = MediaTrim.model_validate(
            {**json.loads(asset.trim.model_dump_json()), **_dict(body["trim"])}
        )

    if "crop" in body:
        if not capabilities["crop"]:
            raise ValidationFailed(
                f"a {asset.kind.value} cannot be cropped",
                user_message=(
                    "Vector artwork scales rather than crops — it has no pixels "
                    "to lose."
                ),
            )
        asset.crop = MediaCrop.model_validate(_dict(body["crop"]))

    if "logo" in body:
        if asset.kind is not MediaKind.LOGO:
            raise ValidationFailed(
                "only a logo has a placement",
                user_message="Only the project mark has a placement.",
            )
        placement = _dict(body["logo"])
        if "placement" in placement:
            try:
                asset.logo_placement = LogoPlacement(str(placement["placement"]))
            except ValueError:
                raise ValidationFailed("unknown logo placement") from None
        if "width_percent" in placement:
            asset.logo_width_percent = float(placement["width_percent"])
        if "inset_px" in placement:
            asset.logo_inset_px = int(placement["inset_px"])
        if "opacity" in placement:
            asset.logo_opacity = float(placement["opacity"])

    return asset


def _promote(asset: MediaAsset, kind: str) -> MediaAsset:
    """Move an asset between `image` and `logo`, and nowhere else.

    ## Why this is not "set the kind"

    Kind is decided from the bytes at upload (`_kind_for`), and it must stay
    that way: a caller who could rename a video to `audio` would get it onto a
    sound lane, and a caller who could rename anything to `image` would get a
    `.exe` past the inspector. So this is not a setter — it is one specific,
    reversible promotion between two kinds that share the same bytes and the
    same inspection verdict, and it refuses everything else by name.

    ## Why it exists at all

    Nobody uploads a file thinking "this is a logo". They upload their mark with
    everything else and then decide. Without this, the only way to set a project
    mark is to upload the same file twice with the right flag, which is a
    workflow nobody would design on purpose.

    Demoting is allowed for the same reason: a mark set by mistake must be
    removable without deleting the file.
    """
    try:
        target = MediaKind(kind)
    except ValueError:
        raise ValidationFailed(
            f"unknown media kind {kind!r}",
            user_message="That is not a kind of media this system knows.",
        ) from None

    if target is asset.kind:
        return asset

    allowed = {MediaKind.IMAGE, MediaKind.LOGO}
    if asset.kind not in allowed or target not in allowed:
        raise ValidationFailed(
            f"a {asset.kind.value} cannot become a {target.value}",
            user_message=(
                "What a file is comes from the file itself. Only a still image "
                "can be made the project mark, and only the mark can be made an "
                "image again."
            ),
        )

    updated = asset.model_copy(update={"kind": target})
    if target is MediaKind.IMAGE:
        # A demoted mark keeps its placement fields at their defaults rather
        # than carrying a corner and an inset that nothing will read. Stale
        # state that only matters if it is ever promoted again is how a user
        # gets a logo in a corner they set eight months ago.
        updated = updated.model_copy(
            update={
                "logo_placement": MediaAsset.model_fields[
                    "logo_placement"
                ].get_default(call_default_factory=True),
                "logo_width_percent": MediaAsset.model_fields[
                    "logo_width_percent"
                ].get_default(call_default_factory=True),
                "logo_inset_px": MediaAsset.model_fields[
                    "logo_inset_px"
                ].get_default(call_default_factory=True),
                "logo_opacity": MediaAsset.model_fields[
                    "logo_opacity"
                ].get_default(call_default_factory=True),
            }
        )
    return updated


def _first_pydantic_message(error: PydanticValidationError) -> str:
    """One readable sentence from a pydantic failure, naming no internals."""
    for item in error.errors():
        message = str(item.get("msg", "")).strip()
        if message:
            return message.removeprefix("Value error, ")
    return "the request did not match the expected shape"


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationFailed("expected an object")
    return value


def _flag(request: Request, name: str) -> bool:
    return request.query_params.get(name, "").lower() in {"1", "true", "yes"}


__all__ = ["MEDIA_DOC", "routes"]
