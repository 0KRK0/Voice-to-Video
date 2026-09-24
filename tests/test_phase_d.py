"""Phase D — the web and the desktop as one product.

Phase C proved a computer could be handed a render and give one back. None of it
was reachable from the editor: there was no way to *choose* where a render ran,
no cloud path to choose instead, no way to pair a machine without an
administrator minting a code first, and — the one that mattered most — nothing
wrote the document the Studio reads, so a video drawn on somebody's own computer
was invisible to the product that asked for it.

These are the tests for the parts that close that gap, and they are grouped by
the question each one answers rather than by module.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tests.test_product_scenarios import ProductTestCase
from vtv.contracts.devices import DeviceHardware
from vtv.contracts.tenancy import Organisation
from vtv.security.directory import Directory


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


class SigningInThroughABrowser(unittest.TestCase):
    """`vtv-desktop pair` with no code, the way desktop apps normally sign in.

    The typed-code flow needs somebody signed in *first*, on another machine, to
    mint a code and carry it over. That is right for an administrator building a
    render farm and wrong for the case the product is actually for: one person,
    one laptop, who has never opened the web app on it.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-pair-")
        self.directory = Directory(path=Path(self._dir.name) / "dir.db")
        self.organisation = self.directory.create_organisation(
            Organisation(name="Acme", slug="acme")
        )
        self.hardware = DeviceHardware(cpu_cores=8, gpu_name="A card")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def start(self) -> tuple[str, str]:
        device_code, user_code, _ = self.directory.open_pairing_request(
            name="DESKTOP-4F2A", hardware=self.hardware
        )
        return device_code, user_code

    def test_a_computer_waits_and_then_is_paired(self) -> None:
        device_code, user_code = self.start()

        self.assertIsNone(
            self.directory.collect_pairing(device_code),
            "a computer collected a credential nobody had approved",
        )
        self.assertTrue(
            self.directory.approve_pairing(
                user_code, organisation_id=self.organisation.organisation_id
            )
        )
        minted = self.directory.collect_pairing(device_code)
        self.assertIsNotNone(minted)
        assert minted is not None
        self.assertEqual(
            minted.record.organisation_id, self.organisation.organisation_id
        )

    def test_the_approver_decides_which_account(self) -> None:
        """The tenant is not known when the computer asks — that is the design.

        A request that arrived with an organisation attached would be a request
        that could name somebody else's. Instead it arrives belonging to nobody,
        and the person who approves it is the one who says whose it is.
        """
        other = self.directory.create_organisation(Organisation(name="Rival", slug="rival"))
        device_code, user_code = self.start()
        self.directory.approve_pairing(
            user_code, organisation_id=other.organisation_id
        )
        minted = self.directory.collect_pairing(device_code)
        assert minted is not None
        self.assertEqual(minted.record.organisation_id, other.organisation_id)

    def test_one_approval_pairs_exactly_one_computer(self) -> None:
        """Collection is a conditional UPDATE, not a check and then a write.

        A retrying client makes two collections racing likely rather than
        exotic: the first response is lost, the desktop retries, and both are in
        flight. Checking first would let both pass and produce two devices on
        one person's single "yes".
        """
        from vtv.contracts.errors import VTVError

        device_code, user_code = self.start()
        self.directory.approve_pairing(
            user_code, organisation_id=self.organisation.organisation_id
        )
        self.assertIsNotNone(self.directory.collect_pairing(device_code))
        with self.assertRaises(VTVError):
            self.directory.collect_pairing(device_code)

    def test_the_short_code_cannot_pair_anything_by_itself(self) -> None:
        """The half that is displayed is the half that authorises nothing.

        The user code is read off a screen, typed into another, and possibly
        said out loud. It is deliberately not the secret: collecting needs the
        device code, which was never shown to anybody.
        """
        from vtv.contracts.errors import VTVError

        _, user_code = self.start()
        self.directory.approve_pairing(
            user_code, organisation_id=self.organisation.organisation_id
        )
        with self.assertRaises(VTVError):
            self.directory.collect_pairing(user_code)

    def test_an_expired_request_pairs_nothing(self) -> None:
        from vtv.contracts.errors import VTVError

        device_code, user_code, _ = self.directory.open_pairing_request(
            name="Slow", hardware=self.hardware, ttl_seconds=0
        )
        self.assertFalse(
            self.directory.approve_pairing(
                user_code, organisation_id=self.organisation.organisation_id
            ),
            "an expired request was approved",
        )
        with self.assertRaises(VTVError):
            self.directory.collect_pairing(device_code)

    def test_the_approval_screen_can_name_the_computer(self) -> None:
        """So the confirmation carries something a person can actually check."""
        _, user_code = self.start()
        pending = self.directory.pending_pairing(user_code)
        assert pending is not None
        self.assertEqual(pending["name"], "DESKTOP-4F2A")
        self.assertIn("8 cores", pending["hardware"].summary)


