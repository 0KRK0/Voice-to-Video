"""P1-7 — deletion removes the bytes, and retention is a plan, not a constant.

Two audit findings, tested together because they are the same operation:

* **`delete_project` removed the rows and left the files.** A customer told
  their project was gone still had it — the video, the narration audio, the
  fetched assets. That is a compliance failure before it is a storage bill,
  because GDPR Article 17 is about the data and not the index.
* **`Plan.max_retention_days` was never read.** It existed on all five plans,
  documented as the thing that stops storage cost growing without bound, and no
  code consulted it. A free trial and an enterprise contract expired on one
  global `temporary_retention_hours`.

* **The orphan sweep deleted live projects' bytes.** `sweep_expired` was an
  `mtime` comparison and an `unlink`, called with a tenant prefix and a 24-hour
  window, and it took everything: `PROJECT` assets, captions, the renders of
  saved projects on a plan promising ten years. It could not have done better —
  the retention class lived on the `ObjectRef` in the database and the sweep
  walked a filesystem. The test below asserting `len(swept) == 4` *encoded* that
  bug; it now asserts that a live project's assets survive.

The interesting tests here are the ones about interruption. Deletion cannot be
atomic across two stores, so the design chooses *which* half-finished state to
leave behind, and these assert that choice rather than pretending it does not
exist.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.repository.sqlite import SqliteProjectRepository
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import RetentionClass, utc_now
from vtv.contracts.errors import PolicyViolation, VTVError
from vtv.contracts.project import PersistenceMode, Project
from vtv.contracts.tenancy import Organisation, PlanTier
from vtv.retention import (
    RetentionService,
    SweepPolicy,
    expiry_for,
    project_of_key,
    retention_for,
)
from vtv.security.directory import Directory
from vtv.security.paths import project_prefix, tenant_key

ORG_A = "org_" + "a" * 24
ORG_B = "org_" + "b" * 24

DAY = 24 * 3600.0

#: What a real render leaves behind, with the class each object is written
#: with in the pipeline: the raw recording is `EPHEMERAL` (voice is intimate,
#: `docs/STORAGE_POLICY.md`), everything a saved project needs to exist is
#: `PROJECT`.
ARTEFACTS: tuple[tuple[tuple[str, str], RetentionClass], ...] = (
    (("recordings", "a.webm"), RetentionClass.EPHEMERAL),
    (("narration", "b.wav"), RetentionClass.PROJECT),
    (("assets", "c.jpg"), RetentionClass.PROJECT),
    (("renders", "d.mp4"), RetentionClass.PROJECT),
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class RetentionTestCase(unittest.TestCase):
    #: The plan both fixture tenants are on unless a test says otherwise.
    #: Enterprise, because that is the case the old sweep got most wrong: a
    #: ten-year retention ceiling and a 24-hour deletion.
    tier = PlanTier.ENTERPRISE

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-retention-")
        root = Path(self._dir.name)
        self.storage = LocalStorageProvider(root / "storage")
        self.repository = SqliteProjectRepository(root / "vtv.db")
        self.directory = Directory(root / "directory.db")
        for index, organisation_id in enumerate((ORG_A, ORG_B)):
            self.directory.create_organisation(
                Organisation(
                    organisation_id=organisation_id,
                    name=f"tenant {index}",
                    slug=f"tenant-{index}",
                    plan=self.tier,
                )
            )
        self.service = RetentionService(
            repository=self.repository,
            storage=self.storage,
            directory=self.directory,
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def project(
        self,
        organisation_id: str = ORG_A,
        *,
        expires_in_hours: float | None = None,
    ) -> Project:
        project = Project(
            organisation_id=organisation_id,
            title="a project",
            persistence=PersistenceMode.TEMPORARY,
        )
        if expires_in_hours is not None:
            project.expires_at = utc_now() + timedelta(hours=expires_in_hours)
        run(self.repository.save_project(project))
        return project

    def artefacts(self, project: Project, count: int = 4) -> list[str]:
        """The spread of objects a real render leaves behind, each with its class."""
        keys: list[str] = []
        for parts, retention in ARTEFACTS[:count]:
            key = tenant_key(
                project.organisation_id, "projects", project.project_id, *parts
            )
            run(
                self.storage.put(
                    key=key,
                    data=b"x" * 32,
                    content_type="video/mp4",
                    retention=retention,
                )
            )
            keys.append(key)
        return keys

    def age(self, key: str, seconds: float) -> None:
        """Backdate an object, so age-dependent rules can be tested in a second."""
        path = self.storage.root / self.storage.bucket / key
        stamp = path.stat().st_mtime - seconds
        os.utime(path, (stamp, stamp))

    def files_under(self, organisation_id: str, project_id: str) -> list[Path]:
        bucket = self.storage.root / self.storage.bucket
        root = bucket / project_prefix(organisation_id, project_id)
        return [path for path in root.rglob("*") if path.is_file()]


class DeletionRemovesTheBytes(RetentionTestCase):
    def test_every_artefact_goes_not_just_the_row(self) -> None:
        """The finding, stated as a test."""
        project = self.project()
        self.artefacts(project)
        self.assertEqual(len(self.files_under(ORG_A, project.project_id)), 4)

        report = run(
            self.service.delete_project(
                organisation_id=ORG_A, project_id=project.project_id
            )
        )

        self.assertEqual(report.objects_deleted, 4)
        self.assertEqual(report.records_deleted, 1)
        self.assertEqual(self.files_under(ORG_A, project.project_id), [])
        self.assertIsNone(
            run(
                self.repository.get_project(
                    project.project_id, organisation_id=ORG_A
                )
            )
        )

    def test_the_directory_itself_is_removed(self) -> None:
        """Directory names are data: they say which projects existed."""
        project = self.project()
        self.artefacts(project)
        run(
            self.service.delete_project(
                organisation_id=ORG_A, project_id=project.project_id
            )
        )
        bucket = self.storage.root / self.storage.bucket
        self.assertFalse(
            (bucket / project_prefix(ORG_A, project.project_id)).exists()
        )

    def test_another_tenants_project_is_untouched(self) -> None:
        mine = self.project(ORG_A)
        theirs = self.project(ORG_B)
        self.artefacts(mine)
        self.artefacts(theirs)

        run(
            self.service.delete_project(
                organisation_id=ORG_A, project_id=mine.project_id
            )
        )

        self.assertEqual(len(self.files_under(ORG_B, theirs.project_id)), 4)
        self.assertIsNotNone(
            run(
                self.repository.get_project(
                    theirs.project_id, organisation_id=ORG_B
                )
            )
        )

    def test_deleting_with_the_wrong_tenant_removes_nothing(self) -> None:
        """The scope is in the operation, not in a check before it."""
        project = self.project(ORG_A)
        self.artefacts(project)

        report = run(
            self.service.delete_project(
                organisation_id=ORG_B, project_id=project.project_id
            )
        )

        self.assertEqual(report.records_deleted, 0)
        self.assertTrue(report.already_absent)
        self.assertEqual(len(self.files_under(ORG_A, project.project_id)), 4)

    def test_deleting_twice_is_not_an_error(self) -> None:
        """A retried job and a user double-clicking land in the same place."""
        project = self.project()
        self.artefacts(project)
        first = run(
            self.service.delete_project(
                organisation_id=ORG_A, project_id=project.project_id
            )
        )
        second = run(
            self.service.delete_project(
                organisation_id=ORG_A, project_id=project.project_id
            )
        )
        self.assertTrue(first.is_complete)
        self.assertTrue(second.is_complete)
        self.assertTrue(second.already_absent)
        self.assertEqual(second.objects_deleted, 0)

    def test_a_project_with_no_artefacts_deletes_cleanly(self) -> None:
        project = self.project()
        report = run(
            self.service.delete_project(
                organisation_id=ORG_A, project_id=project.project_id
            )
        )
        self.assertEqual(report.objects_deleted, 0)
        self.assertEqual(report.records_deleted, 1)


class AnInterruptedDeletionIsRecoverable(RetentionTestCase):
    """Deletion spans two stores, so it can be interrupted. Which half first?

    Database first leaves objects nothing knows about: invisible, unbilled,
    removable only by a full-bucket scan. That is a permanent leak.

    Storage first leaves a row pointing at nothing: visibly broken, found again
    by the next sweep, and completable. Strictly better, so that is the order.
    """

    def test_orphaned_objects_are_reclaimed_by_a_later_pass(self) -> None:
        """The state a crash *after* the storage half leaves."""
        project = self.project()
        self.artefacts(project)
        run(self.repository.delete_project(project.project_id, organisation_id=ORG_A))

        report = run(
            self.service.delete_project(
                organisation_id=ORG_A, project_id=project.project_id
            )
        )
        self.assertTrue(report.already_absent)
        self.assertEqual(report.objects_deleted, 4)
        self.assertEqual(self.files_under(ORG_A, project.project_id), [])

    def test_the_orphan_sweep_respects_age(self) -> None:
        """A freshly written object belongs to a render still in flight.

        The second half of this test used to assert `len(swept) == 4`: that
        sweeping a **live** project's namespace with a zero-second window
        removed every artefact it had. That assertion encoded the defect — the
        sweep read no retention class and never asked whether the project
        existed — so a green suite meant "the sweep deletes saved projects" was
        working as designed. It now asserts the opposite, which is what the
        product promises: only the `EPHEMERAL` recording is past its window; the
        narration, the asset and the render belong to a project that still
        exists and stay until the project does not.
        """
        project = self.project()
        self.artefacts(project)

        kept = run(
            self.service.sweep_orphans(
                organisation_id=ORG_A, older_than_seconds=3600.0
            )
        )
        self.assertEqual(kept, [])
        self.assertEqual(len(self.files_under(ORG_A, project.project_id)), 4)

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=0.0)
        )
        self.assertEqual(
            [key.rsplit("/", 2)[-2] for key in swept],
            ["recordings"],
            "the sweep took something other than the ephemeral recording",
        )
        self.assertEqual(len(self.files_under(ORG_A, project.project_id)), 3)

    def test_the_orphan_sweep_stays_inside_one_tenant(self) -> None:
        mine = self.project(ORG_A)
        theirs = self.project(ORG_B)
        self.artefacts(mine)
        self.artefacts(theirs)

        run(self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=0.0))

        self.assertEqual(len(self.files_under(ORG_B, theirs.project_id)), 4)


class TheOrphanSweepKeepsBytesAProjectStillNeeds(RetentionTestCase):
    """The defect, stated from every side.

    `sweep_orphans` walked a filesystem with an age and deleted everything older
    than it. The retention class was on the `ObjectRef` in the database and the
    project row was never consulted, so a saved enterprise project lost its
    assets after a day. These assert the four cases the old code collapsed into
    one: live and classed `PROJECT` (keep), live and `EPHEMERAL` (go), orphaned
    (go), and unknowable (keep).
    """

    def test_a_live_projects_assets_survive_a_sweep_much_older_than_they_are(
        self,
    ) -> None:
        project = self.project()
        keys = self.artefacts(project)
        for key in keys:
            self.age(key, 30 * DAY)

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertEqual(swept, [keys[0]], "only the raw recording may go")
        self.assertEqual(len(self.files_under(ORG_A, project.project_id)), 3)

    def test_the_very_same_objects_go_once_the_project_row_is_gone(self) -> None:
        """Age was never the whole question — ownership was the other half."""
        project = self.project()
        keys = self.artefacts(project)
        for key in keys:
            self.age(key, 30 * DAY)
        run(self.repository.delete_project(project.project_id, organisation_id=ORG_A))

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertEqual(sorted(swept), sorted(keys))
        self.assertEqual(self.files_under(ORG_A, project.project_id), [])

    def test_an_object_with_no_recorded_class_is_never_swept(self) -> None:
        """Fail closed: unknown must deny the deletion, not permit it.

        Objects written before classes were recorded, or by any path that
        bypassed `put`, cannot be told apart from a saved render. Guessing
        "ephemeral" would delete them.
        """
        project = self.project()
        key = tenant_key(ORG_A, "projects", project.project_id, "legacy", "old.mp4")
        path = self.storage.root / self.storage.bucket / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 32)
        self.age(key, 400 * DAY)
        run(self.repository.delete_project(project.project_id, organisation_id=ORG_A))

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertEqual(swept, [])
        self.assertTrue(path.exists())

    def test_the_refusal_to_sweep_it_comes_with_somewhere_to_look(self) -> None:
        """A refusal with no next step would mean invisible objects forever."""
        project = self.project()
        key = tenant_key(ORG_A, "projects", project.project_id, "legacy", "old.mp4")
        path = self.storage.root / self.storage.bucket / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 32)
        self.artefacts(project)

        self.assertEqual(
            run(self.service.unclassified_objects(organisation_id=ORG_A)), [key]
        )

    def test_an_archive_object_outlives_the_project_it_came_from(self) -> None:
        """Licence receipts have a legal reason to exist past the project."""
        project = self.project()
        key = tenant_key(ORG_A, "projects", project.project_id, "receipts", "l.json")
        run(
            self.storage.put(
                key=key,
                data=b"{}",
                content_type="application/json",
                retention=RetentionClass.ARCHIVE,
            )
        )
        self.age(key, 400 * DAY)
        run(self.repository.delete_project(project.project_id, organisation_id=ORG_A))

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertEqual(swept, [])
        self.assertTrue((self.storage.root / self.storage.bucket / key).exists())

    def test_a_partial_view_of_live_projects_deletes_no_orphans(self) -> None:
        """A truncated page of projects makes live projects look like orphans.

        `list_projects` is paginated. If the sweep trusted a full page it would
        delete the bytes of every project that fell off the end, which is the
        same defect one layer up.
        """
        self.service.live_project_limit = 1
        first = self.project()
        second = self.project()
        keys = self.artefacts(first) + self.artefacts(second)
        for key in keys:
            self.age(key, 30 * DAY)
        run(self.repository.delete_project(second.project_id, organisation_id=ORG_A))

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertNotIn(
            f"orgs/{ORG_A}/projects/{second.project_id}/renders/d.mp4",
            swept,
            "an orphan was deleted on the strength of a truncated project list",
        )
        self.assertEqual(len(self.files_under(ORG_A, second.project_id)), 3)

    def test_storage_that_cannot_report_a_class_is_refused_not_swept_blindly(
        self,
    ) -> None:
        """The old behaviour is not a fallback. It is the bug."""

        class BlindStorage:
            async def sweep_expired(self, **kwargs: object) -> list[str]:
                return []

        service = RetentionService(
            repository=self.repository,
            storage=BlindStorage(),
            directory=self.directory,
        )
        with self.assertRaises(VTVError) as caught:
            run(
                service.sweep_orphans(
                    organisation_id=ORG_A, older_than_seconds=DAY
                )
            )
        self.assertIn("SweepableStorage", str(caught.exception))
        self.assertIn("lifecycle rule", str(caught.exception))


class ThePlanCeilingGovernsBytesNotJustRows(RetentionTestCase):
    """`Plan.max_retention_days` was read by nothing at all.

    Setting `expires_at` from the plan governs *rows*. These assert the other
    half: bytes older than the plan permits go, and bytes younger than it stay,
    on the same objects with the same age and only the plan changed.
    """

    tier = PlanTier.FREE

    def test_bytes_past_the_free_ceiling_go_even_though_the_project_lives(
        self,
    ) -> None:
        project = self.project()
        keys = self.artefacts(project)
        for key in keys:
            self.age(key, 8 * DAY)  # the free plan allows seven

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertEqual(sorted(swept), sorted(keys))
        self.assertEqual(self.files_under(ORG_A, project.project_id), [])

    def test_the_same_bytes_on_an_enterprise_plan_stay(self) -> None:
        """One field on the organisation, opposite outcome. That is the point."""
        organisation = self.directory.organisation(ORG_A)
        assert organisation is not None
        organisation.plan = PlanTier.ENTERPRISE
        self.directory.save_organisation(organisation)

        project = self.project()
        keys = self.artefacts(project)
        for key in keys:
            self.age(key, 8 * DAY)

        swept = run(
            self.service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertEqual(swept, [keys[0]], "only the raw recording may go")
        self.assertEqual(len(self.files_under(ORG_A, project.project_id)), 3)

    def test_with_no_directory_the_ceiling_is_not_applied_at_all(self) -> None:
        """Absent plan information must not authorise a deletion.

        Falling back to "most restrictive plan" reads as the safe default and is
        the opposite: it would delete an enterprise tenant's ten-year data after
        seven days the first time somebody forgot to wire the directory in.
        """
        service = RetentionService(
            repository=self.repository, storage=self.storage
        )
        project = self.project()
        keys = self.artefacts(project)
        for key in keys:
            self.age(key, 400 * DAY)

        swept = run(
            service.sweep_orphans(organisation_id=ORG_A, older_than_seconds=DAY)
        )

        self.assertEqual(swept, [keys[0]])
        self.assertEqual(len(self.files_under(ORG_A, project.project_id)), 3)


class TheSweepPolicyIsTheOnlyPlaceThatDecides(unittest.TestCase):
    """The rule on an object, testable without a filesystem or a database."""

    def policy(self, **overrides: object) -> SweepPolicy:
        fields: dict[str, object] = {
            "ephemeral_window_seconds": DAY,
            "plan_ceiling_seconds": 7 * DAY,
            "live_project_ids": frozenset({"prj_live"}),
        }
        fields.update(overrides)
        return SweepPolicy(**fields)  # type: ignore[arg-type]

    def decide(self, project_id: str, age: float, retention, **overrides):  # type: ignore[no-untyped-def]
        key = f"orgs/{ORG_A}/projects/{project_id}/assets/c.jpg"
        return self.policy(**overrides).decide(
            key=key, age_seconds=age, retention=retention
        )

    def test_a_live_projects_asset_is_kept_at_any_age_under_the_ceiling(self) -> None:
        self.assertFalse(
            self.decide("prj_live", 6 * DAY, RetentionClass.PROJECT).delete
        )

    def test_an_ephemeral_object_goes_after_its_window(self) -> None:
        self.assertTrue(
            self.decide("prj_live", 2 * DAY, RetentionClass.EPHEMERAL).delete
        )

    def test_an_ephemeral_object_inside_its_window_stays(self) -> None:
        self.assertFalse(
            self.decide("prj_live", 0.5 * DAY, RetentionClass.EPHEMERAL).delete
        )

    def test_a_dead_projects_asset_is_an_orphan(self) -> None:
        self.assertTrue(
            self.decide("prj_gone", 2 * DAY, RetentionClass.PROJECT).delete
        )

    def test_an_unknown_class_is_kept_and_says_why(self) -> None:
        decision = self.decide("prj_gone", 400 * DAY, None)
        self.assertFalse(decision.delete)
        self.assertIn("unclassified", decision.reason)

    def test_an_unknown_set_of_live_projects_deletes_no_orphans(self) -> None:
        self.assertFalse(
            self.decide(
                "prj_gone", 2 * DAY, RetentionClass.PROJECT, live_project_ids=None
            ).delete
        )

    def test_an_unknown_plan_ceiling_deletes_nothing_by_ceiling(self) -> None:
        self.assertFalse(
            self.decide(
                "prj_live",
                4000 * DAY,
                RetentionClass.PROJECT,
                plan_ceiling_seconds=None,
            ).delete
        )

    def test_the_ceiling_beats_a_live_project(self) -> None:
        self.assertTrue(
            self.decide("prj_live", 8 * DAY, RetentionClass.PROJECT).delete
        )

    def test_archive_beats_the_ceiling(self) -> None:
        self.assertFalse(
            self.decide("prj_gone", 4000 * DAY, RetentionClass.ARCHIVE).delete
        )

    def test_an_object_outside_any_project_is_not_an_orphan(self) -> None:
        """No project in the key means no project row to be missing."""
        decision = self.policy().decide(
            key=f"orgs/{ORG_A}/exports/report.csv",
            age_seconds=2 * DAY,
            retention=RetentionClass.PROJECT,
        )
        self.assertFalse(decision.delete)

    def test_the_owner_is_read_out_of_the_key(self) -> None:
        self.assertEqual(
            project_of_key(f"orgs/{ORG_A}/projects/prj_x/renders/d.mp4"), "prj_x"
        )
        for key in (f"orgs/{ORG_A}/exports/a.csv", f"orgs/{ORG_A}", "projects/p/a"):
            with self.subTest(key):
                self.assertIsNone(project_of_key(key))


class TheSweepIsTenantScoped(RetentionTestCase):
    def test_only_expired_projects_go(self) -> None:
        expired = self.project(expires_in_hours=-1)
        alive = self.project(expires_in_hours=+24)
        self.artefacts(expired)
        self.artefacts(alive)

        reports = run(self.service.sweep(organisation_id=ORG_A))

        self.assertEqual([r.project_id for r in reports], [expired.project_id])
        self.assertEqual(self.files_under(ORG_A, expired.project_id), [])
        self.assertEqual(len(self.files_under(ORG_A, alive.project_id)), 4)

    def test_another_tenants_expired_project_is_not_swept(self) -> None:
        """The `/internal/sweep` defect, at the layer below the route."""
        mine = self.project(ORG_A, expires_in_hours=-1)
        theirs = self.project(ORG_B, expires_in_hours=-1)
        self.artefacts(mine)
        self.artefacts(theirs)

        run(self.service.sweep(organisation_id=ORG_A))

        self.assertEqual(len(self.files_under(ORG_B, theirs.project_id)), 4)

    def test_there_is_no_cross_tenant_sweep_to_call(self) -> None:
        """Not "discouraged" — absent.

        A cross-tenant sweep exists only as a loop in the worker over tenants
        the worker enumerates itself. Making it a parameter is how an endpoint
        acquires the ability to delete every customer's data.
        """
        import inspect

        signature = inspect.signature(RetentionService.sweep)
        parameter = signature.parameters["organisation_id"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        self.assertEqual(parameter.annotation, "str")


class RetentionComesFromThePlan(unittest.TestCase):
    def test_each_plan_has_its_own_ceiling(self) -> None:
        free = retention_for(PlanTier.FREE)
        enterprise = retention_for(PlanTier.ENTERPRISE)
        self.assertLess(free, enterprise)
        self.assertEqual(free, timedelta(days=7))

    def test_an_unknown_plan_gets_the_most_restrictive_ceiling(self) -> None:
        """Guessing generously would mean an unrecognised tier keeps data
        longest, which is exactly backwards."""
        self.assertEqual(retention_for("not-a-plan"), retention_for(PlanTier.FREE))
        self.assertEqual(retention_for(None), retention_for(PlanTier.FREE))

    def test_a_saved_project_still_hits_the_plan_ceiling(self) -> None:
        """"Saved" meant "never expires", which is unbounded storage cost."""
        now = utc_now()
        project = Project(
            organisation_id=ORG_A, persistence=PersistenceMode.SAVED
        )
        expiry = expiry_for(
            project, tier=PlanTier.FREE, temporary_hours=24, now=now
        )
        self.assertIsNotNone(expiry)
        assert expiry is not None
        self.assertEqual(expiry, now + timedelta(days=7))

    def test_a_temporary_project_expires_sooner_than_the_ceiling(self) -> None:
        now = utc_now()
        project = Project(
            organisation_id=ORG_A, persistence=PersistenceMode.TEMPORARY
        )
        expiry = expiry_for(
            project, tier=PlanTier.ENTERPRISE, temporary_hours=24, now=now
        )
        self.assertEqual(expiry, now + timedelta(hours=24))

    def test_the_plan_ceiling_wins_when_it_is_shorter(self) -> None:
        """A free tenant cannot buy a longer window by asking for one."""
        now = utc_now()
        project = Project(
            organisation_id=ORG_A, persistence=PersistenceMode.TEMPORARY
        )
        expiry = expiry_for(
            project, tier=PlanTier.FREE, temporary_hours=24 * 365, now=now
        )
        self.assertEqual(expiry, now + timedelta(days=7))


class PrefixesAreContained(RetentionTestCase):
    def test_a_prefix_outside_a_tenant_namespace_is_refused(self) -> None:
        for prefix in ("projects/p", "", "..", "orgs"):
            with self.subTest(prefix), self.assertRaises(PolicyViolation):
                run(self.storage.delete_prefix(prefix))

    def test_a_traversal_in_a_tenant_id_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation):
            run(self.storage.delete_prefix("orgs/../../etc"))

    def test_a_missing_prefix_returns_nothing_rather_than_raising(self) -> None:
        self.assertEqual(
            run(self.storage.delete_prefix(project_prefix(ORG_A, "prj_absent"))), []
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
