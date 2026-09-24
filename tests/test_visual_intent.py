"""Reading a sentence for what it should show, and refusing to show some things.

Two properties are under test here and they pull in opposite directions.

The first is usefulness: a sentence must become a query a stock library can
actually answer. "The future of work is changing" is not searchable; "empty
office desks at night" is. The rules cannot do that for every sentence, and
where they cannot they must still produce *something* a search can use rather
than handing back the sentence.

The second is the refusal. It is enforced on the object rather than at the call
site, so these tests construct concepts directly and check that the verdict
appears whatever the caller intended — including when the text came back from a
model rather than from the user.
"""

from __future__ import annotations

import unittest

from vtv.pipeline.selection import content_words
from vtv.pipeline.visual_intent import (
    VisualConcept,
    VisualSafety,
    _with_fallback,
    classify,
    concept_from_rules,
    scrub,
)


class QueriesAreSearchableRatherThanRestatements(unittest.TestCase):
    def test_a_domain_sentence_becomes_a_photographable_subject(self) -> None:
        concept = concept_from_rules(
            "Java is a typed language now, and TypeScript made that normal."
        )
        self.assertIn("source code on a computer screen", concept.queries())

    def test_no_query_is_just_the_sentence(self) -> None:
        line = "It may change what it means to use a computer."
        for query in concept_from_rules(line).queries():
            self.assertNotEqual(query.strip().lower(), line.strip().lower())
            self.assertLessEqual(len(query), 80)

    def test_a_named_subject_leads_the_search(self) -> None:
        concept = concept_from_rules(
            "The transistor was invented at Bell Labs in 1947."
        )
        self.assertEqual(concept.queries()[0], "Bell Labs")

    def test_a_sentence_with_no_domain_still_yields_a_query(self) -> None:
        """The fallback has to be usable, not empty."""
        concept = concept_from_rules("And that creates a fascinating question.")
        self.assertTrue(concept.queries())

    def test_subject_words_are_read_in_the_order_they_were_spoken(self) -> None:
        """Ranking by frequency degenerates to alphabetical in one sentence.

        It produced subjects like "agents businesses" for a sentence about
        businesses running agents, which then reached the user in the inspector.
        """
        concept = concept_from_rules(
            "Businesses could have thousands of agents managing sales."
        )
        self.assertTrue(concept.subject.lower().startswith("businesses"))


class TheVerdictIsAPropertyOfTheConcept(unittest.TestCase):
    def test_a_caller_cannot_supply_it(self) -> None:
        """`safety` is not an argument. Passing one is a TypeError, not a value."""
        with self.assertRaises(TypeError):
            VisualConcept(  # type: ignore[call-arg]
                source_text="anything",
                subject="anything",
                safety=VisualSafety.ALLOW,
            )

    def test_it_is_computed_from_the_prompt_as_well_as_the_narration(self) -> None:
        """A model can propose something the sentence never implied."""
        concept = VisualConcept(
            source_text="The kingdom fell after the last of the wars.",
            subject="a beheading",
            search_queries=("graphic beheading footage",),
            image_prompt="A beheading, photographed closely.",
        )
        self.assertIs(concept.safety, VisualSafety.TEXT_ONLY)
        self.assertEqual(concept.queries(), [])
        self.assertEqual(concept.prompt(), "")

    def test_ordinary_material_is_allowed(self) -> None:
        concept = concept_from_rules("Solar panels went up across the estate.")
        self.assertIs(concept.safety, VisualSafety.ALLOW)
        self.assertTrue(concept.may_search)
        self.assertTrue(concept.may_generate)


class WhatIsRefusedAndWhatIsMerelyNotIllustrated(unittest.TestCase):
    def test_graphic_violence_is_text_only(self) -> None:
        verdict, reason = classify("footage of the massacre and the mutilation")
        self.assertIs(verdict, VisualSafety.TEXT_ONLY)
        self.assertIn("graphic violence", reason)

    def test_self_harm_is_text_only(self) -> None:
        verdict, _ = classify("he had been thinking about suicide for months")
        self.assertIs(verdict, VisualSafety.TEXT_ONLY)

    def test_weapon_making_is_text_only(self) -> None:
        verdict, _ = classify("the manual explained how to make a bomb")
        self.assertIs(verdict, VisualSafety.TEXT_ONLY)

    def test_sexual_content_is_text_only(self) -> None:
        verdict, _ = classify("the site hosted pornography")
        self.assertIs(verdict, VisualSafety.TEXT_ONLY)

    def test_sexual_content_involving_minors_is_refused_outright(self) -> None:
        verdict, _ = classify("explicit pornographic images of children")
        self.assertIs(verdict, VisualSafety.REFUSE)

    def test_children_alone_are_perfectly_fine(self) -> None:
        """The co-occurrence is the rule. A film about a school is not this."""
        verdict, _ = classify("the children walked to school every morning")
        self.assertIs(verdict, VisualSafety.ALLOW)

    def test_violence_as_a_subject_is_not_the_same_as_gore(self) -> None:
        """A history of a war is ordinary material and must stay illustrable."""
        verdict, _ = classify("the war lasted four years and reshaped Europe")
        self.assertIs(verdict, VisualSafety.ALLOW)