class TheIdlePollWidensAndSnapsBack(unittest.TestCase):
    """Seventeen thousand requests a day, per computer, to be told nothing.

    That is a flat five-second poll on a machine nobody is using. The fix is a
    ladder — and the cost of a ladder is that a job queued while a machine has
    settled at sixty seconds waits up to a minute to be noticed. Both halves are
    tested here, because either one alone is the wrong design.
    """

    def test_the_interval_widens_towards_the_ceiling(self) -> None:
        from vtv.desktop.client import (
            IDLE_POLL_SECONDS,
            MAX_IDLE_POLL_SECONDS,
            idle_interval,
        )

        seen = [IDLE_POLL_SECONDS]
        for _ in range(20):
            seen.append(idle_interval(seen[-1]))
        self.assertGreater(seen[3], seen[1], "the ladder did not widen")
        self.assertEqual(seen[-1], MAX_IDLE_POLL_SECONDS)
        self.assertTrue(
            all(b >= a for a, b in zip(seen, seen[1:])),
            "the interval went backwards without a job or a hint",
        )

    def test_a_day_of_idling_costs_an_order_of_magnitude_less(self) -> None:
        """The number that justified the change, asserted so it stays true."""
        from vtv.desktop.client import IDLE_POLL_SECONDS, MAX_IDLE_POLL_SECONDS

        flat = 86_400 / IDLE_POLL_SECONDS
        settled = 86_400 / MAX_IDLE_POLL_SECONDS
        self.assertGreater(flat / settled, 8.0)

    def test_a_held_claim_is_what_actually_delivers_the_latency(self) -> None:
        """The ladder alone could not, and a real run is what showed it.

        A device measured on a real machine settled to a 52-second gap, which
        was the traffic goal met. Then Render was pressed and it started **49
        seconds later**: a `Retry-After` can only shorten the poll *after* the
        next one, and the poll whose lateness was the problem is the one already
        in flight. The header was answering a question nobody had asked yet.

        Holding the request open is what fixes it, and it fixes the traffic at
        the same time — the machine is already connected when the job appears.
        These bounds are asserted so the two numbers cannot drift apart: a client
        that asks for longer than the server allows silently gets the server's
        ceiling and the arithmetic below stops being true.
        """
        from vtv.api.devices import MAX_CLAIM_WAIT_SECONDS
        from vtv.desktop.client import CLAIM_WAIT_SECONDS, IDLE_POLL_SECONDS

        self.assertLessEqual(CLAIM_WAIT_SECONDS, MAX_CLAIM_WAIT_SECONDS)
        held = 86_400 / CLAIM_WAIT_SECONDS
        flat = 86_400 / IDLE_POLL_SECONDS
        self.assertLess(held, flat / 4, "holding the claim saved no traffic")

    def test_the_claim_timeout_outlasts_the_hold(self) -> None:
        """Otherwise the client aborts its own deliberate wait.

        A thirty-second timeout on a twenty-five-second hold leaves five
        seconds of margin for the network, and getting this backwards would
        present as an unreliable connection rather than as the arithmetic
        mistake it is.
        """
        from vtv.desktop.client import CLAIM_WAIT_SECONDS, POLL_TIMEOUT

        self.assertGreater(CLAIM_WAIT_SECONDS + POLL_TIMEOUT, CLAIM_WAIT_SECONDS + 5)

    def test_the_servers_hint_beats_the_ladder(self) -> None:
        """The one thing that can shorten an interval the device has widened.

        The device knows how long it has been idle. The server knows something
        it cannot: that somebody just pressed Render, or opened the panel and is
        watching a spinner. So the hint wins when it is present, and there is no
        hint at all when nobody is waiting.
        """
        from vtv.desktop.client import MAX_IDLE_POLL_SECONDS, _retry_after

        class Response:
            def __init__(self, value: Any) -> None:
                self.headers = {"retry-after": value} if value is not None else {}

        self.assertEqual(_retry_after(Response("2")), 2.0)
        self.assertIsNone(_retry_after(Response(None)))
        # A number off the network deciding how hard this machine hits somebody
        # else's server is a number that gets clamped.
        self.assertEqual(_retry_after(Response("99999")), MAX_IDLE_POLL_SECONDS)
        self.assertIsNone(_retry_after(Response("soon")))

    def test_a_broken_hint_never_breaks_a_poll(self) -> None:
        from vtv.desktop.client import _retry_after

        class Hostile:
            @property
            def headers(self) -> Any:
                raise RuntimeError("no headers here")

        self.assertIsNone(_retry_after(Hostile()))


