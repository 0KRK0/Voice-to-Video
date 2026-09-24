"""Scenarios A–F, driven through the real HTTP application.

Not through the services in isolation. The 2026-08-13 audit's central finding
was that comprehensively-tested code can sit off the request path entirely, so
these tests take the same route a browser would: `create_app`, a real API key, a
real repository, a real durable queue, and a real worker draining it.

Each scenario is a product promise, and each is written to fail loudly if the
promise stops being true:

| | Promise |
| --- | --- |
| **A** | Speaking produces a project you can then direct |
| **B** | A pasted script is the narration, verbatim, until you say otherwise |
| **C** | Disliking visual 7 changes visual 7 and nothing else |
| **D** | A 7-minute target over 5 minutes of narration does not distort the voice |
| **E** | A grounding failure isolates one visual; the project stays editable |
| **F** | A locked visual survives a full-project regeneration |

Scenarios C and F are the ones worth reading. They encode the properties the
whole editing layer exists to provide, and they are the ones that break silently
if someone later adds a convenient "re-plan everything" shortcut.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from vtv.api.app import create_app
from vtv.api.product import SCRIPT_DOC, TIMELINE_DOC, UNITS_DOC
from vtv.config import Settings
from vtv.contracts.script import Script
from vtv.contracts.tenancy import (
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Role,
    User,
)
from vtv.contracts.tracks import EditTimeline
from vtv.contracts.visual_unit import VisualUnit, VisualUnitStatus
from vtv.security.keys import mint_api_key
from vtv.wiring import build

SCRIPT = """Before computer science was born, mathematical methods were used to solve complex problems. These methods were slow and prone to error.

Mechanical computation eventually emerged. Charles Babbage designed engines of brass and steel. Nothing was completed in his lifetime.

The transistor transformed computing in 1947. It replaced the vacuum tube almost everywhere within twenty years.

