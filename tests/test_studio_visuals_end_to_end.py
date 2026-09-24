"""A pasted script gets real pictures, through the real API and the real job.

## The complaint this answers

"I put the image model endpoint and the key in `.env` and no image or video
render had happened."

The key was fine. Two stages were missing between it and the video.

`VisualUnitPlanner.plan` gives a unit a span and no version, because deciding
what a unit shows costs money and cannot happen inside an HTTP request. Nothing
ever filled that gap for a script-authored project, so `flatten` turned every
unit into a text clip and the whole video came out as title cards.

And `_DirectorProducer.produce` — the thing behind every item in the Regenerate
menu — returned typography unconditionally, so pressing "Generate an image" also
produced a title card, and said so in a rationale that blamed a missing
provider.

These tests drive the real endpoints and the real job handler with a stub image
provider registered on the real router. They stop short of the encode: what is
under test is which rung produced each visual, and a 1080p ffmpeg pass proves
nothing about that while taking minutes.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from vtv.adapters.testing import StubImageGenerationProvider
from vtv.api.app import create_app
from vtv.config import Settings
from vtv.contracts.generation import GenerationKind
from vtv.contracts.tenancy import (
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Role,
    User,
)
from vtv.contracts.tracks import EditTimeline
from vtv.contracts.visual_plan import VisualStrategy
from vtv.security.keys import mint_api_key
from vtv.wiring import build

SCRIPT = """Computer science studies how machines can be made to compute.

Your AI checks your schedule and books the meeting for you.

Businesses could have thousands of agents managing sales operations.

