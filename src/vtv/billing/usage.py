"""Metering usage and enforcing quotas.

The two operations that matter are deliberately separate:

**Reserve, then commit.** A render is checked and reserved *before* the provider
calls are made, and the reservation is settled against the real figure
afterwards. Checking after the work is done produces an invoice, not a control;
checking without reserving lets ten concurrent requests each pass a check that
only one of them should.

**Every record carries its own cost.** The `CostLedger` records what a provider
call actually cost; a usage record joins to that, so gross margin per project is
a query rather than a reconstruction. A billing system that knows revenue and
not cost cannot tell you which customers are unprofitable, which is the number
that decides whether the company works.

STATUS: **EXECUTED, IN-PROCESS STORE.** The meter is correct and tested against
a real SQLite file. It is not connected to a payment processor — there is no
network access here — and `docs/BILLING.md` states what a production deployment
must add: an idempotent invoice writer and a reconciliation job against the
processor's own record of truth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import Field

from vtv.billing.plans import Plan, QuotaKind, plan_for
from vtv.contracts.base import Id, IdPrefix, RootDocument, Timestamped, new_id, utc_now
from vtv.contracts.errors import ErrorCode, PolicyViolation
from vtv.contracts.tenancy import PlanTier


class QuotaExceeded(PolicyViolation):
    """The tenant has used its allowance. Not retryable until the period turns."""

    code = ErrorCode.QUOTA_EXCEEDED
    user_message = "You have used your plan's allowance for this month."


class GenerationNotIncluded(QuotaExceeded):
    """This plan does not buy generated visuals.

    Distinct from the generic refusal because on the Free plan it is not a
    failure at all: ``GENERATED_ASSETS: 0`` is what that plan *is*. The video
    still gets made — the caller descends its visual fallback ladder to a drawn
    diagram or a stock clip — so the sentence the customer sees describes what
    we do instead rather than apologising for what we do not.
    """

    user_message = (
        "Your plan draws visuals rather than generating them, so this video "
        "uses drawn diagrams and stock footage instead. Upgrade to Starter or "
        "above to add AI-generated imagery."
    )


@dataclass(frozen=True)
class UsagePeriod:
    """A billing month, as a closed-open interval.

    Calendar months rather than rolling thirty-day windows: a customer
    reconciling an invoice thinks in months, and a rolling window makes "what
    did I use in March" unanswerable.
    """

    year: int
    month: int

    @classmethod
    def containing(cls, moment: datetime | None = None) -> UsagePeriod:
        moment = moment or utc_now()
        return cls(year=moment.year, month=moment.month)

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def start(self) -> datetime:
        return datetime(self.year, self.month, 1, tzinfo=UTC)

    @property
    def end(self) -> datetime:
        if self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=UTC)
        return datetime(self.year, self.month + 1, 1, tzinfo=UTC)

    def next(self) -> UsagePeriod:
        return UsagePeriod.containing(self.end)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def __str__(self) -> str:
        return self.key


class UsageRecord(RootDocument, Timestamped):
    """One metered event.

    Immutable once written. A usage record that can be edited is a usage record
    a customer cannot trust, and the fix for a mistake is a compensating record
    rather than a correction — exactly as in double-entry bookkeeping.
    """

    document_name = "usage_record"

    usage_record_id: Id = Field(default_factory=lambda: new_id(IdPrefix.GENERATION))
    organisation_id: Id
    project_id: Id | None = None
    kind: QuotaKind
    quantity: float = Field(ge=0.0)
    #: What this actually cost us, from the cost ledger. Kept alongside the
    #: billable quantity so margin is a subtraction rather than an estimate.
    cost_usd: float = Field(default=0.0, ge=0.0)
    period_key: str = Field(min_length=7, max_length=7)
    #: Deduplicates a retried job. Two records with the same key are one event.
    idempotency_key: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=200)


@dataclass(frozen=True)
class QuotaVerdict:
    """Whether the work may proceed, and what it would consume."""

    allowed: bool
    kind: QuotaKind
    used: float
    limit: float
    requested: float
    #: True when the plan permits going over and this request does.
    overage: bool = False
    overage_cost_usd: float = 0.0
    reason: str | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise QuotaExceeded(
                self.reason
                or (
                    f"{self.kind.value}: {self.used:g} of {self.limit:g} used, "
                    f"{self.requested:g} requested"
                )
            )


@dataclass(frozen=True)
class UsageSummary:
    """What one tenant consumed in one period."""

    organisation_id: str
    period: UsagePeriod
    plan: Plan
    totals: dict[QuotaKind, float]
    cost_usd: float

    def used(self, kind: QuotaKind) -> float:
        return self.totals.get(kind, 0.0)

    def remaining(self, kind: QuotaKind) -> float:
        return self.plan.remaining(kind, self.used(kind))

    @property
    def gross_margin_usd(self) -> float:
        """Revenue minus what the providers charged us.

        The number that decides whether a customer is worth having. Negative
        margin on a plan is a pricing bug, and it is invisible to any system
        that meters revenue without metering cost.
        """
        return round(self.plan.monthly_price_usd - self.cost_usd, 4)

    def as_dict(self) -> dict[str, object]:
        """The billing page's view, including which of these numbers are real.

        ``enforced`` is not decoration. Four of the eight quotas were declared
        with per-tier limits and metered by nothing, and this payload reported
        each of them as ``used: 0`` against its limit — a figure the API was
        inventing, and one a customer could reasonably read as "I have used
        none of my 1200 transcription minutes". The flag and its note come
        straight off :class:`QuotaKind`, so a quota cannot be added to a plan
        and quietly presented as enforced.
        """
        return {
            "organisation_id": self.organisation_id,
            "period": self.period.key,
            "plan": self.plan.tier.value,
            "usage": {
                kind.value: {
                    "used": round(self.used(kind), 4),
                    "limit": self.plan.limit(kind),
                    "remaining": self.remaining(kind),
                    "enforced": kind.is_enforced,
                    "note": kind.enforcement_note,
                }
                for kind in QuotaKind
            },
            "provider_cost_usd": round(self.cost_usd, 4),
            "gross_margin_usd": self.gross_margin_usd,
        }


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    usage_record_id TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    project_id      TEXT,
    kind            TEXT NOT NULL,
    quantity        REAL NOT NULL,
    cost_usd        REAL NOT NULL DEFAULT 0,
    period_key      TEXT NOT NULL,
    idempotency_key TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS usage_by_org_period
    ON usage_records (organisation_id, period_key, kind);

-- A retried job must not bill twice. Enforced in the schema, because an
-- application-level check loses to two workers racing.
CREATE UNIQUE INDEX IF NOT EXISTS usage_idempotency
    ON usage_records (organisation_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS usage_reservations (
    reservation_id  TEXT PRIMARY KEY,
    organisation_id TEXT NOT NULL,
    kind            TEXT NOT NULL,
    quantity        REAL NOT NULL,
    period_key      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reservations_by_org
    ON usage_reservations (organisation_id, period_key, kind);
"""


