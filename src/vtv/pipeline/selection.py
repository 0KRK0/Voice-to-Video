"""Choosing between candidate visuals, instead of taking the first one that loads.

## The defect this exists to fix

`AssetResolver` searched the commons, walked the results, and returned the first
candidate whose licence was acceptable and whose bytes downloaded. Nothing in
that sentence mentions the picture. Relevance was whatever the search engine
happened to rank first, and the search engines are keyword engines over
volunteer-written filenames, so first place is frequently nonsense.

What that shipped, from one real 75-second render:

| the line said | the query asked for | what the video showed |
|---|---|---|
| "letting intelligent agents figure out the steps" | person talking to AI | a cropped portrait of a named stranger at a conference |
| "systems to accomplish it" | person directing AI | a gold military rank insignia |
| "you don't open dozens of apps" | person clicking on computer | a litter-collecting event at Hof railway station |

None of these is a bug in the search, the licence check, the fetch, or the
render. Every stage did exactly what it was written to do. The missing stage is
the one that asks **whether the picture is about the thing**.

## Two questions, two stages, two very different prices

Relevance turns out to be two separate questions, and conflating them is what
makes people reach for a model when arithmetic would do.

**Did the search engine answer its own query?** — free, local, and decisive.
"Müllsammelaktion am Hofer Hauptbahnhof" shares no word with "person clicking on
computer"; a keyword engine returned it anyway. This needs no intelligence, only
a comparison, and it removes most of the table above at zero cost and zero
latency.

Note carefully what the comparison is against: the **query**, not the sentence.
The query is already the considered visual idea — "sunrise through window" for
"imagine waking up ten years from now" is a good and deliberate metaphor that
shares not one word with the line it illustrates. Screening candidates against
the narration would throw that away. Screening them against the query asks the
narrower, answerable question: *did the engine return what was asked for.*

**Is what was asked for actually a good picture of the idea?** — this one needs
judgement. "Skills for the Future" is a genuine, exact answer to the query
`skills for the future`, and it is a photograph of a man on a conference panel,
which illustrates nothing about the sentence "the most valuable skill may no
longer be…". No lexical rule reaches that. A model does, in one batched call for
the whole script — see `CandidateSelector`.

## What it will not do

It will not lower the floor to fill a frame. A shot with no good picture is
supposed to descend the ladder — to a drawing, or to a generated image — and
those rungs already exist and already work. The whole failure above came from
treating "we found bytes" as "we found a picture", so the one thing this module
must never do is prefer a poor photograph to an honest drawing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Output tokens one shot's verdict needs: a unit id, a key, a score, a clause.
OUTPUT_TOKENS_PER_SHOT = 220

#: Ceiling on one request's output. The smallest every model in use honours.
MAX_BATCH_OUTPUT_TOKENS = 16_000

#: Shots per model call. Derived, so it cannot drift from the budget it exists
#: to respect.
BATCH_SHOTS = MAX_BATCH_OUTPUT_TOKENS // OUTPUT_TOKENS_PER_SHOT

#: Chunks in flight at once. Bounded because these are somebody's rate limit.
BATCH_CONCURRENCY = 4

#: Minimum score a commons candidate needs before it is worth showing at all.
#:
#: Below this the ladder descends and the shot is drawn or generated instead.
#: The number is calibrated against a real render's worth of real titles — see
#: `tests/test_visual_selection.py`, which holds the actual filenames the
#: engines returned and asserts which side of the line each falls on. It is not
#: a guess, and it should not be moved without moving those cases with it.
FLOOR = 0.34

#: Words that carry no subject. Matching on these is how "person clicking on
#: computer" comes to accept a photograph of a railway station: both contain
#: "on".
_STOPWORDS = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "for",
    "from", "had", "has", "have", "he", "her", "his", "how", "i", "in", "into",
    "is", "it", "its", "of", "on", "or", "our", "that", "the", "their", "them",
    "there", "these", "they", "this", "to", "was", "were", "what", "when", "where",
    "which", "who", "will", "with", "you", "your", "about", "above", "after",
    "again", "against", "all", "also", "am", "any", "because", "before", "below",
    "between", "both", "but", "can", "did", "do", "does", "doing", "down",
    "during", "each", "few", "further", "here", "him", "himself", "if", "into",
    "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not",
    "now", "off", "once", "only", "other", "ought", "ourselves", "out", "over",
    "own", "same", "she", "should", "so", "some", "such", "than", "then", "those",
    "through", "too", "under", "until", "up", "very", "we", "while", "whom", "why",
    "would",
))

#: File-name noise. Commons filenames carry upload ids, camera exports and
#: format markers that are not part of what the picture shows.
_NOISE = frozenset((
    "jpg", "jpeg", "png", "gif", "svg", "tif", "tiff", "webp", "file", "image",
    "img", "photo", "photograph", "picture", "raw", "export", "cropped", "crop",
    "original", "full", "resized", "thumb", "thumbnail", "scan", "scanned", "copy",
    "final", "version", "v1", "v2", "dsc", "dscn", "imgp", "p1", "p2", "wikimedia",
    "commons", "upload",
))

#: Split on anything that is not a letter or digit, keeping Unicode letters.
#:
#: `[a-z]+` was the first version and it is wrong twice over: it reads "hof" out
#: of the upload id `HOF02547`, so a photograph agrees with any query about
#: hofs, and it reads "llsammelaktion" out of "Müllsammelaktion", so nothing in
#: a language with diacritics can ever match anything.
_TOKEN = re.compile(r"[^0-9\W_]*\d+[^0-9\W_]*|[^\W\d_]+", re.UNICODE)
_HAS_DIGIT = re.compile(r"\d")


def _stem(word: str) -> str:
    """A deliberately crude stem.

    Enough to see that "clicking" and "click" are the same word, and nothing
    more. A real stemmer would be a dependency and a source of surprises in
    languages this system also has to serve; the failure mode here is a missed
    match, which costs a candidate, not a wrong match, which costs a video.
    """
    for suffix in ("ingly", "edly", "ing", "ers", "er", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_words(text: str) -> frozenset[str]:
    """The words in `text` that say what something is about.

    Tokens containing a digit are dropped whole. Commons filenames are full of
    `HOF02547`, `20230513` and `(7449258498)`, and none of them is a fact about
    what the picture shows.
    """
    return frozenset(
        _stem(word)
        for token in _TOKEN.findall(text.lower())
        if not _HAS_DIGIT.search(token)
        for word in [token]
        # Two letters, not three. The subject of half this product's scripts is
        # "AI", and a three-letter floor discards it — so `AI in business` had
        # no subject at all and matched nothing. The same goes for ML, UI, UX,
        # VR, AR and 3D. Almost every two-letter English word that is *not* a
        # subject is already a stopword, which is what makes the shorter floor
        # safe; single letters stay out.
        if len(word) > 1 and word not in _STOPWORDS and word not in _NOISE
    )


@dataclass(frozen=True)
class Candidate:
    """One search hit, as the selector sees it.

    Deliberately not `AssetCandidate`: this module must be usable on a drawn
    visual and a generated one too, and it must be testable without constructing
    a licence, a provenance record and a download URL to ask whether a title
    matches a query.
    """

    #: Stable handle the caller uses to find the real thing again.
    key: str
    #: What the search engine says this is. Usually a filename.
    title: str = ""
    #: Longer text where the source has one.
    description: str = ""
    #: The query that returned it. The thing it is measured against.
    query: str = ""
    #: Pixel shape, where the source declared one. A tall portrait in a 16:9
    #: frame is cropped to a band across the middle of it, which is how a
    #: perfectly good photograph of a person becomes a close-up of a forehead.
    width: int | None = None
    height: int | None = None
    #: The source's own ranking, normalised. A hint; rankings from different
    #: providers are not comparable, and this whole module exists because the
    #: hint was being treated as an answer.
    relevance: float | None = None

    @property
    def text(self) -> str:
        return f"{self.title} {self.description}".strip()


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    reason: str

    @property
    def is_usable(self) -> bool:
        return self.score >= FLOOR


#: Words that mark a picture as a portrait of a specific identified person.
#:
#: Used to *decline*, not to rank. Illustrating "letting intelligent agents
#: figure out the steps" with a named individual's face implies that person has
#: something to do with the claim, which they have not agreed to and which is
#: very often untrue. The system already refuses to *generate* the likeness of a
#: real person; reusing a photograph of one to illustrate an unrelated sentence
#: is the same wrong with a licence attached.
#: Stemmed at definition, because `content_words` stems what it is compared
#: against. Unstemmed, "speaking" in a title arrives as "speak" and matches
#: nothing here — which is how the one candidate this rule was written for got
#: through the rule written for it.
_PORTRAIT_MARKERS = frozenset(
    _stem(word)
    for word in (
        "portrait", "headshot", "mugshot", "selfie", "speaking", "speaker",
        "panellist", "panelist", "keynote", "interview", "conference",
        "summit", "awards", "ceremony", "premiere", "festival",
    )
)

_NAME = re.compile(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b")

#: The commons naming convention for a photograph of a person at an event:
#: exactly two capitalised words, "at", and a year somewhere after it.
#:
#: "Elin Wieslander at SXSW 2025 03 (cropped).jpg" — the file that put a
#: stranger's forehead across a claim about AI agents — has no word from
#: `_PORTRAIT_MARKERS` in it at all. "SXSW" is the event, and no list of event
#: names is ever finished. The *shape* of the title is the reliable signal.
#:
#: **Exactly two** capitalised words, because "Golden Gate Bridge at Sunset" has
#: three and is a landscape. **A year**, because person-at-event photographs
#: almost always carry one and "Times Square at Night" does not.
#:
#: This will still occasionally refuse a good photograph of a place. That trade
#: is deliberate and it is not close: showing a real person's face under a claim
#: they never made is a legal and an ethical problem, and losing one stock
#: photograph costs a rung on a ladder that has four more.
_PERSON_AT_EVENT = re.compile(
    r"^[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\s+at\s+\S.*?\b(19|20)\d{2}\b"
)


def names_a_person(candidate: Candidate) -> bool:
    """Whether this looks like a photograph of one identified individual.

    Two ways in, because the vocabulary rule alone missed the actual case.

    * **Shape** — `<Name> <Name> at <something> <year>`, the commons convention
      for event photography. See `_PERSON_AT_EVENT`.
    * **Vocabulary** — a capitalised full name *and* a word from the language of
      press photography. Either alone is wrong far too often: names match places
      and institutions, and "conference" matches photographs of empty rooms.

    "Central West Livestock Exchange near Forbes, NSW" has a capitalised name
    and neither of the two, which is correct — it is a photograph of a saleyard.
    """
    text = f"{candidate.title} {candidate.description}".strip()
    if _PERSON_AT_EVENT.match(text):
        return True
    return bool(_NAME.search(text)) and bool(
        _PORTRAIT_MARKERS & content_words(text)
    )


def aspect_penalty(candidate: Candidate, target: float = 16 / 9) -> float:
    """How badly this shape will crop into the frame, from 0.0 to 0.25.

    Not a relevance judgement and deliberately small: a portrait-shaped
    photograph of exactly the right subject still beats a landscape one of the
    wrong subject. It breaks ties, and it is the difference between a person in
    a room and a band across their forehead.
    """
    if not candidate.width or not candidate.height:
        return 0.0
    ratio = candidate.width / candidate.height
    if ratio <= 0:
        return 0.0
    # Log distance, so 2:1 and 1:2 are penalised the same amount.
    from math import log

    distance = abs(log(ratio / target))
    return min(0.25, distance * 0.18)


#: How many content words a query needs before a full match means anything.
#:
#: Coverage alone is a *fraction*, and a fraction is trivially 1.0 when the
#: denominator is 1. That turned out to be the single worst thing about the
#: first version of this module: the concept reader emits fallback queries built
#: from bare words of the narration — `fascinating`, `plan`, `AI` — and any
#: photograph whose title contains that word scores a perfect 1.0 and outranks
#: every result of the four considered, specific queries beside it.
#:
#: A real render did exactly this. "And that creates a fascinating shift: the
#: most valuable skill may no longer be…" searched for `fascinating`, matched
#: "A fascinating glimpse of Hyderabad, India" at 0.99, and put a 1904
#: stereoscopic card of elephants on screen — while `strategic thinking` and
#: `business strategy` returned things that scored lower for being *longer*.
#:
#: So a match is worth what the query is worth, and the arithmetic works out to
#: something simple enough to say in one line:
#:
#:     **about two of the query's words must actually be accounted for.**
#:
#: Coverage times specificity is `matched / max(3, len(query))`, so with the
#: floor at 0.34 the practical rule is: a one-word query never qualifies however
#: perfectly it matches; a two-word query qualifies only if *both* words are
#: there; a longer query qualifies on any two.
#:
#: That line is deliberately sharp, and it drops some near-misses that a person
#: might have allowed — `speech bubble` matching only "speech", `AI in business`
#: matching only "AI". Those are the tenuous stock photographs that fill a frame
#: without illustrating anything, and the rung below them draws the idea instead.
SPECIFIC_ENOUGH = 3


def specificity(wanted: frozenset[str]) -> float:
    """How much a full match on this query is worth, from 0 to 1."""
    return min(1.0, len(wanted) / SPECIFIC_ENOUGH)


def score(candidate: Candidate) -> Scored:
    """How well a candidate answers the query that found it.

    Two factors, multiplied.

    **Coverage** — what fraction of the query's content words the candidate's
    own text accounts for. A fraction rather than a similarity score, because
    the question is not "are these texts alike" (the query is three words and
    the title is a sentence of German) but "does this thing claim to be what was
    asked for".

    **Specificity** — what a full match on that query is worth at all. Without
    it, coverage rewards the vaguest query in the set, which is the opposite of
    what it should do. See `SPECIFIC_ENOUGH`.
    """
    # Before anything else, and regardless of how well it matches. A portrait
    # of a named individual that *does* answer the query is the dangerous case,
    # not the safe one: "Elin Wieslander speaking" is a genuinely good keyword
    # answer to "person talking", and putting her face under a claim about AI
    # agents is exactly the harm — the better the match, the more confidently
    # the wrong thing ships.
    if names_a_person(candidate):
        return Scored(
            candidate,
            0.0,
            "a photograph of an identified person, which must not be used to "
            "illustrate a claim they have not made",
        )

    wanted = content_words(candidate.query)
    if not wanted:
        # Nothing to check against. Neither pass nor fail: fall back to the
        # provider's own ranking rather than inventing a verdict.
        base = candidate.relevance if candidate.relevance is not None else FLOOR
        return Scored(candidate, base, "no query to check against")

    have = content_words(candidate.text)
    if not have:
        return Scored(candidate, 0.0, "the candidate describes itself with nothing")

    matched = wanted & have
    coverage = len(matched) / len(wanted)

    weight = specificity(wanted)
    final = max(0.0, coverage * weight - aspect_penalty(candidate))
    missing = sorted(wanted - have)
    if coverage >= 1.0 and weight >= 1.0:
        reason = "matches every word of the query"
    elif coverage >= 1.0 and final >= FLOOR:
        reason = (
            f"matches all of {'+'.join(sorted(matched))} — a short query, but a "
            "complete match"
        )
    elif coverage >= 1.0:
        reason = (
            f"matches all of {'+'.join(sorted(matched))}, but that is the whole "
            "query — too vague to mean anything"
        )
    elif matched:
        reason = f"matches {'+'.join(sorted(matched))}; nothing about {'+'.join(missing)}"
    else:
        reason = f"nothing in common with the query ({'+'.join(sorted(wanted))})"
    return Scored(candidate, round(final, 4), reason)


def screen(candidates: list[Candidate]) -> list[Scored]:
    """Every candidate scored, best first, unusable ones dropped.

    Returns `Scored` rather than bare candidates so the caller can record *why*
    it chose — the events this feeds are the only way anybody finds out later
    that a shot was drawn because eight photographs were about railway stations.
    """
    ranked = sorted(
        (score(candidate) for candidate in candidates),
        key=lambda s: (-s.score, s.candidate.key),
    )
    return [item for item in ranked if item.is_usable]


@dataclass(frozen=True)
class Brief:
    """What one shot needs a picture of."""

    unit_id: str
    #: The line being illustrated. The judge sees this; the screen does not.
    sentence: str
    #: The visual idea the concept reader settled on.
    subject: str = ""


@dataclass(frozen=True)
class Verdict:
    """The selector's answer for one shot."""

    unit_id: str
    #: Key of the chosen candidate, or `None` for "none of these is good enough,
    #: draw or generate it instead". `None` is a real answer and the most
    #: valuable one this module produces.
    chosen: str | None
    score: float
    reason: str
    #: Which stage decided: `screen` (free), `judge` (the model), or `empty`.
    decided_by: str = "screen"


