"""Stage 26 — usage metering and quota enforcement.

Billing bugs are the expensive kind: they are discovered by the customer, they
are discovered late, and they cost trust rather than uptime. So these tests are
about the specific ways a meter goes wrong — double billing a retry, letting
concurrency slip past a quota, charging an estimate instead of a measurement.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.billing.plans import PLANS, UNLIMITED, Plan, QuotaKind, plan_for
from vtv.billing.usage import (
    QuotaExceeded,
    UsageMeter,
    UsagePeriod,
    invoice_lines,
    periods_between,
)
from vtv.config import Settings
from vtv.contracts.base import Budget, IdPrefix, new_id
from vtv.contracts.errors import Status
from vtv.contracts.generation import (
    GenerationKind,
    GenerationRequest,
    GenerationResult,
    ImageParams,
)
from vtv.contracts.tenancy import PlanTier
from vtv.observability.events import EventSink
from vtv.pipeline.generation import GenerationRouter
from vtv.ports.base import DataPolicy, ProviderCapabilities, ProviderHealth
from vtv.wiring import MeteredSpendAuthoriser, build


def org_id() -> str:
    return new_id(IdPrefix.PROJECT)


class Plans(unittest.TestCase):
    def test_every_tier_has_a_plan(self) -> None:
        for tier in PlanTier:
            with self.subTest(tier=tier):
                self.assertIsInstance(plan_for(tier), Plan)

    def test_an_unknown_tier_gets_the_most_restrictive_plan(self) -> None:
        """Fail closed: an unrecognised tier is a data error, not an upgrade."""
        self.assertIs(plan_for("platinum").tier, PlanTier.FREE)  # type: ignore[arg-type]

    def test_allowances_never_decrease_as_the_price_rises(self) -> None:
        """A pricing table that inverts is a bug nobody notices until renewal."""
        order = [PlanTier.FREE, PlanTier.STARTER, PlanTier.PROFESSIONAL,
                 PlanTier.BUSINESS, PlanTier.ENTERPRISE]
        for kind in QuotaKind:
            values = [PLANS[tier].limit(kind) for tier in order]
            with self.subTest(kind=kind):
                self.assertEqual(values, sorted(values), f"{kind.value} inverts")

    def test_the_free_plan_cannot_spend_on_generation(self) -> None:
        """An unpriced tier that can call a paid provider is an open tap."""
        free = PLANS[PlanTier.FREE]
        self.assertEqual(free.limit(QuotaKind.GENERATED_ASSETS), 0.0)
        self.assertLessEqual(free.limit(QuotaKind.PROVIDER_SPEND_USD), 5.0)

    def test_every_plan_bounds_provider_spend(self) -> None:
        """The circuit breaker between a runaway loop and a runaway bill."""
        for tier, plan in PLANS.items():
            with self.subTest(tier=tier):
                self.assertLess(plan.limit(QuotaKind.PROVIDER_SPEND_USD), UNLIMITED)

    def test_features_are_fields_not_tier_comparisons(self) -> None:
        """So "give this one customer SSO" is a field change, not a code change."""
        self.assertFalse(PLANS[PlanTier.FREE].sso)
        self.assertTrue(PLANS[PlanTier.BUSINESS].sso)
        self.assertTrue(PLANS[PlanTier.ENTERPRISE].data_residency_choice)
        self.assertFalse(PLANS[PlanTier.PROFESSIONAL].data_residency_choice)

    def test_retention_is_bounded_on_every_plan(self) -> None:
        for tier, plan in PLANS.items():
            with self.subTest(tier=tier):
                self.assertGreater(plan.max_retention_days, 0)
                self.assertLessEqual(plan.max_retention_days, 3650)


class Periods(unittest.TestCase):
    def test_a_period_is_a_calendar_month(self) -> None:
        period = UsagePeriod(2026, 3)
        self.assertEqual(period.key, "2026-03")
        self.assertTrue(period.contains(datetime(2026, 3, 31, 23, 59, tzinfo=UTC)))
        self.assertFalse(period.contains(datetime(2026, 4, 1, tzinfo=UTC)))

    def test_december_rolls_into_january(self) -> None:
        self.assertEqual(UsagePeriod(2026, 12).next().key, "2027-01")

    def test_periods_between_is_inclusive(self) -> None:
        keys = [
            period.key
            for period in periods_between(
                datetime(2026, 1, 15).date(), datetime(2026, 4, 2).date()
            )
        ]
        self.assertEqual(keys, ["2026-01", "2026-02", "2026-03", "2026-04"])


class MeterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-billing-")
        self.org = org_id()
        self.tier = PlanTier.STARTER
        self.meter = UsageMeter(
            path=Path(self._dir.name) / "usage.db",
            tier_of=lambda _org: self.tier,
        )

    def tearDown(self) -> None:
        self._dir.cleanup()


class Metering(MeterTestCase):
    def test_usage_accumulates_within_a_period(self) -> None:
        for minutes in (1.5, 2.5, 3.0):
            self.meter.record(
                organisation_id=self.org,
                kind=QuotaKind.RENDERED_MINUTES,
                quantity=minutes,
            )
        self.assertAlmostEqual(
            self.meter.total(
                organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES
            ),
            7.0,
        )

    def test_a_retried_job_is_billed_once(self) -> None:
        """The single most common billing bug, and it is enforced in the schema."""
        for _ in range(3):
            self.meter.record(
                organisation_id=self.org,
                kind=QuotaKind.RENDERED_MINUTES,
                quantity=4.0,
                idempotency_key="render:prj_abc",
            )
        self.assertEqual(
            self.meter.total(
                organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES
            ),
            4.0,
        )

    def test_a_duplicate_is_not_an_error(self) -> None:
        """A retried worker calls this twice legitimately; it must not crash."""
        first = self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.DOCUMENTS,
            quantity=1,
            idempotency_key="doc:1",
        )
        second = self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.DOCUMENTS,
            quantity=1,
            idempotency_key="doc:1",
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_one_tenants_usage_is_invisible_to_another(self) -> None:
        other = org_id()
        self.meter.record(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, quantity=50.0
        )
        self.assertEqual(
            self.meter.total(
                organisation_id=other, kind=QuotaKind.RENDERED_MINUTES
            ),
            0.0,
        )

    def test_usage_does_not_leak_across_periods(self) -> None:
        march = datetime(2026, 3, 10, tzinfo=UTC)
        april = datetime(2026, 4, 10, tzinfo=UTC)
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=100.0,
            at=march,
        )
        self.assertEqual(
            self.meter.total(
                organisation_id=self.org,
                kind=QuotaKind.RENDERED_MINUTES,
                period=UsagePeriod.containing(april),
            ),
            0.0,
        )


class Quotas(MeterTestCase):
    def test_a_request_within_the_allowance_is_allowed(self) -> None:
        verdict = self.meter.check(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, requested=5.0
        )
        self.assertTrue(verdict.allowed)
        self.assertFalse(verdict.overage)

    def test_a_request_past_the_allowance_is_refused_before_spending(self) -> None:
        self.tier = PlanTier.FREE
        self.meter.record(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, quantity=9.5
        )
        verdict = self.meter.check(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, requested=5.0
        )
        self.assertFalse(verdict.allowed)
        with self.assertRaises(QuotaExceeded):
            verdict.raise_if_denied()

    def test_a_plan_with_overage_bills_instead_of_blocking(self) -> None:
        self.tier = PlanTier.PROFESSIONAL
        plan = PLANS[PlanTier.PROFESSIONAL]
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=plan.limit(QuotaKind.RENDERED_MINUTES),
        )
        verdict = self.meter.check(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, requested=10.0
        )
        self.assertTrue(verdict.allowed)
        self.assertTrue(verdict.overage)
        self.assertAlmostEqual(
            verdict.overage_cost_usd,
            10.0 * plan.overage_price_per_minute_usd,
            places=4,
        )

    def test_overage_does_not_apply_to_quotas_that_are_not_billable(self) -> None:
        """Seats and spend are hard ceilings; only rendered minutes bill over."""
        self.tier = PlanTier.PROFESSIONAL
        plan = PLANS[PlanTier.PROFESSIONAL]
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.PROVIDER_SPEND_USD,
            quantity=plan.limit(QuotaKind.PROVIDER_SPEND_USD),
        )
        verdict = self.meter.check(
            organisation_id=self.org,
            kind=QuotaKind.PROVIDER_SPEND_USD,
            requested=1.0,
        )
        self.assertFalse(verdict.allowed)

    def test_the_verdict_says_how_much_is_left(self) -> None:
        self.tier = PlanTier.FREE
        self.meter.record(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, quantity=4.0
        )
        verdict = self.meter.check(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, requested=1.0
        )
        self.assertAlmostEqual(verdict.remaining, 6.0)


class Reservations(MeterTestCase):
    def test_concurrent_requests_cannot_overshoot_a_quota(self) -> None:
        """Ten requests each passing the same check is how a free plan renders
        a hundred minutes. A reservation holds the allowance in the window."""
        self.tier = PlanTier.FREE  # 10 rendered minutes
        held = []
        for _ in range(10):
            verdict, reservation = self.meter.reserve(
                organisation_id=self.org,
                kind=QuotaKind.RENDERED_MINUTES,
                quantity=3.0,
            )
            if verdict.allowed:
                held.append(reservation)
        # 3 × 3 = 9 fits; the fourth would make 12 against a limit of 10.
        self.assertEqual(len(held), 3)

    def test_settling_bills_the_measured_figure_not_the_estimate(self) -> None:
        _verdict, reservation = self.meter.reserve(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=10.0,
        )
        assert reservation is not None
        record = self.meter.settle(reservation, actual=4.2, cost_usd=0.31)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertAlmostEqual(record.quantity, 4.2)
        self.assertAlmostEqual(
            self.meter.total(
                organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES
            ),
            4.2,
        )

    def test_releasing_a_failed_job_costs_the_tenant_nothing(self) -> None:
        _verdict, reservation = self.meter.reserve(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=20.0,
        )
        assert reservation is not None
        self.meter.release(reservation)
        self.assertEqual(
            self.meter.total(
                organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES
            ),
            0.0,
        )
        # And the allowance is available again.
        verdict = self.meter.check(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, requested=20.0
        )
        self.assertTrue(verdict.allowed)

    def test_an_abandoned_reservation_expires(self) -> None:
        """A crashed worker costs an hour of headroom, not a month of it."""
        self.meter.reserve(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=5.0,
        )
        later = datetime.now(UTC) + timedelta(seconds=7200)
        self.assertEqual(self.meter.expire_reservations(now=later), 1)

    def test_settling_an_unknown_reservation_is_a_no_op(self) -> None:
        self.assertIsNone(self.meter.settle("res_nope", actual=1.0))


class Reporting(MeterTestCase):
    def test_a_summary_reports_use_limit_and_remaining(self) -> None:
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=20.0,
            cost_usd=1.25,
        )
        summary = self.meter.summary(organisation_id=self.org)
        payload = summary.as_dict()
        rendered = payload["usage"]["rendered_minutes"]  # type: ignore[index]
        self.assertEqual(rendered["used"], 20.0)
        self.assertEqual(rendered["limit"], 120.0)
        self.assertEqual(rendered["remaining"], 100.0)

    def test_margin_is_revenue_minus_what_the_providers_charged(self) -> None:
        """A billing system that meters revenue and not cost cannot find an
        unprofitable customer."""
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=100.0,
            cost_usd=12.0,
        )
        summary = self.meter.summary(organisation_id=self.org)
        self.assertAlmostEqual(summary.gross_margin_usd, 29.0 - 12.0, places=4)

    def test_a_loss_making_customer_is_visible(self) -> None:
        self.tier = PlanTier.FREE
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=10.0,
            cost_usd=4.0,
        )
        self.assertLess(
            self.meter.summary(organisation_id=self.org).gross_margin_usd, 0.0
        )

    def test_line_items_reconcile_to_the_summary(self) -> None:
        self.tier = PlanTier.PROFESSIONAL
        plan = PLANS[PlanTier.PROFESSIONAL]
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.RENDERED_MINUTES,
            quantity=plan.limit(QuotaKind.RENDERED_MINUTES) + 50.0,
        )
        lines = invoice_lines(self.meter.summary(organisation_id=self.org))
        self.assertEqual(len(lines), 2)
        self.assertAlmostEqual(
            float(lines[1]["amount_usd"]),  # type: ignore[arg-type]
            round(50.0 * plan.overage_price_per_minute_usd, 2),
            places=2,
        )

    def test_no_overage_line_when_within_the_allowance(self) -> None:
        self.meter.record(
            organisation_id=self.org, kind=QuotaKind.RENDERED_MINUTES, quantity=5.0
        )
        self.assertEqual(len(invoice_lines(self.meter.summary(organisation_id=self.org))), 1)

    def test_records_are_readable_as_line_items(self) -> None:
        """A customer disputing an invoice needs the detail, not the total."""
        for index in range(3):
            self.meter.record(
                organisation_id=self.org,
                kind=QuotaKind.RENDERED_MINUTES,
                quantity=1.0,
                project_id=new_id(IdPrefix.PROJECT),
                note=f"render {index}",
            )
        records = self.meter.records(organisation_id=self.org)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record.project_id for record in records))


class ProviderSpendIsABreakerNotAMeter(unittest.TestCase):
    """`PROVIDER_SPEND_USD` now stops spending instead of describing it.

    The quota is documented in plans.py as "the circuit breaker that stops a
    runaway loop becoming a runaway bill". It was recorded once per job, at
    settlement, and checked nowhere — a grep for a check against it found no
    caller in src/. These tests are about the two halves of making it real: the
    refusal, and the in-flight accounting without which the refusal only ever
    arrives after the loop has finished spending.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-spend-")
        self.org = org_id()
        # FREE: a 2 USD monthly ceiling, small enough to reach in a test.
        self.meter = UsageMeter(
            path=Path(self._dir.name) / "usage.db",
            tier_of=lambda _org: PlanTier.FREE,
        )
        self.authoriser = MeteredSpendAuthoriser(meter=self.meter)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def authorise(self, amount: float) -> None:
        self.authoriser.authorise(organisation_id=self.org, amount_usd=amount)

    def test_spend_inside_the_allowance_is_permitted(self) -> None:
        self.authorise(0.5)  # raises if refused

    def test_spend_past_the_allowance_is_refused(self) -> None:
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.PROVIDER_SPEND_USD,
            quantity=1.9,
        )
        with self.assertRaises(QuotaExceeded):
            self.authorise(0.5)

    def test_the_refusal_names_the_remedy(self) -> None:
        """A refusal with no next step is a dead end for the person reading it."""
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.PROVIDER_SPEND_USD,
            quantity=2.0,
        )
        with self.assertRaises(QuotaExceeded) as caught:
            self.authorise(0.01)
        message = caught.exception.info.user_message or ""
        self.assertIn("Upgrade", message)
        self.assertIn("next month", message)

    def test_spend_still_in_flight_counts_before_any_job_settles(self) -> None:
        """The runaway case. Nothing is written to the meter until the job
        finishes, so a check against the meter alone authorises a loop
        indefinitely."""
        for _ in range(4):
            self.authoriser.note_spend(organisation_id=self.org, amount_usd=0.5)
        self.assertEqual(
            self.meter.total(
                organisation_id=self.org, kind=QuotaKind.PROVIDER_SPEND_USD
            ),
            0.0,
            "precondition: settlement has not happened yet",
        )
        with self.assertRaises(QuotaExceeded):
            self.authorise(0.01)

    def test_settled_spend_is_not_counted_a_second_time(self) -> None:
        """Otherwise the tally and the meter both hold the same dollar and the
        tenant is cut off at half their allowance."""
        self.authoriser.note_spend(organisation_id=self.org, amount_usd=1.5)
        self.meter.record(
            organisation_id=self.org,
            kind=QuotaKind.PROVIDER_SPEND_USD,
            quantity=1.5,
        )
        self.authorise(0.4)  # 1.5 committed + 0 in flight + 0.4 <= 2.0

    def test_one_tenants_in_flight_spend_does_not_block_another(self) -> None:
        other = org_id()
        for _ in range(6):
            self.authoriser.note_spend(organisation_id=self.org, amount_usd=0.5)
        self.authoriser.authorise(organisation_id=other, amount_usd=1.0)

    def test_a_runaway_loop_stops_partway_through_a_single_job(self) -> None:
        """End to end, through the router: the thing plans.py promised.

        Before this the loop below ran to completion — every call passed a check
        that was never made — and the bill arrived afterwards.
        """
        router = GenerationRouter(
            events=EventSink(), spend_authoriser=self.authoriser
        )
        provider = _HalfDollarImageProvider()
        router.register(provider, GenerationKind.IMAGE)

        spent = 0.0
        with self.assertRaises(QuotaExceeded):
            for index in range(100):
                result = asyncio.run(
                    router.generate(
                        GenerationRequest(
                            organisation_id=self.org,
                            kind=GenerationKind.IMAGE,
                            # A distinct prompt each time: a cached result costs
                            # nothing and would not exercise the breaker.
                            params=ImageParams(prompt=f"frame {index}"),
                            budget=Budget(max_cost_usd=0.5),
                        )
                    )
                )
                spent += result.cost_usd
        self.assertEqual(provider.calls, 4, "stopped after four calls, not 100")
        self.assertLessEqual(spent, PLANS[PlanTier.FREE].limit(QuotaKind.PROVIDER_SPEND_USD))


class _HalfDollarImageProvider:
    """Charges 50 cents an image, so four of them exhaust the free plan."""

    calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="half-dollar",
            unit_cost_usd=0.5,
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
            provider="half-dollar",
            model="test",
            structured_output={"ok": True},
            cost_usd=0.5,
            latency_ms=1,
        )


class TheAssemblyCannotForgetTheBreaker(unittest.TestCase):
    """The check is only a chokepoint if every deployment has it wired.

    `GenerationRouter.spend_authoriser` is optional on the type — the
    evaluation harness and most unit tests have no tenant — which makes "the
    assembly forgot" a real way to lose the guarantee. This is the test that
    makes it a build failure instead.
    """

    def test_the_wired_router_authorises_spend_against_the_usage_meter(self) -> None:
        with TemporaryDirectory(prefix="vtv-assembly-") as directory:
            root = Path(directory)
            assembly = build(
                Settings(asset_search_endpoint="", 
                    storage_root=root / "storage",
                    database_url=f"sqlite:///{root / 'vtv.db'}",
                    env="development",
                )
            )
            authoriser = assembly.router.spend_authoriser
            self.assertIsInstance(authoriser, MeteredSpendAuthoriser)
            self.assertIs(authoriser.meter, assembly.usage)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
