"""How much thinking a project may buy.

Model work used to be linear in the number of shots: every additional minute of
video cost the same intelligence as the first. That is the wrong shape, and the
reason is not money — it is that the thirty-seventh minute of a lecture is
mostly elaboration of decisions already taken.
"""

from __future__ import annotations

import unittest
from itertools import pairwise

from vtv.pipeline.intelligence import LINES_PER_MINUTE, Budget, spread, stride_for


class LongerVideosThinkLessPerMinute(unittest.TestCase):
    def budget(self, minutes: float) -> Budget:
        return Budget.of(int(minutes * LINES_PER_MINUTE), minutes=minutes)

    def test_a_short_video_reads_every_line(self) -> None:
        """Somebody's first impression of the product, and the marginal call is
        cheap."""
        budget = self.budget(5)
        self.assertTrue(budget.reads_everything)
        self.assertEqual(budget.inferred, 0)

    def test_a_four_hour_video_reads_one_line_in_six(self) -> None:
        self.assertEqual(self.budget(240).stride, 6)

    def test_intelligence_per_minute_falls_monotonically(self) -> None:
        """The property the whole module exists for. Not merely 'sub-linear' —
        every step down the ladder must be cheaper per minute than the one
        above, or a video could get *more* expensive by getting longer."""
        rates = [self.budget(m).calls_per_minute(26) for m in (5, 30, 60, 120, 240)]
        for shorter, longer in pairwise(rates):
            self.assertLessEqual(longer, shorter, rates)

    def test_four_hours_is_an_order_of_magnitude_cheaper_than_five_minutes(self) -> None:
        short = self.budget(5).calls_per_minute(26)
        long = self.budget(240).calls_per_minute(26)
        self.assertGreater(short / long, 5.0)

    def test_total_calls_still_grow_but_slowly(self) -> None:
        """A four-hour video is 48 times longer than a five-minute one and does
        not make 48 times the calls."""
        short = self.budget(5).calls_for(26)
        long = self.budget(240).calls_for(26)
        self.assertLess(long, short * 12)

    def test_an_empty_script_costs_nothing(self) -> None:
        budget = Budget.of(0)
        self.assertEqual(budget.calls_for(26), 0)
        self.assertEqual(budget.read, 0)

    def test_the_duration_is_estimated_when_not_yet_known(self) -> None:
        """The budget is decided before narration is synthesised, so the true
        duration does not exist yet."""
        self.assertAlmostEqual(Budget.of(720).minutes, 40.0, places=1)

    def test_the_stride_ladder_is_ordered(self) -> None:
        strides = [stride_for(m) for m in (5, 20, 45, 90, 200, 1000)]
        self.assertEqual(strides, sorted(strides))


class TheSampleAlwaysIncludesTheOpening(unittest.TestCase):
    def test_the_first_line_is_always_read(self) -> None:
        """A video's opening shot is the one a viewer judges it by, and it is
        also what every other line in its passage inherits from."""
        for lines in (10, 100, 1000):
            budget = Budget.of(lines)
            self.assertEqual(budget.sample(["x"] * lines)[0], 0)

    def test_the_sample_is_evenly_spread(self) -> None:
        budget = Budget.of(600)
        indices = budget.sample(["x"] * 600)
        gaps = {b - a for a, b in pairwise(indices)}
        self.assertEqual(gaps, {budget.stride})


class InheritanceReadsForwards(unittest.TestCase):
    """The decision for a passage is taken at its first sentence and holds
    until something changes it — which is how a person reads."""

    def test_a_line_inherits_from_before_it_never_after(self) -> None:
        out = spread({0: "chart"}, total=3)
        self.assertEqual(out, ["chart", "chart", "chart"])

    def test_a_later_reading_takes_over(self) -> None:
        out = spread({0: "chart", 2: "found_media"}, total=4)
        self.assertEqual(out, ["chart", "chart", "found_media", "found_media"])

    def test_inheritance_never_crosses_a_passage_boundary(self) -> None:
        """Without this, the last line of a chapter about quantities hands its
        chart to the first line of a chapter about people — the failure that
        makes naive sampling look stupid rather than economical."""
        out = spread({0: "chart"}, total=4, boundaries=frozenset({2}))
        self.assertEqual(out, ["chart", "chart", "", ""])

    def test_a_line_with_nothing_before_it_gets_no_suggestion(self) -> None:
        """Empty means "no suggestion", and the director's own rules decide —
        which is strictly better than inheriting something unrelated."""
        self.assertEqual(spread({2: "chart"}, total=3), ["", "", "chart"])

    def test_every_line_gets_an_answer(self) -> None:
        out = spread({0: "a", 5: "b"}, total=9)
        self.assertEqual(len(out), 9)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