class ProfanityIsHandledSeparately(unittest.TestCase):
    def test_it_does_not_change_the_verdict(self) -> None:
        verdict, _ = classify("the fucking compiler crashed again")
        self.assertIs(verdict, VisualSafety.ALLOW)

    def test_it_is_stripped_from_anything_leaving_the_process(self) -> None:
        self.assertEqual(scrub("the fucking compiler"), "the compiler")

    def test_the_narration_itself_is_untouched(self) -> None:
        """We do not edit what the user wrote; we edit what we send onward."""
        line = "The fucking compiler crashed again."
        concept = concept_from_rules(line)
        self.assertEqual(concept.source_text, line)
        for query in concept.queries():
            self.assertNotIn("fucking", query.lower())


class RealSubjectsAreNotInvented(unittest.TestCase):
    def test_a_named_organisation_blocks_generation_but_not_search(self) -> None:
        concept = concept_from_rules(
            "The transistor was invented at Bell Labs in 1947."
        )
        self.assertTrue(concept.depicts_real_subject)
        self.assertTrue(concept.may_search)
        self.assertFalse(concept.may_generate)
        self.assertEqual(concept.prompt(), "")

    def test_a_possessive_phrase_is_not_a_name(self) -> None:
        """Capitalisation makes "Your AI" look like a person to the extractor.

        Treating that as a real subject switched generation off for a large
        share of perfectly ordinary narration, which is a worse failure than the
        one it was guarding against.
        """
        concept = concept_from_rules("Your AI checks your schedule for you.")
        self.assertFalse(concept.depicts_real_subject)
        self.assertTrue(concept.may_generate)



class AnOpeningLineIsAboutTheAction(unittest.TestCase):
    """"Imagine waking up ten years from now" is about *waking up*.

    The rules used to produce the query `imagine waking`, which returns nothing
    because nobody photographs imagining — and it is the first line of a great
    many scripts, so the failure landed on the shot a viewer sees first.

    `keyphrases` ranks by reading order once frequency ties, so the imperative
    was becoming the subject purely by being first. Stripping it before analysis
    rather than filtering it afterwards is what fixes that.
    """

    def test_the_imperative_does_not_become_the_subject(self) -> None:
        concept = concept_from_rules("Imagine waking up ten years from now.")
        self.assertNotIn("imagine", " ".join(concept.queries()).lower())

    def test_the_action_becomes_a_photographable_query(self) -> None:
        concept = concept_from_rules("Imagine waking up ten years from now.")
        self.assertEqual(concept.queries()[0], "person waking up in bed")

    def test_other_openings_are_stripped_too(self) -> None:
        for opening in ("Picture", "Consider", "Think about", "So imagine"):
            concept = concept_from_rules(f"{opening} waking up in a new city.")
            self.assertIn("waking up", concept.queries()[0])

    def test_the_narration_itself_is_untouched(self) -> None:
        """We strip it from the search, not from the video."""
        line = "Imagine waking up ten years from now."
        self.assertEqual(concept_from_rules(line).source_text, line)

    def test_an_action_outranks_a_domain(self) -> None:
        """A sentence about waking in a computerised future wants a person.

        The domain table would otherwise win on sheer word count and return a
        server rack for a line about a human moment.
        """
        concept = concept_from_rules(
            "Imagine waking up in a world run by computers and software."
        )
        self.assertIn("waking", concept.queries()[0])


class TheRulesAreNotAModelAndDoNotPretendToBe(unittest.TestCase):
    def test_a_sentence_the_rules_cannot_read_still_yields_a_query(self) -> None:
        """Weak is acceptable; empty is not — the ladder needs something to try.

        "You don't open dozens of apps" produces `don't open`, which is a poor
        search and an honest one. This is the case the language model earns its
        cost on, and the rules' job is to not leave the ladder with nothing.
        """
        concept = concept_from_rules("You don't open dozens of apps to get things done.")
        self.assertTrue(concept.queries())


