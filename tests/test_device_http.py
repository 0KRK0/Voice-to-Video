"""Phase C over a real HTTP stack.

## Why this file exists separately

Everything in `test_desktop_executor.py` substitutes the two methods that touch
the wire, so that what is under test is the executor rather than a mock. That is
the right shape for those tests and it leaves a specific hole: the wire itself.

Two things were broken in exactly that hole and neither could have been found by
the tests above.

**Signed URLs are relative.** `signed_url` returns `/media/<bucket>/<key>?token=…`
because the server does not necessarily know its own public hostname. Handed
straight to an HTTP client that is *not* bound to the server, that is not a URL
at all. Substituted `fetch` never saw it.

**Nothing served the upload URL.** `signed_upload_url` minted
`/media/upload/<bucket>/<key>` and the application had a download route and no
upload route, so every device upload would have 404ed into the single-page-app
fallback and been reported as a successful render of an HTML document.

Both are the same category — a piece that is correct in isolation and does not
meet its neighbour — and the only thing that finds them is running the real
routes.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from vtv.api.app import create_app
from vtv.config import Settings
from vtv.wiring import build


class TheStorageWireCarriesRealBytes(unittest.TestCase):
    """A signed URL a device can actually use, in both directions."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-device-http-")
        root = Path(self._dir.name)
        self.settings = Settings(
            asset_search_endpoint="",
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
            signing_key="test-signing-key-not-a-real-secret",
        )
        self.assembly = build(self.settings)
        self.app = create_app(self.settings, assembly=self.assembly)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def run_async(self, coroutine):  # type: ignore[no-untyped-def]
        import asyncio

        return asyncio.run(coroutine)

    def test_a_signed_download_url_serves_the_object(self) -> None:
        stored = self.run_async(
            self.assembly.storage.put(
                key="orgs/o/projects/p/photo.png",
                data=b"the photograph",
                content_type="image/png",
            )
        )
        url = self.run_async(self.assembly.storage.signed_url(stored))
        self.assertFalse(url.startswith("http"), "the fixture must be relative")

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"the photograph")

    def test_a_signed_upload_url_accepts_the_object(self) -> None:
        """The route that did not exist. A device's finished render arrives here
        and nowhere else."""
        url = self.run_async(
            self.assembly.storage.signed_upload_url(
                key="orgs/o/projects/p/renders/rnd_x.mp4",
                content_type="video/mp4",
                max_bytes=1024,
            )
        )
        response = self.client.put(
            url, content=b"a finished video", headers={"content-type": "video/mp4"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["bytes"], len(b"a finished video"))

        # And it is really there, readable by the same signing that wrote it.
        from vtv.contracts.base import ObjectRef

        ref = ObjectRef(
            bucket=self.assembly.storage.bucket,
            key="orgs/o/projects/p/renders/rnd_x.mp4",
            content_type="video/mp4",
        )
        self.assertEqual(
            self.assembly.storage.path_for(ref).read_bytes(), b"a finished video"
        )

    def test_an_unsigned_upload_is_refused(self) -> None:
        response = self.client.put(
            "/media/upload/vtv-media-dev/orgs/o/anything.mp4", content=b"x"
        )
        self.assertEqual(response.status_code, 403)

    def test_an_upload_may_not_be_redirected_to_another_key(self) -> None:
        """The bucket and key come from the claims, never from the path. A
        request whose path disagrees with what was signed is refused rather than
        reconciled — otherwise a URL for one render overwrites another."""
        url = self.run_async(
            self.assembly.storage.signed_upload_url(
                key="orgs/o/projects/p/renders/mine.mp4", content_type="video/mp4"
            )
        )
        token = url.split("token=", 1)[1]
        response = self.client.put(
            f"/media/upload/vtv-media-dev/orgs/o/projects/p/renders/yours.mp4?token={token}",
            content=b"x",
        )
        self.assertEqual(response.status_code, 403)

    def test_an_oversized_upload_is_cut_off(self) -> None:
        """The ceiling exists so a leaked URL cannot fill the bucket, which
        means the bytes must not reach the disk — written and then measured
        would be a ceiling that does nothing."""
        url = self.run_async(
            self.assembly.storage.signed_upload_url(
                key="orgs/o/projects/p/renders/big.mp4",
                content_type="video/mp4",
                max_bytes=16,
            )
        )
        response = self.client.put(url, content=b"x" * 4096)
        self.assertEqual(response.status_code, 413)

        from vtv.contracts.base import ObjectRef

        ref = ObjectRef(
            bucket=self.assembly.storage.bucket,
            key="orgs/o/projects/p/renders/big.mp4",
            content_type="video/mp4",
        )
        self.assertFalse(
            self.assembly.storage.path_for(ref).exists(),
            "an over-large upload was written before being measured",
        )

    def test_a_partial_upload_leaves_nothing_that_looks_finished(self) -> None:
        """Written to a `.part` and renamed, so a dropped connection cannot
        leave a truncated file where a finished render belongs."""
        from vtv.contracts.base import ObjectRef

        ref = ObjectRef(
            bucket=self.assembly.storage.bucket,
            key="orgs/o/projects/p/renders/never.mp4",
            content_type="video/mp4",
        )
        target = self.assembly.storage.path_for(ref)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix(".mp4.part").exists())


