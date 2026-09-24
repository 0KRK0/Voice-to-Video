"""Project state, retention policy, and the shape of the provider ports."""

from __future__ import annotations

import unittest
from datetime import timedelta

from pydantic import ValidationError

from vtv.contracts import (
    STAGE_ORDER,
    AudioFormat,
    AudioProperties,
    CaptureSource,
    ObjectRef,
    PersistenceMode,
    PipelineStage,
    Project,
    Recording,
    StageState,
    Status,
    utc_now,
)
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.ports import (
    ALL_PORTS,
    AssetSearchProvider,
    DataPolicy,
    ImageGenerationProvider,
    JobQueue,
    ProviderCapabilities,
    Renderer,
    SpeechToTextProvider,
    StorageProvider,
)

AUDIO = ObjectRef(bucket="b", key="a.webm", content_type="audio/webm")


class ProjectStateMachine(unittest.TestCase):
    def test_a_new_project_has_every_stage_pending(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID)
        self.assertEqual(len(project.stages), len(STAGE_ORDER))
        self.assertEqual(project.progress, 0.0)
        self.assertIs(project.current_stage, PipelineStage.CAPTURE)

    def test_stages_must_be_present_exactly_once_in_order(self) -> None:
        with self.assertRaises(ValidationError):
            Project(organisation_id=SYSTEM_ORGANISATION_ID, stages=[StageState(stage=PipelineStage.RENDERING)])

    def test_progress_reflects_completed_stages(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID)
        project.stage(PipelineStage.CAPTURE).status = Status.READY
        project.stage(PipelineStage.TRANSCRIPTION).status = Status.READY
        self.assertAlmostEqual(project.progress, 2 / len(STAGE_ORDER))
        self.assertIs(project.current_stage, PipelineStage.UNDERSTANDING)

    def test_a_failed_stage_is_identifiable(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID)
        project.stage(PipelineStage.VISUAL_DIRECTION).status = Status.FAILED
        failed = project.failed_stage
        assert failed is not None
        self.assertIs(failed.stage, PipelineStage.VISUAL_DIRECTION)


class RetentionIsADeliberateChoice(unittest.TestCase):
    def test_projects_are_temporary_unless_the_user_says_otherwise(self) -> None:
        self.assertIs(Project(organisation_id=SYSTEM_ORGANISATION_ID).persistence, PersistenceMode.TEMPORARY)

    def test_a_saved_project_carries_its_plans_retention_ceiling(self) -> None:
        """P1-7. This used to be forbidden, and that is why the ceiling was
        dead configuration.

        "Saved" never meant "forever" — every plan has a `max_retention_days`,
        documented as the thing that stops storage cost growing without bound.
        A contract that refused to record an expiry on a saved project made that
        field unenforceable, so nothing enforced it.
        """
        expiry = utc_now() + timedelta(days=90)
        project = Project(
            organisation_id=SYSTEM_ORGANISATION_ID,
            persistence=PersistenceMode.SAVED,
            expires_at=expiry,
        )
        self.assertEqual(project.expires_at, expiry)

    def test_a_temporary_project_may_expire(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID, expires_at=utc_now() + timedelta(hours=6))
        self.assertIsNotNone(project.expires_at)

    def test_consent_defaults_to_the_most_conservative_setting(self) -> None:
        consent = Project(organisation_id=SYSTEM_ORGANISATION_ID).consent
        self.assertFalse(consent.improve_product)
        self.assertFalse(consent.human_review)

    def test_regenerability_requires_the_reasoning_not_the_pixels(self) -> None:
        project = Project(organisation_id=SYSTEM_ORGANISATION_ID)
        self.assertFalse(project.is_regenerable)
        project.transcript_id = "tsc_aaaaaaaaaaaaaaaaaaaaaaaa"
        project.understanding_id = "sem_aaaaaaaaaaaaaaaaaaaaaaaa"
        project.scene_graph_id = "sgr_aaaaaaaaaaaaaaaaaaaaaaaa"
        project.visual_plan_id = "vpl_aaaaaaaaaaaaaaaaaaaaaaaa"
        # No render output, no stored MP4 — and yet the video can be rebuilt.
        self.assertTrue(project.is_regenerable)


