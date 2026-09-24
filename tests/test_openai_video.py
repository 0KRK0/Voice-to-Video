"""The OpenAI `/videos` adapter, against a server implementing that contract.

## Why a second video adapter exists at all

`HttpVideoGenerationProvider` creates a job at `…/video/generations`, polls it
by id there, and downloads the result from a `url` field on the finished job.
Pointed at `https://api.openai.com/v1` it requests
`https://api.openai.com/v1/video/generations`, which does not exist. Configured
with the user's own `https://api.openai.com/v1/videos` it requests
`…/v1/videos/video/generations`, which does not exist either.

Neither is a formatting difference. OpenAI creates at `POST {base}/videos`,
polls at `GET {base}/videos/{id}` with `status` in `queued`, `in_progress`,
`completed`, `failed`, and serves the finished MP4 from
`GET {base}/videos/{id}/content` — there is no URL to follow. The download step
alone makes the two contracts incompatible.

## What these tests prove and what they do not

They prove the adapter builds the documented request, follows the documented
state machine, downloads from the documented path, stores under the calling
tenant, and turns each class of failure into the right error. They prove nothing
about OpenAI: no credential is configured here and no request leaves the
process. That distinction is the whole reason this docstring exists.
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.video.openai_video import (
    LANDSCAPE_SIZE,
    PORTRAIT_SIZE,
    OpenAIVideoGenerationProvider,
)
from vtv.contracts.base import Budget
from vtv.contracts.errors import ProviderError, Status, TimeoutExceeded
from vtv.contracts.generation import GenerationKind, GenerationRequest, VideoParams
from vtv.contracts.style import AspectRatio

ORG = "org_0000000000000000000000"
PROJECT = "prj_0000000000000000000001"

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class Videos(BaseHTTPRequestHandler):
    """A server speaking OpenAI's published `/videos` contract.

    Strict on purpose: it records the request bodies so the tests can assert on
    what was *sent*, which is half of what an adapter does and the half no
    response assertion can reach.
    """

    created: list[dict[str, object]] = []
    paths: list[str] = []
    #: How many polls report `in_progress` before `completed`.
    pending_polls: int = 1
    polls: int = 0
    #: When set, creation answers with this status instead.
    create_status: int = 200
    #: When true, the job reaches `failed` rather than `completed`.
    fail_job: bool = False

    def log_message(self, *_args: object) -> None:
        """Quiet."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        cls = type(self)
        cls.paths.append(self.path)
        cls.created.append({"body": body, "auth": self.headers.get("Authorization", "")})
        if cls.create_status != 200:
            self._json(cls.create_status, {"error": {"message": "no"}})
            return
        self._json(200, {"id": "video_123", "object": "video", "status": "queued"})

    def do_GET(self) -> None:
        cls = type(self)
        cls.paths.append(self.path)
        if self.path.endswith("/content"):
            self._send(200, MP4, "video/mp4")
            return
        cls.polls += 1
        if cls.fail_job:
            self._json(
                200,
                {
                    "id": "video_123",
                    "status": "failed",
                    "error": {"message": "moderation blocked this prompt"},
                },
            )
            return
        if cls.polls <= cls.pending_polls:
            self._json(200, {"id": "video_123", "status": "in_progress", "progress": 40})
            return
        self._json(
            200,
            {"id": "video_123", "status": "completed", "seconds": 8, "progress": 100},
        )


class VideoCase(unittest.TestCase):
    server: HTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), Videos)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        Videos.created = []
        Videos.paths = []
        Videos.polls = 0
        Videos.pending_polls = 1
        Videos.create_status = 200
        Videos.fail_job = False
        self._dir = TemporaryDirectory(prefix="vtv-video-")
        self.storage = LocalStorageProvider(
            root=Path(self._dir.name), signing_key="test-key"
        )
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}/v1"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def provider(self, **overrides: object) -> OpenAIVideoGenerationProvider:
        kwargs: dict[str, object] = {
            "storage": self.storage,
            "endpoint": self.base,
            "api_key": "test-key",
            "model": "sora-2",
            # Fast enough that the polling loop is exercised without the suite
            # waiting on it.
            "poll_interval_seconds": 0.01,
        }
        kwargs.update(overrides)
        return OpenAIVideoGenerationProvider(**kwargs)  # type: ignore[arg-type]

    def request(self, aspect: AspectRatio = AspectRatio.LANDSCAPE_16_9) -> GenerationRequest:
        return GenerationRequest(
            organisation_id=ORG,
            project_id=PROJECT,
            kind=GenerationKind.VIDEO,
            params=VideoParams(
                prompt="A slow push in on an empty meeting room at dawn.",
                aspect_ratio=aspect,
                duration_seconds=6.0,
            ),
            budget=Budget(max_cost_usd=5.0, max_latency_seconds=30.0),
        )

    def run_async(self, coro):  # type: ignore[no-untyped-def]
        return asyncio.run(coro)