class ChoosingWhereARenderRuns(ProductTestCase):
    """Auto, this device, or the cloud — on the route the button already uses.

    The picker is on the Studio's own `POST .../render`, not on a new endpoint
    beside it. The first attempt did add one, at exactly that path, and it could
    never fire: Starlette matches the first route, the Studio's was already
    there, and the new one looked like the feature while being unreachable.
    """

    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.plan(self.project_id)

    def _pair(self, name: str = "Test machine") -> None:
        code = self.client.post("/v1/devices/codes", headers=self.auth())
        self.assertEqual(code.status_code, 201, code.text)
        paired = self.client.post(
            "/v1/devices/pair",
            json={
                "code": code.json()["code"],
                "name": name,
                "hardware": {"cpu_cores": 8},
            },
        )
        self.assertEqual(paired.status_code, 201, paired.text)

    def _render(self, **body: object) -> Any:
        return self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json=body,
            headers=self.auth(),
        )

    def test_auto_names_where_it_chose_and_why(self) -> None:
        """An Auto that does not say what it decided is one nobody trusts twice."""
        self._pair()
        response = self._render(execution="auto")
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        # Never the word "auto": that is what was asked for, not what happened.
        self.assertEqual(body["execution"], "device")
        self.assertIn("Test machine", body["execution_reason"])

    def test_auto_renders_in_the_cloud_when_no_computer_is_available(self) -> None:
        response = self._render(execution="auto")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["execution"], "cloud")

    def test_the_default_is_auto(self) -> None:
        """An existing client that sends no `execution` keeps working."""
        response = self._render()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["execution"], "cloud")

    def test_choosing_this_device_with_none_available_is_refused(self) -> None:
        """Rather than quietly spending their money in the cloud instead."""
        response = self._render(execution="device")
        self.assertGreaterEqual(response.status_code, 400)
        self.assertIn("cloud", response.text.lower())

    def test_an_unknown_execution_is_named_rather_than_defaulted(self) -> None:
        """A typo that silently renders somewhere else is found on the bill."""
        response = self._render(execution="gpu")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("auto", response.text)

    def test_the_device_route_still_means_device(self) -> None:
        """Its name is a promise, and it was briefly broken.

        Sharing the handler with the picker made it default to Auto, so
        `/render/device` rendered in the cloud whenever no computer was
        available — the same route quietly doing the opposite of what it says.
        """
        response = self.client.post(
            f"/v1/projects/{self.project_id}/render/device",
            json={"execution": "cloud"},
            headers=self.auth(),
        )
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertIn("cloud", response.text.lower())

    def test_the_picker_can_show_the_consequence_before_the_click(self) -> None:
        """"Auto to Test machine", rather than three radio buttons and a shrug."""
        self._pair()
        response = self.client.get(
            f"/v1/projects/{self.project_id}/render/where", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        options = {row["execution"]: row for row in response.json()["options"]}
        self.assertEqual(options["auto"]["resolves_to"], "device")
        self.assertTrue(options["device"]["available"])
        self.assertTrue(options["cloud"]["available"])

    def test_an_unavailable_option_comes_with_the_sentence_saying_why(self) -> None:
        response = self.client.get(
            f"/v1/projects/{self.project_id}/render/where", headers=self.auth()
        )
        options = {row["execution"]: row for row in response.json()["options"]}
        self.assertFalse(options["device"]["available"])
        self.assertIn("desktop app", options["device"]["reason"])
        # And Auto still works, because it is allowed to decide for them.
        self.assertEqual(options["auto"]["resolves_to"], "cloud")

    def test_pressing_render_tells_the_computers_to_hurry(self) -> None:
        """The half of adaptive polling that buys the latency back.

        Without it a machine settled at a sixty-second poll takes up to a minute
        to notice a job somebody is watching for. The claim's 204 carries
        `Retry-After` only while somebody is actually waiting.
        """
        self._pair()
        self.assertFalse(
            self.assembly.directory.attention_wanted(self.org),
            "a fresh account should not be asking anybody to hurry",
        )
        self._render(execution="auto")
        self.assertTrue(self.assembly.directory.attention_wanted(self.org))

    def test_the_browser_pairing_flow_over_http(self) -> None:
        """Start on the computer, approve in the browser, collect on the computer."""
        started = self.client.post(
            "/v1/devices/pair/start",
            json={"name": "DESKTOP-4F2A", "hardware": {"cpu_cores": 8}},
        )
        self.assertEqual(started.status_code, 201, started.text)
        payload = started.json()
        self.assertIn("/devices/approve", payload["verification_uri"])

        waiting = self.client.post(
            "/v1/devices/pair/collect",
            json={"device_code": payload["device_code"]},
        )
        self.assertEqual(waiting.status_code, 202, "an unapproved computer was paired")

        shown = self.client.get(
            f"/v1/devices/pair/requests/{payload['user_code']}", headers=self.auth()
        )
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertEqual(shown.json()["name"], "DESKTOP-4F2A")

        approved = self.client.post(
            "/v1/devices/pair/approve",
            json={"code": payload["user_code"]},
            headers=self.auth(),
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        collected = self.client.post(
            "/v1/devices/pair/collect",
            json={"device_code": payload["device_code"]},
        )
        self.assertEqual(collected.status_code, 201, collected.text)
        self.assertTrue(collected.json()["token"])

        # And the token that came back actually authenticates as a device.
        claim = self.client.post(
            "/v1/devices/claim",
            json={"hardware": {"cpu_cores": 8}},
            headers={"authorization": f"Bearer {collected.json()['token']}"},
        )
        self.assertIn(claim.status_code, (200, 204), claim.text)

    def test_an_unapproved_request_pairs_nothing_over_http(self) -> None:
        started = self.client.post(
            "/v1/devices/pair/start", json={"name": "Impatient"}
        ).json()
        for _ in range(3):
            waiting = self.client.post(
                "/v1/devices/pair/collect",
                json={"device_code": started["device_code"]},
            )
            self.assertEqual(waiting.status_code, 202)
        self.assertEqual(
            len(self.client.get("/v1/devices", headers=self.auth()).json()["devices"]),
            0,
            "polling alone created a device",
        )


class AFailingJobStopsFailing(unittest.TestCase):
    """The bug that would have run somebody's laptop at full processor all night.

    A device that hands a job back says "not my fault, try somebody else". The
    queue took that as an instruction rather than a request and re-queued the
    row unconditionally — no attempt ceiling on that path at all. `recover()`
    had one, but `recover()` is for a worker that *crashed*; the polite path
    around it had none, which is the worse shape of the same bug: the rare
    failure is guarded and the common one is not.

    It was found by a real end-to-end run, where a fixture produced a timeline
    two seconds short of its own narration. The device drew it, failed, handed
    it back, was given it straight back, and did that **493 times** before
    anybody looked. Nothing reported it, because a pending job and a busy
    computer are both entirely normal things.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-surrender-")
        from vtv.adapters.queue.durable import DurableJobQueue
        from vtv.observability.events import EventSink

        self.queue = DurableJobQueue(
            Path(self._dir.name) / "queue.db", EventSink(), max_attempts=3
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _fail_once(self, worker: str) -> str | None:
        from vtv.contracts.errors import ErrorCategory, ErrorCode, ErrorInfo

        claimed = run(self.queue.lease(worker_id=worker, kinds=("render_timeline",)))
        if claimed is None:
            return None
        run(
            self.queue.surrender(
                claimed.job_id,
                worker,
                ErrorInfo.of(
                    ErrorCode.RENDER_FAILED,
                    ErrorCategory.INTERNAL,
                    "the card gave up",
                ),
                # The device's own claim that another machine might manage it.
                retry=True,
            )
        )
        return claimed.job_id

    def test_handing_a_job_back_forever_is_not_allowed(self) -> None:
        run(
            self.queue.enqueue(
                kind="render_timeline", payload={"project_id": "prj_" + "a" * 24}
            )
        )
        attempts = 0
        while self._fail_once("device:one") is not None and attempts < 50:
            attempts += 1

        self.assertLess(attempts, 50, "the job was handed back without limit")
        stats = run(self.queue.stats())
        self.assertEqual(stats.get("dead_letter", 0), 1, stats)
        self.assertEqual(stats.get("pending", 0), 0, stats)

    def test_a_job_with_attempts_left_is_still_offered_again(self) -> None:
        """The ceiling must not have removed the retry it is there to bound.

        A machine that ran out of video memory has not made the job impossible,
        and the whole point of `retry=True` is that a different computer gets a
        turn.
        """
        run(
            self.queue.enqueue(
                kind="render_timeline", payload={"project_id": "prj_" + "b" * 24}
            )
        )
        self.assertIsNotNone(self._fail_once("device:one"))
        self.assertIsNotNone(
            self._fail_once("device:two"),
            "a second computer was never offered the job",
        )

    def test_a_terminal_failure_never_gets_a_second_machine(self) -> None:
        from vtv.contracts.errors import ErrorCategory, ErrorCode, ErrorInfo

        run(
            self.queue.enqueue(
                kind="render_timeline", payload={"project_id": "prj_" + "c" * 24}
            )
        )
        claimed = run(
            self.queue.lease(worker_id="device:one", kinds=("render_timeline",))
        )
        assert claimed is not None
        run(
            self.queue.surrender(
                claimed.job_id,
                "device:one",
                ErrorInfo.of(
                    ErrorCode.RENDER_FAILED,
                    ErrorCategory.VALIDATION,
                    "this timeline does not cover its narration",
                ),
                retry=False,
            )
        )
        self.assertIsNone(
            run(self.queue.lease(worker_id="device:two", kinds=("render_timeline",)))
        )

    def test_an_unrenderable_timeline_is_the_jobs_fault_not_the_machines(self) -> None:
        """The classification that turned one error into four hundred of them.

        `TERMINAL_CATEGORIES` is what a device consults to decide whether a
        failure was about *this machine* or about *this job*. `RenderFailed`
        defaults to `INTERNAL` — "my fault, pass it on" — and a timeline that
        does not cover its own narration is wrong in exactly the same way on
        every computer in the world.

        Asked of the renderer rather than of the source, so it stays true if the
        check moves.
        """
        from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
        from vtv.contracts.base import ObjectRef
        from vtv.contracts.errors import TERMINAL_CATEGORIES, RenderFailed
        from vtv.contracts.render import RenderSettings

        from tests.test_render_segments import plain_timeline

        timeline = plain_timeline(
            ObjectRef(bucket="b", key="n.wav", content_type="audio/wav")
        )
        # Two seconds of narration nothing draws — the exact fixture mistake
        # that produced 493 attempts on a real machine.
        holed = timeline.model_copy(
            update={
                "narration": timeline.narration.model_copy(
                    update={
                        "duration_seconds": timeline.narration.duration_seconds + 2.0
                    }
                )
            }
        )
        from vtv.observability.events import EventSink

        renderer = FfmpegRenderer(
            storage=None, events=EventSink(), workdir=Path(self._dir.name)
        )
        with self.assertRaises(RenderFailed) as caught:
            run(renderer.render(timeline=holed, settings=RenderSettings()))
        self.assertIn(
            caught.exception.info.category,
            TERMINAL_CATEGORIES,
            "an unrenderable timeline is handed to the next machine to fail on too",
        )


class ARenderNobodyClaimedGoesToTheCloud(unittest.TestCase):
    """The laptop that was online at the button and shut before its next poll.

    "Auto" asks whether a computer is available at the moment somebody presses
    Render, which is the right question to ask then and stops being the right
    answer about ninety seconds later. The job then sits pending — not failed,
    not running, not wrong in any way the system can detect, because a pending
    job is an entirely normal thing — and the person watches a spinner until
    they give up.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-stranded-")
        from vtv.adapters.queue.durable import DurableJobQueue
        from vtv.dispatch import DevicePool
        from vtv.observability.events import EventSink

        self.clock = [1_000_000.0]
        self.queue = DurableJobQueue(
            Path(self._dir.name) / "queue.db",
            EventSink(),
            clock=lambda: self.clock[0],
        )
        self.pool = DevicePool(
            queue=self.queue, repository=None, storage=None, directory=None
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _queue_for_a_device(self) -> None:
        from vtv.dispatch import DEVICE_KIND

        run(
            self.queue.enqueue(
                kind=DEVICE_KIND,
                payload={
                    "project_id": "prj_" + "a" * 24,
                    "organisation_id": "org_" + "a" * 24,
                    "render_job_id": "rnd_" + "a" * 24,
                    "settings": {},
                },
            )
        )

    def test_a_job_nobody_takes_is_moved_to_the_cloud(self) -> None:
        from vtv.dispatch import CLOUD_KIND, DEVICE_KIND, STRANDED_SECONDS

        self._queue_for_a_device()
        self.assertEqual(run(self.pool.escalate()), 0, "moved too early")

        self.clock[0] += STRANDED_SECONDS + 1
        self.assertEqual(run(self.pool.escalate()), 1)

        # The device row is gone and a cloud row has taken its place, so the
        # work exists exactly once and only one renderer can find it.
        self.assertIsNone(
            run(self.queue.lease(worker_id="device:one", kinds=(DEVICE_KIND,)))
        )
        claimed = run(self.queue.lease(worker_id="cloud", kinds=(CLOUD_KIND,)))
        self.assertIsNotNone(claimed, "the cloud cannot see the escalated render")
        assert claimed is not None
        self.assertEqual(claimed.payload["render_job_id"], "rnd_" + "a" * 24)

    def test_a_job_a_device_is_already_running_is_left_alone(self) -> None:
        """Escalation is for work nobody ever claimed, not for slow work.

        A machine that took a job and went quiet is already handled, and handled
        better, by the queue's own lease recovery. Racing it here would take a
        render away from a computer that is three-quarters through drawing it.
        """
        from vtv.dispatch import DEVICE_KIND, STRANDED_SECONDS

        self._queue_for_a_device()
        run(self.queue.lease(worker_id="device:one", kinds=(DEVICE_KIND,)))
        self.clock[0] += STRANDED_SECONDS * 3
        self.assertEqual(run(self.pool.escalate()), 0)

    def test_escalating_twice_produces_one_cloud_render(self) -> None:
        """The sweep runs on a timer, so it will see the same world twice."""
        from vtv.dispatch import CLOUD_KIND, STRANDED_SECONDS

        self._queue_for_a_device()
        self.clock[0] += STRANDED_SECONDS + 1
        run(self.pool.escalate())
        run(self.pool.escalate())
        stats = run(self.queue.stats())
        self.assertEqual(stats.get("pending", 0), 1, stats)
        self.assertIsNotNone(
            run(self.queue.lease(worker_id="cloud", kinds=(CLOUD_KIND,)))
        )
        self.assertIsNone(
            run(self.queue.lease(worker_id="cloud2", kinds=(CLOUD_KIND,))),
            "one stranded render became two cloud renders",
        )


class WhatADeviceUploadsDoesNotLiveForever(unittest.TestCase):
    """The one write in the system that skipped `put`, and therefore skipped
    retention entirely.

    Every other object reaches storage through `put`, which takes a retention
    class. A signed upload does not: the bytes arrive over HTTP from a device
    and land on disk directly. They were landing with no class at all — and an
    object with no class is one `sweep_expired` will never delete, which
    `unclassified()` exists to find.

    So every video a customer's own computer rendered was being kept forever, by
    a system whose stated design is that it does not become a storage company.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-retention-")
        root = Path(self._dir.name)
        from vtv.api.app import create_app
        from vtv.config import Settings
        from vtv.wiring import build

        self.settings = Settings(
            asset_search_endpoint="",
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
            signing_key="test-signing-key-not-a-real-secret",
        )
        self.assembly = build(self.settings)
        from starlette.testclient import TestClient

        self.client = TestClient(create_app(self.settings, assembly=self.assembly))

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def test_an_uploaded_render_is_swept_like_any_other(self) -> None:
        from vtv.contracts.base import RetentionClass

        key = "orgs/o/projects/p/renders/r.mp4"
        url = run(
            self.assembly.storage.signed_upload_url(
                key=key,
                content_type="video/mp4",
                retention=RetentionClass.EPHEMERAL,
            )
        )
        response = self.client.put(
            url, content=b"\x00" * 64, headers={"content-type": "video/mp4"}
        )
        self.assertEqual(response.status_code, 201, response.text)

        self.assertEqual(
            run(self.assembly.storage.retention_of(key)),
            RetentionClass.EPHEMERAL,
            "an uploaded render has no retention class, so nothing will ever "
            "delete it",
        )
        self.assertNotIn(
            key,
            run(self.assembly.storage.unclassified()),
            "the uploaded render is invisible to the retention sweep",
        )

    def test_the_class_comes_from_the_token_not_the_handler(self) -> None:
        """So an upload URL cannot exist without one.

        Applying it in the route would put the rule at a call site, where the
        next signed-upload feature will forget it. Putting it in the signed
        token means the permission to write and the expiry of what is written
        are one object.
        """
        import base64
        import json as _json

        from vtv.contracts.base import RetentionClass

        url = run(
            self.assembly.storage.signed_upload_url(
                key="orgs/o/projects/p/renders/s.mp4",
                content_type="video/mp4",
                retention=RetentionClass.EPHEMERAL,
            )
        )
        token = url.split("token=", 1)[1]
        body = token.split(".", 1)[0]
        claims = _json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        self.assertEqual(claims.get("r"), RetentionClass.EPHEMERAL.value)


class ALeaseIsAPromiseAboutARealNumber(unittest.TestCase):
    """Three numbers described one fact, and which applied was luck.

    A device was **told** its lease was five minutes. The API's queue took the
    sixty-second default and would have reclaimed its job after one. The
    worker's queue used fifteen minutes. Whichever process happened to run
    `recover()` first decided, so a device drawing a segment that took over a
    minute could have had its work taken away while it was still drawing it,
    having been promised five.

    None of the three was wrong on its own. That is what made it survive: every
    number was defensible where it was written, and nothing put them side by
    side.
    """

    def test_the_lease_is_the_window_the_queue_enforces(self) -> None:
        import tempfile as _tempfile

        from vtv.adapters.queue.durable import (
            RECLAIM_AFTER_SECONDS,
            DurableJobQueue,
        )
        from vtv.dispatch import DevicePool
        from vtv.observability.events import EventSink

        queue = DurableJobQueue(
            Path(_tempfile.mkdtemp()) / "q.db",
            EventSink(),
            reclaim_after_seconds=RECLAIM_AFTER_SECONDS,
        )
        pool = DevicePool(
            queue=queue, repository=None, storage=None, directory=None
        )
        self.assertEqual(pool._lease_seconds(), queue.reclaim_after_seconds)

    def test_a_queue_that_says_nothing_falls_back_rather_than_lying(self) -> None:
        from vtv.contracts.devices import LEASE_TTL_SECONDS
        from vtv.dispatch import DevicePool

        class Silent:
            pass

        pool = DevicePool(
            queue=Silent(), repository=None, storage=None, directory=None
        )
        self.assertEqual(pool._lease_seconds(), float(LEASE_TTL_SECONDS))

    def test_the_api_and_the_worker_agree(self) -> None:
        """Two processes, one queue file, one answer.

        Asserted against the source because building both a worker and an
        application here to compare two numbers is a great deal of machinery
        for a fact that is one line in each.
        """
        api = Path("src/vtv/api/app.py").read_text(encoding="utf-8")
        worker = Path("src/vtv/worker.py").read_text(encoding="utf-8")
        for name, source in (("the API", api), ("the worker", worker)):
            self.assertIn(
                "reclaim_after_seconds=RECLAIM_AFTER_SECONDS",
                source,
                f"{name} sets its own reclaim window instead of the shared one",
            )


class ALogLineCannotStopARender(unittest.TestCase):
    """A decorative character killed every job on Windows.

    The provider trace announces each job with a line beginning `▸` (U+25B8).
    Windows gives a redirected stream the cp1252 codec unless told otherwise,
    and cp1252 has no `▸`. So with tracing on and the worker's output going to a
    log file, `print` raised `UnicodeEncodeError` **inside the context manager
    that wraps every handler** — the job died in ten milliseconds, before a
    frame was drawn, and the queue dead-lettered it after three identical
    instant failures.

    Reported as `internal_error`, three times, with no message. A render that
    was never attempted, recorded as a render that could not be done.
    """

    def _cp1252_stdout(self) -> Any:
        import io

        class Narrow(io.TextIOWrapper):
            pass

        return Narrow(io.BytesIO(), encoding="cp1252", errors="strict")

    def test_the_trace_survives_a_console_that_cannot_spell(self) -> None:
        import contextlib as _contextlib

        from vtv.observability.trace import say

        stream = self._cp1252_stdout()
        with _contextlib.redirect_stdout(stream):
            # The exact line that killed it.
            say("\n▸ render_timeline_cloud prj_x")
        stream.flush()

    def test_a_traced_job_still_runs_on_a_narrow_console(self) -> None:
        """The whole point: the work happens even when the description cannot.

        Asserted through `job()` rather than through `say` alone, because what
        broke was not the printing — it was the printing taking the handler
        down with it.
        """
        import contextlib as _contextlib

        from vtv.observability.trace import ProviderTrace

        trace = ProviderTrace(path=Path(self._dir.name) / "calls.jsonl", echo=True)
        ran = False
        stream = self._cp1252_stdout()
        with _contextlib.redirect_stdout(stream):
            with trace.job("render_timeline_cloud", "prj_x"):
                ran = True
        self.assertTrue(ran, "a log line prevented the work it was describing")

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-narrow-")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_the_entry_points_widen_their_streams(self) -> None:
        """Belt as well as braces, at the process boundary.

        Guarding `say` fixes the one line we know about. Making the streams
        tolerant fixes every line anybody writes later — including a graphics
        driver's name, which this program prints and does not choose.
        """
        worker = Path("src/vtv/worker.py").read_text(encoding="utf-8")
        cli = Path("src/vtv/desktop/cli.py").read_text(encoding="utf-8")
        self.assertIn("configure_logging", worker)
        self.assertIn("tolerant_streams()", cli)

        events = Path("src/vtv/observability/events.py").read_text(encoding="utf-8")
        self.assertIn(
            "tolerant_streams()",
            events.split("def configure_logging")[1][:600],
            "configure_logging no longer widens the streams first",
        )


class OneJobKindPerRenderer(unittest.TestCase):
    """The routing decision is the queue kind, made once, at the button."""

    def test_the_cloud_kind_has_exactly_one_spelling(self) -> None:
        """Two constants that agree today are two constants.

        The day they drift, cloud renders are enqueued under a kind no worker
        claims and sit pending forever — and nothing in the system reports that
        as broken, because a pending job is a perfectly normal thing.
        """
        from vtv.dispatch import CLOUD_KIND
        from vtv.jobs import HANDLERS, JobKind

        self.assertEqual(JobKind.RENDER_TIMELINE_CLOUD.value, CLOUD_KIND)
        self.assertIn(CLOUD_KIND, HANDLERS, "no worker can run a cloud render")

    def test_a_device_and_the_cloud_never_claim_the_same_kind(self) -> None:
        from vtv.dispatch import CLOUD_KIND, DEVICE_KIND
        from vtv.jobs import HANDLERS

        self.assertNotEqual(DEVICE_KIND, CLOUD_KIND)
        self.assertNotIn(
            DEVICE_KIND,
            HANDLERS,
            "a cloud worker can claim device work, so both may draw one job",
        )

    def test_something_actually_enqueues_the_cloud_kind(self) -> None:
        """A handler nothing produces work for is dead code in a good disguise.

        This is the check that was missing when the cloud kind was added. It had
        a handler, a registration, and a test asserting the two spellings agreed
        — and nothing anywhere enqueued one. Every one of those tests passed on
        a feature that could not be reached, which is a more comfortable kind of
        wrong than a failure and a worse one.

        Asserted against the source because the alternative is running the
        seven-minute escalation timer in a unit test, and what needs pinning is
        that a producer exists at all.
        """
        from vtv.dispatch import CLOUD_KIND

        producer = Path("src/vtv/dispatch.py").read_text(encoding="utf-8")
        self.assertTrue(
            "kind=CLOUD_KIND" in producer,
            "nothing enqueues a cloud render, so the handler is unreachable",
        )
        caller = Path("src/vtv/worker.py").read_text(encoding="utf-8")
        self.assertTrue(
            ".escalate(" in caller,
            "nothing ever calls the thing that enqueues a cloud render",
        )
        del CLOUD_KIND


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
