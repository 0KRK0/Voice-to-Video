"""Where a job runs — and the rule that stops the interface lying about it.

The whole value of this module is one guarantee: **a target with no backend
behind it is never offered and never chosen.** Everything else is preference
ordering, which is easy; the guarantee is what makes it safe to put a "use my
graphics card" control in front of a customer before a GPU compositor exists.

The failure being prevented is specific and this product has a matching one in
its history. `RegenerationIntent` offered ten menu items and every one of them
returned typography, so the interface described capabilities the code did not
have and the user discovered it by using it. A "⚡ Fast — GPU" button on a build
with no GPU compositor is the same defect wearing better clothes, and it is
discovered by waiting through a render.
"""

from __future__ import annotations

import unittest

from vtv.contracts.execution import (
    ExecutionPolicy,
    ExecutionTarget,
    Machine,
    NoBackendAvailable,
    Preference,
    Processor,
    Registry,
    resolve,
)

BACKEND = object()


def registry(*targets: ExecutionTarget) -> Registry:
    found = Registry()
    for target in targets:
        found.register(target, BACKEND)
    return found


class ATargetWithNothingBehindItIsNeverChosen(unittest.TestCase):
    """The guarantee. Stated four ways, because it is the only thing here that
    would be expensive to get wrong."""

    def test_auto_picks_only_from_what_exists(self) -> None:
        found = registry(ExecutionTarget.CLOUD_CPU)
        self.assertIs(
            resolve(ExecutionPolicy.auto(), found).target, ExecutionTarget.CLOUD_CPU
        )

    def test_asking_for_a_missing_target_does_not_get_it(self) -> None:
        found = registry(ExecutionTarget.CLOUD_CPU)
        decision = resolve(
            ExecutionPolicy.exactly(ExecutionTarget.LOCAL_GPU), found
        )
        self.assertIs(decision.target, ExecutionTarget.CLOUD_CPU)
        self.assertIs(decision.instead_of, ExecutionTarget.LOCAL_GPU)
        self.assertTrue(decision.substituted)

    def test_a_substitution_is_always_reported(self) -> None:
        """Silently running somewhere else is how a customer on a metered plan
        gets a bill they cannot explain."""
        found = registry(ExecutionTarget.CLOUD_CPU)
        decision = resolve(ExecutionPolicy(preference=Preference.LOCAL), found)
        self.assertTrue(decision.substituted)
        # Asserted as meaning rather than as wording: the sentence is for a
        # person and will be rewritten, and a test that pins the phrasing turns
        # every improvement to it into a failure.
        self.assertIs(decision.target.machine, Machine.CLOUD)
        self.assertTrue(decision.target.costs_us_money)
        self.assertGreater(len(decision.why.split()), 4, decision.why)

    def test_an_empty_registry_refuses_rather_than_guesses(self) -> None:
        with self.assertRaises(NoBackendAvailable):
            resolve(ExecutionPolicy.auto(), Registry())


class StrictMeansStrict(unittest.TestCase):
    """"Never run this in the cloud" is a real requirement — a metered plan, a
    data-residency rule, a customer who does not want their script leaving the
    building — and a policy that silently ignores it is worse than no policy."""

    def test_a_strict_policy_refuses_instead_of_substituting(self) -> None:
        found = registry(ExecutionTarget.CLOUD_CPU)
        with self.assertRaises(NoBackendAvailable):
            resolve(
                ExecutionPolicy.exactly(ExecutionTarget.LOCAL_CPU, strict=True), found
            )

    def test_a_strict_policy_still_succeeds_when_it_can(self) -> None:
        found = registry(ExecutionTarget.LOCAL_CPU)
        decision = resolve(
            ExecutionPolicy.exactly(ExecutionTarget.LOCAL_CPU, strict=True), found
        )
        self.assertIs(decision.target, ExecutionTarget.LOCAL_CPU)
        self.assertFalse(decision.substituted)


