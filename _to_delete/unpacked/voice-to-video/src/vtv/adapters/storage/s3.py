"""S3-compatible object storage.

STATUS: **SKELETON — REQUIRES `boto3` AND CREDENTIALS.**

Not executable in the current environment: neither `boto3` nor network access is
available, so this cannot be run or tested here. The structure is complete and
the method bodies state exactly what they must do; finishing it is an afternoon
once a bucket exists.

The reason it is written now rather than later is that it forces the port to be
honest. Every method below maps onto a single S3 call with no impedance
mismatch, which is evidence that ``StorageProvider`` is the right shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from vtv.contracts.base import ObjectRef, RetentionClass
from vtv.contracts.errors import ErrorCode, VTVError

#: Retention class → S3 object tag. A lifecycle rule on the bucket expires
#: objects by tag, so deletion is the storage system's job rather than a cron
#: task we have to remember to keep running (docs/STORAGE_POLICY.md).
RETENTION_TAGS: dict[RetentionClass, str] = {
    RetentionClass.EPHEMERAL: "retention=ephemeral",
    RetentionClass.PROJECT: "retention=project",
    RetentionClass.ARCHIVE: "retention=archive",
}


class S3StorageProvider:
    """Object storage against S3, R2, MinIO or any compatible endpoint."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.endpoint = endpoint
        self.region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._client: object | None = None

    def _require_client(self) -> object:
        # `_client` is always None until somebody implements this adapter; the
        # check is here so that the failure is a clear error rather than an
        # AttributeError three frames deeper.
        if self._client is None:
            raise VTVError(
                "S3StorageProvider is a skeleton: install boto3, supply "
                "credentials, and implement _require_client",
                code=ErrorCode.STORAGE_UNAVAILABLE,
            )
        return self._client

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        # put_object(Bucket, Key, Body, ContentType, Tagging=RETENTION_TAGS[...],
        #            ServerSideEncryption="AES256"); compute sha256 locally and
        #            return it on the ref so integrity is verifiable.
        self._require_client()
        raise NotImplementedError

    async def put_file(
        self,
        *,
        key: str,
        source: Path,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        # upload_file with multipart for anything over ~8 MB.
        self._require_client()
        raise NotImplementedError

    async def get(self, ref: ObjectRef) -> bytes:
        # get_object; NoSuchKey → NotFound(code=OBJECT_EXPIRED), because an
        # object swept by a lifecycle rule is an expected outcome, not a bug.
        self._require_client()
        raise NotImplementedError

    async def stream(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        self._require_client()
        raise NotImplementedError

    async def exists(self, ref: ObjectRef) -> bool:
        self._require_client()
        raise NotImplementedError

    async def delete(self, ref: ObjectRef) -> None:
        # delete_object is already idempotent in S3.
        self._require_client()
        raise NotImplementedError

    async def signed_url(self, ref: ObjectRef, *, expires_in_seconds: int = 900) -> str:
        # generate_presigned_url("get_object", ExpiresIn=expires_in_seconds)
        self._require_client()
        raise NotImplementedError

    async def signed_upload_url(
        self,
        *,
        key: str,
        content_type: str,
        expires_in_seconds: int = 900,
        max_bytes: int | None = None,
    ) -> str:
        # generate_presigned_post with a content-length-range condition, so the
        # size cap is enforced by S3 rather than trusted from the client.
        self._require_client()
        raise NotImplementedError


__all__ = ["RETENTION_TAGS", "S3StorageProvider"]
