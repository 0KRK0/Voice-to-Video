"""Choosing a picture, tested against the pictures that actually shipped.

Every title in `TheRenderThatWentWrong` is verbatim from one real 75-second
render's worker log. That matters more than it sounds: a relevance threshold
tuned on invented examples is tuned on what its author imagined a search engine
returns, and the whole defect here was that nobody had looked at what they
actually return.
"""

from __future__ import annotations

import asyncio
import unittest

from vtv.contracts.base import IdPrefix, new_id
from vtv.pipeline.selection import (
    FLOOR,
    Brief,
    Candidate,
    CandidateSelector,
    Verdict,
    aspect_penalty,
    content_words,
    names_a_person,
    score,
    screen,
    specificity,
    without_repeats,
)

ORG = new_id(IdPrefix.PROJECT)
PROJECT = new_id(IdPrefix.PROJECT)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def hit(query: str, title: str, **kw: object) -> Candidate:
    return Candidate(key=title[:24], title=title, query=query, **kw)  # type: ignore[arg-type]


class TheRenderThatWentWrong(unittest.TestCase):
    """The six visuals from one real video, judged.

    Three were nonsense, one was a real person's face used to illustrate an
    abstract claim, and two were fine. The screen has to agree.
    """

    def test_a_railway_litter_event_is_not_a_person_at_a_computer(self) -> None:
        candidate = hit(
            "person clicking on computer",
            "Müllsammelaktion am Hofer Hauptbahnhof 20230513 HOF02547 RAW-Export.png",
        )
        self.assertFalse(score(candidate).is_usable)

    def test_a_rank_insignia_is_not_a_person_directing_ai(self) -> None:
        candidate = hit("person directing AI", "AM Mural crown.jpg")
        self.assertFalse(score(candidate).is_usable)

    def test_a_livestock_exchange_is_not_skills_for_the_future(self) -> None:
        candidate = hit(
            "skills for the future",
            "Central West Livestock Exchange near Forbes, NSW 01.jpg",
        )
        self.assertFalse(score(candidate).is_usable)

    def test_a_named_persons_headshot_is_declined_outright(self) -> None:
        """It filled the frame with a stranger's forehead, and it implied she
        had said the thing the narration was saying."""
        candidate = hit(
            "person talking to AI",
            "Elin Wieslander at SXSW 2025 03 (cropped).jpg",
            width=1200,
            height=1600,
        )
        result = score(candidate)
        self.assertFalse(result.is_usable)
        self.assertIn("identified person", result.reason)

    def test_the_sunrise_survives(self) -> None:
        """The one that worked, and the reason the screen measures against the
        query rather than the narration.

        "Imagine waking up ten years from now" shares no word with this file.
        The *query* — a considered visual metaphor — shares all of them.
        """
        candidate = hit(
            "sunrise through window", "Sunrise through window (7449258498).jpg"
        )
        self.assertTrue(score(candidate).is_usable)

    def test_half_a_two_word_query_is_not_enough(self) -> None:
        """"Card stock speech balloons" matches "speech" and not "bubble", and
        it is a stock photograph of four people holding paper shapes. It shipped
        in one render and filled the frame without illustrating anything.

        A two-word query qualifies only when both words are there. The rung
        below draws the idea instead, which is a better picture and free.
        """
        self.assertFalse(score(hit("speech bubble", "Card stock speech balloons.jpg")).is_usable)

    def test_both_words_of_a_two_word_query_is_enough(self) -> None:
        self.assertTrue(score(hit("speech bubble", "A speech bubble on a wall")).is_usable)

    def test_the_conference_panel_is_what_the_screen_cannot_catch(self) -> None:
        """Honest about the limit of the free stage.

        "Skills for the Future" is an exact answer to `skills for the future`
        and is a photograph of a man on a panel. No lexical rule reaches it —
        that is the judge's job, and the reason the judge exists at all.
        """
        candidate = hit("skills for the future", "Skills for the Future (48858525078).jpg")
        self.assertTrue(score(candidate).is_usable)


