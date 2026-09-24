"""Offering render work to a customer's own computers.

## The one job kind a device may run

`render_timeline`, and only that. Not `render_recording`, which transcribes,
plans visuals, searches for assets and calls three providers before it draws
anything — every one of those needs a credential, and a credential on somebody's
desktop is a credential in a coffee shop.

So the split is: **the server decides what the video should be, the device draws
it.** By the time a job is offered here, every AI call has been made, every asset
has been acquired and paid for, and what is left is a resolved `Timeline` and
some pixels. That is the part worth moving to hardware the customer already owns,
and it is also the only part that is safe to move.

## Why the timeline is not in the queue payload

A queue row is not a blob store — the same reason `RenderPayload` carries a
storage key instead of the audio. A four-hour video's timeline is megabytes of
JSON, and putting it in the row would mean every claim, every retry and every
status poll dragging it through the database. The payload names the project; the
timeline is read from the document store at claim time, once, by the one caller
that needs it.

## Why claiming goes through the queue rather than around it

Because the queue already does the hard parts — atomic claim, heartbeats,
stale-claim recovery — and it does them correctly. A device is a worker that
happens to be on the other side of the internet, so a closed laptop is recovered
by exactly the rule that recovers a killed process. Building a second lease table
here would be building a second thing to get wrong.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from datetime import timedelta
from typing import Any

from vtv.contracts.base import ObjectRef, utc_now
from vtv.contracts.devices import (
    LEASE_TTL_SECONDS,
    AssetHandout,
    Assignment,
    Completion,
    Device,
    DeviceState,
    JobLease,
    ProgressReport,
)
from vtv.contracts.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    NotFound,
    PolicyViolation,
    Status,
)

#: The queue kind a device is allowed to claim. One entry, and the allow-list is
#: the point: a device asks for "work", and this is the only thing that word can
#: mean for it.
DEVICE_KIND = "render_timeline"

#: The same work, drawn in the cloud. `JobKind.RENDER_TIMELINE_CLOUD` is this
#: string — it imports the constant rather than repeating it, because the day
#: the two spellings drift is the day cloud renders are enqueued under a kind no
#: worker claims and sit pending forever, which nothing would report as broken.
CLOUD_KIND = "render_timeline_cloud"


class Target(str, Enum):
    """Where a render runs. The picker's three options collapse to two answers.

    `AUTO` is a *request*, not a destination: it is what the person chose, and
    the server turns it into DEVICE or CLOUD before anything is queued. Nothing
    downstream ever sees AUTO, because a job that is still deciding where it
    runs is a job that two renderers can both believe is theirs.
    """

    AUTO = "auto"
    DEVICE = "device"
    CLOUD = "cloud"


@dataclass(frozen=True)
class Plan:
    """Where a render will run, and the sentence explaining it.

    The reason is not decoration. "Auto" that silently picks the cloud while a
    customer's expensive graphics card sits idle is a feature nobody trusts
    twice; the honest version says which it chose and why, every time, and the
    editor can show it.
    """

    target: Target
    reason: str
    kind: str

#: How long a signed asset URL lives. Long enough for a slow domestic connection
#: to pull a few hundred megabytes, short enough that a URL captured from a log
#: is worthless by the time anybody reads it.
ASSET_URL_SECONDS = 3600

#: Ceiling on an uploaded render, so a signed upload URL cannot be used to push
#: arbitrary amounts of data into a customer's bucket.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024

#: How long a render waits for the computer it was sent to before the cloud
#: takes it instead.
#:
#: Longer than a lease, so this never races the ordinary recovery of a
#: machine that took the job and went quiet — that is already handled, and
#: handled better, by the queue. This is for the different case where
#: *nobody ever claimed it*: the laptop that was online when the button was
#: pressed and shut before its next poll.
STRANDED_SECONDS = 420.0


def render_output_key(
    organisation_id: str, project_id: str, render_job_id: str
) -> str:
    """Where a device's finished render is uploaded to.

    One function because two call sites is one too many: the signed upload URL
    handed to a device and the `ObjectRef` recorded afterwards must name the
    same object, and two format strings that agree today are two format strings
    that will not agree after the first refactor.

    A cloud render does *not* come through here — the renderer stores its own
    output and the recorded `ObjectRef` points wherever that is. Nothing reads a
    render back by reconstructing this key; `render_job.output` is the only way
    a finished video is ever found, which is what lets the two paths differ
    without anything downstream caring.
    """
    from vtv.security.paths import safe_storage_key

    return safe_storage_key(
        f"orgs/{organisation_id}/projects/{project_id}/renders/{render_job_id}.mp4"
    )


def _which(online: list[Device]) -> str:
    """Name the machine, because "this device" means nothing on three machines."""
    first = online[0].name or "your computer"
    if len(online) == 1:
        return f"Rendering on {first}."
    return f"Rendering on one of your {len(online)} available computers ({first} and others)."


def worker_id_for(device: Device | str) -> str:
    """How a device names itself to the queue.

    Prefixed, so a glance at `claimed_by` says whether a job is running in the
    cloud or on somebody's desk — which is the first thing anybody wants to know
    when a render is slow.
    """
    identifier = device if isinstance(device, str) else device.device_id
    return f"device:{identifier}"


@dataclass
class DevicePool:
    """The server's half of local execution.

    Everything a device is allowed to do passes through here, and every method
    takes the device rather than trusting an id in the request body. A device
    that could name another device in a payload would be a device that can steal
    or sabotage its neighbour's job.
    """

    queue: Any
    repository: Any
    storage: Any
    directory: Any

    # -- putting work up --------------------------------------------------

    def plan(self, organisation_id: str, wanted: Target = Target.AUTO) -> Plan:
        """Turn what the person picked into where the render actually runs.

        ## What Auto is allowed to consider

        Only whether a computer of theirs could take the job right now: paired,
        not revoked, and seen within the last minute and a half. Not whether it
        has a graphics card — a processor-only machine still renders a
        typography-heavy video faster than it uploads one, and the per-segment
        router already sends each segment wherever it belongs. Hardware decides
        *how* a job is drawn; this decides *where*.

        Deliberately not considered: how busy the machine is, or how fast it was
        last time. Both are measurable and neither is measured yet, and an Auto
        that pretends to weigh evidence it does not have is worse than one that
        says plainly what it looked at.
        """
        online = [
            device
            for device in self.directory.devices_for(organisation_id)
            if device.is_active and device.state() is not DeviceState.OFFLINE
        ]
        if wanted is Target.CLOUD:
            return Plan(Target.CLOUD, "You chose to render in the cloud.", CLOUD_KIND)
        if wanted is Target.DEVICE:
            # Refused rather than quietly redirected. Somebody who picked "this
            # device" has a reason — cost, privacy, a card they paid for — and
            # silently rendering in the cloud instead spends their money to
            # ignore them.
            if not online:
                raise PolicyViolation(
                    f"no available device for organisation {organisation_id}",
                    user_message=(
                        "No computer on this account is available to render "
                        "right now. Start the desktop app on one of them, or "
                        "choose cloud rendering."
                    ),
                )
            return Plan(Target.DEVICE, _which(online), DEVICE_KIND)
        if online:
            return Plan(Target.DEVICE, _which(online), DEVICE_KIND)
        return Plan(
            Target.CLOUD,
            "No computer on this account is available, so this is rendering in "
            "the cloud.",
            CLOUD_KIND,
        )

    async def offer(
        self,
        *,
        project_id: str,
        organisation_id: str,
        render_job_id: str,
        settings: Any,
        execution: Target = Target.AUTO,
    ) -> tuple[str, Plan]:
        """Queue a render wherever `plan` says it should run.

        ## Why the kind is decided here and never later

        Because the queue hands out work by kind, and the kind is therefore the
        routing decision. Enqueueing one kind that both a device and a cloud
        worker could claim would be a race whose loser has already spent minutes
        drawing; a flag inside the payload would be a rule every claimer has to
        remember to check. Choosing the kind at the moment somebody presses the
        button makes the decision unrepeatable and unforgettable at once.
        """
        chosen = self.plan(organisation_id, execution)
        handle = await self.queue.enqueue(
            kind=chosen.kind,
            payload={
                "project_id": project_id,
                "organisation_id": organisation_id,
                "render_job_id": render_job_id,
                "settings": settings.model_dump(mode="json"),
            },
            # Keyed by the render job, so a customer pressing the button twice
            # gets one render. The queue enforces it with a unique index rather
            # than a pre-flight check, so two requests racing both survive.
            idempotency_key=f"device-render:{render_job_id}",
        )
        return getattr(handle, "job_id", str(handle)), chosen

    async def escalate(self, *, older_than_seconds: float = STRANDED_SECONDS) -> int:
        """Move renders to the cloud when the computer they were sent to never came.

        ## The gap this closes

        "Auto" asks whether a computer is available *at the moment somebody
        presses the button*, and that is the right question to ask then. It stops
        being the right answer about ninety seconds later, when the laptop is
        shut and put in a bag. The job then sits pending — not failed, not
        running, not wrong in any way the system can detect, because a pending
        job is an entirely normal thing — and the customer watches a spinner
        until they give up.

        So after a few minutes of nobody taking it, the work goes to the cloud.
        The person gets their video, slower and at our cost, which is the correct
        trade against a render that silently never happens.

        ## Why this is the only caller of the cloud kind

        And why noticing that mattered. `render_timeline_cloud` had a handler, a
        registration and a test asserting the two agreed — and nothing anywhere
        enqueued one. It was a job kind that could only ever be produced by a
        path that did not exist: dead code wearing the clothes of a feature,
        which is exactly the thing an audit is for.
        """
        moved = 0
        try:
            stranded = await self.queue.stranded(
                DEVICE_KIND, older_than_seconds=older_than_seconds
            )
        except Exception:  # noqa: BLE001 - a background sweep must not throw
            return 0

        for job in stranded:
            payload = dict(job.payload)
            try:
                # Enqueued first, cancelled second. The other order can lose a
                # render outright if the process dies between the two; this
                # order can at worst draw one twice, and the idempotency key
                # makes even that unlikely. Losing work and duplicating work are
                # not symmetric.
                await self.queue.enqueue(
                    kind=CLOUD_KIND,
                    payload=payload,
                    idempotency_key=(
                        f"cloud-render:{payload.get('render_job_id') or job.job_id}"
                    ),
                )
                await self.queue.cancel(job.job_id)
                moved += 1
            except Exception:  # noqa: BLE001 - one bad row must not stop the rest
                continue
        return moved

    def capacity(self, organisation_id: str) -> dict[str, Any]:
        """What this tenant could render on right now.

        For the picker in Phase D, and for an honest answer to "why is 'this
        device' greyed out". Reports what is actually there rather than what was
        paired at some point.
        """
        devices = [
            device
            for device in self.directory.devices_for(organisation_id)
            if device.is_active
        ]
        online = [
            device for device in devices if device.state() is not DeviceState.OFFLINE
        ]
        return {
            "paired": len(devices),
            "available": len(online),
            "accelerated": sum(
                1 for device in online if device.hardware.gpu_verified
            ),
            "devices": [
                {
                    "device_id": device.device_id,
                    "name": device.name,
                    "state": device.state().value,
                    "summary": device.hardware.summary,
                }
                for device in devices
            ],
        }

    # -- offering work -----------------------------------------------------

    async def claim(self, device: Device, *, reap: bool = True) -> Assignment | None:
        """Lease one job for this device, or `None` when there is nothing.

        A device with no verified graphics card is not refused work — it renders
        on its processor, which is what the per-segment router would have chosen
        for most of a typography-heavy video anyway. Hardware decides *how* a
        job is drawn, not *whether* this machine may draw one.
        """
        if not device.is_active:
            raise PolicyViolation(
                f"device {device.device_id} is revoked",
                user_message="This computer is no longer paired with that account.",
            )

        # Reap abandoned claims before asking for work.
        #
        # `recover()` is otherwise called only by the cloud worker's maintenance
        # loop, and the entire premise of local execution is a deployment that
        # runs no cloud worker at all. Without this, a device that was killed
        # mid-render holds its job until somebody restarts the API — the job is
        # neither running nor available, the customer watches a spinner, and
        # nothing in the system is broken enough to say so.
        #
        # On the claim path specifically, because that is the moment somebody
        # wants the work: a device asking for a job is the natural trigger for
        # noticing that a job is going spare. It costs one indexed UPDATE.
        # `reap` is off on the repeated looks a held claim makes. Reaping is
        # an indexed write, and doing it twice a second for every idle
        # computer, to catch a job the next long-poll would find anyway, is a
        # lot of writes to save a few seconds once.
        if reap:
            with contextlib.suppress(Exception):
                await self.queue.recover()

        worker = worker_id_for(device)
        claimed = await self.queue.lease(worker_id=worker, kinds=(DEVICE_KIND,))
        if claimed is None:
            return None

        try:
            return await self._assignment(device, claimed)
        except Exception as exc:
            # The job is already claimed at this point. Handing it straight back
            # rather than leaving it to the lease timeout matters: a project
            # whose timeline cannot be loaded would otherwise be claimed,
            # abandoned and re-claimed by every device in the account, five
            # minutes apart, forever.
            await self.queue.surrender(
                claimed.job_id,
                worker,
                ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category=ErrorCategory.INTERNAL,
                    message=f"could not prepare the assignment: {exc}"[:400],
                ),
                retry=True,
            )
            raise

    async def _assignment(self, device: Device, claimed: Any) -> Assignment:
        payload = dict(claimed.payload)
        organisation_id = str(payload.get("organisation_id") or "")
        project_id = str(payload.get("project_id") or "")

        # The tenant check, at the one place a device is handed content. A
        # device belongs to exactly one organisation and may never be given a
        # job from another; this is that rule, and it is a single comparison on
        # purpose so it can be read and audited.
        if not organisation_id or organisation_id != device.organisation_id:
            raise PolicyViolation(
                f"job for {organisation_id!r} offered to a device in "
                f"{device.organisation_id!r}",
                user_message="That job is not available to this computer.",
            )

        from vtv.contracts.render import RenderSettings
        from vtv.contracts.timeline import Timeline

        document = await self.repository.get_document(
            project_id=project_id, kind=Timeline.document_name
        )
        if document is None:
            raise NotFound(f"no timeline stored for {project_id}")
        timeline = Timeline.model_validate(document)
        settings = RenderSettings.model_validate(payload.get("settings") or {})

        now = utc_now()
        return Assignment(
            render_job_id=str(payload.get("render_job_id") or claimed.job_id),
            project_id=project_id,
            organisation_id=organisation_id,
            timeline=timeline,
            settings=settings,
            assets=tuple(await self._handouts(timeline)),
            # Whether there is anywhere to put the result, not where. The URL
            # is minted when the device is ready to upload — see
            # `upload_url_for` — because an hour from *now* is not an hour
            # from when a long render finishes.
            can_upload=bool(
                await self._upload_url(payload, organisation_id, project_id)
            ),
            lease=JobLease(
                render_job_id=str(payload.get("render_job_id") or claimed.job_id),
                device_id=device.device_id,
                organisation_id=organisation_id,
                granted_at=now,
                # The window this queue will *actually* apply, asked of the
                # queue rather than taken from a constant beside it.
                #
                # The constant said five minutes and the queue this device's
                # job lives in enforced either one minute or fifteen, depending
                # on which process happened to run `recover()`. A lease is a
                # promise about when somebody else may take your work; a
                # promise that is not the number being enforced is not a lease,
                # it is a decoration. Deriving it means the two cannot drift,
                # because there is only one of them.
                expires_at=now + timedelta(seconds=self._lease_seconds()),
            ),
        )

    def _lease_seconds(self) -> float:
        """How long this queue really lets a claim go quiet.

        `LEASE_TTL_SECONDS` is the fallback for a queue that does not say —
        a test double, mostly. A real queue is asked.
        """
        window = getattr(self.queue, "reclaim_after_seconds", None)
        return float(window) if window else float(LEASE_TTL_SECONDS)

    async def _handouts(self, timeline: Any) -> list[AssetHandout]:
        """A signed, expiring URL for each object this job draws from.

        Exactly the objects this timeline names, and no prefix or wildcard. A
        device is given the ability to read four photographs, not the ability to
        read the bucket those photographs are in.
        """
        from vtv.contracts.timeline import AssetClipSource

        seen: dict[str, ObjectRef] = {}
        for clip in timeline.clips:
            source = clip.source
            if isinstance(source, AssetClipSource) and source.object is not None:
                seen.setdefault(source.object.key, source.object)
        narration = getattr(timeline.narration, "audio", None)
        if narration is not None:
            seen.setdefault(narration.key, narration)

        handouts: list[AssetHandout] = []
        for ref in seen.values():
            digest = ref.checksum_sha256 or self._digest(ref)
            if not digest:
                # A device that cannot verify an asset must not draw from it: an
                # unverifiable download is how a truncated photograph becomes a
                # grey band in a finished video. Skipping is the safe answer —
                # the renderer describes a missing asset as a message card,
                # which is visible and honest, where a corrupt one is neither.
                continue
            handouts.append(
                AssetHandout(
                    object=ref,
                    url=await self.storage.signed_url(
                        ref, expires_in_seconds=ASSET_URL_SECONDS
                    ),
                    sha256=digest,
                    size_bytes=ref.size_bytes or 0,
                )
            )
        return handouts

    def _digest(self, ref: ObjectRef) -> str:
        """Hash an object that was stored before checksums were recorded."""
        try:
            path = self.storage.path_for(ref)
        except Exception:
            return ""
        if not path.exists():
            return ""
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()

    async def _record(self, held: Any, completion: Completion) -> bool:
        """Write the `RenderJob` document the web app reads. False if it failed.

        ## Why this exists at all

        Everything before this got the video onto the server. Nothing put it
        anywhere the Studio could find it: `GET /projects/{id}/video` serves
        `render_job.output`, and the device path settled a queue row and wrote
        no document. Phase C was verified by running `ffprobe` on the file in
        storage — which proved the render, and quietly skipped the question of
        whether the product could see it. It could not.

        ## Why it checks storage rather than trusting the upload

        The device reports `output_bytes` and it is not taken as proof. A
        completion can arrive after an upload that half-succeeded, or against a
        deployment whose storage has no signed uploads at all and handed the
        executor an empty URL — in which case the device renders, has nowhere to
        put the result, and reports success in perfectly good faith. Asking
        storage whether the object is really there is the difference between
        recording a render and recording a claim about one.
        """
        from vtv.contracts.render import RenderJob, RenderSettings

        payload = dict(held.payload)
        organisation_id = str(payload.get("organisation_id") or "")
        project_id = str(payload.get("project_id") or "")
        render_job_id = str(payload.get("render_job_id") or "")
        if not (organisation_id and project_id and render_job_id):
            return False

        try:
            # Scoped to the tenant in the query rather than compared afterwards.
            project = await self.repository.get_project(
                project_id, organisation_id=organisation_id
            )
            timeline_id = getattr(project, "timeline_id", None) if project else None
            if project is None or not timeline_id:
                return False

            output: ObjectRef | None = None
            if completion.ok:
                candidate = ObjectRef(
                    bucket=self.storage.bucket,
                    key=render_output_key(organisation_id, project_id, render_job_id),
                    content_type="video/mp4",
                    size_bytes=completion.output_bytes or None,
                )
                if not await self.storage.exists(candidate):
                    return False
                output = candidate

            job = RenderJob(
                render_job_id=render_job_id,
                organisation_id=organisation_id,
                project_id=project_id,
                timeline_id=timeline_id,
                settings=RenderSettings.model_validate(payload.get("settings") or {}),
                status=Status.READY if output is not None else Status.FAILED,
                progress=1.0 if output is not None else 0.0,
                output=output,
                duration_seconds=completion.duration_seconds or None,
                render_seconds=completion.render_seconds or None,
                # `{"local_gpu": 10, "local_cpu": 1}` — the answer to "did my
                # graphics card actually do this", kept per render rather than
                # per segment because a four-hour video is twelve hundred of
                # them.
                backends=dict(completion.backends),
                error=None if output is not None else ErrorInfo(
                    code=ErrorCode.RENDER_FAILED,
                    category=(
                        ErrorCategory.INTERNAL if completion.elsewhere
                        else ErrorCategory.VALIDATION
                    ),
                    message=completion.reason or "the device could not render this",
                ),
            )
            await self.repository.put_document(
                project_id=project_id,
                kind=RenderJob.document_name,
                document_id=render_job_id,
                payload=json.loads(job.model_dump_json()),
            )
        except Exception:
            return False
        return True

    async def upload_url_for(self, device: Device) -> str:
        """A fresh upload URL for the job this device is holding.

        Called at the moment the device has a finished file and is ready to send
        it, which is the only moment at which a one-hour URL is a one-hour URL.

        The job is resolved from the claim, never from anything the device says
        — the same rule as `progress` and `complete`. A device that could name a
        job could ask for a write URL into somebody else's render.
        """
        held = await self.queue.held_job(worker_id_for(device))
        if held is None:
            # No claim, no URL. A device asking to upload for a job it does not
            # hold has either lost its lease or is asking for something that is
            # not its business, and both answers are the same one.
            return ""
        payload = dict(held.payload)
        organisation_id = str(payload.get("organisation_id") or "")
        if organisation_id != device.organisation_id:
            return ""
        return await self._upload_url(
            payload, organisation_id, str(payload.get("project_id") or "")
        )

    async def _upload_url(
        self, payload: dict[str, Any], organisation_id: str, project_id: str
    ) -> str:
        """Write-only, single-object, expiring, size-capped.

        Four separate limits because each removes a different thing a leaked URL
        could be used for: reading other renders, overwriting a different
        object, working next week, and filling the bucket.
        """
        key = render_output_key(
            organisation_id, project_id, str(payload.get("render_job_id") or "")
        )
        try:
            from vtv.contracts.base import RetentionClass

            return await self.storage.signed_upload_url(
                key=key,
                content_type="video/mp4",
                expires_in_seconds=ASSET_URL_SECONDS,
                max_bytes=MAX_UPLOAD_BYTES,
                # The same class the cloud renderer gives its own output, so a
                # video costs the same to keep whichever machine drew it. The
                # customer's copy is the one they downloaded; ours is a
                # convenience with an expiry date.
                retention=RetentionClass.EPHEMERAL,
            )
        except Exception:
            # A storage backend without signed uploads is a deployment where
            # devices cannot return their output. Better an empty string the
            # executor treats as "nowhere to upload" than a fabricated URL.
            return ""

    # -- while it works ----------------------------------------------------

    async def progress(self, device: Device, report: ProgressReport) -> bool:
        """Record progress and renew the lease. True if cancellation is wanted.

        ## Why the device does not say which job it is reporting on

        It says, and it is not believed. The row that gets renewed is *the one
        this device holds*, looked up from the claim — so a device physically
        cannot renew, steal or fail another machine's job, and there is no
        comparison anybody has to remember to write.

        The first version addressed the row by the `render_job_id` in the body,
        which was wrong twice over. It trusted a caller-supplied identifier, and
        it did not even work: a render job id and the queue's job id are
        different identifiers, so every renewal silently updated nothing and
        every lease expired mid-render while the device reported progress into
        the void. The tests caught it because they let a lease actually run out
        rather than assuming it would not.
        """
        if report.device_id != device.device_id:
            raise PolicyViolation(
                "a device reported on work belonging to another device",
                user_message="That job is not available to this computer.",
            )
        worker = worker_id_for(device)
        held = await self.queue.held_job(worker)
        if held is None:
            return False
        return await self.queue.renew(held.job_id, worker)

    async def complete(self, device: Device, completion: Completion) -> bool:
        """Settle a job a device has finished with. False if the claim was lost.

        A lost claim is not an error and not a lie to the device: it means the
        lease expired while the machine was drawing and somebody else has the
        job now. The right thing is to discard this outcome, which the queue
        already does by guarding its write on `claimed_by`.
        """
        if completion.device_id != device.device_id:
            raise PolicyViolation(
                "a device completed work belonging to another device",
                user_message="That job is not available to this computer.",
            )

        worker = worker_id_for(device)
        # The job this device holds, not the one it names. See `progress`.
        held = await self.queue.held_job(worker)
        if held is None:
            return False
        if completion.ok:
            # Recorded before the job is settled, and the settle is conditional
            # on the recording having worked.
            #
            # The other order is worse in the way that matters: a settled job
            # with no `render_job` document is a render the queue calls finished
            # and the web app cannot find — the customer's own computer drew
            # their video, uploaded it, and the Studio still shows nothing to
            # play. Failing to settle is recoverable; the lease expires and the
            # render is drawn again, which is wasteful and visible. A silent
            # success is neither.
            recorded = await self._record(held, completion)
            if not recorded:
                return await self.queue.surrender(
                    held.job_id,
                    worker,
                    ErrorInfo(
                        code=ErrorCode.STORAGE_UNAVAILABLE,
                        category=ErrorCategory.INTERNAL,
                        message="the render finished but could not be recorded",
                    ),
                    retry=True,
                )
            return await self.queue.settle(held.job_id, worker)
        await self._record(held, completion)
        return await self.queue.surrender(
            held.job_id,
            worker,
            ErrorInfo(
                code=ErrorCode.RENDER_FAILED,
                # `elsewhere` decides the category, which decides whether the
                # queue retries: the device is the only thing that knows whether
                # its failure was about this machine or about this job.
                #
                # `VALIDATION` for the terminal case specifically because it is
                # in `TERMINAL_CATEGORIES` — the same set the executor uses to
                # decide `elsewhere` in the first place, so the two ends of this
                # decision cannot drift apart.
                category=(
                    ErrorCategory.INTERNAL if completion.elsewhere
                    else ErrorCategory.VALIDATION
                ),
                message=completion.reason or "the device could not render this",
            ),
            retry=completion.elsewhere,
        )


__all__ = [
    "ASSET_URL_SECONDS",
    "STRANDED_SECONDS",
    "CLOUD_KIND",
    "DEVICE_KIND",
    "MAX_UPLOAD_BYTES",
    "DevicePool",
    "Plan",
    "Target",
    "render_output_key",
    "worker_id_for",
]