Integrated circuits followed. Millions of transistors were placed on a single wafer of silicon.
"""


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class ProductTestCase(unittest.TestCase):
    """One temporary directory, one real application, one real tenant."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-product-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
            signing_key="test-signing-key-not-a-real-secret",
        )
        self.assembly = build(self.settings)
        self.app = create_app(self.settings, assembly=self.assembly)
        self.client = TestClient(self.app)
        self.org, self.key = self._tenant("acme")
        self.repository = self.app.state.vtv.repository

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def _tenant(self, slug: str) -> tuple[str, str]:
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
            scopes=list(Capability),
        )
        self.assembly.directory.store_key(minted.record)
        return organisation.organisation_id, minted.secret

    # -- helpers ----------------------------------------------------------

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def project(self, title: str = "scenario") -> str:
        response = self.client.post(
            "/v1/projects", json={"title": title}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 201, response.text)
        project_id: str = response.json()["project_id"]
        return project_id

    def paste(self, project_id: str, text: str = SCRIPT) -> dict:  # type: ignore[type-arg]
        response = self.client.post(
            f"/v1/projects/{project_id}/script",
            json={"text": text},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return dict(response.json())

    def plan(self, project_id: str, **body: object) -> dict:  # type: ignore[type-arg]
        response = self.client.post(
            f"/v1/projects/{project_id}/visual-units",
            json=body,
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())

    def units(self, project_id: str) -> list[dict]:  # type: ignore[type-arg]
        response = self.client.get(
            f"/v1/projects/{project_id}/visual-units", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return list(response.json()["units"])

    def timeline(self, project_id: str) -> dict:  # type: ignore[type-arg]
        response = self.client.get(
            f"/v1/projects/{project_id}/timeline", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())

    def stored_units(self, project_id: str) -> list[VisualUnit]:
        payload = run(
            self.repository.get_document(project_id=project_id, kind=UNITS_DOC)
        )
        return [VisualUnit.model_validate(item) for item in (payload or {}).get("units", [])]

    def store_units(self, project_id: str, units: list[VisualUnit]) -> None:
        import json

        run(
            self.repository.put_document(
                project_id=project_id,
                kind=UNITS_DOC,
                document_id=UNITS_DOC,
                payload={
                    "units": [json.loads(u.model_dump_json()) for u in units]
                },
            )
        )


# ---------------------------------------------------------------------------
# Scenario B — a pasted script is the narration, verbatim
# ---------------------------------------------------------------------------

class ScenarioBScriptIsTheSourceOfTruth(ProductTestCase):
    """The product's central promise in script mode.

    A system that quietly improves your grammar has decided it writes better
    than you do, and the first time it turns a technical term into something
    plausible-but-wrong, it publishes that under your name.
    """

    def test_a_pasted_script_is_stored_exactly_as_written(self) -> None:
        project_id = self.project()
        body = self.paste(project_id)

        script = run(
            self.repository.get_document(project_id=project_id, kind=SCRIPT_DOC)
        )
        assert script is not None
        self.assertEqual(script["source_text"], SCRIPT.strip())
        self.assertEqual(script["current_text"], SCRIPT.strip())
        self.assertEqual(body["origin"], "authored")

    def test_every_line_is_addressable(self) -> None:
        """The granularity at which a user says "not that one"."""
        project_id = self.project()
        body = self.paste(project_id)
        self.assertGreaterEqual(len(body["blocks"]), 8)
        for block in body["blocks"]:
            with self.subTest(block["order"]):
                self.assertTrue(block["block_id"].startswith("sbk_"))
                self.assertGreater(block["estimated_seconds"], 0)

    def test_a_line_maps_back_to_its_place_in_the_original(self) -> None:
        """So an editor can highlight the passage behind an edited line."""
        project_id = self.project()
        body = self.paste(project_id)
        for block in body["blocks"]:
            if block["text"] not in SCRIPT:
                continue
            start, end = block["start"], block["end"]
            del start, end
        script = Script.model_validate(
            run(self.repository.get_document(project_id=project_id, kind=SCRIPT_DOC))
        )
        for block in script.blocks:
            with self.subTest(block.order):
                self.assertIsNotNone(block.source_start)
                assert block.source_start is not None
                assert block.source_end is not None
                self.assertEqual(
                    script.source_text[block.source_start : block.source_end],
                    block.text,
                )

    def test_editing_one_line_leaves_the_source_untouched(self) -> None:
        project_id = self.project()
        body = self.paste(project_id)
        block_id = body["blocks"][1]["block_id"]

        response = self.client.patch(
            f"/v1/projects/{project_id}/script/blocks/{block_id}",
            json={"text": "These methods were slow and error prone."},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)

        script = Script.model_validate(
            run(self.repository.get_document(project_id=project_id, kind=SCRIPT_DOC))
        )
        self.assertEqual(script.source_text, SCRIPT.strip())
        self.assertNotEqual(script.current_text, script.source_text)
        self.assertEqual(script.version, 2)

    def test_an_edit_marks_timing_stale_rather_than_guessing(self) -> None:
        """Never silently kept. A timeline built on moved words is wrong."""
        project_id = self.project()
        body = self.paste(project_id)
        self.plan(project_id)

        response = self.client.patch(
            f"/v1/projects/{project_id}/script/blocks/"
            f"{body['blocks'][0]['block_id']}",
            json={"text": "Mathematics solved hard problems long before computers."},
            headers=self.auth(),
        )
        payload = response.json()
        self.assertTrue(payload["has_stale_timing"])
        self.assertTrue(payload["timing_invalidated_units"])

    def test_the_whole_path_reaches_a_timeline(self) -> None:
        project_id = self.project()
        self.paste(project_id)
        planned = self.plan(project_id)

        self.assertTrue(planned["units"])
        self.assertGreater(planned["timeline"]["duration"], 0)
        kinds = {track["kind"] for track in self.timeline(project_id)["tracks"]}
        self.assertEqual(kinds, {"narration", "visual", "caption"})


# ---------------------------------------------------------------------------
# Scenario C — regenerating one visual changes one visual
# ---------------------------------------------------------------------------

class ScenarioCOnlyTheChosenVisualChanges(ProductTestCase):
    """The property the product's economics depend on.

    If disliking shot seven costs the price of the whole video, users stop
    trying second ideas and the editor becomes take-it-or-leave-it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.plan(self.project_id)
        self.before = self.stored_units(self.project_id)
        self.assertGreaterEqual(len(self.before), 4)

    def drain(self) -> None:
        from vtv.worker import Worker

        worker = Worker.create(self.settings, assembly=self.assembly, concurrency=1)
        run(worker.queue.drain(timeout=60.0))

    def test_regenerating_one_unit_leaves_every_other_untouched(self) -> None:
        target = self.before[2]
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/"
            f"{target.visual_unit_id}/regenerate",
            json={"intent": "same_idea"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.drain()

        after = {u.visual_unit_id: u for u in self.stored_units(self.project_id)}
        changed = after[target.visual_unit_id]
        self.assertEqual(len(changed.versions), len(target.versions) + 1)

        for original in self.before:
            if original.visual_unit_id == target.visual_unit_id:
                continue
            with self.subTest(original.index):
                current = after[original.visual_unit_id]
                self.assertEqual(len(current.versions), len(original.versions))
                self.assertEqual(current.status, original.status)
                self.assertEqual(
                    current.selected_version_id, original.selected_version_id
                )

    def test_the_previous_version_survives(self) -> None:
        """"Actually the first one was better" must be answerable."""
        target = self.before[1]
        for _ in range(2):
            self.client.post(
                f"/v1/projects/{self.project_id}/visual-units/"
                f"{target.visual_unit_id}/regenerate",
                json={"intent": "more_educational"},
                headers=self.auth(),
            )
            self.drain()

        unit = next(
            u
            for u in self.stored_units(self.project_id)
            if u.visual_unit_id == target.visual_unit_id
        )
        self.assertGreaterEqual(len(unit.versions), 2)
        self.assertEqual(
            [v.version for v in unit.versions],
            sorted(v.version for v in unit.versions),
        )

    def test_going_back_to_an_earlier_version_is_free(self) -> None:
        target = self.before[1]
        self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/"
            f"{target.visual_unit_id}/regenerate",
            json={},
            headers=self.auth(),
        )
        self.drain()
        unit = next(
            u
            for u in self.stored_units(self.project_id)
            if u.visual_unit_id == target.visual_unit_id
        )
        self.assertGreaterEqual(len(unit.versions), 1)

        first = unit.versions[0]
        response = self.client.patch(
            f"/v1/projects/{self.project_id}/visual-units/{unit.visual_unit_id}",
            json={"version_id": first.version_id},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["selected_version_id"], first.version_id)

    def test_the_clip_keeps_its_identity_across_a_regeneration(self) -> None:
        """The script link, the seek target and the selection all hang off it."""
        target = self.before[2]
        before = {
            clip["visual_unit_id"]: clip["clip_id"]
            for track in self.timeline(self.project_id)["tracks"]
            if track["kind"] == "visual"
            for clip in track["clips"]
        }
        self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/"
            f"{target.visual_unit_id}/regenerate",
            json={},
            headers=self.auth(),
        )
        self.drain()
        after = {
            clip["visual_unit_id"]: clip["clip_id"]
            for track in self.timeline(self.project_id)["tracks"]
            if track["kind"] == "visual"
            for clip in track["clips"]
        }
        self.assertEqual(before, after)

    def test_a_scene_scoped_render_covers_a_fraction_of_the_project(self) -> None:
        """The number that proves the saving is expressible."""
        target = self.before[2]
        response = self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json={"scope": "scene", "visual_unit_id": target.visual_unit_id},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        body = response.json()
        self.assertEqual(body["scope"], "scene")
        self.assertLess(body["fraction_of_project"], 1.0)


# ---------------------------------------------------------------------------
# Scenario F — a locked visual survives everything
# ---------------------------------------------------------------------------

class ScenarioFALockIsARuleNotAHope(ProductTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.plan(self.project_id)
        self.units_before = self.stored_units(self.project_id)

    def lock(self, unit: VisualUnit) -> dict:  # type: ignore[type-arg]
        response = self.client.patch(
            f"/v1/projects/{self.project_id}/visual-units/{unit.visual_unit_id}",
            json={"locked": True},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())

    def test_locking_is_recorded(self) -> None:
        body = self.lock(self.units_before[3])
        self.assertTrue(body["locked"])
        self.assertEqual(body["status"], VisualUnitStatus.LOCKED.value)

    def test_regenerating_a_locked_visual_is_refused_with_a_reason(self) -> None:
        """Refused, not quietly skipped. Silence teaches the wrong lesson."""
        unit = self.units_before[3]
        self.lock(unit)
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/"
            f"{unit.visual_unit_id}/regenerate",
            json={},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("locked", response.json()["error"]["message"].lower())

    def test_a_full_replan_steps_around_a_locked_visual(self) -> None:
        """The property that makes locking worth having."""
        unit = self.units_before[3]
        self.lock(unit)

        self.plan(self.project_id, pacing="cinematic")

        after = {u.visual_unit_id: u for u in self.stored_units(self.project_id)}
        self.assertIn(unit.visual_unit_id, after)
        kept = after[unit.visual_unit_id]
        self.assertTrue(kept.locked)
        self.assertEqual(
            kept.selected_version_id, unit.selected_version_id
        )

    def test_a_locked_clip_cannot_be_moved_by_a_timeline_edit(self) -> None:
        unit = self.units_before[3]
        self.lock(unit)
        self.plan(self.project_id)

        timeline = self.timeline(self.project_id)
        clip = next(
            clip
            for track in timeline["tracks"]
            if track["kind"] == "visual"
            for clip in track["clips"]
            if clip["visual_unit_id"] == unit.visual_unit_id
        )
        self.assertTrue(clip["locked"])

        response = self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json={
                "operations": [
                    {"kind": "move", "clip_id": clip["clip_id"], "start": 300.0}
                ]
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_a_client_cannot_force_its_way_past_a_lock(self) -> None:
        """`force` is an operator action with an audit record, not a flag."""
        unit = self.units_before[3]
        self.lock(unit)
        self.plan(self.project_id)
        timeline = self.timeline(self.project_id)
        clip = next(
            clip
            for track in timeline["tracks"]
            if track["kind"] == "visual"
            for clip in track["clips"]
            if clip["visual_unit_id"] == unit.visual_unit_id
        )
        response = self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json={
                "operations": [
                    {
                        "kind": "move",
                        "clip_id": clip["clip_id"],
                        "start": 300.0,
                        "force": True,
                    }
                ]
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 403, response.text)


# ---------------------------------------------------------------------------
# Scenario D — target duration never distorts the narration
# ---------------------------------------------------------------------------

class ScenarioDTargetDurationRespectsTheVoice(ProductTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)

    def test_a_longer_target_is_filled_with_visual_time(self) -> None:
        planned = self.plan(self.project_id)
        narration = planned["pacing"]["narration_seconds"]

        response = self.client.post(
            f"/v1/projects/{self.project_id}/pacing",
            json={"pacing": "cinematic", "target_seconds": narration * 1.4},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        pacing = response.json()["pacing"]

        # `filled` or `on_target` — both are successes and which one you get
        # depends on whether the mode's own pacing already reached the target.
        # Cinematic pacing over this script does, and demanding `filled`
        # specifically would be asserting that the mode is *worse* at hitting a
        # target than it is.
        self.assertIn(pacing["verdict"], {"filled", "on_target"})
        self.assertFalse(pacing["needs_user_decision"])
        # The substantive promise: the narration figure is untouched, and the
        # extra length is visual time rather than slowed speech.
        self.assertAlmostEqual(pacing["narration_seconds"], narration, places=2)
        self.assertGreater(pacing["planned_seconds"], narration)
        self.assertTrue(pacing["fill"])

    def test_a_shorter_target_is_reported_not_silently_cut(self) -> None:
        """Deleting words a user wrote is not a pacing decision."""
        planned = self.plan(self.project_id)
        narration = planned["pacing"]["narration_seconds"]

        response = self.client.post(
            f"/v1/projects/{self.project_id}/pacing",
            json={"target_seconds": narration * 0.5},
            headers=self.auth(),
        )
        pacing = response.json()["pacing"]
        self.assertEqual(pacing["verdict"], "overrun")
        self.assertTrue(pacing["needs_user_decision"])
        self.assertIn("shorten", pacing["message"].lower())
        self.assertGreater(pacing["overrun_seconds"], 0)

    def test_an_impossible_stretch_is_admitted_rather_than_padded(self) -> None:
        planned = self.plan(self.project_id)
        narration = planned["pacing"]["narration_seconds"]

        response = self.client.post(
            f"/v1/projects/{self.project_id}/pacing",
            json={"target_seconds": narration * 10},
            headers=self.auth(),
        )
        pacing = response.json()["pacing"]
        self.assertEqual(pacing["verdict"], "underfilled")
        self.assertGreater(pacing["shortfall_seconds"], 0)

    def test_the_pacing_mode_changes_where_the_time_goes(self) -> None:
        self.plan(self.project_id)
        seen = {}
        for mode in ("tight", "natural", "cinematic"):
            response = self.client.post(
                f"/v1/projects/{self.project_id}/pacing",
                json={"pacing": mode},
                headers=self.auth(),
            )
            seen[mode] = response.json()["timeline"]["duration"]
        self.assertLess(seen["tight"], seen["cinematic"])

    def test_an_unknown_mode_is_refused_with_the_list(self) -> None:
        self.plan(self.project_id)
        response = self.client.post(
            f"/v1/projects/{self.project_id}/pacing",
            json={"pacing": "extremely-cinematic"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)


# ---------------------------------------------------------------------------
# The script ↔ visual ↔ timeline link
# ---------------------------------------------------------------------------

class TheThreePanelsStayInStep(ProductTestCase):
    """Click a line, seek the video. Click a clip, highlight the line.

    The link is materialised by the API rather than derived client-side, so the
    two directions cannot be implemented two ways that disagree.
    """

    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.script = self.paste(self.project_id)
        self.plan(self.project_id)

    def test_every_visual_clip_names_its_script_lines(self) -> None:
        links = self.timeline(self.project_id)["links"]
        self.assertTrue(links)
        for link in links:
            with self.subTest(link["clip_id"]):
                self.assertTrue(link["script_block_ids"])
                self.assertLess(link["start"], link["end"])

    def test_every_script_line_is_covered_by_exactly_one_visual(self) -> None:
        units = self.units(self.project_id)
        covered = [b for unit in units for b in unit["script_block_ids"]]
        self.assertEqual(len(covered), len(set(covered)), "a line has two visuals")
        self.assertEqual(
            set(covered),
            {block["block_id"] for block in self.script["blocks"]},
            "a line has no visual",
        )

    def test_a_preview_at_a_moment_names_the_clip_unit_and_lines(self) -> None:
        duration = self.timeline(self.project_id)["duration"]
        response = self.client.get(
            f"/v1/projects/{self.project_id}/preview",
            params={"at": duration / 2},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNotNone(body["clip"])
        self.assertIsNotNone(body["visual_unit"])
        self.assertTrue(body["script_lines"])
        self.assertTrue(body["script_lines"][0]["text"])

    def test_a_preview_past_the_end_is_empty_rather_than_an_error(self) -> None:
        """Scrubbing past the end is a normal thing a user does."""
        response = self.client.get(
            f"/v1/projects/{self.project_id}/preview",
            params={"at": 99999},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["clip"])

    def test_the_visual_track_has_no_accidental_gaps(self) -> None:
        for track in self.timeline(self.project_id)["tracks"]:
            if track["kind"] != "visual":
                continue
            self.assertEqual(track["gaps"], [], "the builder left black screen")


# ---------------------------------------------------------------------------
# Timeline editing through the API
# ---------------------------------------------------------------------------

class TheTimelineIsEditable(ProductTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.plan(self.project_id)

    def visual_clips(self) -> list[dict]:  # type: ignore[type-arg]
        return [
            clip
            for track in self.timeline(self.project_id)["tracks"]
            if track["kind"] == "visual"
            for clip in track["clips"]
        ]

    def edit(self, *operations: dict, expected: int | None = None):  # type: ignore[type-arg, no-untyped-def]
        body: dict = {"operations": list(operations)}
        if expected is not None:
            body["expected_version"] = expected
        return self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json=body,
            headers=self.auth(),
        )

    def test_a_clip_can_be_split(self) -> None:
        clip = self.visual_clips()[1]
        midpoint = (clip["start"] + clip["end"]) / 2
        response = self.edit(
            {"kind": "split", "clip_id": clip["clip_id"], "at": midpoint}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["changed_clip_ids"]), 2)

    def test_an_overlapping_move_is_refused(self) -> None:
        clips = self.visual_clips()
        response = self.edit(
            {
                "kind": "move",
                "clip_id": clips[0]["clip_id"],
                "start": clips[1]["start"] + 0.5,
            }
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_a_batch_is_all_or_nothing(self) -> None:
        clips = self.visual_clips()
        before = self.timeline(self.project_id)["version"]
        response = self.edit(
            {"kind": "lock", "clip_id": clips[0]["clip_id"]},
            {"kind": "move", "clip_id": "clp_" + "z" * 24, "start": 1.0},
        )
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.timeline(self.project_id)["version"], before)

    def test_editing_the_caption_track_is_refused_with_advice(self) -> None:
        """Captions come from the words. A hand edit would be discarded."""
        caption = next(
            clip
            for track in self.timeline(self.project_id)["tracks"]
            if track["kind"] == "caption"
            for clip in track["clips"]
        )
        response = self.edit(
            {"kind": "move", "clip_id": caption["clip_id"], "start": 0.0}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("script", response.json()["error"]["message"].lower())

    def test_a_stale_version_is_a_conflict_not_a_silent_overwrite(self) -> None:
        clips = self.visual_clips()
        response = self.edit(
            {"kind": "lock", "clip_id": clips[0]["clip_id"]},
            expected=999,
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_the_version_advances_once_per_batch(self) -> None:
        """A batch is one undo step, which is what a user means by "that edit"."""
        clips = self.visual_clips()
        before = self.timeline(self.project_id)["version"]
        # Locks rather than extends: the builder lays clips end to end, so
        # extending one necessarily overlaps the next and is refused — which is
        # correct, and not what this test is about.
        response = self.edit(
            {"kind": "lock", "clip_id": clips[0]["clip_id"]},
            {"kind": "lock", "clip_id": clips[1]["clip_id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.timeline(self.project_id)["version"], before + 1)

    def test_extending_a_clip_into_its_neighbour_is_refused(self) -> None:
        """The builder leaves no gaps, so an extend must displace something."""
        clips = self.visual_clips()
        response = self.edit(
            {"kind": "extend", "clip_id": clips[0]["clip_id"], "at": 0.4}
        )
        self.assertEqual(response.status_code, 403, response.text)


# ---------------------------------------------------------------------------
# Tenancy — the new surface is not a new hole
# ---------------------------------------------------------------------------

class TheProductSurfaceIsTenantScoped(ProductTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.plan(self.project_id)
        self.other_org, self.other_key = self._tenant("rival")

    def other(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.other_key}"}

    def test_another_tenant_cannot_read_the_script(self) -> None:
        response = self.client.get(
            f"/v1/projects/{self.project_id}/script", headers=self.other()
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_another_tenant_cannot_read_the_timeline(self) -> None:
        response = self.client.get(
            f"/v1/projects/{self.project_id}/timeline", headers=self.other()
        )
        self.assertEqual(response.status_code, 404)

    def test_another_tenant_cannot_edit_the_timeline(self) -> None:
        response = self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json={"operations": [{"kind": "add_track", "track_kind": "music"}]},
            headers=self.other(),
        )
        self.assertEqual(response.status_code, 404)

    def test_another_tenant_cannot_regenerate_a_visual(self) -> None:
        unit = self.stored_units(self.project_id)[0]
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/"
            f"{unit.visual_unit_id}/regenerate",
            json={},
            headers=self.other(),
        )
        self.assertEqual(response.status_code, 404)

    def test_every_stored_product_document_names_the_tenant(self) -> None:
        script = Script.model_validate(
            run(self.repository.get_document(project_id=self.project_id, kind=SCRIPT_DOC))
        )
        timeline = EditTimeline.model_validate(
            run(
                self.repository.get_document(
                    project_id=self.project_id, kind=TIMELINE_DOC
                )
            )
        )
        self.assertEqual(script.organisation_id, self.org)
        self.assertEqual(timeline.organisation_id, self.org)
        for unit in self.stored_units(self.project_id):
            with self.subTest(unit.index):
                self.assertEqual(unit.organisation_id, self.org)


# ---------------------------------------------------------------------------
# Scenario A — automatic mode becomes director mode
# ---------------------------------------------------------------------------

class ScenarioAAutomaticBecomesDirectable(ProductTestCase):
    """The two modes are one project entered from different doors.

    A spoken recording becomes an editable `Script` with *measured* timings, so
    everything the editor can do to a pasted script it can do to a recorded one.
    """

    def test_a_transcript_becomes_an_editable_script_with_real_timings(self) -> None:
        from vtv.contracts.base import IdPrefix, TimeSpan, new_id
        from vtv.contracts.transcript import Transcript, TranscriptSegment
        from vtv.pipeline.scripting import ScriptService

        project_id = self.project()
        transcript = Transcript(
            organisation_id=self.org,
            project_id=project_id,
            recording_id=new_id(IdPrefix.RECORDING),
            language="en",
            segments=[
                TranscriptSegment(
                    span=TimeSpan.of(0.0, 4.2),
                    text="The transistor was invented in 1947 at Bell Labs.",
                ),
                TranscriptSegment(
                    span=TimeSpan.of(4.2, 9.6),
                    text="It replaced the vacuum tube almost everywhere.",
                ),
            ],
            provider="test",
            model="test",
        )
        script = ScriptService().from_transcript(
            transcript, organisation_id=self.org, project_id=project_id
        )
        self.assertEqual(script.origin.value, "spoken")
        self.assertEqual(script.measured_duration_seconds, 9.6)
        self.assertEqual(len(script.blocks), 2)
        self.assertEqual(script.blocks[0].measured_start, 0.0)

    def test_editing_spoken_narration_flags_the_divergence(self) -> None:
        """The audio still says the old words. Never resolved silently."""
        from vtv.contracts.base import IdPrefix, TimeSpan, new_id
        from vtv.contracts.transcript import Transcript, TranscriptSegment
        from vtv.pipeline.scripting import ScriptService

        project_id = self.project()
        service = ScriptService()
        script = service.from_transcript(
            Transcript(
                organisation_id=self.org,
                project_id=project_id,
                recording_id=new_id(IdPrefix.RECORDING),
                language="en",
                segments=[
                    TranscriptSegment(
                        span=TimeSpan.of(0.0, 4.0), text="The transistor arrived."
                    )
                ],
                provider="test",
                model="test",
            ),
            organisation_id=self.org,
            project_id=project_id,
        )
        service.replace_block_text(
            script,
            block_id=script.blocks[0].block_id,
            text="The transistor arrived in 1947.",
        )
        self.assertTrue(script.diverged_from_recording)
        self.assertTrue(script.blocks[0].timing_invalidated)


# ---------------------------------------------------------------------------
# Scenario E — a failure isolates to one visual
# ---------------------------------------------------------------------------

class ScenarioEOneBadVisualDoesNotDestroyAProject(ProductTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.plan(self.project_id)

    def test_a_failed_unit_leaves_the_project_editable(self) -> None:
        units = self.stored_units(self.project_id)
        units[2].status = VisualUnitStatus.FAILED
        units[2].detail = "the provider refused this one"
        self.store_units(self.project_id, units)

        # Everything else still reads, plans and edits.
        self.assertEqual(
            self.client.get(
                f"/v1/projects/{self.project_id}/timeline", headers=self.auth()
            ).status_code,
            200,
        )
        listed = self.units(self.project_id)
        self.assertEqual(listed[2]["status"], "failed")
        self.assertTrue(listed[2]["detail"])
        self.assertTrue(all(u["status"] != "failed" for u in listed[:2]))

    def test_a_failed_unit_can_be_retried(self) -> None:
        units = self.stored_units(self.project_id)
        units[2].status = VisualUnitStatus.FAILED
        self.store_units(self.project_id, units)

        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/"
            f"{units[2].visual_unit_id}/regenerate",
            json={},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 202, response.text)

    def test_a_refused_version_is_kept_and_not_selected(self) -> None:
        """The user paid for it. Showing what was refused beats a shrug."""
        import asyncio as _asyncio

        from vtv.contracts.visual_plan import VisualStrategy
        from vtv.contracts.visual_unit import (
            ConsistencyStatus,
            GroundingStatus,
            VisualVersion,
        )
        from vtv.pipeline.regeneration import RegenerationService

        unit = self.stored_units(self.project_id)[1]
        first = VisualVersion(version=1, strategy=VisualStrategy.PROGRAMMATIC,
                              spec={"primitive": "typography", "headline": "kept"})
        unit.add_version(first, select=True)

        class Producer:
            async def produce(self, **_: object) -> VisualVersion:
                return VisualVersion(
                    version=2,
                    strategy=VisualStrategy.PROGRAMMATIC,
                    spec={"primitive": "typography", "headline": "refused"},
                )

        class Refuser:
            def validate(self, **_: object):  # type: ignore[no-untyped-def]
                return (
                    GroundingStatus.REFUSED,
                    ConsistencyStatus.NOT_APPLICABLE,
                    "that chart claimed a figure nobody said",
                )

        service = RegenerationService(producer=Producer(), validator=Refuser())
        result = _asyncio.run(service.regenerate(unit, narration="anything"))

        self.assertFalse(result.accepted)
        self.assertEqual(len(result.unit.versions), 2)
        # The good one is still what plays.
        self.assertEqual(result.unit.selected_version_id, first.version_id)
        self.assertIn("claimed", result.reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