It may change what it means to use a computer.
"""


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class StudioCase(unittest.TestCase):
    """One real application, one real tenant, one pasted script."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-studio-")
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
        self.repository = self.app.state.vtv.repository

        self.image = StubImageGenerationProvider(storage=self.assembly.storage)
        self.assembly.router.register(self.image, GenerationKind.IMAGE)

        self.key = self._tenant()
        self.project_id = self._project()
        self._paste()
        self._plan()

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    # -- harness ----------------------------------------------------------

    def _tenant(self) -> str:
        organisation = self.assembly.directory.create_organisation(
            Organisation(name="Studio", slug="studio", plan=PlanTier.BUSINESS)
        )
        user = self.assembly.directory.create_user(
            User(email="o@studio.example", sso_subject="sso|studio")
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
        return minted.secret

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def _project(self) -> str:
        response = self.client.post(
            "/v1/projects", json={"title": "studio"}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["project_id"])

    def _paste(self) -> None:
        response = self.client.post(
            f"/v1/projects/{self.project_id}/script",
            json={"text": SCRIPT},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201, response.text)

    def _plan(self) -> None:
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units",
            json={},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def job_context(self):  # type: ignore[no-untyped-def]
        from vtv.jobs import JobContext

        scratch = Path(self._dir.name) / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        return JobContext(
            assembly=self.assembly,
            repository=self.repository,
            usage=self.assembly.usage,
            audit=self.assembly.audit,
            events=self.assembly.events,
            scratch=scratch,
        )

    def units(self):  # type: ignore[no-untyped-def]
        from vtv.product_jobs import _load_units

        return run(_load_units(self.job_context(), self.project_id))

    def project(self):  # type: ignore[no-untyped-def]
        return run(self.repository.get_project(project_id=self.project_id))

    def source_all(self):  # type: ignore[no-untyped-def]
        """The stage `run_render_scope` runs before it encodes anything."""
        from vtv.product_jobs import _load, _source_unclaimed_visuals

        context = self.job_context()
        timeline = run(_load(context, self.project_id, "edit_timeline", EditTimeline))
        return run(_source_unclaimed_visuals(context, self.project(), timeline))


class APlannedProjectHasNoPicturesUntilSomethingSourcesThem(StudioCase):
    def test_planning_alone_leaves_every_unit_without_a_version(self) -> None:
        """Not a defect — the reason the stage below has to exist."""
        units = self.units()
        self.assertTrue(units)
        self.assertTrue(all(unit.selected is None for unit in units))

    def test_the_render_job_sources_them_before_it_encodes(self) -> None:
        self.source_all()
        units = self.units()
        self.assertTrue(units)
        self.assertTrue(
            all(unit.selected is not None for unit in units),
            "every visual should have been given something to show",
        )

    def test_with_an_image_provider_the_pictures_are_generated(self) -> None:
        """The user's actual complaint: a key in `.env` and no image anywhere."""
        self.source_all()
        strategies = {unit.selected.strategy for unit in self.units()}
        self.assertIn(VisualStrategy.GENERATED_IMAGE, strategies)
        self.assertGreater(self.image.calls, 0)

    def test_the_timeline_points_at_the_pictures(self) -> None:
        """A version nothing renders is not a picture in the video."""
        timeline = self.source_all()
        from vtv.contracts.tracks import ClipSourceKind, TrackKind

        track = timeline.track_of_kind(TrackKind.VISUAL)
        assert track is not None
        objects = [
            clip
            for clip in track.clips
            if clip.source_kind is ClipSourceKind.OBJECT and clip.object is not None
        ]
        self.assertTrue(objects, "no clip on the visual track points at an image")

    def test_sourcing_twice_does_not_pay_twice(self) -> None:
        self.source_all()
        first = self.image.calls
        self.source_all()
        self.assertEqual(self.image.calls, first)


class WhatTheUserOwnsIsLeftAlone(StudioCase):
    def test_a_locked_unit_is_not_given_a_new_picture(self) -> None:
        units = self.units()
        target = units[0]
        response = self.client.patch(
            f"/v1/projects/{self.project_id}/visual-units/{target.visual_unit_id}",
            json={"locked": True},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)

        self.source_all()
        after = {u.visual_unit_id: u for u in self.units()}[target.visual_unit_id]
        self.assertTrue(after.locked)
        self.assertIsNone(after.selected)


class TheRegenerateMenuReachesTheProvider(StudioCase):
    def regenerate(self, unit_id: str, intent: str) -> None:
        from vtv.product_jobs import run_regenerate_visual

        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/{unit_id}/regenerate",
            json={"intent": intent},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 202, response.text)
        run(
            run_regenerate_visual(
                self.job_context(),
                {
                    "organisation_id": self.project().organisation_id,
                    "project_id": self.project_id,
                    "visual_unit_id": unit_id,
                    "intent": intent,
                },
            )
        )

    def test_generate_an_image_actually_generates_an_image(self) -> None:
        unit = self.units()[1]
        self.regenerate(unit.visual_unit_id, "use_generated_image")

        after = {u.visual_unit_id: u for u in self.units()}[unit.visual_unit_id]
        self.assertIsNotNone(after.selected)
        self.assertEqual(after.selected.strategy, VisualStrategy.GENERATED_IMAGE)
        self.assertIsNotNone(after.selected.object)
        self.assertEqual(self.image.calls, 1)

    def test_use_typography_does_not_call_a_generator(self) -> None:
        unit = self.units()[1]
        self.regenerate(unit.visual_unit_id, "use_typography")

        after = {u.visual_unit_id: u for u in self.units()}[unit.visual_unit_id]
        self.assertEqual(after.selected.strategy, VisualStrategy.PROGRAMMATIC)
        self.assertEqual(self.image.calls, 0)

    def test_the_rationale_no_longer_blames_a_missing_provider(self) -> None:
        """The old stub said this with a provider configured. It was not true."""
        unit = self.units()[1]
        self.regenerate(unit.visual_unit_id, "use_generated_image")

        after = {u.visual_unit_id: u for u in self.units()}[unit.visual_unit_id]
        self.assertNotIn(
            "no image or video provider is configured", after.selected.rationale
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class OneModelCallForTheWholeScript(StudioCase):
    """Thirteen shots used to mean thirteen model calls asking the same thing.

    Each re-sent the same instruction, so most of the tokens paid for were the
    instruction rather than the script — and each was a round trip and a chance
    to be rate-limited. A real render's log showed thirteen
    `POST /v1/chat/completions` in a row, one per visual.

    Batching also gives the model the surrounding lines, which is the
    difference between "It may change what it means to use a computer" being
    unsearchable on its own and being obvious in context.
    """

    def setUp(self) -> None:
        super().setUp()
        from vtv.adapters.testing import ScriptedTextGenerationProvider

        # One response carrying an entry per line, which is the shape the batch
        # reader asks for.
        self.text = ScriptedTextGenerationProvider(
            responses=[
                {
                    "visuals": [
                        {
                            "index": index,
                            "subject": f"subject {index}",
                            "search_queries": [f"query {index}"],
                            "image_prompt": f"prompt {index}",
                            "motion": "still",
                            "rationale": f"because {index}",
                        }
                        for index in range(8)
                    ]
                }
            ]
        )
        self.assembly.router.register(self.text, GenerationKind.TEXT)

    def test_the_model_is_called_once_not_once_per_visual(self) -> None:
        units = len(self.units())
        self.assertGreater(units, 1, "the fixture needs several visuals to prove this")
        self.source_all()
        self.assertEqual(self.text.calls, 1, f"{units} visuals, {self.text.calls} calls")

    def test_each_visual_still_gets_its_own_reading(self) -> None:
        """One call, not one answer shared by everybody."""
        self.source_all()
        rationales = {
            unit.selected.rationale for unit in self.units() if unit.selected
        }
        self.assertGreater(len(rationales), 1)

    def test_a_malformed_batch_falls_back_to_rules_rather_than_failing(self) -> None:
        self.text.responses = [{"visuals": "not a list"}]
        self.source_all()
        self.assertTrue(all(unit.selected is not None for unit in self.units()))

    def test_a_partial_batch_fills_the_gaps_with_rules(self) -> None:
        """A model that answered for two lines has not answered for the rest."""
        self.text.responses = [
            {
                "visuals": [
                    {
                        "index": 0,
                        "subject": "only the first",
                        "search_queries": ["a query"],
                        "image_prompt": "a prompt",
                        "motion": "still",
                        "rationale": "answered",
                    }
                ]
            }
        ]
        self.source_all()
        self.assertTrue(all(unit.selected is not None for unit in self.units()))


class TheUserChoosesWhatAPictureCosts(StudioCase):
    """Budget and quality, from the studio's controls to the vendor's body.

    Two settings that used to live only in a `.env` file the user cannot see.
    Each has a fifteen-to-sixteen-fold effect on the bill for one video, so
    "which tier did that render use" had to stop being answerable only by
    reading a server's environment.
    """

    def spend(self, **body: object) -> dict[str, object]:
        response = self.client.patch(
            f"/v1/projects/{self.project_id}", json=body, headers=self.auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())

    def detail(self) -> dict[str, object]:
        response = self.client.get(
            f"/v1/projects/{self.project_id}", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())

    def test_what_was_set_is_what_comes_back(self) -> None:
        """The field was write-only before this: `PATCH` accepted a budget and
        `GET` never returned one, so the studio's box was empty after every
        reload — indistinguishable from the setting having been dropped."""
        self.spend(budget_usd=2.5, visual_fidelity="standard")
        detail = self.detail()
        self.assertEqual(detail["budget_usd"], 2.5)
        self.assertEqual(detail["visual_fidelity"], "standard")

    def test_the_prices_come_back_so_the_studio_can_say_what_it_buys(self) -> None:
        """The studio answers "how many pictures is $2" while the field is still
        being typed into. It must use the server's prices, not its own, or it
        is a second opinion about the user's money."""
        prices = self.detail()["price_usd"]
        assert isinstance(prices, dict)
        self.assertEqual(set(prices), {"draft", "standard", "fine"})

    def test_an_unknown_tier_is_refused_rather_than_quietly_ignored(self) -> None:
        """Falling through to the cheapest would look exactly like the feature
        not working, and the user could not tell which had happened."""
        response = self.client.patch(
            f"/v1/projects/{self.project_id}",
            json={"visual_fidelity": "ultra"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_clearing_a_tier_returns_to_the_deployment_default(self) -> None:
        self.spend(visual_fidelity="fine")
        self.assertEqual(self.detail()["visual_fidelity"], "fine")
        self.spend(visual_fidelity=None)
        self.assertIsNone(self.detail()["visual_fidelity"])

    def test_the_chosen_tier_reaches_the_provider(self) -> None:
        """The whole point. A setting the render does not honour is worse than
        no setting: the user believes they chose and the invoice disagrees.

        Asserted on the params the provider was actually handed, because every
        link between the studio control and that object — project field, job,
        sourcing service, composer, request — is a place it could be dropped.
        """
        self.spend(visual_fidelity="fine")
        self.source_all()
        self.assertTrue(self.image.seen, "no image request was made at all")
        for request in self.image.seen:
            self.assertEqual(request.params.fidelity.value, "fine")

    def test_a_budget_too_small_for_one_picture_buys_none(self) -> None:
        """And says so in the plan, rather than generating and overrunning.

        The stub declares a cent an image through its capabilities and nothing
        else, which is the case that used to be priced at zero — and a zero
        price permits every shot however small the budget.
        """
        from vtv.product_jobs import BUDGET_DOC

        self.spend(budget_usd=0.0, visual_fidelity="fine")
        self.source_all()
        plan = run(
            self.repository.get_document(project_id=self.project_id, kind=BUDGET_DOC)
        )
        self.assertIsNotNone(plan, "the render recorded no budget plan")
        assert plan is not None
        self.assertEqual(plan["permitted"], 0)
        self.assertEqual(plan["projected_usd"], 0.0)
        self.assertEqual(self.image.calls, 0, "it generated despite the plan")


class WhenEveryVisualFailsTheSameWay(StudioCase):
    """One failing is a title card. Thirteen failing identically is a defect.

    The per-shot handler is deliberately broad — a render must survive one bad
    photograph — and that breadth hides programming errors perfectly. A call
    site that forgets an argument raises `TypeError` on every shot, is swallowed
    thirteen times, and the render reports success with no pictures in it.

    This exact thing happened: `AssetResolver.resolve` was called without
    `organisation_id` for months, so the commons rung raised on every shot of
    every render and the product simply never used the commons.
    """

    def test_it_is_reported_once_loudly_not_thirteen_times_quietly(self) -> None:
        import vtv.product_jobs as jobs

        seen: list[dict] = []
        original_emit = self.assembly.events.emit

        def capture(name, **kw):  # type: ignore[no-untyped-def]
            data = kw.get("data") or {}
            if isinstance(data, dict) and "alert" in data:
                seen.append(data)
            return original_emit(name, **kw)

        self.assembly.events.emit = capture  # type: ignore[method-assign]
        original = jobs.source_one_visual

        async def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise TypeError("source_one_visual() got an unexpected keyword argument")

        jobs.source_one_visual = broken
        try:
            self.source_all()
        finally:
            jobs.source_one_visual = original
            self.assembly.events.emit = original_emit  # type: ignore[method-assign]

        self.assertEqual(len(seen), 1, "the all-failed alert should fire exactly once")
        self.assertEqual(seen[0]["sourced"], 0)
        self.assertIn("TypeError", seen[0]["codes"])

    def test_one_failure_among_many_is_not_an_alert(self) -> None:
        """Otherwise the alert fires on every render with one bad photograph
        and stops meaning anything."""
        import vtv.product_jobs as jobs

        seen: list[dict] = []
        original_emit = self.assembly.events.emit

        def capture(name, **kw):  # type: ignore[no-untyped-def]
            data = kw.get("data") or {}
            if isinstance(data, dict) and "alert" in data:
                seen.append(data)
            return original_emit(name, **kw)

        self.assembly.events.emit = capture  # type: ignore[method-assign]
        original = jobs.source_one_visual
        calls = {"n": 0}

        async def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("one bad photograph")
            return await original(*args, **kwargs)

        jobs.source_one_visual = flaky
        try:
            self.source_all()
        finally:
            jobs.source_one_visual = original
            self.assembly.events.emit = original_emit  # type: ignore[method-assign]

        self.assertEqual(seen, [])
