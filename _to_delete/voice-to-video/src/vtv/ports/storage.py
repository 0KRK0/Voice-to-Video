"""Object storage.

Application logic never opens a file and never builds a URL. It hands an
:class:`~vtv.contracts.base.ObjectRef` to this port and gets bytes, a stream, or
a time-limited signed URL back (Rule 14). The same code therefore works against a
local disk in development, an S3-compatible service in production, and whatever
replaces it in three years.

Retention is passed in at write time and enforced by the adapter through storage
lifecycle rules, so temporary voice recordings expire because the bucket says so
rather than because a cleanup job remembered to run (``docs/STORAGE_POLICY.md``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from vtv.contracts.base import ObjectRef, RetentionClass


@runtime_checkable
class StorageProvider(Protocol):
    """Read and write blobs, without revealing where they live."""

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


__all__ = ["StorageProvider"]
