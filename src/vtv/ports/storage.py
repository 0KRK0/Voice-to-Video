"""Object storage.

Application logic never opens a file and never builds a URL. It hands an
:class:`~vtv.contracts.base.ObjectRef` to this port and gets bytes, a stream, or
a time-limited signed URL back (Rule 14). The same code therefore works against a
local disk in development, an S3-compatible service in production, and whatever
replaces it in three years.

Retention is passed in at write time and enforced by the adapter through storage
lifecycle rules, so temporary voice recordings expire because the bucket says so
rather than because a cleanup job remembered to run (``docs/STORAGE_POLICY.md``).

For that to be true the adapter has to *keep* the class it was handed at write
time — next to the bytes, not only on the ``ObjectRef`` in the database. An
adapter that forgets it cannot expire anything correctly, which is why
:class:`SweepableStorage` below makes storing and reading it back an explicit
part of the contract rather than an assumption.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vtv.contracts.base import ObjectRef, RetentionClass

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    # Imported lazily so the port keeps its "adapters need no import from us
    # but this module" property at runtime; the policy is domain code.
    from vtv.retention import SweepPolicy


@runtime_checkable
class StorageProvider(Protocol):
    """Read and write blobs, without revealing where they live."""

    async def get_by_key(self, key: str) -> bytes:
        """Fetch by key alone, for callers that hold a job payload not a ref."""
        ...

    async def delete_by_key(self, key: str) -> None:
        """Remove an object. Must be idempotent."""
        ...

    async def delete_prefix(self, prefix: str) -> list[str]:
        """Remove every object under a prefix. Returns the keys removed.

        On the port because deletion has to be *complete* to be deletion: a
        project's bytes are spread across recordings, narration, assets and
        renders, and enumerating those key shapes at the call site is how one
        gets forgotten. The prefix is the tenant-namespaced project root, so a
        backend that has no directories (S3) implements this as a paginated
        list-and-delete and a backend that has directories removes the subtree.

        Must be idempotent: a prefix that is already gone returns an empty list.
        """
        ...

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        """Store bytes and return a reference to them.

        Implementations compute and record the SHA-256 checksum so that
        integrity is verifiable and identical content can be de-duplicated.
        """
        ...

    async def get(self, ref: ObjectRef) -> bytes:
        """Read an object in full.

        Raises :class:`~vtv.contracts.errors.NotFound` if it is gone, including
        when it has been swept by a retention policy — an expired object is a
        normal, expected outcome, not an exception to be surprised by.
        """
        ...

    async def stream(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        """Read an object in chunks. Used for anything media-sized."""
        ...

    async def exists(self, ref: ObjectRef) -> bool:
        ...

    async def delete(self, ref: ObjectRef) -> None:
        """Delete an object. Idempotent: deleting what is already gone succeeds.

        This is the operation behind a user's right to erasure, so it must be
        real deletion, not a tombstone.
        """
        ...

    async def signed_url(self, ref: ObjectRef, *, expires_in_seconds: int = 900) -> str:
        """A short-lived URL a browser can use directly.

        Short-lived by default: fifteen minutes is enough for a download and
        short enough that a leaked link is close to worthless.
        """
        ...

    async def classify(self, key: str, retention: RetentionClass) -> None:
        """Record an object's retention class after it was written.

        Only for writes that do not come through `put` — today, exactly one: a
        signed upload, where the bytes arrive over HTTP from a device. An object
        with no class is one `sweep_expired` will never touch, which is how a
        storage bill becomes permanent.
        """
        ...

    async def signed_upload_url(
        self,
        *,
        key: str,
        content_type: str,
        expires_in_seconds: int = 900,
        max_bytes: int | None = None,
    ) -> str:
        """A short-lived URL a browser can upload to directly.

        Audio goes from the microphone to storage without passing through our
        API. That removes a bandwidth bottleneck, and it means the request path
        that handles the largest, most sensitive payload in the product is one
        we do not operate.
        """
        ...


@runtime_checkable
class SweepableStorage(Protocol):
    """A backend whose objects can be swept *by policy* rather than by age.

    Separate from :class:`StorageProvider` on purpose. Writing and reading bytes
    is the minimum an adapter must do; deleting them on a schedule is a
    capability, and in production it is usually not this code's job at all — an
    S3 bucket expires objects with a lifecycle rule keyed on the retention tag
    and never calls any of this.

    The split matters because of what happens to a backend that cannot answer
    these questions. `RetentionService` refuses to sweep it rather than falling
    back to "delete everything older than N", which is the exact behaviour that
    deleted live projects' assets: retention class lives on the ``ObjectRef`` in
    the database, the sweep walks storage, and a walk that cannot recover the
    class must not delete.

    **What an adapter must guarantee.** The retention class passed to ``put`` is
    stored *with the object*, durably, and can be read back by key. Local disk
    keeps a sidecar in a parallel metadata tree; S3 keeps an object tag
    (``RETENTION_TAGS`` in ``adapters/storage/s3.py``) and reads it with
    ``GetObjectTagging``. An object whose class cannot be read is reported as
    unknown, never assumed.
    """

    async def retention_of(self, key: str) -> RetentionClass | None:
        """The class recorded for this object, or ``None`` if there is none.

        ``None`` must mean "not recorded", never "probably ephemeral". The
        caller treats it as undeletable, so a guess here is a deletion.
        """
        ...

    async def sweep_expired(
        self, *, policy: SweepPolicy, prefix: str | None = None
    ) -> list[str]:
        """Delete the objects ``policy`` permits deleting. Returns their keys.

        ``policy`` is mandatory and has no default: an adapter that could be
        called without one would grow a call site that does not pass one, and
        that call site would delete saved projects. The adapter supplies facts
        (key, age, recorded class) and does not decide.

        ``prefix`` scopes the sweep to one tenant's namespace. An unscoped sweep
        reachable from a request is how the retention endpoint used to delete
        other tenants' objects.
        """
        ...

    async def unclassified(self, *, prefix: str | None = None) -> list[str]:
        """Keys with no recorded retention class, which the sweep will not touch.

        The remedy for failing closed. Objects written before classes were
        recorded, or by a path that bypassed ``put``, are invisible to the sweep
        forever; this is how an operator finds them.
        """
        ...


__all__ = ["StorageProvider", "SweepableStorage"]