class AVagueQueryIsWorthLess(unittest.TestCase):
    """The worst thing about the first version of this module.

    Coverage is a fraction, and a fraction is trivially 1.0 when the denominator
    is 1. The concept reader emitted fallback queries built from bare words of
    the narration — `fascinating`, `plan`, `AI` — and any photograph whose title
    contained that word scored a perfect 1.0 and beat every result of the four
    considered queries beside it.
    """

    def test_the_elephants(self) -> None:
        """A 1904 stereoscopic card of elephants in Hyderabad, under a sentence
        about the most valuable skill changing, because the card is titled "A
        fascinating glimpse of Hyderabad" and one of the queries was
        `fascinating`."""
        self.assertFalse(
            score(hit("fascinating", "A fascinating glimpse of Hyderabad, India.jpg")).is_usable
        )

    def test_a_considered_query_now_outranks_a_vague_one(self) -> None:
        """The ranking was upside down: a longer, more specific query is harder
        to match completely, so it lost to a one-word fragment every time."""
        vague = hit("fascinating", "A fascinating glimpse of Hyderabad, India.jpg")
        considered = hit("person waking up in bed", "A person waking up in bed at sunrise")
        self.assertGreater(score(considered).score, score(vague).score)

    def test_specificity_saturates_rather_than_rewarding_essays(self) -> None:
        """Otherwise a nine-word query would be worth three times a three-word
        one, and the reader would be rewarded for padding."""
        self.assertEqual(specificity(content_words("one two three")), 1.0)
        self.assertEqual(specificity(content_words("one two three four five six")), 1.0)


class WhatTheScreenMeasures(unittest.TestCase):
    def test_stopwords_do_not_count_as_agreement(self) -> None:
        """How a railway station comes to answer "person clicking on computer":
        both contain "on"."""
        self.assertEqual(content_words("person clicking on the computer"),
                         content_words("computer person click"))

    def test_a_crude_stem_joins_click_and_clicking(self) -> None:
        self.assertIn("click", content_words("clicking"))
        self.assertIn("click", content_words("clicks"))

    def test_filename_noise_is_not_subject_matter(self) -> None:
        """Otherwise every JPEG agrees with every other JPEG."""
        self.assertEqual(content_words("HOF02547 RAW-Export.jpg"), frozenset())

    def test_a_two_letter_subject_is_a_subject(self) -> None:
        """"AI" is what half this product's scripts are about, and a
        three-letter floor discarded it — so `AI in business` had no subject at
        all and matched nothing at all. Same for ML, UI, UX, VR, AR, 3D."""
        self.assertIn("ai", content_words("AI in business"))
        self.assertTrue(
            score(hit("AI in business", "AI adoption in business, 2024")).is_usable
        )
        # …but matching only the "AI" half is still half a query. "Generative AI
        # in Sales Market" is a market-size chart, and a chart of a market is
        # not a picture of AI in business.
        self.assertFalse(
            score(hit("AI in business", "Generative AI in Sales Market.png")).is_usable
        )

    def test_two_letter_noise_is_still_noise(self) -> None:
        """The shorter floor is safe because nearly every two-letter English
        word that is not a subject is already a stopword."""
        self.assertFalse(
            score(hit("person directing AI", "AM Mural crown.jpg")).is_usable
        )

    def test_single_letters_stay_out(self) -> None:
        self.assertEqual(content_words("a b c"), frozenset())

    def test_full_coverage_outranks_partial(self) -> None:
        best = hit("red bicycle", "A red bicycle leaning on a wall")
        worse = hit("red bicycle", "A red door")
        ranked = screen([worse, best])
        self.assertEqual(ranked[0].candidate.key, best.key)

    def test_ranking_is_stable_for_equal_scores(self) -> None:
        """Two identical renders must not choose differently."""
        a = Candidate(key="a", title="a red bicycle", query="red bicycle")
        b = Candidate(key="b", title="a red bicycle", query="red bicycle")
        self.assertEqual(
            [s.candidate.key for s in screen([a, b])],
            [s.candidate.key for s in screen([b, a])],
        )

    def test_a_candidate_with_no_words_of_its_own_scores_zero(self) -> None:
        self.assertEqual(score(hit("red bicycle", "IMG_0042.jpg")).score, 0.0)

    def test_with_no_query_the_providers_own_ranking_stands(self) -> None:
        """Neither pass nor fail. Inventing a verdict with nothing to check
        against is how a screen starts rejecting good pictures."""
        candidate = Candidate(key="k", title="something", query="", relevance=0.9)
        self.assertAlmostEqual(score(candidate).score, 0.9)


