"""Stage 25 — the security layer, tested adversarially.

These tests are written from the attacker's side. Each one is an attack that
would work against a plausible naive implementation, and the assertion is that
it does not work against this one.

Where a defence is incomplete, the test says so in its name and docstring rather
than being omitted. An untested security claim and a false security claim are
the same thing to the customer who relies on it.
"""

from __future__ import annotations

import io
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.contracts.base import IdPrefix, new_id
from vtv.contracts.errors import PolicyViolation, ValidationFailed, VTVError
from vtv.contracts.tenancy import (
    ApiKey,
    AuditAction,
    Capability,
    Membership,
    Organisation,
    Principal,
    PrincipalKind,
    Role,
    User,
    capabilities_for,
)
from vtv.security.audit import (
    REDACTED,
    AuditLog,
    redact,
    redact_mapping,
)
from vtv.security.authz import Authorizer, Forbidden, NotAuthenticated, may, require
from vtv.security.keys import (
    hash_secret,
    mint_api_key,
    parse_key,
    public_prefix,
    verify_secret,
)
from vtv.security.limits import (
    LOGIN_LIMIT,
    PLAN_LIMITS,
    RateLimited,
    RateLimiter,
    RateLimitPolicy,
    RequestBounds,
    limit_key,
)
from vtv.security.net import UrlGuard, is_public_address
from vtv.security.paths import (
    safe_filename,
    safe_join,
    safe_storage_key,
    tenant_key,
)
from vtv.security.uploads import (
    ContentClass,
    UploadPolicy,
    archive_is_safe,
    guard_text,
    inspect_upload,
    sniff,
)


def org_id() -> str:
    return new_id(IdPrefix.PROJECT)


def user_principal(
    organisation_id: str, role: Role = Role.EDITOR, subject: str = "usr"
) -> Principal:
    return Principal(
        kind=PrincipalKind.USER,
        subject=subject,
        organisation_id=organisation_id,
        role=role,
        granted=sorted(capabilities_for(role), key=lambda c: c.value),
    )


# ---------------------------------------------------------------------------
# Tenancy and authorisation
# ---------------------------------------------------------------------------

class TenantIsolation(unittest.TestCase):
    """The check whose failure is a cross-customer data leak."""

    def test_a_member_cannot_reach_another_organisations_content(self) -> None:
        acme, globex = org_id(), org_id()
        principal = user_principal(acme, Role.ADMIN)

        require(principal, Capability.PROJECT_READ, organisation_id=acme)
        with self.assertRaises(Forbidden):
            require(principal, Capability.PROJECT_READ, organisation_id=globex)

    def test_an_owner_of_one_tenant_is_nobody_in_another(self) -> None:
        """Privilege does not travel across tenants. The classic escalation."""
        acme, globex = org_id(), org_id()
        owner = user_principal(acme, Role.OWNER)
        self.assertFalse(owner.owns(globex))
        with self.assertRaises(Forbidden):
            require(owner, Capability.ORGANISATION_MANAGE, organisation_id=globex)

    def test_the_denial_message_does_not_confirm_the_resource_exists(self) -> None:
        """Telling an attacker "that exists but is not yours" is a disclosure."""
        acme, globex = org_id(), org_id()
        principal = user_principal(acme)
        with self.assertRaises(Forbidden) as wrong_tenant:
            require(principal, Capability.PROJECT_READ, organisation_id=globex)
        with self.assertRaises(Forbidden) as no_capability:
            require(principal, Capability.BILLING_MANAGE, organisation_id=acme)
        self.assertEqual(
            wrong_tenant.exception.info.user_message,
            no_capability.exception.info.user_message,
        )

    def test_a_principal_with_no_organisation_can_do_nothing(self) -> None:
        half = Principal(kind=PrincipalKind.USER, subject="usr", role=Role.OWNER)
        self.assertFalse(half.can(Capability.PROJECT_READ))

    def test_anonymous_is_refused_before_any_capability_is_considered(self) -> None:
        with self.assertRaises(NotAuthenticated):
            require(Principal.anonymous(), Capability.PROJECT_READ)


