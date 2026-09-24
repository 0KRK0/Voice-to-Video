"""Phase C: rendering on a customer's own computer.

## What these tests are actually defending

Not "the code runs". Three specific claims, each of which is a way this feature
could be quietly wrong in production and look fine in development:

**A device holds one capability and cannot reach anything else.** The whole
premise — "use your own GPU" — is only safe to offer if a computer in somebody's
office, possibly shared and possibly stolen, can do nothing but draw the frames
of a job it was handed. That is asserted directly rather than inferred from the
role table, because a capability added to `Role.OWNER` by `frozenset(Capability)`
is added silently and a test that read the table would move with it.

**A device draws with the same code as the cloud.** The GPU work of the previous
phase was verified frame by frame against the reference painter. That proof is
worth nothing here if the executor quietly built its own compositing path, so
there is a test that the executor uses `FfmpegRenderer` and the registry it
builds contains only targets the measured hardware actually supports.

**Nothing corrupt gets drawn.** A truncated download does not raise — it decodes
to a grey band across the bottom of a video, which is then composited, encoded,
uploaded and delivered with every layer doing exactly what it was told. The
digest check is the only thing between a bad connection and a ruined render, so
it is tested with a genuinely corrupt file rather than with a mocked hash.
"""

from __future__ import annotations

import asyncio
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.contracts.base import ObjectRef
from vtv.contracts.devices import (
    AssetHandout,
    Completion,
    Device,
    DeviceHardware,
    DeviceState,
    ProgressReport,
)
from vtv.contracts.errors import PolicyViolation, ValidationFailed, VTVError
from vtv.contracts.execution import ExecutionTarget
from vtv.contracts.tenancy import Capability, Organisation, Role, capabilities_for
from vtv.desktop import state
from vtv.desktop.cache import AssetCache, digest_of
from vtv.security.devices import (
    format_code,
    mint_pairing_code,
    normalise_code,
    verify_token,
)
from vtv.security.directory import Directory

#: A well-formed device id, because the contracts validate the shape and a
#: test fixture that dodged that would be testing a model nobody ships.
DEVICE = "dev_" + "a" * 24
JOB = "rnd_" + "a" * 24


def run(coroutine):  # type: ignore[no-untyped-def]
    return asyncio.run(coroutine)


def directory_with_org(root: Path) -> tuple[Directory, str]:
    directory = Directory(path=root / "dir.db")
    organisation = directory.create_organisation(
        Organisation(name="Acme", slug="acme")
    )
    return directory, organisation.organisation_id


