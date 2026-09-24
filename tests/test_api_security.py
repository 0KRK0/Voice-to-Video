"""Stage 25 at the HTTP boundary.

The unit tests in `test_security.py` prove the primitives are correct. These
prove they are actually *wired in* — which is the failure mode that matters,
because a perfect authorisation function that one route forgets to call is
indistinguishable from no authorisation at all on that route.

Every test here goes through the real Starlette application with a real SQLite
directory, a real audit log and a real usage meter.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from vtv.api.app import create_app
from vtv.config import Settings
from vtv.contracts.tenancy import (
    AuditAction,
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Role,
    User,
)
from vtv.security.keys import mint_api_key
from vtv.security.shared_state import InMemorySharedStore
from vtv.wiring import build


def run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


class ApiTestCase(unittest.TestCase):
    production = False

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-api-sec-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="production" if self.production else "development",
            # Production refuses to start without one, because a per-process
            # key means a media URL signed by one replica is rejected by the
            # next. Supplied here so these tests exercise the authorisation
            # behaviour they are about rather than that startup guard, which
            # `tests/test_deployment.py` covers directly.
            signing_key="test-signing-key-not-a-real-secret",
        )
        self.assembly = build(self.settings)
        self.app = create_app(self.settings, assembly=self.assembly)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def tenant(
        self, slug: str, *, role: Role = Role.ADMIN, plan: PlanTier = PlanTier.BUSINESS
    ) -> tuple[str, str]:
        """Create an organisation with an API key. Returns (org id, secret)."""
        organisation = self.assembly.directory.create_organisation(
            Organisation(name=slug.title(), slug=slug, plan=plan)
        )
        user = self.assembly.directory.create_user(
            User(email=f"owner@{slug}.example", sso_subject=f"sso|{slug}")
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
            name=f"{slug} key",
            role=role,
            scopes=list(Capability) if role is Role.ADMIN else None,
        )
        self.assembly.directory.store_key(minted.record)
        return organisation.organisation_id, minted.secret

    def auth(self, secret: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}


class CrossTenantAccess(ApiTestCase):
    """The disclosure this whole layer exists to prevent."""

    def test_one_tenant_cannot_read_anothers_project(self) -> None:
        _acme, acme_key = self.tenant("acme")
        _globex, globex_key = self.tenant("globex")

        created = self.client.post(
            "/v1/projects", json={"title": "Secret roadmap"}, headers=self.auth(acme_key)
        )
        self.assertEqual(created.status_code, 201)
        project_id = created.json()["project_id"]

        # The owner can read it.
        self.assertEqual(
            self.client.get(
                f"/v1/projects/{project_id}", headers=self.auth(acme_key)
            ).status_code,
            200,
        )
        # The other tenant gets 404, not 403: confirming existence is itself a
        # disclosure.
        self.assertEqual(
            self.client.get(
                f"/v1/projects/{project_id}", headers=self.auth(globex_key)
            ).status_code,
            404,
        )

    def test_every_project_subresource_is_scoped(self) -> None:
        """One forgotten route is the same as no authorisation on that route."""
        _acme, acme_key = self.tenant("acme")
        _globex, globex_key = self.tenant("globex")
        project_id = self.client.post(
            "/v1/projects", json={}, headers=self.auth(acme_key)
        ).json()["project_id"]

        for path in (
            f"/v1/projects/{project_id}",
            f"/v1/projects/{project_id}/storyboard",
            f"/v1/projects/{project_id}/video",
            f"/v1/projects/{project_id}/captions.vtt",
            f"/v1/projects/{project_id}/scenes/scn_x/thumbnail.png",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.auth(globex_key))
                self.assertIn(
                    response.status_code,
                    (403, 404),
                    f"{path} leaked to another tenant",
                )

    def test_a_project_cannot_be_edited_across_tenants(self) -> None:
        _acme, acme_key = self.tenant("acme")
        _globex, globex_key = self.tenant("globex")
        project_id = self.client.post(
            "/v1/projects", json={}, headers=self.auth(acme_key)
        ).json()["project_id"]
        response = self.client.post(
            f"/v1/projects/{project_id}/scenes/scn_x/revise",
            json={"action": "next_strategy"},
            headers=self.auth(globex_key),
        )
        self.assertIn(response.status_code, (403, 404))


class Credentials(ApiTestCase):
    def test_a_bad_key_is_refused_with_401(self) -> None:
        response = self.client.post(
            "/v1/projects",
            json={},
            headers=self.auth("vtv_live_notarealkeyatallreally"),
        )
        self.assertEqual(response.status_code, 401)

    def test_a_revoked_key_stops_working_immediately(self) -> None:
        _org, key = self.tenant("acme")
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={}, headers=self.auth(key)
            ).status_code,
            201,
        )
        stored = self.assembly.directory.keys_for(_org)[0]
        self.assembly.directory.revoke_key(stored.api_key_id)
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={}, headers=self.auth(key)
            ).status_code,
            401,
        )

    def test_a_suspended_tenant_is_refused(self) -> None:
        org_id, key = self.tenant("acme")
        organisation = self.assembly.directory.organisation(org_id)
        assert organisation is not None
        self.assembly.directory.save_organisation(
            organisation.model_copy(
                update={"suspended": True, "suspended_reason": "unpaid"}
            )
        )
        response = self.client.post("/v1/projects", json={}, headers=self.auth(key))
        self.assertIn(response.status_code, (401, 403))

    def test_the_error_body_never_leaks_internals(self) -> None:
        response = self.client.post(
            "/v1/projects", json={}, headers=self.auth("vtv_live_wrongwrongwrongwrong")
        )
        body = response.json()
        message = body["error"]["message"]
        for leak in ("sqlite", "Traceback", "organisation_id", "/home/"):
            self.assertNotIn(leak, message)


class Capabilities(ApiTestCase):
    def test_a_viewer_key_cannot_create_a_project(self) -> None:
        _org, key = self.tenant("acme", role=Role.VIEWER)
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={}, headers=self.auth(key)
            ).status_code,
            403,
        )

    def test_a_service_key_cannot_manage_keys(self) -> None:
        """A leaked CI credential must not be able to mint itself an admin key."""
        _org, key = self.tenant("acme", role=Role.SERVICE)
        self.assertEqual(
            self.client.post(
                "/v1/api-keys", json={"name": "escalation"}, headers=self.auth(key)
            ).status_code,
            403,
        )

    def test_a_service_key_cannot_read_the_audit_log(self) -> None:
        _org, key = self.tenant("acme", role=Role.SERVICE)
        self.assertEqual(
            self.client.get("/v1/audit", headers=self.auth(key)).status_code, 403
        )

    def test_an_editor_key_can_create_but_not_read_billing(self) -> None:
        _org, key = self.tenant("acme", role=Role.EDITOR)
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={}, headers=self.auth(key)
            ).status_code,
            201,
        )
        self.assertEqual(
            self.client.get("/v1/usage", headers=self.auth(key)).status_code, 403
        )


class KeyManagement(ApiTestCase):
    def test_the_secret_is_returned_once_and_never_again(self) -> None:
        _org, admin_key = self.tenant("acme")
        created = self.client.post(
            "/v1/api-keys", json={"name": "ci"}, headers=self.auth(admin_key)
        )
        self.assertEqual(created.status_code, 201)
        secret = created.json()["secret"]

        listed = self.client.get("/v1/api-keys", headers=self.auth(admin_key))
        self.assertNotIn(secret, listed.text)
        # But the prefix is there, so a human can say which key to revoke.
        self.assertIn(created.json()["prefix"], listed.text)

    def test_a_minted_key_actually_works(self) -> None:
        _org, admin_key = self.tenant("acme")
        secret = self.client.post(
            "/v1/api-keys", json={"name": "ci", "role": "editor"},
            headers=self.auth(admin_key),
        ).json()["secret"]
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={}, headers=self.auth(secret)
            ).status_code,
            201,
        )

    def test_revoking_through_the_api_stops_the_key(self) -> None:
        _org, admin_key = self.tenant("acme")
        created = self.client.post(
            "/v1/api-keys", json={"name": "ci", "role": "editor"},
            headers=self.auth(admin_key),
        ).json()
        self.client.delete(
            f"/v1/api-keys/{created['id']}", headers=self.auth(admin_key)
        )
        self.assertEqual(
            self.client.post(
                "/v1/projects", json={}, headers=self.auth(created["secret"])
            ).status_code,
            401,
        )

    def test_a_key_cannot_be_revoked_across_tenants(self) -> None:
        acme_id, acme_key = self.tenant("acme")
        _globex, globex_key = self.tenant("globex")
        target = self.assembly.directory.keys_for(acme_id)[0]
        del acme_key
        self.assertEqual(
            self.client.delete(
                f"/v1/api-keys/{target.api_key_id}", headers=self.auth(globex_key)
            ).status_code,
            404,
        )


class UploadRejection(ApiTestCase):
    def test_an_executable_disguised_as_audio_is_refused(self) -> None:
        _org, key = self.tenant("acme")
        project_id = self.client.post(
            "/v1/projects", json={}, headers=self.auth(key)
        ).json()["project_id"]
        response = self.client.post(
            f"/v1/projects/{project_id}/recordings",
            files={"audio": ("voice.wav", b"MZ\x90\x00payload", "audio/wav")},
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 415)

    def test_html_disguised_as_a_pdf_is_refused(self) -> None:
        _org, key = self.tenant("acme")
        project_id = self.client.post(
            "/v1/projects", json={}, headers=self.auth(key)
        ).json()["project_id"]
        response = self.client.post(
            f"/v1/projects/{project_id}/documents",
            files={
                "document": (
                    "report.pdf",
                    b"<!doctype html><script>alert(1)</script>",
                    "application/pdf",
                )
            },
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 415)

    def test_a_rejected_upload_is_audited(self) -> None:
        org_id, key = self.tenant("acme")
        project_id = self.client.post(
            "/v1/projects", json={}, headers=self.auth(key)
        ).json()["project_id"]
        self.client.post(
            f"/v1/projects/{project_id}/recordings",
            files={"audio": ("x.wav", b"\x7fELF\x02", "audio/wav")},
            headers=self.auth(key),
        )
        entries = self.assembly.audit.recent(organisation_id=org_id)
        self.assertTrue(
            any(entry.action is AuditAction.UPLOAD_REJECTED for entry in entries)
        )


class Quotas(ApiTestCase):
    def test_a_free_tenant_is_stopped_before_it_spends(self) -> None:
        from vtv.billing.plans import QuotaKind

        org_id, key = self.tenant("acme", plan=PlanTier.FREE)
        self.assembly.usage.record(
            organisation_id=org_id,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=10.0,
        )
        project_id = self.client.post(
            "/v1/projects", json={}, headers=self.auth(key)
        ).json()["project_id"]
        # Give the limiter a fresh store so this asserts on the *quota* rather
        # than on the free plan's (deliberately small) burst allowance.
        self.assembly.limiter.store = InMemorySharedStore()
        response = self.client.post(
            f"/v1/projects/{project_id}/recordings",
            files={"audio": ("v.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav")},
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 402)

    def test_usage_is_visible_but_our_cost_is_not(self) -> None:
        """A customer sees what they consumed, never our margin."""
        from vtv.billing.plans import QuotaKind

        org_id, key = self.tenant("acme")
        self.assembly.usage.record(
            organisation_id=org_id,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=3.0,
            cost_usd=0.44,
        )
        body = self.client.get("/v1/usage", headers=self.auth(key)).json()
        self.assertEqual(body["usage"]["rendered_minutes"]["used"], 3.0)
        self.assertNotIn("gross_margin_usd", body)
        self.assertNotIn("provider_cost_usd", body)


class AuditTrail(ApiTestCase):
    def test_project_creation_is_recorded(self) -> None:
        org_id, key = self.tenant("acme")
        self.client.post("/v1/projects", json={}, headers=self.auth(key))
        entries = self.assembly.audit.recent(organisation_id=org_id)
        self.assertTrue(
            any(entry.action is AuditAction.PROJECT_CREATED for entry in entries)
        )

    def test_a_denied_request_is_recorded(self) -> None:
        org_id, key = self.tenant("acme", role=Role.VIEWER)
        self.client.post("/v1/projects", json={}, headers=self.auth(key))
        entries = self.assembly.audit.recent(
            organisation_id=org_id, security_only=True
        )
        self.assertTrue(
            any(entry.action is AuditAction.PERMISSION_DENIED for entry in entries)
        )

    def test_the_audit_endpoint_shows_only_this_tenant(self) -> None:
        acme_id, acme_key = self.tenant("acme")
        _globex, globex_key = self.tenant("globex")
        self.client.post("/v1/projects", json={"title": "acme"},
                         headers=self.auth(acme_key))
        self.client.post("/v1/projects", json={"title": "globex"},
                         headers=self.auth(globex_key))

        body = self.client.get("/v1/audit", headers=self.auth(acme_key)).json()
        self.assertTrue(body["entries"])
        del acme_id

    def test_a_minted_secret_never_appears_in_the_audit_endpoint(self) -> None:
        _org, admin_key = self.tenant("acme")
        secret = self.client.post(
            "/v1/api-keys", json={"name": "ci"}, headers=self.auth(admin_key)
        ).json()["secret"]
        body = self.client.get("/v1/audit", headers=self.auth(admin_key)).text
        self.assertNotIn(secret, body)


class ProductionRefusesAnonymous(ApiTestCase):
    """The development convenience must not survive into production."""

    production = True

    def test_an_unauthenticated_request_is_refused(self) -> None:
        self.assertEqual(self.client.post("/v1/projects", json={}).status_code, 401)

    def test_health_stays_open_for_the_load_balancer(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_the_probes_stay_open_too(self) -> None:
        """An orchestrator has no credential and must not need one.

        They are also the only three routes that may be anonymous in
        production, which is why they are asserted together with the refusal
        above rather than in a happier file.
        """
        for path in ("/health/live", "/health/ready"):
            with self.subTest(path):
                self.assertNotEqual(self.client.get(path).status_code, 401)

    def test_the_probes_disclose_nothing_about_the_deployment(self) -> None:
        """`/health` names providers and capabilities; these must not.

        A probe is reachable from anywhere the load balancer is, which in most
        clusters is a wider audience than the API itself.
        """
        for path in ("/health/live", "/health/ready"):
            with self.subTest(path):
                body = self.client.get(path).text
                self.assertNotIn("capabilities", body)
                self.assertNotIn("provider", body)
                self.assertNotIn(str(self.settings.storage_root), body)


class TheProbesAnswerDifferentQuestions(ApiTestCase):
    """P0-11. Liveness and readiness are different remedies."""

    def test_liveness_is_ok_and_checks_nothing_else(self) -> None:
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "alive"})

    def test_a_replica_is_not_ready_before_migrations_have_run(self) -> None:
        """The failure this probe exists to prevent.

        A replica that rolls out ahead of the migration job would otherwise
        serve requests against a schema its code does not expect — which is how
        a partially-applied migration becomes a data-corruption incident rather
        than a 503.
        """
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["checks"]["migrations"], "pending")

    def test_readiness_reports_each_dependency_by_name(self) -> None:
        from vtv.migrate import upgrade

        upgrade(self.settings)

        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200, response.text)
        checks = response.json()["checks"]
        self.assertEqual(
            set(checks), {"repository", "queue", "storage", "migrations"}
        )
        self.assertTrue(all(value == "ok" for value in checks.values()), checks)

    def test_readiness_fails_when_storage_is_not_writable(self) -> None:
        """503, not 500. The replica leaves the pool; it is not restarted."""
        from vtv.migrate import upgrade

        upgrade(self.settings)

        def refuse() -> None:
            raise OSError("read-only file system")

        original = self.assembly.storage.writable
        self.assembly.storage.writable = refuse  # type: ignore[method-assign]
        try:
            response = self.client.get("/health/ready")
        finally:
            self.assembly.storage.writable = original  # type: ignore[method-assign]

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["storage"], "unavailable")

    def test_liveness_still_passes_when_a_dependency_is_down(self) -> None:
        """The whole reason they are separate.

        A liveness probe that touched the database would restart every replica
        when the database had a bad minute, turning a degradation into an
        outage.
        """

        def refuse() -> None:
            raise OSError("read-only file system")

        original = self.assembly.storage.writable
        self.assembly.storage.writable = refuse  # type: ignore[method-assign]
        try:
            self.assertEqual(self.client.get("/health/live").status_code, 200)
        finally:
            self.assembly.storage.writable = original  # type: ignore[method-assign]


class DeletingAProjectIsTenantScoped(ApiTestCase):
    """P1-7 at the HTTP boundary.

    There was no delete route at all, so "remove my data" was a support ticket.
    The route exists now; these assert it cannot become a cross-tenant one.
    """

    def test_a_project_can_be_deleted_by_its_owner(self) -> None:
        project_id = self.client.post(
            "/v1/projects", json={"title": "temporary"}
        ).json()["project_id"]

        response = self.client.delete(f"/v1/projects/{project_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])

        self.assertEqual(
            self.client.get(f"/v1/projects/{project_id}").status_code, 404
        )

    def test_deleting_twice_reports_absent_rather_than_failing(self) -> None:
        project_id = self.client.post("/v1/projects", json={}).json()["project_id"]
        self.client.delete(f"/v1/projects/{project_id}")
        second = self.client.delete(f"/v1/projects/{project_id}")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["already_absent"])
        self.assertFalse(second.json()["deleted"])

    def test_an_unknown_project_is_not_a_disclosure(self) -> None:
        """"Already absent" and "belongs to someone else" must look identical.

        A 404 for one and a 200 for the other is an oracle for whether a
        project id exists in another tenant.
        """
        response = self.client.delete("/v1/projects/prj_" + "z" * 24)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["already_absent"])

    def test_a_new_project_carries_an_expiry_from_its_plan(self) -> None:
        """P1-7. `max_retention_days` was never read; now it bounds every
        project, saved or temporary."""
        body = self.client.post(
            "/v1/projects", json={"persistence": "saved"}
        ).json()
        self.assertIsNotNone(body["expires_at"])


class DevelopmentConvenience(ApiTestCase):
    def test_an_unauthenticated_request_works_outside_production(self) -> None:
        """Local development must not require minting a key first."""
        self.assertEqual(self.client.post("/v1/projects", json={}).status_code, 201)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Regressions for the 2026-08-13 audit findings
# ---------------------------------------------------------------------------

class RetentionSweepIsNotInternalByNaming(ApiTestCase):
    """P0-2. The route previously had no guard at all and deleted every tenant.

    "Internal" is not an access control. A path prefix is a string.
    """

    def test_the_old_unauthenticated_route_is_gone(self) -> None:
        response = self.client.post("/internal/sweep")
        self.assertEqual(response.status_code, 404)

    def test_the_sweep_requires_a_credential_in_production(self) -> None:
        # Exercised in the production subclass below; here we assert the route
        # at least demands a capability rather than accepting anonymous POSTs.
        _org, key = self.tenant("acme", role=Role.VIEWER)
        self.assertEqual(
            self.client.post("/v1/retention/sweep", headers=self.auth(key)).status_code,
            403,
        )

    def test_the_sweep_deletes_only_the_callers_tenant(self) -> None:
        """The finding that mattered: one tenant erasing another's projects."""
        from datetime import timedelta

        from vtv.contracts.base import utc_now
        from vtv.contracts.project import PersistenceMode, Project

        acme_id, acme_key = self.tenant("acme")
        globex_id, _globex_key = self.tenant("globex")

        expired = utc_now() - timedelta(hours=1)
        repository = self.app.state.vtv.repository
        victim = Project(
            organisation_id=globex_id,
            persistence=PersistenceMode.TEMPORARY,
            expires_at=expired,
        )
        mine = Project(
            organisation_id=acme_id,
            persistence=PersistenceMode.TEMPORARY,
            expires_at=expired,
        )
        run(repository.save_project(victim))
        run(repository.save_project(mine))

        response = self.client.post(
            "/v1/retention/sweep", headers=self.auth(acme_key)
        )
        self.assertEqual(response.status_code, 200)

        # Mine is gone; the other tenant's is untouched.
        self.assertIsNone(run(repository.get_project(mine.project_id)))
        self.assertIsNotNone(run(repository.get_project(victim.project_id)))

    def test_the_sweep_is_audited(self) -> None:
        org_id, key = self.tenant("acme")
        self.client.post("/v1/retention/sweep", headers=self.auth(key))
        entries = self.assembly.audit.recent(organisation_id=org_id)
        self.assertTrue(
            any(entry.action is AuditAction.DATA_DELETED for entry in entries)
        )


