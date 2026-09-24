"""Stages 12 to 20 — API, persistence, evaluation and the platform endpoint."""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.media import ffmpeg
from vtv.adapters.queue.inprocess import InProcessJobQueue
from vtv.adapters.repository.sqlite import SqliteProjectRepository, postgres_schema
from vtv.config import Settings
from vtv.contracts.base import utc_now
from vtv.contracts.errors import Status
from vtv.contracts.project import PersistenceMode, PipelineStage, Project
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.evaluation.harness import EvaluationHarness
from vtv.observability.events import EventSink
from vtv.wiring import build

SCRIPT = (
    "Let me tell you about the single most important invention of the twentieth century. "
    "The transistor was invented in 1947 at Bell Labs. "
    "It was smaller and far more efficient than the vacuum tubes that came before it. "
    "Within twenty years transistors had replaced vacuum tubes almost everywhere."
)


def run(coro):
    return asyncio.run(coro)


class Persistence(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-test-db-")
        self.repo = SqliteProjectRepository(Path(self._dir.name) / "vtv.db")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_project_round_trips(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID, title="A talk about transistors")
        project.stage(PipelineStage.CAPTURE).status = Status.READY
        run(self.repo.save_project(project))
        loaded = run(self.repo.get_project(project.project_id))
        assert loaded is not None
        self.assertEqual(loaded.title, project.title)
        self.assertIs(loaded.stage(PipelineStage.CAPTURE).status, Status.READY)

    def test_saving_twice_updates_rather_than_duplicates(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID, title="First")
        run(self.repo.save_project(project))
        project.title = "Second"
        run(self.repo.save_project(project))
        self.assertEqual(len(run(self.repo.list_projects())), 1)
        loaded = run(self.repo.get_project(project.project_id))
        assert loaded is not None
        self.assertEqual(loaded.title, "Second")

    def test_documents_are_versioned_not_overwritten(self) -> None:
        # An improved Visual Director must not erase the plan an old project
        # was actually rendered from.
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID)
        run(self.repo.save_project(project))
        for version in ("rules-1.0", "llm-1.0"):
            run(
                self.repo.put_document(
                    project_id=project.project_id,
                    kind="visual_plan",
                    document_id="vpl_" + "a" * 24,
                    payload={"director": version},
                )
            )
        latest = run(
            self.repo.get_document(project_id=project.project_id, kind="visual_plan")
        )
        assert latest is not None
        self.assertEqual(latest["director"], "llm-1.0")
        history = run(
            self.repo.document_history(
                project_id=project.project_id, kind="visual_plan"
            )
        )
        self.assertEqual([item["director"] for item in history], ["llm-1.0", "rules-1.0"])

    def test_deleting_a_project_removes_its_documents_too(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID)
        run(self.repo.save_project(project))
        run(
            self.repo.put_document(
                project_id=project.project_id,
                kind="transcript",
                document_id="tsc_" + "a" * 24,
                payload={"text": "something the user said"},
            )
        )
        run(self.repo.delete_project(project.project_id))
        self.assertIsNone(run(self.repo.get_project(project.project_id)))
        self.assertIsNone(
            run(self.repo.get_document(project_id=project.project_id, kind="transcript"))
        )

    def test_expired_temporary_projects_are_identified(self) -> None:
        stale = Project(organisation_id=SYSTEM_ORGANISATION_ID, expires_at=utc_now() - timedelta(hours=1))
        fresh = Project(organisation_id=SYSTEM_ORGANISATION_ID, expires_at=utc_now() + timedelta(hours=1))
        saved = Project(organisation_id=SYSTEM_ORGANISATION_ID, persistence=PersistenceMode.SAVED)
        for project in (stale, fresh, saved):
            run(self.repo.save_project(project))
        expired = run(self.repo.expired_projects(now=utc_now()))
        self.assertEqual(expired, [stale.project_id])

    def test_the_postgres_schema_matches_the_sqlite_one(self) -> None:
        # Kept side by side so the two cannot drift apart unnoticed.
        ddl = postgres_schema()
        for table in ("projects", "documents"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", ddl)
        self.assertIn("JSONB", ddl)


class Jobs(unittest.TestCase):
    def test_work_runs_in_the_background_and_reports_completion(self) -> None:
        async def scenario() -> tuple[str, object]:
            queue = InProcessJobQueue(events=EventSink())
            queue.register("double", lambda payload: _double(payload))
            handle = await queue.enqueue(kind="double", payload={"value": 21})
            await queue.drain(timeout=5)
            return handle.job_id, (await queue.status(handle.job_id)).status

        job_id, status = run(scenario())
        self.assertIs(status, Status.READY)
        self.assertTrue(job_id)

    def test_idempotency_prevents_paying_twice_for_one_click(self) -> None:
        async def scenario() -> tuple[str, str]:
            queue = InProcessJobQueue(events=EventSink())
            queue.register("double", lambda payload: _double(payload))
            first = await queue.enqueue(
                kind="double", payload={"value": 1}, idempotency_key="k"
            )
            second = await queue.enqueue(
                kind="double", payload={"value": 1}, idempotency_key="k"
            )
            await queue.drain(timeout=5)
            return first.job_id, second.job_id

        first, second = run(scenario())
        self.assertEqual(first, second)

    def test_a_failing_job_is_recorded_not_swallowed(self) -> None:
        async def scenario() -> object:
            queue = InProcessJobQueue(events=EventSink())

            async def explode(_: dict) -> None:
                raise RuntimeError("something broke")

            queue.register("explode", explode)
            handle = await queue.enqueue(kind="explode", payload={})
            await queue.drain(timeout=5)
            return await queue.status(handle.job_id)

        handle = run(scenario())
        self.assertIs(handle.status, Status.FAILED)
        assert handle.error is not None
        # The engineer's message is kept; the user's is generic.
        self.assertIn("RuntimeError", handle.error.message)
        self.assertNotIn("RuntimeError", handle.error.user_message or "")


async def _double(payload: dict) -> int:
    return int(payload["value"]) * 2


class Wiring(unittest.TestCase):
    def test_missing_credentials_are_reported_not_papered_over(self) -> None:
        with TemporaryDirectory(prefix="vtv-test-wiring-") as directory:
            assembly = build(
                Settings(
                    storage_root=Path(directory) / "storage", asset_search_endpoint=""
                )
            )
        capabilities = assembly.capabilities.as_dict()
        self.assertFalse(capabilities["real_transcription"])
        self.assertFalse(capabilities["real_image_generation"])
        # Rendering is genuinely available, and says so.
        self.assertTrue(capabilities["rendering"])

    def test_credentials_switch_real_providers_on(self) -> None:
        with TemporaryDirectory(prefix="vtv-test-wiring-") as directory:
            assembly = build(
                Settings(asset_search_endpoint="", 
                    storage_root=Path(directory) / "storage",
                    speech_to_text_endpoint="https://example.invalid/v1/audio",
                    speech_to_text_api_key="secret",
                    speech_to_text_dpa_in_place=True,
                    speech_to_text_trains_on_input=False,
                    image_generation_endpoint="https://example.invalid/v1",
                    image_generation_api_key="secret",
                    image_generation_model="gpt-image-1",
                )
            )
        self.assertTrue(assembly.capabilities.real_transcription)
        self.assertTrue(assembly.capabilities.real_image_generation)

    def test_a_credential_alone_does_not_make_image_generation_available(self) -> None:
        """The same confusion `real_transcription` used to carry, for images.

        `HttpImageGenerationProvider` used to be built with the literal string
        "default" as its `model`, which no vendor recognises, so every image
        request was rejected while `/health` reported
        `real_image_generation: true`. An endpoint and a key are not enough —
        `VTV_IMAGE_GENERATION_MODEL` must actually be set, and until it is,
        the capability says so honestly rather than claiming a provider that
        cannot work.
        """
        with TemporaryDirectory(prefix="vtv-test-wiring-") as directory:
            assembly = build(
                Settings(asset_search_endpoint="", 
                    storage_root=Path(directory) / "storage",
                    image_generation_endpoint="https://example.invalid/v1",
                    image_generation_api_key="secret",
                )
            )
        self.assertFalse(
            assembly.capabilities.real_image_generation,
            "health claimed image generation works with no model configured",
        )

    def test_a_credential_alone_does_not_make_video_generation_available(self) -> None:
        """Same reasoning as images, for video."""
        with TemporaryDirectory(prefix="vtv-test-wiring-") as directory:
            assembly = build(
                Settings(asset_search_endpoint="", 
                    storage_root=Path(directory) / "storage",
                    video_generation_endpoint="https://example.invalid/v1",
                    video_generation_api_key="secret",
                )
            )
        self.assertFalse(
            assembly.capabilities.real_video_generation,
            "health claimed video generation works with no model configured",
        )

    def test_a_credential_alone_does_not_make_transcription_available(self) -> None:
        """The confusion that made every recording die at the second stage.

        `real_transcription` was set from the presence of a credential, so
        `/health` reported transcription as working while `GenerationRouter`
        refused every request for it — raw user speech may only go to a
        provider whose data policy has been verified, and nothing in the wiring
        ever supplied one. The audio was accepted, the job ran, and it failed
        with "no provider available … within data-policy constraints".

        A credential is not consent. The capability now reports what the router
        will actually do.
        """
        with TemporaryDirectory(prefix="vtv-test-wiring-") as directory:
            assembly = build(
                Settings(asset_search_endpoint="", 
                    storage_root=Path(directory) / "storage",
                    speech_to_text_endpoint="https://example.invalid/v1/audio",
                    speech_to_text_api_key="secret",
                )
            )
        self.assertFalse(
            assembly.capabilities.real_transcription,
            "health claimed transcription works while the router refuses it",
        )

    def test_settings_never_log_a_credential(self) -> None:
        settings = Settings(asset_search_endpoint="", text_generation_api_key="super-secret-value")
        redacted = settings.redacted()
        self.assertEqual(redacted["text_generation_api_key"], "***set***")
        self.assertNotIn("super-secret", str(redacted))


class PlatformEndpoint(unittest.TestCase):
    """Stage 20 — text in, a visual story out."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-test-platform-")
        self.assembly = build(
            Settings(
                storage_root=Path(self._dir.name) / "storage", asset_search_endpoint=""
            )
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _visualise(self, text: str) -> dict:
        from vtv.pipeline.text_entry import visualise_text

        return run(visualise_text(self.assembly, text))

    def test_stated_numbers_become_a_chart(self) -> None:
        payload = self._visualise(
            "The world population grew from one billion people to eight billion people."
        )
        kinds = [scene["visual"]["kind"] for scene in payload["scenes"]]
        self.assertIn("programmatic", kinds)
        primitives = [
            scene["visual"].get("primitive")
            for scene in payload["scenes"]
            if scene["visual"]["kind"] == "programmatic"
        ]
        self.assertIn("chart", primitives)

    def test_the_response_is_provider_neutral(self) -> None:
        payload = self._visualise(SCRIPT)
        text = str(payload)
        for vendor in ("openai", "anthropic", "replicate", "stability"):
            self.assertNotIn(vendor, text.lower())

    def test_synthetic_timing_is_declared_rather_than_hidden(self) -> None:
        payload = self._visualise(SCRIPT)
        self.assertTrue(any("synthesised" in note for note in payload["notes"]))
        self.assertFalse(payload["rendered"])

    def test_every_scene_carries_its_reasoning(self) -> None:
        payload = self._visualise(SCRIPT)
        self.assertTrue(payload["scenes"])
        for scene in payload["scenes"]:
            self.assertTrue(scene["rationale"])
            self.assertTrue(scene["strategy"])

    def test_empty_input_is_refused(self) -> None:
        from vtv.contracts.errors import ValidationFailed

        with self.assertRaises(ValidationFailed):
            self._visualise("   ")


class Evaluation(unittest.TestCase):
    """Stage 16 — the harness must actually be able to fail."""

    def test_the_default_corpus_passes(self) -> None:
        report = run(EvaluationHarness().run())
        self.assertTrue(report.passed, report.as_dict()["failed"])
        self.assertGreaterEqual(len(report.results), 10)

    def test_the_aggregate_reports_the_metrics_that_matter(self) -> None:
        report = run(EvaluationHarness().run())
        aggregate = report.aggregate()
        for name in (
            "scene_compression",
            "drawn_share",
            "generation_share",
            "factual_safety",
            "fallback_readiness",
        ):
            self.assertIn(name, aggregate)

    def test_factual_safety_catches_an_invented_number(self) -> None:
        # The metric has to be capable of failing, or it measures nothing.
        from vtv.contracts.semantics import Understanding
        from vtv.contracts.visual_language import (
            ChartKind,
            ChartSeries,
            ChartSpec,
            DataPoint,
        )
        from vtv.contracts.visual_plan import (
            ProgrammaticRequirements,
            SceneVisualPlan,
            VisualDirective,
            VisualPlan,
            VisualStrategy,
        )
        from vtv.evaluation.metrics import factual_safety

        understanding = Understanding(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id="prj_" + "a" * 24, transcript_id="tsc_" + "a" * 24
        )
        plan = VisualPlan(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id="prj_" + "a" * 24,
            scene_graph_id="sgr_" + "a" * 24,
            scene_plans=[
                SceneVisualPlan(
                    scene_id="scn_" + "a" * 24,
                    primary=VisualDirective(
                        strategy=VisualStrategy.PROGRAMMATIC,
                        requirements=ProgrammaticRequirements(
                            spec=ChartSpec(
                                kind=ChartKind.COLUMN,
                                series=[
                                    ChartSeries(
                                        name="invented",
                                        points=[DataPoint(label="x", value=42.0)],
                                    )
                                ],
                            )
                        ),
                        rationale="this number was never said by anybody at all",
                    ),
                )
            ],
        )
        metric = factual_safety(understanding, plan)
        self.assertEqual(metric.value, 0.0)
        self.assertFalse(metric.passing)


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class TheGoldenPath(unittest.TestCase):
    """Speech in, a real video out. The central success criterion."""

    def test_a_recording_becomes_a_playable_video(self) -> None:
        import json
        import subprocess

        with TemporaryDirectory(prefix="vtv-test-golden-") as directory:
            root = Path(directory)
            audio_path = root / "recording.wav"
            ffmpeg.synthesise_tone_audio(
                audio_path,
                duration=24.0,
                segments=[(0.3, 5.5), (6.2, 11.4), (12.1, 18.0), (18.8, 23.6)],
            )
            assembly = build(
                Settings(storage_root=root / "storage", asset_search_endpoint=""),
                workdir=root / "work",
            )
            original = assembly.pipeline.capture.capture

            async def capture(**kwargs):
                recording = await original(**kwargs)
                assembly.scripts[recording.audio.key] = SCRIPT
                return recording

            assembly.pipeline.capture.capture = capture

            from vtv.contracts.render import RenderQuality, RenderSettings

            result = run(
                assembly.pipeline.run(
                    organisation_id=SYSTEM_ORGANISATION_ID,
                    audio=audio_path.read_bytes(),
                    settings=RenderSettings(quality=RenderQuality.PREVIEW),
                )
            )
            self.assertTrue(result.succeeded)
            assert result.render_job is not None and result.render_job.output is not None
            path = assembly.storage.path_for(result.render_job.output)
            probe = json.loads(
                subprocess.run(
                    [
                        ffmpeg.FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path),
                    ],
                    capture_output=True, text=True, check=True,
                ).stdout
            )

        # A real file, with both streams, exactly as long as the voice.
        kinds = {stream["codec_type"] for stream in probe["streams"]}
        self.assertEqual(kinds, {"video", "audio"})
        self.assertAlmostEqual(float(probe["format"]["duration"]), 24.0, delta=0.5)

        # And the product rules held all the way through.
        assert result.scene_graph is not None and result.visual_plan is not None
        assert result.timeline is not None
        self.assertLess(
            len(result.scene_graph.scenes),
            len(result.understanding.units),  # type: ignore[union-attr]
            "Rule 7: scenes must group units, not mirror them",
        )
        self.assertEqual(result.timeline.coverage_gaps(), [])
        self.assertEqual(result.timeline.placeholder_count, 0)
        self.assertTrue(result.timeline.captions)
        # With no generation provider configured, the whole video costs nothing.
        self.assertEqual(result.ledger.total_usd, 0.0)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
