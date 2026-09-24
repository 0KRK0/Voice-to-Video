"""P1-2 — every stored object is inside its tenant's namespace.

The audit's finding was precise and slightly embarrassing: `security/paths.py`
contained `tenant_key()`, a correct function for building a tenant-namespaced
storage key, and it had **zero callers**. Every writer in the system built its
own key with an f-string — `f"projects/{project_id}/renders/…"` — so objects
from every customer sat in one flat namespace.

That is not primarily a confidentiality bug. Project identifiers are 24
characters of random base32, so nothing was guessable. It is a bug about every
operation that needs to answer *"which of these bytes belong to this customer"*:

* deleting a tenant, completely, on request (GDPR Article 17)
* applying a per-plan retention rule
* scoping a lifecycle policy or a residency requirement
* answering an auditor asking where one customer's data is

None of those can be expressed over a flat namespace without consulting the
database for every object, and a deletion that depends on the database being
correct is a deletion that misses whatever the database has forgotten.

**The fix is a chokepoint, not a convention.** `LocalStorageProvider.put` and
`put_file` refuse any key that is not under `orgs/<organisation>/`. A writer that
forgets gets an exception at the moment of writing, rather than an object in the
wrong place and a discovery six months later.

For that refusal to be *usable*, the tenant has to be reachable at every write
site, so `organisation_id` is now a required field on every project-scoped
document — exactly as it became required on `Project` in P0-3, and for the same
reason: optional ownership is not ownership.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import IdPrefix, RetentionClass, new_id
from vtv.contracts.errors import PolicyViolation
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.retention import SweepPolicy
from vtv.security.paths import (
    is_tenant_key,
    project_prefix,
    require_tenant_key,
    tenant_key,
    tenant_prefix,
)

ORG_A = "org_" + "a" * 24
ORG_B = "org_" + "b" * 24


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TheKeyBuilder(unittest.TestCase):
    def test_a_key_is_rooted_in_its_tenants_namespace(self) -> None:
        key = tenant_key(ORG_A, "projects", "prj_1", "renders", "out.mp4")
        self.assertEqual(key, f"orgs/{ORG_A}/projects/prj_1/renders/out.mp4")
        self.assertTrue(key.startswith(tenant_prefix(ORG_A) + "/"))

    def test_a_component_cannot_smuggle_a_traversal(self) -> None:
        """The point of building rather than formatting."""
        for evil in ("..", "../..", "a/b", "", "."):
            with self.subTest(evil), self.assertRaises(PolicyViolation):
                tenant_key(ORG_A, "projects", evil)

    def test_one_tenant_cannot_be_a_prefix_of_another(self) -> None:
        """`orgs/acme` must not match `orgs/acme-corp`.

        Prefix matching without the trailing separator is how a sweep deletes
        the wrong customer's data.
        """
        self.assertFalse(is_tenant_key(f"orgs/{ORG_A}x/projects/p/a.png", ORG_A))
        self.assertTrue(is_tenant_key(f"orgs/{ORG_A}/projects/p/a.png", ORG_A))

    def test_the_namespace_itself_is_not_an_object(self) -> None:
        self.assertFalse(is_tenant_key(f"orgs/{ORG_A}"))
        self.assertTrue(is_tenant_key(f"orgs/{ORG_A}/x"))

    def test_a_key_outside_any_tenant_is_refused(self) -> None:
        for key in ("projects/p/a.png", "renders/x.mp4", "a.png", "org/x/y"):
            with self.subTest(key), self.assertRaises(PolicyViolation):
                require_tenant_key(key)

    def test_the_refusal_never_names_the_other_tenant(self) -> None:
        """An error message is a disclosure channel."""
        try:
            require_tenant_key(f"orgs/{ORG_B}/projects/p/a.png", ORG_A)
        except PolicyViolation as error:
            self.assertNotIn(ORG_B, str(error))
        else:  # pragma: no cover - the call above must raise
            self.fail("a cross-tenant key was accepted")


class TheStorageAdapterRefusesUnnamespacedWrites(unittest.TestCase):
    """The chokepoint. A caller that forgets cannot succeed."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-tenant-storage-")
        self.storage = LocalStorageProvider(Path(self._dir.name) / "storage")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_flat_key_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation):
            run(
                self.storage.put(
                    key="projects/prj_1/renders/out.mp4",
                    data=b"x",
                    content_type="video/mp4",
                )
            )

    def test_a_namespaced_key_is_accepted(self) -> None:
        ref = run(
            self.storage.put(
                key=tenant_key(ORG_A, "projects", "prj_1", "out.mp4"),
                data=b"x",
                content_type="video/mp4",
            )
        )
        self.assertTrue(ref.key.startswith(f"orgs/{ORG_A}/"))

    def test_put_file_is_guarded_too(self) -> None:
        """Renders go through `put_file`, not `put`.

        Guarding one and not the other would have left the largest and
        longest-lived objects in the system — the videos — unnamespaced.
        """
        source = Path(self._dir.name) / "video.mp4"
        source.write_bytes(b"x" * 64)
        with self.assertRaises(PolicyViolation):
            run(
                self.storage.put_file(
                    key="projects/prj_1/renders/out.mp4",
                    source=source,
                    content_type="video/mp4",
                )
            )
        ref = run(
            self.storage.put_file(
                key=tenant_key(ORG_A, "projects", "prj_1", "out.mp4"),
                source=source,
                content_type="video/mp4",
            )
        )
        self.assertEqual(ref.size_bytes, 64)

    def test_two_tenants_land_in_separate_subtrees(self) -> None:
        """What makes per-tenant deletion and retention expressible at all."""
        for org in (ORG_A, ORG_B):
            run(
                self.storage.put(
                    key=tenant_key(org, "projects", "prj_1", "out.png"),
                    data=b"x",
                    content_type="image/png",
                )
            )
        root = Path(self._dir.name) / "storage" / self.storage.bucket / "orgs"
        self.assertEqual(sorted(p.name for p in root.iterdir()), sorted([ORG_A, ORG_B]))

    def test_a_sweep_over_one_prefix_leaves_the_other_alone(self) -> None:
        """The operation the flat namespace made impossible to express."""
        for org in (ORG_A, ORG_B):
            run(
                self.storage.put(
                    key=tenant_key(org, "projects", "prj_1", "out.png"),
                    data=b"x",
                    content_type="image/png",
                )
            )
        run(
            self.storage.sweep_expired(
                policy=SweepPolicy(ephemeral_window_seconds=0.0),
                prefix=tenant_prefix(ORG_A),
            )
        )

        bucket = Path(self._dir.name) / "storage" / self.storage.bucket
        self.assertFalse(
            list((bucket / tenant_prefix(ORG_A)).rglob("*.png")),
            "the swept tenant still has objects",
        )
        self.assertTrue(
            list((bucket / tenant_prefix(ORG_B)).rglob("*.png")),
            "the sweep crossed into another tenant",
        )