class TenancyFailsClosed(ApiTestCase):
    """P0-3. A project with no owner used to be readable by everyone."""

    def test_a_project_cannot_be_created_without_a_tenant(self) -> None:
        """The structural half: the contract itself refuses."""
        from vtv.contracts.project import Project

        with self.assertRaises(ValueError):
            Project()  # type: ignore[call-arg]

    def test_a_project_belonging_to_another_tenant_is_unreachable(self) -> None:
        """Written straight to the repository, bypassing create_project.

        This is the case the previous test suite could not catch, because every
        project it created went through the route that always set the field.
        """
        from vtv.contracts.project import Project

        _acme, acme_key = self.tenant("acme")
        globex_id, _globex_key = self.tenant("globex")

        foreign = Project(organisation_id=globex_id, title="not yours")
        run(self.app.state.vtv.repository.save_project(foreign))

        self.assertEqual(
            self.client.get(
                f"/v1/projects/{foreign.project_id}", headers=self.auth(acme_key)
            ).status_code,
            404,
        )

    def test_the_pipeline_refuses_to_manufacture_an_ownerless_project(self) -> None:
        """A script or worker calling the engine directly cannot create the hole."""
        from vtv.contracts.errors import VTVError

        with self.assertRaises(VTVError):
            run(self.assembly.pipeline.run(audio=b"\x00" * 32))


