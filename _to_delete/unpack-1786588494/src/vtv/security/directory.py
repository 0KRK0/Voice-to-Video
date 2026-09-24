"""Where organisations, members and API keys live.

STATUS: **EXECUTED.** Real SQLite storage, exercised by tests.

The design point worth stating: a key is looked up by its *public prefix*, then
its secret is verified in constant time against the stored digest. Looking up by
the full secret would mean either storing the secret or hashing on every row,
and looking up by digest alone loses the ability to say "your key ending 7f3a
was revoked". The prefix carries no secret and is indexed.

Password verification is deliberately absent. A password needs Argon2 or bcrypt,
neither of which is installable here, and a home-made KDF is worse than none.
:meth:`Directory.verify_password` therefore refuses rather than implementing
something weak — see `docs/SECURITY.md`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vtv.contracts.base import utc_now
from vtv.contracts.errors import NotFound, PolicyViolation, VTVError
from vtv.contracts.tenancy import (
    ApiKey,
    Membership,
    Organisation,
    PlanTier,
    Principal,
    PrincipalKind,
    Role,
    User,
)
from vtv.security.keys import public_prefix, verify_secret

SCHEMA = """
CREATE TABLE IF NOT EXISTS organisations (
    organisation_id TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    user_id         TEXT NOT NULL,
    organisation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    PRIMARY KEY (user_id, organisation_id)
);

CREATE TABLE IF NOT EXISTS api_keys (
    api_key_id      TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    prefix          TEXT NOT NULL UNIQUE,
    secret_hash     TEXT NOT NULL,
    payload         TEXT NOT NULL,
    revoked_at      TEXT
);

