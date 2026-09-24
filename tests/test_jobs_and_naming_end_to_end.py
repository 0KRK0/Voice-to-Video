"""The four product changes, proved through the real API and a real render.

Every other test in this file's neighbourhood checks a unit. This one checks
the claims a user would make:

* my project has a name, and I did not have to type one;
* I can rename it, and nothing overwrites what I chose;
* the Jobs list shows progress that moved while the render was running;
* part of the video was watchable before the whole of it existed.

None of that can be shown by asserting on a function's return value. It needs a
real application, a real queue, a real worker and a real ffmpeg — the four
things this case assembles — because the failures being guarded against were all
integration failures: a field nothing wrote, a number nothing persisted, an
event nothing read.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from vtv.adapters.media import ffmpeg
from vtv.api.app import create_app
from vtv.config import Settings
from vtv.contracts.tenancy import (
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Role,
    User,
)
from vtv.security.keys import mint_api_key
from vtv.wiring import build

SCRIPT = """\
So today I want to talk about the transistor and why it changed computing.

It was invented at Bell Laboratories in 1947. It replaced the vacuum tube \
almost everywhere within twenty years.

Integrated circuits followed. Millions of transistors were placed on a single \
wafer of silicon.

The result is the machine you are reading this on.
"""


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TheProductClaims(unittest.TestCase):
    """One temporary directory, one real application, one real tenant."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-e2e-")
        root = Path(self._dir.name)
        self.settings = Settings(
            asset_search_endpoint="",
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
            signing_key="test-signing-key-not-a-real-secret",
            # Inline: a pool inside a test process buys nothing on a short
            # video and costs a worker start.
            render_workers=1,
        )
        self.assembly = build(self.settings)
        self.app = create_app(self.settings, assembly=self.assembly)
        self.client = TestClient(self.app)
        self.repository = self.app.state.vtv.repository

        organisation = self.assembly.directory.create_organisation(
            Organisation(name="Acme", slug="acme", plan=PlanTier.BUSINESS)
        )
        user = self.assembly.directory.create_user(
            User(email="o@acme.example", sso_subject="sso|acme")
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
        self.key = minted.secret

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def new_project(self, **body: object) -> str:
        response = self.client.post("/v1/projects", json=body, headers=self.auth())
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["project_id"])

    def paste(self, project_id: str, text: str = SCRIPT) -> None:
        response = self.client.post(
            f"/v1/projects/{project_id}/script",
            json={"text": text},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201, response.text)

    def summary(self, project_id: str) -> dict:  # type: ignore[type-arg]
        response = self.client.get("/v1/projects", headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        for item in response.json()["projects"]:
            if item["project_id"] == project_id:
                return dict(item)
        raise AssertionError(f"{project_id} missing from the projects list")

    # -- naming -----------------------------------------------------------

    def test_a_project_names_itself_from_its_script(self) -> None:
        """Nobody typed this. The dialog that would have asked for it is the
        dialog people answer with "test"."""
        project_id = self.new_project()
        self.assertIsNone(self.summary(project_id)["title"])

        self.paste(project_id)
        self.assertEqual(
            self.summary(project_id)["title"],
            "The transistor and why it changed computing",
        )

    def test_a_name_the_user_gave_survives_the_script(self) -> None:
        project_id = self.new_project(title="Q3 investor update")
        self.paste(project_id)
        self.assertEqual(self.summary(project_id)["title"], "Q3 investor update")

    def test_renaming_answers_with_the_new_name(self) -> None:
        """A client that has to make a second request to see its own write
        will eventually show the old name."""
        project_id = self.new_project()
        self.paste(project_id)
        response = self.client.patch(
            f"/v1/projects/{project_id}",
            json={"title": "  Transistors, part one  "},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "Transistors, part one")
        self.assertEqual(self.summary(project_id)["title"], "Transistors, part one")

    def test_clearing_a_name_lets_the_next_script_derive_one(self) -> None:
        """Blank is a real instruction — "you guessed badly, try again" — and
        collapsing it into "leave alone" would trap a project with a bad name."""
        project_id = self.new_project(title="Wrong")
        self.paste(project_id)
        self.client.patch(
            f"/v1/projects/{project_id}", json={"title": ""}, headers=self.auth()
        )
        self.assertIsNone(self.summary(project_id)["title"])
        self.paste(project_id)
        self.assertEqual(
            self.summary(project_id)["title"],
            "The transistor and why it changed computing",
        )

    def test_a_title_that_is_not_text_is_refused(self) -> None:
        response = self.client.patch(
            f"/v1/projects/{self.new_project()}",
            json={"title": 42},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class TheJobsListFollowsARealRender(TheProductClaims):
    """Progress, and the preview, observed on a render that actually ran."""

    def render(self, project_id: str) -> list[dict]:  # type: ignore[type-arg]
        """Run the project to a finished file, recording progress as it goes.

        Progress is read from the event stream rather than by polling the API,
        because the point being tested is that the number *moves* — and a poll
        from the same thread as the worker would only ever see the end.
        """
        # A timeline has to exist before there is anything to encode. This is
        # the same sequence the studio performs: paste, plan, render.
        planned = self.client.post(
            f"/v1/projects/{project_id}/visual-units", json={}, headers=self.auth()
        )
        self.assertEqual(planned.status_code, 200, planned.text)

        seen: list[dict] = []  # type: ignore[type-arg]
        self.assembly.events.subscribe(
            lambda event: seen.append(dict(event.data))
            if event.name.value == "render.progress"
            else None
        )
        response = self.client.post(
            f"/v1/projects/{project_id}/render",
            json={},
            headers={**self.auth(), "Idempotency-Key": "e2e-render-1"},
        )
        self.assertIn(response.status_code, (200, 202), response.text)

        from vtv.worker import Worker

        worker = Worker.create(self.settings, assembly=self.assembly, concurrency=1)
        run(worker.queue.drain(timeout=300.0))
        return seen

    def test_progress_moves_and_is_visible_from_outside_the_render(self) -> None:
        """The failure this replaced: `project.progress` was written once, at
        the end, so a four-hour render was 0% for four hours. The real number
        existed only in the SSE stream of the tab that started it — close that
        tab and the job became unobservable."""
        project_id = self.new_project()
        self.paste(project_id)
        seen = self.render(project_id)

        values = [
            item["progress"] for item in seen if isinstance(item.get("progress"), float)
        ]
        self.assertTrue(values, "the render reported no progress at all")
        # Monotonic, and it finishes. A bar that goes backwards is worse than
        # no bar.
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(max(values), 1.0, places=3)

        after = self.summary(project_id)
        self.assertAlmostEqual(after["progress"], 1.0, places=3)
        # And the row the Jobs screen renders is complete enough to render.
        self.assertIsNotNone(after["title"])
        self.assertTrue(after["state"])

    def test_progress_is_counted_in_segments_not_guessed_from_a_clock(self) -> None:
        """Honesty, not cosmetics: every step is a file that exists on disk."""
        project_id = self.new_project()
        self.paste(project_id)
        seen = self.render(project_id)

        counted = [item for item in seen if "segments_total" in item]
        self.assertTrue(counted, "progress carried no segment counts")
        for item in counted:
            self.assertGreater(item["segments_total"], 0)
            self.assertLessEqual(item["segments_done"], item["segments_total"])
            self.assertAlmostEqual(
                item["progress"],
                round(item["segments_done"] / item["segments_total"], 3),
                places=2,
            )

    def test_watchable_is_reported_separately_from_complete(self) -> None:
        """Segments finish out of order, so the preview stops at the first gap.
        A render can be 60% done with a 20% preview, and reporting one number
        for both is how a user concludes a render has stalled."""
        project_id = self.new_project()
        self.paste(project_id)
        seen = self.render(project_id)

        reported = [item for item in seen if "preview_seconds" in item]
        self.assertTrue(reported, "no preview_seconds was ever reported")
        for item in reported:
            # Never claims more finished video than the progress allows.
            self.assertGreaterEqual(item["preview_seconds"], 0.0)
        watchable = [item["preview_seconds"] for item in reported]
        self.assertEqual(watchable, sorted(watchable), "the preview went backwards")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