def without_repeats(verdicts: dict[str, Verdict]) -> dict[str, Verdict]:
    """Stop the same photograph being used twice in one video.

    ## Why this is necessary at all

    Each shot is judged on its own, so two shots that happen to share a query —
    and many do, because a script about one subject asks about that subject
    repeatedly — can each be handed the same photograph. One real render put an
    identical 1904 stereoscopic card of Hyderabad on two separate scenes, which
    reads to a viewer as a bug in the editor rather than a coincidence.

    ## Why the loser is dropped rather than given the runner-up

    Because "no photograph" is a good outcome here, not a gap: the shot descends
    to a drawing or a generated image, both of which are made for that specific
    line and will be more apt than the second-best result of somebody else's
    search. Reaching for the runner-up would be the same "fill the frame with
    something" instinct that this whole module exists to remove.

    The keeper is the higher-scoring shot, with the unit id breaking ties so two
    identical renders make the same video.
    """
    ranked = sorted(
        verdicts.items(), key=lambda item: (-item[1].score, item[0])
    )
    taken: set[str] = set()
    out: dict[str, Verdict] = {}
    for unit_id, verdict in ranked:
        if verdict.chosen is None or verdict.chosen not in taken:
            if verdict.chosen is not None:
                taken.add(verdict.chosen)
            out[unit_id] = verdict
            continue
        out[unit_id] = Verdict(
            unit_id=unit_id,
            chosen=None,
            score=verdict.score,
            reason="already used for another shot in this video",
            decided_by=verdict.decided_by,
        )
    return out


