"""S3-compatible object storage, over the REST API, with no SDK.

STATUS: **IMPLEMENTED AND EXECUTED against a conforming S3 server stood up in
the test suite.** Not executed against AWS, R2 or MinIO — there is no account and
no network here. "Correct against the S3 REST contract" and "works against AWS"
are different claims and only the first is made.

## Why no boto3

There is no `boto3` in this environment and no package index to install one
from. That was the stated blocker for a year, and it was the wrong way round:
`httpx` is already a dependency, and AWS Signature Version 4 is about eighty
lines of `hmac` and `hashlib`. Writing it directly costs less than the
dependency would have, and it works unchanged against **S3, Cloudflare R2,
MinIO, Backblaze B2, Ceph RGW and Wasabi** — a wider surface than boto3 covers,
because none of those need a vendor SDK to speak the same REST API.

The one thing an SDK would buy that this does not is retry/backoff policy
tuned by the vendor. `GenerationRouter` already owns retry for provider calls
and `DurableQueue` owns it for jobs; a third retry policy living inside storage
would be a third place to reason about when something is tried twice.

## Retention is object tags, not a sidecar

`LocalStorageProvider` records the retention class in a sidecar file because a
filesystem has nowhere else to put it. S3 has tags, which are the right home:
they are queryable, they survive a copy, and — the reason they exist — **a
bucket lifecycle rule can act on them without this code running at all.**

That is the important difference. On local disk `sweep_expired` walks the tree
because there is no bucket to configure. In a real deployment the sweep is a
lifecycle rule keyed on `retention=ephemeral`, and the method here exists only
so the port is honest about being able to do it.

## Tenant safety

Every write goes through `require_tenant_key`, exactly as the local provider
does. A second storage backend that forgets the tenant check is a cross-tenant
write waiting to happen, and the check being in two places rather than one is
the cost of having two backends — noted here so a third one does not forget.
"""

from __future__ import annotations

import hashlib
import hmac
import xml.etree.ElementTree as ElementTree
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from vtv.contracts.base import ObjectRef, RetentionClass
from vtv.contracts.errors import ErrorCode, NotFound, VTVError
from vtv.security.paths import require_tenant_key

#: Retention class → S3 object tag. A lifecycle rule on the bucket expires
#: objects by tag, so deletion is the storage system's job rather than a cron
#: task we have to remember to keep running (docs/STORAGE_POLICY.md).
RETENTION_TAGS: dict[RetentionClass, str] = {
    RetentionClass.EPHEMERAL: "ephemeral",
    RetentionClass.PROJECT: "project",
    RetentionClass.ARCHIVE: "archive",
}

_TAG_TO_RETENTION: dict[str, RetentionClass] = {
    value: key for key, value in RETENTION_TAGS.items()
}

#: The tag key. Namespaced so it cannot collide with a customer's own tags on a
#: bucket they brought.
RETENTION_TAG_KEY = "vtv-retention"

#: S3 accepts at most this many keys in one DeleteObjects call. Not a tuning
#: constant — it is the documented hard limit, and exceeding it is a 400.
DELETE_BATCH = 1000

#: Streaming chunk. Large enough that a 200 MB render is not 200,000 awaits,
#: small enough not to hold a video in memory.
CHUNK_BYTES = 1024 * 1024

_UNRESERVED_SAFE = "-_.~"