class ShapeBreaksTiesAndNothingMore(unittest.TestCase):
    """A portrait in a 16:9 frame crops to a band across the middle of it."""

    def test_a_tall_portrait_is_penalised(self) -> None:
        self.assertGreater(aspect_penalty(Candidate(key="k", width=1200, height=1600)), 0.05)

    def test_a_landscape_photo_is_not(self) -> None:
        self.assertLess(aspect_penalty(Candidate(key="k", width=1920, height=1080)), 0.01)

    def test_unknown_dimensions_are_not_a_penalty(self) -> None:
        """Most commons results declare none, and guessing would reject them
        all."""
        self.assertEqual(aspect_penalty(Candidate(key="k")), 0.0)

    def test_shape_never_outweighs_subject(self) -> None:
        """A portrait of the right thing still beats a landscape of the wrong
        one — the penalty is a tiebreak, not a judgement."""
        right = hit("red bicycle", "a red bicycle", width=1200, height=1600)
        wrong = hit("red bicycle", "a blue lorry", width=1920, height=1080)
        self.assertGreater(score(right).score, score(wrong).score)

    def test_the_penalty_is_symmetric(self) -> None:
        """2:1 and 1:2 are equally wrong for a 16:9 frame."""
        wide = aspect_penalty(Candidate(key="k", width=2000, height=1000))
        tall = aspect_penalty(Candidate(key="k", width=1000, height=2000))
        self.assertGreater(tall, wide)


class APhotographOfAPersonIsNotAnIdea(unittest.TestCase):
    """The system refuses to *generate* a real person's likeness. Reusing a
    photograph of one to illustrate a claim they never made is the same wrong
    with a licence attached."""

    def test_the_real_filename_that_shipped_is_caught(self) -> None:
        """It contains no word from the press-photography vocabulary at all —
        "SXSW" is the event, and no list of event names is ever finished."""
        self.assertTrue(
            names_a_person(
                Candidate(key="k", title="Elin Wieslander at SXSW 2025 03 (cropped).jpg")
            )
        )

    def test_three_capitalised_words_is_a_place_not_a_person(self) -> None:
        """"Golden Gate Bridge at Sunset 2019" is a landscape."""
        self.assertFalse(
            names_a_person(Candidate(key="k", title="Golden Gate Bridge at Sunset 2019"))
        )

    def test_without_a_year_it_is_not_read_as_an_event(self) -> None:
        self.assertFalse(names_a_person(Candidate(key="k", title="Times Square at Night")))

    def test_a_name_plus_an_event_word_is_a_portrait(self) -> None:
        self.assertTrue(
            names_a_person(Candidate(key="k", title="Elin Wieslander at SXSW 2025, speaking"))
        )

    def test_a_place_name_alone_is_not(self) -> None:
        """Otherwise every Commons photograph of anywhere is refused."""
        self.assertFalse(
            names_a_person(
                Candidate(key="k", title="Central West Livestock Exchange near Forbes")
            )
        )

    def test_an_event_word_alone_is_not(self) -> None:
        self.assertFalse(names_a_person(Candidate(key="k", title="a conference room")))