CREATE INDEX IF NOT EXISTS keys_by_org ON api_keys (organisation_id);
"""


@dataclass
class Directory:
    """Tenants, members and credentials."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # -- organisations ----------------------------------------------------

    def create_organisation(self, organisation: Organisation) -> Organisation:
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO organisations (organisation_id, slug, payload, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (
                        organisation.organisation_id,
                        organisation.slug,
                        organisation.model_dump_json(),
                        organisation.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PolicyViolation(
                    f"the handle {organisation.slug!r} is already taken"
                ) from exc
        return organisation

    def organisation(self, organisation_id: str) -> Organisation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM organisations WHERE organisation_id = ?",
                (organisation_id,),
            ).fetchone()
        return Organisation.model_validate_json(str(row["payload"])) if row else None

    def organisation_by_slug(self, slug: str) -> Organisation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM organisations WHERE slug = ?", (slug,)
            ).fetchone()
        return Organisation.model_validate_json(str(row["payload"])) if row else None

    def save_organisation(self, organisation: Organisation) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE organisations SET payload = ?, slug = ? "
                "WHERE organisation_id = ?",
                (
                    organisation.model_dump_json(),
                    organisation.slug,
                    organisation.organisation_id,
                ),
            )

    def tier_of(self, organisation_id: str) -> PlanTier:
        """The plan a tenant is on. Unknown tenants get the free plan."""
        organisation = self.organisation(organisation_id)
        return organisation.plan if organisation else PlanTier.FREE

    def suspended_ids(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT organisation_id, payload FROM organisations"
            ).fetchall()
        return {
            str(row["organisation_id"])
            for row in rows
            if not Organisation.model_validate_json(str(row["payload"])).is_active
        }

    # -- users and membership ---------------------------------------------

    def create_user(self, user: User) -> User:
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO users (user_id, email, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        user.user_id,
                        user.email.lower(),
                        user.model_dump_json(),
                        user.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PolicyViolation("that email is already registered") from exc
        return user

    def user_by_email(self, email: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM users WHERE email = ?", (email.lower(),)
            ).fetchone()
        return User.model_validate_json(str(row["payload"])) if row else None

    def add_member(self, membership: Membership) -> Membership:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO memberships (user_id, organisation_id, "
                "role, payload) VALUES (?, ?, ?, ?)",
                (
                    membership.user_id,
                    membership.organisation_id,
                    membership.role.value,
                    membership.model_dump_json(),
                ),
            )
        return membership

    def membership(self, user_id: str, organisation_id: str) -> Membership | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM memberships WHERE user_id = ? "
                "AND organisation_id = ?",
                (user_id, organisation_id),
            ).fetchone()
        return Membership.model_validate_json(str(row["payload"])) if row else None

    def members(self, organisation_id: str) -> list[Membership]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM memberships WHERE organisation_id = ?",
                (organisation_id,),
            ).fetchall()
        return [Membership.model_validate_json(str(row["payload"])) for row in rows]

    def remove_member(self, user_id: str, organisation_id: str) -> None:
        """Remove a member, refusing to leave an organisation with no owner.

        An organisation nobody can administer is a support ticket that cannot be
        resolved without a database edit.
        """
        remaining = [
            member
            for member in self.members(organisation_id)
            if member.user_id != user_id and member.role is Role.OWNER
        ]
        current = self.membership(user_id, organisation_id)
        if current is not None and current.role is Role.OWNER and not remaining:
            raise PolicyViolation("an organisation must keep at least one owner")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM memberships WHERE user_id = ? AND organisation_id = ?",
                (user_id, organisation_id),
            )

    # -- API keys ---------------------------------------------------------

    def store_key(self, key: ApiKey) -> ApiKey:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO api_keys (api_key_id, organisation_id, prefix, "
                "secret_hash, payload, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    key.api_key_id,
                    key.organisation_id,
                    key.prefix,
                    key.secret_hash,
                    key.model_dump_json(),
                ),
            )
        return key

    def keys_for(self, organisation_id: str) -> list[ApiKey]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM api_keys WHERE organisation_id = ?",
                (organisation_id,),
            ).fetchall()
        return [ApiKey.model_validate_json(str(row["payload"])) for row in rows]

    def revoke_key(self, api_key_id: str, *, at: datetime | None = None) -> ApiKey:
        moment = at or utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM api_keys WHERE api_key_id = ?", (api_key_id,)
            ).fetchone()
            if row is None:
                raise NotFound("no such API key")
            key = ApiKey.model_validate_json(str(row["payload"])).model_copy(
                update={"revoked_at": moment}
            )
            connection.execute(
                "UPDATE api_keys SET payload = ?, revoked_at = ? WHERE api_key_id = ?",
                (key.model_dump_json(), moment.isoformat(), api_key_id),
            )
        return key

    def authenticate(self, secret: str) -> Principal:
        """Resolve a presented API key into a principal.

        Every failure returns the same error. Distinguishing "no such key" from
        "wrong secret" from "revoked" tells an attacker which of their guesses
        was closest, and the user with a real problem gets the detail from the
        audit log instead.
        """
        try:
            prefix = public_prefix(secret)
        except VTVError as exc:
            raise _bad_credential() from exc

        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, secret_hash FROM api_keys WHERE prefix = ?",
                (prefix,),
            ).fetchone()
        if row is None:
            raise _bad_credential()

        if not verify_secret(secret, str(row["secret_hash"])):
            raise _bad_credential()

        key = ApiKey.model_validate_json(str(row["payload"]))
        if not key.is_active:
            raise _bad_credential()

        organisation = self.organisation(key.organisation_id)
        if organisation is None or not organisation.is_active:
            raise _bad_credential()

        return Principal(
            kind=PrincipalKind.API_KEY,
            subject=key.api_key_id,
            organisation_id=key.organisation_id,
            role=key.role,
            granted=sorted(key.capabilities, key=lambda item: item.value),
        )

    def principal_for_user(self, user_id: str, organisation_id: str) -> Principal:
        """The principal a signed-in user has *within one organisation*.

        Never a principal that spans organisations. A user who belongs to three
        tenants gets three principals and picks one per request, which is what
        keeps :meth:`Principal.owns` a simple equality.
        """
        membership = self.membership(user_id, organisation_id)
        if membership is None:
            raise _bad_credential()
        return Principal(
            kind=PrincipalKind.USER,
            subject=user_id,
            organisation_id=organisation_id,
            role=membership.role,
            granted=sorted(membership.capabilities, key=lambda item: item.value),
        )

    def verify_password(self, user: User, password: str) -> bool:
        """Not implemented, deliberately.

        A password needs Argon2id or bcrypt. Neither is installable in this
        environment, and a hand-rolled KDF would be a security theatre worse
        than the absence it replaces. This raises so that no deployment can
        accidentally ship password login backed by something weak.
        """
        del user, password
        raise NotImplementedError(
            "password verification requires argon2-cffi or bcrypt; install one "
            "and implement this against it. See docs/SECURITY.md."
        )


def _bad_credential() -> PolicyViolation:
    from vtv.security.authz import NotAuthenticated

    return NotAuthenticated("the credential presented is not usable")


def bootstrap(
    directory: Directory,
    *,
    name: str,
    slug: str,
    owner_email: str,
    plan: PlanTier = PlanTier.FREE,
) -> tuple[Organisation, User, Membership]:
    """Create the first organisation, its owner and their membership.

    Used by the CLI and by tests. The owner is created SSO-only, because there
    is no password hasher here and creating an account with a weak one would be
    the exact mistake `verify_password` exists to prevent.
    """
    organisation = directory.create_organisation(
        Organisation(name=name, slug=slug, plan=plan)
    )
    user = directory.create_user(
        User(email=owner_email, sso_subject=f"bootstrap|{owner_email}")
    )
    membership = directory.add_member(
        Membership(
            user_id=user.user_id,
            organisation_id=organisation.organisation_id,
            role=Role.OWNER,
            accepted_at=utc_now(),
        )
    )
    return organisation, user, membership


__all__ = ["SCHEMA", "Directory", "bootstrap"]