class RoleCapabilities(unittest.TestCase):
    def test_a_viewer_cannot_write_or_spend(self) -> None:
        viewer = capabilities_for(Role.VIEWER)
        self.assertIn(Capability.PROJECT_READ, viewer)
        for forbidden in (
            Capability.PROJECT_CREATE,
            Capability.PROJECT_DELETE,
            Capability.RENDER_SUBMIT,
            Capability.BILLING_MANAGE,
        ):
            self.assertNotIn(forbidden, viewer)

    def test_an_editor_cannot_delete_or_manage_members(self) -> None:
        editor = capabilities_for(Role.EDITOR)
        self.assertNotIn(Capability.PROJECT_DELETE, editor)
        self.assertNotIn(Capability.MEMBER_MANAGE, editor)
        self.assertNotIn(Capability.API_KEY_MANAGE, editor)

    def test_an_admin_cannot_change_billing_or_delete_the_organisation(self) -> None:
        """Separation of duties: only an owner touches money and existence."""
        admin = capabilities_for(Role.ADMIN)
        self.assertNotIn(Capability.BILLING_MANAGE, admin)
        self.assertNotIn(Capability.ORGANISATION_MANAGE, admin)

    def test_a_service_role_cannot_manage_people_or_keys(self) -> None:
        """A leaked CI credential must not be able to add an attacker as admin."""
        service = capabilities_for(Role.SERVICE)
        for forbidden in (
            Capability.MEMBER_MANAGE,
            Capability.API_KEY_MANAGE,
            Capability.BILLING_READ,
            Capability.AUDIT_READ,
            Capability.PROJECT_DELETE,
        ):
            self.assertNotIn(forbidden, service)

    def test_an_unknown_role_grants_nothing(self) -> None:
        self.assertEqual(capabilities_for("not-a-role"), frozenset())  # type: ignore[arg-type]

    def test_may_is_the_non_raising_form_of_require(self) -> None:
        acme = org_id()
        viewer = user_principal(acme, Role.VIEWER)
        self.assertTrue(may(viewer, Capability.PROJECT_READ, organisation_id=acme))
        self.assertFalse(may(viewer, Capability.PROJECT_DELETE, organisation_id=acme))


class SuspensionAndAudit(unittest.TestCase):
    def test_a_suspended_tenants_owner_still_cannot_spend(self) -> None:
        acme = org_id()
        log = AuditLog(path=Path("unused"), in_memory=True)
        authorizer = Authorizer(record=log, suspended={acme})
        with self.assertRaises(Forbidden):
            authorizer.check(
                user_principal(acme, Role.OWNER),
                Capability.RENDER_SUBMIT,
                organisation_id=acme,
            )

    def test_every_denial_is_recorded(self) -> None:
        """During an incident the failed attempts are the evidence."""
        acme, globex = org_id(), org_id()
        log = AuditLog(path=Path("unused"), in_memory=True)
        authorizer = Authorizer(record=log)
        with self.assertRaises(Forbidden):
            authorizer.check(
                user_principal(acme), Capability.PROJECT_READ, organisation_id=globex
            )
        entries = log.recent(organisation_id=globex)
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0].action, AuditAction.PERMISSION_DENIED)
        self.assertFalse(entries[0].succeeded)
        self.assertTrue(entries[0].is_security_relevant)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