class SafetyScoresAWholeProject(unittest.TestCase):
    def test_a_clean_script_scores_full_marks(self) -> None:
        from vtv.pipeline.visual_intent import report_for

        report = report_for([
            concept_from_rules("Solar panels went up across the estate."),
            concept_from_rules("Revenue grew from 3 million to 47 million."),
        ])
        self.assertEqual(report.total, 100)
        self.assertTrue(report.is_clean)

    def test_the_total_is_a_mean_not_a_minimum(self) -> None:
        """One flagged line in forty is not the same as forty in forty."""
        from vtv.pipeline.visual_intent import report_for

        clean = concept_from_rules("Solar panels went up across the estate.")
        flagged = concept_from_rules("The report described the torture in detail.")
        mostly_clean = report_for([clean] * 9 + [flagged])
        all_flagged = report_for([flagged] * 10)
        self.assertGreater(mostly_clean.total, all_flagged.total)
        self.assertEqual(all_flagged.total, 40)

    def test_it_says_what_happened_and_who_is_responsible(self) -> None:
        from vtv.pipeline.visual_intent import SafetyReport, report_for

        report = report_for([
            concept_from_rules("Solar panels went up across the estate."),
            concept_from_rules("The report described the torture in detail."),
        ])
        self.assertIn("1 of 2 sections", report.headline)
        self.assertIn("responsible for what you publish", SafetyReport.RESPONSIBILITY)
        self.assertTrue(report.reasons)

    def test_an_empty_project_is_clean_rather_than_zero(self) -> None:
        from vtv.pipeline.visual_intent import report_for

        self.assertEqual(report_for([]).total, 100)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RuleQueriesAreAFallbackNotAnAddition(unittest.TestCase):
    """They used to be concatenated behind the model's, and they won.

    The rule-written queries are built from bare words of the narration. Behind
    four considered queries they are not a fallback, they are pollution — and
    because a short query is easier to match completely than a long one, they
    outranked the queries they were supposed to stand in for. One render
    searched `fascinating` and put a 1904 stereoscopic card of elephants under
    a sentence about the most valuable skill changing.
    """

    def test_no_single_word_query_is_ever_generated(self) -> None:
        """One word out of a sentence is not a description of a picture, and
        asking for one costs an HTTP round trip to be told so."""
        for line in [
            "And that creates a fascinating shift in what matters.",
            "Your AI checks your schedule and prepares your work.",
            "Businesses could have AI agents managing sales operations.",
        ]:
            for query in concept_from_rules(line).queries():
                with self.subTest(line=line[:30], query=query):
                    # Content words, not raw words: "Your AI" is two words and
                    # one of them is a pronoun, and the selector scores it the
                    # same as the bare "AI" it effectively is.
                    self.assertGreaterEqual(len(content_words(query)), 2, query)

    def test_the_models_queries_stand_alone_when_there_are_enough(self) -> None:
        from vtv.pipeline.visual_intent import _with_fallback

        chosen = ["person waking up in bed", "sunrise through window"]
        self.assertEqual(_with_fallback(chosen, ("fascinating", "plan")), chosen)

    def test_the_rules_fill_in_when_the_model_gave_almost_nothing(self) -> None:
        """That is what they are for, and a deployment with no language model
        still has to search for something."""
        self.assertEqual(
            _with_fallback(["one thing"], ("a rule query",)),
            ["one thing", "a rule query"],
        )
        self.assertEqual(_with_fallback([], ("a rule query",)), ["a rule query"])


