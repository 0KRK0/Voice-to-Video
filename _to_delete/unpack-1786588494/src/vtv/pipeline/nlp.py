"""Deterministic language analysis.

Rule 15 of the engineering brief: prefer deterministic software for anything that
does not require AI. A surprising amount of semantic extraction falls into that
category — dates, quantities, proper nouns, discourse cues — and doing it with
rules is faster, free, reproducible, and cannot hallucinate.

This module is therefore not a stand-in for the language model. It is:

* the **baseline** the model layer must beat, measured in Stage 16;
* the **fallback** when no model is reachable, so the pipeline never stops;
* the **pre-processor** that gives the model real structure to work from rather
  than a wall of text.

Everything here is English-first and honest about it. Other languages need
their own lexicons, and pretending otherwise would be worse than saying so.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field

from vtv.contracts.semantics import EntityType, SemanticIntent

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

#: Words that start sentences and are capitalised for that reason alone. Without
#: this list every sentence-initial "The" becomes a proper noun.
_SENTENCE_STARTERS = {
    "the", "this", "that", "these", "those", "it", "its", "they", "we", "i",
    "you", "he", "she", "there", "here", "and", "but", "so", "if", "when",
    "while", "after", "before", "because", "however", "then", "now", "today",
    "let", "a", "an", "in", "on", "at", "for", "with", "by", "from", "to",
    "what", "why", "how", "who", "which", "every", "each", "most", "many",
    "some", "all", "one", "two", "three", "first", "second", "third", "next",
    "within", "about", "over", "under", "between", "during", "through",
    "imagine", "think", "consider", "look", "take", "say", "suppose",
    "everything", "everyone", "everybody", "something", "someone", "somebody",
    "anything", "anyone", "nothing", "nobody", "another", "both", "either",
    "neither", "such", "much", "more", "less", "later", "eventually",
}

#: Organisation and place suffixes that identify an entity's type once we have
#: found it. Ordered longest-first so "Labs" does not shadow "Laboratories".
_ORG_MARKERS = (
    "laboratories", "university", "institute", "corporation", "foundation",
    "company", "college", "school", "labs", "inc", "ltd", "llc", "gmbh",
    "agency", "department", "ministry", "committee", "council", "society",
)
_PLACE_MARKERS = (
    "island", "mountain", "river", "valley", "ocean", "sea", "desert", "city",
    "county", "province", "state", "kingdom", "republic", "bay", "gulf",
)

#: Well-known place names worth recognising without a gazetteer. Deliberately
#: short: a wrong guess about a place produces a wrong map, so this list holds
#: only names that are unambiguous.
_KNOWN_PLACES = {
    "africa", "america", "antarctica", "asia", "australia", "europe",
    "atlantic", "pacific", "arctic", "britain", "england", "scotland",
    "wales", "ireland", "france", "germany", "spain", "italy", "greece",
    "china", "japan", "india", "russia", "brazil", "canada", "mexico",
    "egypt", "kenya", "nigeria", "london", "paris", "berlin", "rome",
    "tokyo", "beijing", "delhi", "moscow", "cairo", "sydney", "toronto",
    "chicago", "boston", "seattle", "mars", "earth", "venus", "jupiter",
}

#: Discourse cues that reveal what the speaker is doing. Checked as phrases at
#: the start of a unit, or anywhere for the stronger markers.
_INTENT_CUES: list[tuple[SemanticIntent, tuple[str, ...]]] = [
    (SemanticIntent.QUESTION, ("?",)),
    (SemanticIntent.DEFINITION, (
        " is a ", " is an ", " is the ", " are a ", " means ", " refers to ",
        " is defined as ", " is called ", " known as ", " what we call ",
    )),
    (SemanticIntent.COMPARISON, (
        " than ", " compared to ", " versus ", " whereas ", " unlike ",
        " similar to ", " the difference between ", " on the other hand ",
    )),
    (SemanticIntent.CAUSATION, (
        " because ", " therefore ", " as a result ", " which meant ",
        " leads to ", " led to ", " causes ", " caused ", " so that ",
        " consequently ", " that is why ",
    )),
    (SemanticIntent.PROCESS, (
        " first, ", " then ", " next, ", " after that ", " finally ",
        " step ", " begins by ", " starts by ", " the process ",
    )),
    (SemanticIntent.EXAMPLE, (
        " for example ", " for instance ", " such as ", " imagine ",
        " think of ", " consider ", " take the case ", " like a ", " like the ",
    )),
    (SemanticIntent.CONCLUSION, (
        " in short ", " to sum up ", " in conclusion ", " the point is ",
        " that is why ", " ultimately ", " in the end ", " which is why ",
    )),
    (SemanticIntent.ENUMERATION, (
        " there are three ", " there are two ", " there are four ",
        " several ", " a number of ", " the following ",
    )),
    (SemanticIntent.TRANSITION, (
        " now, ", " so, ", " anyway ", " moving on ", " let me tell you ",
        " let's talk about ", " but first ",
    )),
]

#: Tokens that carry no content. A unit made *entirely* of these is filler.
_FILLER_TOKENS = {
    "um", "uh", "er", "ah", "okay", "ok", "right", "so", "well", "yeah", "yep",
    "hmm", "mm", "like", "anyway", "alright", "sure", "you", "know", "i",
    "mean",
}


def is_filler(text: str) -> bool:
    """True when a stretch of speech is entirely hesitation and no content."""
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    return bool(tokens) and all(token in _FILLER_TOKENS for token in tokens)

#: Number words we can turn into values. Beyond a certain size, speakers say
#: "one billion" rather than "1000000000", and a chart needs the number.
_NUMBER_WORDS: dict[str, float] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_SCALES: dict[str, float] = {
    "hundred": 1e2, "thousand": 1e3, "million": 1e6, "billion": 1e9,
    "trillion": 1e12,
}

#: Verb phrases we can turn into relations, mapped to a snake_case predicate.
#: Ordered longest-first so "was invented at" wins over "invented".
_RELATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:was|were|is|are)\s+invented\s+(?:at|in|by)\b", re.I), "invented_at"),
    (re.compile(r"\b(?:was|were|is|are)\s+(?:developed|created|built|founded)\s+(?:at|in|by)\b", re.I), "created_by"),
    (re.compile(r"\b(?:replaced|superseded|displaced)\b", re.I), "replaced"),
    (re.compile(r"\b(?:led\s+to|caused|resulted\s+in)\b", re.I), "led_to"),
    (re.compile(r"\b(?:is|are|was|were)\s+part\s+of\b", re.I), "part_of"),
    (re.compile(r"\b(?:contains|includes|comprises)\b", re.I), "contains"),
    (re.compile(r"\b(?:uses|relies\s+on|depends\s+on)\b", re.I), "depends_on"),
    (re.compile(r"\b(?:became|turned\s+into|evolved\s+into)\b", re.I), "became"),
    (re.compile(r"\b(?:discovered|found)\b", re.I), "discovered"),
]

_STOPWORDS = set(
    ["a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those", "of", "in", "on", "at", "to", "for", "with", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "they", "them", "he", "she", "we", "you", "i", "not", "no", "so", "such", "very", "more", "most", "much", "many", "some", "any", "all", "each", "every", "other", "another", "own", "same", "just", "also", "only", "even", "still", "yet", "about", "over", "under", "into", "out", "up", "down", "off", "again", "once", "here", "there", "when", "where", "why", "how", "what", "which", "who", "whom", "whose", "can", "could", "would", "should", "will", "shall", "may", "might", "must", "do", "does", "did", "done", "have", "has", "had", "having", "get", "got", "make", "made", "like", "well", "now", "new", "one", "two", "three", "first", "second", "next", "last", "long", "time", "way", "thing", "things", "people", "world", "year", "years", "day", "days", "hundred", "thousand", "million", "billion", "trillion", "percent"]
)

#: Comparatives and evaluative adjectives. Without a part-of-speech tagger these
#: are the words most likely to be mistaken for subjects — "smaller" is repeated
#: three times in a comparison and looks exactly like a topic to a frequency
#: counter. A short explicit list is more honest than a suffix rule that would
#: also swallow "computer" and "water".
_ADJECTIVES = set(
    ["smaller", "bigger", "larger", "greater", "lesser", "faster", "slower", "better", "worse", "cheaper", "higher", "lower", "stronger", "weaker", "heavier", "lighter", "shorter", "longer", "wider", "narrower", "older", "newer", "younger", "harder", "softer", "simpler", "easier", "richer", "poorer", "deeper", "closer", "further", "earlier", "quicker", "safer", "cleaner", "smallest", "biggest", "largest", "greatest", "fastest", "slowest", "best", "worst", "cheapest", "highest", "lowest", "strongest", "weakest", "efficient", "powerful", "important", "significant", "different", "similar", "possible", "impossible", "available", "common", "rare", "complex", "simple", "basic", "advanced", "modern", "ancient", "tiny", "huge", "massive", "enormous", "entire", "whole", "single", "multiple"]
)

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]+")
_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_NUMERIC = re.compile(
    r"(?P<value>\d[\d,]*\.?\d*)\s*(?P<scale>hundred|thousand|million|billion|trillion)?"
    r"\s*(?P<unit>percent|%|dollars?|euros?|pounds?|years?|months?|days?|hours?|"
    r"minutes?|seconds?|kilometers?|kilometres?|miles?|meters?|metres?|feet|"
    r"degrees?|people|users?|times)?",
    re.I,
)
_WORD_NUMBER = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS) + r")\s+(hundred|thousand|million|billion|trillion)\b",
    re.I,
)
_PROPER = re.compile(r"\b([A-Z][A-Za-z0-9&'’.-]*(?:\s+[A-Z][A-Za-z0-9&'’.-]*)*)\b")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class MentionedEntity:
    name: str
    type: EntityType
    #: How many times it was mentioned; the salience signal.
    count: int = 1


@dataclass
class MentionedQuantity:
    value: float
    unit: str | None = None
    of_what: str | None = None
    at: str | None = None


@dataclass
class Analysis:
    intent: SemanticIntent
    entities: list[MentionedEntity] = field(default_factory=list)
    quantities: list[MentionedQuantity] = field(default_factory=list)
    keyphrases: list[str] = field(default_factory=list)
    predicates: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def classify_intent(text: str) -> SemanticIntent:
    """What is the speaker doing with this stretch of speech?

    Intent predicts visual form far more reliably than topic does, which is why
    it is extracted first and why the cue list is ordered by specificity.
    """
    stripped = text.strip()
    if not stripped or is_filler(stripped):
        return SemanticIntent.FILLER
    padded = f" {stripped.lower()} "

    if stripped.endswith("?"):
        return SemanticIntent.QUESTION

    for intent, cues in _INTENT_CUES:
        if intent is SemanticIntent.QUESTION:
            continue
        if any(cue in padded for cue in cues):
            if intent is SemanticIntent.TRANSITION and _carries_content(stripped):
                # "Let me tell you about the most important invention of the
                # century" opens with a transition cue but states the thesis.
                # Demoting it to filler would throw away the opening shot.
                break
            return intent

    if _YEAR.search(stripped) or _has_month(padded):
        return SemanticIntent.EVENT_NARRATION
    if extract_quantities(stripped):
        return SemanticIntent.NUMERIC_FACT
    return SemanticIntent.CLAIM


def _carries_content(text: str) -> bool:
    """Whether a stretch of speech says something, beyond signposting."""
    content = [
        token
        for token in _TOKEN.findall(text.lower())
        if token not in _STOPWORDS and token not in _FILLER_TOKENS
    ]
    return len(text) > 60 and len(content) >= 4


def _has_month(padded_lower: str) -> bool:
    return any(f" {month} " in padded_lower for month in MONTHS)


def normalise_term(word: str) -> str:
    """Crudest possible stemming: enough to unify singular and plural.

    "transistor" and "transistors" are the same subject, and a viewer would be
    baffled by a diagram that treated them as two. Full morphology is a research
    project; this handles the case that actually occurs in spoken explanation.
    """
    lowered = word.lower().strip()
    for suffix, replacement in (("ies", "y"), ("sses", "ss"), ("ses", "s"), ("s", "")):
        if len(lowered) > 4 and lowered.endswith(suffix):
            return lowered[: -len(suffix)] + replacement
    return lowered


def salient_terms(text: str, *, min_count: int = 2, limit: int = 24) -> dict[str, str]:
    """Content words repeated across a whole transcript.

    Extraction runs clause by clause, so a subject mentioned once per sentence
    never accumulates enough evidence locally to look important. Running this
    over the full text first is what lets "transistor" — lowercase, ordinary,
    and the entire point of the recording — be recognised as the subject.

    Returns ``{normalised_term: display_form}``.
    """
    tokens = [t.lower() for t in _TOKEN.findall(text)]

    def is_content(word: str) -> bool:
        return (
            len(word) >= 4
            and word not in _STOPWORDS
            and word not in _SENTENCE_STARTERS
            and word not in _ADJECTIVES
            and word not in _FILLER_TOKENS
        )

    unigrams: dict[str, int] = {}
    display: dict[str, str] = {}
    for word in tokens:
        if not is_content(word):
            continue
        stem = normalise_term(word)
        unigrams[stem] = unigrams.get(stem, 0) + 1
        display.setdefault(stem, word)

    # Collocations. "vacuum tube" is one subject, and counting its two halves
    # separately produces two entities and, downstream, two unrelated visuals.
    bigrams: dict[str, int] = {}
    bigram_display: dict[str, str] = {}
    for first, second in itertools.pairwise(tokens):
        if not (is_content(first) and is_content(second)):
            continue
        stem = f"{normalise_term(first)} {normalise_term(second)}"
        bigrams[stem] = bigrams.get(stem, 0) + 1
        bigram_display.setdefault(stem, f"{first} {second}")

    chosen: dict[str, str] = {}
    for stem, count in sorted(bigrams.items(), key=lambda i: (-i[1], i[0])):
        if count < min_count:
            continue
        chosen[stem] = bigram_display[stem]
        # The phrase accounts for these mentions; do not also count the parts.
        for part in stem.split():
            if part in unigrams:
                unigrams[part] -= count

    for stem, count in sorted(unigrams.items(), key=lambda i: (-i[1], i[0])):
        if count >= min_count and stem not in chosen:
            chosen[stem] = display[stem]

    return dict(list(chosen.items())[:limit])


def extract_entities(
    text: str, *, vocabulary: dict[str, str] | None = None
) -> list[MentionedEntity]:
    """Proper nouns, dates and notable common nouns.

    Capitalisation is the primary signal, which means sentence-initial words
    have to be filtered — that filter is most of the accuracy of this function.
    """
    found: dict[str, MentionedEntity] = {}

    def add(name: str, entity_type: EntityType) -> None:
        key = name.lower()
        if key in found:
            found[key].count += 1
        else:
            found[key] = MentionedEntity(name=name, type=entity_type)

    # Years and dates.
    for match in _YEAR.finditer(text):
        add(match.group(1), EntityType.DATE)
    lowered = f" {text.lower()} "
    for month in MONTHS:
        if f" {month} " in lowered:
            add(month.capitalize(), EntityType.DATE)

    # Proper-noun phrases.
    for match in _PROPER.finditer(text):
        phrase = match.group(1).strip(" .,;:")
        if not phrase:
            continue
        words = phrase.split()
        # Drop a leading sentence-starter: "The transistor" → "transistor" is
        # handled below as a common noun, but "The Bell Labs" → "Bell Labs".
        if words and words[0].lower() in _SENTENCE_STARTERS:
            words = words[1:]
        if not words:
            continue
        phrase = " ".join(words)
        if len(phrase) < 2 or phrase.lower() in _STOPWORDS:
            continue
        if phrase.isupper() and len(phrase) <= 3:
            add(phrase, EntityType.ORGANIZATION)
            continue
        add(phrase, _classify_proper(phrase))

    # Subjects established across the whole recording, matched here. Phrases
    # are matched first and consume their words, so "vacuum tube" does not also
    # register as "vacuum" and "tube".
    if vocabulary:
        tokens = [t.lower() for t in _TOKEN.findall(text)]
        stems = [normalise_term(t) for t in tokens]
        seen = {normalise_term(name) for name in found}
        consumed: set[int] = set()
        for index in range(len(stems) - 1):
            phrase = f"{stems[index]} {stems[index + 1]}"
            if phrase in vocabulary and phrase not in seen:
                add(vocabulary[phrase], EntityType.CONCEPT)
                seen.add(phrase)
                consumed.update({index, index + 1})
        for index, stem in enumerate(stems):
            if index in consumed or stem in seen:
                continue
            if stem in vocabulary:
                add(vocabulary[stem], EntityType.CONCEPT)
                seen.add(stem)

    return sorted(found.values(), key=lambda e: (-e.count, e.name.lower()))


def _classify_proper(phrase: str) -> EntityType:
    lowered = phrase.lower()
    words = lowered.split()
    if any(marker in words for marker in _ORG_MARKERS):
        return EntityType.ORGANIZATION
    if any(lowered.endswith(marker) for marker in _ORG_MARKERS):
        return EntityType.ORGANIZATION
    if lowered in _KNOWN_PLACES or any(marker in words for marker in _PLACE_MARKERS):
        return EntityType.LOCATION
    # Two capitalised words with no marker is most often a person's name.
    if len(words) == 2 and all(word[:1].isalpha() for word in phrase.split()):
        return EntityType.PERSON
    return EntityType.OTHER


_DEFINITION_SUBJECT = re.compile(
    r"^\s*(?:a|an|the)?\s*([a-z][a-z\s'-]{2,40}?)\s+(?:is|are|means|refers to)\b",
    re.I,
)


def definition_subject(text: str) -> str | None:
    """The thing being defined, for a unit whose intent is DEFINITION.

    "A blockchain is a distributed ledger" names its subject in lowercase, so
    capitalisation-based extraction misses it entirely — and the subject of a
    definition is exactly what the visual has to be about.
    """
    match = _DEFINITION_SUBJECT.match(text.strip())
    if not match:
        return None
    subject = " ".join(match.group(1).split())
    if not subject or subject.lower() in _STOPWORDS:
        return None
    return subject


def extract_quantities(text: str) -> list[MentionedQuantity]:
    """Numbers the speaker asserted, in a form a chart can consume."""
    quantities: list[MentionedQuantity] = []
    year_spans = {match.span() for match in _YEAR.finditer(text)}

    for match in _WORD_NUMBER.finditer(text):
        base = _NUMBER_WORDS[match.group(1).lower()]
        scale = _SCALES[match.group(2).lower()]
        quantities.append(
            MentionedQuantity(value=base * scale, unit=None, of_what=_context(text, match.start()))
        )

    for match in _NUMERIC.finditer(text):
        raw = match.group("value")
        if not raw:
            continue
        if any(start <= match.start() < end for start, end in year_spans):
            continue  # a year is a date, not a measurement
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        scale_word = match.group("scale")
        if scale_word:
            value *= _SCALES[scale_word.lower()]
        unit = match.group("unit")
        quantities.append(
            MentionedQuantity(
                value=value,
                unit=_normalise_unit(unit),
                of_what=_context(text, match.start()),
            )
        )

    # Deduplicate on value, keeping the first context found.
    unique: dict[float, MentionedQuantity] = {}
    for quantity in quantities:
        unique.setdefault(quantity.value, quantity)
    return list(unique.values())


def _normalise_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    lowered = str(unit).lower().rstrip("s")
    return {"%": "percent", "dollar": "usd", "euro": "eur", "pound": "gbp"}.get(
        lowered, lowered
    )


def _context(text: str, index: int, window: int = 48) -> str | None:
    """A short slice around a number, used as its label on a chart."""
    tail = text[index : index + window].strip()
    return tail or None


def extract_predicates(text: str) -> list[str]:
    """Relation predicates present in a stretch of text."""
    return [
        predicate
        for pattern, predicate in _RELATION_PATTERNS
        if pattern.search(text)
    ]


def keyphrases(text: str, limit: int = 6) -> list[str]:
    """The words most worth putting on screen."""
    counts: dict[str, int] = {}
    for token in _TOKEN.findall(text):
        lowered = token.lower()
        if lowered in _STOPWORDS or len(lowered) < 4:
            continue
        counts[lowered] = counts.get(lowered, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:limit]]


def analyse(text: str, *, vocabulary: dict[str, str] | None = None) -> Analysis:
    """Everything the deterministic layer can say about one stretch of speech."""
    intent = classify_intent(text)
    entities = extract_entities(text, vocabulary=vocabulary)
    if intent is SemanticIntent.DEFINITION:
        subject = definition_subject(text)
        if subject and not any(e.name.lower() == subject.lower() for e in entities):
            entities.insert(0, MentionedEntity(name=subject, type=EntityType.CONCEPT, count=2))
    return Analysis(
        intent=intent,
        entities=entities,
        quantities=extract_quantities(text),
        keyphrases=keyphrases(text),
        predicates=extract_predicates(text),
    )


def summarise(sentences: list[str], limit: int = 3) -> str:
    """Extractive summary: the sentences densest in salient words.

    Not abstractive and not pretending to be. It exists so that a scene graph
    has a through-line to check itself against even with no model available.
    """
    if not sentences:
        return ""
    weights: dict[str, int] = {}
    for sentence in sentences:
        for token in _TOKEN.findall(sentence.lower()):
            if token in _STOPWORDS or len(token) < 4:
                continue
            weights[token] = weights.get(token, 0) + 1

    def score(sentence: str) -> float:
        tokens = [t for t in _TOKEN.findall(sentence.lower()) if t not in _STOPWORDS]
        if not tokens:
            return 0.0
        total: int = sum(weights.get(token, 0) for token in tokens)
        # Normalised by sqrt(length) so a long sentence does not win on volume.
        return total / math.sqrt(len(tokens))

    ranked = sorted(range(len(sentences)), key=lambda i: -score(sentences[i]))
    chosen = sorted(ranked[:limit])
    return " ".join(sentences[i].strip() for i in chosen)


#: Entity types that are never the subject of a recording. A year is when
#: something happened, not what the recording is about.
_NON_TOPIC_TYPES = {EntityType.DATE, EntityType.QUANTITY}


def topic_of(entities: list[MentionedEntity], fallback: str = "") -> str:
    """The most salient non-incidental entity is a decent guess at the subject."""
    candidates = [e for e in entities if e.type not in _NON_TOPIC_TYPES] or list(entities)
    if not candidates:
        return fallback
    ranked = sorted(candidates, key=lambda e: (-e.count, len(e.name)))
    return ranked[0].name


__all__ = [
    "Analysis",
    "MentionedEntity",
    "MentionedQuantity",
    "analyse",
    "classify_intent",
    "definition_subject",
    "extract_entities",
    "extract_predicates",
    "extract_quantities",
    "is_filler",
    "keyphrases",
    "normalise_term",
    "salient_terms",
    "summarise",
    "topic_of",
]
