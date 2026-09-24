"""Stages 27–28 at runtime — the API/worker split, proven end to end.

The audit's finding was not that the durable queue was wrong. It was that
nothing used it: the API held jobs as asyncio tasks and rendered inside its own
event loop, so every durability guarantee in `adapters/queue/durable.py`
described code no request ever reached.

These tests drive the real HTTP application and the real worker against shared
files, and assert the properties that only hold when the split is genuine:

* the API cannot run a job even if asked
* the artifact survives the API process that accepted it
* a replica that never saw the upload can still serve the video
* a crashed worker's job is reclaimed rather than lost
* redelivery does not bill twice
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from starlette.testclient import TestClient

from vtv.adapters.media import ffmpeg
from vtv.api.app import create_app
from vtv.billing.plans import QuotaKind
from vtv.config import Settings
from vtv.contracts.tenancy import (
    Membership,
    Organisation,
    PlanTier,
    Role,
    User,
)
from vtv.jobs import JobKind
from vtv.security.keys import mint_api_key
from vtv.wiring import build, queue_path
from vtv.worker import Worker

SCRIPT = (
    "The transistor was invented in 1947 at Bell Labs. "
    "It replaced the vacuum tube almost everywhere within twenty years."
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class RuntimeTestCase(unittest.TestCase):
    """One temporary directory holding the storage, database and queue.

    Everything below builds *separate* API and worker objects over these shared
    files, which is the closest a single process can come to the real topology.
    """

    def setUp(self) -> None:
        if not ffmpeg.is_available():
            self.skipTest("ffmpeg is required for a real render")
        self._dir = TemporaryDirectory(prefix="vtv-runtime-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
        )
        self.assembly = build(self.settings)
        self.app = create_app(self.settings, assembly=self.assembly)
        self.client = TestClient(self.app)
        self.org, self.key = self.tenant("acme")

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def tenant(self, slug: str) -> tuple[str, str]:
        organisation = self.assembly.directory.create_organisation(
            Organisation(name=slug.title(), slug=slug, plan=PlanTier.BUSINESS)
        )
        user = self.assembly.directory.create_user(
            User(email=f"o@{slug}.example", sso_subject=f"sso|{slug}")
        )
        self.assembly.directory.add_member(
            Membership(
                user_id=user.user_id,
                organisation_id=organisation.organisation_id,
                role=Role.OWNER,
            )
        )
        minted = mint_api_key(
            organisation_id=organisation.organisation_id,
            name="key",
            role=Role.ADMIN,
            scopes=list(__import__(
                "vtv.contracts.tenancy", fromlist=["Capability"]
            ).Capability),
        )
        self.assembly.directory.store_key(minted.record)
        return organisation.organisation_id, minted.secret

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def submit_document(self) -> tuple[str, str]:
        """Create a project and enqueue a document render. Returns (project, job)."""
        project_id = self.client.post(
            "/v1/projects", json={"title": "runtime"}, headers=self.auth()
        ).json()["project_id"]

        body = (
            b"# The Transistor\n\n"
            b"The transistor was invented in 1947 at Bell Labs.\n\n"
            b"It replaced the vacuum tube almost everywhere within twenty years.\n"
        )
        response = self.client.post(
            f"/v1/projects/{project_id}/documents",
            files={"document": ("note.md", body, "text/markdown")},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return project_id, response.json()["job_id"]

    def worker(self) -> Worker:
        """A worker over the same files, as a separate object with its own queue."""
        return Worker.create(self.settings, assembly=self.assembly, concurrency=1)


class TheApiDoesNotRunWork(RuntimeTestCase):
    def test_the_api_registers_no_handlers(self) -> None:
        """The structural half of the split.

        The API physically does not know how to execute a job, so it cannot do
        so by accident — which is stronger than a convention that rendering
        belongs elsewhere.
        """
        self.assertEqual(self.app.state.vtv.queue.handlers, {})

    def test_uploading_returns_immediately_without_rendering(self) -> None:
        project_id, job_id = self.submit_document()
        self.assertTrue(job_id)

        # Nothing has rendered, because nothing has consumed the queue.
        body = self.client.get(
            f"/v1/projects/{project_id}", headers=self.auth()
        ).json()
        self.assertNotIn("video_url", body)

    def test_the_job_is_durably_queued(self) -> None:
        _project_id, job_id = self.submit_document()
        queue = self.app.state.vtv.queue
        handle = run(queue.status(job_id))
        self.assertEqual(handle.kind, JobKind.RENDER_DOCUMENT.value)

    def test_the_queue_file_is_shared_not_in_memory(self) -> None:
        self.submit_document()
        self.assertTrue(queue_path(self.settings).exists())


class TheWorkerRendersAndTheArtifactSurvives(RuntimeTestCase):
    def test_a_document_becomes_a_video_through_the_queue(self) -> None:
        project_id, _job_id = self.submit_document()

        worker = self.worker()
        run(worker.queue.drain(timeout=180.0))

        body = self.client.get(
            f"/v1/projects/{project_id}", headers=self.auth()
        ).json()
        self.assertIn("video_url", body, body)
        video = self.client.get(body["video_url"], headers=self.auth())
        self.assertEqual(video.status_code, 200)
        self.assertGreater(len(video.content), 10_000)

    def test_a_replica_that_never_saw_the_upload_serves_the_video(self) -> None:
        """The in-memory result store made this impossible.

        A second application object over the same files stands in for a second
        pod. It never handled the upload and never ran the job.
        """
        project_id, _job_id = self.submit_document()
        run(self.worker().queue.drain(timeout=180.0))

        replica_assembly = build(self.settings)
        replica = create_app(self.settings, assembly=replica_assembly)
        with TestClient(replica) as other:
            response = other.get(
                f"/v1/projects/{project_id}/video", headers=self.auth()
            )
            self.assertEqual(response.status_code, 200)
            self.assertGreater(len(response.content), 10_000)

    def test_captions_and_storyboard_also_survive(self) -> None:
        project_id, _job_id = self.submit_document()
        run(self.worker().queue.drain(timeout=180.0))

        replica = create_app(self.settings, assembly=build(self.settings))
        with TestClient(replica) as other:
            captions = other.get(
                f"/v1/projects/{project_id}/captions.vtt", headers=self.auth()
            )
            self.assertEqual(captions.status_code, 200)
            self.assertIn("WEBVTT", captions.text)

            storyboard = other.get(
                f"/v1/projects/{project_id}/storyboard", headers=self.auth()
            )
            self.assertEqual(storyboard.status_code, 200)
            self.assertTrue(storyboard.json()["scenes"])

    def test_the_input_is_stored_under_the_tenants_prefix(self) -> None:
        """P1-2. A raw key would have put one tenant's upload beside another's."""
        project_id, job_id = self.submit_document()
        handle = run(self.app.state.vtv.queue.status(job_id))
        del handle
        bucket = self.settings.storage_root / self.settings.storage_bucket
        uploads = list((bucket / "orgs" / self.org).rglob("*"))
        self.assertTrue(
            any(path.is_file() for path in uploads),
            "the upload is not inside the tenant's namespace",
        )
        del project_id


class FailureAndRecovery(RuntimeTestCase):
    def test_a_reclaimed_job_completes_after_a_worker_dies(self) -> None:
        """Simulates the crash by writing the state a dead worker leaves."""
        import sqlite3

        project_id, job_id = self.submit_document()
        with sqlite3.connect(queue_path(self.settings), isolation_level=None) as db:
            db.execute(
                "UPDATE jobs SET state = 'running', claimed_by = 'dead-worker', "
                "heartbeat_at = 0 WHERE job_id = ?",
                (job_id,),
            )

        worker = Worker.create(
            self.settings, assembly=self.assembly, concurrency=1
        )
        # A fresh worker recovers on construction and again here, exactly as the
        # real one does before it starts consuming.
        recovered, dead = run(worker.queue.recover())
        self.assertEqual(dead, 0)
        run(worker.queue.drain(timeout=180.0))

        body = self.client.get(
            f"/v1/projects/{project_id}", headers=self.auth()
        ).json()
        self.assertIn("video_url", body, f"recovered={recovered} body={body}")

    def test_redelivery_does_not_bill_twice(self) -> None:
        """At-least-once delivery is only safe because the effects converge."""
        project_id, _job_id = self.submit_document()
        run(self.worker().queue.drain(timeout=180.0))

        first = self.assembly.usage.total(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES
        )
        self.assertGreater(first, 0.0)

        # Re-run the identical work, as a redelivered message would.
        from vtv.jobs import RenderPayload, run_render_document

        payload = RenderPayload(
            project_id=project_id,
            organisation_id=self.org,
            input_key=f"orgs/{self.org}/projects/{project_id}/input/document",
            filename="note.md",
        )
        from vtv.contracts.errors import VTVError

        worker = self.worker()
        with self.assertRaises(VTVError):
            # The input was deleted on success, so a redelivery fails cleanly
            # rather than rendering a second time and charging again. It fails
            # as a domain error, not as an unhandled crash.
            run(run_render_document(worker.context(), payload.model_dump(mode="json")))

        second = self.assembly.usage.total(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES
        )
        self.assertEqual(first, second, "a redelivered job billed twice")


class DegradationIsVisible(RuntimeTestCase):
    def test_a_mute_render_is_reported_degraded_not_ready(self) -> None:
        """P1-4. The audit's product-truth finding.

        No speech synthesiser is configured here, so the document path produces
        a silent video. It must not claim plain success.
        """
        project_id, _job_id = self.submit_document()
        run(self.worker().queue.drain(timeout=180.0))

        body = self.client.get(
            f"/v1/projects/{project_id}", headers=self.auth()
        ).json()
        self.assertEqual(body["outcome"], "degraded")
        self.assertTrue(body["deliverable"])
        self.assertFalse(body["narration"]["has_speech"])
        self.assertTrue(
            any("voice" in note.lower() for note in body["degradations"]),
            body.get("degradations"),
        )


class VoiceRecordingBridgesToStudio(RuntimeTestCase):
    """The gap between the two lanes, closed.

    Before `vtv.product_bridge.derive_product_documents`, a completed voice
    recording rendered a real MP4 and left every Studio endpoint 404: the
    words were transcribed and rendered, and nowhere editable. These tests
    drive a real recording through the real API and worker and fail without
    the bridge — `/script`, `/visual-units` and `/timeline` all 404 on this
    project otherwise.
    """

    #: Three sentences, three real speech regions in a synthesised recording.
    #: `ScriptedSpeechToTextProvider` aligns this text to those regions using
    #: ffmpeg's own silence detection, so the resulting segment timings are
    #: genuine measurements of the audio, not numbers this test invents.
    RECORDING_SCRIPT = (
        "The transistor was invented in 1947 at Bell Labs. "
        "It was smaller and far more efficient than the vacuum tubes that came "
        "before it. Within twenty years transistors had replaced vacuum tubes "
        "almost everywhere."
    )
    SPEECH_REGIONS = [(0.3, 6.0), (6.8, 13.0), (13.8, 19.5)]
    DURATION = 20.0

    def submit_recording(self) -> tuple[str, str]:
        project_id = self.client.post(
            "/v1/projects", json={"title": "voice"}, headers=self.auth()
        ).json()["project_id"]

        # `ScriptedSpeechToTextProvider` looks up its script by the
        # *recording's* storage key, which `CaptureService.capture` mints
        # fresh — not by the upload's own key, which is what the
        # `/recordings` endpoint's `script` field registers
        # (`run_render_recording` calls
        # `register_script_for_key(job.input_key, ...)`, and `job.input_key`
        # is never the key the transcriber looks up). That mismatch is a
        # pre-existing defect that leaves the real HTTP upload path unable to
        # drive the development transcriber at all — see the report. This
        # test drives the real API and worker exactly as everywhere else in
        # this file, and works around only that one defect the same way
        # `TheGoldenPath` in `test_api_and_platform.py` already does:
        # registering the script against the key capture actually mints.
        original_capture = self.assembly.pipeline.capture.capture

        async def capture(**kwargs):  # type: ignore[no-untyped-def]
            recording = await original_capture(**kwargs)
            self.assembly.scripts[recording.audio.key] = self.RECORDING_SCRIPT
            return recording

        self.assembly.pipeline.capture.capture = capture

        with TemporaryDirectory(prefix="vtv-test-voice-") as directory:
            path = Path(directory) / "a.wav"
            ffmpeg.synthesise_tone_audio(
                path, duration=self.DURATION, segments=self.SPEECH_REGIONS
            )
            data = path.read_bytes()

        response = self.client.post(
            f"/v1/projects/{project_id}/recordings",
            files={"audio": ("a.wav", data, "audio/wav")},
            data={"quality": "preview"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return project_id, response.json()["job_id"]

    async def _pipeline_result(self, worker: Worker, project_id: str):  # type: ignore[no-untyped-def]
        """Reload the pipeline's own persisted documents as a `PipelineResult`.

        Used only to prove the derivation is idempotent: calling it a second
        time needs the same input it saw the first time, and the worker does
        not hand that object back once the job has finished, so it is rebuilt
        from exactly what `persist_result` wrote.
        """
        from vtv.contracts.recording import Recording
        from vtv.contracts.render import RenderJob
        from vtv.contracts.scene import SceneGraph
        from vtv.contracts.timeline import Timeline
        from vtv.contracts.transcript import Transcript
        from vtv.contracts.visual_plan import VisualPlan
        from vtv.pipeline.orchestrator import PipelineResult

        repository = worker.repository

        async def doc(kind: str, model: Any):  # type: ignore[no-untyped-def]
            payload = await repository.get_document(project_id=project_id, kind=kind)
            return model.model_validate(payload) if payload else None

        project = await repository.get_project(project_id, organisation_id=self.org)
        assert project is not None
        return PipelineResult(
            project=project,
            recording=await doc("recording", Recording),
            transcript=await doc("transcript", Transcript),
            scene_graph=await doc("scene_graph", SceneGraph),
            visual_plan=await doc("visual_plan", VisualPlan),
            timeline=await doc("timeline", Timeline),
            render_job=await doc("render_job", RenderJob),
        )

    def test_a_recording_becomes_an_editable_project(self) -> None:
        project_id, _job_id = self.submit_recording()
        run(self.worker().queue.drain(timeout=180.0))

        # The pipeline's own promise still holds: a real video exists.
        project_body = self.client.get(
            f"/v1/projects/{project_id}", headers=self.auth()
        ).json()
        self.assertIn("video_url", project_body, project_body)

        # And now Studio can open on the same recording.
        script_response = self.client.get(
            f"/v1/projects/{project_id}/script", headers=self.auth()
        )
        self.assertEqual(script_response.status_code, 200, script_response.text)
        script = script_response.json()
        self.assertEqual(script["origin"], "spoken")
        self.assertEqual(len(script["blocks"]), 3, script["blocks"])
        all_block_ids = {block["block_id"] for block in script["blocks"]}
        for block in script["blocks"]:
            # Measured, not estimated: real timing from the transcript segment,
            # and never marked stale on arrival.
            self.assertIsNotNone(block["start"])
            self.assertIsNotNone(block["end"])
            self.assertGreater(block["end"], block["start"])
            self.assertFalse(block["timing_invalidated"])

        units_response = self.client.get(
            f"/v1/projects/{project_id}/visual-units", headers=self.auth()
        )
        self.assertEqual(units_response.status_code, 200, units_response.text)
        units = units_response.json()["units"]
        self.assertTrue(units)
        covered_block_ids: set[str] = set()
        for unit in units:
            # Not a fresh, unplanned set: every unit already carries the
            # version the pipeline actually produced.
            self.assertTrue(unit["versions"], unit)
            self.assertTrue(unit["deliverable"], unit)
            version = unit["versions"][0]
            # No providers are configured in this build, so every scene's
            # ladder descends to the one rung guaranteed to render: typed
            # motion typography. That is a real, read-back decision — not a
            # guess this test is making up — see `_realised_strategy`.
            self.assertEqual(version["strategy"], "programmatic")
            self.assertTrue(version["rationale"])
            self.assertIsNotNone(unit["start"])
            self.assertIsNotNone(unit["end"])
            covered_block_ids.update(unit["script_block_ids"])
        # Every spoken line belongs to some visual; none was silently dropped.
        self.assertEqual(covered_block_ids, all_block_ids)

        timeline_response = self.client.get(
            f"/v1/projects/{project_id}/timeline", headers=self.auth()
        )
        self.assertEqual(timeline_response.status_code, 200, timeline_response.text)
        timeline = timeline_response.json()
        kinds = {track["kind"] for track in timeline["tracks"]}
        self.assertEqual(kinds, {"narration", "visual", "caption"})
        self.assertGreater(timeline["duration"], 0.0)
        visual_track = next(t for t in timeline["tracks"] if t["kind"] == "visual")
        self.assertEqual(len(visual_track["clips"]), len(units))

    def test_deriving_twice_does_not_double_the_blocks(self) -> None:
        project_id, _job_id = self.submit_recording()
        worker = self.worker()
        run(worker.queue.drain(timeout=180.0))

        def snapshot() -> tuple[int, int, int]:
            script = self.client.get(
                f"/v1/projects/{project_id}/script", headers=self.auth()
            ).json()
            units = self.client.get(
                f"/v1/projects/{project_id}/visual-units", headers=self.auth()
            ).json()["units"]
            timeline = self.client.get(
                f"/v1/projects/{project_id}/timeline", headers=self.auth()
            ).json()
            clips = sum(len(track["clips"]) for track in timeline["tracks"])
            return len(script["blocks"]), len(units), clips

        before = snapshot()
        self.assertNotEqual(before, (0, 0, 0))

        from vtv.product_bridge import derive_product_documents

        result = run(self._pipeline_result(worker, project_id))
        context = worker.context()
        run(derive_product_documents(context, result))
        run(derive_product_documents(context, result))

        after = snapshot()
        self.assertEqual(
            before, after, "re-running the derivation changed the block/unit/clip counts"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