class ADeviceResolvesWhatTheServerGaveIt(unittest.TestCase):
    """The other half of the same gap, on the device's side."""

    def client(self, server: str = "http://127.0.0.1:8000"):  # type: ignore[no-untyped-def]
        from vtv.desktop.client import DeviceClient

        return DeviceClient(server=server, token="t")

    def test_a_relative_signed_url_is_resolved_against_the_server(self) -> None:
        """A relative URL means "on the server you are already talking to",
        which the device knows and the server does not."""
        resolved = self.client().absolute("/media/vtv-media-dev/orgs/o/p.png?token=x")
        self.assertEqual(
            resolved, "http://127.0.0.1:8000/media/vtv-media-dev/orgs/o/p.png?token=x"
        )

    def test_an_absolute_url_is_left_alone(self) -> None:
        """A deployment behind a CDN signs absolute URLs, and rewriting one
        would point the device back at an origin that does not serve it."""
        target = "https://cdn.example.com/o/p.png?token=x"
        self.assertEqual(self.client().absolute(target), target)

    def test_a_trailing_slash_on_the_server_does_not_double_up(self) -> None:
        """People paste server URLs with trailing slashes. `//media/...` is a
        different path on some servers and a redirect on others."""
        resolved = self.client("http://127.0.0.1:8000/").absolute("/media/b/k")
        self.assertEqual(resolved, "http://127.0.0.1:8000/media/b/k")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheUploadGoesOverARealSocket(unittest.TestCase):
    """The bug that a substituted client cannot see.

    `httpx.AsyncClient.put(url, content=open(path, "rb"))` is the obvious way to
    stream a file, it type-checks, and at runtime it raises *"attempted to send
    an sync request with an AsyncClient instance"*. Every unit test above
    replaces `upload`, so every one of them passed while the real thing could
    not send a single byte.

    So this one uses a real socket and a real server. It is slower than the rest
    of the file and it is the only test here that would have failed before the
    fix, which is the whole argument for it.
    """

    def setUp(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self) -> None:
                # Chunked, because a generator body has no content-length. A
                # server that only read `content-length` would silently receive
                # nothing, which is the other half of the same mistake.
                if self.headers.get("transfer-encoding", "").lower() == "chunked":
                    body = bytearray()
                    while True:
                        size = int(self.rfile.readline().strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        body += self.rfile.read(size)
                        self.rfile.readline()
                else:
                    length = int(self.headers.get("content-length", 0))
                    body = bytearray(self.rfile.read(length))
                received["body"] = bytes(body)
                received["path"] = self.path
                received["type"] = self.headers.get("content-type")
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_: object) -> None:
                return

        self.received = received
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self._dir = TemporaryDirectory(prefix="vtv-upload-")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._dir.cleanup()

    def test_a_file_is_actually_transmitted(self) -> None:
        import asyncio

        from vtv.desktop.client import DeviceClient

        payload = b"a finished render" * 5000
        source = Path(self._dir.name) / "render.mp4"
        source.write_bytes(payload)

        client = DeviceClient(server=self.base, token="t")
        sent = asyncio.run(client.upload("/media/upload/b/k?token=x", source))

        self.assertEqual(sent, len(payload))
        self.assertEqual(self.received["body"], payload)
        self.assertEqual(self.received["type"], "video/mp4")
        self.assertEqual(self.received["path"], "/media/upload/b/k?token=x")

    def test_a_relative_upload_url_reaches_the_server(self) -> None:
        """Both halves of the wire gap in one assertion: the URL is resolved
        against the server, and the body arrives."""
        import asyncio

        from vtv.desktop.client import DeviceClient

        source = Path(self._dir.name) / "small.mp4"
        source.write_bytes(b"x" * 32)
        asyncio.run(DeviceClient(server=self.base, token="t").upload("/media/upload/b/k", source))
        self.assertEqual(self.received["body"], b"x" * 32)

    def test_a_download_comes_back_over_the_same_wire(self) -> None:
        """`fetch` streams to disk; the same relative-URL resolution applies."""
        import asyncio

        from vtv.contracts.errors import ProviderError
        from vtv.desktop.client import DeviceClient

        target = Path(self._dir.name) / "downloaded.bin"
        with self.assertRaises(ProviderError):
            # This handler serves no GET, so the point is only that a relative
            # URL is turned into a request that reaches the socket at all
            # rather than failing before it leaves the process.
            asyncio.run(DeviceClient(server=self.base, token="t").fetch("/nothing", target))


