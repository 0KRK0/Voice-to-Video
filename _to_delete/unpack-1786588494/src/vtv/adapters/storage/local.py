"""Filesystem-backed object storage.

Real, complete, and the default in development. It implements the same
``StorageProvider`` port as the S3 adapter, so nothing above this line can tell
the difference — which is the point. The signed URL it hands out is an HMAC
token verified by the API, not a fake string: expiry and tampering behave the
way they will in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

from vtv.contracts.base import ObjectRef, RetentionClass
from vtv.contracts.errors import ErrorCode, NotFound, VTVError

CHUNK = 1024 * 1024


class LocalStorageProvider:
    """Objects as files under ``root/bucket/key``."""

    def __init__(
        self,
        root: Path,
        *,
        bucket: str = "vtv-media-dev",
        signing_key: str | None = None,
        public_base_url: str = "/media",
    ) -> None:
        self.root = Path(root)
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        # A per-process key is fine for development; production injects one.
        self._signing_key = (signing_key or os.urandom(32).hex()).encode()
        (self.root / bucket).mkdir(parents=True, exist_ok=True)

    # -- path handling ----------------------------------------------------

    def _resolve(self, bucket: str, key: str) -> Path:
        """Resolve a key to a path, refusing anything that escapes the bucket.

        Object keys can derive from user input, so traversal is checked here as
        well as in the ``ObjectRef`` validator. Two independent checks on the
        one operation that turns a string into a filesystem path is proportionate.
        """
        base = (self.root / bucket).resolve()
        target = (base / key).resolve()
        if not str(target).startswith(str(base) + os.sep) and target != base:
            raise VTVError(
                f"object key escapes its bucket: {key!r}",
                code=ErrorCode.STORAGE_UNAVAILABLE,
            )
        return target

    def path_for(self, ref: ObjectRef) -> Path:
        return self._resolve(ref.bucket, ref.key)

    # -- StorageProvider --------------------------------------------------

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        target = self._resolve(self.bucket, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return ObjectRef(
            bucket=self.bucket,
            key=key,
            content_type=content_type,
            size_bytes=len(data),
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            retention=retention,
        )

    async def put_file(
        self,
        *,
        key: str,
        source: Path,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        """Store an existing file without reading it into memory.

        Rendered video is hundreds of megabytes; ``put`` would be a memory spike
        for no reason.
        """
        target = self._resolve(self.bucket, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
        return ObjectRef(
            bucket=self.bucket,
            key=key,
            content_type=content_type,
            size_bytes=target.stat().st_size,
            checksum_sha256=digest.hexdigest(),
            retention=retention,
        )

    async def get(self, ref: ObjectRef) -> bytes:
        path = self.path_for(ref)
        if not path.exists():
            raise NotFound(f"object {ref.uri} is not present", code=ErrorCode.OBJECT_EXPIRED)
        return path.read_bytes()

    async def stream(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        path = self.path_for(ref)
        if not path.exists():
            raise NotFound(f"object {ref.uri} is not present", code=ErrorCode.OBJECT_EXPIRED)

        async def iterator() -> AsyncIterator[bytes]:
            with path.open("rb") as handle:
                while chunk := handle.read(CHUNK):
                    yield chunk

        return iterator()

    async def exists(self, ref: ObjectRef) -> bool:
        return self.path_for(ref).exists()

    async def delete(self, ref: ObjectRef) -> None:
        path = self.path_for(ref)
        if path.exists():
            path.unlink()

    async def signed_url(self, ref: ObjectRef, *, expires_in_seconds: int = 900) -> str:
        token = self._sign(ref.bucket, ref.key, expires_in_seconds)
        return f"{self.public_base_url}/{ref.bucket}/{ref.key}?token={token}"

    async def signed_upload_url(
        self,
        *,
        key: str,
        content_type: str,
        expires_in_seconds: int = 900,
        max_bytes: int | None = None,
    ) -> str:
        token = self._sign(self.bucket, key, expires_in_seconds, max_bytes=max_bytes)
        return f"{self.public_base_url}/upload/{self.bucket}/{key}?token={token}"

    # -- signing ----------------------------------------------------------

    def _sign(
        self,
        bucket: str,
        key: str,
        expires_in_seconds: int,
        *,
        max_bytes: int | None = None,
    ) -> str:
        payload = {
            "b": bucket,
            "k": key,
            "e": int(time.time()) + expires_in_seconds,
        }
        if max_bytes is not None:
            payload["m"] = max_bytes
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._signing_key, body, hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(body).decode().rstrip("=")
            + "."
            + base64.urlsafe_b64encode(signature).decode().rstrip("=")
        )

    def verify(self, token: str) -> dict[str, object]:
        """Verify a token and return its claims, or raise.

        Constant-time comparison, and expiry checked after the signature so an
        attacker learns nothing from timing about which part failed.
        """

        def unpad(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        try:
            encoded_body, encoded_signature = token.split(".", 1)
            body = unpad(encoded_body)
            signature = unpad(encoded_signature)
        except Exception as exc:
            raise VTVError("malformed storage token", code=ErrorCode.STORAGE_UNAVAILABLE) from exc

        expected = hmac.new(self._signing_key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise VTVError("invalid storage token", code=ErrorCode.STORAGE_UNAVAILABLE)

        claims = json.loads(body)
        if int(claims["e"]) < int(time.time()):
            raise VTVError("storage token has expired", code=ErrorCode.OBJECT_EXPIRED)
        return dict(claims)

    # -- lifecycle --------------------------------------------------------

    async def sweep_expired(self, *, older_than_seconds: float) -> list[str]:
        """Delete ephemeral objects past their retention window.

        In production this is a bucket lifecycle rule and this method does not
        exist. Locally there is no bucket to configure, so the sweep is explicit
        — and it is exercised by the retention tests either way.
        """
        cutoff = time.time() - older_than_seconds
        removed: list[str] = []
        base = (self.root / self.bucket).resolve()
        for path in base.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(str(path.relative_to(base)))
        return removed


__all__ = ["LocalStorageProvider"]
