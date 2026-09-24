"""Projects name themselves.

`Project.title` was on the contract from the start and nothing ever wrote to
it, so every record in the database reads `title: None` and the projects list
is a column of identifiers. These tests are the statement of what a project is
now called, and — more importantly — of what is never overwritten.
"""

from __future__ import annotations

import unittest

from vtv.pipeline.naming import MAX_TITLE_CHARS, derive, ensure


class Holder:
    def __init__(self, title: str | None = None) -> None:
        self.title = title


class AProjectIsNamedAfterWhatItIsAbout(unittest.TestCase):
    def test_a_plain_opening_is_the_title(self) -> None:
        self.assertEqual(
            derive("Revenue rose from 12 million to 48 million in three years."),
            "Revenue rose from 12 million to 48 million in three years",
        )

    def test_throat_clearing_is_removed(self) -> None:
        """"So today I want to talk about" is how people start talking and says
        nothing about the video. A project called that is worse than one called
        by its id, because it looks like content and is not."""
        self.assertEqual(
            derive(
                "So today I want to talk about the future of artificial "
                "intelligence and what it means for work."
            ),
            "The future of artificial intelligence and what it means for work",
        )

    def test_it_moves_on_when_a_whole_sentence_is_preamble(self) -> None:
        """The bug the first version shipped: "Welcome back!" strips to "!",
        which is not empty, so the emptiness check passed and the project was
        named "!". Punctuation is not preamble and it is not a title."""
        self.assertEqual(
            derive(
                "Welcome back! In this video we are going to explore how "
                "tariffs reshape supply chains across the Pacific."
            ),
            "How tariffs reshape supply chains across the Pacific",
        )

    def test_an_acronym_keeps_its_capitals(self) -> None:
        self.assertEqual(derive("NASA budget cuts explained."), "NASA budget cuts explained")

    def test_a_shouted_script_is_not_a_shouted_title(self) -> None:
        self.assertEqual(
            derive("THE HISTORY OF THE TRANSISTOR"), "The history of the transistor"
        )

    def test_a_long_opening_is_cut_on_a_word(self) -> None:
        """A title ending mid-word reads as a bug rather than as truncation."""
        title = derive(
            "The transistor was invented at Bell Laboratories in 1947 by John "
            "Bardeen, Walter Brattain and William Shockley, and it changed "
            "everything about how machines compute."
        )
        self.assertLessEqual(len(title), MAX_TITLE_CHARS + 1)
        self.assertTrue(title.endswith("…"))
        self.assertNotIn("  ", title)

    def test_nothing_to_go_on_is_said_plainly(self) -> None:
        for text in ("", "   ", "\n\n"):
            with self.subTest(text=repr(text)):
                self.assertEqual(derive(text), "Untitled project")

    def test_it_never_returns_bare_punctuation(self) -> None:
        """The class of failure worth guarding as a class, not a case."""
        for text in ("...", "!!!", "— — —", "Hello.", "So.", "?"):
            with self.subTest(text=text):
                title = derive(text)
                self.assertTrue(
                    sum(c.isalnum() for c in title) >= 2,
                    f"{text!r} produced {title!r}",
                )


class ANameTheUserChoseIsNeverOverwritten(unittest.TestCase):
    """The whole reason `ensure` exists rather than a bare assignment. It is
    called on every script upload, so the guard has to live in one place rather
    than in every caller's memory."""

    def test_a_project_with_no_name_gets_one(self) -> None:
        project = Holder()
        self.assertTrue(ensure(project, "The history of the transistor."))
        self.assertEqual(project.title, "The history of the transistor")

    def test_a_project_the_user_named_is_left_alone(self) -> None:
        project = Holder("Q3 investor update")
        self.assertFalse(ensure(project, "Something else entirely."))
        self.assertEqual(project.title, "Q3 investor update")

    def test_a_second_upload_does_not_rename(self) -> None:
        project = Holder()
        ensure(project, "The history of the transistor.")
        ensure(project, "A completely different script about tariffs.")
        self.assertEqual(project.title, "The history of the transistor")

    def test_an_empty_title_counts_as_no_name(self) -> None:
        """Clearing the name through the API sets it to `None`, and a cleared
        project should be re-derivable rather than stuck with a bad guess."""
        project = Holder("")
        self.assertTrue(ensure(project, "Tariffs and supply chains."))
        self.assertEqual(project.title, "Tariffs and supply chains")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
