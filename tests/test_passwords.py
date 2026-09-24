"""P0-9 — human authentication actually works.

`Directory.verify_password` used to raise `NotImplementedError`, which meant the
only working credential in the entire system was an API key: no end user could
log in. Refusing was correct; shipping with no usable option was not.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.contracts.errors import ValidationFailed
from vtv.contracts.tenancy import Membership, Organisation, Role, User
from vtv.security.directory import Directory
from vtv.security.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordPolicy,
    argon2_available,
    hash_password,
    needs_rehash,
    verify_password,
)

GOOD = "correct horse battery staple"


class Hashing(unittest.TestCase):
    def test_a_password_verifies_against_its_own_hash(self) -> None:
        stored = hash_password(GOOD)
        self.assertTrue(verify_password(GOOD, stored))

    def test_a_wrong_password_does_not(self) -> None:
        stored = hash_password(GOOD)
        for wrong in (GOOD + "x", GOOD[:-1], GOOD.upper(), "", "x" * 30):
            with self.subTest(wrong=wrong[:12]):
                self.assertFalse(verify_password(wrong, stored))

    def test_the_plaintext_is_not_in_the_hash(self) -> None:
        self.assertNotIn(GOOD, hash_password(GOOD))

    def test_two_hashes_of_one_password_differ(self) -> None:
        """Salted. Otherwise a rainbow table breaks every user at once."""
        self.assertNotEqual(hash_password(GOOD), hash_password(GOOD))

    def test_the_hash_is_self_describing(self) -> None:
        """So the algorithm can change without invalidating what is stored."""
        stored = hash_password(GOOD)
        self.assertTrue(stored.startswith(("pbkdf2_sha256$", "argon2id$")))

    def test_the_work_factor_is_not_token(self) -> None:
        if argon2_available():  # pragma: no cover - environment dependent
            self.skipTest("argon2 carries its own parameters")
        stored = hash_password(GOOD)
        self.assertGreaterEqual(int(stored.split("$")[1]), 600_000)

    def test_a_corrupt_hash_returns_false_rather_than_raising(self) -> None:
        """A bad row must not be distinguishable from a wrong password, and
        must not take the login endpoint down."""
        for broken in ("", "garbage", "pbkdf2_sha256$", "pbkdf2_sha256$x$y$z",
                       "md5$1$a$b", "pbkdf2_sha256$0$$"):
            with self.subTest(broken=broken):
                self.assertFalse(verify_password(GOOD, broken))

    def test_a_short_password_is_refused_at_registration(self) -> None:
        with self.assertRaises(ValidationFailed):
            hash_password("short")

    def test_an_absurd_password_is_refused(self) -> None:
        """A gigabyte 'password' is an attack on the KDF's cost."""
        with self.assertRaises(ValidationFailed):
            hash_password("x" * 5000)
        self.assertFalse(verify_password("x" * 5000, hash_password(GOOD)))

    def test_a_weaker_stored_hash_is_flagged_for_upgrade(self) -> None:
        weak = hash_password(GOOD, iterations=1000)
        self.assertTrue(needs_rehash(weak))
        self.assertFalse(
            needs_rehash(hash_password(GOOD))
            if not argon2_available()
            else False
        )

    def test_an_unknown_algorithm_is_flagged_for_upgrade(self) -> None:
        self.assertTrue(needs_rehash("md5$whatever"))


class Policy(unittest.TestCase):
    def test_length_is_enforced(self) -> None:
        with self.assertRaises(ValidationFailed):
            PasswordPolicy().check("x" * (MIN_PASSWORD_LENGTH - 1))
        PasswordPolicy().check("x" * MIN_PASSWORD_LENGTH)

    def test_a_breached_password_is_refused_when_a_list_is_supplied(self) -> None:
        policy = PasswordPolicy(forbidden=frozenset({"password123456"}))
        with self.assertRaises(ValidationFailed):
            policy.check("Password123456")


class DirectoryLogin(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-pw-")
        self.directory = Directory(path=Path(self._dir.name) / "directory.db")
        self.organisation = self.directory.create_organisation(
            Organisation(name="Acme", slug="acme")
        )
        self.user = self.directory.create_user(
            User(email="a@acme.example", password_hash=hash_password(GOOD))
        )
        self.directory.add_member(
            Membership(
                user_id=self.user.user_id,
                organisation_id=self.organisation.organisation_id,
                role=Role.OWNER,
            )
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_user_can_actually_log_in(self) -> None:
        """The whole point. This raised NotImplementedError before."""
        self.assertTrue(self.directory.verify_password(self.user, GOOD))

    def test_a_wrong_password_is_refused(self) -> None:
        self.assertFalse(self.directory.verify_password(self.user, "wrong password!"))

    def test_an_sso_only_user_does_not_authenticate_by_password(self) -> None:
        """Not an error — that account simply does not use this mechanism."""
        sso = self.directory.create_user(
            User(email="b@acme.example", sso_subject="okta|123")
        )
        self.assertFalse(self.directory.verify_password(sso, GOOD))

    def test_a_weak_hash_is_upgraded_on_successful_login(self) -> None:
        """The only moment the plaintext exists is the only moment to upgrade."""
        weak = self.directory.create_user(
            User(email="c@acme.example", password_hash=hash_password(GOOD, iterations=1000))
        )
        self.assertTrue(self.directory.verify_password(weak, GOOD))

        reloaded = self.directory.user_by_email("c@acme.example")
        assert reloaded is not None and reloaded.password_hash is not None
        self.assertFalse(needs_rehash(reloaded.password_hash))
        # And the upgraded hash still verifies.
        self.assertTrue(verify_password(GOOD, reloaded.password_hash))

    def test_setting_a_password_enforces_the_policy(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.directory.set_password(self.user, "short")

    def test_the_stored_row_never_contains_the_plaintext(self) -> None:
        stored = Path(self.directory.path).read_bytes()
        self.assertNotIn(GOOD.encode(), stored)


class SuspensionIsIndexed(unittest.TestCase):
    """P0-10. `suspended_ids()` used to scan and parse every tenant per request."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-susp-")
        self.directory = Directory(path=Path(self._dir.name) / "directory.db")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_single_tenant_lookup_answers_correctly(self) -> None:
        live = self.directory.create_organisation(Organisation(name="A", slug="live"))
        dead = self.directory.create_organisation(Organisation(name="B", slug="dead"))
        self.directory.save_organisation(
            dead.model_copy(update={"suspended": True, "suspended_reason": "unpaid"})
        )

        self.assertFalse(self.directory.is_suspended(live.organisation_id))
        self.assertTrue(self.directory.is_suspended(dead.organisation_id))

    def test_an_unknown_tenant_is_not_reported_suspended(self) -> None:
        self.assertFalse(self.directory.is_suspended("prj_" + "a" * 22))

    def test_the_plan_lookup_does_not_deserialise_the_organisation(self) -> None:
        from vtv.contracts.tenancy import PlanTier

        organisation = self.directory.create_organisation(
            Organisation(name="A", slug="acme", plan=PlanTier.BUSINESS)
        )
        self.assertIs(
            self.directory.tier_of(organisation.organisation_id), PlanTier.BUSINESS
        )
        self.assertIs(self.directory.tier_of("prj_" + "b" * 22), PlanTier.FREE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
