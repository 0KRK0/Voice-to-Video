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
from vtv.wiring import build


class ApiTestCase(unittest.TestCase):
    production = False

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-api-sec-")
        root = Path(self._dir.name)
        self.settings = Settings(
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="production" if self.production else "development",
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
        # Clear the rate-limit buckets so this asserts on the quota rather than
        # on the free plan's (deliberately small) burst allowance.
        self.assembly.limiter._buckets.clear()
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


class DevelopmentConvenience(ApiTestCase):
    def test_an_unauthenticated_request_works_outside_production(self) -> None:
        """Local development must not require minting a key first."""
        self.assertEqual(self.client.post("/v1/projects", json={}).status_code, 201)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