class TheJudgeIsAskedOnceForTheWholeScript(unittest.TestCase):
    def briefs(self, count: int) -> list[tuple[Brief, list[Candidate]]]:
        return [
            (
                Brief(unit_id=f"vun_{i}", sentence="a line", subject="an idea"),
                [hit("red bicycle", "a red bicycle in a street")],
            )
            for i in range(count)
        ]

    def test_no_router_leaves_the_screens_answer_standing(self) -> None:
        """Strictly better than what it replaced, and it never blocks a render."""
        verdicts = run(
            CandidateSelector().select(
                self.briefs(3), organisation_id=ORG, project_id=PROJECT
            )
        )
        self.assertEqual(len(verdicts), 3)
        self.assertTrue(all(v.decided_by == "screen" for v in verdicts.values()))

    def test_thirteen_shots_are_one_call_not_thirteen(self) -> None:
        """The cost shape this system has twice had to fix."""

        class Router:
            calls = 0

            async def generate(self, request):  # type: ignore[no-untyped-def]
                Router.calls += 1
                from vtv.contracts.errors import Status

                shots = request.params.input_json["shots"]
                return _ready(
                    {
                        "choices": [
                            {"unit": s["unit"], "key": s["candidates"][0]["key"],
                             "score": 0.9, "reason": "apt"}
                            for s in shots
                        ]
                    },
                    Status,
                )

        verdicts = run(
            CandidateSelector(router=Router()).select(
                self.briefs(13), organisation_id=ORG, project_id=PROJECT
            )
        )
        self.assertEqual(Router.calls, 1)
        self.assertEqual(len(verdicts), 13)
        self.assertTrue(all(v.decided_by == "judge" for v in verdicts.values()))

    def test_the_judge_may_refuse_everything_the_screen_allowed(self) -> None:
        """The most valuable answer this module produces. "Skills for the
        Future" passes the screen and is a conference panel."""

        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                from vtv.contracts.errors import Status

                shots = request.params.input_json["shots"]
                return _ready(
                    {"choices": [{"unit": s["unit"], "key": None, "score": 0.1,
                                  "reason": "a photo of a panel, not of the idea"}
                                 for s in shots]},
                    Status,
                )

        verdicts = run(
            CandidateSelector(router=Router()).select(
                self.briefs(2), organisation_id=ORG, project_id=PROJECT
            )
        )
        for verdict in verdicts.values():
            self.assertIsNone(verdict.chosen)
            self.assertEqual(verdict.decided_by, "judge")

    def test_a_key_the_model_invented_is_refused(self) -> None:
        """The one path by which a hallucinated field could reach storage."""

        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                from vtv.contracts.errors import Status

                shots = request.params.input_json["shots"]
                return _ready(
                    {"choices": [{"unit": s["unit"], "key": "ast_not_a_real_thing",
                                  "score": 0.99, "reason": "trust me"}
                                 for s in shots]},
                    Status,
                )

        verdicts = run(
            CandidateSelector(router=Router()).select(
                self.briefs(1), organisation_id=ORG, project_id=PROJECT
            )
        )
        verdict = verdicts["vun_0"]
        self.assertEqual(verdict.decided_by, "screen")
        self.assertNotEqual(verdict.chosen, "ast_not_a_real_thing")

    def test_a_low_confidence_choice_is_treated_as_none(self) -> None:
        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                from vtv.contracts.errors import Status

                shots = request.params.input_json["shots"]
                return _ready(
                    {"choices": [{"unit": s["unit"], "key": s["candidates"][0]["key"],
                                  "score": 0.05, "reason": "a stretch"}
                                 for s in shots]},
                    Status,
                )

        verdicts = run(
            CandidateSelector(router=Router()).select(
                self.briefs(1), organisation_id=ORG, project_id=PROJECT
            )
        )
        self.assertIsNone(verdicts["vun_0"].chosen)
        self.assertLess(verdicts["vun_0"].score, FLOOR)

    def test_a_provider_that_raises_does_not_lose_the_video(self) -> None:
        """Selection improves a render that already works without it."""

        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                raise RuntimeError("vendor is down")

        verdicts = run(
            CandidateSelector(router=Router()).select(
                self.briefs(2), organisation_id=ORG, project_id=PROJECT
            )
        )
        self.assertEqual(len(verdicts), 2)
        self.assertTrue(all(v.decided_by == "screen" for v in verdicts.values()))

    def test_shots_the_screen_emptied_are_never_sent_to_the_model(self) -> None:
        """Paying a vendor to confirm that a railway station is not a bicycle."""
        sent: list[int] = []

        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                from vtv.contracts.errors import Status

                sent.append(len(request.params.input_json["shots"]))
                return _ready({"choices": []}, Status)

        work = [
            (Brief(unit_id="good", sentence="s"), [hit("red bicycle", "a red bicycle")]),
            (Brief(unit_id="bad", sentence="s"),
             [hit("red bicycle", "Müllsammelaktion am Hofer Hauptbahnhof")]),
        ]
        verdicts = run(
            CandidateSelector(router=Router()).select(
                work, organisation_id=ORG, project_id=PROJECT
            )
        )
        self.assertEqual(sent, [1])
        self.assertIsNone(verdicts["bad"].chosen)
        self.assertEqual(verdicts["bad"].decided_by, "empty")