@dataclass
class UsageMeter:
    """Records usage and answers quota questions.

    Reservations are the part worth explaining. Between "may this render start"
    and "this render used 4.2 minutes" there is a window in which ten more
    requests can each pass the same check. A reservation occupies the allowance
    for the duration of that window, so concurrency cannot overshoot a quota —
    which matters most on exactly the plan where overshooting is free.
    """

    path: Path
    #: Resolves a tenant's tier. Injected so the meter does not depend on the
    #: organisation repository.
    tier_of: object = None
    in_memory: bool = False
    _records: list[UsageRecord] = field(default_factory=list)
    _reservations: dict[str, tuple[str, QuotaKind, float, str]] = field(
        default_factory=dict
    )
    #: Reservations older than this are treated as abandoned — the job that made
    #: one crashed. Without expiry a crash permanently consumes an allowance.
    reservation_ttl_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.in_memory:
            return
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def plan_of(self, organisation_id: str) -> Plan:
        if self.tier_of is None:
            return plan_for(PlanTier.FREE)
        tier = self.tier_of(organisation_id)  # type: ignore[operator]
        return plan_for(tier if isinstance(tier, PlanTier) else PlanTier.FREE)

    # -- recording --------------------------------------------------------

    def record(
        self,
        *,
        organisation_id: str,
        kind: QuotaKind,
        quantity: float,
        cost_usd: float = 0.0,
        project_id: str | None = None,
        idempotency_key: str | None = None,
        note: str | None = None,
        at: datetime | None = None,
    ) -> UsageRecord | None:
        """Meter one event. Returns ``None`` when it was already recorded.

        A duplicate is not an error. A retried render job legitimately calls
        this twice, and the correct behaviour is to bill once and carry on.
        """
        period = UsagePeriod.containing(at)
        entry = UsageRecord(
            organisation_id=organisation_id,
            project_id=project_id,
            kind=kind,
            quantity=max(0.0, quantity),
            cost_usd=max(0.0, cost_usd),
            period_key=period.key,
            idempotency_key=idempotency_key,
            note=note,
        )

        if self.in_memory:
            if idempotency_key and any(
                existing.idempotency_key == idempotency_key
                and existing.organisation_id == organisation_id
                for existing in self._records
            ):
                return None
            self._records.append(entry)
            return entry

        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO usage_records (usage_record_id, organisation_id, "
                    "project_id, kind, quantity, cost_usd, period_key, "
                    "idempotency_key, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.usage_record_id,
                        entry.organisation_id,
                        entry.project_id,
                        entry.kind.value,
                        entry.quantity,
                        entry.cost_usd,
                        entry.period_key,
                        entry.idempotency_key,
                        entry.note,
                        entry.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError:
            return None
        return entry

    # -- quotas -----------------------------------------------------------

    def check(
        self,
        *,
        organisation_id: str,
        kind: QuotaKind,
        requested: float,
        at: datetime | None = None,
    ) -> QuotaVerdict:
        """Would this consumption be allowed? Does not reserve anything."""
        plan = self.plan_of(organisation_id)
        period = UsagePeriod.containing(at)
        used = self.total(
            organisation_id=organisation_id, kind=kind, period=period
        ) + self._reserved(organisation_id, kind, period)
        limit = plan.limit(kind)

        if used + requested <= limit:
            return QuotaVerdict(True, kind, used, limit, requested)

        if plan.allow_overage and kind is QuotaKind.RENDERED_MINUTES:
            over = (used + requested) - limit
            return QuotaVerdict(
                True,
                kind,
                used,
                limit,
                requested,
                overage=True,
                overage_cost_usd=round(over * plan.overage_price_per_minute_usd, 4),
                reason=f"{over:.2f} minutes billed as overage",
            )

        return QuotaVerdict(
            False,
            kind,
            used,
            limit,
            requested,
            reason=(
                f"this would use {used + requested:g} of a {limit:g} "
                f"{kind.value} allowance"
            ),
        )

    def check_level(
        self,
        *,
        organisation_id: str,
        kind: QuotaKind,
        current: float,
        adding: float = 1.0,
    ) -> QuotaVerdict:
        """Judge a *level* against the plan: seats, active API keys.

        Levels are counted by looking at the thing, not by summing events, and
        the difference is not academic. Revoking an API key has to give the
        allowance back; a period-scoped sum of creation records never would, and
        it would also reset every month, so a tenant on a one-key plan could
        mint a second key in February. `api/app.py` was doing this correctly by
        hand for API keys — and calling :meth:`check` beside it and discarding
        the verdict, which read like a check and was not one. This is that rule
        with somewhere to live.

        Deliberately does not consult usage records or reservations: for a level
        they are the wrong number, and silently mixing them is how "used" stops
        matching what the customer can see in their own account.
        """
        plan = self.plan_of(organisation_id)
        limit = plan.limit(kind)
        current = max(0.0, current)
        if current + adding <= limit:
            return QuotaVerdict(True, kind, current, limit, adding)
        return QuotaVerdict(
            False,
            kind,
            current,
            limit,
            adding,
            reason=(
                f"your plan allows {limit:g} {kind.value}; you have "
                f"{current:g}. Upgrade your plan to raise the limit, or remove "
                f"one you are not using."
            ),
        )

    def reserve(
        self,
        *,
        organisation_id: str,
        kind: QuotaKind,
        quantity: float,
        at: datetime | None = None,
    ) -> tuple[QuotaVerdict, str | None]:
        """Check and hold an allowance. Returns the verdict and a reservation id.

        The reservation must be released — by :meth:`settle` when the work
        finishes, or by :meth:`release` when it fails. An abandoned one expires,
        so a crashed worker costs a tenant an hour of headroom rather than a
        month of it.
        """
        verdict = self.check(
            organisation_id=organisation_id, kind=kind, requested=quantity, at=at
        )
        if not verdict.allowed:
            return verdict, None

        period = UsagePeriod.containing(at)
        reservation_id = new_id(IdPrefix.GENERATION)
        if self.in_memory:
            self._reservations[reservation_id] = (
                organisation_id,
                kind,
                quantity,
                period.key,
            )
        if not self.in_memory:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO usage_reservations (reservation_id, "
                    "organisation_id, kind, quantity, period_key, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        reservation_id,
                        organisation_id,
                        kind.value,
                        quantity,
                        period.key,
                        utc_now().isoformat(),
                    ),
                )
        return verdict, reservation_id

    def settle(
        self,
        reservation_id: str,
        *,
        actual: float,
        cost_usd: float = 0.0,
        project_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> UsageRecord | None:
        """Turn a reservation into a usage record using the measured figure.

        The estimate is discarded. Billing a customer for what we guessed rather
        than what we produced is the kind of thing that ends up in a support
        thread and then in a refund.
        """
        held = self._take_reservation(reservation_id)
        if held is None:
            return None
        organisation_id, kind, _estimate, _period = held
        return self.record(
            organisation_id=organisation_id,
            kind=kind,
            quantity=actual,
            cost_usd=cost_usd,
            project_id=project_id,
            idempotency_key=idempotency_key,
        )

    def release(self, reservation_id: str) -> None:
        """Give back an allowance for work that failed. Costs the tenant nothing."""
        self._take_reservation(reservation_id)

    def _take_reservation(
        self, reservation_id: str
    ) -> tuple[str, QuotaKind, float, str] | None:
        """Remove a hold and return what it held, or ``None`` if it was gone.

        Removal and read are one operation so that two workers settling the
        same redelivered job cannot both believe they hold it.
        """
        if self.in_memory:
            return self._reservations.pop(reservation_id, None)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT organisation_id, kind, quantity, period_key "
                    "FROM usage_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    "DELETE FROM usage_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        self._reservations.pop(reservation_id, None)
        return (
            str(row["organisation_id"]),
            QuotaKind(row["kind"]),
            float(row["quantity"]),
            str(row["period_key"]),
        )

    def _forget_reservation(self, reservation_id: str) -> None:
        if self.in_memory:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM usage_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )

    def expire_reservations(self, *, now: datetime | None = None) -> int:
        """Drop abandoned holds. Returns how many were released.

        Called by the worker's maintenance loop every 30 seconds. It previously
        had no caller at all, so a crashed render held a tenant's quota until
        somebody noticed.
        """
        moment = now or utc_now()
        cutoff = moment - timedelta(seconds=self.reservation_ttl_seconds)
        if self.in_memory:
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT reservation_id FROM usage_reservations WHERE created_at < ?",
                (cutoff.isoformat(),),
            ).fetchall()
            for row in rows:
                self._reservations.pop(str(row["reservation_id"]), None)
            cursor = connection.execute(
                "DELETE FROM usage_reservations WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
            return int(cursor.rowcount)

    def _reserved(
        self, organisation_id: str, kind: QuotaKind, period: UsagePeriod
    ) -> float:
        """How much of this allowance is currently held by in-flight work.

        Reads the **table**, not a process dictionary. That was the audit's
        finding: `_reserved` consulted an in-memory dict while `reserve` wrote
        rows, so two replicas could not see each other's holds and the quota
        guarantee evaporated the moment the service was scaled out.
        """
        if self.in_memory:
            return sum(
                quantity
                for org, held_kind, quantity, period_key in self._reservations.values()
                if org == organisation_id
                and held_kind is kind
                and period_key == period.key
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS held FROM usage_reservations "
                "WHERE organisation_id = ? AND kind = ? AND period_key = ?",
                (organisation_id, kind.value, period.key),
            ).fetchone()
        return float(row["held"])

    # -- reading ----------------------------------------------------------

    def total(
        self,
        *,
        organisation_id: str,
        kind: QuotaKind,
        period: UsagePeriod | None = None,
    ) -> float:
        period = period or UsagePeriod.containing()
        if self.in_memory:
            return sum(
                entry.quantity
                for entry in self._records
                if entry.organisation_id == organisation_id
                and entry.kind is kind
                and entry.period_key == period.key
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS total FROM usage_records "
                "WHERE organisation_id = ? AND kind = ? AND period_key = ?",
                (organisation_id, kind.value, period.key),
            ).fetchone()
        return float(row["total"])

    def summary(
        self, *, organisation_id: str, period: UsagePeriod | None = None
    ) -> UsageSummary:
        period = period or UsagePeriod.containing()
        totals: dict[QuotaKind, float] = {}
        cost = 0.0

        if self.in_memory:
            for entry in self._records:
                if (
                    entry.organisation_id != organisation_id
                    or entry.period_key != period.key
                ):
                    continue
                totals[entry.kind] = totals.get(entry.kind, 0.0) + entry.quantity
                cost += entry.cost_usd
        else:
            with self._connect() as connection:
                for row in connection.execute(
                    "SELECT kind, SUM(quantity) AS quantity, SUM(cost_usd) AS cost "
                    "FROM usage_records WHERE organisation_id = ? AND period_key = ? "
                    "GROUP BY kind",
                    (organisation_id, period.key),
                ):
                    totals[QuotaKind(row["kind"])] = float(row["quantity"])
                    cost += float(row["cost"])

        return UsageSummary(
            organisation_id=organisation_id,
            period=period,
            plan=self.plan_of(organisation_id),
            totals=totals,
            cost_usd=round(cost, 6),
        )

    def records(
        self,
        *,
        organisation_id: str,
        period: UsagePeriod | None = None,
        limit: int = 500,
    ) -> list[UsageRecord]:
        """Line items, for the invoice detail a customer can reconcile against."""
        period = period or UsagePeriod.containing()
        if self.in_memory:
            return [
                entry
                for entry in self._records
                if entry.organisation_id == organisation_id
                and entry.period_key == period.key
            ][:limit]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_records WHERE organisation_id = ? "
                "AND period_key = ? ORDER BY created_at DESC LIMIT ?",
                (organisation_id, period.key, max(1, min(limit, 5000))),
            ).fetchall()
        return [
            UsageRecord(
                usage_record_id=str(row["usage_record_id"]),
                organisation_id=str(row["organisation_id"]),
                project_id=row["project_id"],
                kind=QuotaKind(row["kind"]),
                quantity=float(row["quantity"]),
                cost_usd=float(row["cost_usd"]),
                period_key=str(row["period_key"]),
                idempotency_key=row["idempotency_key"],
                note=row["note"],
            )
            for row in rows
        ]


