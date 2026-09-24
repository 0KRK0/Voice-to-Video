"""Talking to the server, from a computer that may be on a hotel wifi.

Every method here assumes the network is bad. Not as a matter of style — the
whole premise of this package is that the renderer is on somebody's desk rather
than in a datacentre, so a dropped connection is a normal Tuesday rather than an
incident.

## What that changes

**Polling is allowed to fail.** `claim` returning `None` and `claim` raising are
the same thing to the caller: there is no work right now, try again shortly. An
executor that stopped because the wifi blinked would be an executor people turn
off.

**Progress is best-effort.** A report that does not arrive must not fail a
render that is going fine. The lease will expire if enough of them are lost,
which is the correct outcome and is reached without any special handling.

**Completion is not.** Finishing is the one message that must arrive: it is what
turns a lease into a finished video, and losing it means the work is done and
nobody knows. So it retries, with backoff, well past the point where the other
calls would have given up.

## Why the retry budget differs per call

Uniform retry policy is how you get a system that hammers a struggling server
with the calls that do not matter and gives up on the one that does.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vtv.contracts.devices import (
    Assignment,
    Completion,
    DeviceHardware,
    ProgressReport,
)
from vtv.contracts.errors import PolicyViolation, ProviderError, ValidationFailed

#: Seconds between polls when there is nothing to do. Long enough that an idle
#: executor is invisible in a server's request log, short enough that a person
#: who presses Render does not wonder whether it worked.
IDLE_POLL_SECONDS = 5.0

#: Ceiling on the idle poll. A machine nobody is using settles here.
#:
#: Five seconds forever is about seventeen thousand requests a day per
#: computer, every one of them answered "nothing". Sixty is about fourteen
#: hundred. The cost of the wider interval is up to a minute before an idle
#: machine notices a new job, and that is what the server's `Retry-After`
#: buys back at the only moment it matters — when somebody is watching.
MAX_IDLE_POLL_SECONDS = 60.0

#: How the interval grows between empty answers. Gentle on purpose: the
#: ladder should be most of the way open after a couple of minutes of
#: silence, not after ten.
IDLE_POLL_GROWTH = 1.6

#: Ceiling for the backoff an unreachable server produces. A laptop that has
#: been shut in a bag for an hour should come back within a minute of the wifi
#: returning, not keep doubling into the afternoon.
MAX_BACKOFF_SECONDS = 60.0

#: How long to ask the server to hold a claim open.
#:
#: Must stay under the server's own ceiling or the extra is ignored, and
#: under the idle timeout of anything in between. At twenty-five seconds an
#: idle computer makes about three thousand requests a day — against
#: seventeen thousand for a five-second poll — and starts a job in under a
#: second rather than up to a minute later.
CLAIM_WAIT_SECONDS = 25.0

#: A render is minutes of silence from the server's point of view; a poll is
#: milliseconds. Separate timeouts because one number cannot be right for both.
POLL_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 900.0


def idle_interval(previous: float, *, floor: float = IDLE_POLL_SECONDS,
                  ceiling: float = MAX_IDLE_POLL_SECONDS) -> float:
    """The next idle poll interval, widened by one step.

    Separate from `backoff` and deliberately not jittered. `backoff` is for
    a server that is failing, where jitter stops every device retrying in
    the same instant and knocking it over a second time. This is for a
    server that is fine and has nothing to say, where the devices are
    already spread across the interval by when each of them started, and a
    predictable ladder is one somebody can reason about from a log.
    """
    return min(ceiling, max(floor, previous * IDLE_POLL_GROWTH))


def _retry_after(response: Any) -> float | None:
    """The server's `Retry-After`, in seconds, or None.

    Only the integer-seconds form is read. The HTTP-date form is legal and
    is not used here, and guessing at a date this server never sends would
    be code that cannot be wrong in a way anybody would notice.
    """
    try:
        raw = response.headers.get("retry-after")
    except Exception:  # noqa: BLE001 - a hint must never break a poll
        return None
    if not raw:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    # Clamped, because this is a number from the network deciding how hard
    # this machine hits somebody's server.
    return min(MAX_IDLE_POLL_SECONDS, max(0.5, seconds))


def backoff(attempt: int, *, ceiling: float = MAX_BACKOFF_SECONDS) -> float:
    """Exponential, with jitter, capped.

    The jitter is not decoration. When a server comes back after an outage,
    every device that was waiting retries at once and knocks it over again;
    spreading them over the interval is what stops the recovery being the second
    outage. Full jitter rather than half, because the population here is small
    and the cost of an extra second is nothing.
    """
    ceiling = max(0.0, ceiling)
    return random.uniform(0.0, min(ceiling, 2.0 ** max(0, attempt)))


@dataclass
class DeviceClient:
    """The device's half of the protocol.

    Holds the token and nothing else that matters: no provider credentials, no
    storage credentials, no access to any project. Everything it can reach is
    reachable because a lease says so.
    """

    server: str
    token: str
    #: Injected so the executor can be tested without a network and without a
    #: fake HTTP server. Defaults to a real client built on first use.
    transport: Any = None
    timeout: float = POLL_TIMEOUT
    #: What the server asked for on the last empty claim, in seconds, or
    #: None. Written by `claim`, read by the agent. A field rather than a
    #: return value because `claim` already has one meaning — the
    #: assignment or nothing — and overloading it with pacing would make
    #: every caller unpack a tuple to ignore half of it.
    hurry_seconds: float | None = field(default=None, repr=False)
    _owned: Any = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"DeviceClient(server={self.server!r}, token=<redacted>)"

    # -- wire --------------------------------------------------------------

    async def _client(self) -> Any:
        if self.transport is not None:
            return self.transport
        if self._owned is None:
            import httpx

            self._owned = httpx.AsyncClient(
                base_url=self.server.rstrip("/"),
                timeout=self.timeout,
                headers={"authorization": f"Bearer {self.token}"},
            )
        return self._owned

    async def aclose(self) -> None:
        if self._owned is not None:
            await self._owned.aclose()
            self._owned = None

    async def _post(self, path: str, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        client = await self._client()
        kwargs: dict[str, Any] = {"json": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = await client.post(path, **kwargs)
        return self._checked(response)

    def _checked(self, response: Any) -> Any:
        status = int(getattr(response, "status_code", 0))
        if status in (401, 403):
            # Terminal and worth saying plainly: a revoked device that kept
            # retrying would poll forever while its owner assumed it had
            # stopped. The executor turns this into an exit, not a backoff.
            raise PolicyViolation(
                "this computer is no longer paired with that account. "
                "Pair it again:  vtv-desktop pair --code ABC-DEF"
            )
        if status == 204:
            return None
        if status >= 400:
            raise ProviderError(f"server returned {status}: {self._body(response)[:300]}")
        return response.json()

    @staticmethod
    def _body(response: Any) -> str:
        try:
            return str(response.text)
        except Exception:  # pragma: no cover - defensive
            return "<unreadable>"

    # -- the protocol ------------------------------------------------------

    async def announce(self, hardware: DeviceHardware) -> dict[str, Any]:
        """Say what this machine has, and that it is alive.

        Sent on every claim rather than once at pairing, because hardware
        changes underneath a paired device — a driver update, a card swap, an
        undocked laptop. A machine whose card stopped verifying must stop being
        offered graphics work on its next poll.
        """
        return await self._post(
            "/v1/devices/announce", {"hardware": hardware.model_dump(mode="json")}
        ) or {}

    async def claim(
        self, hardware: DeviceHardware, *, wait: float = CLAIM_WAIT_SECONDS
    ) -> Assignment | None:
        """Ask for work, and let the server hold the question open.

        `wait` asks the server not to answer "nothing" straight away but to keep
        looking for up to that long. It is what makes pressing Render feel
        immediate: the machine is already connected when the job appears, so it
        starts in well under a second instead of whenever its next poll happens
        to fall.

        A server that ignores `wait` answers immediately and everything still
        works — the agent's own interval takes over, more slowly. That is why
        this is a request in the body rather than a required parameter.

        A `Retry-After` on the empty answer is kept in `hurry_seconds` for the
        agent to read. It stays useful even with the hold: a device whose wait
        has just expired can be told to come straight back rather than following
        its own widened interval.
        """
        client = await self._client()
        response = await client.post(
            "/v1/devices/claim",
            json={"hardware": hardware.model_dump(mode="json"), "wait": wait},
            # The request now spends most of its life deliberately waiting, so
            # the poll timeout has to outlast the hold. Without this the client
            # gives up at thirty seconds on a twenty-five-second wait, which
            # would look like an unreliable network and be a arithmetic mistake.
            timeout=wait + POLL_TIMEOUT,
        )
        payload = self._checked(response)
        if not payload:
            self.hurry_seconds = _retry_after(response)
            return None
        self.hurry_seconds = None
        return Assignment.model_validate(payload)

    async def upload_url(self) -> str:
        """Ask where to put the finished video, now that there is one.

        Deliberately at upload time rather than at claim time. A signed URL
        minted when the job was handed out has already spent the whole render
        expiring, which is exactly how a correct sixty-minute render ended in
        `403 object_expired` with every frame drawn.
        """
        payload = await self._post("/v1/devices/upload-url", {})
        return str((payload or {}).get("url") or "")

    async def report(self, progress: ProgressReport) -> bool:
        """Say how far along. Never raises; the lease is the real deadline.

        Best-effort on purpose. A report that does not arrive must not fail a
        render that is going fine, and losing enough of them expires the lease,
        which is exactly the outcome that should follow from a device that has
        genuinely stopped talking.
        """
        try:
            await self._post("/v1/devices/progress", progress.model_dump(mode="json"))
            return True
        except PolicyViolation:
            raise
        except Exception:
            return False

    async def finish(self, completion: Completion, *, attempts: int = 8) -> bool:
        """Say how it ended. Retries hard, because this is the message that counts.

        Everything else here gives up quickly and lets the lease sort it out.
        This one cannot: the work is done, the file is uploaded, and a lost
        completion means a finished render that nobody knows is finished. Eight
        attempts with backoff is minutes of tolerance for a bad connection.
        """
        for attempt in range(attempts):
            try:
                await self._post(
                    "/v1/devices/complete", completion.model_dump(mode="json")
                )
                return True
            except PolicyViolation:
                raise
            except Exception:
                if attempt == attempts - 1:
                    return False
                await asyncio.sleep(backoff(attempt))
        return False

    def absolute(self, url: str) -> str:
        """Resolve a URL the server gave us against the server itself.

        Storage signs **relative** URLs — `/media/<bucket>/<key>?token=…` —
        because the server does not necessarily know its own public hostname,
        and hard-coding one into every signed URL is how a deployment behind a
        different domain serves links nobody can open. A relative URL therefore
        means "on the server you are already talking to", which this device
        knows and the server does not.

        Not a workaround: it is the one place that mapping belongs. It is also
        the first thing that breaks the moment this runs over real HTTP instead
        of against a substituted client, which is exactly why it is worth
        naming rather than inlining.
        """
        if "://" in url:
            return url
        return f"{self.server.rstrip('/')}/{url.lstrip('/')}"

    async def fetch(self, url: str, target: Path) -> int:
        """Stream one asset to disk. Returns bytes written.

        Streamed rather than held: a job's photographs can be hundreds of
        megabytes and a desktop that loaded them all into memory before writing
        would fall over on exactly the machines this feature exists to use.

        Written to a `.part` and renamed by the caller only once the digest
        matches, so a partial download can never be mistaken for an asset.
        """
        import httpx

        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            async with client.stream("GET", self.absolute(url)) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        f"asset download failed with {response.status_code}"
                    )
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                        written += len(chunk)
        return written

    async def upload(self, url: str, source: Path) -> int:
        """Send the finished video. Returns bytes sent.

        Streamed from an **async** generator, which is not a stylistic choice:
        handing `httpx.AsyncClient.put` an open file object raises "attempted to
        send an sync request with an AsyncClient instance" at runtime. It is the
        obvious way to write it, it type-checks, and it fails on the first real
        upload — which is precisely what a test with a substituted client cannot
        see.

        No `content-length` header either. With a generator httpx uses chunked
        transfer-encoding, and a length that contradicts the framing is a
        request some servers reject and others silently truncate.
        """
        import httpx

        if not source.exists():
            raise ValidationFailed(f"nothing to upload at {source}")
        size = source.stat().st_size

        async def chunks() -> Any:
            # Read on the loop thread. A desktop executor uploads one file at a
            # time and has nothing else to do meanwhile, so the pool this would
            # otherwise need would cost more than it saves.
            with source.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk

        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            response = await client.put(
                self.absolute(url),
                content=chunks(),
                headers={"content-type": "video/mp4"},
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"upload failed with {response.status_code}: {self._body(response)[:200]}"
            )
        return size


__all__ = [
    "CLAIM_WAIT_SECONDS",
    "IDLE_POLL_GROWTH",
    "IDLE_POLL_SECONDS",
    "MAX_IDLE_POLL_SECONDS",
    "idle_interval",
    "MAX_BACKOFF_SECONDS",
    "POLL_TIMEOUT",
    "UPLOAD_TIMEOUT",
    "DeviceClient",
    "backoff",
]