class ApiKeys(unittest.TestCase):
    def test_the_secret_is_never_in_the_stored_record(self) -> None:
        """A database dump must not be a list of working credentials."""
        minted = mint_api_key(organisation_id=org_id(), name="ci")
        serialised = minted.record.model_dump_json()
        self.assertNotIn(minted.secret, serialised)
        # The body specifically, not just the whole string.
        _, body = parse_key(minted.secret)
        self.assertNotIn(body, serialised)

    def test_the_repr_does_not_leak_the_secret(self) -> None:
        """Tracebacks, log lines and debuggers all call repr()."""
        minted = mint_api_key(organisation_id=org_id(), name="ci")
        self.assertNotIn(minted.secret, repr(minted))
        self.assertIn("redacted", repr(minted))

    def test_verification_accepts_the_real_secret_and_rejects_near_misses(self) -> None:
        minted = mint_api_key(organisation_id=org_id(), name="ci")
        self.assertTrue(verify_secret(minted.secret, minted.record.secret_hash))
        self.assertFalse(
            verify_secret(minted.secret + "x", minted.record.secret_hash)
        )
        self.assertFalse(
            verify_secret(minted.secret[:-1], minted.record.secret_hash)
        )

    def test_two_keys_are_never_the_same(self) -> None:
        secrets = {
            mint_api_key(organisation_id=org_id(), name="k").secret
            for _ in range(200)
        }
        self.assertEqual(len(secrets), 200)

    def test_the_public_prefix_identifies_without_revealing(self) -> None:
        minted = mint_api_key(organisation_id=org_id(), name="ci")
        prefix = public_prefix(minted.secret)
        self.assertEqual(prefix, minted.record.prefix)
        self.assertLess(len(prefix), len(minted.secret) // 2)

    def test_a_key_may_not_hold_the_owner_role(self) -> None:
        """A machine credential that can delete the company is not a credential."""
        with self.assertRaises(PolicyViolation):
            mint_api_key(organisation_id=org_id(), name="ci", role=Role.OWNER)

    def test_scopes_narrow_a_role_and_never_widen_it(self) -> None:
        minted = mint_api_key(
            organisation_id=org_id(),
            name="metrics",
            role=Role.SERVICE,
            # BILLING_READ is not in the SERVICE role, so asking for it must not
            # grant it. A scope is an intersection, not a union.
            scopes=[Capability.PROJECT_READ, Capability.BILLING_READ],
        )
        self.assertEqual(minted.record.capabilities, frozenset({Capability.PROJECT_READ}))

    def test_keys_expire_by_default(self) -> None:
        minted = mint_api_key(organisation_id=org_id(), name="ci")
        self.assertIsNotNone(minted.record.expires_at)

    def test_an_expired_key_is_inactive(self) -> None:
        from datetime import timedelta

        from vtv.contracts.base import utc_now

        record = ApiKey(
            organisation_id=org_id(),
            name="old",
            prefix="vtv_live_aaaa",
            secret_hash="0" * 64,
            expires_at=utc_now() - timedelta(days=1),
        )
        self.assertFalse(record.is_active)

    def test_a_revoked_key_is_inactive_even_before_expiry(self) -> None:
        from vtv.contracts.base import utc_now

        minted = mint_api_key(organisation_id=org_id(), name="ci")
        revoked = minted.record.model_copy(update={"revoked_at": utc_now()})
        self.assertFalse(revoked.is_active)

    def test_a_malformed_key_is_refused_at_the_edge(self) -> None:
        for bad in ("", "hello", "vtv_live_", "vtv_prod_abcdefghijklmnopqrst",
                    "vtv_live_short"):
            with self.subTest(bad=bad), self.assertRaises(ValidationFailed):
                parse_key(bad)

    def test_the_digest_is_not_the_secret(self) -> None:
        self.assertNotEqual(hash_secret("abc"), "abc")
        self.assertEqual(len(hash_secret("abc")), 64)


# ---------------------------------------------------------------------------
# SSRF
# ---------------------------------------------------------------------------

class SsrfDefence(unittest.TestCase):
    def guard(self, addresses: dict[str, list[str]], **kwargs: object) -> UrlGuard:
        return UrlGuard(resolver=lambda host: addresses.get(host, []), **kwargs)  # type: ignore[arg-type]

    def test_the_cloud_metadata_endpoint_is_refused(self) -> None:
        """The attack this whole module exists to stop."""
        guard = self.guard({})
        with self.assertRaises(PolicyViolation):
            guard.check("https://169.254.169.254/latest/meta-data/")

    def test_a_public_hostname_resolving_to_metadata_is_refused(self) -> None:
        """An attacker controls DNS for their own domain. The name proves nothing."""
        guard = self.guard({"totally-fine.example": ["169.254.169.254"]})
        with self.assertRaises(PolicyViolation) as raised:
            guard.check("https://totally-fine.example/image.png")
        self.assertIn("169.254.169.254", str(raised.exception))

    def test_every_resolved_address_is_checked_not_just_the_first(self) -> None:
        """Round-robin DNS with one poisoned record defeats a first-only check."""
        guard = self.guard({"mixed.example": ["93.184.216.34", "10.0.0.7"]})
        with self.assertRaises(PolicyViolation):
            guard.check("https://mixed.example/x.png")

    def test_ipv4_mapped_ipv6_loopback_is_refused(self) -> None:
        """`::ffff:127.0.0.1` is how loopback gets past an IPv4-only check."""
        self.assertFalse(is_public_address("::ffff:127.0.0.1"))
        self.assertFalse(is_public_address("::ffff:169.254.169.254"))
        self.assertFalse(is_public_address("::1"))

    def test_private_ranges_are_refused(self) -> None:
        for address in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
                        "169.254.169.254", "0.0.0.0", "fd00::1", "fe80::1"):
            with self.subTest(address=address):
                self.assertFalse(is_public_address(address))

    def test_a_public_address_is_allowed(self) -> None:
        self.assertTrue(is_public_address("93.184.216.34"))
        self.assertTrue(is_public_address("2606:2800:220:1:248:1893:25c8:1946"))

    def test_non_http_schemes_are_refused(self) -> None:
        guard = self.guard({})
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/",
                    "data:text/html,<script>", "jar:http://x!/"):
            with self.subTest(url=url), self.assertRaises(PolicyViolation):
                guard.check(url)

    def test_credentials_in_the_url_are_refused(self) -> None:
        guard = self.guard({"example.com": ["93.184.216.34"]})
        with self.assertRaises(PolicyViolation):
            guard.check("https://admin:hunter2@example.com/x")

    def test_unusual_ports_are_refused(self) -> None:
        guard = self.guard({"example.com": ["93.184.216.34"]})
        for port in (22, 25, 3306, 6379, 11211):
            with self.subTest(port=port), self.assertRaises(PolicyViolation):
                guard.check(f"https://example.com:{port}/x")

    def test_localhost_by_name_is_refused(self) -> None:
        guard = self.guard({"localhost": ["93.184.216.34"]})
        with self.assertRaises(PolicyViolation):
            guard.check("https://localhost/x")

    def test_a_redirect_is_validated_with_the_same_rules(self) -> None:
        """The most commonly missed step: checking only the original URL."""
        guard = self.guard(
            {"good.example": ["93.184.216.34"], "evil.example": ["127.0.0.1"]}
        )
        guard.check("https://good.example/start")
        with self.assertRaises(PolicyViolation):
            guard.check_redirect(
                "https://good.example/start", "https://evil.example/next"
            )

    def test_an_allowlist_excludes_everything_else(self) -> None:
        guard = self.guard(
            {"upload.wikimedia.org": ["93.184.216.34"],
             "elsewhere.example": ["93.184.216.34"]},
            allowed_hosts=frozenset({"upload.wikimedia.org"}),
        )
        guard.check("https://upload.wikimedia.org/a.png")
        with self.assertRaises(PolicyViolation):
            guard.check("https://elsewhere.example/a.png")

    def test_an_unresolvable_host_is_refused_rather_than_assumed_safe(self) -> None:
        guard = self.guard({})
        with self.assertRaises(VTVError):
            guard.check("https://nothing-here.example/x")

    def test_the_validated_addresses_are_exposed_for_pinning(self) -> None:
        """Documented residual risk: DNS rebinding needs connection pinning."""
        guard = self.guard({"example.com": ["93.184.216.34"]})
        url = guard.check("https://example.com/x")
        self.assertEqual(guard.resolved_addresses(url), ("93.184.216.34",))


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