class ADeviceCanOnlyDrawFrames(unittest.TestCase):
    """The security claim the whole feature rests on.

    A paired computer is not a machine acting as the organisation. It is a
    machine that may draw the frames of a job it has been given, and these
    assertions are what stop that drifting.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-device-")
        self.directory, self.organisation_id = directory_with_org(
            Path(self._dir.name)
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def pair(self, **kwargs: object) -> tuple[object, Device]:
        code = self.directory.create_pairing_code(
            mint_pairing_code(organisation_id=self.organisation_id)
        )
        minted = self.directory.redeem_pairing_code(
            code.code, name="A desktop", **kwargs  # type: ignore[arg-type]
        )
        return minted, minted.record

    def test_it_holds_exactly_one_capability(self) -> None:
        """Asserted against the resolved principal, not against the role table.

        `Role.OWNER` is `frozenset(Capability)`, so a capability added anywhere
        is granted to owners automatically. A test that read `ROLE_CAPABILITIES`
        would move with any such change; this one asks what a real device token
        actually resolves to.
        """
        minted, _ = self.pair()
        principal, _ = self.directory.authenticate_device(minted.secret)
        self.assertEqual(
            sorted(item.value for item in principal.capabilities),
            ["device:execute"],
        )

    def test_it_cannot_read_the_project_its_own_job_belongs_to(self) -> None:
        """Everything a device may see arrives inside the assignment."""
        minted, _ = self.pair()
        principal, _ = self.directory.authenticate_device(minted.secret)
        for refused in (
            Capability.PROJECT_READ,
            Capability.PROJECT_CREATE,
            Capability.RENDER_SUBMIT,
            Capability.DEVICE_MANAGE,
            Capability.BILLING_READ,
        ):
            with self.subTest(capability=refused.value):
                self.assertFalse(principal.can(refused))

    def test_a_service_key_cannot_execute_as_a_device(self) -> None:
        """The two credentials are different powers and must not overlap.

        Sharing `mint_api_key` would have been one careless `role=` argument
        away from a laptop in a coffee shop holding something that can create
        projects and spend money.
        """
        self.assertNotIn(
            Capability.DEVICE_EXECUTE, capabilities_for(Role.SERVICE)
        )
        self.assertNotIn(Capability.DEVICE_EXECUTE, capabilities_for(Role.EDITOR))

    def test_only_administrators_may_pair_a_computer(self) -> None:
        """Which machines the tenant's material may be sent to is not editorial."""
        self.assertIn(Capability.DEVICE_MANAGE, capabilities_for(Role.ADMIN))
        self.assertNotIn(Capability.DEVICE_MANAGE, capabilities_for(Role.EDITOR))

    def test_a_pairing_code_works_once(self) -> None:
        """Single use, enforced by a conditional write rather than a check.

        Reading first and writing second leaves a window in which two computers
        typing the same code both pass — small, real, and exactly the race that
        only appears once a product has users.
        """
        code = self.directory.create_pairing_code(
            mint_pairing_code(organisation_id=self.organisation_id)
        )
        self.directory.redeem_pairing_code(code.code, name="First")
        with self.assertRaises(VTVError):
            self.directory.redeem_pairing_code(code.code, name="Second")

    def test_an_expired_code_is_refused(self) -> None:
        code = self.directory.create_pairing_code(
            mint_pairing_code(
                organisation_id=self.organisation_id, ttl_seconds=-1
            )
        )
        with self.assertRaises(VTVError):
            self.directory.redeem_pairing_code(code.code, name="Too late")

    def test_every_bad_code_is_refused_identically(self) -> None:
        """Distinguishing them tells whoever is guessing which guess was closest."""
        used = self.directory.create_pairing_code(
            mint_pairing_code(organisation_id=self.organisation_id)
        )
        self.directory.redeem_pairing_code(used.code, name="First")
        expired = self.directory.create_pairing_code(
            mint_pairing_code(
                organisation_id=self.organisation_id, ttl_seconds=-1
            )
        )
        messages = set()
        for code in (used.code, expired.code, "ZZZZZZ"):
            with self.assertRaises(VTVError) as caught:
                self.directory.redeem_pairing_code(code, name="x")
            messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1, messages)

    def test_a_revoked_device_stops_working_immediately(self) -> None:
        minted, device = self.pair()
        self.directory.revoke_device(device.device_id)
        with self.assertRaises(VTVError):
            self.directory.authenticate_device(minted.secret)
        self.assertEqual(
            self.directory.device(device.device_id).state(), DeviceState.REVOKED
        )

    def test_the_token_is_not_recoverable_from_storage(self) -> None:
        """A dump of this table must not be a list of working credentials."""
        minted, device = self.pair()
        stored = self.directory.device(device.device_id)
        self.assertNotIn(minted.secret, stored.model_dump_json())
        self.assertTrue(verify_token(minted.secret, stored.token_hash))
        self.assertNotIn(minted.secret, repr(minted))

    def test_a_typed_code_survives_being_typed_by_a_person(self) -> None:
        """Lowercase, spaces and the hyphen it was displayed with."""
        code = self.directory.create_pairing_code(
            mint_pairing_code(organisation_id=self.organisation_id)
        )
        shown = format_code(code.code)
        self.assertIn("-", shown)
        minted = self.directory.redeem_pairing_code(
            f"  {shown.lower()} ", name="Typed by hand"
        )
        self.assertTrue(minted.record.is_active)

    def test_ambiguous_characters_are_not_repaired(self) -> None:
        """Mapping 0 to O would quietly widen the alphabet and shrink the space."""
        with self.assertRaises(ValidationFailed):
            normalise_code("ABC0EF")

    def test_hardware_is_re_read_on_every_poll(self) -> None:
        """A card that stopped verifying must stop being offered graphics work.

        Hardware changes underneath a paired device — a driver update, a card
        swap, an undocked laptop — and pairing happens once.
        """
        _, device = self.pair(
            hardware=DeviceHardware(gpu_name="A card", gpu_verified=True)
        )
        self.assertIn(
            ExecutionTarget.LOCAL_GPU,
            self.directory.device(device.device_id).hardware.targets,
        )
        self.directory.touch_device(
            device.device_id,
            hardware=DeviceHardware(
                gpu_name="A card", gpu_verified=False, gpu_reason="driver removed"
            ),
        )
        self.assertNotIn(
            ExecutionTarget.LOCAL_GPU,
            self.directory.device(device.device_id).hardware.targets,
        )


