"""Stage 26, continued — the quotas that were declared but not enforced.

`tests/test_billing.py` covers the meter itself and the spend breaker. This
module covers the three claims an earlier pass on this codebase left half true:

* `QuotaKind.GENERATED_ASSETS` was counted only when a render job settled —
  after every asset that job bought — so nothing before settlement could tell
  the difference between an image bought and one about to be bought.
* `QuotaKind.SEATS` was declared with a per-tier limit and consulted by
  nothing: `Directory.add_member` inserted a row regardless of the plan.
* `QuotaKind.is_enforced` is a promise the billing page repeats verbatim
  (`/v1/usage`'s ``enforced`` flag). Nothing checked that the promise and the
  set of quotas actually gated agreed with each other.

Every test class below is written to fail against the code as it stood before
the fix in this change: the asset and seat tests by exercising the gate that
did not exist, and the declaration test by asserting the closed-world property
`billing/plans.py` claims but had never been checked in a test.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.billing.plans import PLANS, QuotaKind
from vtv.billing.usage import (
    GeneratedAssetAllowance,
    GenerationNotIncluded,
    QuotaExceeded,
    UsageMeter,
)
from vtv.config import Settings
from vtv.contracts.base import Budget, IdPrefix, new_id, utc_now
from vtv.contracts.errors import Status
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    GenerationResult,
    ImageParams,
)
from vtv.contracts.tenancy import Membership, Organisation, PlanTier, Role, User
from vtv.observability.events import EventSink
from vtv.pipeline.generation import GenerationRouter
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth
from vtv.security.directory import Directory
from vtv.wiring import PlanSeatAuthoriser, build


def org_id() -> str:
    return new_id(IdPrefix.PROJECT)


class EveryQuotaDeclaresWhetherItIsEnforced(unittest.TestCase):
    """The property `/v1/usage` trusts, checked rather than assumed.

    `billing/plans.py` documents this as a closed world: a `QuotaKind` is
    either in `_ENFORCED` or explains itself in `_UNENFORCED`, never both,
    never neither. Nothing asserted that until now — the docstring on
    `QuotaKind.is_enforced` says a test does, and it did not exist.
    """

    def test_every_kind_is_enforced_xor_explains_why_it_is_not(self) -> None:
        for kind in QuotaKind:
            with self.subTest(kind=kind):
                if kind.is_enforced:
                    self.assertIsNone(
                        kind.enforcement_note,
                        f"{kind.value} claims enforcement and still carries a "
                        "note explaining why it does not",
                    )
                else:
                    self.assertIsNotNone(
                        kind.enforcement_note,
                        f"{kind.value} reports enforced: false with no "
                        "explanation a customer could act on",
                    )

    def test_generated_assets_and_seats_are_now_enforced(self) -> None:
        """The two quotas this change wires. A regression here is a silent
        return to `/v1/usage` reporting a limit nothing applies."""
        self.assertTrue(QuotaKind.GENERATED_ASSETS.is_enforced)
        self.assertTrue(QuotaKind.SEATS.is_enforced)


class GeneratedAssetsAreAGateNotOnlyAMeter(unittest.TestCase):
    """`GENERATED_ASSETS` now stops generation instead of only describing it.

    Mirrors `tests.test_billing.ProviderSpendIsABreakerNotAMeter`: the quota is
    a hard ceiling per `billing/plans.py`, it was recorded once per job at
    settlement and checked nowhere, and these tests are about the two halves
    of making it real — the refusal, and the in-flight accounting without
    which the refusal only arrives after the loop already bought the assets.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-assets-")
        self.org = org_id()
        self.tier = PlanTier.STARTER  # 200 generated assets/month
        self.meter = UsageMeter(
            path=Path(self._dir.name) / "usage.db",
            tier_of=lambda _org: self.tier,
        )
        self.authoriser = GeneratedAssetAllowance(meter=self.meter)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def authorise(self) -> None:
        self.authoriser.authorise(organisation_id=self.org)

    def test_an_asset_inside_the_allowance_is_permitted(self) -> None:
        self.authorise()  # raises if refused

    def test_the_free_plan_refuses_every_generated_asset(self) -> None:
        """Not an error path — the Free plan buys zero generated visuals, and
        the caller is expected to descend to a drawn visual instead."""
        self.tier = PlanTier.FREE
        with self.assertRaises(GenerationNotIncluded):
            self.authorise()

    def test_a_paid_plan_past_its_allowance_is_refused_with_a_remedy(self) -> None:
        limit = PLANS[PlanTier.STARTER].limit(QuotaKind.GENERATED_ASSETS)
        self.meter.record(
            organisation_id=self.org, kind=QuotaKind.GENERATED_ASSETS, quantity=limit
        )
        with self.assertRaises(QuotaExceeded) as caught:
            self.authorise()
        message = caught.exception.info.user_message or ""
        self.assertIn("Upgrade", message)

    def test_assets_still_in_flight_count_before_any_job_settles(self) -> None:
        """The runaway case. Nothing is written to the meter until the job
        finishes, so a check against the meter alone authorises a loop
        indefinitely."""
        limit = PLANS[PlanTier.STARTER].limit(QuotaKind.GENERATED_ASSETS)
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.GENERATED_ASSETS,
            quantity=limit - 1,
        )
        self.authorise()  # the last one the committed figure allows
        self.authoriser.note_generated(organisation_id=self.org)
        with self.assertRaises(QuotaExceeded):
            self.authorise()

    def test_settled_assets_are_not_counted_a_second_time(self) -> None:
        """Otherwise the tally and the meter both hold the same asset and the
        tenant is cut off at half their allowance."""
        self.authoriser.note_generated(organisation_id=self.org)
        self.meter.record(
            organisation_id=self.org, kind=QuotaKind.GENERATED_ASSETS, quantity=1.0
        )
        self.authorise()  # 1 committed + 0 in flight + 1 requested <= 200

    def test_one_tenants_in_flight_assets_do_not_block_another(self) -> None:
        other = org_id()
        for _ in range(5):
            self.authoriser.note_generated(organisation_id=self.org)
        self.authoriser.authorise(organisation_id=other)

    def test_a_runaway_loop_stops_partway_through_a_single_job(self) -> None:
        """End to end, through the router: the thing plans.py promised.

        Before this change the loop below ran to completion — nothing before
        settlement could refuse an asset — and the count only showed up on
        `/v1/usage` afterwards.
        """
        limit = PLANS[PlanTier.STARTER].limit(QuotaKind.GENERATED_ASSETS)
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.GENERATED_ASSETS,
            quantity=limit - 1,
        )
        router = GenerationRouter(events=EventSink(), asset_authoriser=self.authoriser)
        provider = _CheapImageProvider()
        router.register(provider, GenerationKind.IMAGE)

        with self.assertRaises(QuotaExceeded):
            for index in range(50):
                asyncio.run(
                    router.generate(
                        GenerationRequest(
                            organisation_id=self.org,
                            kind=GenerationKind.IMAGE,
                            # A distinct prompt each time: a cached result is
                            # free and would not exercise the gate.
                            params=ImageParams(prompt=f"frame {index}"),
                            budget=Budget(max_cost_usd=0.5),
                        )
                    )
                )
        self.assertEqual(provider.calls, 1, "stopped after the first call, not 50")