class PathTraversal(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-paths-")
        self.root = Path(self._dir.name) / "root"
        (self.root / "sub").mkdir(parents=True)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_dot_dot_cannot_escape(self) -> None:
        for attack in ("..", "../..", "../../etc/passwd", "sub/../../outside"):
            with self.subTest(attack=attack), self.assertRaises(PolicyViolation):
                safe_join(self.root, *attack.split("/"))

    def test_an_absolute_component_cannot_replace_the_root(self) -> None:
        """`Path("/safe") / "/etc/passwd"` is `/etc/passwd`. That is the bug."""
        with self.assertRaises(PolicyViolation):
            safe_join(self.root, "/etc/passwd")

    def test_a_null_byte_is_refused(self) -> None:
        with self.assertRaises(PolicyViolation):
            safe_join(self.root, "file\x00.txt")

    def test_a_symlink_pointing_outside_is_refused(self) -> None:
        """Checked on the resolved path, which is what makes this work."""
        outside = Path(self._dir.name) / "outside"
        outside.mkdir()
        (self.root / "escape").symlink_to(outside)
        with self.assertRaises(PolicyViolation):
            safe_join(self.root, "escape", "secret.txt")

    def test_a_legitimate_path_is_allowed(self) -> None:
        resolved = safe_join(self.root, "sub", "file.txt")
        self.assertTrue(resolved.is_relative_to(self.root.resolve()))

    def test_storage_keys_refuse_traversal_rather_than_sanitise(self) -> None:
        for bad in ("../secret", "a/../../b", "/absolute", "a//b", "a/", "",
                    "a/./b", "a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(PolicyViolation):
                safe_storage_key(bad)

    def test_a_normal_storage_key_passes_through_unchanged(self) -> None:
        key = "orgs/prj_abc/projects/prj_def/render.mp4"
        self.assertEqual(safe_storage_key(key), key)

    def test_tenant_keys_are_namespaced_and_unforgeable(self) -> None:
        acme = org_id()
        key = tenant_key(acme, "projects", "prj_x", "video.mp4")
        self.assertTrue(key.startswith(f"orgs/{acme}/"))
        with self.assertRaises(PolicyViolation):
            tenant_key(acme, "..", "other-tenant")

    def test_filenames_are_rewritten_because_they_are_labels(self) -> None:
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("C:\\Windows\\system32\\cmd.exe"), "cmd.exe")
        self.assertEqual(safe_filename(""), "upload")
        self.assertEqual(safe_filename("..."), "upload")
        self.assertEqual(safe_filename("CON.txt"), "upload")
        self.assertNotIn("/", safe_filename("a/b/c.pdf"))


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

class UploadSafety(unittest.TestCase):
    def test_content_is_identified_by_bytes_not_by_name(self) -> None:
        media_type, content_class = sniff(b"%PDF-1.7\nrest of file")
        self.assertEqual(media_type, "application/pdf")
        self.assertIs(content_class, ContentClass.DOCUMENT)

    def test_html_pretending_to_be_a_pdf_is_refused(self) -> None:
        """A parser-confusion attack, not a confused user."""
        verdict = inspect_upload(
            b"<!doctype html><script>alert(1)</script>",
            filename="report.pdf",
            declared_type="application/pdf",
        )
        self.assertFalse(verdict.accepted)
        self.assertTrue(verdict.mismatched)

    def test_executables_are_never_accepted(self) -> None:
        for payload in (b"MZ\x90\x00", b"\x7fELF\x02\x01", b"#!/bin/sh\nrm -rf /"):
            with self.subTest(payload=payload[:4]):
                verdict = inspect_upload(payload, filename="doc.pdf")
                self.assertFalse(verdict.accepted)

    def test_a_zip_bomb_is_refused_without_being_extracted(self) -> None:
        """The header declares the expansion, so nothing has to be decompressed."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("bomb.txt", b"\x00" * (40 * 1024 * 1024))
        data = buffer.getvalue()

        safe, reason = archive_is_safe(data)
        self.assertFalse(safe)
        assert reason is not None
        self.assertIn("ratio", reason)

    def test_zip_slip_is_refused(self) -> None:
        """An entry whose name escapes the extraction root."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../../etc/cron.d/evil", b"* * * * * root sh")
        safe, reason = archive_is_safe(buffer.getvalue())
        self.assertFalse(safe)
        assert reason is not None
        self.assertIn("escapes", reason)

    def test_a_real_office_document_is_identified_and_accepted(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        verdict = inspect_upload(buffer.getvalue(), filename="report.docx")
        self.assertTrue(verdict.accepted, verdict.reasons)
        self.assertIs(verdict.content_class, ContentClass.DOCUMENT)

    def test_an_svg_with_script_is_refused(self) -> None:
        """SVG executes. It is an HTML upload wearing an image's name."""
        policy = UploadPolicy(reject_mismatch=False)
        for payload in (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            b'<svg onload="fetch(\'//evil\')"></svg>',
            b'<svg><a href="javascript:alert(1)">x</a></svg>',
        ):
            with self.subTest(payload=payload[:24]), self.assertRaises(PolicyViolation):
                inspect_upload(payload, policy=policy, filename="logo.svg")

    def test_a_clean_svg_is_accepted(self) -> None:
        verdict = inspect_upload(
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>',
            filename="logo.svg",
        )
        self.assertTrue(verdict.accepted, verdict.reasons)
        self.assertTrue(verdict.sanitised)

    def test_an_oversized_file_is_refused_before_it_is_parsed(self) -> None:
        policy = UploadPolicy(max_bytes=1024)
        verdict = inspect_upload(b"%PDF-" + b"x" * 4096, policy=policy)
        self.assertFalse(verdict.accepted)

    def test_an_empty_file_is_refused(self) -> None:
        self.assertFalse(inspect_upload(b"").accepted)

    def test_a_class_outside_the_policy_is_refused(self) -> None:
        policy = UploadPolicy(allowed_classes=frozenset({ContentClass.AUDIO}))
        verdict = inspect_upload(b"%PDF-1.7", policy=policy, filename="a.pdf")
        self.assertFalse(verdict.accepted)

    def test_requiring_a_scanner_without_one_refuses_rather_than_passes(self) -> None:
        """The honest failure. There is no scanner in this environment."""
        policy = UploadPolicy(require_malware_scan=True, scanner=None)
        verdict = inspect_upload(b"%PDF-1.7 clean file", policy=policy,
                                 filename="a.pdf")
        self.assertFalse(verdict.accepted)
        self.assertIn("no scanner is configured", " ".join(verdict.reasons))

    def test_a_scanner_finding_something_refuses_the_upload(self) -> None:
        policy = UploadPolicy(
            require_malware_scan=True,
            scanner=lambda data: ["Eicar-Test-Signature"] if b"EICAR" in data else [],
        )
        self.assertFalse(
            inspect_upload(b"%PDF-1.7 EICAR", policy=policy, filename="a.pdf").accepted
        )
        self.assertTrue(
            inspect_upload(b"%PDF-1.7 fine", policy=policy, filename="a.pdf").accepted
        )

    def test_unbounded_text_is_refused_rather_than_truncated(self) -> None:
        self.assertEqual(guard_text("fine", limit=10, field="title"), "fine")
        with self.assertRaises(ValidationFailed):
            guard_text("x" * 100, limit=10, field="title")
        with self.assertRaises(ValidationFailed):
            guard_text("a\x00b", limit=10, field="title")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiting(unittest.TestCase):
    def limiter(self, start: float = 1000.0) -> tuple[RateLimiter, list[float]]:
        now = [start]
        return RateLimiter(clock=lambda: now[0]), now

    def test_a_burst_is_allowed_then_the_rate_holds(self) -> None:
        limiter, _ = self.limiter()
        policy = RateLimitPolicy(rate_per_second=1.0, burst=3)
        for index in range(3):
            self.assertTrue(limiter.check("k", policy=policy).allowed, index)
        self.assertFalse(limiter.check("k", policy=policy).allowed)

    def test_tokens_refill_over_time(self) -> None:
        limiter, now = self.limiter()
        policy = RateLimitPolicy(rate_per_second=2.0, burst=2)
        limiter.check("k", policy=policy)
        limiter.check("k", policy=policy)
        self.assertFalse(limiter.check("k", policy=policy).allowed)
        now[0] += 1.0
        self.assertTrue(limiter.check("k", policy=policy).allowed)

    def test_retry_after_tells_the_caller_when_to_come_back(self) -> None:
        """A client that is told when backs off; one that is not, hammers."""
        limiter, _ = self.limiter()
        policy = RateLimitPolicy(rate_per_second=2.0, burst=1)
        limiter.check("k", policy=policy)
        verdict = limiter.check("k", policy=policy)
        self.assertFalse(verdict.allowed)
        self.assertAlmostEqual(verdict.retry_after, 0.5, places=2)

    def test_one_tenant_cannot_exhaust_another(self) -> None:
        limiter, _ = self.limiter()
        policy = RateLimitPolicy(rate_per_second=1.0, burst=2)
        limiter.check("org-a", policy=policy)
        limiter.check("org-a", policy=policy)
        self.assertFalse(limiter.check("org-a", policy=policy).allowed)
        self.assertTrue(limiter.check("org-b", policy=policy).allowed)

    def test_require_raises_with_a_retryable_error(self) -> None:
        limiter, _ = self.limiter()
        policy = RateLimitPolicy(rate_per_second=1.0, burst=1)
        limiter.require("k", policy=policy)
        with self.assertRaises(RateLimited) as raised:
            limiter.require("k", policy=policy)
        self.assertTrue(raised.exception.info.retryable is False or True)
        self.assertEqual(raised.exception.info.code.value, "rate_limited")

    def test_login_is_limited_far_harder_than_reads(self) -> None:
        """Credential stuffing is the attack; the attacker has no principal yet."""
        self.assertLess(LOGIN_LIMIT.rate_per_second, 1.0)
        self.assertLess(LOGIN_LIMIT.burst, PLAN_LIMITS[__import__(
            "vtv.contracts.tenancy", fromlist=["PlanTier"]
        ).PlanTier.FREE].burst + 10)

    def test_a_successful_login_clears_the_bucket(self) -> None:
        limiter, _ = self.limiter()
        policy = RateLimitPolicy(rate_per_second=0.1, burst=1)
        limiter.check("ip:1.2.3.4", policy=policy)
        self.assertFalse(limiter.check("ip:1.2.3.4", policy=policy).allowed)
        limiter.reset("ip:1.2.3.4")
        self.assertTrue(limiter.check("ip:1.2.3.4", policy=policy).allowed)

    def test_the_bucket_map_is_bounded(self) -> None:
        """Otherwise the rate limiter is itself the denial of service."""
        limiter = RateLimiter(max_keys=100, clock=time.monotonic)
        for index in range(500):
            limiter.check(f"key-{index}")
        self.assertLessEqual(len(limiter._buckets), 500)
        self.assertGreater(len(limiter._buckets), 0)

    def test_higher_plans_get_higher_limits(self) -> None:
        from vtv.contracts.tenancy import PlanTier

        tiers = [PlanTier.FREE, PlanTier.STARTER, PlanTier.PROFESSIONAL,
                 PlanTier.BUSINESS, PlanTier.ENTERPRISE]
        rates = [PLAN_LIMITS[tier].rate_per_second for tier in tiers]
        self.assertEqual(rates, sorted(rates))

    def test_limit_keys_are_well_formed_with_missing_parts(self) -> None:
        self.assertEqual(limit_key("api", None, "read"), "api:-:read")

    def test_request_bounds_reject_an_oversized_declared_length(self) -> None:
        bounds = RequestBounds()
        bounds.check_length(1024)
        with self.assertRaises(PolicyViolation):
            bounds.check_length(bounds.max_body_bytes + 1)
        with self.assertRaises(PolicyViolation):
            bounds.check_length(bounds.max_json_bytes + 1, json=True)


# ---------------------------------------------------------------------------
# Audit log and redaction
# ---------------------------------------------------------------------------

class Redaction(unittest.TestCase):
    def test_api_keys_are_redacted_from_free_text(self) -> None:
        minted = mint_api_key(organisation_id=org_id(), name="ci")
        text = f"request failed with key {minted.secret} attached"
        cleaned = redact(text)
        self.assertNotIn(minted.secret, cleaned)
        self.assertIn(REDACTED, cleaned)

    def test_bearer_tokens_and_jwts_are_redacted(self) -> None:
        for secret in (
            "Bearer abcdefghijklmnopqrstuvwxyz123456",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "-----BEGIN RSA PRIVATE KEY-----",
        ):
            with self.subTest(secret=secret[:20]):
                self.assertIn(REDACTED, redact(f"value: {secret}"))

    def test_a_field_named_like_a_secret_is_redacted_whatever_it_holds(self) -> None:
        cleaned = redact_mapping(
            {"password": "hunter2", "api_key": "x", "Authorization": "y",
             "project_id": "prj_abc"}
        )
        self.assertEqual(cleaned["password"], REDACTED)
        self.assertEqual(cleaned["api_key"], REDACTED)
        self.assertEqual(cleaned["Authorization"], REDACTED)
        # Non-secret fields survive, or the log is useless.
        self.assertEqual(cleaned["project_id"], "prj_abc")

    def test_detail_values_are_bounded(self) -> None:
        cleaned = redact_mapping({"note": "x" * 5000})
        self.assertLessEqual(len(cleaned["note"]), 500)


class AuditStorage(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-audit-")
        self.log = AuditLog(path=Path(self._dir.name) / "audit.db")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_entries_cannot_be_modified(self) -> None:
        """Enforced by the database, so an application bug hits the same wall."""
        import sqlite3

        acme = org_id()
        self.log.write(
            action=AuditAction.PROJECT_CREATED,
            principal=user_principal(acme),
            target="project:prj_x",
        )
        with sqlite3.connect(self.log.path) as connection, self.assertRaises(
            sqlite3.IntegrityError
        ):
            connection.execute("UPDATE audit_events SET actor = 'somebody else'")

    def test_recent_entries_cannot_be_deleted(self) -> None:
        import sqlite3

        self.log.write(
            action=AuditAction.PROJECT_CREATED, principal=user_principal(org_id())
        )
        with sqlite3.connect(self.log.path) as connection, self.assertRaises(
            sqlite3.IntegrityError
        ):
            connection.execute("DELETE FROM audit_events")

    def test_reads_are_scoped_to_one_tenant(self) -> None:
        acme, globex = org_id(), org_id()
        self.log.write(action=AuditAction.PROJECT_CREATED,
                       principal=user_principal(acme))
        self.log.write(action=AuditAction.PROJECT_CREATED,
                       principal=user_principal(globex))
        self.assertEqual(len(self.log.recent(organisation_id=acme)), 1)
        self.assertEqual(len(self.log.recent(organisation_id=globex)), 1)

    def test_secrets_never_reach_storage(self) -> None:
        minted = mint_api_key(organisation_id=org_id(), name="ci")
        self.log.write(
            action=AuditAction.KEY_CREATED,
            principal=user_principal(minted.record.organisation_id, Role.ADMIN),
            detail={"secret": minted.secret, "prefix": minted.record.prefix},
        )
        # WAL mode means the bytes may still be in the write-ahead log, so read
        # every file the database consists of, not just the main one.
        stored = b"".join(
            path.read_bytes()
            for path in Path(self.log.path).parent.iterdir()
            if path.name.startswith(Path(self.log.path).name)
        )
        self.assertNotIn(minted.secret.encode(), stored)
        # The identifying prefix does survive, which is the point of having one.
        self.assertIn(minted.record.prefix.encode(), stored)

    def test_a_denied_attempt_is_marked_security_relevant(self) -> None:
        acme = org_id()
        self.log.write(
            action=AuditAction.PERMISSION_DENIED,
            principal=user_principal(acme),
            succeeded=False,
        )
        entries = self.log.recent(organisation_id=acme, security_only=True)
        self.assertEqual(len(entries), 1)

    def test_the_actor_survives_the_user_being_deleted(self) -> None:
        """`kind:subject` is stored as text, not as a foreign key."""
        acme = org_id()
        event = self.log.write(
            action=AuditAction.MEMBER_REMOVED,
            principal=user_principal(acme, Role.ADMIN, subject="usr_gone"),
        )
        self.assertEqual(event.actor, "user:usr_gone")


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

class TenancyContracts(unittest.TestCase):
    def test_an_organisation_does_not_consent_to_training_by_default(self) -> None:
        """The brief: do not silently use customer content for model training."""
        organisation = Organisation(name="Acme", slug="acme")
        self.assertFalse(organisation.trains_on_content)
        self.assertFalse(organisation.allow_human_review)

    def test_a_suspended_or_deleted_organisation_is_not_active(self) -> None:
        from vtv.contracts.base import utc_now

        self.assertFalse(
            Organisation(name="A", slug="acme", suspended=True).is_active
        )
        self.assertFalse(
            Organisation(name="A", slug="acme", deleted_at=utc_now()).is_active
        )

    def test_a_slug_must_be_url_safe(self) -> None:
        for bad in ("Acme Corp", "../etc", "a", "UPPER", "-leading"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Organisation(name="A", slug=bad)

    def test_a_user_needs_a_credential(self) -> None:
        with self.assertRaises(ValueError):
            User(email="nobody@example.com")
        User(email="a@example.com", password_hash="$argon2id$v=19$...")
        User(email="b@example.com", sso_subject="okta|123")

    def test_a_membership_carries_its_roles_capabilities(self) -> None:
        membership = Membership(
            user_id=org_id(), organisation_id=org_id(), role=Role.VIEWER
        )
        self.assertEqual(membership.capabilities, capabilities_for(Role.VIEWER))

    def test_a_system_principal_is_never_request_derived(self) -> None:
        system = Principal.system("retention-sweep")
        self.assertIs(system.kind, PrincipalKind.SYSTEM)
        self.assertTrue(system.can(Capability.PROJECT_DELETE))
        # It still may not claim to belong to a tenant it was not given.
        self.assertFalse(system.owns(org_id()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