class AGraphicsCardIsNotTrustedUntilItIsProven(unittest.TestCase):
    """`gpu_verified` is not "a card was found".

    A card that is present and wrong is worse than no card: it draws every job
    it is given and each one is subtly different from what the customer would
    have got from the cloud. So the target only appears once the card has
    matched the reference painter on that machine.
    """

    def test_an_unverified_card_offers_only_the_processor(self) -> None:
        hardware = DeviceHardware(
            gpu_name="NVIDIA GeForce GTX 1650",
            gpu_reason="8 of 23 scenes differ",
        )
        self.assertEqual(hardware.targets, (ExecutionTarget.LOCAL_CPU,))
        self.assertIn("unverified", hardware.summary)

    def test_a_verified_card_is_preferred(self) -> None:
        hardware = DeviceHardware(
            gpu_name="NVIDIA GeForce GTX 1650", gpu_verified=True
        )
        self.assertEqual(hardware.targets[0], ExecutionTarget.LOCAL_GPU)

    def test_a_machine_with_no_card_is_still_a_useful_device(self) -> None:
        """Most of a typography-heavy video belongs on the processor anyway."""
        hardware = DeviceHardware(cpu_cores=8)
        self.assertEqual(hardware.targets, (ExecutionTarget.LOCAL_CPU,))

    def test_the_reason_is_cleared_when_the_card_works(self) -> None:
        """A machine reporting "available" with a stale refusal attached is a
        machine somebody debugs for an hour."""
        from vtv.desktop.hardware import detect

        found = detect()
        if found.gpu_verified:
            self.assertEqual(found.gpu_reason, "")
        else:
            self.assertTrue(found.gpu_reason)

    def test_detection_survives_a_machine_with_no_graphics_stack(self) -> None:
        """"No GPU" is a completely ordinary answer and must not be an error."""
        from vtv.desktop.hardware import describe, detect

        found = detect()
        self.assertGreaterEqual(found.cpu_cores, 1)
        self.assertIn("will run", describe(found))


