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

import contextlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from vtv.contracts.base import utc_now
from vtv.contracts.errors import NotFound, PolicyViolation, VTVError
from vtv.contracts.devices import (
    PAIRING_TTL_SECONDS,
    Device,
    DeviceHardware,
    PairingCode,
)
from vtv.contracts.tenancy import (
    ApiKey,
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Principal,
    PrincipalKind,
    Role,
    User,
)
from vtv.security.devices import (
    CODE_ALPHABET,
    CODE_LENGTH,
    DeviceToken,
    hash_token,
    mint_device,
    normalise_code,
    token_prefix,
    verify_token,
)
from vtv.security.keys import public_prefix, verify_secret
from vtv.security.passwords import (
    PasswordPolicy,
    hash_password,
    needs_rehash,
    verify_password,
)


@runtime_checkable
class SeatAuthoriser(Protocol):
    """Whether an organisation may add one more member.

    Same shape, and for the same reason, as
    `vtv.pipeline.generation.SpendAuthoriser`: docs/ARCHITECTURE.md keeps
    `security/` importing nothing from `billing/` — they are sibling
    cross-cutting packages, neither below the other — so `Directory` cannot
    know what a plan is or what `QuotaKind.SEATS` means. It knows only that
    something outside it can veto the next membership. `vtv.wiring
    .PlanSeatAuthoriser` is the implementation that answers this from the
    tenant's plan.
    """

    def authorise(self, *, organisation_id: str, current_members: int) -> None:
        """Raise a :class:`VTVError` if one more member would exceed the plan."""
        ...

SCHEMA = """
CREATE TABLE IF NOT EXISTS organisations (
    organisation_id TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    -- Denormalised from the payload so the request path can answer "is this
    -- tenant active, and on what plan" with an indexed read instead of parsing
    -- JSON for every organisation on every request.
    active          INTEGER NOT NULL DEFAULT 1,
    plan            TEXT NOT NULL DEFAULT 'free',
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS organisations_active ON organisations (active);

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

CREATE TABLE IF NOT EXISTS devices (
    device_id       TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    prefix          TEXT NOT NULL UNIQUE,
    token_hash      TEXT NOT NULL,
    payload         TEXT NOT NULL,
    -- Denormalised out of the payload so "which of this tenant's computers are
    -- reachable" is an indexed read rather than a parse of every row, which is
    -- a question the render dispatcher asks on every job.
    last_seen_at    TEXT,
    revoked_at      TEXT
);

CREATE INDEX IF NOT EXISTS devices_by_org ON devices (organisation_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code            TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    payload         TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    -- The single-use guard. Redemption updates this row conditionally on it
    -- being NULL, so two computers racing on the same code produce one device
    -- and one refusal rather than two devices.
    consumed_at     TEXT
);

CREATE INDEX IF NOT EXISTS pairing_expiry ON pairing_codes (expires_at);

-- One row per tenant, saying "work is expected here shortly, poll quickly".
--
-- An idle computer polling every five seconds is about seventeen thousand
-- requests a day to be told there is nothing, forever. Backing off is the
-- obvious fix and it buys the delay back at the worst moment: somebody presses
-- Render and waits a minute for a machine that had settled into a slow poll.
--
-- This is how the two are reconciled. Pressing Render, or opening the devices
-- panel, writes a timestamp here; a device that finds one still fresh is told
-- to come back in a couple of seconds instead of following its own ladder. It
-- is a hint, not a command — a device that never reads it still works, just
-- more slowly — which is why a missing row, an old row and a broken table all
-- degrade to "use your own interval" rather than to an error.
--
-- In the database rather than in memory because the API runs as more than one
-- process, and a hint the other worker cannot see is a hint that fires about
-- half the time and is impossible to reason about when it does not.
CREATE TABLE IF NOT EXISTS device_attention (
    organisation_id TEXT PRIMARY KEY,
    until           TEXT NOT NULL
);

-- Pairing that starts on the computer instead of in the browser.
--
-- The typed-code flow above is org-first: an admin who is already signed in
-- mints a code and carries it to the machine. This one is device-first, the way
-- signing a desktop app into an account normally goes — you run it, a browser
-- opens, you sign in, and the app is paired. Which account the computer joins
-- is decided by whoever approves it, which is why `organisation_id` starts
-- empty here and is filled in at approval.
--
-- A separate table rather than columns bolted onto `pairing_codes` because the
-- two flows differ in the thing that matters — when the tenant is known — and
-- because `CREATE TABLE IF NOT EXISTS` silently does nothing to an existing
-- table, so added columns would be missing on every database that already
-- exists and absent in a way nothing reports.
--
-- **No token is ever stored here.** Approval records only that a person said
-- yes and which account they said it for; the device credential is minted when
-- the waiting computer collects it, and exists in the database only as a hash
-- from that moment on. Parking a live token in a row until somebody fetches it
-- would be a readable credential at rest, waiting, for exactly as long as the
-- laptop takes to notice.
CREATE TABLE IF NOT EXISTS pairing_requests (
    -- Hashed, not stored raw: this is the secret the waiting computer proves
    -- itself with, and a leaked table should not hand somebody every pending
    -- pairing.
    device_code_hash TEXT PRIMARY KEY,
    -- What the person reads off one screen and confirms on another. Short,
    -- unambiguous, and the only part a human ever sees.
    user_code        TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    payload          TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    organisation_id  TEXT,
    approved_at      TEXT,
    approved_by      TEXT,
    -- The single-use guard, same shape as `pairing_codes.consumed_at`: the
    -- collection is a conditional UPDATE on this being NULL, so two processes
    -- racing produce one device and one refusal.
    collected_at     TEXT
);

CREATE INDEX IF NOT EXISTS pairing_request_expiry ON pairing_requests (expires_at);
"""


