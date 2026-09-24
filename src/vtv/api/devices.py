"""The HTTP surface for a customer's own computers.

Eight endpoints in two groups, and the split between them is the security story.

**Three are administrative** — create a pairing code, list the computers on an
account, revoke one — and need `DEVICE_MANAGE`, which only admins and owners
hold. Deciding which machines an organisation's material may be sent to is an
administrative act, not an editorial one.

**One is unauthenticated on purpose.** `/pair` is where a computer that has no
credential gets one; requiring a credential to obtain a credential is a circle.
The pairing code *is* the authentication, which is why it is single-use, expires
in minutes, and is rate limited harder than anything else here.

**Four are for devices**, and every one of them resolves the device from the
bearer token and then ignores any device id in the body. A payload that could
name a device would be a payload that lets one machine renew, steal or fail
another's job.

## Why the failures are uniform

Every bad pairing attempt returns the same refusal — no such code, expired,
already used, wrong organisation, all identical. Distinguishing them tells
somebody guessing which guess was closest. The person with a real problem is
better served by generating another code, which takes two seconds, than by an
error message that also helps an attacker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from vtv.contracts.devices import Completion, DeviceHardware, ProgressReport
from vtv.contracts.errors import VTVError
from vtv.contracts.tenancy import Capability, Principal
from vtv.security.devices import format_code, mint_pairing_code
from vtv.security.limits import RateLimitPolicy, limit_key

#: Pairing is the one unauthenticated write in this file, so it gets the
#: tightest budget in the system. Six characters of code is about 29 bits: at
#: ten attempts a minute, guessing one inside its ten-minute life is not an
#: attack anybody completes, and this is what makes that true.
PAIR_LIMIT = RateLimitPolicy(rate_per_second=0.2, burst=10)

#: Polling is frequent and cheap and must not be throttled into uselessness — a
#: device asking every five seconds is the design, not abuse.
DEVICE_LIMIT = RateLimitPolicy(rate_per_second=10.0, burst=60)

#: What a device is told to wait when somebody is watching. Short enough
#: that pressing Render feels immediate, long enough that a few machines
#: on one account are not a load test of one's own server.
ATTENTIVE_POLL_SECONDS = 2

#: How often a computer waiting for a browser approval should ask. Slow
#: enough not to be a load generator while somebody hunts for a password,
#: fast enough that the machine is paired before they look back at it.
PAIR_POLL_SECONDS = 3

#: Longest a claim may be held open waiting for work.
#:
#: Under a minute, so it sits comfortably inside the default idle timeouts
#: of the proxies and load balancers this will eventually run behind — a
#: hold that outlives the intermediary is a connection reset the device has
#: to treat as an error.
MAX_CLAIM_WAIT_SECONDS = 25.0

#: How often a held claim looks again. The cost of a shorter tick is a
#: database read per device per tick; the benefit is the delay between a
#: person pressing Render and their machine starting.
CLAIM_TICK_SECONDS = 0.5


def routes(
    *,
    guard: Callable[..., Principal],
    owned_project: Callable[[Request, Principal], Awaitable[Any]],
    json_body: Callable[[Request], Awaitable[dict[str, Any]]],
    client_ip: Callable[[Request], str],
    assembly: Any,
    pool: Any,
) -> list[Route]:
    """Build the device routes over the application's existing plumbing.

    Dependencies passed in rather than imported, for the same reason as the
    product routes: this module must not be able to acquire its own
    authentication or its own rate limiter, because those are the two things
    that have to have exactly one implementation.
    """
    directory = assembly.directory

    def _device(request: Request) -> Any:
        """Resolve the bearer token into a device, or refuse.

        The single place a device identity comes from. Everything downstream
        takes the `Device` object this returns and never an identifier out of a
        request body — that is the difference between a device acting for itself
        and a device nominating whose work it is touching.
        """
        header = request.headers.get("authorization") or ""
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer" or not credential.strip():
            raise VTVError("a device token is required")
        assembly.limiter.require(
            limit_key("device", client_ip(request)), policy=DEVICE_LIMIT
        )
        _, device = directory.authenticate_device(credential.strip())
        return device

    # -- administrative ---------------------------------------------------

    async def create_code(request: Request) -> Response:
        """Mint a pairing code for somebody to type into their computer."""
        principal = guard(request, Capability.DEVICE_MANAGE)
        code = directory.create_pairing_code(
            mint_pairing_code(
                organisation_id=principal.organisation_id or "",
                created_by=principal.subject,
            )
        )
        return JSONResponse(
            {
                # Shown grouped, because it is going to be read off a screen and
                # typed into another one, and possibly read aloud.
                "code": format_code(code.code),
                "expires_at": code.expires_at.isoformat(),
                "instructions": (
                    "On the computer you want to render on, run:  "
                    f"vtv-desktop pair --code {format_code(code.code)} "
                    "--server <this server>"
                ),
            },
            status_code=201,
        )

    async def list_devices(request: Request) -> Response:
        principal = guard(request, Capability.DEVICE_MANAGE)
        found = directory.devices_for(principal.organisation_id or "")
        return JSONResponse({"devices": [_view(device) for device in found]})

    async def revoke(request: Request) -> Response:
        principal = guard(request, Capability.DEVICE_MANAGE)
        device_id = request.path_params["device_id"]
        existing = directory.device(device_id)
        # Checked before revoking, so revoking a device id from another tenant
        # is a not-found rather than a successful cross-tenant write.
        if existing is None or not principal.owns(existing.organisation_id):
            return JSONResponse({"error": "no such device"}, status_code=404)
        return JSONResponse(_view(directory.revoke_device(device_id)))

    # -- pairing ----------------------------------------------------------

    async def pair(request: Request) -> Response:
        """Turn a typed code into a device token. The only unauthenticated write.

        Rate limited by address rather than by tenant, because there is no tenant
        yet — that is what the code is for — and an unauthenticated endpoint with
        no per-caller budget is an endpoint somebody enumerates.
        """
        assembly.limiter.require(
            limit_key("pair", client_ip(request)), policy=PAIR_LIMIT
        )
        body = await json_body(request)
        hardware = DeviceHardware.model_validate(body.get("hardware") or {})
        minted = directory.redeem_pairing_code(
            str(body.get("code") or ""),
            name=str(body.get("name") or "").strip() or "A computer",
            hardware=hardware,
        )
        return JSONResponse(
            {
                "device_id": minted.record.device_id,
                # The only moment this value exists outside the desktop. Never
                # returned again, never logged, never recoverable from the
                # database — a lost token means pairing again.
                "token": minted.secret,
                "name": minted.record.name,
                "organisation_id": minted.record.organisation_id,
            },
            status_code=201,
        )

    # -- pairing that starts on the computer -------------------------------
    #
    # The typed-code flow above asks somebody to be signed in *first*, mint a
    # code, walk to the other machine and type it. That is the right shape for
    # an administrator provisioning a render farm and the wrong shape for one
    # person with a laptop, who expects what every desktop application does:
    # run it, a browser opens, sign in, done.
    #
    # These four endpoints are that, and they are deliberately the same shape as
    # the device authorisation grant every other desktop app uses — a device
    # code the computer keeps, a short user code the person reads, and polling
    # until somebody says yes. Not because a standard was required, but because
    # the failure modes are known: the secret the computer proves itself with is
    # never the thing shown on screen, and the short code that *is* shown can
    # approve nothing on its own.

    async def pair_start(request: Request) -> Response:
        """A computer with no credential asks to be paired.

        Unauthenticated, and inert: this creates nothing, belongs to no account,
        and expires in minutes. It becomes a device only when a signed-in person
        approves it and names the account.
        """
        assembly.limiter.require(
            limit_key("pair", client_ip(request)), policy=PAIR_LIMIT
        )
        body = await json_body(request)
        hardware = DeviceHardware.model_validate(body.get("hardware") or {})
        device_code, user_code, expires_at = directory.open_pairing_request(
            name=str(body.get("name") or "").strip() or "A computer",
            hardware=hardware,
        )
        base = str(request.base_url).rstrip("/")
        return JSONResponse(
            {
                # Kept by the computer, shown to nobody. This is what proves, at
                # collection, that the machine asking is the machine that asked.
                "device_code": device_code,
                # Read off one screen and confirmed on another. On its own it
                # authorises nothing — approving with it still needs a signed-in
                # person — which is what makes it safe to display and say aloud.
                "user_code": format_code(user_code),
                "verification_uri": f"{base}/devices/approve",
                "verification_uri_complete": (
                    f"{base}/devices/approve?code={format_code(user_code)}"
                ),
                "interval": PAIR_POLL_SECONDS,
                "expires_at": expires_at.isoformat(),
            },
            status_code=201,
        )

    async def pair_pending(request: Request) -> Response:
        """What the browser is being asked to approve.

        So the screen can name the computer — "Approve DESKTOP-4F2A, 8 cores,
        GTX 1650?" — rather than showing a bare Yes. A confirmation that carries
        nothing to check is a confirmation everybody clicks.
        """
        guard(request, Capability.DEVICE_MANAGE)
        found = directory.pending_pairing(str(request.path_params["user_code"]))
        if found is None:
            # One answer for unknown, expired and already-collected. The person
            # with a real problem starts the pairing again, which takes seconds;
            # a more specific message would mostly help somebody guessing codes.
            return JSONResponse({"error": "no such pairing request"}, status_code=404)
        return JSONResponse(
            {
                "user_code": format_code(found["user_code"]),
                "name": found["name"],
                "summary": found["hardware"].summary,
                "expires_at": found["expires_at"],
            }
        )

    async def pair_approve(request: Request) -> Response:
        """A signed-in person says yes, and thereby says which account.

        `DEVICE_MANAGE`, because this is the act that adds a computer to an
        organisation — the same authority as minting a pairing code, reached
        from the other end.
        """
        principal = guard(request, Capability.DEVICE_MANAGE)
        body = await json_body(request)
        approved = directory.approve_pairing(
            str(body.get("code") or ""),
            organisation_id=principal.organisation_id or "",
            approved_by=principal.subject,
        )
        if not approved:
            return JSONResponse(
                {"error": "that pairing request is no longer waiting"},
                status_code=404,
            )
        return JSONResponse({"approved": True})

    async def pair_collect(request: Request) -> Response:
        """The waiting computer takes its credential, once it exists.

        Unauthenticated in the sense of carrying no account credential, which is
        the point — it has none yet. The device code *is* the credential, it was
        never displayed, and it is checked against a hash.

        `202` for "not yet", because the desktop polls this while somebody is
        still finding their password, and a 4xx for the ordinary case would make
        every log look like a failing system.
        """
        assembly.limiter.require(
            limit_key("pair", client_ip(request)), policy=PAIR_LIMIT
        )
        body = await json_body(request)
        hardware = DeviceHardware.model_validate(body.get("hardware") or {})
        minted = directory.collect_pairing(
            str(body.get("device_code") or ""), hardware=hardware
        )
        if minted is None:
            return JSONResponse(
                {"pending": True, "interval": PAIR_POLL_SECONDS}, status_code=202
            )
        return JSONResponse(
            {
                "device_id": minted.record.device_id,
                # As with `/pair`: the only moment this value exists outside the
                # desktop. Never stored, never returned again.
                "token": minted.secret,
                "name": minted.record.name,
                "organisation_id": minted.record.organisation_id,
            },
            status_code=201,
        )

    # -- putting work up --------------------------------------------------

    async def offer(request: Request) -> Response:
        """Queue a render for this tenant's own computers.

        `RENDER_SUBMIT` and not `DEVICE_MANAGE`: choosing *where* a render runs
        is an editorial decision an editor makes every time they press the
        button, while deciding *which computers exist* is administrative. The
        two are separated because the same person often is not both.

        ## Why the project is in the path and not the body

        Because `owned_project` — the one place that decides whether this
        caller's tenant owns this project — reads it from `path_params`. A body
        parameter meant either a `KeyError` or a second, private ownership check
        living here, and a multi-tenant system with two implementations of "may
        you touch this" has one of them wrong.

        The first version took it in the body and raised `KeyError` on the very
        first real request. Nothing in the unit tests could see it: they call
        `DevicePool.offer` directly, which is the right level for what they
        test and is below the layer that was broken.
        """
        principal = guard(request, Capability.RENDER_SUBMIT)
        body = await json_body(request)
        project = await owned_project(request, principal)  # reads the path param
        if project is None:
            return JSONResponse({"error": "no such project"}, status_code=404)

        from vtv.contracts.base import IdPrefix, new_id
        from vtv.contracts.render import RenderSettings
        from vtv.dispatch import Target

        # `/render/device` means device, whatever the body says. It is the
        # route Phase C verified and its name is a promise; defaulting it to
        # Auto made it silently render in the cloud, which is the same route
        # quietly doing the opposite of what it is called.
        forced = request.url.path.endswith("/render/device")
        wanted = (
            Target.DEVICE.value
            if forced
            else str(body.get("execution") or Target.AUTO.value).strip().lower()
        )
        try:
            execution = Target(wanted)
        except ValueError:
            # Named rather than silently defaulted to Auto. A typo in a client
            # that quietly renders somewhere the customer did not choose is the
            # kind of bug that is only ever found on the bill.
            return JSONResponse(
                {
                    "error": f"unknown execution {wanted!r}",
                    "expected": [target.value for target in Target],
                },
                status_code=400,
            )

        # Before queueing, so a device polling right now is already being
        # told to hurry by the time the row exists.
        directory.nudge(principal.organisation_id or "")
        render_job_id = new_id(IdPrefix.RENDER_JOB)
        job_id, chosen = await pool.offer(
            project_id=project.project_id,
            organisation_id=principal.organisation_id or "",
            render_job_id=render_job_id,
            settings=RenderSettings.model_validate(body.get("settings") or {}),
            execution=execution,
        )
        return JSONResponse(
            {
                "render_job_id": render_job_id,
                "job_id": job_id,
                # What was actually chosen, never what was asked for. An "auto"
                # echoed back tells the editor nothing it can show a person.
                "execution": chosen.target.value,
                "reason": chosen.reason,
            },
            status_code=202,
        )

    async def where(request: Request) -> Response:
        """What Auto would choose right now, without queueing anything.

        So the picker can show the consequence before the click — "Auto →
        Rajesh's PC" rather than three radio buttons and a shrug. Read-only and
        free, which is what lets an editor call it whenever the panel opens.
        """
        principal = guard(request, Capability.PROJECT_READ)
        from vtv.dispatch import Target

        organisation_id = principal.organisation_id or ""
        options = []
        for target in Target:
            try:
                chosen = pool.plan(organisation_id, target)
                options.append(
                    {
                        "execution": target.value,
                        "available": True,
                        "resolves_to": chosen.target.value,
                        "reason": chosen.reason,
                    }
                )
            except VTVError as refusal:
                # A greyed-out option with the sentence saying why. The refusal
                # itself is the explanation, so there is no second copy of this
                # reasoning to keep in step with `plan`.
                #
                # `.info.user_message`, not `.user_message`. The attribute on
                # the exception class is a *default* — `PolicyViolation`'s is
                # "We could not use that material under its licence" — and
                # reading it returns that sentence whatever the raise site
                # passed. This exact mistake already shipped once, telling
                # somebody who had simply not started their desktop app that
                # their footage was unlicensed, and it came back here in a new
                # place three weeks later.
                options.append(
                    {
                        "execution": target.value,
                        "available": False,
                        "resolves_to": None,
                        "reason": (
                            getattr(refusal.info, "user_message", None)
                            or str(refusal)
                        ),
                    }
                )
        return JSONResponse({"options": options})

    async def capacity(request: Request) -> Response:
        """What this account could render on right now.

        Read with `PROJECT_READ` rather than `DEVICE_MANAGE`, because an editor
        deciding where to render needs to know whether "this device" is even an
        option — and greying out a button with no explanation is the thing this
        endpoint exists to prevent.
        """
        principal = guard(request, Capability.PROJECT_READ)
        # Opening the panel is somebody looking at their computers, which
        # is the moment before they press Render on one.
        directory.nudge(principal.organisation_id or "")
        return JSONResponse(pool.capacity(principal.organisation_id or ""))

    # -- the device's own endpoints ---------------------------------------

    async def announce(request: Request) -> Response:
        """A device saying it is alive and what it currently has."""
        device = _device(request)
        body = await json_body(request)
        hardware = DeviceHardware.model_validate(body.get("hardware") or {})
        updated = directory.touch_device(device.device_id, hardware=hardware)
        return JSONResponse(_view(updated))

    async def claim(request: Request) -> Response:
        """Ask for work. 204 means there is none, which is the common answer."""
        device = _device(request)
        body = await json_body(request)
        if body.get("hardware"):
            # Re-read on every claim, not just at pairing: a driver update or an
            # undocked laptop must change what this machine is offered on its
            # next poll, and pairing happens once.
            device = directory.touch_device(
                device.device_id,
                hardware=DeviceHardware.model_validate(body["hardware"]),
            )
        else:
            device = directory.touch_device(device.device_id)

        # Long-polling, and the reason it exists is a measurement rather than a
        # preference.
        #
        # The first design was an adaptive ladder: an idle machine widens its
        # poll from five seconds towards a minute, which takes a computer that
        # nobody is using from about seventeen thousand requests a day to
        # fourteen hundred. That part worked — a real device was measured
        # settling to a 52-second gap.
        #
        # What did not work was the half meant to buy the latency back. The
        # server set `Retry-After` when somebody pressed Render, and a device
        # already asleep in a 52-second wait cannot read a header it has not
        # asked for yet: the same measured run showed it starting **49 seconds**
        # after the button. A hint can only shorten the poll *after* the next
        # one, which is exactly the poll whose lateness was the problem.
        #
        # Holding the request open fixes both numbers at once — the device is
        # already connected when the job appears, so it starts in under a
        # second, and it makes about three thousand requests a day rather than
        # seventeen. It is not a second channel to keep alive; it is the same
        # request, answered late.
        wait = min(
            MAX_CLAIM_WAIT_SECONDS, max(0.0, float(body.get("wait") or 0.0))
        )
        assignment = await pool.claim(device)
        deadline = time.monotonic() + wait
        while assignment is None and time.monotonic() < deadline:
            await asyncio.sleep(CLAIM_TICK_SECONDS)
            # `reap=False` on the ticks. Reaping abandoned claims is an indexed
            # write, and doing it twice a second for every idle device in the
            # world to catch a job that the next long-poll would find anyway is
            # a lot of writes to save a few seconds once.
            assignment = await pool.claim(device, reap=False)

        if assignment is None:
            # `Retry-After` only when the server wants to *override* the
            # device's own backoff, never as a routine instruction.
            #
            # The device knows how long it has been idle and can widen its own
            # interval; the server knows something the device cannot — that
            # somebody just pressed Render, or opened the panel and is watching.
            # Sending the header unconditionally would put the whole polling
            # policy on the server and make every deployment's tuning a
            # server-side release; sending it only to say "come back quickly"
            # keeps the mechanism in one place and the exception in the other.
            headers = {}
            if directory.attention_wanted(device.organisation_id):
                headers["Retry-After"] = str(ATTENTIVE_POLL_SECONDS)
            return Response(status_code=204, headers=headers)
        return JSONResponse(assignment.model_dump(mode="json"))

    async def upload_url(request: Request) -> Response:
        """Where to put the finished video, asked for when it is finished.

        The assignment used to carry this, minted when the job was claimed and
        good for an hour. That works for a thirty-minute video and cannot work
        for a longer one: a real sixty-minute render took seventy-one minutes
        and then died with `403 object_expired`, having drawn every frame
        correctly. The URL went stale while the machine was busy earning it.

        The job is resolved from the claim, never from the request, so a device
        cannot ask for a write URL into another machine's render.
        """
        device = _device(request)
        directory.touch_device(device.device_id)
        url = await pool.upload_url_for(device)
        if not url:
            # Either this device holds no job, or the deployment's storage
            # cannot sign uploads. Both mean "there is nowhere for you to put
            # it", and neither is worth distinguishing to a caller that can do
            # nothing differently.
            return JSONResponse({"url": ""}, status_code=409)
        return JSONResponse({"url": url})

    async def progress(request: Request) -> Response:
        device = _device(request)
        report = ProgressReport.model_validate(await json_body(request))
        directory.touch_device(device.device_id)
        cancelled = await pool.progress(device, report)
        # The response is how a cancellation reaches a running device: it is
        # already talking to us every few seconds, so a separate channel would
        # be a second connection to keep alive for a message that fits here.
        return JSONResponse({"cancelled": bool(cancelled)})

    async def complete(request: Request) -> Response:
        device = _device(request)
        completion = Completion.model_validate(await json_body(request))
        directory.touch_device(device.device_id)
        settled = await pool.complete(device, completion)
        # `accepted: false` is not an error. It means the lease expired while
        # this machine was drawing and the job belongs to somebody else now —
        # the device did nothing wrong and there is nothing for it to retry.
        return JSONResponse({"accepted": bool(settled)})

    return [
        Route("/v1/devices/codes", create_code, methods=["POST"]),
        Route("/v1/devices", list_devices, methods=["GET"]),
        Route("/v1/devices/{device_id}", revoke, methods=["DELETE"]),
        Route("/v1/devices/pair", pair, methods=["POST"]),
        Route("/v1/devices/pair/start", pair_start, methods=["POST"]),
        Route("/v1/devices/pair/collect", pair_collect, methods=["POST"]),
        Route("/v1/devices/pair/approve", pair_approve, methods=["POST"]),
        Route("/v1/devices/pair/requests/{user_code}", pair_pending),
        Route("/v1/devices/capacity", capacity, methods=["GET"]),
        # A project route, deliberately: it is the same shape as every other
        # "do something to this project" endpoint, and it is what lets the one
        # tenancy check in the application apply to it unchanged.
        # The picker lives on the Studio's own `POST .../render`, in
        # `api/product.py`, which is where the render button already goes.
        # A second route at that path was written here first and could never
        # fire — Starlette matches the first — so it was a route that looked
        # like the feature and was dead on arrival.
        Route(
            "/v1/projects/{project_id}/render/device", offer, methods=["POST"]
        ),
        Route("/v1/projects/{project_id}/render/where", where),
        Route("/v1/devices/announce", announce, methods=["POST"]),
        Route("/v1/devices/claim", claim, methods=["POST"]),
        Route("/v1/devices/upload-url", upload_url, methods=["POST"]),
        Route("/v1/devices/progress", progress, methods=["POST"]),
        Route("/v1/devices/complete", complete, methods=["POST"]),
    ]


def _view(device: Any) -> dict[str, Any]:
    """A device as the UI sees it. No token, no digest, not even the prefix's tail."""
    return {
        "device_id": device.device_id,
        "name": device.name,
        "state": device.state().value,
        "hardware": {
            "summary": device.hardware.summary,
            "platform": device.hardware.platform,
            "cpu_cores": device.hardware.cpu_cores,
            "memory_mb": device.hardware.memory_mb,
            "gpu_name": device.hardware.gpu_name,
            "gpu_verified": device.hardware.gpu_verified,
            # Carried through so the UI can answer "why isn't my card being
            # used" without anybody reading a log on the customer's machine.
            "gpu_reason": device.hardware.gpu_reason,
            "targets": [target.value for target in device.hardware.targets],
        },
        "prefix": device.prefix,
        "paired_at": device.paired_at.isoformat() if device.paired_at else None,
        "last_seen_at": (
            device.last_seen_at.isoformat() if device.last_seen_at else None
        ),
    }


__all__ = ["DEVICE_LIMIT", "PAIR_LIMIT", "routes"]