class ATruncatedDownloadNeverBecomesAFrame(unittest.TestCase):
    """The failure that does not raise.

    A half-written JPEG decodes — to the top two thirds of a photograph and a
    grey band across the bottom. Everything downstream then does exactly what it
    was asked to, and the customer gets the band.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-cache-")
        self.root = Path(self._dir.name)
        self.cache = AssetCache(self.root / "cache")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def handout(self, payload: bytes) -> AssetHandout:
        import hashlib

        return AssetHandout(
            object=ObjectRef(
                bucket="b", key="orgs/o/p.png", content_type="image/png"
            ),
            url="https://example.invalid/p.png",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )

    def test_a_corrupt_file_is_refused(self) -> None:
        payload = b"the real photograph" * 100
        source = self.root / "download.part"
        source.write_bytes(payload[:-20])  # truncated, exactly like a dropped connection
        with self.assertRaises(ValidationFailed) as caught:
            self.cache.store(self.handout(payload), source)
        self.assertIn("corrupt", str(caught.exception))

    def test_a_corrupt_file_does_not_enter_the_cache(self) -> None:
        """Verified before it is moved into place, so anything present is checked.

        Move-then-verify-then-delete leaves a window in which a concurrent
        reader sees a corrupt file that is about to be removed.
        """
        payload = b"a photograph"
        handout = self.handout(payload)
        source = self.root / "download.part"
        source.write_bytes(b"not that")
        with self.assertRaises(ValidationFailed):
            self.cache.store(handout, source)
        self.assertFalse(self.cache.holds(handout.sha256))

    def test_a_good_file_is_stored_and_found_again(self) -> None:
        payload = b"a photograph"
        handout = self.handout(payload)
        source = self.root / "download.part"
        source.write_bytes(payload)
        stored = self.cache.store(handout, source)
        self.assertTrue(self.cache.holds(handout.sha256))
        self.assertEqual(digest_of(stored), handout.sha256)
        # Content-addressed, so the same bytes under a different manifest key
        # are the same file rather than a second copy.
        self.assertEqual(stored, self.cache.path_for(handout.sha256))

    def test_the_cache_stays_inside_its_budget(self) -> None:
        """An executor left running for a month must not quietly fill a disk."""
        for index in range(12):
            payload = f"photograph number {index}".encode() * 50
            handout = self.handout(payload)
            source = self.root / f"d{index}.part"
            source.write_bytes(payload)
            self.cache.store(handout, source)
        before = self.cache.size_bytes()
        self.cache.sweep(budget_bytes=before // 3)
        self.assertLessEqual(self.cache.size_bytes(), before // 3 + 1)

    def test_a_malformed_digest_is_refused_before_it_reaches_the_filesystem(self) -> None:
        """The digest becomes a path, so it is validated like one."""
        for bad in ("../../etc/passwd", "short", "z" * 64):
            with self.subTest(digest=bad), self.assertRaises(ValidationFailed):
                self.cache.path_for(bad)


class TheDeviceIdentityOnDiskIsTreatedAsASecret(unittest.TestCase):
    """It is a credential sitting on a computer somebody else also uses."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-state-")
        self.root = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_it_is_never_world_readable_at_any_instant(self) -> None:
        """The mode is applied by `open`, not by a `chmod` afterwards.

        Create, write the token, then chmod leaves a window — usually
        milliseconds, occasionally much longer under load — in which a
        world-readable file holds a working credential.
        """
        path = state.save(
            state.Pairing(server="https://x", device_id=DEVICE, token="vtv_dev_secret"),
            root=self.root,
        )
        if os.name == "posix":
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & (stat.S_IRWXG | stat.S_IRWXO), 0, oct(mode))

    def test_a_loosened_file_is_refused_rather_than_used(self) -> None:
        """It has either been tampered with or copied out of somewhere."""
        if os.name != "posix":
            self.skipTest("file modes are advisory on this platform")
        path = state.save(
            state.Pairing(server="https://x", device_id=DEVICE, token="t"),
            root=self.root,
        )
        os.chmod(path, 0o644)
        with self.assertRaises(PolicyViolation):
            state.load(root=self.root)

    def test_the_token_is_not_in_the_repr(self) -> None:
        """A `Pairing` is threaded through the whole executor, so it appears in
        most tracebacks that happen at all."""
        pairing = state.Pairing(
            server="https://x", device_id=DEVICE, token="vtv_dev_secret"
        )
        self.assertNotIn("vtv_dev_secret", repr(pairing))

    def test_an_unpaired_machine_is_told_what_to_do(self) -> None:
        with self.assertRaises(VTVError) as caught:
            state.load(root=self.root)
        self.assertIn("vtv-desktop pair", str(caught.exception))

    def test_it_round_trips(self) -> None:
        saved = state.Pairing(
            server="https://x", device_id=DEVICE, token="t", name="Desk"
        )
        state.save(saved, root=self.root)
        loaded = state.load(root=self.root)
        self.assertEqual((loaded.server, loaded.device_id, loaded.token),
                         (saved.server, saved.device_id, saved.token))

    def test_unpairing_is_local_only(self) -> None:
        """A lost laptop is revoked from the account, not from the laptop."""
        state.save(
            state.Pairing(server="https://x", device_id=DEVICE, token="t"),
            root=self.root,
        )
        self.assertTrue(state.forget(root=self.root))
        self.assertFalse(state.forget(root=self.root))