@dataclass
class GeneratedAssetAllowance:
    """Turns ``QuotaKind.GENERATED_ASSETS`` from a declaration into a gate.

    Shaped to be injected into `vtv.pipeline.generation.GenerationRouter` beside
    the spend authoriser, and for the same reason that one is there: the router
    is the last point every paid image and video call passes through, and it is
    the *only* point that can refuse a single asset without failing the video.
    A check at the job boundary would authorise one render once and then let it
    generate without limit, which is exactly the defect the spend breaker was
    written to fix.

    It matters most on the Free plan, whose allowance is zero. Refusing there is
    not an error path: the router raising is how the caller is told to descend
    its visual fallback ladder, so the scene comes out drawn instead of
    generated. That is the plan working, which is why the refusal carries
    :class:`GenerationNotIncluded` and its product sentence rather than the
    generic "you have used your allowance".

    ``_pending`` is the same trick as `wiring.MeteredSpendAuthoriser`, for the
    same reason: generated assets reach the meter once, when a render job
    settles, which is after every asset that job bought. Checking the committed
    total alone would authorise a loop from the same stale figure a thousand
    times. Assets the router reports are held here as uncommitted, counted
    against the allowance immediately, and drained as the meter catches up —
    drained rather than cleared, so settlement does not charge twice.

    WIRED: `vtv.wiring.build` attaches this to
    `GenerationRouter.asset_authoriser`, so every image and video call is
    authorised here before dispatch, exactly as `PROVIDER_SPEND_USD` is
    authorised by `MeteredSpendAuthoriser` beside it. `jobs._record_generated_assets`
    still does the "record after" half against the measured cost ledger — that
    part was always correct, and settlement is still what the invoice is
    reconciled against.
    """

    meter: UsageMeter
    #: ``(organisation, period) -> assets generated but not yet in the meter``.
    _pending: dict[tuple[str, str], float] = field(default_factory=dict)
    #: The committed total each key was last reconciled against.
    _committed_seen: dict[tuple[str, str], float] = field(default_factory=dict)

    def authorise(self, *, organisation_id: str, count: float = 1.0) -> None:
        """Raise unless this tenant may buy ``count`` more generated assets."""
        verdict = self.meter.check(
            organisation_id=organisation_id,
            kind=QuotaKind.GENERATED_ASSETS,
            requested=count + self._uncommitted(organisation_id),
        )
        if verdict.allowed:
            return
        if verdict.limit <= 0:
            # Not "you ran out" — this plan never included generation. Saying
            # so plainly is a better product statement than an apology.
            raise GenerationNotIncluded(
                f"the plan for {organisation_id} includes no generated assets"
            )
        raise QuotaExceeded(
            f"generated assets for {organisation_id} are {verdict.used:g} of a "
            f"{verdict.limit:g} monthly allowance",
            user_message=(
                "This account has used all the AI-generated visuals its plan "
                "includes this month, so the rest of this video uses drawn "
                "diagrams and stock footage. Upgrade your plan to raise the "
                "limit, or wait for it to reset at the start of next month."
            ),
        )

    def note_generated(self, *, organisation_id: str, count: float = 1.0) -> None:
        """Record an asset that has just been bought, so the next check sees it."""
        if count <= 0:
            return
        key = (organisation_id, UsagePeriod.containing().key)
        self._pending[key] = self._pending.get(key, 0.0) + count

    def _uncommitted(self, organisation_id: str) -> float:
        """In-flight assets, after crediting anything the meter now knows about."""
        period = UsagePeriod.containing()
        key = (organisation_id, period.key)
        committed = self.meter.total(
            organisation_id=organisation_id,
            kind=QuotaKind.GENERATED_ASSETS,
            period=period,
        )
        settled_since = committed - self._committed_seen.get(key, 0.0)
        if settled_since > 0:
            self._pending[key] = max(0.0, self._pending.get(key, 0.0) - settled_since)
            self._committed_seen[key] = committed
        return self._pending.get(key, 0.0)


