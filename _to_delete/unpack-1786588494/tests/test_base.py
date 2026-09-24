"""Foundational value types: identifiers, time, storage references, budgets."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from vtv.contracts import (
    Budget,
    IdPrefix,
    ObjectRef,
    RetentionClass,
    TimeSpan,
    id_prefix_of,
    new_id,
)


class Identifiers(unittest.TestCase):
    def test_ids_are_prefixed_and_unique(self) -> None:
        ids = {new_id(IdPrefix.SCENE) for _ in range(2000)}
        self.assertEqual(len(ids), 2000, "identifier collision in 2000 draws")
        for value in list(ids)[:20]:
            self.assertTrue(value.startswith("scn_"))
            self.assertEqual(id_prefix_of(value), "scn")

    def test_ids_avoid_ambiguous_characters(self) -> None:
        # i, l, o and u are excluded so identifiers survive being read aloud or
        # retyped from a screenshot in a support ticket.
        body = new_id(IdPrefix.PROJECT).split("_", 1)[1]
        self.assertFalse(set(body) & set("ilou"))

    def test_prefix_must_be_three_lowercase_letters(self) -> None:
        for bad in ("PRJ", "pr", "proj", "p1j", ""):
            with self.assertRaises(ValueError):
                new_id(bad)


class TimeSpans(unittest.TestCase):
    def test_span_must_advance(self) -> None:
        with self.assertRaises(ValidationError):
            TimeSpan(start=4.0, end=4.0)
        with self.assertRaises(ValidationError):
            TimeSpan(start=4.0, end=1.0)

    def test_duration_overlap_and_containment(self) -> None:
        a = TimeSpan.of(0.0, 10.0)
        b = TimeSpan.of(5.0, 15.0)
        c = TimeSpan.of(10.0, 12.0)
        self.assertEqual(a.duration, 10.0)
        self.assertTrue(a.overlaps(b))
        # Spans are half-open, so touching at a boundary is not an overlap.
        self.assertFalse(a.overlaps(c))
        self.assertTrue(a.contains(TimeSpan.of(1.0, 9.0)))
        self.assertFalse(a.contains(b))

    def test_intersection(self) -> None:
        overlap = TimeSpan.of(0.0, 10.0).intersection(TimeSpan.of(6.0, 20.0))
        assert overlap is not None
        self.assertEqual((overlap.start, overlap.end), (6.0, 10.0))
        self.assertIsNone(TimeSpan.of(0.0, 5.0).intersection(TimeSpan.of(5.0, 9.0)))


class StorageReferences(unittest.TestCase):
    def test_keys_may_not_escape_their_prefix(self) -> None:
        for bad_key in ("/etc/passwd", "projects/../../secrets", "../x"):
            with self.assertRaises(ValidationError):
                ObjectRef(bucket="b", key=bad_key, content_type="image/png")

    def test_uri_is_storage_agnostic(self) -> None:
        ref = ObjectRef(bucket="media", key="a/b.png", content_type="image/png")
        self.assertEqual(ref.uri, "obj://media/a/b.png")
        self.assertIs(ref.retention, RetentionClass.EPHEMERAL)

    def test_checksum_must_be_a_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            ObjectRef(
                bucket="b", key="k", content_type="image/png", checksum_sha256="nope"
            )


class Budgets(unittest.TestCase):
    def test_unset_budget_allows_everything(self) -> None:
        self.assertTrue(Budget().allows(cost_usd=1000.0, latency_seconds=1e6))

    def test_budget_refuses_overspend(self) -> None:
        budget = Budget(max_cost_usd=0.10, max_latency_seconds=30)
        self.assertTrue(budget.allows(cost_usd=0.09, latency_seconds=29))
        self.assertFalse(budget.allows(cost_usd=0.11, latency_seconds=1))
        self.assertFalse(budget.allows(cost_usd=0.01, latency_seconds=31))


class Fingerprints(unittest.TestCase):
    def test_equal_models_hash_equally_regardless_of_field_order(self) -> None:
        one = ObjectRef(bucket="b", key="k", content_type="image/png", size_bytes=10)
        two = ObjectRef(size_bytes=10, content_type="image/png", key="k", bucket="b")
        self.assertEqual(one.fingerprint(), two.fingerprint())

    def test_different_models_hash_differently(self) -> None:
        one = ObjectRef(bucket="b", key="k", content_type="image/png")
        two = ObjectRef(bucket="b", key="k2", content_type="image/png")
        self.assertNotEqual(one.fingerprint(), two.fingerprint())


class UnknownFieldsAreRejected(unittest.TestCase):
    """The most valuable single line of configuration in the contracts.

    A language model that invents a field, or a client on a stale schema, must
    fail loudly here rather than have its extra data silently dropped and
    discovered missing four stages later.
    """

    def test_extra_fields_raise(self) -> None:
        with self.assertRaises(ValidationError):
            ObjectRef(
                bucket="b",
                key="k",
                content_type="image/png",
                confidence_score=0.9,  # type: ignore[call-arg]
            )


if __name__ == "__main__":
    unittest.main()
