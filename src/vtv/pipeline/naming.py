"""What to call a project the user did not name.

## Why this exists

`Project.title` has been on the contract since the beginning and nothing ever
wrote to it. Every project in the database has `title: None`, so the projects
list is a column of identifiers and the studio's title bar is empty — which is
survivable with one project and useless with nine, and "which of these is the
one about tariffs" is a question the product could not answer about its own
records.

## Why it is derived rather than demanded

A dialog asking for a name before the user has done anything is a form standing
between somebody and the thing they came to do, and they will type "test". The
script already says what the video is about; the first sentence of a narration
is, almost by construction, its subject. So a project names itself, and the
user renames it if we guessed badly.

That ordering matters: **derive, then allow renaming** — never the reverse. A
name the user typed is a decision and is never overwritten, which is why
`derive` is only ever called on a project whose title is still empty.
"""

from __future__ import annotations

import re

#: Longest derived title. `Project.title` permits 200; a list column does not.
MAX_TITLE_CHARS = 72

#: Openers that carry no information about the subject. A title of "So today
#: I want to talk about" is worse than the project id, because it looks like
#: content and is not.
_PREAMBLE = re.compile(
    r"^(?:so|and|but|now|okay|ok|right|well|hello|hi|hey|today|"
    r"in this (?:video|episode|talk)|welcome(?: to| back)?|"
    r"i(?:'m| am) going to|i want to|let(?:'s| us)|we(?:'re| are) going to)\b"
    r"[\s,:-]*",
    re.I,
)

#: Filler that survives the opener strip and still says nothing.
_TRAILING = re.compile(r"\b(?:talk about|discuss|explore|look at|cover)\b[\s,:-]*", re.I)

_WHITESPACE = re.compile(r"\s+")

#: Everything a title may not begin or end with. Punctuation included, which is
#: the detail the first version missed: stripping "welcome back" off "Welcome
#: back!" leaves "!", which is not empty and is not a name.
_EDGES = " \t,;:.!?-—–\"'“”‘’()[]"


def _trim(text: str) -> str:
    return text.strip(_EDGES).strip()


def _is_substantive(text: str) -> bool:
    """Whether this is a name rather than leftovers.

    Two letters is the bar. It admits "AI" — a legitimate title for a script
    that opens by naming its subject — and rejects the punctuation and single
    stray characters that stripping preamble leaves behind.
    """
    return sum(character.isalnum() for character in text) >= 2


def _strip_preamble(sentence: str) -> str:
    """Remove stacked openers. "So today, I want to talk about X" has three."""
    sentence = _trim(sentence)
    for _ in range(4):
        stripped = _trim(_TRAILING.sub("", _PREAMBLE.sub("", sentence)))
        if stripped == sentence:
            break
        sentence = stripped
    return sentence


def derive(text: str, *, fallback: str = "Untitled project") -> str:
    """A name for a project, from the opening of its script.

    Takes the first sentence, strips the throat-clearing off the front, and
    truncates on a word boundary. Deliberately not a model call: naming happens
    the moment a script is pasted, on every project, and a title is not worth a
    round trip or a cent — nor worth failing a script upload for when a provider
    is down.
    """
    cleaned = _WHITESPACE.sub(" ", (text or "").strip())
    if not cleaned:
        return fallback

    # Walk sentences until one survives the strip with something in it.
    #
    # "Welcome back! In this video we are going to explore how tariffs reshape
    # supply chains" is entirely preamble for its first sentence and entirely
    # subject for its second. Taking the first sentence and giving up produced
    # a project called "!" — punctuation is not preamble and not a title, and
    # an emptiness check that only looks for `""` does not catch it.
    sentence = ""
    for candidate in re.split(r"(?<=[.!?])\s+", cleaned)[:4]:
        sentence = _strip_preamble(candidate)
        if _is_substantive(sentence):
            break
        sentence = ""

    if not sentence:
        # Every opening was preamble. The unstripped first sentence is a worse
        # title but a real one, and better than pretending we have none.
        sentence = _trim(re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0])
    if not _is_substantive(sentence):
        return fallback

    if len(sentence) > MAX_TITLE_CHARS:
        # Cut on a word boundary. A title ending mid-word reads as a bug.
        head = sentence[: MAX_TITLE_CHARS + 1]
        cut = head.rfind(" ")
        sentence = (head[:cut] if cut > MAX_TITLE_CHARS // 2 else head[:-1]).rstrip(
            " ,;:-"
        )
        sentence = f"{sentence}…"

    # Sentence case, and only when the source was not deliberately capitalised
    # — an all-caps script should not become an all-caps project name, but
    # "NASA budget cuts" must keep its acronym.
    if sentence.isupper():
        sentence = sentence.capitalize()
    elif sentence[:1].islower():
        # Stripping "So today I want to talk about" leaves a title starting
        # mid-sentence and therefore lowercase. Only the first character is
        # touched — title-casing the whole thing would turn "the future of AI"
        # into "The Future Of Ai".
        sentence = sentence[0].upper() + sentence[1:]
    return _trim(sentence[: MAX_TITLE_CHARS + 1])


def ensure(project: object, text: str) -> bool:
    """Give a project a name if it has none. Returns whether it changed.

    The guard is the point: a title the user typed is never overwritten, so
    this is safe to call on every script upload and every revision without
    anyone having to remember which of those is the first one.
    """
    if getattr(project, "title", None):
        return False
    derived = derive(text)
    if not derived:
        return False
    project.title = derived  # type: ignore[attr-defined]
    return True


__all__ = ["MAX_TITLE_CHARS", "derive", "ensure"]