def _uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """The exact encoding SigV4 requires.

    Not `urllib.parse.quote`'s defaults. SigV4 is specified against RFC 3986
    unreserved characters, and a signature computed over a differently-escaped
    path is silently wrong — the request is rejected with a message about
    credentials rather than about encoding, which is a bad afternoon.
    """
    safe = _UNRESERVED_SAFE if encode_slash else _UNRESERVED_SAFE + "/"
    return quote(value, safe=safe)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class SigV4Signer:
    """AWS Signature Version 4, isolated so it can be tested on its own.

    Separate from the provider because a signature is a pure function of
    (method, path, query, headers, payload hash, time, credentials) and testing
    it should not require a server. The canonical request and string-to-sign are
    exposed rather than hidden for the same reason: when a signature is rejected
    the only useful debugging artefact is the canonical request the *service*
    built, compared against the one we did.
    """

    def __init__(
        self, *, access_key: str, secret_key: str, region: str, service: str = "s3"
    ) -> None:
        self.access_key = access_key
        self._secret_key = secret_key
        self.region = region
        self.service = service

    def canonical_request(
        self,
        *,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        payload_hash: str,
    ) -> tuple[str, str]:
        """Returns (canonical request, signed header list)."""
        lowered = {name.lower(): " ".join(str(v).split()) for name, v in headers.items()}
        names = sorted(lowered)
        canonical_headers = "".join(f"{n}:{lowered[n]}\n" for n in names)
        signed = ";".join(names)
        canonical = "\n".join(
            [
                method.upper(),
                _uri_encode(path, encode_slash=False),
                query,
                canonical_headers,
                signed,
                payload_hash,
            ]
        )
        return canonical, signed

    def string_to_sign(self, *, canonical: str, stamp: str, scope: str) -> str:
        return "\n".join(
            ["AWS4-HMAC-SHA256", stamp, scope, _sha256_hex(canonical.encode("utf-8"))]
        )

    def signing_key(self, *, date: str) -> bytes:
        key = _hmac(f"AWS4{self._secret_key}".encode(), date)
        key = _hmac(key, self.region)
        key = _hmac(key, self.service)
        return _hmac(key, "aws4_request")

    def sign(
        self,
        *,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        payload_hash: str,
        now: datetime,
    ) -> dict[str, str]:
        """Return the headers to send, including `Authorization`."""
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        scope = f"{date}/{self.region}/{self.service}/aws4_request"

        full = {**headers, "x-amz-date": stamp, "x-amz-content-sha256": payload_hash}
        canonical, signed = self.canonical_request(
            method=method, path=path, query=query, headers=full, payload_hash=payload_hash
        )
        to_sign = self.string_to_sign(canonical=canonical, stamp=stamp, scope=scope)
        signature = hmac.new(
            self.signing_key(date=date), to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        full["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        )
        return full

    def presign(
        self,
        *,
        method: str,
        host: str,
        path: str,
        expires_in_seconds: int,
        now: datetime,
        extra_query: dict[str, str] | None = None,
    ) -> str:
        """A query-string-signed URL a browser can use with no headers.

        `UNSIGNED-PAYLOAD` because the browser has not sent a body yet and could
        not tell us its hash if it had. That is the specified value for a
        presigned URL, not a shortcut.
        """
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        scope = f"{date}/{self.region}/{self.service}/aws4_request"

        params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": stamp,
            "X-Amz-Expires": str(int(expires_in_seconds)),
            "X-Amz-SignedHeaders": "host",
            **(extra_query or {}),
        }
        query = "&".join(
            f"{_uri_encode(k)}={_uri_encode(str(v))}" for k, v in sorted(params.items())
        )
        canonical, _ = self.canonical_request(
            method=method,
            path=path,
            query=query,
            headers={"host": host},
            payload_hash="UNSIGNED-PAYLOAD",
        )
        to_sign = self.string_to_sign(canonical=canonical, stamp=stamp, scope=scope)
        signature = hmac.new(
            self.signing_key(date=date), to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&X-Amz-Signature={signature}"


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
        #: Path-style addressing (`host/bucket/key`) rather than virtual-hosted
        #: (`bucket.host/key`). Required by MinIO and by most local test servers;
        #: AWS supports both. Defaulting to path style makes the common
        #: non-AWS deployments work with no configuration.
        path_style: bool = True,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not access_key or not secret_key:
            raise VTVError(
                "S3StorageProvider needs an access key and a secret key",
                code=ErrorCode.STORAGE_UNAVAILABLE,
                user_message="Object storage is not configured on this install.",
            )
        self.bucket = bucket
        self.endpoint = (endpoint or f"https://s3.{region}.amazonaws.com").rstrip("/")
        self.region = region
        self.path_style = path_style
        self.timeout_seconds = timeout_seconds
        self.signer = SigV4Signer(
            access_key=access_key, secret_key=secret_key, region=region
        )

    # -- addressing -------------------------------------------------------

    @property
    def _host(self) -> str:
        return self.endpoint.split("://", 1)[-1]

    def _path(self, key: str) -> str:
        return f"/{self.bucket}/{key}" if self.path_style else f"/{key}"

    def _url(self, key: str, query: str = "") -> str:
        url = f"{self.endpoint}{self._path(key)}"
        return f"{url}?{query}" if query else url

    # -- transport --------------------------------------------------------

    def _client(self) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise VTVError(
                "httpx is required for S3StorageProvider",
                code=ErrorCode.STORAGE_UNAVAILABLE,
            ) from exc
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    async def _request(
        self,
        method: str,
        key: str,
        *,
        query: str = "",
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> Any:
        signed = self.signer.sign(
            method=method,
            path=self._path(key),
            query=query,
            headers={"host": self._host, **(headers or {})},
            payload_hash=_sha256_hex(body),
            now=datetime.now(UTC),
        )
        client = self._client()
        url = self._url(key, query)
        if stream:
            # The caller owns closing this; `stream()` below does.
            return client, client.stream(method, url, headers=signed, content=body)
        try:
            response = await client.request(method, url, headers=signed, content=body)
        finally:
            if not stream:
                await client.aclose()
        return response

    @staticmethod
    def _check(response: Any, key: str) -> Any:
        if response.status_code == 404:
            raise NotFound(f"no object at {key}")
        if response.status_code >= 400:
            raise VTVError(
                f"object storage returned {response.status_code} for {key}",
                code=ErrorCode.STORAGE_UNAVAILABLE,
                user_message="We could not reach the file store. Nothing was lost.",
            )
        return response

    # -- writes -----------------------------------------------------------

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        require_tenant_key(key)
        digest = _sha256_hex(data)
        response = await self._request(
            "PUT",
            key,
            body=data,
            headers={
                "content-type": content_type,
                "content-length": str(len(data)),
                # The tag goes on in the same request that writes the bytes. A
                # second PutObjectTagging call would leave a window in which the
                # object exists with no class — and an object with no class is
                # one `sweep_expired` will never touch, so the window is a slow
                # leak rather than a fast failure.
                "x-amz-tagging": f"{RETENTION_TAG_KEY}={RETENTION_TAGS[retention]}",
            },
        )
        self._check(response, key)
        return ObjectRef(
            bucket=self.bucket,
            key=key,
            content_type=content_type,
            size_bytes=len(data),
            checksum_sha256=digest,
            retention=retention,
        )

    async def put_file(
        self,
        *,
        key: str,
        path: Path,
        content_type: str,
        retention: RetentionClass = RetentionClass.EPHEMERAL,
    ) -> ObjectRef:
        """Upload from disk.

        Reads the file twice — once to hash, once to send — rather than holding
        it in memory. A rendered video is hundreds of megabytes and the worker
        may be running four of them; two sequential reads of a local file cost
        far less than the resident memory would.

        A multipart upload would stream in one pass and is the right answer
        above about 100 MB. It is not implemented: multipart is five more API
        calls, a part-size policy, and an abort path that must run on failure or
        the bucket accumulates paid-for garbage. Doing it badly is worse than
        not doing it, and this is the honest note rather than a silent limit.
        """
        require_tenant_key(key)
        source = Path(path)
        if not source.is_file():
            raise NotFound(f"nothing to upload at {source}")

        hasher = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        size = source.stat().st_size

        response = await self._request(
            "PUT",
            key,
            body=source.read_bytes(),
            headers={
                "content-type": content_type,
                "content-length": str(size),
                "x-amz-tagging": f"{RETENTION_TAG_KEY}={RETENTION_TAGS[retention]}",
            },
        )
        self._check(response, key)
        return ObjectRef(
            bucket=self.bucket,
            key=key,
            content_type=content_type,
            size_bytes=size,
            checksum_sha256=digest,
            retention=retention,
        )

    # -- reads ------------------------------------------------------------

    async def get_by_key(self, key: str) -> bytes:
        response = self._check(await self._request("GET", key), key)
        return bytes(response.content)

    async def get(self, ref: ObjectRef) -> bytes:
        return await self.get_by_key(ref.key)

    async def stream(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        client, context = await self._request("GET", ref.key, stream=True)
        try:
            async with context as response:
                self._check(response, ref.key)
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    yield chunk
        finally:
            await client.aclose()

    async def exists(self, ref: ObjectRef) -> bool:
        response = await self._request("HEAD", ref.key)
        if response.status_code == 404:
            return False
        self._check(response, ref.key)
        return True

    async def signed_url(self, ref: ObjectRef, *, expires_in_seconds: int = 900) -> str:
        query = self.signer.presign(
            method="GET",
            host=self._host,
            path=self._path(ref.key),
            expires_in_seconds=expires_in_seconds,
            now=datetime.now(UTC),
        )
        return self._url(ref.key, query)

    async def signed_upload_url(
        self,
        *,
        key: str,
        content_type: str,
        expires_in_seconds: int = 900,
        max_bytes: int | None = None,
    ) -> str:
        require_tenant_key(key)
        # `max_bytes` cannot be enforced by a presigned PUT — that needs a
        # POST policy document, which is a different signing scheme. Refusing is
        # better than returning a URL that silently ignores the cap the caller
        # asked for, because the caller asked for it for a reason.
        if max_bytes is not None:
            raise VTVError(
                "a presigned PUT cannot enforce a size cap; use a POST policy",
                code=ErrorCode.STORAGE_UNAVAILABLE,
                user_message="Direct upload with a size limit is not available yet.",
            )
        query = self.signer.presign(
            method="PUT",
            host=self._host,
            path=self._path(key),
            expires_in_seconds=expires_in_seconds,
            now=datetime.now(UTC),
        )
        return self._url(key, query)

    async def classify(self, key: str, retention: RetentionClass) -> None:
        """Tag an object that arrived without one.

        `put` tags on the way in with `x-amz-tagging`; a **presigned PUT cannot**
        — the device doing the uploading has no way to know the tag, and adding
        it to the signature would mean the device could choose it. So the tag is
        applied here, afterwards, by the server that granted the permission.

        Without this the lifecycle rule keyed on `vtv-retention` never matches a
        device's uploaded render and the object is kept for ever. That was the
        actual state: every video a customer's own computer rendered was
        untagged, and the bill for keeping them would have grown without any
        part of the system reporting a fault.
        """
        require_tenant_key(key)
        body = (
            "<Tagging><TagSet><Tag>"
            f"<Key>{RETENTION_TAG_KEY}</Key>"
            f"<Value>{RETENTION_TAGS[retention]}</Value>"
            "</Tag></TagSet></Tagging>"
        ).encode()
        response = await self._request(
            "PUT",
            key,
            query="tagging=",
            body=body,
            headers={"content-type": "application/xml"},
        )
        self._check(response, key)

    # -- deletion ---------------------------------------------------------

    async def delete(self, ref: ObjectRef) -> None:
        await self.delete_by_key(ref.key)

    async def delete_by_key(self, key: str) -> None:
        response = await self._request("DELETE", key)
        # 204 on success, 404 when already gone. Both are success: deletion is
        # idempotent because it is the operation behind a right to erasure, and
        # a retry that fails because the first attempt worked is a support
        # ticket about nothing.
        if response.status_code not in (200, 204, 404):
            self._check(response, key)

    async def list_keys(self, prefix: str) -> list[str]:
        """Every key under a prefix, following continuation tokens."""
        keys: list[str] = []
        token: str | None = None
        while True:
            params = [
                ("list-type", "2"),
                ("max-keys", "1000"),
                ("prefix", prefix),
            ]
            if token:
                params.append(("continuation-token", token))
            query = "&".join(
                f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(params)
            )
            response = self._check(
                await self._request("GET", "", query=query), prefix
            )
            root = ElementTree.fromstring(response.text)
            namespace = ""
            if root.tag.startswith("{"):
                namespace = root.tag.split("}", 1)[0] + "}"
            for node in root.findall(f"{namespace}Contents"):
                found = node.find(f"{namespace}Key")
                if found is not None and found.text:
                    keys.append(found.text)
            truncated = root.find(f"{namespace}IsTruncated")
            if truncated is None or (truncated.text or "").lower() != "true":
                break
            nxt = root.find(f"{namespace}NextContinuationToken")
            token = nxt.text if nxt is not None else None
            if not token:
                break
        return keys

    async def delete_prefix(self, prefix: str) -> list[str]:
        """Remove everything under a prefix, in batches of a thousand.

        One request per thousand keys rather than one per key: deleting a
        project with four hundred generated frames is one call, not four
        hundred. The batch size is S3's documented hard limit, not a tuning
        choice — exceeding it is a 400.
        """
        keys = await self.list_keys(prefix)
        removed: list[str] = []
        for start in range(0, len(keys), DELETE_BATCH):
            batch = keys[start : start + DELETE_BATCH]
            body = (
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<Delete><Quiet>true</Quiet>"
                + "".join(
                    f"<Object><Key>{_xml_escape(k)}</Key></Object>" for k in batch
                )
                + "</Delete>"
            ).encode("utf-8")
            response = await self._request(
                "POST",
                "",
                query="delete=",
                body=body,
                headers={
                    "content-type": "application/xml",
                    "content-length": str(len(body)),
                    "content-md5": _content_md5(body),
                },
            )
            self._check(response, prefix)
            removed.extend(batch)
        return removed

    # -- retention --------------------------------------------------------

    async def retention_of(self, key: str) -> RetentionClass | None:
        """The class recorded on the object, or `None` when it has none.

        `None` means *unknown*, never *ephemeral*. An object whose class cannot
        be read is one the sweep must keep — guessing here would delete a
        customer's saved footage because a tagging call failed once.
        """
        response = await self._request("GET", key, query="tagging=")
        if response.status_code == 404:
            return None
        self._check(response, key)
        root = ElementTree.fromstring(response.text)
        namespace = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
        for tag in root.iter(f"{namespace}Tag"):
            name = tag.find(f"{namespace}Key")
            value = tag.find(f"{namespace}Value")
            if name is not None and name.text == RETENTION_TAG_KEY and value is not None:
                return _TAG_TO_RETENTION.get(value.text or "")
        return None

    async def unclassified(self, *, prefix: str | None = None) -> list[str]:
        """Keys carrying no readable retention class — the sweep's blind spot."""
        keys = await self.list_keys(prefix or "")
        out: list[str] = []
        for key in keys:
            if await self.retention_of(key) is None:
                out.append(key)
        return out

    async def sweep_expired(self, *, policy: Any, prefix: str | None = None) -> list[str]:
        """Delete what the policy says may go.

        **In a real deployment this method should not run.** Retention is a
        bucket lifecycle rule keyed on the `vtv-retention` tag, which S3
        evaluates itself, for free, without this process being awake. This
        exists so the port is honest — a backend that cannot expire anything
        should not claim to — and for deployments against a compatible service
        whose lifecycle support is absent or untrusted.

        It is O(objects) in API calls because the class is a per-object tag, so
        it is the expensive path by construction. That is the argument for the
        lifecycle rule, stated where somebody would otherwise reach for this.
        """
        removed: list[str] = []
        for key in await self.list_keys(prefix or ""):
            decision = policy.decide(key=key, retention=await self.retention_of(key))
            if getattr(decision, "delete", False):
                await self.delete_by_key(key)
                removed.append(key)
        return removed


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _content_md5(body: bytes) -> str:
    """Required by DeleteObjects, and only by it.

    MD5 here is a transport integrity check specified by the S3 API, not a
    security primitive — it is the one place this codebase uses it, and this
    comment is why a reader should not treat it as a lapse.
    """
    import base64

    return base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()


__all__ = [
    "DELETE_BATCH",
    "RETENTION_TAGS",
    "RETENTION_TAG_KEY",
    "S3StorageProvider",
    "SigV4Signer",
]
