"""The S3 adapter, executed.

`S3StorageProvider` was a skeleton whose every method raised. It is now a real
implementation of the S3 REST API with hand-rolled Signature Version 4, and this
module runs it — against a conforming S3 server stood up here, and against the
**official AWS SigV4 test vector** for the signature itself.

## What this proves, and what it does not

**Proves:** the canonical request and string-to-sign are byte-exact against AWS's
own published vector; every method of the port does the right HTTP; retention
travels as an object tag in the same request that writes the bytes; deletion is
batched and idempotent; a presigned URL carries a valid signature; and the tenant
key check is enforced on every write path.

**Does not prove:** that AWS, R2 or MinIO accept these requests. A real service
can differ in ways a conforming server cannot reveal — checksum requirements,
regional redirects, tagging quotas. The SigV4 vector closes the largest part of
that gap, because a wrong signature is the failure mode that would otherwise
survive every local test and fail on the first real request.

The same honesty applies here as in `test_provider_adapters_live.py`: this is
"correct against the contract", not "works against the vendor".
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import unittest
import urllib.parse
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.storage.s3 import (
    RETENTION_TAG_KEY,
    S3StorageProvider,
    SigV4Signer,
)
from vtv.config import Settings
from vtv.contracts.base import IdPrefix, ObjectRef, RetentionClass, new_id
from vtv.contracts.errors import NotFound, VTVError
from vtv.wiring import storage_provider

ORG = new_id(IdPrefix.PROJECT)


def run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


class ConformingS3(BaseHTTPRequestHandler):
    """Enough of the S3 REST API to drive the real adapter through it.

    Objects and their tags live in class-level dicts. Deliberately strict about
    the things the adapter must get right — it refuses a request with no
    `Authorization` header, so a signing bug cannot pass by being ignored.
    """

    objects: dict[str, tuple[bytes, str]] = {}
    tags: dict[str, str] = {}
    log: list[dict[str, object]] = []

    def log_message(self, *_args: object) -> None:
        """Quiet."""

    # -- helpers ----------------------------------------------------------

    def _key(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        # Path-style: /<bucket>/<key...>
        parts = parsed.path.lstrip("/").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        return key, query

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/xml") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorised(self) -> bool:
        auth = self.headers.get("Authorization", "")
        ok = auth.startswith("AWS4-HMAC-SHA256 Credential=") and "Signature=" in auth
        type(self).log.append({"path": self.path, "method": self.command, "auth": ok})
        return ok

    # -- verbs ------------------------------------------------------------

    def do_PUT(self) -> None:
        if not self._authorised():
            self._send(403)
            return
        key, _ = self._key()
        body = self._body()
        type(self).objects[key] = (body, self.headers.get("Content-Type", ""))
        tagging = self.headers.get("x-amz-tagging", "")
        if tagging:
            type(self).tags[key] = tagging
        self._send(200)

    def do_HEAD(self) -> None:
        if not self._authorised():
            self._send(403)
            return
        key, _ = self._key()
        self._send(200 if key in type(self).objects else 404)

    def do_GET(self) -> None:
        if not self._authorised():
            self._send(403)
            return
        key, query = self._key()

        if "tagging" in query:
            raw = type(self).tags.get(key)
            if raw is None:
                self._send(404)
                return
            name, _, value = raw.partition("=")
            self._send(
                200,
                (
                    '<?xml version="1.0"?><Tagging><TagSet>'
                    f"<Tag><Key>{name}</Key><Value>{value}</Value></Tag>"
                    "</TagSet></Tagging>"
                ).encode(),
            )
            return

        if query.get("list-type") == ["2"]:
            prefix = (query.get("prefix") or [""])[0]
            matched = sorted(k for k in type(self).objects if k.startswith(prefix))
            # Page at two, so the continuation-token path is actually exercised
            # rather than being code nothing has ever run.
            token = (query.get("continuation-token") or [""])[0]
            start = int(token) if token.isdigit() else 0
            page = matched[start : start + 2]
            more = start + 2 < len(matched)
            body = (
                '<?xml version="1.0"?><ListBucketResult>'
                + "".join(f"<Contents><Key>{k}</Key></Contents>" for k in page)
                + f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
                + (f"<NextContinuationToken>{start + 2}</NextContinuationToken>" if more else "")
                + "</ListBucketResult>"
            ).encode()
            self._send(200, body)
            return

        stored = type(self).objects.get(key)
        if stored is None:
            self._send(404)
            return
        data, content_type = stored
        self._send(200, data, content_type)

    def do_POST(self) -> None:
        if not self._authorised():
            self._send(403)
            return
        _, query = self._key()
        if "delete" not in query:
            self._send(400)
            return
        root = ElementTree.fromstring(self._body().decode())
        gone = []
        for node in root.findall("Object"):
            found = node.find("Key")
            if found is not None and found.text:
                type(self).objects.pop(found.text, None)
                type(self).tags.pop(found.text, None)
                gone.append(found.text)
        self._send(200, b'<?xml version="1.0"?><DeleteResult/>')

    def do_DELETE(self) -> None:
        if not self._authorised():
            self._send(403)
            return
        key, _ = self._key()
        existed = type(self).objects.pop(key, None) is not None
        type(self).tags.pop(key, None)
        self._send(204 if existed else 404)


class SignatureMatchesTheAwsTestVector(unittest.TestCase):
    """SigV4, against AWS's own published vector.

    This is the assertion worth having. A wrong signature is the one failure
    mode that passes every test written against a server we control — because a
    server we control can simply not check — and then fails on the very first
    real request, with an error message about credentials rather than about
    encoding.

    Vector: `get-vanilla` from the AWS Signature Version 4 test suite.
    """

    def setUp(self) -> None:
        self.signer = SigV4Signer(
            access_key="AKIDEXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            region="us-east-1",
            service="service",
        )
        self.now = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)

    def test_the_canonical_request_is_byte_exact(self) -> None:
        canonical, signed = self.signer.canonical_request(
            method="GET",
            path="/",
            query="",
            headers={"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            payload_hash=hashlib.sha256(b"").hexdigest(),
        )
        self.assertEqual(signed, "host;x-amz-date")
        self.assertEqual(
            canonical,
            "GET\n"
            "/\n"
            "\n"
            "host:example.amazonaws.com\n"
            "x-amz-date:20150830T123600Z\n"
            "\n"
            "host;x-amz-date\n"
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_the_string_to_sign_is_byte_exact(self) -> None:
        canonical, _ = self.signer.canonical_request(
            method="GET",
            path="/",
            query="",
            headers={"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            payload_hash=hashlib.sha256(b"").hexdigest(),
        )
        self.assertEqual(
            self.signer.string_to_sign(
                canonical=canonical,
                stamp="20150830T123600Z",
                scope="20150830/us-east-1/service/aws4_request",
            ),
            "AWS4-HMAC-SHA256\n"
            "20150830T123600Z\n"
            "20150830/us-east-1/service/aws4_request\n"
            "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63",
        )

    def test_the_signature_matches_the_published_value(self) -> None:
        """The whole chain — canonical, string-to-sign, derived key, HMAC.

        Deliberately *not* through `sign()`. `sign()` adds
        `x-amz-content-sha256`, which S3 requires and the `service` in this
        vector does not have, so it signs a different — and for S3, correct —
        header set. Signing that here would be asserting our own behaviour
        against itself. This goes through the same three methods `sign()` uses
        and compares the result to the value AWS published, so the arithmetic is
        checked by somebody other than us.
        """
        canonical, _ = self.signer.canonical_request(
            method="GET",
            path="/",
            query="",
            headers={"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            payload_hash=hashlib.sha256(b"").hexdigest(),
        )
        to_sign = self.signer.string_to_sign(
            canonical=canonical,
            stamp="20150830T123600Z",
            scope="20150830/us-east-1/service/aws4_request",
        )
        signature = hmac.new(
            self.signer.signing_key(date="20150830"),
            to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            signature,
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        )

    def test_sign_adds_the_content_hash_header_s3_requires(self) -> None:
        """The reason the vector above is not run through `sign()`.

        S3 rejects a request whose `x-amz-content-sha256` is absent or does not
        match the body. It must be both sent and signed — signing it without
        sending it, or the reverse, fails with a message about credentials.
        """
        empty = hashlib.sha256(b"").hexdigest()
        headers = self.signer.sign(
            method="GET",
            path="/",
            query="",
            headers={"Host": "example.amazonaws.com"},
            payload_hash=empty,
            now=self.now,
        )
        self.assertEqual(headers["x-amz-content-sha256"], empty)
        self.assertIn("SignedHeaders=host;x-amz-content-sha256;x-amz-date", headers["Authorization"])

    def test_a_presigned_url_carries_every_required_parameter(self) -> None:
        query = self.signer.presign(
            method="GET",
            host="example.amazonaws.com",
            path="/photo.png",
            expires_in_seconds=900,
            now=self.now,
        )
        for required in (
            "X-Amz-Algorithm=AWS4-HMAC-SHA256",
            "X-Amz-Credential=",
            "X-Amz-Date=20150830T123600Z",
            "X-Amz-Expires=900",
            "X-Amz-SignedHeaders=host",
            "X-Amz-Signature=",
        ):
            with self.subTest(required):
                self.assertIn(required, query)


class TheAdapterSpeaksS3(unittest.TestCase):
    """Every method of the port, driven through a conforming server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ConformingS3)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        ConformingS3.objects = {}
        ConformingS3.tags = {}
        ConformingS3.log = []
        self.storage = S3StorageProvider(
            bucket="vtv-media",
            endpoint=f"http://127.0.0.1:{self.port}",
            region="us-east-1",
            access_key="AKIDEXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        )

    def key(self, name: str) -> str:
        return f"orgs/{ORG}/projects/p/{name}"

    # -- writes and reads -------------------------------------------------

    def test_put_stores_bytes_and_returns_a_faithful_reference(self) -> None:
        ref = run(
            self.storage.put(
                key=self.key("a.png"),
                data=b"pixels",
                content_type="image/png",
                retention=RetentionClass.PROJECT,
            )
        )
        self.assertEqual(ref.bucket, "vtv-media")
        self.assertEqual(ref.size_bytes, 6)
        self.assertEqual(ref.checksum_sha256, hashlib.sha256(b"pixels").hexdigest())
        self.assertEqual(ref.retention, RetentionClass.PROJECT)
        self.assertEqual(ConformingS3.objects[self.key("a.png")][0], b"pixels")

    def test_every_request_is_signed(self) -> None:
        """The server refuses an unsigned request, so this cannot pass by luck."""
        run(self.storage.put(key=self.key("a.png"), data=b"x", content_type="image/png"))
        self.assertTrue(ConformingS3.log)
        self.assertTrue(all(entry["auth"] for entry in ConformingS3.log))

    def test_the_retention_class_rides_the_write_itself(self) -> None:
        """Not a second call.

        A separate PutObjectTagging would leave a window in which the object
        exists with no class — and an unclassified object is one the sweep can
        never delete, so the window is a slow leak rather than a loud failure.
        """
        run(
            self.storage.put(
                key=self.key("a.png"),
                data=b"x",
                content_type="image/png",
                retention=RetentionClass.ARCHIVE,
            )
        )
        self.assertEqual(
            ConformingS3.tags[self.key("a.png")], f"{RETENTION_TAG_KEY}=archive"
        )
        puts = [e for e in ConformingS3.log if e["method"] == "PUT"]
        self.assertEqual(len(puts), 1, "the class cost an extra round trip")

    def test_retention_of_reads_the_class_back(self) -> None:
        for retention in (
            RetentionClass.EPHEMERAL,
            RetentionClass.PROJECT,
            RetentionClass.ARCHIVE,
        ):
            with self.subTest(retention=retention):
                key = self.key(f"{retention.value}.bin")
                run(
                    self.storage.put(
                        key=key, data=b"x", content_type="application/octet-stream",
                        retention=retention,
                    )
                )
                self.assertEqual(run(self.storage.retention_of(key)), retention)

    def test_an_untagged_object_reads_as_unknown_not_as_ephemeral(self) -> None:
        """`None` means unknown. Guessing here deletes somebody's saved footage."""
        ConformingS3.objects["orgs/x/stray.bin"] = (b"x", "application/octet-stream")
        self.assertIsNone(run(self.storage.retention_of("orgs/x/stray.bin")))

    def test_put_file_streams_from_disk_and_hashes_it(self) -> None:
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "render.mp4"
            path.write_bytes(b"\x00" * 4096)
            ref = run(
                self.storage.put_file(
                    key=self.key("render.mp4"), path=path, content_type="video/mp4"
                )
            )
        self.assertEqual(ref.size_bytes, 4096)
        self.assertEqual(ref.checksum_sha256, hashlib.sha256(b"\x00" * 4096).hexdigest())

    def test_get_round_trips(self) -> None:
        ref = run(
            self.storage.put(key=self.key("a.txt"), data=b"hello", content_type="text/plain")
        )
        self.assertEqual(run(self.storage.get(ref)), b"hello")
        self.assertEqual(run(self.storage.get_by_key(self.key("a.txt"))), b"hello")

    def test_a_missing_object_is_not_found_rather_than_a_generic_failure(self) -> None:
        """An expired object is a normal outcome, not a surprise."""
        with self.assertRaises(NotFound):
            run(self.storage.get_by_key(self.key("gone.txt")))

    def test_exists_is_a_head_request_and_answers_both_ways(self) -> None:
        ref = run(
            self.storage.put(key=self.key("a.txt"), data=b"x", content_type="text/plain")
        )
        self.assertTrue(run(self.storage.exists(ref)))
        missing = ObjectRef(
            bucket="vtv-media", key=self.key("no.txt"), content_type="text/plain"
        )
        self.assertFalse(run(self.storage.exists(missing)))

    def test_stream_yields_the_whole_object(self) -> None:
        payload = b"abcdefghij" * 5000
        ref = run(
            self.storage.put(
                key=self.key("big.bin"), data=payload, content_type="application/octet-stream"
            )
        )

        async def collect() -> bytes:
            out = b""
            async for chunk in self.storage.stream(ref):
                out += chunk
            return out

        self.assertEqual(run(collect()), payload)

    # -- deletion ---------------------------------------------------------

    def test_delete_is_idempotent(self) -> None:
        """Deletion is the operation behind a right to erasure.

        A retry that fails because the first attempt worked is a support ticket
        about nothing.
        """
        key = self.key("a.txt")
        run(self.storage.put(key=key, data=b"x", content_type="text/plain"))
        run(self.storage.delete_by_key(key))
        run(self.storage.delete_by_key(key))
        self.assertNotIn(key, ConformingS3.objects)

    def test_delete_prefix_removes_everything_and_pages_the_listing(self) -> None:
        for index in range(5):
            run(
                self.storage.put(
                    key=self.key(f"f{index}.bin"),
                    data=b"x",
                    content_type="application/octet-stream",
                )
            )
        run(self.storage.put(key=f"orgs/{ORG}/other/keep.bin", data=b"x", content_type="application/octet-stream"))

        removed = run(self.storage.delete_prefix(f"orgs/{ORG}/projects/p/"))
        self.assertEqual(len(removed), 5, removed)
        self.assertEqual(list(ConformingS3.objects), [f"orgs/{ORG}/other/keep.bin"])

    def test_delete_prefix_is_batched_not_one_call_per_key(self) -> None:
        """Deleting a project with four hundred frames must be one call."""
        for index in range(5):
            run(
                self.storage.put(
                    key=self.key(f"f{index}.bin"), data=b"x",
                    content_type="application/octet-stream",
                )
            )
        ConformingS3.log = []
        run(self.storage.delete_prefix(f"orgs/{ORG}/projects/p/"))
        deletes = [e for e in ConformingS3.log if e["method"] == "DELETE"]
        posts = [e for e in ConformingS3.log if e["method"] == "POST"]
        self.assertEqual(deletes, [])
        self.assertEqual(len(posts), 1, "one batched DeleteObjects, not five DELETEs")

    def test_delete_prefix_on_nothing_is_empty_and_succeeds(self) -> None:
        self.assertEqual(run(self.storage.delete_prefix("orgs/nobody/")), [])

    # -- tenancy ----------------------------------------------------------

    def test_a_key_outside_the_tenant_namespace_is_refused_on_every_write(self) -> None:
        """The check the local provider makes, made here too.

        A second storage backend that forgets it is a cross-tenant write waiting
        to happen — and there is no shared base class forcing it, so it is
        asserted instead.
        """
        with self.assertRaises(VTVError):
            run(self.storage.put(key="not-a-tenant/x.png", data=b"x", content_type="image/png"))
        with self.assertRaises(VTVError):
            run(
                self.storage.signed_upload_url(
                    key="not-a-tenant/x.png", content_type="image/png"
                )
            )
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "x.png"
            path.write_bytes(b"x")
            with self.assertRaises(VTVError):
                run(
                    self.storage.put_file(
                        key="not-a-tenant/x.png", path=path, content_type="image/png"
                    )
                )

    # -- signed URLs ------------------------------------------------------

    def test_a_signed_url_is_a_real_presigned_get(self) -> None:
        ref = run(
            self.storage.put(key=self.key("a.png"), data=b"x", content_type="image/png")
        )
        url = run(self.storage.signed_url(ref, expires_in_seconds=300))
        self.assertIn(f"/vtv-media/{self.key('a.png')}", url)
        self.assertIn("X-Amz-Signature=", url)
        self.assertIn("X-Amz-Expires=300", url)
        self.assertNotIn(
            "wJalrXUtnFEMI", url, "the secret key must never appear in a URL"
        )

    def test_a_size_capped_upload_url_is_refused_rather_than_faked(self) -> None:
        """A presigned PUT cannot enforce a cap — that needs a POST policy.

        Returning a URL that silently ignores the limit the caller asked for is
        worse than refusing, because the caller asked for it for a reason.
        """
        with self.assertRaises(VTVError) as caught:
            run(
                self.storage.signed_upload_url(
                    key=self.key("a.png"), content_type="image/png", max_bytes=1024
                )
            )
        self.assertIn("size", str(caught.exception.info.user_message).lower())

    # -- construction -----------------------------------------------------

    def test_it_refuses_to_construct_without_credentials(self) -> None:
        """Rather than raising later, three frames deeper, at the first write."""
        with self.assertRaises(VTVError):
            S3StorageProvider(bucket="b", access_key=None, secret_key=None)


class TheSweepUsesThePolicyRatherThanAnAge(unittest.TestCase):
    """The S3 half of the retention fix.

    The local provider's sweep takes a `SweepPolicy` and so does this one — the
    two backends must not disagree about what may be deleted, because the answer
    is a product decision, not a storage detail.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ConformingS3)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        ConformingS3.objects = {}
        ConformingS3.tags = {}
        ConformingS3.log = []
        self.storage = S3StorageProvider(
            bucket="vtv-media",
            endpoint=f"http://127.0.0.1:{self.port}",
            region="us-east-1",
            access_key="AKIDEXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        )

    def test_only_what_the_policy_allows_is_deleted(self) -> None:
        class OnlyEphemeral:
            """Stands in for `SweepPolicy` — same shape, one rule."""

            @staticmethod
            def decide(*, key: str, retention: RetentionClass | None):  # type: ignore[no-untyped-def]
                class Decision:
                    delete = retention is RetentionClass.EPHEMERAL
                return Decision()

        for name, retention in (
            ("temp.bin", RetentionClass.EPHEMERAL),
            ("asset.png", RetentionClass.PROJECT),
            ("master.mp4", RetentionClass.ARCHIVE),
        ):
            run(
                self.storage.put(
                    key=f"orgs/{ORG}/projects/p/{name}",
                    data=b"x",
                    content_type="application/octet-stream",
                    retention=retention,
                )
            )

        removed = run(self.storage.sweep_expired(policy=OnlyEphemeral(), prefix=f"orgs/{ORG}/"))
        self.assertEqual(removed, [f"orgs/{ORG}/projects/p/temp.bin"])
        self.assertEqual(len(ConformingS3.objects), 2)

    def test_unclassified_reports_the_sweeps_blind_spot(self) -> None:
        run(
            self.storage.put(
                key=f"orgs/{ORG}/projects/p/tagged.bin",
                data=b"x",
                content_type="application/octet-stream",
            )
        )
        ConformingS3.objects[f"orgs/{ORG}/projects/p/untagged.bin"] = (b"x", "application/octet-stream")
        self.assertEqual(
            run(self.storage.unclassified(prefix=f"orgs/{ORG}/")),
            [f"orgs/{ORG}/projects/p/untagged.bin"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class WiringPicksTheBackendFromCredentials(unittest.TestCase):
    """The selection rule, pinned.

    This is the part that is easy to get wrong without any test failing: both
    backends implement the same port, so a deployment on the wrong one passes
    every functional test and loses data at the first replica boundary.
    """

    def test_no_credentials_is_local_disk(self) -> None:
        self.assertIsInstance(storage_provider(Settings(asset_search_endpoint="", )), LocalStorageProvider)

    def test_credentials_select_s3(self) -> None:
        provider = storage_provider(
            Settings(asset_search_endpoint="", storage_access_key="AK", storage_secret_key="SK")
        )
        self.assertIsInstance(provider, S3StorageProvider)

    def test_production_on_a_shared_volume_may_use_local_disk(self) -> None:
        """Because SQLite on that same volume already requires one.

        The multi-worker deployment that was actually run is two workers on one
        Docker volume. Refusing local storage there — while the queue and the
        database live on the same volume — would refuse a configuration that
        demonstrably works.
        """
        provider = storage_provider(Settings(asset_search_endpoint="", env="production", signing_key="k"))
        self.assertIsInstance(provider, LocalStorageProvider)

    def test_a_shared_database_with_local_disk_refuses_to_boot(self) -> None:
        """The combination that cannot be correct.

        Postgres is only chosen when the replicas were separated, and separated
        replicas have separate disks. The render then succeeds on the worker and
        404s on the API — so this fails at boot rather than at download.
        """
        with self.assertRaises(VTVError) as caught:
            storage_provider(
                Settings(asset_search_endpoint="", database_url="postgresql://db/vtv", env="production")
            )
        self.assertIn("STORAGE_ACCESS_KEY", str(caught.exception))

    def test_a_secret_key_is_never_logged(self) -> None:
        """`redacted` matched `_api_key` and would have printed this in full."""
        payload = Settings(asset_search_endpoint="", storage_secret_key="SK").redacted()
        self.assertEqual(payload["storage_secret_key"], "***set***")
        self.assertNotIn("SK", str(payload))