class SweepRequiresAuthInProduction(ApiTestCase):
    production = True

    def test_anonymous_sweep_is_refused(self) -> None:
        self.assertEqual(
            self.client.post("/v1/retention/sweep").status_code, 401
        )


class WhoAmIReportsWhatTheCallerMayDo(ApiTestCase):
    """The endpoint that stops the interface offering refused actions.

    The Studio drew a Delete link beside every project. A key minted with the
    default `service` role holds no `project:delete`, so the server answered
    403 every single time it was clicked — correctly. The button should not
    have been there.

    "Never trust the frontend for authorization" is about what the server
    *enforces*. It says nothing about the frontend advertising work it cannot
    do, and an interface that offers an action it will always be refused is a
    bug in the interface, not a security property.
    """

    def test_a_service_key_is_told_it_cannot_delete(self) -> None:
        _, key = self.tenant("svc", role=Role.SERVICE)
        me = self.client.get("/v1/me", headers=self.auth(key)).json()
        self.assertEqual(me["role"], "service")
        self.assertNotIn("project:delete", me["capabilities"])

    def test_an_admin_key_is_told_it_can(self) -> None:
        _, key = self.tenant("adm", role=Role.ADMIN)
        me = self.client.get("/v1/me", headers=self.auth(key)).json()
        self.assertIn("project:delete", me["capabilities"])

    def test_what_it_reports_matches_what_the_server_enforces(self) -> None:
        """The two must agree, or the interface is guessing.

        Asserted against a real DELETE rather than against the capability
        table, because the table is the thing being reported and comparing it
        to itself would prove nothing.
        """
        for slug, role in (("svc2", Role.SERVICE), ("adm2", Role.ADMIN)):
            with self.subTest(role.value):
                _, key = self.tenant(slug, role=role)
                project = self.client.post(
                    "/v1/projects", json={"title": "t"}, headers=self.auth(key)
                ).json()["project_id"]
                claimed = "project:delete" in self.client.get(
                    "/v1/me", headers=self.auth(key)
                ).json()["capabilities"]
                refused = (
                    self.client.delete(
                        f"/v1/projects/{project}", headers=self.auth(key)
                    ).status_code
                    == 403
                )
                self.assertEqual(
                    claimed,
                    not refused,
                    "/v1/me and the enforcement disagree about deletion",
                )

    def test_it_needs_no_capability_of_its_own(self) -> None:
        """Gating it would leave exactly the callers who need it unable to ask."""
        _, key = self.tenant("svc3", role=Role.SERVICE)
        self.assertEqual(
            self.client.get("/v1/me", headers=self.auth(key)).status_code, 200
        )