@dataclass
class Directory:
    """Tenants, members and credentials."""

    path: Path
    #: Veto on adding a member beyond the plan's seat count. Optional on the
    #: type — `Directory` is built directly by `bootstrap`, by the CLI and by
    #: most tests, none of which have a plan to check against — and mandatory
    #: in `vtv.wiring.build`, which always attaches one backed by the usage
    #: meter. Same optionality, and the same reasoning, as
    #: `GenerationRouter.spend_authoriser`.
    seat_authoriser: SeatAuthoriser | None = None

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
                    "INSERT INTO organisations (organisation_id, slug, active, "
                    "plan, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        organisation.organisation_id,
                        organisation.slug,
                        int(organisation.is_active),
                        organisation.plan.value,
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
                "UPDATE organisations SET payload = ?, slug = ?, active = ?, "
                "plan = ? WHERE organisation_id = ?",
                (
                    organisation.model_dump_json(),
                    organisation.slug,
                    int(organisation.is_active),
                    organisation.plan.value,
                    organisation.organisation_id,
                ),
            )

    def tier_of(self, organisation_id: str) -> PlanTier:
        """The plan a tenant is on. Unknown tenants get the free plan.

        Reads the denormalised column rather than deserialising the whole
        organisation, because this runs on every single request.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT plan FROM organisations WHERE organisation_id = ?",
                (organisation_id,),
            ).fetchone()
        if row is None:
            return PlanTier.FREE
        try:
            return PlanTier(row["plan"])
        except ValueError:
            return PlanTier.FREE

    def is_suspended(self, organisation_id: str) -> bool:
        """Whether one tenant is suspended. Indexed, O(1).

        This replaced `suspended_ids()`, which every request called and which
        read *and JSON-parsed every organisation in the deployment*. Request
        cost grew linearly with customer count — the kind of thing that is
        invisible at ten tenants and fatal at ten thousand.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT active FROM organisations WHERE organisation_id = ?",
                (organisation_id,),
            ).fetchone()
        return row is not None and not bool(row["active"])

    def active_organisation_ids(self) -> list[str]:
        """Every tenant a scheduled job should visit.

        For the worker's retention sweep, never for a request. Suspended and
        deleted tenants are excluded here and swept by the account-closure path
        instead: a suspended customer's data must not be quietly deleted by a
        routine job while the account is still recoverable.

        Ordered so that a sweep interrupted part way resumes over the same
        sequence and makes progress rather than revisiting the same prefix.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT organisation_id FROM organisations WHERE active = 1 "
                "ORDER BY organisation_id"
            ).fetchall()
        return [str(row["organisation_id"]) for row in rows]

    def suspended_ids(self) -> set[str]:
        """Every suspended tenant. For an operator view, never for a request."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT organisation_id FROM organisations WHERE active = 0"
            ).fetchall()
        return {str(row["organisation_id"]) for row in rows}

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
        """Add a member, or update an existing one's role.

        Only a genuinely *new* membership consumes a seat. `INSERT OR REPLACE`
        below also covers a role change on an existing member, and refusing
        "promote this person to admin" because the seat count is full would
        enforce a headcount quota against a request that adds no head. The
        check is skipped entirely when no `seat_authoriser` is attached, which
        is every `Directory` except the one `vtv.wiring.build` constructs — see
        the field's docstring.
        """
        if self.seat_authoriser is not None and self.membership(
            membership.user_id, membership.organisation_id
        ) is None:
            self.seat_authoriser.authorise(
                organisation_id=membership.organisation_id,
                current_members=len(self.members(membership.organisation_id)),
            )
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

    # -- devices ----------------------------------------------------------
    #
    # A customer's own computer, and the code that pairs one to an account.
    # Here rather than in a store of their own because this is the file that
    # answers "who is this, and what may they do" — a device is a credential
    # before it is anything else, and splitting credentials across two databases
    # is how one of them ends up with a subtly different revocation rule.

    def create_pairing_code(self, code: PairingCode) -> PairingCode:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO pairing_codes (code, organisation_id, payload, "
                "expires_at, consumed_at) VALUES (?, ?, ?, ?, NULL)",
                (
                    code.code,
                    code.organisation_id,
                    code.model_dump_json(),
                    code.expires_at.isoformat(),
                ),
            )
        return code

    def redeem_pairing_code(
        self,
        raw_code: str,
        *,
        name: str,
        hardware: DeviceHardware | None = None,
    ) -> DeviceToken:
        """Turn a typed code into a paired device. Single use, enforced here.

        One transaction, and the consumption is conditional on the code still
        being unconsumed. Checking first and writing second would leave a window
        in which two computers typing the same code both pass the check — small,
        real, and exactly the sort of race that only shows up once a product has
        users.

        Every failure returns the same refusal. Distinguishing "no such code"
        from "expired" from "already used" tells whoever is guessing how close
        they got, and the person with a genuine problem is better served by
        generating another code than by a more specific error.
        """
        code = normalise_code(raw_code)
        moment = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM pairing_codes WHERE code = ? "
                "AND consumed_at IS NULL AND expires_at > ?",
                (code, moment.isoformat()),
            ).fetchone()
            if row is None:
                raise _bad_credential()
            pairing = PairingCode.model_validate_json(str(row["payload"]))

            organisation = self.organisation(pairing.organisation_id)
            if organisation is None or not organisation.is_active:
                raise _bad_credential()

            minted = mint_device(
                organisation_id=pairing.organisation_id,
                name=name,
                hardware=hardware,
                paired_by=pairing.created_by,
            )
            consumed = pairing.model_copy(
                update={"consumed_at": moment, "device_id": minted.record.device_id}
            )
            claimed = connection.execute(
                "UPDATE pairing_codes SET payload = ?, consumed_at = ? "
                "WHERE code = ? AND consumed_at IS NULL",
                (consumed.model_dump_json(), moment.isoformat(), code),
            )
            if claimed.rowcount != 1:
                # Somebody else redeemed it between the read and the write.
                raise _bad_credential()
            connection.execute(
                "INSERT INTO devices (device_id, organisation_id, prefix, "
                "token_hash, payload, last_seen_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    minted.record.device_id,
                    minted.record.organisation_id,
                    minted.record.prefix,
                    minted.record.token_hash,
                    minted.record.model_dump_json(),
                    moment.isoformat(),
                ),
            )
        return minted

    def authenticate_device(self, secret: str) -> tuple[Principal, Device]:
        """Resolve a device token into a principal and the device it names.

        The device comes back alongside the principal because callers need it:
        a dispatcher deciding whether to offer this machine GPU work needs the
        hardware it reported, and re-reading it would mean a second query on
        every poll.

        Same uniform refusal as `authenticate`, for the same reason.
        """
        try:
            prefix = token_prefix(secret)
        except VTVError as exc:
            raise _bad_credential() from exc

        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, token_hash FROM devices WHERE prefix = ?",
                (prefix,),
            ).fetchone()
        if row is None:
            raise _bad_credential()
        if not verify_token(secret, str(row["token_hash"])):
            raise _bad_credential()

        device = Device.model_validate_json(str(row["payload"]))
        if not device.is_active:
            raise _bad_credential()

        organisation = self.organisation(device.organisation_id)
        if organisation is None or not organisation.is_active:
            raise _bad_credential()

        principal = Principal(
            kind=PrincipalKind.DEVICE,
            subject=device.device_id,
            organisation_id=device.organisation_id,
            role=Role.DEVICE,
            granted=[Capability.DEVICE_EXECUTE],
        )
        return principal, device

    def devices_for(self, organisation_id: str) -> list[Device]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM devices WHERE organisation_id = ? "
                "ORDER BY last_seen_at DESC",
                (organisation_id,),
            ).fetchall()
        return [Device.model_validate_json(str(row["payload"])) for row in rows]

    def device(self, device_id: str) -> Device | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return Device.model_validate_json(str(row["payload"])) if row else None

    def touch_device(
        self, device_id: str, *, hardware: DeviceHardware | None = None
    ) -> Device:
        """Record that a device is alive, and what it now says it has.

        Hardware is re-read on every pairing *and* every poll rather than once,
        because it changes: a driver is installed, a card is replaced, a laptop
        is docked. A machine whose graphics card stopped verifying must stop
        being offered graphics work on the next poll, not at the next pairing.
        """
        moment = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                raise NotFound("no such device")
            device = Device.model_validate_json(str(row["payload"]))
            update: dict[str, object] = {"last_seen_at": moment}
            if hardware is not None:
                update["hardware"] = hardware
            device = device.model_copy(update=update)
            connection.execute(
                "UPDATE devices SET payload = ?, last_seen_at = ? WHERE device_id = ?",
                (device.model_dump_json(), moment.isoformat(), device_id),
            )
        return device

    def revoke_device(self, device_id: str, *, at: datetime | None = None) -> Device:
        """Unpair a computer. Terminal: it must pair again to come back."""
        moment = at or utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None:
                raise NotFound("no such device")
            device = Device.model_validate_json(str(row["payload"])).model_copy(
                update={"revoked_at": moment}
            )
            connection.execute(
                "UPDATE devices SET payload = ?, revoked_at = ? WHERE device_id = ?",
                (device.model_dump_json(), moment.isoformat(), device_id),
            )
        return device

    def sweep_pairing_codes(self, *, now: datetime | None = None) -> int:
        """Delete codes that can no longer be redeemed.

        Consumed ones go too. The row's only remaining purpose after redemption
        is to say "already used", and the uniform refusal means an expired code
        and a used one are indistinguishable to a caller anyway — so keeping
        them is a growing table that answers a question nobody may ask.
        """
        moment = now or utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pairing_codes WHERE consumed_at IS NOT NULL "
                "OR expires_at <= ?",
                (moment.isoformat(),),
            )
        return int(cursor.rowcount)

    # -- pairing that starts on the computer -------------------------------

    def open_pairing_request(
        self,
        *,
        name: str,
        hardware: DeviceHardware | None = None,
        ttl_seconds: int = PAIRING_TTL_SECONDS,
    ) -> tuple[str, str, datetime]:
        """Begin a browser sign-in. Returns (device_code, user_code, expiry).

        Unauthenticated, because the whole point is that this computer has no
        credential yet. What stops it being an open door is that the request is
        inert until a signed-in person approves it: on its own it creates
        nothing, belongs to no account, and expires in minutes.
        """
        device_code = secrets.token_urlsafe(32)
        expires_at = utc_now() + timedelta(seconds=ttl_seconds)
        for _ in range(8):
            user_code = "".join(
                secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH)
            )
            try:
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO pairing_requests (device_code_hash, "
                        "user_code, name, payload, expires_at, organisation_id, "
                        "approved_at, approved_by, collected_at) "
                        "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                        (
                            hash_token(device_code),
                            user_code,
                            name[:120] or "A computer",
                            (hardware or DeviceHardware()).model_dump_json(),
                            expires_at.isoformat(),
                        ),
                    )
                return device_code, user_code, expires_at
            except sqlite3.IntegrityError:
                # A live request already holds that user code. Thirty characters
                # to the sixth is 729 million, so this is rare — and retrying is
                # still much better than handing somebody a code that would
                # approve a stranger's computer.
                continue
        raise VTVError("could not allocate a pairing code; try again")

    def pending_pairing(self, user_code: str) -> dict[str, Any] | None:
        """What a person is being asked to approve, or None.

        So the approval screen can say *which computer* — "Approve
        DESKTOP-4F2A?" rather than "Approve?". A confirmation nobody can check
        is a confirmation everybody clicks.
        """
        code = normalise_code(user_code)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_code, name, payload, expires_at FROM "
                "pairing_requests WHERE user_code = ? AND collected_at IS NULL "
                "AND expires_at > ?",
                (code, utc_now().isoformat()),
            ).fetchone()
        if row is None:
            return None
        return {
            "user_code": str(row["user_code"]),
            "name": str(row["name"]),
            "hardware": DeviceHardware.model_validate_json(str(row["payload"])),
            "expires_at": str(row["expires_at"]),
        }

    def approve_pairing(
        self, user_code: str, *, organisation_id: str, approved_by: str | None = None
    ) -> bool:
        """A signed-in person says yes, and says which account.

        This is where the tenant is decided — not at `open_pairing_request`,
        which knows nothing about who is asking. The approval is the whole of
        the authorisation, which is why it needs `DEVICE_MANAGE` at the route
        and why it is conditional on the row still being unapproved.
        """
        if not organisation_id:
            return False
        code = normalise_code(user_code)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE pairing_requests SET organisation_id = ?, "
                "approved_at = ?, approved_by = ? WHERE user_code = ? "
                "AND approved_at IS NULL AND collected_at IS NULL "
                "AND expires_at > ?",
                (
                    organisation_id,
                    utc_now().isoformat(),
                    approved_by,
                    code,
                    utc_now().isoformat(),
                ),
            )
        return updated.rowcount == 1

    def collect_pairing(
        self, device_code: str, *, hardware: DeviceHardware | None = None
    ) -> DeviceToken | None:
        """The waiting computer takes its credential. None while unapproved.

        The device is minted **here**, at collection, not at approval. Nothing
        anywhere ever holds a usable device token at rest: it is created, handed
        to the one computer that proved it owns the device code, and thereafter
        exists only as a hash.

        `None` means "not yet" and is the ordinary answer — the desktop polls
        this while the person is still finding their password. An expired,
        unknown or already-collected request raises the same uniform refusal as
        every other bad credential in this file.
        """
        moment = utc_now()
        digest = hash_token(device_code)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_code, name, organisation_id, approved_at, "
                "approved_by, payload FROM pairing_requests "
                "WHERE device_code_hash = ? AND collected_at IS NULL "
                "AND expires_at > ?",
                (digest, moment.isoformat()),
            ).fetchone()
            if row is None:
                raise _bad_credential()
            if row["approved_at"] is None:
                return None

            organisation_id = str(row["organisation_id"] or "")
            organisation = self.organisation(organisation_id)
            if organisation is None or not organisation.is_active:
                raise _bad_credential()

            minted = mint_device(
                organisation_id=organisation_id,
                name=str(row["name"]),
                hardware=hardware
                or DeviceHardware.model_validate_json(str(row["payload"])),
                paired_by=row["approved_by"],
            )
            # Conditional on still being uncollected, so two polls racing —
            # which a retrying client makes likely, not unlikely — produce one
            # device and one refusal rather than two devices on one approval.
            claimed = connection.execute(
                "UPDATE pairing_requests SET collected_at = ? "
                "WHERE device_code_hash = ? AND collected_at IS NULL",
                (moment.isoformat(), digest),
            )
            if claimed.rowcount != 1:
                raise _bad_credential()
            connection.execute(
                "INSERT INTO devices (device_id, organisation_id, prefix, "
                "token_hash, payload, last_seen_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    minted.record.device_id,
                    minted.record.organisation_id,
                    minted.record.prefix,
                    minted.record.token_hash,
                    minted.record.model_dump_json(),
                    moment.isoformat(),
                ),
            )
        return minted

    def sweep_pairing_requests(self, *, now: datetime | None = None) -> int:
        """Delete requests that can no longer be collected."""
        moment = now or utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pairing_requests WHERE collected_at IS NOT NULL "
                "OR expires_at <= ?",
                (moment.isoformat(),),
            )
        return int(cursor.rowcount)

    def nudge(self, organisation_id: str, *, seconds: float = 90.0) -> None:
        """Tell this tenant's computers that work is expected shortly.

        Called when somebody presses Render, and when the devices panel is
        opened — the two moments a person is watching and a slow poll is felt as
        the product being slow.

        Failures are swallowed. This is a latency optimisation; a deployment
        whose database is momentarily unhappy should render a few seconds later,
        not fail to render.
        """
        if not organisation_id:
            return
        until = (utc_now() + timedelta(seconds=max(0.0, seconds))).isoformat()
        with contextlib.suppress(Exception):
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO device_attention (organisation_id, until) "
                    "VALUES (?, ?) ON CONFLICT(organisation_id) DO UPDATE SET "
                    # `max` so a fresh nudge never shortens a longer one that is
                    # still running.
                    "until = max(excluded.until, device_attention.until)",
                    (organisation_id, until),
                )

    def attention_wanted(self, organisation_id: str, *, now: datetime | None = None) -> bool:
        """Whether a device for this tenant should be polling quickly."""
        if not organisation_id:
            return False
        moment = (now or utc_now()).isoformat()
        with contextlib.suppress(Exception):
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM device_attention WHERE organisation_id = ? "
                    "AND until > ?",
                    (organisation_id, moment),
                ).fetchone()
            return row is not None
        return False

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
        """Check a password, and transparently upgrade a weak stored hash.

        Uses Argon2id when `argon2-cffi` is installed and PBKDF2-HMAC-SHA256 at
        600,000 iterations otherwise — see `vtv.security.passwords` for why that
        is an acceptable fallback rather than a compromise.

        A user with no password hash (SSO-only) returns `False` rather than
        raising: an account that cannot use this mechanism is not an error, it
        simply does not authenticate this way.
        """
        if user.password_hash is None:
            return False
        if not verify_password(password, user.password_hash):
            return False

        if needs_rehash(user.password_hash):
            # The only moment the plaintext exists, so the only moment an
            # upgrade is possible. Failing to store it is not a login failure.
            with contextlib.suppress(Exception):
                self.set_password(user, password)
        return True

    def set_password(self, user: User, password: str, *, policy: object = None) -> User:
        """Set or replace a password, enforcing the deployment's policy."""
        rules = policy if isinstance(policy, PasswordPolicy) else PasswordPolicy()
        rules.check(password)
        updated = user.model_copy(update={"password_hash": hash_password(password)})
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET payload = ? WHERE user_id = ?",
                (updated.model_dump_json(), updated.user_id),
            )
        return updated


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


__all__ = ["SCHEMA", "Directory", "SeatAuthoriser", "bootstrap"]