class _CheapImageProvider:
    """Charges a cent an image — cheap enough that only the asset gate, never
    the spend breaker, is what stops the loop in the test above."""

    calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="cheap-image",
            unit_cost_usd=0.01,
            typical_latency_seconds=0.01,
            data_policy=DataPolicy(
                retains_input=False, trains_on_input=False, dpa_in_place=True
            ),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth()

    async def generate_image(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        return GenerationResult(
            request_id=request.request_id,
            cache_key=request.cache_key(),
            status=Status.READY,
            provider="cheap-image",
            model="test",
            structured_output={"ok": True},
            cost_usd=0.01,
            latency_ms=1,
        )


class DirectoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-directory-")
        self.directory = Directory(path=Path(self._dir.name) / "directory.db")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def make_organisation(self, tier: PlanTier) -> Organisation:
        slug = "org-" + new_id(IdPrefix.PROJECT)[-12:].lower()
        return self.directory.create_organisation(
            Organisation(name="Acme", slug=slug, plan=tier)
        )

    def make_user(self, email: str) -> User:
        return self.directory.create_user(User(email=email, sso_subject=f"sso|{email}"))


class SeatsAreEnforcedWhenAMemberIsAdded(DirectoryTestCase):
    """`SEATS` used to be a number on a plan and nothing else.

    `Directory.add_member` inserted a row regardless of how many members an
    organisation already had. `PlanSeatAuthoriser` is what `wiring.build`
    attaches so the same call refuses instead.
    """

    def setUp(self) -> None:
        super().setUp()
        self.directory.seat_authoriser = PlanSeatAuthoriser(
            tier_of=self.directory.tier_of
        )

    def test_the_first_member_of_a_free_organisation_is_seated(self) -> None:
        organisation = self.make_organisation(PlanTier.FREE)
        owner = self.make_user("owner@example.com")
        self.directory.add_member(
            Membership(
                user_id=owner.user_id,
                organisation_id=organisation.organisation_id,
                role=Role.OWNER,
                accepted_at=utc_now(),
            )
        )
        self.assertEqual(len(self.directory.members(organisation.organisation_id)), 1)

    def test_a_second_member_of_a_free_organisation_is_refused(self) -> None:
        """FREE allows exactly one seat — see `PLANS[PlanTier.FREE]`."""
        organisation = self.make_organisation(PlanTier.FREE)
        owner = self.make_user("owner@example.com")
        self.directory.add_member(
            Membership(
                user_id=owner.user_id,
                organisation_id=organisation.organisation_id,
                role=Role.OWNER,
                accepted_at=utc_now(),
            )
        )
        second = self.make_user("second@example.com")
        with self.assertRaises(QuotaExceeded):
            self.directory.add_member(
                Membership(
                    user_id=second.user_id,
                    organisation_id=organisation.organisation_id,
                    role=Role.EDITOR,
                    accepted_at=utc_now(),
                )
            )
        self.assertEqual(len(self.directory.members(organisation.organisation_id)), 1)

    def test_a_starter_organisation_seats_up_to_its_plan(self) -> None:
        """STARTER allows three — the boundary is exercised, not just zero."""
        organisation = self.make_organisation(PlanTier.STARTER)
        for index in range(3):
            user = self.make_user(f"member{index}@example.com")
            self.directory.add_member(
                Membership(
                    user_id=user.user_id,
                    organisation_id=organisation.organisation_id,
                    role=Role.EDITOR,
                    accepted_at=utc_now(),
                )
            )
        fourth = self.make_user("fourth@example.com")
        with self.assertRaises(QuotaExceeded):
            self.directory.add_member(
                Membership(
                    user_id=fourth.user_id,
                    organisation_id=organisation.organisation_id,
                    role=Role.EDITOR,
                    accepted_at=utc_now(),
                )
            )

    def test_updating_an_existing_members_role_does_not_consume_a_seat(self) -> None:
        """A role change is not a new head. Refusing it because the plan is
        full at capacity would enforce a headcount quota against a request
        that adds no head."""
        organisation = self.make_organisation(PlanTier.FREE)
        owner = self.make_user("owner@example.com")
        self.directory.add_member(
            Membership(
                user_id=owner.user_id,
                organisation_id=organisation.organisation_id,
                role=Role.OWNER,
                accepted_at=utc_now(),
            )
        )
        updated = self.directory.add_member(
            Membership(
                user_id=owner.user_id,
                organisation_id=organisation.organisation_id,
                role=Role.ADMIN,
                accepted_at=utc_now(),
            )
        )
        self.assertEqual(updated.role, Role.ADMIN)

    def test_the_refusal_names_the_remedy(self) -> None:
        organisation = self.make_organisation(PlanTier.FREE)
        owner = self.make_user("owner@example.com")
        self.directory.add_member(
            Membership(
                user_id=owner.user_id,
                organisation_id=organisation.organisation_id,
                role=Role.OWNER,
                accepted_at=utc_now(),
            )
        )
        second = self.make_user("second@example.com")
        with self.assertRaises(QuotaExceeded) as caught:
            self.directory.add_member(
                Membership(
                    user_id=second.user_id,
                    organisation_id=organisation.organisation_id,
                    role=Role.EDITOR,
                    accepted_at=utc_now(),
                )
            )
        message = caught.exception.info.user_message or ""
        self.assertIn("Upgrade", message)


class ADirectoryWithNoSeatAuthoriserDoesNotEnforce(DirectoryTestCase):
    """`Directory` is constructed directly by `bootstrap`, by the CLI and by
    most tests — none of which supply a plan to check against. The absence of
    a `seat_authoriser` must not raise; it must simply not gate, exactly as an
    absent `spend_authoriser` does not gate `GenerationRouter`."""

    def test_no_authoriser_means_no_limit(self) -> None:
        self.assertIsNone(self.directory.seat_authoriser)
        organisation = self.make_organisation(PlanTier.FREE)
        for index in range(5):
            user = self.make_user(f"member{index}@example.com")
            self.directory.add_member(
                Membership(
                    user_id=user.user_id,
                    organisation_id=organisation.organisation_id,
                    role=Role.EDITOR,
                    accepted_at=utc_now(),
                )
            )
        self.assertEqual(len(self.directory.members(organisation.organisation_id)), 5)


class TheAssemblyCannotForgetEitherGate(unittest.TestCase):
    """Optional on the type, mandatory in any real deployment — checked the
    same way `tests.test_billing.TheAssemblyCannotForgetTheBreaker` checks the
    spend breaker, so "the assembly forgot" fails the build."""

    def test_the_wired_router_authorises_generated_assets(self) -> None:
        with TemporaryDirectory(prefix="vtv-assembly-assets-") as directory:
            root = Path(directory)
            assembly = build(
                Settings(asset_search_endpoint="", 
                    storage_root=root / "storage",
                    database_url=f"sqlite:///{root / 'vtv.db'}",
                    env="development",
                )
            )
            authoriser = assembly.router.asset_authoriser
            self.assertIsInstance(authoriser, GeneratedAssetAllowance)
            self.assertIs(authoriser.meter, assembly.usage)

    def test_the_wired_directory_authorises_seats(self) -> None:
        with TemporaryDirectory(prefix="vtv-assembly-seats-") as directory:
            root = Path(directory)
            assembly = build(
                Settings(asset_search_endpoint="", 
                    storage_root=root / "storage",
                    database_url=f"sqlite:///{root / 'vtv.db'}",
                    env="development",
                )
            )
            self.assertIsInstance(assembly.directory.seat_authoriser, PlanSeatAuthoriser)
            self.assertEqual(
                assembly.directory.seat_authoriser.tier_of("nonexistent-org"),
                PlanTier.FREE,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