class TheCommandLineActuallyRuns(unittest.TestCase):
    """`python -m vtv.desktop.cli` must do something.

    Without a `if __name__ == "__main__":` guard, running a module with `-m`
    imports it, defines every function in it, reaches the end and exits —
    silently, with status 0, having done nothing. No error, no usage, no output.

    The console script hides it completely: `vtv-desktop` calls `cli()` directly
    and works, so anybody who has pip-installed the package never sees this.
    Anybody running from a source checkout with `PYTHONPATH` set — which is what
    a verification procedure asks for — gets their prompt straight back.

    So this runs the real interpreter in a subprocess and insists on output. It
    is the only shape of test that can fail for this reason: importing the
    module and calling `main()` works fine either way, which is exactly how the
    bug survived being looked at.
    """

    def run_module(self, *args: str):  # type: ignore[no-untyped-def]
        import os
        import subprocess
        import sys

        environment = dict(os.environ)
        root = Path(__file__).resolve().parents[1] / "src"
        environment["PYTHONPATH"] = str(root)
        return subprocess.run(
            [sys.executable, "-m", "vtv.desktop.cli", *args],
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )

    def test_hardware_prints_what_it_measured(self) -> None:
        result = self.run_module("hardware", "--skip-gpu-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            result.stdout.strip(),
            "`python -m vtv.desktop.cli hardware` produced no output at all",
        )
        self.assertIn("will run", result.stdout)

    def test_no_arguments_explains_itself_rather_than_exiting_quietly(self) -> None:
        """A bare invocation must print usage and fail, not succeed in silence."""
        result = self.run_module()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", (result.stderr + result.stdout).lower())

    def test_an_unpaired_machine_is_told_what_to_do(self) -> None:
        """The error path through `main` — a sentence on stderr and a non-zero
        status, not a traceback."""
        import tempfile

        with tempfile.TemporaryDirectory() as empty:
            import os
            import subprocess
            import sys

            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            environment["VTV_DESKTOP_HOME"] = empty
            result = subprocess.run(
                [sys.executable, "-m", "vtv.desktop.cli", "status"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=120,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("pair", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)


class StoppingSaysWhatItWillActuallyDo(unittest.TestCase):
    """A printed promise the platform cannot keep is worse than no promise.

    `loop.add_signal_handler` raises `NotImplementedError` on Windows. The first
    version suppressed that and printed "Ctrl-C to stop after the current job"
    anyway — so somebody forty minutes into a render pressed Ctrl-C expecting it
    to finish, and it did not. It also meant the session tally was never printed
    on the one exit route people actually use.
    """

    def test_a_graceful_stop_is_installed_where_one_is_possible(self) -> None:
        import asyncio

        from vtv.desktop.cli import _catch_interrupts

        async def go() -> bool:
            return _catch_interrupts(lambda: None)

        self.assertTrue(asyncio.run(go()))

    def test_it_falls_back_when_the_loop_refuses(self) -> None:
        """Windows. `add_signal_handler` is unavailable and `signal.signal` is
        not — so the fallback is what makes the promise keepable there."""
        import asyncio
        from unittest import mock

        from vtv.desktop.cli import _catch_interrupts

        async def go() -> bool:
            loop = asyncio.get_running_loop()
            with mock.patch.object(
                loop, "add_signal_handler", side_effect=NotImplementedError
            ):
                return _catch_interrupts(lambda: None)

        self.assertTrue(
            asyncio.run(go()),
            "no graceful stop was installed on a platform that supports signals",
        )

    def test_it_admits_when_no_stop_could_be_installed(self) -> None:
        """Reported rather than assumed, so the message on screen can be true."""
        import asyncio
        from unittest import mock

        from vtv.desktop.cli import _catch_interrupts

        async def go() -> bool:
            loop = asyncio.get_running_loop()
            with mock.patch.object(
                loop, "add_signal_handler", side_effect=NotImplementedError
            ), mock.patch("signal.signal", side_effect=ValueError):
                return _catch_interrupts(lambda: None)

        self.assertFalse(asyncio.run(go()))

    def test_the_tally_is_printed_even_when_interrupted(self) -> None:
        """It was after the `try` and so never printed on a Ctrl-C — a session
        that had rendered something reported nothing at all."""
        import inspect

        from vtv.desktop import cli

        source = inspect.getsource(cli._run)
        finally_block = source.split("finally:", 1)[1]
        self.assertIn("stopped.", finally_block)


class TheDesktopSaysWhereItKeepsThings(unittest.TestCase):
    """Two terminals with different `VTV_DESKTOP_HOME` values use two different
    directories, and nothing said so. The symptom is `Cannot find path ...` from
    a command that looks correct, which reads as a bug and is not one."""

    def test_the_banner_names_the_workspace(self) -> None:
        import inspect

        from vtv.desktop import cli

        self.assertIn("workspace", inspect.getsource(cli._run))

    def test_the_identity_location_follows_the_environment(self) -> None:
        """The whole reason two terminals can disagree."""
        import os
        from unittest import mock

        from vtv.desktop import state

        with mock.patch.dict(os.environ, {"VTV_DESKTOP_HOME": "/tmp/elsewhere"}):
            self.assertEqual(str(state.config_root()), "/tmp/elsewhere")
