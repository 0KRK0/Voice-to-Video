"""P1-7 — deletion that actually deletes, and retention that is actually a policy.

Two findings, one module, because they are the same operation seen from two
sides: "remove this project now" and "remove this project when its time is up".

**Deletion was incomplete.** `delete_project` removed the database rows and left
every byte on disk — the rendered video, the narration audio, the fetched
assets. A customer told their project was gone still had it, which is a
compliance failure (GDPR Article 17 is about the data, not the index) before it
is a storage bill.

**Retention was not enforced.** `Plan.max_retention_days` existed on all five
plans, was documented as "a plan that keeps content forever is a plan whose
storage cost grows without bound" — and was **never read**. Expiry came from one
global `temporary_retention_hours`, identical for a free trial and an enterprise
contract.

## The ordering, and why it is the way round it is

Storage first, then the database. Both orders can be interrupted, so the
question is which wreckage is recoverable:

* database first, then storage: the row is gone, so nothing knows the objects
  exist. They are invisible, unbilled, undeletable except by a full-bucket
  scan. That is a permanent leak of customer data.
* storage first, then database: the objects are gone and the row remains,
  pointing at nothing. The project reads as broken, the sweep finds it again on
  the next pass, and the deletion completes. Recoverable, and visibly so.

The second failure mode is strictly better, so deletion is ordered to produce
it. This is the same reasoning as "reserve then settle" in billing: when a
crash is inevitable, choose where it lands.

## The sweep deleted live projects

A third finding, found after the first two were fixed. `sweep_orphans` handed a
tenant prefix and a 24-hour age to a filesystem walk:

    for path in base.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()

No retention class — the class lives on the `ObjectRef` in the database and the
sweep walks a disk — and no check that a project row still exists. Every object
in a tenant's namespace older than a day went: `PROJECT` assets, captions, the
renders of saved projects whose plan promises ten years. A test asserted this
behaviour, which is how it survived.

`SweepPolicy` below closes the gap. It is the single place that decides whether
bytes may go, it takes the retention class and the set of live projects as
inputs, and `StorageProvider.sweep_expired` cannot be called without one.

## Idempotence

Every operation here is safe to repeat. The sweep is a scheduled job with
at-least-once delivery, so it *will* be repeated, and a deletion that throws on
its second run turns a duplicate message into a dead-lettered job and a tenant
whose retention silently stops running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vtv.billing.plans import PLANS, Plan
from vtv.contracts.base import RetentionClass, utc_now
from vtv.contracts.errors import ErrorCode, VTVError
from vtv.contracts.project import PersistenceMode, Project
from vtv.contracts.tenancy import PlanTier
from vtv.security.paths import TENANT_ROOT, project_prefix, tenant_prefix


@dataclass(frozen=True)
class DeletionReport:
    """What a deletion actually removed. Returned rather than logged.

    A caller that cannot see the object count cannot tell "deleted a project
    with no artefacts" from "failed to find the artefacts", and those need
    different responses.
    """

    project_id: str
    objects_deleted: int = 0
    records_deleted: int = 0
    #: Set when the project was already gone. Not an error — a retried job and
    #: a user pressing delete twice both land here — but worth distinguishing.
    already_absent: bool = False

    @property
    def is_complete(self) -> bool:
        return self.already_absent or self.records_deleted > 0


def project_of_key(key: str) -> str | None:
    """The project a storage key belongs to, or ``None`` if it names no project.

    Keys are built by `tenant_key()` and project-scoped ones have the shape
    ``orgs/<organisation>/projects/<project>/…``. Reading the owner back out of
    the key is what lets a sweep that walks *storage* ask a question about the
    *database* without a lookup per object.

    Anything else — a tenant-level object, a key some future writer invents —
    returns ``None``, which the policy below treats as "cannot prove this is an
    orphan" and therefore keeps.
    """
    parts = key.split("/")
    if len(parts) >= 5 and parts[0] == TENANT_ROOT and parts[2] == "projects":
        return parts[3]
    return None


@dataclass(frozen=True)
class SweepDecision:
    """Whether one object may be deleted, and the reason either way.

    The reason is returned rather than logged because a sweep that deletes the
    wrong thing and a sweep that deletes nothing look identical from the
    outside, and both have happened here.
    """

    delete: bool
    reason: str


@dataclass(frozen=True)
class SweepPolicy:
    """The one place that decides whether stored bytes may be swept.

    **This is the fix for the P1 defect.** `sweep_expired` used to be a
    filesystem walk with an `mtime` comparison in it: no retention class, no
    idea whether a project still existed. Called with a 24-hour window over a
    tenant's whole namespace, it deleted `PROJECT`-class assets belonging to
    live, saved projects on plans that promise ten years of retention.

    The rule lives on this object, and `sweep_expired` cannot be called without
    one, so there is no path through the adapter that deletes bytes without
    asking. Putting the same three `if`s at the call site is exactly the shape
    of defect every audit of this repo has found.

    The adapter supplies the facts (key, age, class); this supplies the policy.
    That split is what lets the local adapter and the S3 adapter share one
    definition of "may be deleted" while reading the class from a sidecar file
    and from an object tag respectively.
    """

    #: How old an `EPHEMERAL` object must be before it may go. Raw voice is
    #: `EPHEMERAL`, so this is the window in `docs/STORAGE_POLICY.md`.
    ephemeral_window_seconds: float
    #: `Plan.max_retention_days` in seconds — the ceiling on *bytes*, not just
    #: on rows. `None` means the tenant's plan could not be determined, and an
    #: unknown ceiling authorises no deletion at all: defaulting to the most
    #: restrictive plan would delete an enterprise tenant's bytes after seven
    #: days the first time the directory was not wired in.
    plan_ceiling_seconds: float | None = None
    #: Projects that still exist in the database. `None` means "the caller could
    #: not enumerate them", which disables orphan deletion entirely rather than
    #: treating every object as an orphan.
    live_project_ids: frozenset[str] | None = None

    def decide(
        self, *, key: str, age_seconds: float, retention: RetentionClass | None
    ) -> SweepDecision:
        """May the sweep delete this object?

        Ordered deliberately. Each rule that *keeps* an object is checked before
        every rule that deletes one, because the cost of the two mistakes is not
        symmetric: keeping a byte too long is a storage line item, deleting a
        customer's saved render is unrecoverable.
        """
        if retention is None:
            # Fail closed. An object with no recorded class is one written
            # before this policy existed, or by a path that bypassed `put` —
            # either way we do not know what it is, and guessing "ephemeral"
            # deletes saved work. REMEDY: `StorageProvider.unclassified()`
            # lists these so an operator can re-`put` them with a class or
            # delete them deliberately; new writes get a class at `put` time.
            return SweepDecision(False, "unclassified: no retention class recorded")

        if retention is RetentionClass.ARCHIVE:
            # Licence receipts and audit records. They outlive the project on
            # purpose and have a legal reason to exist, so no age-based rule may
            # remove them. Erasure requests still delete them, through
            # `delete_project`, which is a deliberate act rather than a sweep.
            return SweepDecision(False, "archive: exempt from age-based sweeps")

        ceiling = self.plan_ceiling_seconds
        if ceiling is not None and age_seconds >= ceiling:
            # `Plan.max_retention_days` governs bytes. It was previously read by
            # nothing at all, so a free tenant's objects lived exactly as long
            # as an enterprise tenant's. The row-side rule (`expiry_for`) sets
            # the same ceiling on `expires_at`; this is the backstop for objects
            # whose row never got one.
            return SweepDecision(True, "past the plan's retention ceiling")

        project_id = project_of_key(key)
        if (
            project_id is not None
            and self.live_project_ids is not None
            and project_id not in self.live_project_ids
            and age_seconds >= self.ephemeral_window_seconds
        ):
            # The orphan case, and the only one that may delete a `PROJECT`
            # object: its project row is gone, so nothing will ever ask for
            # these bytes again. Age still matters — a freshly written object
            # belongs to a render whose project has not been saved yet.
            return SweepDecision(True, "orphan: no project row owns these bytes")

        if (
            retention is RetentionClass.EPHEMERAL
            and age_seconds >= self.ephemeral_window_seconds
        ):
            return SweepDecision(True, "ephemeral and past its window")

        # Everything else: a `PROJECT` object of a project that still exists, or
        # an object younger than its window. This is the branch the old code did
        # not have.
        return SweepDecision(False, "live project bytes, or not yet past its window")


def retention_for(tier: PlanTier | str | None) -> timedelta:
    """How long this plan may keep content.

    The ceiling, not the default. A project can ask for less (a temporary
    project expires in hours); it cannot ask for more, because storage that
    outlives the plan paying for it is unbounded cost with no revenue.
    """
    plan = _plan_for(tier)
    return timedelta(days=plan.max_retention_days)


def expiry_for(
    project: Project,
    *,
    tier: PlanTier | str | None,
    temporary_hours: int,
    now: datetime | None = None,
) -> datetime | None:
    """When this project's content must be gone by.

    `None` means "no expiry from this rule" and is reachable only for a
    persistent project on a plan whose ceiling has not been hit — the ceiling
    always wins, which is the whole point of it being a plan ceiling.
    """
    moment = now or utc_now()
    ceiling = moment + retention_for(tier)

    if project.persistence is PersistenceMode.TEMPORARY:
        # The shorter of the two. A free-tier temporary project expires in
        # hours, not in the plan's seven days.
        return min(moment + timedelta(hours=temporary_hours), ceiling)
    return ceiling


@dataclass
class RetentionService:
    """Deletes projects completely, and on schedule.

    Holds the repository and storage rather than reaching for a global, so a
    test can drive it against temporary files and the worker and the API can
    share exactly one implementation. Before this there were two — one in
    `jobs.run_retention_sweep` and one inline in the API's sweep route — and
    only the API's applied a tenant scope.
    """

    repository: Any
    storage: Any
    #: Fallback window for a project with no plan on record.
    temporary_hours: int = 24
    #: Supplies `tier_of(organisation_id)`. Without it the plan ceiling on bytes
    #: cannot be resolved and is therefore not applied — see
    #: `_plan_ceiling_seconds`, which says what to pass and from where.
    directory: Any | None = None
    #: How many live projects one tenant may have before the orphan sweep stops
    #: trusting its own view of them. See `_live_project_ids`.
    live_project_limit: int = 10_000
    #: Names the tenant of every operation, so an unscoped call is impossible to
    #: write by accident rather than merely discouraged.
    _seen: set[str] = field(default_factory=set, repr=False)

    async def delete_project(
        self, *, organisation_id: str, project_id: str
    ) -> DeletionReport:
        """Remove one project's bytes and then its records.

        Scoped to a tenant in both halves. The storage prefix contains the
        organisation id, and the repository delete carries it as a query
        parameter, so a caller holding the wrong tenant's identifier removes
        nothing rather than removing something.
        """
        existing = await self.repository.get_project(
            project_id, organisation_id=organisation_id
        )
        if existing is None:
            # Still sweep the prefix. A row can be gone while its objects
            # remain — that is exactly the state an interrupted deletion
            # leaves, and refusing to clean it up would make the interruption
            # permanent.
            removed = await self.storage.delete_prefix(
                project_prefix(organisation_id, project_id)
            )
            return DeletionReport(
                project_id=project_id,
                objects_deleted=len(removed),
                already_absent=True,
            )

        removed = await self.storage.delete_prefix(
            project_prefix(organisation_id, project_id)
        )
        await self.repository.delete_project(
            project_id, organisation_id=organisation_id
        )
        return DeletionReport(
            project_id=project_id,
            objects_deleted=len(removed),
            records_deleted=1,
        )

    async def sweep(
        self,
        *,
        organisation_id: str,
        now: datetime | None = None,
    ) -> list[DeletionReport]:
        """Delete everything of this tenant's that has passed its retention.

        Deliberately tenant-scoped with no "all tenants" option. The
        cross-tenant form is a loop in the worker over organisations the worker
        enumerates itself; making it reachable from here is how an endpoint
        ends up deleting every customer's data, which is the defect the audit
        found in `/internal/sweep`.
        """
        expired = await self.repository.expired_projects(
            now=now or utc_now(), organisation_id=organisation_id
        )
        reports: list[DeletionReport] = []
        for project_id in expired:
            reports.append(
                await self.delete_project(
                    organisation_id=organisation_id, project_id=project_id
                )
            )
        return reports

    async def sweep_orphans(
        self, *, organisation_id: str, older_than_seconds: float
    ) -> list[str]:
        """Remove objects with no project left to own them.

        The second half of "interrupted deletion is recoverable": objects whose
        row is gone are found here, by age, inside this tenant's namespace
        only. Age matters — a freshly written object belongs to a render that
        has not yet saved its project.

        **This used to delete a live project's assets.** It passed an age and a
        tenant prefix to a filesystem walk that knew nothing about retention
        classes or project rows, so every object older than the window went:
        `PROJECT` assets, captions, renders belonging to saved projects on a
        plan promising ten years. It now builds a `SweepPolicy` from the
        database and the plan, and the storage adapter cannot sweep without one.
        """
        storage = self._sweepable_storage()
        policy = SweepPolicy(
            ephemeral_window_seconds=older_than_seconds,
            plan_ceiling_seconds=self._plan_ceiling_seconds(organisation_id),
            live_project_ids=await self._live_project_ids(organisation_id),
        )
        return list(
            await storage.sweep_expired(
                policy=policy, prefix=tenant_prefix(organisation_id)
            )
        )

    async def unclassified_objects(self, *, organisation_id: str) -> list[str]:
        """Objects the sweep refuses to touch because their class is unknown.

        The remedy the sweep's refusal points at. Without this, "fail closed on
        an unrecorded retention class" would mean objects accumulating silently
        forever with no way to find them; with it, an operator has a list and
        can re-`put` them with a class or delete them deliberately.
        """
        storage = self._sweepable_storage()
        return list(
            await storage.unclassified(prefix=tenant_prefix(organisation_id))
        )

    # -- policy inputs ----------------------------------------------------

    def _sweepable_storage(self) -> Any:
        """The storage provider, if it can answer "what class is this object?".

        A backend that cannot report retention classes must not be swept by
        age: that is precisely the operation that deleted saved projects. So
        this refuses rather than degrading to the old behaviour, and the
        message names what to implement.
        """
        storage = self.storage
        required = ("sweep_expired", "retention_of", "unclassified")
        if not all(hasattr(storage, name) for name in required):
            raise VTVError(
                "this storage provider cannot be swept safely: it does not "
                "implement SweepableStorage (retention_of/sweep_expired/"
                "unclassified). Implement it — for S3 that is GetObjectTagging "
                "against RETENTION_TAGS — or express retention as a bucket "
                "lifecycle rule and do not call the sweep at all",
                code=ErrorCode.STORAGE_UNAVAILABLE,
            )
        return storage

    def _plan_ceiling_seconds(self, organisation_id: str) -> float | None:
        """The plan's byte ceiling for this tenant, or `None` if unknowable.

        `None` disables ceiling-based deletion. That is the fail-closed answer
        for a *destructive* rule: an absent directory means we cannot tell an
        enterprise tenant from a free one, and assuming the most restrictive
        plan would delete ten years of retention after seven days. REMEDY: pass
        `directory=` (the `Directory` from the assembly, which has `tier_of`)
        when constructing this service — `jobs.run_retention_sweep` and the
        API's sweep route are the two places that build one.
        """
        if self.directory is None:
            return None
        return retention_for(self.directory.tier_of(organisation_id)).total_seconds()

    async def _live_project_ids(self, organisation_id: str) -> frozenset[str] | None:
        """Which of this tenant's projects still exist.

        `None` means "I could not enumerate them", which disables orphan
        deletion. That case is reachable: `list_projects` is paginated, and a
        tenant with more projects than `live_project_limit` would hand back a
        partial set in which the projects that fell off the page look exactly
        like orphans. Deleting on a partial answer is how a sweep removes a
        live customer's data, so a full page is treated as no answer.

        TRADEOFF ACCEPTED: the live set is O(projects per tenant), held in
        memory for the length of one sweep. At the current shape of the
        product — thousands of projects for a large tenant, a few hundred bytes
        each — that is a megabyte and one query. WHAT WOULD CHANGE MY MIND: a
        tenant regularly exceeding `live_project_limit`. At that point the
        membership test belongs in the store that owns the answer — an
        anti-join in SQL, or S3 Inventory joined against the projects table —
        rather than in this process's memory.
        """
        projects = await self.repository.list_projects(
            organisation_id=organisation_id, limit=self.live_project_limit
        )
        if len(projects) >= self.live_project_limit:
            return None
        return frozenset(project.project_id for project in projects)


def _plan_for(tier: PlanTier | str | None) -> Plan:
    if isinstance(tier, PlanTier):
        return PLANS[tier]
    if isinstance(tier, str):
        try:
            return PLANS[PlanTier(tier)]
        except ValueError:
            pass
    # Unknown or absent: the most restrictive plan. Guessing generously here
    # would mean an unrecognised tier keeps data the longest, which is exactly
    # backwards.
    return PLANS[PlanTier.FREE]


__all__ = [
    "DeletionReport",
    "RetentionService",
    "SweepDecision",
    "SweepPolicy",
    "expiry_for",
    "project_of_key",
    "retention_for",
]
