"""A customer's own computer, as something the system can send work to.

## Why this is not an API key

An `ApiKey` is a machine acting *as the organisation*: it can create projects,
submit renders, and spend money. A device is the opposite shape. It is a
computer somebody left running in an office, possibly shared, possibly stolen,
and it needs exactly one power: **draw the frames of a job it has been given**.

So a device credential grants `DEVICE_EXECUTE` and nothing else, and even that
is not enough on its own. Holding the capability means "may execute jobs";
holding a `JobLease` means "may execute *this* job". Both are required, and they
are checked in different places on purpose — a stolen device token with no lease
can do nothing but ask for work that will not be offered to it.

## Why a lease and not a flag on the job

Because the failure that matters is not "an attacker takes a job". It is a
laptop that is closed mid-render. If the job simply said "device X has this",
the render would be stuck until a human noticed. A lease expires, so a machine
that goes quiet gives the work back without anybody intervening, and the
cloud — or another device — picks it up.

That is also why the lease is renewed by progress rather than by a separate
heartbeat. A device that is still reporting frames is a device that is still
working; one that is running but wedged should lose the job, and a heartbeat on
its own timer would keep telling us it is fine.

## What a device is never sent

Provider credentials. Not the OpenAI key, not the search keys, not the storage
credentials. A device receives a resolved `Timeline`, the settings, and
short-lived URLs for the specific assets that job needs. Everything that costs
money or carries a secret happens server-side, before the assignment is written.

This is the line that makes "use your own GPU" safe to offer at all: the
customer's machine gets pixels and a plan, never the keys to the account.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.execution import ExecutionTarget
from vtv.contracts.render import RenderSettings
from vtv.contracts.timeline import Timeline

#: How long a pairing code is worth typing. Short, because it is six characters
#: of shared secret displayed on a screen: the whole security of the pairing
#: step is that the window is small and the code is single-use.
PAIRING_TTL_SECONDS = 10 * 60

#: How long a claimed job stays claimed without progress. Longer than the
#: slowest segment anybody should be encoding, short enough that a closed laptop
#: does not strand a render for the rest of the afternoon.
LEASE_TTL_SECONDS = 5 * 60

#: Below this, a device that has not been heard from is shown as offline. It is
#: a display fact, not an authorisation one — an offline device is simply one
#: that will not be offered work.
ONLINE_WITHIN_SECONDS = 90


class DeviceState(str, Enum):
    """What a device is doing, as far as the server can tell."""

    #: Paired, reachable, not currently holding a job.
    IDLE = "idle"
    #: Holding a lease and reporting progress.
    BUSY = "busy"
    #: Paired but not heard from recently.
    OFFLINE = "offline"
    #: Deliberately unpaired. Terminal; a revoked device must pair again.
    REVOKED = "revoked"


class DeviceHardware(VTVModel):
    """What a machine actually has, as measured on that machine.

    Every field here is observed rather than declared. The distinction is the
    whole point: a device that *says* it has a graphics card is a device that
    will be sent GPU work and fail it, and the customer will experience that as
    the product being broken rather than as their driver being broken.

    `gpu_verified` is the one that matters, and it is not "a card was found". It
    is "this card drew the twenty-three calibration scenes and matched the
    reference painter", which is the same bar the renderer applies before it
    will use a card in-process.
    """

    platform: str = Field(default="", max_length=200)
    cpu_cores: int = Field(default=1, ge=1, le=1024)
    memory_mb: int = Field(default=0, ge=0)

    #: Empty when no usable device was found.
    gpu_name: str = Field(default="", max_length=200)
    gpu_api: str = Field(default="", max_length=40)
    #: Whether the card passed the equivalence check on this machine. A card
    #: that is present and wrong is worse than no card, so this stays false
    #: until it has been proven, and the renderer refuses the target until then.
    gpu_verified: bool = False
    #: Why not, when not. Shown to the user, so it must be a sentence and not a
    #: stack trace: "8 of 23 scenes differ" is actionable, "GLError" is not.
    gpu_reason: str = Field(default="", max_length=400)

    #: Measured composition throughput, frames per second, by content kind.
    #: Absent until the machine has actually been timed — see
    #: `RenderCapabilities.frames_per_second` for why this is not zero.
    measured_fps: dict[str, float] = Field(default_factory=dict)

    @property
    def targets(self) -> tuple[ExecutionTarget, ...]:
        """What this machine may actually be asked to run.

        The graphics card appears only when it has been verified. This is the
        chokepoint for "may we use the GPU on this machine": nothing else in the
        system is allowed to decide it, because everything else would decide it
        from the card's *name*.
        """
        if self.gpu_verified and self.gpu_name:
            return (ExecutionTarget.LOCAL_GPU, ExecutionTarget.LOCAL_CPU)
        return (ExecutionTarget.LOCAL_CPU,)

    @property
    def summary(self) -> str:
        """One line for a device list in the UI."""
        cores = f"{self.cpu_cores} core{'s' if self.cpu_cores != 1 else ''}"
        if self.gpu_verified and self.gpu_name:
            return f"{cores}, {self.gpu_name}"
        if self.gpu_name:
            return f"{cores}, {self.gpu_name} (unverified)"
        return f"{cores}, no usable graphics device"


class Device(RootDocument, Timestamped):
    """One paired computer.

    The token is stored the way an API key's secret is stored: a public prefix
    in the clear so a human can say "revoke the one starting vtv_dev_7f3a", and
    a digest for the rest. A dump of this table is not a list of working
    credentials.
    """

    document_name = "device"

    device_id: Id = Field(default_factory=lambda: new_id(IdPrefix.DEVICE))
    organisation_id: Id
    #: What the person called it. "Konar's desktop", not a serial number.
    name: str = Field(min_length=1, max_length=120)
    hardware: DeviceHardware = Field(default_factory=DeviceHardware)

    #: Public identifying prefix, e.g. ``vtv_dev_7f3a``.
    prefix: str = Field(min_length=4, max_length=32)
    #: Hex SHA-256 of the full token. Comparison is constant-time.
    token_hash: str = Field(min_length=64, max_length=128)

    #: The user who typed the pairing code. Recorded because "who put a computer
    #: on our account" is a question an administrator will eventually ask.
    paired_by: Id | None = None
    paired_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def state(self, *, holding_job: bool = False, now: datetime | None = None) -> DeviceState:
        from vtv.contracts.base import utc_now

        if self.revoked_at is not None:
            return DeviceState.REVOKED
        moment = now or utc_now()
        if self.last_seen_at is None:
            return DeviceState.OFFLINE
        if (moment - self.last_seen_at).total_seconds() > ONLINE_WITHIN_SECONDS:
            return DeviceState.OFFLINE
        return DeviceState.BUSY if holding_job else DeviceState.IDLE


class PairingCode(RootDocument, Timestamped):
    """A short-lived, single-use code that turns into a device token.

    ## Why a code and not a pasted token

    Because the alternative puts a long-lived account-wide credential into a
    desktop app's config file, and revoking one machine then means revoking
    every machine. Here the desktop never sees an account credential at all: it
    sees six characters that are worth nothing ten minutes later and nothing at
    all once used.

    The code is stored in the clear, deliberately, unlike every other credential
    in this system. It is not a secret in the same sense — it is worthless
    without also being redeemed inside its window, it grants only the ability to
    *become* a device, and the person reading it off their own screen needs to
    compare it with what they typed. Hashing it would make "that code is already
    used" impossible to say.
    """

    document_name = "pairing_code"

    #: Human-typable: uppercase, no ambiguous characters. Grouped for reading
    #: aloud, which is how half of these will be transferred.
    code: str = Field(min_length=6, max_length=16)
    organisation_id: Id
    created_by: Id | None = None
    expires_at: datetime
    #: Set when redeemed. Single use is enforced on this field.
    consumed_at: datetime | None = None
    device_id: Id | None = None

    @property
    def is_redeemable(self) -> bool:
        from vtv.contracts.base import utc_now

        return self.consumed_at is None and self.expires_at > utc_now()


class JobLease(VTVModel):
    """One device's claim on one render job, for a bounded time.

    Expiry is the whole design. A lease that had to be released explicitly would
    strand a render every time a laptop was closed, and "explicitly released" is
    exactly what a crashed process cannot do.
    """

    render_job_id: Id
    device_id: Id
    organisation_id: Id
    granted_at: datetime
    expires_at: datetime
    #: Renewed by progress, not by a timer of its own — a device that is still
    #: reporting frames is still working, and a heartbeat would keep insisting a
    #: wedged process was healthy.
    segments_done: int = Field(default=0, ge=0)
    segments_total: int = Field(default=0, ge=0)

    @property
    def is_live(self) -> bool:
        from vtv.contracts.base import utc_now

        return self.expires_at > utc_now()

    def renewed(self, *, seconds: int = LEASE_TTL_SECONDS) -> JobLease:
        from vtv.contracts.base import utc_now

        return self.model_copy(
            update={"expires_at": utc_now() + timedelta(seconds=seconds)}
        )


class AssetHandout(VTVModel):
    """One file a device needs, and how to prove it arrived intact.

    The digest is not belt and braces. A truncated download is the most likely
    thing to go wrong on a domestic connection, and a half-written JPEG does not
    raise — it decodes to a grey band across the bottom of somebody's video. The
    device verifies before it caches, and a mismatch is a re-download rather
    than a render.
    """

    #: The object exactly as the timeline refers to it.
    #:
    #: The whole reference and not just a key, because that is what lets the
    #: device use the *real* renderer unchanged. It places the bytes where a
    #: `LocalStorageProvider` would put them for this ref, hands that provider
    #: to `FfmpegRenderer`, and the renderer materialises assets by the same
    #: code path it uses in the cloud. A device with its own asset-resolution
    #: logic would be a second implementation of the one thing that must not
    #: differ between where a video is drawn and where it is drawn instead.
    object: ObjectRef
    #: Where to get the bytes. Expiring, single-purpose, read-only, and scoped
    #: to this object — not a credential, and useless for reaching anything else.
    url: str = Field(min_length=1, max_length=4096)
    #: Computed server-side from the same object the cloud renderer would have
    #: used. Required here even though `ObjectRef.checksum_sha256` is optional:
    #: an asset a device cannot verify is an asset it must not draw from.
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)

    @property
    def key(self) -> str:
        return self.object.key


class Assignment(VTVModel):
    """A whole job, handed to a device, with no credentials in it.

    Everything the device needs to draw the video and nothing else: the resolved
    timeline, the settings, and time-limited URLs for exactly the assets this
    job references. No provider keys, no storage credentials, no access to any
    other project.
    """

    render_job_id: Id
    project_id: Id
    organisation_id: Id
    timeline: Timeline
    settings: RenderSettings
    assets: tuple[AssetHandout, ...] = ()
    #: Whether there is anywhere to upload the finished video at all.
    #:
    #: **Not the URL.** This carried the signed upload URL, minted when the job
    #: was claimed and good for an hour — which is fine for a thirty-minute
    #: video and impossible for a longer one. A real sixty-minute render on a
    #: GTX 1650 took seventy-one minutes and then died with
    #: `403 object_expired`, having drawn every frame correctly. The URL had
    #: gone stale while the machine was busy earning it.
    #:
    #: So the device asks for a URL at the moment it is ready to upload, from
    #: `POST /v1/devices/upload-url`, and the hour it is good for is an hour
    #: that starts when it is needed. A URL that expires during the work it was
    #: issued for is not a security boundary, it is a time bomb with a
    #: certificate.
    #:
    #: This flag remains because a deployment whose storage cannot sign uploads
    #: has nowhere for a device to put anything, and the device should know that
    #: before it spends an hour rendering rather than after.
    can_upload: bool = True
    lease: JobLease

    @property
    def total_bytes(self) -> int:
        return sum(asset.size_bytes for asset in self.assets)


class ProgressReport(VTVModel):
    """What a device says while it is working.

    Segments rather than a percentage, because a segment is a real, verifiable
    thing that exists on disk, and a percentage is a number a UI can be made to
    show without anything having happened. The fraction is derived here rather
    than sent, so a device cannot report ninety percent while producing nothing.
    """

    render_job_id: Id
    device_id: Id
    segments_done: int = Field(ge=0)
    segments_total: int = Field(ge=0)
    #: Which hardware actually drew them, counted. The answer to "did my
    #: graphics card do this", from the only place that knows.
    backends: dict[str, int] = Field(default_factory=dict)
    message: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _done_within_total(self) -> ProgressReport:
        if self.segments_total and self.segments_done > self.segments_total:
            raise ValueError("a device cannot finish more segments than exist")
        return self

    @property
    def fraction(self) -> float:
        if not self.segments_total:
            return 0.0
        return min(1.0, self.segments_done / self.segments_total)


class Completion(VTVModel):
    """How a job ended on a device.

    Carries `elsewhere` for the same reason `SegmentOutcome` does: "this machine
    cannot do this" and "this cannot be done" need different answers, and only
    the machine that failed knows which it was. A device that ran out of video
    memory should hand the job back; a device given a timeline referencing a
    missing asset should not, because every other machine will fail identically.
    """

    render_job_id: Id
    device_id: Id
    ok: bool
    #: Empty when ok. One line, safe to show a user.
    reason: str = Field(default="", max_length=400)
    #: Whether the job is worth offering to different hardware.
    elsewhere: bool = False
    output_bytes: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    render_seconds: float = Field(default=0.0, ge=0.0)
    backends: dict[str, int] = Field(default_factory=dict)


__all__ = [
    "LEASE_TTL_SECONDS",
    "ONLINE_WITHIN_SECONDS",
    "PAIRING_TTL_SECONDS",
    "AssetHandout",
    "Assignment",
    "Completion",
    "Device",
    "DeviceHardware",
    "DeviceState",
    "JobLease",
    "PairingCode",
    "ProgressReport",
]
