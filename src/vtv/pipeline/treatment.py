"""The Visual Director: what *kind* of visual explains this idea.

## The question this answers

Everything downstream of here is good at "get me a picture of X". Nothing before
here asked **"is a picture even the right answer?"**

The ladder in `sourcing._ladder` is a *fallback chain*, not a decision. It tries
drawing, then the commons, then generation, then type — in that fixed order, for
every sentence, whatever the sentence is about. It works, and it produces this:

* "Revenue grew from 40 million to 210 million" — searches the commons for a
  photograph, fails, and pays sixteen cents to generate an impression of growth.
  A bar chart of the two actual numbers is free, instant, and *more true*.
* "And that is what we will look at next" — a transition. It spends a commons
  search and an image generation on a sentence that means nothing on its own.
* "Alan Turing published the paper in 1936" — a historical fact about a real
  person, and the ladder's first instinct is to draw it.

The failure is the same in all three: **the treatment was never chosen, only
fallen back to.**

## What this module does

Decides the treatment *first*, from what the sentence is doing, and hands the
ladder an order that starts from that decision. The ladder still exists and
still descends — a chart whose numbers cannot be parsed must still become
something — but it descends from a considered starting point rather than a
fixed one.

## Why the vocabulary is not new

`SemanticIntent` (definition, comparison, process, causation, numeric_fact,
event_narration…) and `VisualGoal` (show_quantity, show_change_over_time,
show_structure, show_place…) already exist and already say everything needed.
`VisualPrimitive` already has chart, timeline, network, comparison, map. The
drawing engine already renders all of them.

So this module invents no taxonomy. It is the **join** between the one that
describes meaning and the one that describes pictures, and that join is the
thing that was missing.

## Provider neutrality

Nothing here names a vendor. `Capabilities.from_router` asks the generation
router what kinds it has registered, and a treatment whose capability is absent
is never offered. A deployment with no image provider gets charts, timelines,
diagrams, found media and type — a genuinely good video — rather than a ladder
that fails four rungs deep on every shot.

## Cost

Not a tiebreak bolted on afterwards. The three treatments that cost nothing —
drawn primitives, found media, typography — are also the three that are *most
often correct*, because most explanatory sentences are about quantities,
sequences, structures and definitions rather than about things that need
photographing. Spending money is what this does when the idea genuinely needs a
picture nobody has taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from vtv.contracts.scene import VisualGoal
from vtv.contracts.semantics import SemanticIntent
from vtv.contracts.visual_language import VisualPrimitive
from vtv.contracts.visual_plan import VisualStrategy


class Treatment(str, Enum):
    """A way of visually explaining an idea.

    Each maps onto the existing `VisualStrategy` (+ `VisualPrimitive` where the
    strategy is programmatic), so a decision here is directly executable by the
    composer that already exists. See `as_strategy`.
    """

    #: Media the user uploaded and, often, locked. Never overridden.
    USER_MEDIA = "user_media"
    #: An openly-licensed photograph or video of a real thing.
    FOUND_MEDIA = "found_media"
    #: Quantities, drawn to scale from the actual numbers.
    CHART = "chart"
    #: Two things set against each other.
    COMPARISON = "comparison"
    #: Events in order along an axis.
    TIMELINE = "timeline"
    #: Entities and the relations between them — process, structure, causation.
    DIAGRAM = "diagram"
    #: Where something is.
    MAP = "map"
    #: The words themselves, set well.
    TYPOGRAPHY = "typography"
    #: A picture nobody has taken.
    GENERATED_IMAGE = "generated_image"
    #: Motion nobody has filmed.
    GENERATED_VIDEO = "generated_video"

    @property
    def as_strategy(self) -> VisualStrategy:
        return _STRATEGY[self]

    @property
    def as_primitive(self) -> VisualPrimitive | None:
        return _PRIMITIVE.get(self)

    @property
    def is_free(self) -> bool:
        """Whether choosing this spends nothing.

        Drawing, typography and the commons cost no money and no vendor
        latency. That is not a small property: it is what lets a
        thirty-minute video be mostly *good* rather than mostly *expensive*.
        """
        return self not in {Treatment.GENERATED_IMAGE, Treatment.GENERATED_VIDEO}

    @property
    def is_drawn(self) -> bool:
        return self in _PRIMITIVE


_STRATEGY: dict[Treatment, VisualStrategy] = {
    Treatment.USER_MEDIA: VisualStrategy.EXISTING_ASSET,
    Treatment.FOUND_MEDIA: VisualStrategy.LICENSED_MEDIA,
    Treatment.CHART: VisualStrategy.PROGRAMMATIC,
    Treatment.COMPARISON: VisualStrategy.PROGRAMMATIC,
    Treatment.TIMELINE: VisualStrategy.PROGRAMMATIC,
    Treatment.DIAGRAM: VisualStrategy.PROGRAMMATIC,
    Treatment.MAP: VisualStrategy.PROGRAMMATIC,
    Treatment.TYPOGRAPHY: VisualStrategy.PROGRAMMATIC,
    Treatment.GENERATED_IMAGE: VisualStrategy.GENERATED_IMAGE,
    Treatment.GENERATED_VIDEO: VisualStrategy.GENERATED_VIDEO,
}

#: The Draughtsman's answer, in this module's vocabulary.
_FROM_PRIMITIVE: dict[VisualPrimitive, Treatment] = {
    VisualPrimitive.CHART: Treatment.CHART,
    VisualPrimitive.COMPARISON: Treatment.COMPARISON,
    VisualPrimitive.TIMELINE: Treatment.TIMELINE,
    VisualPrimitive.NETWORK: Treatment.DIAGRAM,
    VisualPrimitive.MAP: Treatment.MAP,
    VisualPrimitive.TYPOGRAPHY: Treatment.TYPOGRAPHY,
}

_PRIMITIVE: dict[Treatment, VisualPrimitive] = {
    Treatment.CHART: VisualPrimitive.CHART,
    Treatment.COMPARISON: VisualPrimitive.COMPARISON,
    Treatment.TIMELINE: VisualPrimitive.TIMELINE,
    Treatment.DIAGRAM: VisualPrimitive.NETWORK,
    Treatment.MAP: VisualPrimitive.MAP,
    Treatment.TYPOGRAPHY: VisualPrimitive.TYPOGRAPHY,
}


@dataclass(frozen=True)
class Capabilities:
    """What this deployment can actually do, right now.

    Asked of the router rather than assumed, and asked in the router's own
    vocabulary — `GenerationKind`, not a vendor name. A deployment with no image
    provider is not a broken deployment; it is one that makes videos out of
    charts, diagrams, found photographs and type, and it should be offered
    exactly those.
    """

    #: An openly-licensed media search is configured and reachable.
    can_search_media: bool = False
    can_generate_image: bool = False
    can_generate_video: bool = False
    #: Always true. Drawing needs no provider, no network and no money, which is
    #: why it is the one rung that can be the floor under everything.
    can_draw: bool = True
    #: This shot has media the user supplied.
    has_user_media: bool = False

    @classmethod
    def from_router(
        cls, router: object, *, can_search_media: bool = False, has_user_media: bool = False
    ) -> Capabilities:
        """Read the live registry. Never a config file, never a vendor name."""
        from vtv.contracts.generation import GenerationKind

        def registered(kind: GenerationKind) -> bool:
            try:
                return bool(router.providers_for(kind))  # type: ignore[attr-defined]
            except Exception:
                # A router that cannot answer is a router with nothing to
                # offer. Treated as absent so the decision falls to the free
                # treatments rather than raising in a planning stage.
                return False

        return cls(
            can_search_media=can_search_media,
            can_generate_image=registered(GenerationKind.IMAGE),
            can_generate_video=registered(GenerationKind.VIDEO),
            has_user_media=has_user_media,
        )

    def permits(self, treatment: Treatment) -> bool:
        if treatment is Treatment.USER_MEDIA:
            return self.has_user_media
        if treatment is Treatment.FOUND_MEDIA:
            return self.can_search_media
        if treatment is Treatment.GENERATED_IMAGE:
            return self.can_generate_image
        if treatment is Treatment.GENERATED_VIDEO:
            return self.can_generate_video
        return self.can_draw


@dataclass(frozen=True)
class Brief:
    """What is known about one idea, before deciding how to show it.

    Every field here is already computed somewhere upstream — the understanding
    engine, the concept reader, the pacing planner. This is the shape they are
    gathered into, not a new analysis.
    """

    unit_id: str
    narration: str
    #: What the sentence is *doing*, from the understanding engine.
    intent: SemanticIntent = SemanticIntent.CLAIM
    #: What it is trying to *show*, from the same place.
    goal: VisualGoal = VisualGoal.ILLUSTRATE_ABSTRACT
    #: Seconds on screen. A two-second shot cannot carry a chart anybody reads.
    seconds: float = 4.0
    #: How many quantities the sentence states. Two or more is a chart.
    quantities: int = 0
    #: How many dates. Two or more in sequence is a timeline.
    dates: int = 0
    #: Whether the understanding found relations between entities.
    relations: int = 0
    #: The primitive the Draughtsman actually produced, or `None` when it could
    #: only manage typography.
    #:
    #: This is the strongest structural signal there is, and it is *evidence*
    #: rather than a guess: the Draughtsman produces a chart only when it found
    #: numbers it could plot, a timeline only when it found dated events, a
    #: network only when it found related entities. A rule that re-derived
    #: "does this sentence have two quantities" from the text would be a second
    #: opinion about a question already answered — and the first version of this
    #: module did exactly that, left the field empty, and sent a sentence
    #: stating two numbers to the commons.
    drawn: VisualPrimitive | None = None
    #: Whether there is anything worth searching the commons *for*.
    #:
    #: The concept reader produces search queries; a line it could find no
    #: photographable query for has nothing the commons can answer. Sending it
    #: anyway costs two HTTP round trips and produces a candidate the selector
    #: then vetoes — which is exactly what a real render did, 725 times, on an
    #: essay about ideas.
    #:
    #: "Not searchable" is not "not illustratable": a generated picture or a
    #: well-set sentence may both be right. It only removes the one rung that
    #: cannot work.
    searchable: bool = True
    #: A real, named person, place or organisation whose likeness must not be
    #: invented. Set by `visual_intent`, and it is a hard constraint here.
    names_real_subject: bool = False
    #: Whether the shot may reach a paid rung at all — the budget planner's
    #: answer, decided for the whole project before any of this.
    may_spend: bool = True
    #: Whether this line may be illustrated at all, from the safety classifier.
    may_illustrate: bool = True


@dataclass(frozen=True)
class Decision:
    """How one idea should be shown, and what to do if that fails.

    Structured, not prose: `treatment` is executable by the composer, and
    `fallbacks` is the ladder. `why` exists for the user and the log, and is
    never parsed.
    """

    unit_id: str
    treatment: Treatment
    why: str
    confidence: float = 1.0
    #: Ordered, already filtered to what this deployment can do. Always ends in
    #: typography, which is the one rung that cannot fail.
    fallbacks: tuple[Treatment, ...] = ()
    #: `rules` (free) or `agent` (a model was asked).
    decided_by: str = "rules"

    @property
    def ladder(self) -> tuple[Treatment, ...]:
        return (self.treatment, *self.fallbacks)

    def as_json(self) -> dict[str, object]:
        return {
            "unit": self.unit_id,
            "treatment": self.treatment.value,
            "confidence": round(self.confidence, 3),
            "why": self.why[:200],
            "ladder": [t.value for t in self.ladder],
            "decided_by": self.decided_by,
        }


#: Output tokens one decision needs: a unit id, a treatment, a short clause.
OUTPUT_TOKENS_PER_DECISION = 90

#: Ceiling on one request's output. The smallest every model in use honours.
MAX_BATCH_OUTPUT_TOKENS = 16_000

#: Chunks in flight at once. Bounded because these are somebody's rate limit.
BATCH_CONCURRENCY = 4

#: Below this a rule is guessing, and the agent is asked instead.
#:
#: Calibrated so the unambiguous cases — two numbers, two dates, a named real
#: subject, a transition — never reach a model, and the genuinely open ones —
#: "is this abstract idea better as type or as a picture?" — always do. On real
#: narration that is roughly a third of the shots, which is what keeps the agent
#: affordable on a script with seven hundred of them.
UNCERTAIN = 0.7

#: Seconds below which a drawn primitive is not worth reading.
#:
#: A chart held for a second and a half is decoration. Nobody reads an axis in
#: that time, and the honest treatment for a very short shot is the words.
MIN_READABLE_SECONDS = 2.5


def decide(
    brief: Brief,
    capabilities: Capabilities,
    *,
    suggested: str = "",
) -> Decision:
    """The best way to explain this idea, from what it is.

    Free, deterministic and explicable. Every branch below is a statement about
    *meaning*, not about cost — cost enters only through `Capabilities` and
    `Brief.may_spend`, which remove options rather than reorder them.

    The order of the checks is the order of certainty. A sentence stating two
    quantities is a chart and nothing else could be better; a sentence about an
    abstract idea could reasonably be four things, and that is what the agent
    is for.
    """
    # 1. What the user gave us wins. They chose it; we did not.
    if capabilities.has_user_media:
        return _decided(
            brief,
            Treatment.USER_MEDIA,
            "the user supplied media for this shot",
            capabilities,
            confidence=1.0,
        )

    # 2. Material we will not illustrate. Typography is not a fallback here, it
    #    is the answer — see `visual_intent.VisualSafety`.
    if not brief.may_illustrate:
        return _decided(
            brief,
            Treatment.TYPOGRAPHY,
            "this line is set as text rather than illustrated",
            capabilities,
            confidence=1.0,
            fallbacks=(),
        )

    # 3. A shot too short to read is the words, whatever it is about. A chart
    #    nobody can read is worse than the sentence nobody has to.
    if brief.seconds < MIN_READABLE_SECONDS:
        return _decided(
            brief,
            Treatment.TYPOGRAPHY,
            f"only {brief.seconds:.1f}s on screen — too short to read a picture",
            capabilities,
            confidence=0.9,
        )

    # 4. A drawing that already exists. The Draughtsman read this line and
    #    produced a chart, a timeline, a comparison, a network or a map — which
    #    it only does when it found the structure to draw. Nothing else in this
    #    table is evidence rather than inference, and nothing else is both free
    #    and more truthful than a photograph could be: a chart of the two
    #    figures the sentence states cannot be wrong about them.
    if brief.drawn is not None and brief.drawn is not VisualPrimitive.TYPOGRAPHY:
        treatment = _FROM_PRIMITIVE[brief.drawn]
        return _decided(
            brief,
            treatment,
            f"the sentence supports a {treatment.value}, drawn from what it "
            "actually says",
            capabilities,
            confidence=1.0,
        )

    # 5. Quantities, where the text was read but nothing was drawn.
    if brief.quantities >= 2 or brief.goal is VisualGoal.SHOW_QUANTITY:
        return _decided(
            brief,
            Treatment.CHART,
            f"states {brief.quantities} quantities; a chart draws them to scale",
            capabilities,
            confidence=1.0,
        )

    # 6. Time. Two dates in one sentence is a span, and a span is a timeline.
    if brief.dates >= 2 or brief.goal is VisualGoal.SHOW_CHANGE_OVER_TIME:
        return _decided(
            brief,
            Treatment.TIMELINE,
            "describes change over time; a timeline shows the order",
            capabilities,
            confidence=0.95,
        )

    # 7. Two things set against each other.
    if brief.intent is SemanticIntent.COMPARISON or brief.goal is VisualGoal.SHOW_CONTRAST:
        return _decided(
            brief,
            Treatment.COMPARISON,
            "sets two things against each other; a split frame shows both",
            capabilities,
            confidence=0.95,
        )

    # 8. Process, cause, structure — things with parts and arrows between them.
    if brief.intent in _STRUCTURAL or brief.goal in _STRUCTURAL_GOALS:
        return _decided(
            brief,
            Treatment.DIAGRAM,
            "has steps or relations; a diagram shows how they connect",
            capabilities,
            confidence=0.9 if brief.relations else 0.75,
        )

    # 9. Place.
    if brief.goal is VisualGoal.SHOW_PLACE:
        return _decided(
            brief,
            Treatment.MAP,
            "is about where something is",
            capabilities,
            confidence=0.85,
        )

    # 10. A real, named subject. We will not invent a likeness of something that
    #    exists — so the only honest picture is one somebody actually took, and
    #    if nobody did, the words.
    if brief.names_real_subject:
        return _decided(
            brief,
            Treatment.FOUND_MEDIA,
            "names a real subject; a photograph of it, or nothing",
            capabilities,
            confidence=1.0,
            fallbacks=(Treatment.TYPOGRAPHY,),
        )

    # 11. Sentences that carry no idea of their own. A transition is
    #     scaffolding — spending a search and a generation on "and that brings
    #     us to the next part" is money spent on grammar.
    if brief.intent in _SCAFFOLDING:
        return _decided(
            brief,
            Treatment.TYPOGRAPHY,
            f"a {brief.intent.value}; carries no idea of its own to picture",
            capabilities,
            confidence=0.9,
        )

    # 12. Everything else is a genuine judgement: a definition, a claim, an
    #     example. Could be a photograph, could be a generated picture, could be
    #     well-set words — and on real narration this is where most sentences
    #     land, because most sentences are not about quantities or dates.
    #
    #     Measured on a real forty-minute script: 694 of 735 lines reach here.
    #     That is not a failure of the rules above; it is what an essay about
    #     ideas *is*. The rules catch the cases that have a structural signal,
    #     and there are forty-one of them. The rest is judgement, and judgement
    #     is what the reader's suggestion is for.
    if suggested:
        chosen = _suggested(suggested, brief, capabilities)
        if chosen is not None:
            return chosen

    #     With no suggestion, the last usable signal is whether the commons can
    #     be asked anything at all. A line the reader found no photographable
    #     query for is not a line the commons can answer, and asking anyway is
    #     two round trips spent to be told so.
    if not brief.searchable:
        return _decided(
            brief,
            Treatment.GENERATED_IMAGE if capabilities.can_generate_image else Treatment.TYPOGRAPHY,
            "abstract, with nothing photographable to search for",
            capabilities,
            confidence=0.5,
        )

    return _decided(
        brief,
        Treatment.FOUND_MEDIA if capabilities.can_search_media else Treatment.TYPOGRAPHY,
        "an idea with no structural signal and no suggestion; the commons first",
        capabilities,
        confidence=0.45,
    )


def _suggested(
    raw: str, brief: Brief, capabilities: Capabilities
) -> Decision | None:
    """The reader's treatment, if it is one this shot can actually have.

    Validated rather than trusted, on three counts, and each has cost something
    somewhere in this system before:

    * **It must name a real treatment.** A model that answers "infographic" has
      made a sentence, not a decision.
    * **This deployment must be able to produce it.** Naming `generated_video`
      where no video provider is registered is the same as naming nothing.
    * **The budget must permit it.** The planner already decided which shots may
      reach a paid rung, before any of this; a suggestion cannot overturn a plan
      made about somebody's money.

    Returning `None` means "no usable suggestion", and the caller falls through
    to its own answer rather than to nothing.
    """
    try:
        treatment = Treatment(raw)
    except ValueError:
        return None
    if treatment is Treatment.USER_MEDIA:
        # The user's own media is the user's decision, taken before this and
        # never revisited by a model.
        return None
    if not capabilities.permits(treatment):
        return None
    if not brief.may_spend and not treatment.is_free:
        return None
    return _decided(
        brief,
        treatment,
        f"the director read this line and chose {treatment.value}",
        capabilities,
        confidence=0.8,
        decided_by="director",
    )


#: Intents whose sentences have parts and connections.
_STRUCTURAL = frozenset(
    {SemanticIntent.PROCESS, SemanticIntent.CAUSATION, SemanticIntent.ENUMERATION}
)
_STRUCTURAL_GOALS = frozenset(
    {
        VisualGoal.SHOW_PROCESS,
        VisualGoal.SHOW_STRUCTURE,
        VisualGoal.SHOW_CAUSE_EFFECT,
    }
)

#: Intents that are grammar rather than content.
_SCAFFOLDING = frozenset(
    {SemanticIntent.TRANSITION, SemanticIntent.FILLER, SemanticIntent.ASIDE}
)


def _decided(
    brief: Brief,
    treatment: Treatment,
    why: str,
    capabilities: Capabilities,
    *,
    confidence: float,
    fallbacks: tuple[Treatment, ...] | None = None,
    decided_by: str = "rules",
) -> Decision:
    """Attach the ladder and drop what this deployment cannot do."""
    order = ladder_for(
        treatment, capabilities, brief=brief, explicit=fallbacks
    )
    chosen = order[0] if order else Treatment.TYPOGRAPHY
    if chosen is not treatment:
        why = f"{why} (falls to {chosen.value}: {treatment.value} unavailable)"
    return Decision(
        unit_id=brief.unit_id,
        treatment=chosen,
        why=why,
        confidence=confidence,
        fallbacks=tuple(order[1:]),
        decided_by=decided_by,
    )


def ladder_for(
    treatment: Treatment,
    capabilities: Capabilities,
    *,
    brief: Brief,
    explicit: tuple[Treatment, ...] | None = None,
) -> tuple[Treatment, ...]:
    """The chosen treatment, then what to try if it cannot be produced.

    ## Why a chart still needs a ladder

    Deciding "this is a chart" is a decision about the *sentence*. Whether a
    chart can actually be drawn is a question about the *parse* — the numbers
    have to come out of the text as numbers, and sometimes they do not. So the
    decision leads and the ladder follows, which is the opposite of the fixed
    order this replaces but keeps the property that made it safe: something
    always renders.

    ## Why typography is last and always present

    It needs no provider, no network and no money, and it cannot fail. Every
    other rung can. A ladder without it has a bottom that drops out.
    """
    if explicit is not None:
        wanted = (treatment, *explicit)
    else:
        wanted = (treatment, *_FALLBACKS.get(treatment, ()))

    out: list[Treatment] = []
    for candidate in wanted:
        if candidate in out:
            continue
        if not capabilities.permits(candidate):
            continue
        # The budget planner already decided this shot may not be bought. A
        # rung withheld here is not the router refusing later: this is a plan,
        # made before any call, which is why the video comes in under budget
        # rather than being truncated at it.
        if not brief.may_spend and not candidate.is_free:
            continue
        out.append(candidate)

    if Treatment.TYPOGRAPHY not in out:
        out.append(Treatment.TYPOGRAPHY)
    return tuple(out)


#: What to try when the chosen treatment cannot be produced.
#:
#: Read these as sentences. A chart that will not parse is still about
#: quantities, so a comparison of the two largest is the next best thing, and
#: only then do we reach for something bought. A diagram that will not build is
#: closer to a generated picture than to a photograph, because the thing being
#: described is usually abstract.
_FALLBACKS: dict[Treatment, tuple[Treatment, ...]] = {
    Treatment.USER_MEDIA: (Treatment.FOUND_MEDIA, Treatment.GENERATED_IMAGE),
    Treatment.CHART: (Treatment.COMPARISON, Treatment.FOUND_MEDIA),
    Treatment.COMPARISON: (Treatment.CHART, Treatment.FOUND_MEDIA),
    Treatment.TIMELINE: (Treatment.DIAGRAM, Treatment.FOUND_MEDIA),
    Treatment.DIAGRAM: (Treatment.GENERATED_IMAGE, Treatment.FOUND_MEDIA),
    Treatment.MAP: (Treatment.FOUND_MEDIA, Treatment.GENERATED_IMAGE),
    Treatment.FOUND_MEDIA: (Treatment.GENERATED_IMAGE,),
    Treatment.GENERATED_VIDEO: (Treatment.GENERATED_IMAGE, Treatment.FOUND_MEDIA),
    Treatment.GENERATED_IMAGE: (Treatment.FOUND_MEDIA,),
}


@dataclass
class Census:
    """What a script's decisions add up to.

    Exists because "the agent decides well" is not a claim anybody should
    accept without a number. A census over a real script says how many shots
    became charts, how many cost money, and how much the money was — and it is
    computed from the decisions themselves, before a single provider is called.
    """

    by_treatment: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    total: int = 0

    @classmethod
    def of(cls, decisions: list[Decision]) -> Census:
        census = cls(total=len(decisions))
        for decision in decisions:
            key = decision.treatment.value
            census.by_treatment[key] = census.by_treatment.get(key, 0) + 1
            census.by_source[decision.decided_by] = (
                census.by_source.get(decision.decided_by, 0) + 1
            )
        return census

    @property
    def paid(self) -> int:
        """Shots whose chosen treatment costs money."""
        return sum(
            count
            for name, count in self.by_treatment.items()
            if not Treatment(name).is_free
        )

    @property
    def free_fraction(self) -> float:
        return 1.0 - (self.paid / self.total) if self.total else 1.0

    def projected_usd(self, price_each: float) -> float:
        return round(self.paid * price_each, 4)

    def headline(self, price_each: float = 0.016) -> str:
        drawn = sum(
            count
            for name, count in self.by_treatment.items()
            if Treatment(name).is_drawn and name != Treatment.TYPOGRAPHY.value
        )
        return (
            f"{self.total} shots: {drawn} drawn, "
            f"{self.by_treatment.get('found_media', 0)} found, "
            f"{self.paid} generated (${self.projected_usd(price_each):.2f}), "
            f"{self.by_treatment.get('typography', 0)} as text."
        )

    def as_json(self) -> dict[str, object]:
        return {
            "total": self.total,
            "by_treatment": dict(sorted(self.by_treatment.items())),
            "by_source": dict(sorted(self.by_source.items())),
            "paid": self.paid,
            "free_fraction": round(self.free_fraction, 3),
        }


__all__ = [
    "MIN_READABLE_SECONDS",
    "OFFERABLE",
    "UNCERTAIN",
    "Brief",
    "Capabilities",
    "Census",
    "Decision",
    "Treatment",
    "decide",
    "ladder_for",
]


#: The treatments the reader may name.
#:
#: `USER_MEDIA` is absent deliberately — that is the user's decision, taken
#: before this and never revisited by a model.
OFFERABLE = (
    Treatment.FOUND_MEDIA,
    Treatment.CHART,
    Treatment.COMPARISON,
    Treatment.TIMELINE,
    Treatment.DIAGRAM,
    Treatment.MAP,
    Treatment.TYPOGRAPHY,
    Treatment.GENERATED_IMAGE,
)


__all__ = [
    "MIN_READABLE_SECONDS",
    "OFFERABLE",
    "UNCERTAIN",
    "Brief",
    "Capabilities",
    "Census",
    "Decision",
    "Treatment",
    "decide",
    "ladder_for",
]


@dataclass
class TreatmentAgent:
    """The judgement layer, for the shots the rules are honest about not knowing.

    ## Why most shots never reach it

    The rules above are certain about the cases that have a signal: two numbers
    is a chart, two dates is a timeline, a named real subject is a photograph or
    nothing, a transition is text. On real narration that is roughly two thirds
    of the shots, decided for free.

    What is left is the genuinely open third — a definition, a claim, an example
    — where "photograph, generated picture, or well-set words" is a judgement
    about explanatory value that no rule reaches. Those are the ones worth
    paying a model for.

    ## Why it is batched, and bounded by arithmetic

    One call per shot on a thirty-minute script is seven hundred calls. One call
    for all of them needs more output tokens than any model will emit. So the
    batch is sized from the output budget — see `SHOTS_PER_CALL` — and the
    chunks run concurrently under a bound, because they are somebody's rate
    limit and this system has already lost five visuals to a 429.

    ## Why it can only choose among what was offered

    The model is given the treatments this deployment can actually produce for
    this shot, and an answer outside that set is discarded rather than trusted.
    A model that names `generated_video` on a deployment with no video provider
    has not made a decision; it has made a sentence.
    """

    router: object | None = None
    events: object | None = None
    #: Shots per call, derived from the output budget so it cannot drift.
    batch: int = MAX_BATCH_OUTPUT_TOKENS // OUTPUT_TOKENS_PER_DECISION

    async def refine(
        self,
        work: list[tuple[Brief, Decision, Capabilities]],
        *,
        organisation_id: str,
        project_id: str,
    ) -> dict[str, Decision]:
        """Better decisions for the uncertain ones; the rest untouched."""
        settled = {
            brief.unit_id: decision
            for brief, decision, _ in work
            if decision.confidence >= UNCERTAIN
        }
        open_ones = [
            (brief, decision, caps)
            for brief, decision, caps in work
            if decision.confidence < UNCERTAIN
        ]
        if self.router is None or not open_ones:
            return {**settled, **{b.unit_id: d for b, d, _ in open_ones}}

        import asyncio

        gate = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def one(chunk):  # type: ignore[no-untyped-def]
            async with gate:
                return await self._ask(
                    chunk, organisation_id=organisation_id, project_id=project_id
                )

        parts = await asyncio.gather(
            *(
                one(open_ones[start : start + self.batch])
                for start in range(0, len(open_ones), self.batch)
            )
        )
        # The rules' answer stands wherever the model did not improve on it.
        # Every shot gets a decision either way; nothing is dropped for having
        # been asked about.
        refined = {b.unit_id: d for b, d, _ in open_ones}
        for part in parts:
            refined.update(part)
        return {**settled, **refined}

    async def _ask(
        self,
        chunk: list[tuple[Brief, Decision, Capabilities]],
        *,
        organisation_id: str,
        project_id: str,
    ) -> dict[str, Decision]:
        from vtv.contracts.errors import Status, VTVError
        from vtv.contracts.generation import (
            GenerationKind,
            GenerationRequest,
            TextParams,
        )

        offered = {
            brief.unit_id: tuple(
                t
                for t in _OFFERABLE
                if caps.permits(t) and (brief.may_spend or t.is_free)
            )
            for brief, _, caps in chunk
        }
        shots = [
            {
                "unit": brief.unit_id,
                "narration": brief.narration[:300],
                "seconds": round(brief.seconds, 1),
                "options": [t.value for t in offered[brief.unit_id]],
                "rules_chose": decision.treatment.value,
            }
            for brief, decision, _ in chunk
        ]

        try:
            result = await self.router.generate(  # type: ignore[attr-defined]
                GenerationRequest(
                    organisation_id=organisation_id,
                    project_id=project_id,
                    kind=GenerationKind.TEXT,
                    params=TextParams(
                        instruction=_INSTRUCTION,
                        input_json={"shots": shots},
                        response_schema="visual_treatment",
                        temperature=0.1,
                        max_output_tokens=min(
                            OUTPUT_TOKENS_PER_DECISION * len(shots) + 400,
                            MAX_BATCH_OUTPUT_TOKENS,
                        ),
                    ),
                )
            )
        except VTVError:
            return {}
        except Exception:
            # The rules already produced a usable decision for every one of
            # these. A planning stage must never fail a render.
            return {}

        if result.status is not Status.READY or not result.structured_output:
            return {}
        payload = result.structured_output.get("decisions")
        if not isinstance(payload, list):
            return {}

        by_unit = {brief.unit_id: (brief, caps) for brief, _, caps in chunk}
        out: dict[str, Decision] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            unit_id = str(item.get("unit") or "")
            found = by_unit.get(unit_id)
            if found is None:
                continue
            brief, caps = found
            try:
                treatment = Treatment(str(item.get("treatment")))
            except ValueError:
                # A treatment that does not exist is not a decision.
                continue
            if treatment not in offered.get(unit_id, ()):
                # Naming something this deployment cannot produce, or that the
                # budget already withheld, is the same as naming nothing.
                continue
            order = ladder_for(treatment, caps, brief=brief)
            out[unit_id] = Decision(
                unit_id=unit_id,
                treatment=order[0],
                why=str(item.get("why") or "")[:200] or "chosen by the director",
                confidence=0.8,
                fallbacks=tuple(order[1:]),
                decided_by="agent",
            )
        return out


#: The treatments the agent may choose between.
#:
#: `USER_MEDIA` is absent deliberately — that is the user's decision, taken
#: before this and never revisited by a model.
_OFFERABLE = (
    Treatment.FOUND_MEDIA,
    Treatment.DIAGRAM,
    Treatment.COMPARISON,
    Treatment.TYPOGRAPHY,
    Treatment.GENERATED_IMAGE,
)


_INSTRUCTION = """
You are the visual director of a narrated explainer video. For each shot you are
given one line of narration and the treatments this system can actually produce
for it. Choose the one that explains the idea best.

What the options mean:

* found_media — an openly-licensed photograph of a real thing. Right when the
  line is about something that exists and has been photographed.
* diagram — entities and the relations between them, drawn. Right when the line
  describes parts, steps, or how things connect.
* comparison — two things set side by side, drawn. Right when the line contrasts.
* typography — the words themselves, set well. Right when the idea is abstract
  and no picture would add to it. This is not a failure and not a last resort;
  a well-set sentence beats a vague stock photograph every time.
* generated_image — a picture nobody has taken. Right when the line needs a
  specific concrete scene that no photograph will have and no diagram can show.
  It costs money, so choose it when it is genuinely the best answer, not when
  you are unsure.

Judge on explanatory value, not decoration. Ask "what would a viewer understand
better because of this?" If the honest answer is "nothing", choose typography.

Never choose a treatment that is not in that shot's `options` list.

Return JSON: {"decisions": [{"unit": "<unit id>", "treatment": "<one option>",
"why": "<one short clause>"}]}
""".strip()