@dataclass
class CandidateSelector:
    """Picks the best candidate per shot, for a whole script at once.

    ## Why the model call is batched, and why there is only one

    A per-shot judge is a model call per shot. On the thirteen-shot render this
    module was written for, that is thirteen calls to answer the same kind of
    question thirteen times — the exact cost shape `ConceptReader.read_many`
    already exists to avoid, and the one the user of this system has twice asked
    to stop paying for. One call for the whole script asks it once.

    ## Why the model sees only what the screen let through

    The screen removes the candidates that do not answer their own query, which
    on real data is most of them. What survives is a short list of plausible
    answers, and the model is asked the one question the screen cannot answer:
    *is a photograph of a conference panel a picture of this idea?*

    Sending everything instead would pay to have a model reject railway stations
    that a set intersection rejects for nothing.

    ## Why it degrades to the screen rather than to nothing

    With no router, a malformed response, or a refusal, the screen's own ranking
    stands. That is strictly better than what this replaced and it never blocks
    a render. A selector that failed closed would turn a vendor outage into a
    video with no pictures.
    """

    router: object | None = None
    events: object | None = None
    #: Shots per model call. See `BATCH_SHOTS`.
    batch: int = BATCH_SHOTS
    #: Candidates per shot shown to the model. Beyond a handful the marginal
    #: candidate is one the screen ranked last, and the tokens cost more than
    #: the improvement.
    shortlist: int = 4

    async def select(
        self,
        work: list[tuple[Brief, list[Candidate]]],
        *,
        organisation_id: str,
        project_id: str,
    ) -> dict[str, Verdict]:
        """One verdict per brief, keyed by unit id."""
        screened: dict[str, list[Scored]] = {}
        verdicts: dict[str, Verdict] = {}
        for brief, candidates in work:
            survivors = screen(candidates)
            screened[brief.unit_id] = survivors
            verdicts[brief.unit_id] = _from_screen(brief, survivors)

        askable = [
            (brief, screened[brief.unit_id][: self.shortlist])
            for brief, _ in work
            if screened.get(brief.unit_id)
        ]
        if self.router is None or not askable:
            return without_repeats(verdicts)

        judged = await self._judge_all(
            askable, organisation_id=organisation_id, project_id=project_id
        )
        for unit_id, verdict in judged.items():
            verdicts[unit_id] = verdict
        return without_repeats(verdicts)

    async def _judge_all(
        self,
        askable: list[tuple[Brief, list[Scored]]],
        *,
        organisation_id: str,
        project_id: str,
    ) -> dict[str, Verdict]:
        """Every shot, in as few calls as one request's output budget allows.

        ## Why this is not one call

        A thirty-minute narration is about 740 shots. One call would need
        roughly 160 000 output tokens against a 16 000 ceiling, and a prompt
        carrying 740 shots' shortlists — several thousand candidate
        descriptions — would exceed the context window outright.

        Neither failure is loud. The first returns verdicts for the first
        seventy shots and nothing for the rest; the second raises, is caught,
        and every shot falls back to the free screen. Both look exactly like
        "the agent ran", which is why the batch is bounded by arithmetic rather
        than by hope.

        Still batched, and heavily: 740 shots is eleven calls, not 740.
        """
        if len(askable) <= self.batch:
            return await self._judge(
                askable, organisation_id=organisation_id, project_id=project_id
            )

        import asyncio

        gate = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def one(chunk: list[tuple[Brief, list[Scored]]]) -> dict[str, Verdict]:
            async with gate:
                return await self._judge(
                    chunk, organisation_id=organisation_id, project_id=project_id
                )

        parts = await asyncio.gather(
            *(
                one(askable[start : start + self.batch])
                for start in range(0, len(askable), self.batch)
            )
        )
        merged: dict[str, Verdict] = {}
        for part in parts:
            merged.update(part)
        return merged

    async def _judge(
        self,
        askable: list[tuple[Brief, list[Scored]]],
        *,
        organisation_id: str,
        project_id: str,
    ) -> dict[str, Verdict]:
        from vtv.contracts.errors import Status, VTVError
        from vtv.contracts.generation import (
            GenerationKind,
            GenerationRequest,
            TextParams,
        )

        shots = [
            {
                "unit": brief.unit_id,
                "narration": brief.sentence[:400],
                "idea": brief.subject[:200],
                "candidates": [
                    {
                        "key": item.candidate.key,
                        "found_by": item.candidate.query[:120],
                        "described_as": item.candidate.text[:300],
                    }
                    for item in shortlist
                ],
            }
            for brief, shortlist in askable
        ]

        try:
            result = await self.router.generate(  # type: ignore[attr-defined]
                GenerationRequest(
                    organisation_id=organisation_id,
                    project_id=project_id,
                    kind=GenerationKind.TEXT,
                    params=TextParams(
                        instruction=_JUDGE_INSTRUCTION,
                        input_json={"shots": shots},
                        response_schema="visual_selection",
                        temperature=0.1,
                        max_output_tokens=min(220 * len(shots) + 400, 16_000),
                    ),
                )
            )
        except VTVError:
            return {}
        except Exception:
            # Selection is an improvement to a render that already works without
            # it. Nothing here is worth failing a video for.
            return {}

        if result.status is not Status.READY or not result.structured_output:
            return {}
        payload = result.structured_output.get("choices")
        if not isinstance(payload, list):
            return {}

        by_unit = {brief.unit_id: shortlist for brief, shortlist in askable}
        out: dict[str, Verdict] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            unit_id = str(item.get("unit") or "")
            shortlist = by_unit.get(unit_id)
            if shortlist is None:
                continue
            verdict = _from_judge(unit_id, item, shortlist)
            if verdict is not None:
                out[unit_id] = verdict
        return out


