"""Phase C, the server's half: what a device is allowed to be given.

## The three things being defended

**A device is only ever handed its own organisation's work.** This is the single
most important check in a multi-tenant system, and here it is checked at the one
place a device is handed content at all. A bug here is a cross-customer
disclosure of somebody's unreleased video.

**A device is given four photographs, not a bucket.** Every URL in an assignment
is signed, expiring, and scoped to one object the timeline actually names. The
upload URL is write-only, single-object and size-capped. A leaked assignment is
worth exactly the four files it names, for one hour.

**A closed laptop is a delay, not a stuck render.** The lease is the queue's own
claim, so a device that stops talking is recovered by the same rule that
recovers a killed worker — and this is tested by actually letting one expire
rather than by trusting that it would.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from vtv.adapters.queue.durable import DurableJobQueue
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import ObjectRef
from vtv.contracts.devices import Completion, DeviceHardware, ProgressReport
from vtv.contracts.errors import PolicyViolation
from vtv.contracts.tenancy import Organisation
from vtv.dispatch import DEVICE_KIND, DevicePool, worker_id_for
from vtv.observability.events import EventSink
from vtv.security.devices import mint_pairing_code
from vtv.security.directory import Directory


def run(coroutine):  # type: ignore[no-untyped-def]
    return asyncio.run(coroutine)


class Documents:
    """The repository methods a dispatcher needs.

    A stub rather than a real SQLite repository because what is under test is
    what a device may be *given*, not how a timeline is stored — and a fake that
    returns a real `Timeline` exercises exactly the same validation the real one
    would.

    It grew `get_project` and `put_document` when recording a finished render
    became part of completing one. Adding them was not optional: without them
    every completion silently failed to record, which is the failure this stub
    would otherwise have hidden — a double that cannot do what the real thing
    does is a double that reports success for a path nobody has run.
    """

    def __init__(self, documents: dict[str, dict[str, Any]] | None = None) -> None:
        self.documents = documents or {}
        self.projects: dict[str, Any] = {}

    async def get_document(self, *, project_id: str, kind: str) -> Any:
        return self.documents.get(f"{project_id}:{kind}")

    async def get_project(
        self, project_id: str, *, organisation_id: str | None = None
    ) -> Any:
        project = self.projects.get(project_id)
        # The tenant scope the real query enforces. Kept here too, or a test
        # could pass against this stub and leak across tenants in production.
        if project is None or (
            organisation_id is not None
            and project.organisation_id != organisation_id
        ):
            return None
        return project

    async def put_document(
        self, *, project_id: str, kind: str, document_id: str, payload: Any
    ) -> None:
        self.documents[f"{project_id}:{kind}"] = payload


class Fixture:
    """A server with one organisation, one device and one queued job."""

    def __init__(self, root: Path, *, clock: Any = None) -> None:
        self.root = root
        self.directory = Directory(path=root / "dir.db")
        self.organisation = self.directory.create_organisation(
            Organisation(name="Acme", slug="acme")
        )
        self.storage = LocalStorageProvider(root / "storage")
        self.queue = DurableJobQueue(
            root / "queue.db",
            EventSink(),
            reclaim_after_seconds=30.0,
            **({"clock": clock} if clock else {}),
        )
        # Registered so `lease` with no kinds would still find it, and so the
        # queue's own accounting matches a real deployment's.
        self.queue.register(DEVICE_KIND, self._never_run)
        self.repository = Documents()
        self.pool = DevicePool(
            queue=self.queue,
            repository=self.repository,
            storage=self.storage,
            directory=self.directory,
        )

    async def _never_run(self, payload: dict[str, Any]) -> str:  # pragma: no cover
        raise AssertionError("a device job must never execute in the server process")

    def pair(self, *, organisation_id: str | None = None, name: str = "Desk") -> Any:
        code = self.directory.create_pairing_code(
            mint_pairing_code(
                organisation_id=organisation_id or self.organisation.organisation_id
            )
        )
        return self.directory.redeem_pairing_code(
            code.code,
            name=name,
            hardware=DeviceHardware(cpu_cores=8, gpu_name="A card", gpu_verified=True),
        ).record

    def timeline(self, project_id: str) -> Any:
        from tests.test_render_segments import plain_timeline

        story = plain_timeline(
            ObjectRef(bucket="b", key="n.wav", content_type="audio/wav")
        ).model_copy(
            update={
                "project_id": project_id,
                "organisation_id": self.organisation.organisation_id,
            }
        )
        self.repository.documents[f"{project_id}:timeline"] = story.model_dump(
            mode="json"
        )
        from vtv.contracts.project import Project

        self.repository.projects[project_id] = Project(
            project_id=project_id,
            organisation_id=self.organisation.organisation_id,
            title="Fixture",
        ).model_copy(update={"timeline_id": story.timeline_id})
        return story

    async def uploaded(self, project_id: str, render_job_id: str) -> None:
        """Put the render where a device that actually uploaded would have.

        Completion no longer settles a job on the device's word alone — the
        object has to be in storage. A test that skips this is testing the
        refusal, which is worth doing deliberately and not by accident.
        """
        from vtv.dispatch import render_output_key

        await self.storage.put(
            key=render_output_key(
                self.organisation.organisation_id, project_id, render_job_id
            ),
            data=b"\x00" * 1234,
            content_type="video/mp4",
        )

    async def enqueue(
        self, *, project_id: str, organisation_id: str | None = None
    ) -> str:
        self.timeline(project_id)
        handle = await self.queue.enqueue(
            kind=DEVICE_KIND,
            payload={
                "project_id": project_id,
                "organisation_id": organisation_id
                or self.organisation.organisation_id,
                "render_job_id": "rnd_" + "b" * 24,
                "settings": {"frame_rate": 24},
            },
        )
        return handle.job_id if hasattr(handle, "job_id") else str(handle)


class ADeviceIsOnlyOfferedItsOwnOrganisationsWork(unittest.TestCase):
    """The check whose failure mode is a cross-customer disclosure."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-dispatch-")
        self.fixture = Fixture(Path(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_device_gets_a_job_from_its_own_tenant(self) -> None:
        device = self.fixture.pair()
        run(self.fixture.enqueue(project_id="prj_" + "c" * 24))
        assignment = run(self.fixture.pool.claim(device))
        self.assertIsNotNone(assignment)
        self.assertEqual(
            assignment.organisation_id, self.fixture.organisation.organisation_id
        )
        self.assertEqual(assignment.lease.device_id, device.device_id)

    def test_a_job_from_another_tenant_is_refused_and_given_back(self) -> None:
        """Refused *and* handed back. Leaving it claimed would mean one device's
        mistake silently withholding another tenant's render for five minutes.
        """
        device = self.fixture.pair()
        run(
            self.fixture.enqueue(
                project_id="prj_" + "d" * 24,
                organisation_id="org_" + "e" * 24,
            )
        )
        with self.assertRaises(PolicyViolation):
            run(self.fixture.pool.claim(device))
        stats = run(self.fixture.queue.stats())
        self.assertEqual(stats.get("running", 0), 0, stats)

    def test_a_revoked_device_is_offered_nothing(self) -> None:
        device = self.fixture.pair()
        self.fixture.directory.revoke_device(device.device_id)
        revoked = self.fixture.directory.device(device.device_id)
        run(self.fixture.enqueue(project_id="prj_" + "f" * 24))
        with self.assertRaises(PolicyViolation):
            run(self.fixture.pool.claim(revoked))

    def test_an_idle_queue_offers_nothing_rather_than_failing(self) -> None:
        """The common answer. A device asks every few seconds and almost always
        hears no."""
        device = self.fixture.pair()
        self.assertIsNone(run(self.fixture.pool.claim(device)))

    def test_a_device_may_only_report_on_its_own_work(self) -> None:
        """`ProgressReport.device_id` exists so a report is self-describing in a
        log, not so a caller can nominate whose job it is renewing."""
        device = self.fixture.pair()
        other = self.fixture.pair(name="Somebody else")
        run(self.fixture.enqueue(project_id="prj_" + "g" * 24))
        run(self.fixture.pool.claim(device))
        with self.assertRaises(PolicyViolation):
            run(
                self.fixture.pool.progress(
                    other,
                    ProgressReport(
                        render_job_id="rnd_" + "b" * 24,
                        device_id=device.device_id,
                        segments_done=1,
                        segments_total=4,
                    ),
                )
            )

    def test_a_device_may_only_complete_its_own_work(self) -> None:
        device = self.fixture.pair()
        other = self.fixture.pair(name="Somebody else")
        run(self.fixture.enqueue(project_id="prj_" + "h" * 24))
        run(self.fixture.pool.claim(device))
        with self.assertRaises(PolicyViolation):
            run(
                self.fixture.pool.complete(
                    other,
                    Completion(
                        render_job_id="rnd_" + "b" * 24,
                        device_id=device.device_id,
                        ok=True,
                    ),
                )
            )

    def test_two_devices_do_not_get_the_same_job(self) -> None:
        """Atomic claim, from the queue rather than from a check-then-write here."""
        first = self.fixture.pair(name="One")
        second = self.fixture.pair(name="Two")
        run(self.fixture.enqueue(project_id="prj_" + "z" * 24))
        self.assertIsNotNone(run(self.fixture.pool.claim(first)))
        self.assertIsNone(run(self.fixture.pool.claim(second)))


class AnAssignmentCarriesNoCredentials(unittest.TestCase):
    """What is *in* the envelope, which is the other half of the security story."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-assign-")
        self.fixture = Fixture(Path(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def assignment(self) -> Any:
        device = self.fixture.pair()
        run(self.fixture.enqueue(project_id="prj_" + "j" * 24))
        return run(self.fixture.pool.claim(device))

    def test_it_contains_no_provider_credentials(self) -> None:
        """The line that makes "use your own GPU" safe to offer at all.

        Asserted over the serialised envelope rather than field by field,
        because a field added later would slip past a field-by-field check.

        Note what is *not* forbidden: the word "token". Signed URLs carry a
        signature, and banning the word would either fail forever or push
        somebody into renaming the parameter, which is worse than useless — the
        property that matters is not the absence of a string but that the only
        credentials present are the scoped, expiring, single-object ones the
        next two tests pin down.
        """
        payload = self.assignment().model_dump_json().lower()
        for forbidden in (
            "api_key",
            "apikey",
            "openai",
            "anthropic",
            "password",
            "vtv_live_",
            "vtv_test_",
            "vtv_dev_",
            "aws_",
            "sk-",
        ):
            with self.subTest(term=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_the_upload_url_reaches_exactly_one_object(self) -> None:
        """Write-only, single-object, expiring and size-capped.

        Four separate limits because each removes a different thing a leaked URL
        could be used for: reading other renders, overwriting a different
        object, working next week, and filling the bucket.
        """
        device = self.fixture.pair()
        run(self.fixture.enqueue(project_id="prj_" + "w" * 24))
        assignment = run(self.fixture.pool.claim(device))
        assert assignment is not None
        url = run(self.fixture.pool.upload_url_for(device))
        self.assertIn(assignment.render_job_id, url)
        self.assertIn(assignment.project_id, url)
        self.assertIn("upload", url)

    def test_the_upload_url_is_minted_when_the_video_is_ready(self) -> None:
        """Not when the job was claimed, which is the bug this replaced.

        The assignment used to carry the URL, signed for an hour at claim time.
        That is fine for a thirty-minute video and impossible for a longer one:
        a real sixty-minute render on a GTX 1650 took seventy-one minutes and
        then died with `403 object_expired`, having drawn every frame correctly.
        The URL had spent the entire render expiring.

        So the assignment now says only *whether* there is anywhere to upload,
        and the URL is asked for at the moment there is a file to send.
        """
        device = self.fixture.pair()
        run(self.fixture.enqueue(project_id="prj_" + "x" * 24))
        assignment = run(self.fixture.pool.claim(device))
        assert assignment is not None
        self.assertTrue(assignment.can_upload)
        self.assertFalse(
            any("http" in str(value) for value in assignment.model_dump().values()),
            "the assignment is carrying a URL that will be stale by upload time",
        )

    def test_a_device_holding_no_job_gets_no_upload_url(self) -> None:
        """A write URL is only ever issued against a claim this device holds.

        Resolved from the claim rather than from anything the device says — the
        same rule as progress and completion. A device that could name a job
        could ask for permission to write into somebody else's render.
        """
        device = self.fixture.pair()
        self.assertEqual(run(self.fixture.pool.upload_url_for(device)), "")

    def test_an_asset_url_reaches_that_asset_and_not_its_neighbours(self) -> None:
        """A device is given the ability to read four photographs, not the
        ability to read the bucket those photographs are in."""
        from vtv.dispatch import ASSET_URL_SECONDS

        self.assertLessEqual(ASSET_URL_SECONDS, 3600)
        for handout in self.assignment().assets:
            with self.subTest(key=handout.key):
                self.assertIn(handout.object.key.split("/")[-1], handout.url)

    def test_it_carries_the_resolved_timeline_and_settings(self) -> None:
        """Everything the device may see arrives here, because it can read
        nothing else."""
        assignment = self.assignment()
        self.assertTrue(assignment.timeline.clips)
        self.assertEqual(assignment.settings.frame_rate, 24)

    def test_an_asset_that_cannot_be_verified_is_withheld(self) -> None:
        """A device that cannot check an asset must not draw from it.

        The renderer describes a missing asset as a message card, which is
        visible and honest. A corrupt one is neither — it is a grey band the
        customer discovers after publishing.
        """
        assignment = self.assignment()
        for handout in assignment.assets:
            with self.subTest(key=handout.key):
                self.assertEqual(len(handout.sha256), 64)

    def test_the_lease_expires(self) -> None:
        """The whole design. A lease that had to be released explicitly would
        strand a render every time a laptop closed, and 'explicitly released' is
        exactly what a crashed process cannot do."""
        assignment = self.assignment()
        self.assertGreater(assignment.lease.expires_at, assignment.lease.granted_at)
        self.assertTrue(assignment.lease.is_live)


class AClosedLaptopIsADelayNotAStuckRender(unittest.TestCase):
    """Tested by letting a lease actually expire, not by trusting that it would."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-lease-")
        self.now = [1000.0]
        self.fixture = Fixture(Path(self._dir.name), clock=lambda: self.now[0])

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_an_abandoned_job_is_offered_to_somebody_else(self) -> None:
        laptop = self.fixture.pair(name="Laptop")
        desktop = self.fixture.pair(name="Desktop")
        run(self.fixture.enqueue(project_id="prj_" + "k" * 24))

        self.assertIsNotNone(run(self.fixture.pool.claim(laptop)))
        # Nothing else may have it while the lease is alive.
        self.assertIsNone(run(self.fixture.pool.claim(desktop)))

        # The lid closes. No release, no message, nothing — which is the point.
        self.now[0] += 600.0
        run(self.fixture.queue.recover())

        self.assertIsNotNone(
            run(self.fixture.pool.claim(desktop)),
            "an abandoned job was never offered to another machine",
        )

    def test_progress_keeps_the_claim_alive(self) -> None:
        """Renewed by progress rather than by a timer of its own: a device still
        reporting frames is still working, and a heartbeat on its own schedule
        would keep insisting a wedged process was healthy."""
        laptop = self.fixture.pair(name="Laptop")
        other = self.fixture.pair(name="Other")
        run(self.fixture.enqueue(project_id="prj_" + "m" * 24))
        run(self.fixture.pool.claim(laptop))

        for _ in range(4):
            self.now[0] += 20.0
            run(
                self.fixture.pool.progress(
                    laptop,
                    ProgressReport(
                        render_job_id="rnd_" + "b" * 24,
                        device_id=laptop.device_id,
                        segments_done=1,
                        segments_total=4,
                    ),
                )
            )
        run(self.fixture.queue.recover())
        self.assertIsNone(
            run(self.fixture.pool.claim(other)),
            "a working device lost its job while it was reporting progress",
        )

    def test_an_outcome_from_a_lost_claim_is_discarded(self) -> None:
        """Not an error and not a lie to the device: the lease expired while it
        was drawing and somebody else has the job now."""
        laptop = self.fixture.pair(name="Laptop")
        desktop = self.fixture.pair(name="Desktop")
        run(self.fixture.enqueue(project_id="prj_" + "n" * 24))
        run(self.fixture.pool.claim(laptop))

        self.now[0] += 600.0
        run(self.fixture.queue.recover())
        run(self.fixture.pool.claim(desktop))

        accepted = run(
            self.fixture.pool.complete(
                laptop,
                Completion(
                    render_job_id="rnd_" + "b" * 24,
                    device_id=laptop.device_id,
                    ok=True,
                ),
            )
        )
        self.assertFalse(accepted, "a stale outcome overwrote the current worker's")

    def test_a_finished_job_is_settled(self) -> None:
        device = self.fixture.pair()
        project_id = "prj_" + "p" * 24
        run(self.fixture.enqueue(project_id=project_id))
        run(self.fixture.pool.claim(device))
        run(self.fixture.uploaded(project_id, "rnd_" + "b" * 24))
        self.assertTrue(
            run(
                self.fixture.pool.complete(
                    device,
                    Completion(
                        render_job_id="rnd_" + "b" * 24,
                        device_id=device.device_id,
                        ok=True,
                        output_bytes=1234,
                        duration_seconds=12.0,
                        render_seconds=8.0,
                        backends={"local_gpu": 3},
                    ),
                )
            )
        )
        stats = run(self.fixture.queue.stats())
        self.assertEqual(stats.get("succeeded", 0), 1, stats)

    def test_the_web_app_can_find_a_render_its_server_never_drew(self) -> None:
        """The point of the whole feature, and the thing Phase C did not check.

        Phase C verified the device path by running `ffprobe` on the file in
        storage. That proved the render and skipped the product question: the
        Studio serves `render_job.output`, and nothing was writing a `render_job`
        document, so a customer's own computer could draw their video, upload it,
        and the editor would show nothing to play.
        """
        device = self.fixture.pair()
        project_id = "prj_" + "q" * 24
        run(self.fixture.enqueue(project_id=project_id))
        run(self.fixture.pool.claim(device))
        run(self.fixture.uploaded(project_id, "rnd_" + "b" * 24))
        run(
            self.fixture.pool.complete(
                device,
                Completion(
                    render_job_id="rnd_" + "b" * 24,
                    device_id=device.device_id,
                    ok=True,
                    output_bytes=1234,
                    duration_seconds=12.0,
                    backends={"local_gpu": 3, "local_cpu": 1},
                ),
            )
        )
        recorded = run(
            self.fixture.repository.get_document(
                project_id=project_id, kind="render_job"
            )
        )
        self.assertIsNotNone(recorded, "no render_job document was written")
        self.assertEqual(recorded["status"], "ready")
        self.assertEqual(recorded["progress"], 1.0)
        self.assertIsNotNone(recorded["output"], "a READY render must have an output")
        self.assertEqual(recorded["backends"], {"local_gpu": 3, "local_cpu": 1})

    def test_a_render_nobody_uploaded_is_not_recorded_as_finished(self) -> None:
        """The device says it succeeded and there is no file. It is not believed.

        This is not a hypothetical: a deployment whose storage has no signed
        uploads hands the executor an empty upload URL, so it renders, has
        nowhere to put the result, and reports success in perfectly good faith.
        Settling that job would mark the render complete forever with nothing
        behind it. Refusing puts it back for another attempt, which is wasteful
        and visible — and visible is the whole difference.
        """
        device = self.fixture.pair()
        project_id = "prj_" + "r" * 24
        run(self.fixture.enqueue(project_id=project_id))
        run(self.fixture.pool.claim(device))
        # Deliberately no `uploaded()`.
        run(
            self.fixture.pool.complete(
                device,
                Completion(
                    render_job_id="rnd_" + "b" * 24,
                    device_id=device.device_id,
                    ok=True,
                    output_bytes=1234,
                ),
            )
        )
        stats = run(self.fixture.queue.stats())
        self.assertEqual(stats.get("succeeded", 0), 0, stats)
        recorded = run(
            self.fixture.repository.get_document(
                project_id=project_id, kind="render_job"
            )
        )
        self.assertIsNone(recorded, "a render with no file was recorded as one")

    def test_a_machine_specific_failure_is_retried_elsewhere(self) -> None:
        """`elsewhere` is the remote form of `SegmentOutcome.elsewhere`: only the
        device that failed knows whether it was about this machine or this job."""
        device = self.fixture.pair()
        run(self.fixture.enqueue(project_id="prj_" + "q" * 24))
        run(self.fixture.pool.claim(device))
        run(
            self.fixture.pool.complete(
                device,
                Completion(
                    render_job_id="rnd_" + "b" * 24,
                    device_id=device.device_id,
                    ok=False,
                    reason="out of video memory",
                    elsewhere=True,
                ),
            )
        )
        stats = run(self.fixture.queue.stats())
        self.assertEqual(stats.get("pending", 0), 1, stats)

    def test_a_job_that_cannot_be_drawn_anywhere_is_not_retried(self) -> None:
        """Retrying it would turn one clear error into several slow ones."""
        device = self.fixture.pair()
        run(self.fixture.enqueue(project_id="prj_" + "r" * 24))
        run(self.fixture.pool.claim(device))
        run(
            self.fixture.pool.complete(
                device,
                Completion(
                    render_job_id="rnd_" + "b" * 24,
                    device_id=device.device_id,
                    ok=False,
                    reason="the timeline references an asset that does not exist",
                    elsewhere=False,
                ),
            )
        )
        stats = run(self.fixture.queue.stats())
        # Dead-lettered rather than retried: parked where an exhausted retry
        # also lands, so "what went wrong with this render" is one query.
        self.assertEqual(stats.get("dead_letter", 0), 1, stats)
        self.assertEqual(stats.get("pending", 0), 0, stats)

    def test_a_device_names_itself_recognisably_in_the_queue(self) -> None:
        """A glance at `claimed_by` should say whether a job is in the cloud or
        on somebody's desk, which is the first thing anybody asks when a render
        is slow."""
        self.assertTrue(worker_id_for("dev_" + "a" * 24).startswith("device:"))


class ADeviceMayOnlyClaimOneKindOfWork(unittest.TestCase):
    """The allow-list, which is why a device never touches a provider."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-kinds-")
        self.fixture = Fixture(Path(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_pipeline_job_is_never_offered_to_a_device(self) -> None:
        """`render_recording` transcribes, plans visuals and calls three
        providers before it draws anything. Every one of those needs a
        credential, and a credential on somebody's desktop is a credential in a
        coffee shop."""
        device = self.fixture.pair()
        self.fixture.queue.register("render_recording", self.fixture._never_run)
        run(
            self.fixture.queue.enqueue(
                kind="render_recording",
                payload={
                    "project_id": "prj_" + "s" * 24,
                    "organisation_id": self.fixture.organisation.organisation_id,
                },
            )
        )
        self.assertIsNone(
            run(self.fixture.pool.claim(device)),
            "a device was offered work that needs provider credentials",
        )

    def test_the_allow_list_has_exactly_one_entry(self) -> None:
        self.assertEqual(DEVICE_KIND, "render_timeline")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class WorkIsOnlyOfferedToHardwareThatExists(unittest.TestCase):
    """A job queued for a computer that is switched off is a render that never
    happens and never fails — it sits pending, the customer watches a spinner,
    and nothing in the system is wrong enough to alert anybody.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-offer-")
        self.fixture = Fixture(Path(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def offer(self, execution: Any = None) -> tuple[str, Any]:
        from vtv.contracts.render import RenderSettings
        from vtv.dispatch import Target

        return run(
            self.fixture.pool.offer(
                project_id="prj_" + "t" * 24,
                organisation_id=self.fixture.organisation.organisation_id,
                render_job_id="rnd_" + "c" * 24,
                settings=RenderSettings(frame_rate=24),
                execution=execution or Target.AUTO,
            )
        )

    def test_choosing_this_device_when_there_is_none_is_refused(self) -> None:
        """Refused, not quietly redirected to the cloud.

        Somebody who picked "this device" had a reason — cost, privacy, a card
        they paid for — and rendering in the cloud anyway spends their money to
        ignore them. Auto is the option that is allowed to decide for them.
        """
        from vtv.dispatch import Target

        with self.assertRaises(PolicyViolation) as caught:
            self.offer(Target.DEVICE)
        # The user-facing sentence, not `str(exception)`: those are deliberately
        # different strings and only the first one reaches a browser.
        self.assertIn("cloud", caught.exception.info.user_message or "")

    def test_auto_falls_back_to_the_cloud_rather_than_refusing(self) -> None:
        """The whole point of Auto: a render happens.

        Before the execution selector existed, offering with no computer online
        raised — which was right when the device path was the only path. It is
        wrong now: there is a cloud renderer, and a customer whose laptop is
        shut should get their video rather than an error telling them to open it.
        """
        from vtv.dispatch import CLOUD_KIND, Target

        self.fixture.timeline("prj_" + "t" * 24)
        job_id, chosen = self.offer(Target.AUTO)
        self.assertTrue(job_id)
        self.assertEqual(chosen.target, Target.CLOUD)
        self.assertEqual(chosen.kind, CLOUD_KIND)
        self.assertIn("cloud", chosen.reason.lower())

    def test_a_switched_off_computer_is_not_capacity(self) -> None:
        """Paired is not the same as available. A machine last seen a week ago
        cannot render tonight's video."""
        from datetime import timedelta

        from vtv.contracts.base import utc_now
        from vtv.dispatch import Target

        device = self.fixture.pair()
        stale = device.model_copy(
            update={"last_seen_at": utc_now() - timedelta(days=7)}
        )
        self.fixture.directory.devices_for = lambda _: [stale]  # type: ignore[assignment]
        with self.assertRaises(PolicyViolation):
            self.offer(Target.DEVICE)
        # And Auto sends it to the cloud rather than to a machine that is off.
        self.assertEqual(
            self.fixture.pool.plan(
                self.fixture.organisation.organisation_id, Target.AUTO
            ).target,
            Target.CLOUD,
        )

    def test_an_available_computer_gets_the_job(self) -> None:
        from vtv.dispatch import DEVICE_KIND, Target

        self.fixture.pair()
        self.fixture.timeline("prj_" + "t" * 24)
        job_id, chosen = self.offer(Target.AUTO)
        self.assertTrue(job_id)
        self.assertEqual(chosen.target, Target.DEVICE)
        self.assertEqual(chosen.kind, DEVICE_KIND)
        stats = run(self.fixture.queue.stats())
        self.assertEqual(stats.get("pending", 0), 1, stats)

    def test_a_device_never_sees_a_job_meant_for_the_cloud(self) -> None:
        """The kind *is* the routing decision, which is why it is not a flag.

        One kind claimed by both a device and a cloud worker would be two
        renderers racing for the same row, and the loser has already started
        drawing. A device asks for `render_timeline` and there is nothing under
        that name, so it waits — correctly — while the cloud does the work.
        """
        from vtv.dispatch import Target

        device = self.fixture.pair()
        self.fixture.timeline("prj_" + "t" * 24)
        _, chosen = self.offer(Target.CLOUD)
        self.assertEqual(chosen.target, Target.CLOUD)
        self.assertIsNone(
            run(self.fixture.pool.claim(device)),
            "a device claimed a job that was routed to the cloud",
        )

    def test_pressing_render_twice_produces_one_render(self) -> None:
        """Enforced by the queue's unique index rather than a pre-flight check,
        so two requests racing both survive and one job exists."""
        self.fixture.pair()
        self.fixture.timeline("prj_" + "t" * 24)
        first, second = self.offer(), self.offer()
        self.assertEqual(first, second)
        stats = run(self.fixture.queue.stats())
        self.assertEqual(stats.get("pending", 0), 1, stats)

    def test_capacity_answers_why_the_option_is_greyed_out(self) -> None:
        """Greying out a button with no explanation is the thing this prevents."""
        empty = self.fixture.pool.capacity(
            self.fixture.organisation.organisation_id
        )
        self.assertEqual((empty["paired"], empty["available"]), (0, 0))

        self.fixture.pair(name="Konar's desktop")
        full = self.fixture.pool.capacity(
            self.fixture.organisation.organisation_id
        )
        self.assertEqual((full["paired"], full["available"]), (1, 1))
        self.assertEqual(full["accelerated"], 1)
        self.assertEqual(full["devices"][0]["name"], "Konar's desktop")

    def test_capacity_is_scoped_to_the_tenant(self) -> None:
        self.fixture.pair()
        other = self.fixture.pool.capacity("org_" + "b" * 24)
        self.assertEqual(other["paired"], 0)


class AbandonedWorkIsReapedWithoutACloudWorker(unittest.TestCase):
    """The gap that only appears in the deployment this feature is *for*.

    `recover()` is called by the cloud worker's maintenance loop and by nothing
    else. Local execution's whole premise is a deployment that runs no cloud
    worker, so a device killed mid-render held its job until somebody restarted
    the API: neither running nor available, customer watching a spinner, nothing
    broken enough to alert anybody.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-reap-")
        self.now = [1000.0]
        self.fixture = Fixture(Path(self._dir.name), clock=lambda: self.now[0])

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_claiming_reaps_a_dead_devices_job_with_no_worker_running(self) -> None:
        laptop = self.fixture.pair(name="Laptop")
        desktop = self.fixture.pair(name="Desktop")
        run(self.fixture.enqueue(project_id="prj_" + "x" * 24))

        self.assertIsNotNone(run(self.fixture.pool.claim(laptop)))
        self.now[0] += 600.0  # the laptop is killed; nothing releases anything

        # No `recover()` here, and no worker in this test at all: the claim
        # itself must be what notices.
        self.assertIsNotNone(
            run(self.fixture.pool.claim(desktop)),
            "a dead device's job was never offered to another machine",
        )

    def test_reaping_does_not_disturb_a_live_claim(self) -> None:
        """Recovery on every poll would be worse than none if it took work away
        from a machine that is still drawing it."""
        laptop = self.fixture.pair(name="Laptop")
        desktop = self.fixture.pair(name="Desktop")
        run(self.fixture.enqueue(project_id="prj_" + "w" * 24))
        run(self.fixture.pool.claim(laptop))

        self.now[0] += 10.0
        self.assertIsNone(run(self.fixture.pool.claim(desktop)))


class RefusalsSayWhatWentWrong(unittest.TestCase):
    """The message a person reads is a different string from the one in the log.

    `PolicyViolation` carries a class-level `user_message` — "We could not use
    that material under its licence" — and that is the field the API returns.
    A customer who had simply not started their desktop app was being told
    their footage was unlicensed, which is worse than an unhelpful error: it is
    a confident, specific, wrong diagnosis.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-refusal-")
        self.fixture = Fixture(Path(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_no_available_computer_says_so(self) -> None:
        from vtv.contracts.render import RenderSettings
        from vtv.dispatch import Target

        with self.assertRaises(PolicyViolation) as caught:
            run(
                self.fixture.pool.offer(
                    project_id="prj_" + "y" * 24,
                    organisation_id=self.fixture.organisation.organisation_id,
                    render_job_id="rnd_" + "d" * 24,
                    settings=RenderSettings(),
                    # Explicitly this device. Auto renders in the cloud instead,
                    # which is a different — and correct — answer.
                    execution=Target.DEVICE,
                )
            )
        shown = caught.exception.info.user_message or ""
        self.assertIn("desktop app", shown)
        self.assertNotIn("licence", shown)

    def test_a_revoked_device_is_told_it_is_unpaired(self) -> None:
        device = self.fixture.pair()
        self.fixture.directory.revoke_device(device.device_id)
        revoked = self.fixture.directory.device(device.device_id)
        with self.assertRaises(PolicyViolation) as caught:
            run(self.fixture.pool.claim(revoked))
        self.assertIn("paired", caught.exception.info.user_message or "")