class TheRetentionClassIsStoredWithTheObject(unittest.TestCase):
    """The gap the sweep fell through, closed at the write.

    Retention class lived only on the `ObjectRef` in the database, so a sweep
    walking storage had no way to tell a saved project's render from a stale
    recording — and deleted both. The adapter now keeps the class beside the
    bytes: locally a sidecar in a parallel tree, in S3 an object tag.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-tenant-storage-")
        self.root = Path(self._dir.name) / "storage"
        self.storage = LocalStorageProvider(self.root)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def key(self, name: str = "out.png") -> str:
        return tenant_key(ORG_A, "projects", "prj_1", name)

    def test_the_class_passed_at_write_time_can_be_read_back(self) -> None:
        key = self.key()
        run(
            self.storage.put(
                key=key,
                data=b"x",
                content_type="image/png",
                retention=RetentionClass.PROJECT,
            )
        )
        self.assertIs(run(self.storage.retention_of(key)), RetentionClass.PROJECT)

    def test_put_file_records_it_too(self) -> None:
        """Renders go through `put_file`. Guarding one and not the other would
        have left the largest, longest-lived objects unclassified."""
        source = Path(self._dir.name) / "video.mp4"
        source.write_bytes(b"x" * 8)
        key = self.key("out.mp4")
        run(
            self.storage.put_file(
                key=key,
                source=source,
                content_type="video/mp4",
                retention=RetentionClass.PROJECT,
            )
        )
        self.assertIs(run(self.storage.retention_of(key)), RetentionClass.PROJECT)

    def test_an_object_nobody_classified_reads_as_unknown_not_as_ephemeral(
        self,
    ) -> None:
        """`None` must mean "not recorded". A guess here is a deletion."""
        key = self.key("legacy.png")
        path = self.root / self.storage.bucket / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

        self.assertIsNone(run(self.storage.retention_of(key)))
        self.assertEqual(run(self.storage.unclassified()), [key])

    def test_damaged_metadata_reads_as_unknown_rather_than_optimistically(
        self,
    ) -> None:
        key = self.key()
        run(self.storage.put(key=key, data=b"x", content_type="image/png"))
        sidecar = self.root / ".vtv-meta" / self.storage.bucket / key
        sidecar.write_text("{not json", encoding="utf-8")

        self.assertIsNone(run(self.storage.retention_of(key)))

    def test_metadata_does_not_appear_in_the_object_namespace(self) -> None:
        """A sidecar inside the bucket would need filtering by every walker.

        `delete_prefix` counting sidecars as deleted objects, a media route
        serving one, a test counting artefacts — each would need to remember to
        skip them, and a rule everyone must remember is the defect shape this
        codebase keeps finding. So the metadata tree is a sibling of the bucket.
        """
        run(
            self.storage.put(
                key=self.key(),
                data=b"x",
                content_type="image/png",
                retention=RetentionClass.PROJECT,
            )
        )
        bucket = self.root / self.storage.bucket
        self.assertEqual(
            [path.name for path in bucket.rglob("*") if path.is_file()], ["out.png"]
        )

    def test_deleting_an_object_takes_its_metadata_with_it(self) -> None:
        key = self.key()
        run(
            self.storage.put(
                key=key,
                data=b"x",
                content_type="image/png",
                retention=RetentionClass.PROJECT,
            )
        )
        run(self.storage.delete_by_key(key))
        self.assertIsNone(run(self.storage.retention_of(key)))

    def test_deleting_a_prefix_takes_the_whole_metadata_subtree(self) -> None:
        """Erasure has to be complete in both trees to be erasure."""
        keys = [self.key("a.png"), self.key("b.png")]
        for key in keys:
            run(
                self.storage.put(
                    key=key,
                    data=b"x",
                    content_type="image/png",
                    retention=RetentionClass.PROJECT,
                )
            )
        run(self.storage.delete_prefix(project_prefix(ORG_A, "prj_1")))

        meta = self.root / ".vtv-meta" / self.storage.bucket
        self.assertEqual([path for path in meta.rglob("*") if path.is_file()], [])

    def test_a_sweep_cannot_be_asked_to_delete_by_age_alone(self) -> None:
        """The signature is the guarantee.

        The old sweep took `older_than_seconds` and nothing else, which is why
        it deleted live projects. There is no longer a way to express that call:
        the policy is required and there is no default.
        """
        with self.assertRaises(TypeError):
            run(self.storage.sweep_expired(prefix=tenant_prefix(ORG_A)))  # type: ignore[call-arg]


class EveryProjectScopedDocumentNamesItsTenant(unittest.TestCase):
    """Required, not optional — the P0-3 lesson applied to the rest.

    A nullable owner is the shape that let the API read "no owner" as "yours".
    These contracts are the ones that decide where bytes are written, so an
    optional tenant on any of them would put the same defect in storage.
    """

    def test_the_contracts_that_cause_writes_require_a_tenant(self) -> None:
        from vtv.contracts.generation import GenerationRequest
        from vtv.contracts.project import Project
        from vtv.contracts.recording import Recording
        from vtv.contracts.render import RenderJob
        from vtv.contracts.scene import SceneGraph
        from vtv.contracts.source import SourceDocument
        from vtv.contracts.timeline import Timeline
        from vtv.contracts.transcript import Transcript
        from vtv.contracts.visual_plan import VisualPlan

        for model in (
            GenerationRequest,
            Project,
            Recording,
            RenderJob,
            SceneGraph,
            SourceDocument,
            Timeline,
            Transcript,
            VisualPlan,
        ):
            with self.subTest(model.__name__):
                field = model.model_fields.get("organisation_id")
                self.assertIsNotNone(field, f"{model.__name__} has no tenant")
                assert field is not None
                self.assertTrue(
                    field.is_required(),
                    f"{model.__name__}.organisation_id is optional",
                )

    def test_an_identifier_shaped_value_is_still_required_to_be_safe(self) -> None:
        """A tenant id reaches a filesystem path, so it is validated as a key."""
        with self.assertRaises(PolicyViolation):
            tenant_key("../../etc", "projects", "p")


class NoWriterBuildsItsOwnKey(unittest.TestCase):
    """A source-level check, because this is exactly the rule that decays.

    Every guarantee the audit found broken was broken the same way: a helper
    existed and a caller did not use it. The only durable defence against that
    is a test that reads the source and fails when a new f-string key appears.
    """

    def test_no_module_formats_a_storage_key_by_hand(self) -> None:
        import re

        package = Path(__file__).resolve().parents[1] / "src" / "vtv"
        pattern = re.compile(r'key\s*=\s*f?"(?!orgs/)[a-z]+/')
        offenders: list[str] = []
        for path in package.rglob("*.py"):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(package)}:{number}")
        self.assertEqual(
            offenders,
            [],
            "these build a storage key by hand; use tenant_key() instead",
        )


class TheWorkedExampleIsNamespacedToo(unittest.TestCase):
    def test_the_example_project_writes_inside_a_tenant(self) -> None:
        from vtv.examples import transistor_project

        example = transistor_project()
        self.assertEqual(example.project.organisation_id, SYSTEM_ORGANISATION_ID)
        self.assertTrue(
            example.recording.audio.key.startswith(f"orgs/{SYSTEM_ORGANISATION_ID}/"),
            example.recording.audio.key,
        )

    def test_a_fresh_identifier_is_a_valid_namespace(self) -> None:
        """Identifiers minted at runtime must satisfy the key grammar.

        Organisations are minted with the project prefix today; what matters
        here is that whatever `new_id` produces is usable as a namespace
        without escaping or rewriting.
        """
        key = tenant_key(new_id(IdPrefix.PROJECT), "projects", "p", "a.png")
        self.assertTrue(is_tenant_key(key))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