def _ready(structured: dict[str, object], status):  # type: ignore[no-untyped-def]
    """A minimal stand-in for a GenerationResult the selector will accept."""
    status_ready = status.READY


    class Result:
        status = status_ready
        structured_output = structured

    return Result()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class NoPhotographTwice(unittest.TestCase):
    """One real render put an identical 1904 stereoscopic card of Hyderabad on
    two separate scenes. To a viewer that reads as a broken editor, not a
    coincidence."""

    def verdicts(self, *pairs: tuple[str, str | None, float]) -> dict[str, Verdict]:
        return {
            unit: Verdict(unit_id=unit, chosen=chosen, score=score_, reason="")
            for unit, chosen, score_ in pairs
        }

    def test_the_second_user_of_a_photograph_loses_it(self) -> None:
        out = without_repeats(
            self.verdicts(("a", "hyderabad", 0.9), ("b", "hyderabad", 0.5))
        )
        self.assertEqual(out["a"].chosen, "hyderabad")
        self.assertIsNone(out["b"].chosen)
        self.assertIn("already used", out["b"].reason)

    def test_the_better_match_keeps_it(self) -> None:
        out = without_repeats(
            self.verdicts(("a", "shared", 0.4), ("b", "shared", 0.95))
        )
        self.assertEqual(out["b"].chosen, "shared")
        self.assertIsNone(out["a"].chosen)

    def test_the_loser_gets_nothing_rather_than_the_runner_up(self) -> None:
        """"No photograph" is a good outcome — the shot descends to a drawing
        made for that specific line. Reaching for the runner-up would be the
        same "fill the frame" instinct this module exists to remove."""
        out = without_repeats(self.verdicts(("a", "x", 0.9), ("b", "x", 0.8)))
        self.assertIsNone(out["b"].chosen)

    def test_different_photographs_are_left_alone(self) -> None:
        out = without_repeats(self.verdicts(("a", "x", 0.9), ("b", "y", 0.8)))
        self.assertEqual(out["a"].chosen, "x")
        self.assertEqual(out["b"].chosen, "y")

    def test_ties_break_the_same_way_every_time(self) -> None:
        """Two identical renders must make the same video."""
        first = without_repeats(self.verdicts(("a", "x", 0.7), ("b", "x", 0.7)))
        second = without_repeats(self.verdicts(("b", "x", 0.7), ("a", "x", 0.7)))
        self.assertEqual(
            {u: v.chosen for u, v in first.items()},
            {u: v.chosen for u, v in second.items()},
        )

    def test_every_unit_still_gets_a_verdict(self) -> None:
        """Dropping a unit entirely would send it down a different path than
        the one that was decided for it."""
        out = without_repeats(self.verdicts(("a", "x", 0.9), ("b", "x", 0.8), ("c", None, 0.0)))
        self.assertEqual(set(out), {"a", "b", "c"})


class ALongScriptIsStillOneQuestionPerShot(unittest.TestCase):
    """A thirty-minute narration is about 740 shots.

    One call for all of them needs roughly 160 000 output tokens against a
    16 000 ceiling, and a prompt carrying 740 shortlists would exceed the
    context window outright. Neither failure is loud: the first answers the
    first seventy and drops the rest, the second raises and every shot falls
    back to the free screen. Both look exactly like "the agent ran".
    """

    def work(self, count: int) -> list[tuple[Brief, list[Candidate]]]:
        return [
            (
                Brief(unit_id=f"vun_{i:04d}", sentence="a line", subject="an idea"),
                [hit("red bicycle", f"a red bicycle number {i}")],
            )
            for i in range(count)
        ]

    def judge(self, count: int):  # type: ignore[no-untyped-def]
        sizes: list[int] = []

        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                from vtv.contracts.errors import Status

                shots = request.params.input_json["shots"]
                sizes.append(len(shots))
                return _ready(
                    {
                        "choices": [
                            {"unit": s["unit"], "key": s["candidates"][0]["key"],
                             "score": 0.9, "reason": "apt"}
                            for s in shots
                        ]
                    },
                    Status,
                )

        verdicts = run(
            CandidateSelector(router=Router()).select(
                self.work(count), organisation_id=ORG, project_id=PROJECT
            )
        )
        return sizes, verdicts

    def test_every_shot_gets_a_verdict_from_the_model(self) -> None:
        """Not from the fallback. 737 shots, and every one judged."""
        _, verdicts = self.judge(737)
        self.assertEqual(len(verdicts), 737)
        self.assertTrue(all(v.decided_by == "judge" for v in verdicts.values()))

    def test_no_single_call_exceeds_the_output_budget(self) -> None:
        from vtv.pipeline.selection import BATCH_SHOTS

        sizes, _ = self.judge(737)
        self.assertTrue(all(size <= BATCH_SHOTS for size in sizes), sizes)

    def test_it_is_still_heavily_batched(self) -> None:
        """Eleven calls for 737 shots, not 737."""
        sizes, _ = self.judge(737)
        self.assertLess(len(sizes), 20)
        self.assertGreater(len(sizes), 1)

    def test_a_short_script_is_still_exactly_one_call(self) -> None:
        """The common case must not pay for the uncommon one."""
        sizes, _ = self.judge(13)
        self.assertEqual(sizes, [13])