def _from_screen(brief: Brief, survivors: list[Scored]) -> Verdict:
    if not survivors:
        return Verdict(
            unit_id=brief.unit_id,
            chosen=None,
            score=0.0,
            reason="no candidate answered the query it was found by",
            decided_by="empty",
        )
    best = survivors[0]
    return Verdict(
        unit_id=brief.unit_id,
        chosen=best.candidate.key,
        score=best.score,
        reason=best.reason,
        decided_by="screen",
    )


def _from_judge(
    unit_id: str, payload: dict[str, object], shortlist: list[Scored]
) -> Verdict | None:
    """One model answer about one shot, validated against what it was shown.

    A key the model invented is refused rather than trusted. It is the one way
    a hallucinated field could reach into storage, and "the model named an asset
    that was not on the list" has exactly one safe reading: it did not choose.
    """
    keys = {item.candidate.key for item in shortlist}
    raw = payload.get("key")
    reason = str(payload.get("reason") or "")[:240]

    try:
        confidence = float(payload.get("score"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    if raw in (None, "", "none"):
        return Verdict(
            unit_id=unit_id,
            chosen=None,
            score=confidence,
            reason=reason or "no candidate depicts this idea",
            decided_by="judge",
        )
    key = str(raw)
    if key not in keys:
        return None
    if confidence < FLOOR:
        return Verdict(
            unit_id=unit_id,
            chosen=None,
            score=confidence,
            reason=reason or "the best candidate was still not good enough",
            decided_by="judge",
        )
    return Verdict(
        unit_id=unit_id, chosen=key, score=confidence, reason=reason, decided_by="judge"
    )


_JUDGE_INSTRUCTION = """
You are the picture editor for a narrated explainer video. For each shot you are
given the line of narration, the visual idea behind it, and a shortlist of
openly-licensed photographs found by keyword search. Choose the one a careful
editor would actually put on screen, or choose none.

Choose none whenever that is the honest answer. The shot has good alternatives —
the system will draw the idea itself or generate an image — and an unrelated
photograph is much worse than either. These are all real examples of answers
that should have been "none":

* narration about letting AI agents work out the steps; candidate is a portrait
  of a named person at a conference. A face is not an idea, and using someone's
  photograph implies they said the thing.
* narration about the most valuable skill changing; candidate is "Skills for the
  Future" — a photograph of a man speaking on a panel. It matches the words and
  depicts nothing about the claim.
* narration about systems accomplishing tasks; candidate is a gold military rank
  insignia. Keyword coincidence.

Prefer a picture that shows the *thing being described* over one that merely
shares its vocabulary. A photograph of a conference about a topic is not a
photograph of the topic. Generic but apt beats specific but unrelated: an empty
sunlit room is a fine picture of "imagine waking up ten years from now".

Return JSON: {"choices": [{"unit": "<unit id>", "key": "<candidate key or null>",
"score": <0.0-1.0 confidence that this picture genuinely illustrates the line>,
"reason": "<one short clause>"}]}

One entry per shot, using the exact unit ids and candidate keys you were given.
Never invent a key. Score honestly: below 0.34 is treated as "none".
""".strip()


__all__ = [
    "FLOOR",
    "Brief",
    "Candidate",
    "CandidateSelector",
    "Scored",
    "Verdict",
    "aspect_penalty",
    "content_words",
    "names_a_person",
    "score",
    "screen",
    "specificity",
    "without_repeats",
]