class ALongScriptIsStillDirected(unittest.TestCase):
    """A thirty-minute narration is about 740 lines.

    One call for all of them needs roughly 440 000 output tokens against a
    16 000 ceiling. The model answers for the first two dozen lines, stops, and
    every remaining line quietly falls back to rules — a video whose first
    minute is directed and whose other twenty-nine are not, with nothing in the
    log to say so.
    """

    def read(self, count: int):  # type: ignore[no-untyped-def]
        import asyncio

        from vtv.contracts.base import IdPrefix, new_id
        from vtv.contracts.errors import Status
        from vtv.observability.events import EventSink
        from vtv.pipeline.visual_intent import ConceptReader

        sizes: list[int] = []

        class Result:
            status = Status.READY
            structured_output: dict[str, object] = {}

        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                lines = request.params.input_json["lines"]
                sizes.append(len(lines))
                result = Result()
                result.structured_output = {
                    "visuals": [
                        {
                            "index": line["index"],
                            "subject": "a considered subject",
                            "search_queries": ["a considered query"],
                            "image_prompt": "a considered picture",
                        }
                        for line in lines
                    ]
                }
                return result

        texts = [f"Sentence number {i} about computers." for i in range(count)]
        reader = ConceptReader(events=EventSink(), router=Router())
        concepts = asyncio.run(
            reader.read_many(
                texts,
                organisation_id=new_id(IdPrefix.PROJECT),
                project_id=new_id(IdPrefix.PROJECT),
            )
        )
        return sizes, concepts

    def test_every_line_ends_up_directed(self) -> None:
        """Read or inherited — never fallen back to rules.

        The rules produce queries like "don't open" and "knowing matters" from
        bare words of the narration. A line that inherits its passage's visual
        idea has a considered one; a line that falls back has narration
        fragments. On a 737-line script the old behaviour was one model call
        that answered 26 lines and dropped 711 of them.
        """
        _sizes, concepts = self.read(737)
        self.assertEqual(len(concepts), 737)
        fell_back = [c for c in concepts if c.source == "rules"]
        self.assertEqual(fell_back, [], f"{len(fell_back)} lines fell back to rules")

    def test_a_long_script_reads_a_sample_not_every_line(self) -> None:
        """Intelligence per minute falls as the video grows. 737 lines is about
        forty minutes, which is read one line in three."""
        from vtv.pipeline.intelligence import Budget

        _sizes, concepts = self.read(737)
        budget = Budget.of(737)
        read = sum(1 for c in concepts if c.source == "llm")
        self.assertEqual(read, budget.read)
        self.assertLess(read, 737)

    def test_an_inherited_line_keeps_its_own_words(self) -> None:
        """It inherits the visual idea, not the sentence. A shot showing the
        wrong caption would be a much worse bug than a repeated photograph."""
        _sizes, concepts = self.read(100)
        for index, concept in enumerate(concepts):
            self.assertIn(f"number {index} ", concept.source_text)

    def test_no_call_asks_for_more_than_it_can_receive(self) -> None:
        from vtv.pipeline.visual_intent import BATCH_LINES

        sizes, _ = self.read(737)
        self.assertTrue(all(size <= BATCH_LINES for size in sizes), sizes)

    def test_the_lines_stay_in_order(self) -> None:
        """A reader that returned concepts out of order would attach every
        line's visual idea to a different line."""
        _sizes, concepts = self.read(13)
        for index, concept in enumerate(concepts):
            self.assertIn(f"number {index} ", concept.source_text)

    def test_a_short_script_is_still_one_call(self) -> None:
        sizes, _ = self.read(13)
        self.assertEqual(sizes, [13])


class TheReaderAlsoDecidesTheKind(unittest.TestCase):
    """The Visual Director's judgement, in the call that was already happening.

    The reader is already reading every line **with the lines around it** —
    which is the context the decision needs and the one a per-line rule cannot
    have. Asking it what kind of visual explains the line costs output tokens,
    not a second pass over the script.
    """

    def read(self, treatment: str):  # type: ignore[no-untyped-def]
        import asyncio

        from vtv.contracts.base import IdPrefix, new_id
        from vtv.contracts.errors import Status
        from vtv.observability.events import EventSink
        from vtv.pipeline.visual_intent import ConceptReader

        class Result:
            status = Status.READY
            structured_output = {
                "visuals": [
                    {
                        "index": 0,
                        "subject": "a considered subject",
                        "search_queries": ["a photographable query"],
                        "image_prompt": "a considered picture",
                        "treatment": treatment,
                    }
                ]
            }

        class Router:
            async def generate(self, request):  # type: ignore[no-untyped-def]
                del request
                return Result()

        reader = ConceptReader(events=EventSink(), router=Router())
        return asyncio.run(
            reader.read_many(
                ["An abstract idea about the future."],
                organisation_id=new_id(IdPrefix.PROJECT),
                project_id=new_id(IdPrefix.PROJECT),
            )
        )[0]

    def test_the_treatment_survives_the_round_trip(self) -> None:
        self.assertEqual(self.read("typography").treatment, "typography")

    def test_it_is_normalised(self) -> None:
        """A model that shouts is still answering."""
        self.assertEqual(self.read("  GENERATED_IMAGE  ").treatment, "generated_image")

    def test_a_reader_that_says_nothing_leaves_it_empty(self) -> None:
        """Empty means "no suggestion", and the director's own rules decide."""
        self.assertEqual(self.read("").treatment, "")

    def test_the_director_honours_it(self) -> None:
        """The whole point of the field. A line the rules would have sent to the
        commons goes to type because the reader, which saw the surrounding
        lines, judged that no photograph would add anything."""
        from vtv.pipeline.treatment import Brief, Capabilities, Treatment, decide

        concept = self.read("typography")
        decision = decide(
            Brief(unit_id="v", narration=concept.source_text, seconds=5.0),
            Capabilities(can_search_media=True, can_generate_image=True),
            suggested=concept.treatment,
        )
        self.assertIs(decision.treatment, Treatment.TYPOGRAPHY)
        self.assertEqual(decision.decided_by, "director")

    def test_a_nonsense_treatment_does_not_reach_the_director(self) -> None:
        from vtv.pipeline.treatment import Brief, Capabilities, decide

        concept = self.read("interpretive dance")
        decision = decide(
            Brief(unit_id="v", narration=concept.source_text, seconds=5.0),
            Capabilities(can_search_media=True),
            suggested=concept.treatment,
        )
        self.assertEqual(decision.decided_by, "rules")