def invoice_lines(summary: UsageSummary) -> list[dict[str, object]]:
    """Turn a period into invoice lines.

    Subscription first, then overage. Deliberately plain data: the shape a
    payment processor wants changes with the processor, and none of that belongs
    in the meter.
    """
    lines: list[dict[str, object]] = [
        {
            "description": f"{summary.plan.display_name} plan — {summary.period.key}",
            "quantity": 1.0,
            "unit_price_usd": summary.plan.monthly_price_usd,
            "amount_usd": summary.plan.monthly_price_usd,
        }
    ]
    rendered = summary.used(QuotaKind.RENDERED_MINUTES)
    included = summary.plan.limit(QuotaKind.RENDERED_MINUTES)
    if summary.plan.allow_overage and rendered > included:
        over = rendered - included
        lines.append(
            {
                "description": "Additional rendered minutes",
                "quantity": round(over, 2),
                "unit_price_usd": summary.plan.overage_price_per_minute_usd,
                "amount_usd": round(
                    over * summary.plan.overage_price_per_minute_usd, 2
                ),
            }
        )
    return lines


def periods_between(first: date, last: date) -> Iterable[UsagePeriod]:
    """Every billing period in a range, for a usage report."""
    current = UsagePeriod(year=first.year, month=first.month)
    end = UsagePeriod(year=last.year, month=last.month)
    while (current.year, current.month) <= (end.year, end.month):
        yield current
        current = current.next()


__all__ = [
    "SCHEMA",
    "GeneratedAssetAllowance",
    "GenerationNotIncluded",
    "QuotaExceeded",
    "QuotaVerdict",
    "UsageMeter",
    "UsagePeriod",
    "UsageRecord",
    "UsageSummary",
    "invoice_lines",
    "periods_between",
]