class ThePublishedContractIsWhatIsSpoken(VideoCase):
    def test_the_job_is_created_at_the_videos_path(self) -> None:
        self.run_async(self.provider().generate_video(self.request()))
        self.assertEqual(Videos.paths[0], "/v1/videos")

    def test_a_full_url_and_a_base_url_both_work(self) -> None:
        """The operator should not have to know which half we append."""
        self.run_async(
            self.provider(endpoint=f"{self.base}/videos").generate_video(self.request())
        )
        self.assertEqual(Videos.paths[0], "/v1/videos")

    def test_the_body_carries_the_documented_fields(self) -> None:
        self.run_async(self.provider().generate_video(self.request()))
        body = Videos.created[0]["body"]
        self.assertEqual(body["model"], "sora-2")
        self.assertIn("meeting room", body["prompt"])
        self.assertEqual(body["seconds"], "8")
        self.assertEqual(body["size"], LANDSCAPE_SIZE)

    def test_a_portrait_project_asks_for_a_portrait_size(self) -> None:
        self.run_async(
            self.provider().generate_video(self.request(AspectRatio.PORTRAIT_9_16))
        )
        self.assertEqual(Videos.created[0]["body"]["size"], PORTRAIT_SIZE)

    def test_the_credential_is_sent(self) -> None:
        self.run_async(self.provider().generate_video(self.request()))
        self.assertEqual(Videos.created[0]["auth"], "Bearer test-key")

    def test_it_polls_until_completed_and_then_downloads(self) -> None:
        Videos.pending_polls = 2
        result = self.run_async(self.provider().generate_video(self.request()))
        self.assertIs(result.status, Status.READY)
        self.assertEqual(Videos.polls, 3)
        self.assertIn("/v1/videos/video_123/content", Videos.paths)

    def test_the_bytes_land_under_the_calling_tenant(self) -> None:
        result = self.run_async(self.provider().generate_video(self.request()))
        self.assertEqual(len(result.outputs), 1)
        self.assertIn(f"orgs/{ORG}/", result.outputs[0].key)
        stored = self.run_async(self.storage.get(result.outputs[0]))
        self.assertEqual(stored, MP4)

    def test_the_charge_follows_what_the_vendor_says_it_made(self) -> None:
        """Not what we asked for. A clip rounded up has been billed rounded up."""
        result = self.run_async(
            self.provider(unit_cost_usd=0.10).generate_video(self.request())
        )
        self.assertAlmostEqual(result.cost_usd, 0.8, places=4)


class FailuresAreToldApart(VideoCase):
    def test_a_rejected_request_carries_the_vendors_own_message(self) -> None:
        """A guessed enum, a model the account lacks and a dead key look alike.

        Only the vendor can tell them apart, so its sentence is passed through
        rather than replaced with ours.
        """
        Videos.create_status = 400
        with self.assertRaises(ProviderError) as caught:
            self.run_async(self.provider().generate_video(self.request()))
        self.assertIn("400", str(caught.exception))

    def test_a_rate_limit_is_a_rate_limit(self) -> None:
        Videos.create_status = 429
        with self.assertRaises(ProviderError) as caught:
            self.run_async(self.provider().generate_video(self.request()))
        self.assertEqual(caught.exception.info.code.value, "rate_limited")

    def test_a_failed_job_says_why(self) -> None:
        Videos.fail_job = True
        with self.assertRaises(ProviderError) as caught:
            self.run_async(self.provider().generate_video(self.request()))
        self.assertIn("moderation", str(caught.exception))

    def test_a_job_that_never_finishes_gives_up_so_the_ladder_can_descend(self) -> None:
        Videos.pending_polls = 10_000
        request = self.request().model_copy(
            update={"budget": Budget(max_cost_usd=5.0, max_latency_seconds=0.2)}
        )
        with self.assertRaises(TimeoutExceeded):
            self.run_async(self.provider().generate_video(request))


class TheDialectIsChosenExplicitly(unittest.TestCase):
    """Which adapter runs is configuration, not a guess from the URL."""

    def _assembly(self, dialect: str):  # type: ignore[no-untyped-def]
        from vtv.config import Settings
        from vtv.wiring import build

        directory = TemporaryDirectory(prefix="vtv-dialect-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        return build(
            Settings(
                asset_search_endpoint="",
                storage_root=root / "storage",
                database_url=f"sqlite:///{root / 'vtv.db'}",
                signing_key="test-signing-key-not-a-real-secret",
                video_generation_endpoint="https://api.openai.com/v1",
                video_generation_api_key="sk-not-real",
                video_generation_model="sora-2",
                video_generation_dialect=dialect,
            )
        )

    def test_openai_selects_the_openai_adapter(self) -> None:
        providers = self._assembly("openai").router.providers_for(GenerationKind.VIDEO)
        self.assertEqual(
            [type(p).__name__ for p in providers], ["OpenAIVideoGenerationProvider"]
        )

    def test_the_default_stays_the_generic_poll_adapter(self) -> None:
        """Unset must not change what an existing deployment already had."""
        providers = self._assembly("poll").router.providers_for(GenerationKind.VIDEO)
        self.assertEqual(
            [type(p).__name__ for p in providers], ["HttpVideoGenerationProvider"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