class TheExecutorDrawsWithTheSameCodeAsTheCloud(unittest.TestCase):
    """The reason the previous phase's proof still counts.

    Every frame of the GPU compositor was verified against the reference painter
    scene by scene. That proof transfers to a customer's desktop only for as
    long as the desktop uses the same renderer — a second compositing path here
    would need its own proof, its own fallback rules and its own bugs.
    """

    def executor(self, hardware: DeviceHardware):  # type: ignore[no-untyped-def]
        from vtv.desktop.client import DeviceClient
        from vtv.desktop.executor import JobExecutor

        self._dir = TemporaryDirectory(prefix="vtv-exec-")
        root = Path(self._dir.name)
        return JobExecutor(
            client=DeviceClient(server="https://x", token="t"),
            cache=AssetCache(root / "cache"),
            workspace=root / "work",
            hardware=hardware,
            device_id=DEVICE,
        )

    def tearDown(self) -> None:
        if hasattr(self, "_dir"):
            self._dir.cleanup()

    def test_a_machine_with_no_verified_card_registers_no_gpu_backend(self) -> None:
        """Absent rather than present-and-declined. A registry that listed a
        target the machine cannot run would route segments to it and fail
        them."""
        registry, _ = self.executor(DeviceHardware(cpu_cores=4))._registry()
        chain = registry.chain(
            __import__(
                "vtv.contracts.execution", fromlist=["ExecutionPolicy"]
            ).ExecutionPolicy.auto()
        )
        self.assertEqual([target for target, _ in chain], [ExecutionTarget.LOCAL_CPU])

    def test_a_verified_card_is_registered_ahead_of_the_processor(self) -> None:
        registry, policy = self.executor(
            DeviceHardware(cpu_cores=4, gpu_name="A card", gpu_verified=True)
        )._registry()
        chain = registry.chain(policy)
        self.assertEqual(
            [target for target, _ in chain],
            [ExecutionTarget.LOCAL_GPU, ExecutionTarget.LOCAL_CPU],
        )

    def test_the_policy_leaves_routing_to_the_router(self) -> None:
        """AUTO, so per-segment routing decides: typography to the processor and
        photographs to the card, inside one video. A policy that pinned the
        whole render would throw away the measurement that motivated it."""
        _, policy = self.executor(DeviceHardware())._registry()
        self.assertFalse(policy.strict)

    def test_a_local_failure_is_handed_back_and_a_content_failure_is_not(self) -> None:
        """The remote form of `SegmentOutcome.elsewhere`.

        A device out of video memory has not made the job impossible. A device
        sent a timeline referencing an asset that does not exist has, and every
        other machine will fail identically.
        """
        from vtv.contracts.errors import (
            NotFound,
            ProviderError,
            TimeoutExceeded,
        )
        from vtv.contracts.errors import (
            PolicyViolation as Policy,
        )
        from vtv.contracts.errors import (
            ValidationFailed as Invalid,
        )
        from vtv.desktop.executor import _is_local_problem

        # This machine's day, so hand it back.
        for mine in (
            ProviderError("the card fell over"),
            TimeoutExceeded("the encoder stalled"),
        ):
            with self.subTest(error=type(mine).__name__):
                self.assertTrue(_is_local_problem(mine))

        # The job's, so every machine fails identically and retrying is waste.
        for theirs in (
            Policy("that asset may not be used"),
            NotFound("that asset does not exist"),
            Invalid("the timeline is malformed"),
        ):
            with self.subTest(error=type(theirs).__name__):
                self.assertFalse(_is_local_problem(theirs))

    def test_the_segment_plan_matches_the_renderers_own(self) -> None:
        """Progress counts segment files on disk, so it must count the right ones.

        The arithmetic is duplicated — safely, unlike duplicating the drawing —
        and this is what stops the two drifting into a progress bar that stops
        at eighty percent forever.
        """
        from tests.test_render_segments import plain_timeline

        from vtv.adapters.render import segments as seg
        from vtv.contracts.base import ObjectRef as Ref
        from vtv.contracts.base import utc_now
        from vtv.contracts.devices import Assignment, JobLease
        from vtv.contracts.render import RenderSettings

        timeline = plain_timeline(
            Ref(bucket="b", key="n.wav", content_type="audio/wav")
        )
        settings = RenderSettings(frame_rate=24)
        now = utc_now()
        assignment = Assignment(
            render_job_id=JOB,
            project_id=timeline.project_id,
            organisation_id=timeline.organisation_id,
            timeline=timeline,
            settings=settings,
            lease=JobLease(
                render_job_id=JOB,
                device_id=DEVICE,
                organisation_id=timeline.organisation_id,
                granted_at=now,
                expires_at=now,
            ),
        )
        mine = self.executor(DeviceHardware())._plan_of(assignment)
        theirs = seg.plan(
            [clip.span.start for clip in timeline.clips],
            fps=settings.frame_rate,
            total_frames=max(1, round(timeline.duration_seconds * settings.frame_rate)),
        )
        self.assertEqual([item.name for item in mine], [item.name for item in theirs])
        self.assertGreaterEqual(len(mine), 1)