class TheCustomersMachineComesFirst(unittest.TestCase):
    """The economics, expressed as a default.

    Rendering is the only cost that scales with the length of somebody's video.
    Running it on their own machine is free to us, faster for them — nobody is
    sharing those cores — and removes the ceiling on project length. So `AUTO`
    prefers local, and cloud is the fallback rather than the default.
    """

    def test_local_wins_over_cloud_when_both_exist(self) -> None:
        found = registry(ExecutionTarget.CLOUD_GPU, ExecutionTarget.LOCAL_CPU)
        self.assertIs(
            resolve(ExecutionPolicy.auto(), found).target, ExecutionTarget.LOCAL_CPU
        )

    def test_a_gpu_wins_over_a_cpu_on_the_same_machine(self) -> None:
        found = registry(ExecutionTarget.LOCAL_CPU, ExecutionTarget.LOCAL_GPU)
        self.assertIs(
            resolve(ExecutionPolicy.auto(), found).target, ExecutionTarget.LOCAL_GPU
        )

    def test_only_cloud_execution_costs_us_anything(self) -> None:
        for target in ExecutionTarget:
            with self.subTest(target=target):
                self.assertEqual(
                    target.costs_us_money, target.machine is Machine.CLOUD
                )

    def test_every_target_decomposes_into_a_machine_and_a_processor(self) -> None:
        """The enum is a pair, and reading it as one is what lets pricing ask
        "whose computer" without also asking "which chip"."""
        self.assertIs(ExecutionTarget.LOCAL_GPU.machine, Machine.LOCAL)
        self.assertIs(ExecutionTarget.LOCAL_GPU.processor, Processor.GPU)
        self.assertIs(ExecutionTarget.CLOUD_CPU.machine, Machine.CLOUD)
        self.assertIs(ExecutionTarget.CLOUD_CPU.processor, Processor.CPU)


class PreferencesAreNotPlaces(unittest.TestCase):
    """`AUTO` is a policy and `LOCAL_GPU` is a location, and an enum holding
    both means every reader has to handle a value that is not a place. The ones
    that forget treat "you choose" as somewhere to run."""

    def test_the_target_enum_holds_only_real_places(self) -> None:
        for target in ExecutionTarget:
            with self.subTest(target=target):
                self.assertIn(target.machine, (Machine.LOCAL, Machine.CLOUD))
                self.assertIn(target.processor, (Processor.CPU, Processor.GPU))
        self.assertEqual(len(ExecutionTarget), 4)

    def test_fallbacks_are_honoured_in_order(self) -> None:
        found = registry(ExecutionTarget.CLOUD_CPU, ExecutionTarget.CLOUD_GPU)
        decision = resolve(
            ExecutionPolicy(
                preference=Preference.EXACT,
                target=ExecutionTarget.LOCAL_GPU,
                fallbacks=(ExecutionTarget.CLOUD_GPU, ExecutionTarget.CLOUD_CPU),
            ),
            found,
        )
        self.assertIs(decision.target, ExecutionTarget.CLOUD_GPU)


class TheReportExplainsItself(unittest.TestCase):
    """An interface listing one option cannot explain why there are not four,
    and "why can't I use my graphics card" is a support ticket either way."""

    def test_every_target_is_listed_whether_or_not_it_works(self) -> None:
        listed = registry(ExecutionTarget.CLOUD_CPU).available()
        self.assertEqual(len(listed), len(ExecutionTarget))
        self.assertEqual(sum(1 for item in listed if item.ready), 1)

    def test_an_unavailable_target_carries_a_reason(self) -> None:
        for item in registry(ExecutionTarget.CLOUD_CPU).available():
            if not item.ready:
                self.assertTrue(item.reason, f"{item.target} has no reason")

    def test_a_probed_reason_beats_the_generic_one(self) -> None:
        """"This machine has no supported card" and "no GPU compositor exists"
        send a user to two different actions."""
        found = registry(ExecutionTarget.CLOUD_CPU)
        found.notes[ExecutionTarget.LOCAL_GPU] = "no supported card was found"
        reasons = {
            item.target: item.reason for item in found.available() if not item.ready
        }
        self.assertEqual(reasons[ExecutionTarget.LOCAL_GPU], "no supported card was found")


class WhatThisBuildActuallyOffers(unittest.TestCase):
    """The report the product ships, checked against the code that exists.

    This is the test that fails the day somebody adds a GPU option to the
    interface without adding a GPU backend behind it.
    """

    def registry(self) -> Registry:
        from vtv.config import Settings
        from vtv.wiring import execution_registry

        # Built the way the running system builds it — from the composition
        # root — so this test cannot pass against a registry that only exists
        # in the test.
        return execution_registry(Settings(asset_search_endpoint=""))

    def test_exactly_one_backend_is_registered(self) -> None:
        self.assertEqual(len(self.registry().ready()), 1)

    def test_and_it_is_a_cpu_one(self) -> None:
        (only,) = self.registry().ready()
        self.assertIs(only.processor, Processor.CPU)

    def test_no_gpu_target_claims_to_be_ready(self) -> None:
        """Skipped rather than failed on a machine that has one — this asserts
        what *this* build offers, and on a workstation with a working card the
        honest answer is different."""
        from vtv.adapters.render.gpu_probe import probe

        if probe().available:
            self.skipTest("this machine has a proven GPU; see the equivalence run")
        for item in self.registry().available():
            if item.target.processor is Processor.GPU:
                self.assertFalse(item.ready, f"{item.target} claims a GPU path")
                # Actionable, not merely present: "install a graphics library"
                # and "your driver is too old" send a user to different places.
                self.assertGreater(len(item.reason.split()), 3, item.reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