class RecordingInvariants(unittest.TestCase):
    def recording(self, **overrides: object) -> Recording:
        fields: dict[str, object] = {
            "project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaa",
            "format": AudioFormat.WEBM_OPUS,
            "audio": AUDIO,
            "source": CaptureSource.MICROPHONE,
        }
        fields.update(overrides)
        return Recording(**fields)  # type: ignore[arg-type]

    def test_microphone_is_the_default_capture_path(self) -> None:
        self.assertIs(Recording(
            organisation_id=SYSTEM_ORGANISATION_ID,
            project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
            format=AudioFormat.WEBM_OPUS,
            audio=AUDIO,
        ).source, CaptureSource.MICROPHONE)

    def test_a_ready_recording_must_have_been_probed(self) -> None:
        with self.assertRaises(ValidationError):
            self.recording(status=Status.READY)

    def test_a_failed_recording_must_say_why(self) -> None:
        with self.assertRaises(ValidationError):
            self.recording(status=Status.FAILED)

    def test_absurd_durations_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            AudioProperties(
                duration_seconds=60 * 60 * 3, sample_rate_hz=48_000, channels=1
            )
        with self.assertRaises(ValidationError):
            AudioProperties(
                duration_seconds=0.2, sample_rate_hz=48_000, channels=1
            )


class PortsAreImplementableWithoutTheCoreKnowing(unittest.TestCase):
    """Structural typing means an adapter needs no import from us but the port."""

    def test_every_port_is_a_runtime_checkable_protocol(self) -> None:
        for port in ALL_PORTS:
            self.assertTrue(
                getattr(port, "_is_runtime_protocol", False),
                f"{port.__name__} must be a runtime_checkable Protocol so that "
                "the router can verify adapters at wiring time",
            )

    def test_a_fake_adapter_satisfies_its_port(self) -> None:
        class FakeStorage:
            async def get_by_key(self, key: str) -> bytes: return b""
            async def delete_by_key(self, key: str) -> None: ...
            async def delete_prefix(self, prefix: str) -> list[str]: return []
            async def put(self, **kwargs: object) -> None: ...
            async def get(self, ref: object) -> bytes: return b""
            async def stream(self, ref: object) -> None: ...
            async def exists(self, ref: object) -> bool: return True
            async def delete(self, ref: object) -> None: ...
            async def signed_url(self, ref: object, **kwargs: object) -> str: return ""
            async def signed_upload_url(self, **kwargs: object) -> str: return ""
            # Added when the port grew it. Not optional: an object with no
            # retention class is one the sweep will never delete, so a
            # backend that cannot record one keeps every file for ever.
            async def classify(self, key: str, retention: object) -> None: ...

        self.assertIsInstance(FakeStorage(), StorageProvider)

    def test_an_incomplete_adapter_does_not_satisfy_its_port(self) -> None:
        class HalfBuilt:
            async def get_by_key(self, key: str) -> bytes: return b""
            async def delete_by_key(self, key: str) -> None: ...
            async def put(self, **kwargs: object) -> None: ...

        self.assertNotIsInstance(HalfBuilt(), StorageProvider)

    def test_all_expected_ports_are_exported(self) -> None:
        for port in (
            StorageProvider,
            SpeechToTextProvider,
            ImageGenerationProvider,
            AssetSearchProvider,
            Renderer,
            JobQueue,
        ):
            self.assertIn(port, ALL_PORTS)


class ProviderSelectionIsDataDriven(unittest.TestCase):
    def test_an_unverified_data_policy_excludes_a_provider_from_voice(self) -> None:
        # Pessimistic defaults: a provider we have not vetted cannot receive
        # user speech, and that is a field comparison rather than a promise.
        self.assertFalse(DataPolicy().is_acceptable_for_user_voice)

    def test_a_vetted_provider_may_receive_voice(self) -> None:
        vetted = DataPolicy(
            retains_input=False,
            trains_on_input=False,
            retention_days=0,
            dpa_in_place=True,
        )
        self.assertTrue(vetted.is_acceptable_for_user_voice)

    def test_affordability_is_computed_from_declared_cost(self) -> None:
        capability = ProviderCapabilities(
            name="a-provider", unit_cost_usd=0.04, unit="image"
        )
        self.assertTrue(capability.can_afford(1, 0.05))
        self.assertFalse(capability.can_afford(2, 0.05))
        self.assertTrue(capability.can_afford(1000, None))


if __name__ == "__main__":
    unittest.main()