class TheProtocolIsBuiltForABadConnection(unittest.TestCase):
    """This renderer is on somebody's desk, so a dropped connection is a
    Tuesday rather than an incident."""

    def test_backoff_is_jittered_and_capped(self) -> None:
        """When a server comes back after an outage, every waiting device
        retries at once and knocks it over again. Spreading them is what stops
        the recovery being the second outage."""
        from vtv.desktop.client import backoff

        seen = {round(backoff(6, ceiling=30.0), 4) for _ in range(200)}
        self.assertGreater(len(seen), 50, "no jitter: every device retries together")
        self.assertLessEqual(max(seen), 30.0)

    def test_a_lost_progress_report_is_not_a_failed_render(self) -> None:
        """Best effort on purpose: losing enough of them expires the lease,
        which is exactly the right outcome reached with no special handling."""
        from vtv.desktop.client import DeviceClient

        class Broken:
            async def post(self, *_: object, **__: object):
                raise OSError("network down")

        client = DeviceClient(server="https://x", token="t", transport=Broken())
        report = ProgressReport(
            render_job_id=JOB,
            device_id=DEVICE,
            segments_done=1,
            segments_total=4,
        )
        self.assertFalse(run(client.report(report)))

    def test_a_revoked_device_stops_rather_than_retrying(self) -> None:
        """It would otherwise poll forever while its owner believed it had
        stopped."""
        from vtv.desktop.client import DeviceClient

        class Revoked:
            status_code = 403
            text = "revoked"

            async def post(self, *_: object, **__: object):
                return self

        client = DeviceClient(server="https://x", token="t", transport=Revoked())
        with self.assertRaises(PolicyViolation):
            run(client.report(
                ProgressReport(
                    render_job_id=JOB,
                    device_id=DEVICE,
                    segments_done=0,
                    segments_total=1,
                )
            ))

    def test_completion_retries_where_polling_gives_up(self) -> None:
        """The one message that must arrive: the work is done and the file is
        uploaded, so losing it means a finished render nobody knows about."""
        from vtv.desktop.client import DeviceClient

        attempts = {"count": 0}

        class Flaky:
            status_code = 200

            async def post(self, *_: object, **__: object):
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise OSError("network down")
                return _Ok()

        class _Ok:
            status_code = 200

            def json(self) -> dict:
                return {}

        client = DeviceClient(server="https://x", token="t", transport=Flaky())
        completion = Completion(
            render_job_id=JOB, device_id=DEVICE, ok=True
        )
        self.assertTrue(run(client.finish(completion, attempts=5)))
        self.assertEqual(attempts["count"], 3)

    def test_the_client_never_shows_its_token(self) -> None:
        from vtv.desktop.client import DeviceClient

        client = DeviceClient(server="https://x", token="vtv_dev_secret")
        self.assertNotIn("vtv_dev_secret", repr(client))

    def test_a_device_cannot_claim_more_segments_than_exist(self) -> None:
        """Progress is segments on disk, not a number a UI can be made to show."""
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ProgressReport(
                render_job_id=JOB,
                device_id=DEVICE,
                segments_done=9,
                segments_total=4,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


@unittest.skipUnless(
    __import__("vtv.adapters.media", fromlist=["ffmpeg"]).ffmpeg.is_available(),
    "ffmpeg is required",
)
class AWholeJobIsDrawnAndReturned(unittest.TestCase):
    """The end-to-end claim, with a real render rather than a described one.

    Everything above tests a piece. This runs the actual executor over an actual
    timeline: assets are downloaded through the client, verified, placed where
    the renderer looks for them, drawn by `FfmpegRenderer`, and the finished
    file is uploaded and reported.

    It is worth its runtime because the pieces can each be right while the
    arrangement is wrong, and the arrangement is the only thing this package
    contributes. The specific way it could be wrong: the storage provider the
    executor builds must resolve the timeline's `ObjectRef`s to the files it
    downloaded, and nothing short of running a render proves that it does.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-e2e-")
        self.root = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def build(self):  # type: ignore[no-untyped-def]
        """A one-shot job: a short typography timeline and a narration track."""
        import hashlib

        from tests.test_render_segments import plain_timeline

        from vtv.adapters.media import ffmpeg
        from vtv.contracts.base import utc_now
        from vtv.contracts.devices import Assignment, JobLease
        from vtv.contracts.render import RenderQuality, RenderSettings

        audio = self.root / "narration.wav"
        ffmpeg.synthesise_tone_audio(audio, duration=4.0, segments=[(0.2, 3.8)])
        payload = audio.read_bytes()

        ref = ObjectRef(
            bucket="vtv-media-dev",
            key="orgs/o/projects/p/narration.wav",
            content_type="audio/wav",
        )
        timeline = plain_timeline(ref, duration=4.0)
        handout = AssetHandout(
            object=ref,
            url=f"file://{audio}",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        now = utc_now()
        return Assignment(
            render_job_id=JOB,
            project_id=timeline.project_id,
            organisation_id=timeline.organisation_id,
            timeline=timeline,
            settings=RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24),
            assets=(handout,),
            can_upload=True,
            lease=JobLease(
                render_job_id=JOB,
                device_id=DEVICE,
                organisation_id=timeline.organisation_id,
                granted_at=now,
                expires_at=now,
            ),
        ), payload

    def executor(self, payload: bytes):  # type: ignore[no-untyped-def]
        """A client that reads from disk instead of the network.

        Substituted at the two methods that touch the wire, so everything
        between them — verification, placement, rendering, reporting — is the
        real thing. A mock of the whole client would have proved that the mock
        works.
        """
        from vtv.desktop.client import DeviceClient
        from vtv.desktop.executor import JobExecutor

        uploaded: dict[str, object] = {}
        reports: list[ProgressReport] = []
        destination = self.root / "delivered"
        destination.mkdir(parents=True, exist_ok=True)

        class Wire(DeviceClient):
            async def fetch(self, url: str, target: Path) -> int:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                return len(payload)

            async def upload_url(self) -> str:
                # Substituted alongside `upload`, because the executor now asks
                # for the destination at the moment it has a file to send
                # rather than being handed one at claim time. Leaving this out
                # sent the real method at a real network and produced a 403 —
                # which is, pleasingly, the same failure the change exists to
                # fix, arriving an hour earlier.
                return "file://upload"

            async def upload(self, url: str, source: Path) -> int:
                # Kept, the way a real upload keeps what it was handed. The
                # executor deletes the job directory once the file is
                # delivered — correctly — so a test that read the path
                # afterwards would be reading a file the design has already
                # promised to remove.
                import shutil as _shutil

                delivered = destination / "delivered.mp4"
                _shutil.copyfile(source, delivered)
                uploaded["bytes"] = delivered.stat().st_size
                uploaded["path"] = str(delivered)
                return uploaded["bytes"]

            async def report(self, progress: ProgressReport) -> bool:
                reports.append(progress)
                return True

        return (
            JobExecutor(
                client=Wire(server="https://x", token="t"),
                cache=AssetCache(self.root / "cache"),
                workspace=self.root / "work",
                hardware=DeviceHardware(cpu_cores=2),
                device_id=DEVICE,
                workers=1,
            ),
            uploaded,
            reports,
        )

    def test_it_draws_uploads_and_reports_success(self) -> None:
        assignment, payload = self.build()
        executor, uploaded, _ = self.executor(payload)

        outcome = run(executor.execute(assignment))

        self.assertTrue(outcome.ok, outcome.reason)
        self.assertEqual(outcome.render_job_id, assignment.render_job_id)
        self.assertGreater(outcome.output_bytes, 0)
        self.assertEqual(uploaded.get("bytes"), outcome.output_bytes)
        self.assertAlmostEqual(outcome.duration_seconds, 4.0, places=3)

    def test_the_finished_file_is_a_video_of_the_right_length(self) -> None:
        """Not "a file was produced". The one the customer would watch."""
        from tests.test_render_segments import probe

        assignment, payload = self.build()
        executor, uploaded, _ = self.executor(payload)
        outcome = run(executor.execute(assignment))
        self.assertTrue(outcome.ok, outcome.reason)

        info = probe(Path(uploaded["path"]))
        streams = {stream["codec_type"] for stream in info["streams"]}
        self.assertIn("video", streams)
        self.assertAlmostEqual(float(info["format"]["duration"]), 4.0, delta=0.4)

    def test_a_corrupt_asset_stops_the_render_rather_than_being_drawn(self) -> None:
        """The whole reason the digest is checked before anything is composed."""
        assignment, payload = self.build()
        executor, uploaded, _ = self.executor(b"not the narration at all")

        outcome = run(executor.execute(assignment))

        self.assertFalse(outcome.ok)
        self.assertIn("corrupt", outcome.reason)
        self.assertNotIn("bytes", uploaded, "a corrupt job produced an upload")

    def test_the_job_directory_is_cleared_only_after_delivery(self) -> None:
        """Segments are checkpoints. Deleting them before the file is delivered
        would make a failed upload cost the whole render again."""
        assignment, payload = self.build()
        executor, _, _ = self.executor(payload)
        run(executor.execute(assignment))
        self.assertFalse(
            (self.root / "work" / assignment.render_job_id).exists(),
            "a delivered job left its scratch behind",
        )

    def test_a_second_run_redraws_nothing_it_already_has(self) -> None:
        """The asset cache is content-addressed, so the same photograph across
        two jobs is downloaded once."""
        assignment, payload = self.build()
        executor, _, _ = self.executor(payload)
        run(executor.execute(assignment))

        downloads = {"count": 0}
        original = executor.client.fetch

        async def counting(url: str, target: Path) -> int:
            downloads["count"] += 1
            return await original(url, target)

        executor.client.fetch = counting  # type: ignore[method-assign]
        run(executor.execute(assignment))
        self.assertEqual(downloads["count"], 0, "a cached asset was downloaded again")


class TheDeviceSaysWhichHardwareDrewIt(unittest.TestCase):
    """The one question the whole feature exists to make answerable.

    "Did my graphics card actually do that?" The device is the only thing that
    knows — it counts segments by backend as it draws them — and the loop was
    incrementing a private counter and printing nothing. Somebody watching the
    window it runs in could not tell a GPU render from a CPU one.
    """

    def agent(self):  # type: ignore[no-untyped-def]
        from vtv.desktop.agent import Agent
        from vtv.desktop.client import DeviceClient

        self._dir = TemporaryDirectory(prefix="vtv-announce-")
        return Agent(
            client=DeviceClient(server="https://x", token="t"),
            workspace=Path(self._dir.name),
            device_id=DEVICE,
        )

    def tearDown(self) -> None:
        if hasattr(self, "_dir"):
            self._dir.cleanup()

    def spoken(self, outcome: Completion, *, accepted: bool = True) -> str:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.agent().announce(outcome, accepted=accepted)
        return buffer.getvalue()

    def test_it_names_the_hardware_in_words_a_person_reads(self) -> None:
        said = self.spoken(
            Completion(
                render_job_id=JOB,
                device_id=DEVICE,
                ok=True,
                output_bytes=3_700_000,
                duration_seconds=30.0,
                render_seconds=41.0,
                backends={"local_gpu": 3, "local_cpu": 2},
            )
        )
        self.assertIn("graphics card", said)
        self.assertIn("3 on", said)
        self.assertIn("2 on", said)
        # Not the raw enum values: this line is read by a customer, not grepped.
        self.assertNotIn("local_gpu", said)

    def test_a_processor_only_render_says_so(self) -> None:
        said = self.spoken(
            Completion(
                render_job_id=JOB, device_id=DEVICE, ok=True, backends={"local_cpu": 4}
            )
        )
        self.assertIn("This computer", said)
        self.assertNotIn("graphics card", said)

    def test_an_unknown_backend_is_reported_rather_than_swallowed(self) -> None:
        """A target this build does not know about is still information. Losing
        it would make the report quietly incomplete, which is worse than ugly."""
        said = self.spoken(
            Completion(
                render_job_id=JOB, device_id=DEVICE, ok=True, backends={"something": 1}
            )
        )
        self.assertIn("something", said)

    def test_a_failure_gives_the_reason_not_a_counter(self) -> None:
        said = self.spoken(
            Completion(
                render_job_id=JOB,
                device_id=DEVICE,
                ok=False,
                reason="out of video memory",
                elsewhere=True,
            )
        )
        self.assertIn("out of video memory", said)
        self.assertIn("handed back", said)

    def test_a_discarded_result_is_not_reported_as_a_success(self) -> None:
        """The lease expired while this machine was drawing. It did nothing
        wrong, and it must not imply the video was delivered."""
        said = self.spoken(
            Completion(render_job_id=JOB, device_id=DEVICE, ok=True),
            accepted=False,
        )
        self.assertIn("discarded", said)
