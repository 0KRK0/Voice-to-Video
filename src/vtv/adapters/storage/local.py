"""Filesystem-backed object storage.

Real, complete, and the default in development. It implements the same
``StorageProvider`` port as the S3 adapter, so nothing above this line can tell
the difference — which is the point. The signed URL it hands out is an HMAC
token verified by the API, not a fake string: expiry and tampering behave the
way they will in production.

It also implements ``SweepableStorage``: the retention class handed to ``put``
is stored beside the bytes, so a sweep that walks the disk can recover it. It is
the local stand-in for an S3 object tag, and it exists because without it the
sweep had no way to tell a saved project's assets from a stale recording — and
deleted both.
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
from vtv.contracts.errors import ErrorCode, NotFound, PolicyViolation, VTVError
from vtv.retention import SweepPolicy
from vtv.security.paths import require_tenant_key, safe_storage_key

CHUNK = 1024 * 1024

#: Where the retention class of every object is kept, as a sibling of the bucket
#: directory rather than inside it.
#:
#: TRADEOFF ACCEPTED — a sidecar per object, so one stored object costs two
#: inodes and two writes. It was chosen over the alternatives because:
#:
#: * *Encoding the class in the key prefix* is free, but makes the class
#:   immutable after write. Promoting an `EPHEMERAL` recording to `PROJECT`
#:   when the user saves — the product's central action — would become a copy
#:   of every byte, and every `ObjectRef` already stored would name the old key.
#: * *Passing the set of live keys into the sweep* needs no on-disk metadata,
#:   but it is O(objects) in the caller's memory and it does not exist for S3,
#:   where expiry is a bucket lifecycle rule that never talks to us. The sweep
#:   does take live *project* ids, which is O(projects) and bounded.
#: * A sidecar maps exactly onto what production actually uses: an S3 object
#:   tag. Same fact, stored with the object, read back by key.
#:
#: WHAT WOULD CHANGE MY MIND: inode pressure showing up in a real deployment of
#: the local adapter. It is the development and single-node backend, where
#: doubling a few thousand inodes costs nothing; the day someone runs it over
#: millions of objects, the right answer is one metadata database (SQLite next
#: to the bucket) rather than one file per object.
#:
#: The tree is a *sibling* of the bucket, not interleaved with the objects, so
#: that nothing which walks the bucket — `delete_prefix`, the sweep, a media
#: route, a test counting artefacts — has to remember to filter sidecars out.
#: A rule that every walker must remember is the shape of defect this codebase
#: keeps finding.
META_ROOT = ".vtv-meta"


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

    # -- object metadata --------------------------------------------------

    def _meta_path(self, bucket: str, key: str) -> Path:
        """Where the sidecar for one object lives.

        Contained the same way as the object path: the key reaches a filesystem
        path here too, so it gets the same resolve-and-compare check rather than
        a second, weaker one.
        """
        base = (self.root / META_ROOT / bucket).resolve()
        target = (base / key).resolve()
        if not str(target).startswith(str(base) + os.sep):
            raise VTVError(
                f"object key escapes its bucket: {key!r}",
                code=ErrorCode.STORAGE_UNAVAILABLE,
            )
        return target

    def _write_meta(self, key: str, retention: RetentionClass) -> None:
        """Record an object's retention class before the object itself exists.

        Order matters. Metadata first means an object that is present always has
        a class; object first would leave a window in which a crash produces
        bytes nobody can classify, and an unclassifiable object is one the sweep
        will refuse to touch forever. A sidecar whose object never arrived is
        the harmless failure — the sweep prunes it.
        """
        path = self._meta_path(self.bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"retention": retention.value}), encoding="utf-8"
        )

    def _read_meta(self, key: str) -> RetentionClass | None:
        """The recorded class, or ``None`` — never a guess.

        Corrupt or unreadable metadata returns ``None`` for the same reason a
        missing sidecar does: the caller treats ``None`` as undeletable, so the
        worst case is an object that outlives its window and is reported by
        `unclassified()`. Reading a damaged file optimistically would make the
        worst case a deleted render.
        """
        path = self._meta_path(self.bucket, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return RetentionClass(payload["retention"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _delete_meta(self, key: str) -> None:
        self._meta_path(self.bucket, key).unlink(missing_ok=True)

    def writable(self) -> None:
        """Raise unless this replica can actually write an object.

        Readiness needs the answer to "can I store a render", and a mounted
        volume that has gone read-only or filled up answers that with an
        exception rather than with a missing directory. Writing and removing a
        probe file is the only check that distinguishes the two.
        """
        base = (self.root / self.bucket).resolve()
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".vtv-readiness"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)

    # -- StorageProvider --------------------------------------------------

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        require_tenant_key(key)
        target = self._resolve(self.bucket, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The class is recorded here, at the only chokepoint every write passes
        # through, rather than by each caller after the fact. A caller that
        # forgets to record it would produce an object the sweep can never
        # classify and therefore never delete.
        self._write_meta(key, retention)
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
        require_tenant_key(key)
        target = self._resolve(self.bucket, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_meta(key, retention)
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
        # The sidecar goes with the object, always. A metadata tree that
        # outlives its objects is how a "deleted" key comes back classified on
        # the next write and how the meta tree grows without bound.
        self._delete_meta(ref.key)

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
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> str:
        """A write permission, carrying how long what it writes may live.

        The retention travels *in the signed token* rather than being applied by
        whoever happens to handle the upload. Every other write in the system
        goes through `put`, which takes a retention class; a signed upload is
        the one path where the bytes arrive without passing through it, and it
        was landing them on disk with no class at all.

        An object with no retention class is one the sweep will never delete —
        `unclassified()` exists precisely to find them — so every video a
        customer's own computer rendered was being kept forever, by a system
        whose stated design is that it does not become a storage company.

        Putting it in the token means an upload URL cannot exist without one.
        """
        token = self._sign(
            self.bucket,
            key,
            expires_in_seconds,
            max_bytes=max_bytes,
            retention=retention,
        )
        return f"{self.public_base_url}/upload/{self.bucket}/{key}?token={token}"

    # -- signing ----------------------------------------------------------

    def _sign(
        self,
        bucket: str,
        key: str,
        expires_in_seconds: int,
        *,
        max_bytes: int | None = None,
        retention: RetentionClass | None = None,
    ) -> str:
        payload = {
            "b": bucket,
            "k": key,
            "e": int(time.time()) + expires_in_seconds,
        }
        if max_bytes is not None:
            payload["m"] = max_bytes
        if retention is not None:
            payload["r"] = retention.value
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

    async def get_by_key(self, key: str) -> bytes:
        """Fetch by key alone.

        The worker has a job payload, not an `ObjectRef`. Keeping the bytes in
        storage and the key in the queue is what lets a worker on another
        machine pick up work the API accepted — a local temp path could not.
        """
        safe_storage_key(key)
        path = self._resolve(self.bucket, key)
        if not path.exists():
            raise NotFound(f"no object at {key}", code=ErrorCode.OBJECT_EXPIRED)
        return path.read_bytes()

    async def delete_by_key(self, key: str) -> None:
        """Remove an object. Idempotent: deleting twice is not an error."""
        safe_storage_key(key)
        path = self._resolve(self.bucket, key)
        path.unlink(missing_ok=True)
        self._delete_meta(key)

    def _contained(self, prefix: str) -> Path:
        """Resolve a prefix and prove it is inside the bucket.

        The prefix is built from a tenant id and a project id, both of which
        originate outside this process. Re-resolving after joining is what makes
        a symlink or a `..` inside either of them a refusal rather than a
        deletion somewhere else on the disk.
        """
        require_tenant_key(prefix + "/x")
        base = (self.root / self.bucket).resolve()
        scoped = (base / prefix).resolve()
        if scoped != base and not scoped.is_relative_to(base):
            raise PolicyViolation("prefix escapes the bucket")
        return scoped

    async def delete_prefix(self, prefix: str) -> list[str]:
        """Delete every object under ``prefix``. Returns what went.

        P1-7. Deleting a project used to remove its database rows and leave
        every byte it had produced on disk: the video, the narration audio, the
        fetched assets. The customer was told their project was gone and it was
        not, which is a compliance problem before it is a storage-cost problem.

        Empty directories are removed too, so a tenant that deletes everything
        leaves no trace of *which* projects existed — directory names are data.
        """
        base = self._contained(prefix)
        # Metadata is removed whether or not the objects are still there: an
        # interrupted deletion can leave either half, and erasure has to be
        # complete in both trees for "the project is gone" to be true.
        meta = self._meta_path(self.bucket, prefix)
        if meta.exists():
            shutil.rmtree(meta)
        if not base.exists():
            return []

        removed: list[str] = []
        for path in sorted(base.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed.append(f"{prefix}/{path.relative_to(base)}")
            elif path.is_dir():
                path.rmdir()
        base.rmdir()
        return removed

    def _scoped_root(self, prefix: str | None) -> tuple[Path, Path]:
        """The bucket root and the subtree a sweep may touch.

        The prefix is derived from a tenant id, which is external input, so it
        is resolved and contained here rather than trusted. Both paths are
        returned because a decision needs the *full* key — the tenant and
        project it belongs to are in the part above the prefix.
        """
        root = (self.root / self.bucket).resolve()
        if not prefix:
            return root, root
        scoped = (root / prefix).resolve()
        if scoped != root and not scoped.is_relative_to(root):
            raise PolicyViolation("sweep prefix escapes the bucket")
        return root, scoped

    async def classify(self, key: str, retention: RetentionClass) -> None:
        """Record how long an object may live, after the fact.

        For the one write that does not come through `put`: a signed upload,
        where the bytes arrive over HTTP and the caller is a device. The class
        comes out of the signed token, so it is decided when the permission is
        granted and not by whatever handles the request.
        """
        self._write_meta(key, retention)

    async def retention_of(self, key: str) -> RetentionClass | None:
        """The class recorded for this object, or ``None`` if none was."""
        safe_storage_key(key)
        return self._read_meta(key)

    async def unclassified(self, *, prefix: str | None = None) -> list[str]:
        """Objects the sweep will never delete because their class is unknown."""
        root, base = self._scoped_root(prefix)
        if not base.exists():
            return []
        return sorted(
            str(path.relative_to(root))
            for path in base.rglob("*")
            if path.is_file()
            and self._read_meta(str(path.relative_to(root))) is None
        )

    async def sweep_expired(
        self, *, policy: SweepPolicy, prefix: str | None = None
    ) -> list[str]:
        """Delete the objects ``policy`` permits deleting. Returns their keys.

        **This method used to delete live projects' data.** Its body was an
        `mtime` comparison and an `unlink`, called from the retention job with a
        tenant prefix and a 24-hour window, so it removed every object in a
        tenant's namespace older than a day — `PROJECT` assets, captions and the
        renders of saved projects on a plan promising ten years of retention. It
        could not do otherwise: the retention class lived only on the
        `ObjectRef` in the database and this walks a filesystem.

        The class now lives beside the object (see `META_ROOT`) and the decision
        lives in `SweepPolicy`, which is a required argument. There is no
        parameter here that means "delete everything older than N", because the
        moment one exists something will pass it.

        In production this is a bucket lifecycle rule keyed on the retention
        tag and this method is not called at all. Locally there is no bucket to
        configure, so the sweep is explicit — and the policy is the same one
        either way.
        """
        now = time.time()
        root, base = self._scoped_root(prefix)
        if not base.exists():
            return []

        removed: list[str] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            key = str(path.relative_to(root))
            decision = policy.decide(
                key=key,
                age_seconds=now - path.stat().st_mtime,
                retention=self._read_meta(key),
            )
            if decision.delete:
                path.unlink()
                self._delete_meta(key)
                removed.append(key)

        self._prune_orphaned_metadata(prefix)
        return removed

    def _prune_orphaned_metadata(self, prefix: str | None) -> None:
        """Drop sidecars whose object is gone.

        Metadata is written before the object, and objects can also be removed
        by something other than this adapter (an operator, a restore). Without
        this the meta tree only ever grows, and a key that is written, deleted
        outside the adapter and written again would inherit a stale class.

        The known race: a sweep landing between `_write_meta` and the object
        write in `put` prunes a sidecar whose object is about to appear, leaving
        it unclassified. Not fixed here because the outcome is an object that is
        kept rather than deleted and that `unclassified()` reports. Fixing it
        properly means a real transaction over both trees, which is the point at
        which the sidecar should become a metadata database instead.
        """
        meta_root = (self.root / META_ROOT / self.bucket).resolve()
        base = meta_root if not prefix else (meta_root / prefix).resolve()
        if not base.exists() or (base != meta_root and not base.is_relative_to(meta_root)):
            return
        for path in base.rglob("*"):
            if path.is_file():
                key = str(path.relative_to(meta_root))
                if not self._resolve(self.bucket, key).exists():
                    path.unlink()


__all__ = ["LocalStorageProvider"]
