"""Getting an actual picture for one stretch of narration.

This is the service the Studio's Regenerate menu needed and did not have.

## What used to happen

`product_jobs._DirectorProducer.produce` built a `TypographySpec` and returned
it. Unconditionally — same answer for "Generate an image", "Generate a video",
"Use a real source" and "Use animation", and the same answer whether or not an
image provider was configured. Its own rationale said "no image or video
provider is configured", which every user with a key in `.env` read as a lie,
and its docstring said "with a provider configured this is where the director's
ladder runs", which described code that was not there.

## What happens now

One ladder, executed by `SceneComposer`, which is the same object the pipeline
lane uses. That matters more than it looks: a second ladder executor written for
the product lane would be a second set of rules to keep in step, and the two
would diverge the first time either was fixed. So this module decides the *order
of the rungs* and `SceneComposer` climbs them.

The default order is the economic one:

1. **the commons** — Openverse and Wikimedia, free, real, licensed;
2. **a generated image** — about four cents, and it invents;
3. **a generated video** — about a dollar, and only when motion is the point;
4. **typography** — free, instant, and states only what was said.

An explicit intent reorders this. "Generate an image" starts at rung 2 because
the user asked it to; it still falls to the commons and then to type if the
generator refuses, because a menu item that produces nothing is worse than one
that produces something and says what it did.

## Cost is bounded before anything is attempted

Every rung is attempted under a `Budget`, which `GenerationRouter` enforces per
call along with the tenant's spend and generated-asset allowances. The ladder
descends on `BudgetExceeded` exactly as it descends on a provider outage, so a
project at its ceiling degrades to photographs and type instead of failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vtv.contracts.base import Budget, ObjectRef, TimeSpan
from vtv.contracts.errors import DegradationStep, ErrorCode, VTVError
from vtv.contracts.generation import GenerationKind, VisualFidelity
from vtv.contracts.scene import Scene, ScenePurpose, VisualGoal
from vtv.contracts.style import StyleProfile
from vtv.contracts.timeline import (
    AssetClipSource,
    PlaceholderClipSource,
    ProgrammaticClipSource,
)
from vtv.contracts.visual_language import CameraMotion, TypographySpec
from vtv.contracts.visual_plan import (
    CostEstimate,
    ImageGenerationRequirements,
    LicensedMediaRequirements,
    ProgrammaticRequirements,
    SceneVisualPlan,
    VideoGenerationRequirements,
    VisualDirective,
    VisualStrategy,
)
from vtv.contracts.visual_unit import RegenerationIntent
from vtv.observability.events import EventName, EventSink
from vtv.observability.trace import traced
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.drawing import Draughtsman, DrawnVisual
from vtv.pipeline.visual_intent import ConceptReader, VisualConcept, VisualSafety

#: Estimates used to order and bound the rungs. The generative ones are
#: deliberately pessimistic: over-estimating generation biases the ladder
#: towards the commons, which is the bias this product wants.
_ESTIMATES: dict[VisualStrategy, CostEstimate] = {
    VisualStrategy.PROGRAMMATIC: CostEstimate(usd=0.0, latency_seconds=0.4),
    VisualStrategy.LICENSED_MEDIA: CostEstimate(usd=0.0, latency_seconds=2.0),
    VisualStrategy.GENERATED_IMAGE: CostEstimate(usd=0.04, latency_seconds=14.0),
    VisualStrategy.GENERATED_VIDEO: CostEstimate(usd=1.20, latency_seconds=150.0),
}

#: How each intent colours the prompt and the search. Not free text: the intents
#: are a closed enum precisely so that nothing a user types reaches a model that
#: decides what appears in a video.
_STYLE: dict[RegenerationIntent, tuple[str, str]] = {
    RegenerationIntent.MORE_CINEMATIC: (
        "cinematic", "Shallow depth of field, dramatic lighting, film grain."
    ),
    RegenerationIntent.MORE_REALISTIC: (
        "photograph", "Documentary photograph, natural light, unstaged."
    ),
    RegenerationIntent.MORE_EDUCATIONAL: (
        "diagram", "Clean explanatory illustration, labelled, uncluttered."
    ),
    RegenerationIntent.SIMPLER: (
        "simple", "Minimal composition, one clear subject, plenty of space."
    ),
}


@dataclass
class SourcedVisual:
    """What the ladder actually produced, and what it gave up on the way."""

    strategy: VisualStrategy
    rationale: str
    object: ObjectRef | None = None
    asset_id: str | None = None
    spec: dict[str, object] | None = None
    attribution: str | None = None
    degradations: list[DegradationStep] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.object is not None or self.spec is not None


@dataclass(frozen=True)
class Direction:
    """What kind of visual a line gets, decided before anything is fetched.

    ## Why this exists as a value rather than as a call

    The director's decision used to be taken inside `_ladder`, at the moment a
    shot was about to be sourced — which is *after* every stage that spends
    money and time on the assumption that a photograph might be wanted. On a
    forty-minute essay the commons were searched seven hundred and twenty-five
    times, two providers each, and the candidates were sent to a judge, so that
    the director could then decide that most of those lines were typography and
    throw all of it away.

    Deciding first is not merely cheaper. It is the difference between a
    pipeline that *asks what a line needs* and one that *offers a line
    everything and lets it refuse*. The second shape cannot help doing the work
    before it learns it was unnecessary.

    ## Why the decision travels rather than being recomputed

    `decide` is pure and costs nothing, so recomputing it per stage would be
    free — and wrong. Two call sites that each build their own `Brief` are two
    directors, and they disagree the moment one of them is given a field the
    other was not. That has already happened once here: `Brief` was constructed
    without the Draughtsman's primitive, so a sentence stating two numbers went
    to the commons rather than to a chart.

    One decision per line, made once, carried everywhere. The gate on the
    commons pre-pass reads this; the ladder reads this; the census reads this.
    """

    unit_id: str
    #: The director's `Decision` — treatment, why, confidence, fallback ladder.
    decision: object
    #: What the Draughtsman found it could draw for this line, or `None`.
    #: Carried so the up-front pass and the per-shot pass do not both run it.
    drawn: DrawnVisual | None

    @property
    def wants_photograph(self) -> bool:
        """Whether this line could actually end up using a photograph.

        The gate on the commons pre-pass, and the rule it encodes took two
        attempts to get right.

        **Not** "is a photograph anywhere in the ladder". Almost every ladder
        ends in one, because a chart whose numbers will not parse has to become
        *something* — so that reading of the question lets everything through
        and the inversion buys nothing.

        **Not** "is a photograph the chosen treatment" either. A line whose
        first choice is a generated image will fall to a photograph when the
        generator is rate-limited or the budget runs out, and a fallback that
        has not been judged is the old defect: the first search hit with a
        clear licence, which is how a sentence about AI agents came to be
        illustrated with a gold military rank insignia.

        The rule is **"is a photograph the first rung that is not already in
        hand"**. A drawn visual the Draughtsman has already produced a spec for
        cannot fail — realising it is local arithmetic — and neither can the
        user's own media or typography. Anything above a photograph that *can*
        fail means the photograph is genuinely reachable, and reachable is what
        deserves a judged choice rather than a lucky one.
        """
        from vtv.pipeline.treatment import Treatment

        for treatment in getattr(self.decision, "ladder", ()):
            if treatment is Treatment.FOUND_MEDIA:
                return True
            if self._in_hand(treatment):
                return False
        return False

    def _in_hand(self, treatment: object) -> bool:
        """Whether this rung will certainly succeed, so nothing below it runs."""
        from vtv.pipeline.treatment import Treatment

        if treatment in (Treatment.USER_MEDIA, Treatment.TYPOGRAPHY):
            return True
        if getattr(treatment, "is_drawn", False):
            # A drawn treatment with no drawing behind it is a rung the ladder
            # will skip — the director can choose `chart` from the reader's
            # suggestion without the Draughtsman having found numbers to plot.
            return self.drawn is not None and not self.drawn.is_typography
        return False


@dataclass
class VisualSourcingService:
    """Reads the intent behind narration and climbs the ladder for it."""

    composer: SceneComposer
    concepts: ConceptReader
    events: EventSink
    #: Rung one. Free, instant, and it cannot invent a fact — so it goes above
    #: every rung that costs money, and it is what "Use animation" means.
    draughtsman: Draughtsman | None = None

    async def source(
        self,
        *,
        narration: str,
        intent: RegenerationIntent | None,
        style: StyleProfile,
        organisation_id: str,
        project_id: str,
        unit_id: str,
        span: TimeSpan,
        budget: Budget,
        concept: VisualConcept | None = None,
        may_generate: bool = True,
        #: False when the selector has already looked at every photograph the
        #: commons offer for this line and judged that none of them depicts it.
        #: Withholding the rung is not the same as letting it fail: it fails by
        #: returning the least-bad photograph, which is the defect.
        may_use_commons: bool = True,
        #: The photograph the selector judged best for this line, when it judged
        #: one. Handed to the composer rather than re-derived, because the
        #: selector read the narration and the ranking rule below it did not.
        chosen_media: object | None = None,
        #: The kind of visual this line gets, decided before anything was
        #: fetched. See `Direction`. `None` means "decide it here", which is
        #: what a single regeneration does — it has one line and nothing to
        #: save by planning over a script.
        direction: Direction | None = None,
        #: The project's chosen fidelity, `None` for the deployment default.
        #: Carried rather than looked up because this service has no repository
        #: and should not grow one to answer a question its caller already knows.
        fidelity: VisualFidelity | None = None,
    ) -> SourcedVisual:
        """One visual, from the best rung that works.

        `concept` may be supplied by a caller that read a whole script in one
        model call — see `ConceptReader.read_many`. Passing it is what turns
        thirteen model calls into one; omitting it reads this line on its own,
        which is what a single regeneration does.
        """
        if concept is None:
            with traced(unit=unit_id, stage="read-intent"):
                concept = await self.concepts.read(
                    narration, organisation_id=organisation_id, project_id=project_id
                )

        self.events.emit(
            EventName.VISUAL_PLAN_CREATED,
            project_id=project_id,
            data={
                "director": "sourcing-1.0",
                "unit": unit_id,
                "reader": concept.source,
                "safety": concept.safety.value,
                "intent": intent.value if intent else "none",
                "queries": len(concept.queries()),
            },
        )

        if concept.safety is VisualSafety.REFUSE:
            raise VTVError(
                f"no visual will be sourced for this section: {concept.safety_reason}",
                code=_refusal_code(),
                user_message=(
                    "We will not create a visual for this section. You can "
                    "reword it, or set it as text."
                ),
            )

        # Rung one is computed before the ladder is built, because whether it
        # exists at all depends on what the sentence contains: a line stating
        # two numbers can be charted, and most lines cannot be drawn as
        # anything but type. Only a *real* drawing earns a place above the
        # paid rungs — typography is the ladder's floor, not its ceiling.
        drawn = (
            direction.drawn
            if direction is not None
            else await self._drawn(
                narration, style=style, organisation_id=organisation_id,
                project_id=project_id, span=span,
            )
        )
        ladder = self._ladder(
            concept, intent, style, span, drawn, may_generate, may_use_commons,
            direction=direction,
        )
        ladder = self._affordable(ladder, budget, intent)
        scene = _scene_for(narration, span, unit_id)
        plan = SceneVisualPlan(
            scene_id=scene.scene_id,
            primary=ladder[0],
            fallbacks=ladder[1:],
            budget=budget,
        )
        # Everything the ladder spends is attributed to this visual. Without
        # this a trace of a whole render is a flat list of forty calls with no
        # way to see that eleven of them were for one stubborn shot.
        with traced(unit=unit_id, rungs="→".join(r.strategy.value for r in ladder)):
            clip, _assets = await self.composer.realise(
                scene,
                plan,
                span,
                organisation_id=organisation_id,
                project_id=project_id,
                fidelity=fidelity,
                chosen_media=chosen_media,
            )
        return _from_clip(clip, concept, intent)

    # -- the ladder -------------------------------------------------------

    #: The intent that asked for each rung, for the message below.
    _ASKED_FOR = {
        VisualStrategy.GENERATED_VIDEO: RegenerationIntent.USE_GENERATED_VIDEO,
        VisualStrategy.GENERATED_IMAGE: RegenerationIntent.USE_GENERATED_IMAGE,
    }

    def _affordable(
        self,
        ladder: list[VisualDirective],
        budget: Budget,
        intent: RegenerationIntent | None,
    ) -> list[VisualDirective]:
        """Drop rungs this shot cannot pay for, *before* the call is made.

        ## Why this is not left to the router

        `GenerationRouter.candidates` filters on `unit_cost_usd`, which is a
        price *per unit* — per image, and per **second** of video. A ten-cent
        second passes a twenty-five-cent ceiling and then bills eighty cents for
        an eight-second clip, at which point the router raises `BudgetExceeded`
        and the ladder descends. That is the right refusal at the wrong moment:
        the work happened, the vendor charges for it, and the user pays for a
        result the system then throws away.

        Comparing the *whole shot's* estimate against the ceiling first turns
        that into a rung that is never attempted.

        ## Why an explicit ask raises instead of quietly descending

        A user who chose "Generate a video" and silently received a photograph
        has been told nothing. They need the sentence that names the ceiling,
        because that is the thing they can change.
        """
        ceiling = budget.max_cost_usd
        if ceiling is None:
            return ladder

        kept = [rung for rung in ladder if rung.estimate.usd <= ceiling]
        if kept and kept[0] is ladder[0]:
            return kept

        dropped = ladder[0].strategy
        if intent is not None and self._ASKED_FOR.get(dropped) is intent:
            raise VTVError(
                f"a {dropped.value} for this section is estimated at "
                f"${ladder[0].estimate.usd:.2f}, over this visual's ceiling of "
                f"${ceiling:.2f}",
                code=ErrorCode.BUDGET_EXCEEDED,
                user_message=(
                    "That option costs more than this project's limit allows "
                    "for one visual. Raise VTV_MAX_PROJECT_COST_USD and try "
                    "again, or pick a cheaper option."
                ),
            )
        return kept or [ladder[-1]]

    # -- direction --------------------------------------------------------

    async def direct(
        self,
        *,
        unit_id: str,
        narration: str,
        concept: VisualConcept,
        style: StyleProfile,
        span: TimeSpan,
        organisation_id: str,
        project_id: str,
        may_generate: bool = True,
    ) -> Direction:
        """Decide what kind of visual this line gets, before fetching anything.

        Costs no money and no network. The Draughtsman is rules over one
        sentence, and `decide` is a table of twelve rules over the reader's
        output — so directing an entire four-hour script is arithmetic, and the
        stages that *do* cost something get to run only on the lines that
        turned out to need them.

        This is the inversion. The order used to be:

            read → search the commons for every line → judge them all
                 → decide the kind → discard most of the searching

        and it is now:

            read → decide the kind → search only the lines that want a
                 photograph → judge those

        The Draughtsman result is carried on the `Direction` rather than
        recomputed at sourcing time, because it is the evidence the decision
        rests on: running it twice risks the second run disagreeing with the
        decision the first one produced.
        """
        drawn = await self._drawn(
            narration,
            style=style,
            organisation_id=organisation_id,
            project_id=project_id,
            span=span,
        )
        return Direction(
            unit_id=unit_id,
            decision=self._decide(
                concept, span=span, drawn=drawn, may_generate=may_generate
            ),
            drawn=drawn,
        )

    def _decide(
        self,
        concept: VisualConcept,
        *,
        span: TimeSpan,
        drawn: DrawnVisual | None,
        may_generate: bool,
        may_use_commons: bool = True,
    ) -> object:
        """Build the brief and ask the director. The only place either happens.

        Two call sites that each assemble their own `Brief` are two directors,
        and they disagree the moment one is given a field the other was not.
        That is not hypothetical: this brief was once built without
        `drawn`, and a sentence stating two numbers went to the commons instead
        of to a chart of the actual figures.
        """
        from vtv.pipeline.treatment import Brief, Capabilities, decide

        capabilities = Capabilities(
            # "Searchable" and "we have a search provider" are one question
            # here on purpose: `_commons` returns nothing when the reader found
            # no photographable query, and a rung with nothing to ask for is
            # not a capability.
            can_search_media=may_use_commons and self._commons(concept) is not None,
            can_generate_image=may_generate and self._available(GenerationKind.IMAGE),
            can_generate_video=may_generate and self._available(GenerationKind.VIDEO),
        )
        brief = Brief(
            unit_id="",
            narration=concept.source_text,
            seconds=span.duration,
            names_real_subject=concept.depicts_real_subject,
            may_spend=may_generate,
            may_illustrate=concept.may_search or concept.may_generate,
            # The reader found something to photograph, or it did not.
            #
            # Not "did it produce a query". The rules reader ends by joining
            # two leftover words of the sentence, and that fires on almost
            # every line of abstract prose: on a real thirty-eight minute
            # essay it manufactured a query for 690 of 697 lines and sent
            # 97.7% of the script to the commons on the strength of strings
            # like `tells message`. Having something to search with and having
            # a reason to search are different questions.
            searchable=bool(concept.queries()) and concept.names_a_subject,
            # The Draughtsman's answer, which is evidence rather than a guess:
            # it produces a chart only when it found numbers it could plot.
            drawn=drawn.spec.primitive if drawn is not None else None,
        )
        return decide(brief, capabilities, suggested=concept.treatment)

    async def _drawn(
        self,
        narration: str,
        *,
        style: StyleProfile,
        organisation_id: str,
        project_id: str,
        span: TimeSpan,
    ) -> DrawnVisual | None:
        """What we could draw for this line, or `None` if only type."""
        if self.draughtsman is None:
            return None
        try:
            drawn = await self.draughtsman.draw(
                narration,
                style=style,
                organisation_id=organisation_id,
                project_id=project_id,
                duration=span.duration,
            )
        except Exception:
            # Rung one failing must not cost the shot. It is rules over one
            # sentence and it should not raise, but the ladder's whole design
            # is that a rung failing is ordinary.
            return None
        return drawn

    def _drawn_directive(self, drawn: DrawnVisual) -> VisualDirective:
        return VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=drawn.requirements,
            rationale=drawn.rationale[:400],
            estimate=_ESTIMATES[VisualStrategy.PROGRAMMATIC],
            confidence=0.85,
        )

    def _ladder(
        self,
        concept: VisualConcept,
        intent: RegenerationIntent | None,
        style: StyleProfile,
        span: TimeSpan,
        drawn: DrawnVisual | None = None,
        may_generate: bool = True,
        may_use_commons: bool = True,
        direction: Direction | None = None,
    ) -> list[VisualDirective]:
        """Which rungs to try, in which order.

        Always ends in typography. That rung cannot fail — it needs no provider,
        no network and no money — which is what makes it safe for every path
        above it to raise rather than invent something.
        """
        commons = self._commons(concept)
        # The selector has already seen every photograph the commons offer for
        # this line and judged that none of them is a picture of it. Offering
        # the rung anyway means it succeeds — it always succeeds, there is
        # always *a* photograph — and ships the least-bad one, which is how a
        # sentence about AI agents came to be illustrated with a gold military
        # rank insignia.
        #
        # The veto governs the *automatic* path only. A user who clicked "use a
        # real source" has asked for a photograph and gets the best one there
        # is; overriding an explicit instruction with our own opinion of it
        # would be a button that silently does something else.
        auto_commons = commons if may_use_commons else None
        # The project's budget already decided this shot may not be bought —
        # see `pipeline/allowance.py`. Withholding the rung is not the same as
        # letting the router refuse it: the router refuses per call, after the
        # ladder has offered it, which is a limit rather than a plan.
        image = self._image(concept, intent, style) if may_generate else None
        video = (
            self._video(concept, intent, style, span) if may_generate else None
        )
        type_ = self._typography(concept)
        # Only a real drawing. A `DrawnVisual` that is typography is the
        # ladder's floor, and putting the floor at the top would mean nothing
        # else was ever tried.
        draw = (
            self._drawn_directive(drawn)
            if drawn is not None and not drawn.is_typography
            else None
        )

        if intent is RegenerationIntent.USE_TYPOGRAPHY:
            return [type_]
        if intent is RegenerationIntent.USE_ANIMATION:
            # "Draw it, do not fetch it." This used to return typography —
            # identical to the button beside it — because there was no
            # `Understanding` to hand and so no drawn primitive to reach for.
            # `Draughtsman` supplies one, and the menu item now differs from
            # its neighbour. It still falls to type when the sentence supports
            # no drawing, which is most sentences and is honest.
            return [rung for rung in (draw, type_) if rung is not None]
        if intent is RegenerationIntent.USE_REAL_SOURCE:
            return [rung for rung in (commons, type_) if rung is not None]
        if intent is RegenerationIntent.USE_GENERATED_IMAGE:
            return [rung for rung in (image, commons, type_) if rung is not None]
        if intent is RegenerationIntent.USE_GENERATED_VIDEO:
            return [rung for rung in (video, image, commons, type_) if rung is not None]

        # No explicit instruction — including every "same idea, but…" intent,
        # which changes the prompt rather than the order. This is the path the
        # selector's veto governs, so the commons rung here is the vetted one.
        #
        # ## The order is decided, not fixed
        #
        # It used to be `draw, commons, image, type` for every sentence in
        # every script, and that is a *fallback chain* rather than a decision.
        # It produced exactly what you would expect from a chain that never
        # asks what the sentence is about: a line stating two numbers went to
        # the commons, failed, and paid to generate an impression of growth,
        # when a chart of the actual figures is free and more truthful; and a
        # line reading "and that brings us to the next part" spent a search and
        # a generation on grammar.
        #
        # `pipeline/treatment.py` decides the *kind* first — from what the line
        # is doing, what this deployment can actually produce, and whether the
        # budget planner permitted this shot to cost anything — and the ladder
        # is built from that decision. The chain still descends, because a
        # chart whose numbers will not parse must still become something; it
        # just descends from a considered starting point.
        return self._decided_ladder(
            concept,
            style=style,
            span=span,
            drawn=drawn,
            commons=auto_commons,
            image=image,
            video=video,
            type_=type_,
            intent=intent,
            may_generate=may_generate,
            # Only when the assumptions still hold. The up-front pass directed
            # this line believing a photograph was available; if the selector
            # has since looked at every photograph the commons offer and judged
            # that none of them depicts it, that belief is false and the
            # decision has to be taken again with the rung withheld. Reusing it
            # would put a vetoed photograph back at the top of the ladder,
            # which is the exact defect the veto exists to prevent.
            decision=(
                direction.decision
                if direction is not None and may_use_commons
                else None
            ),
        )

    def _decided_ladder(
        self,
        concept: VisualConcept,
        *,
        style: StyleProfile,
        span: TimeSpan,
        drawn: DrawnVisual | None,
        commons: VisualDirective | None,
        image: VisualDirective | None,
        video: VisualDirective | None,
        type_: VisualDirective,
        intent: RegenerationIntent | None,
        may_generate: bool,
        decision: object | None = None,
    ) -> list[VisualDirective]:
        """Turn the director's decision into rungs this composer can execute.

        The decision is in `Treatment`s, which are about meaning. The composer
        speaks `VisualDirective`s, which are about execution. This is the
        translation, and it is deliberately the only place the two vocabularies
        meet — a second translation somewhere else would be a second opinion
        about what a chart is.
        """
        from vtv.pipeline.treatment import Treatment

        # Reused when the caller already directed this line — which is the
        # ordinary path now, because the commons pre-pass has to know the
        # treatment before it decides whether to search. Recomputing here would
        # be free and would be a second director; see `Direction`.
        if decision is None:
            decision = self._decide(
                concept,
                span=span,
                drawn=drawn,
                may_generate=may_generate,
                may_use_commons=commons is not None,
            )

        # Drawn treatments all execute through the one drawn directive the
        # Draughtsman produced. It already chose its own primitive from the
        # same understanding — asking for a chart here and getting a network
        # there would be two directors disagreeing, so the decision decides
        # *whether* to draw and the Draughtsman decides *what*.
        drawing = (
            self._drawn_directive(drawn)
            if drawn is not None and not drawn.is_typography
            else None
        )
        by_treatment: dict[Treatment, VisualDirective | None] = {
            Treatment.FOUND_MEDIA: commons,
            Treatment.GENERATED_IMAGE: image,
            Treatment.GENERATED_VIDEO: video,
            Treatment.TYPOGRAPHY: type_,
        }

        rungs: list[VisualDirective] = []
        for treatment in decision.ladder:
            rung = (
                drawing
                if treatment.is_drawn and treatment is not Treatment.TYPOGRAPHY
                else by_treatment.get(treatment)
            )
            if rung is not None and rung not in rungs:
                rungs.append(rung)

        self.events.emit(
            EventName.VISUAL_PLAN_CREATED,
            data={
                "stage": "treatment",
                "chose": decision.treatment.value,
                "why": decision.why[:160],
                "by": decision.decided_by,
                "ladder": [t.value for t in decision.ladder],
            },
        )
        del intent
        return rungs or [type_]

    def _available(self, kind: GenerationKind) -> bool:
        """Whether a provider for this kind is registered at all.

        A rung with no provider behind it is left out of the ladder rather than
        attempted and failed. Both reach typography in the end; only one of them
        can explain itself, and `/health` already tells the user which
        capabilities are dark.
        """
        router = self.composer.router
        if router is None:
            return False
        return bool(router.providers_for(kind))  # type: ignore[attr-defined]

    def commons_requirements(
        self, concept: VisualConcept
    ) -> LicensedMediaRequirements | None:
        """What this line would ask the commons for.

        Public because the selector needs to run exactly this search before the
        ladder does — see `product_jobs._judge_commons`. Sharing the builder
        rather than reconstructing it is what stops the judge from being asked
        about a different set of photographs than the one the ladder will fetch
        from, which would be a review of the wrong thing.
        """
        queries = concept.queries()
        if not queries or self.composer.asset_resolver is None:
            return None
        return LicensedMediaRequirements(
            query=queries[0],
            alternate_queries=queries[1:4],
            camera_motion=CameraMotion.KEN_BURNS,
        )

    def _commons(self, concept: VisualConcept) -> VisualDirective | None:
        requirements = self.commons_requirements(concept)
        if requirements is None:
            return None
        return VisualDirective(
            strategy=VisualStrategy.LICENSED_MEDIA,
            requirements=requirements,
            rationale=(
                f"searching openly-licensed media for {concept.subject}: a real "
                "photograph is free, and it did not have to be invented"
            )[:400],
            estimate=_ESTIMATES[VisualStrategy.LICENSED_MEDIA],
            confidence=0.7,
        )

    def _image(
        self,
        concept: VisualConcept,
        intent: RegenerationIntent | None,
        style: StyleProfile,
    ) -> VisualDirective | None:
        prompt = concept.prompt()
        if not prompt or not self._available(GenerationKind.IMAGE):
            return None
        label, clause = _STYLE.get(intent, ("image", ""))
        return VisualDirective(
            strategy=VisualStrategy.GENERATED_IMAGE,
            requirements=ImageGenerationRequirements(
                prompt=f"{prompt} {clause}".strip()[:900],
                negative_prompt="text, watermark, logos, distorted anatomy",
                aspect_ratio=style.aspect_ratio,
                camera_motion=CameraMotion.ZOOM_IN,
                depicts_reality=False,
            ),
            rationale=(
                f"no photograph of {concept.subject} was available, so a "
                f"{label} was generated for it"
            )[:400],
            estimate=_ESTIMATES[VisualStrategy.GENERATED_IMAGE],
            confidence=0.6,
        )

    def _video(
        self,
        concept: VisualConcept,
        intent: RegenerationIntent | None,
        style: StyleProfile,
        span: TimeSpan,
    ) -> VisualDirective | None:
        prompt = concept.prompt()
        if not prompt or not self._available(GenerationKind.VIDEO):
            return None
        _, clause = _STYLE.get(intent, ("video", ""))
        return VisualDirective(
            strategy=VisualStrategy.GENERATED_VIDEO,
            requirements=VideoGenerationRequirements(
                prompt=f"{prompt} {clause}".strip()[:900],
                negative_prompt="text, watermark, logos, distorted anatomy",
                aspect_ratio=style.aspect_ratio,
                duration_seconds=min(10.0, max(2.0, span.duration)),
                motion_description=concept.motion or "a slow, steady camera move",
                depicts_reality=False,
            ),
            rationale=(
                f"motion carries the meaning here, so {concept.subject} was "
                "generated as video"
            )[:400],
            estimate=_ESTIMATES[VisualStrategy.GENERATED_VIDEO],
            confidence=0.5,
        )

    def _typography(self, concept: VisualConcept) -> VisualDirective:
        headline = _headline(concept.source_text)
        reason = (
            concept.safety_reason
            if concept.safety is not VisualSafety.ALLOW
            else "type always renders, and it states only what is said"
        )
        return VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(spec=TypographySpec(headline=headline)),
            rationale=reason[:400],
            estimate=_ESTIMATES[VisualStrategy.PROGRAMMATIC],
            confidence=0.5,
        )


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _refusal_code():  # type: ignore[no-untyped-def]
    from vtv.contracts.errors import ErrorCode

    return ErrorCode.GENERATION_REFUSED


def _headline(text: str) -> str:
    stripped = " ".join(text.split())
    first = stripped.split(". ")[0]
    return (first or stripped)[:120] or "…"


def _scene_for(narration: str, span: TimeSpan, unit_id: str) -> Scene:
    """A one-scene stand-in, so the product lane can use the pipeline's executor.

    `SceneComposer` works in scenes. The Studio works in visual units. They are
    the same idea reached from two directions — a span of narration that gets one
    picture — and this is the adapter between them rather than a reason to write
    a second executor.
    """
    return Scene(
        index=0,
        span=span,
        narration=" ".join(narration.split())[:2000],
        purpose=ScenePurpose.EXPLANATION,
        visual_goal=VisualGoal.ILLUSTRATE_ABSTRACT,
        visual_brief=_headline(narration),
        importance=0.7,
    )


def _from_clip(
    clip: object, concept: VisualConcept, intent: RegenerationIntent | None
) -> SourcedVisual:
    """Read back what the ladder produced — including which rung won.

    Deliberately derived from the clip rather than from the directive that was
    asked for. The Director's first choice is a request; the clip is what the
    system actually has. Reporting the request would tell a user their section is
    a photograph when the search found nothing and it is a title card.
    """
    import json

    source = clip.source  # type: ignore[attr-defined]
    degradations = list(getattr(clip, "degradation", []) or [])
    descended = bool(degradations)

    if isinstance(source, AssetClipSource):
        generated = source.illustrative_label or not source.attribution
        strategy = (
            VisualStrategy.GENERATED_IMAGE if generated else VisualStrategy.LICENSED_MEDIA
        )
        if intent is RegenerationIntent.USE_GENERATED_VIDEO and generated:
            strategy = VisualStrategy.GENERATED_VIDEO
        rationale = (
            f"generated for {concept.subject}"
            if generated
            else f"an openly-licensed photograph of {concept.subject}"
        )
        if descended:
            rationale = f"{rationale}; earlier options were unavailable"
        return SourcedVisual(
            strategy=strategy,
            rationale=_rationale(rationale, concept),
            object=source.object,
            asset_id=source.asset_id,
            attribution=source.attribution,
            degradations=degradations[:6],
        )

    if isinstance(source, ProgrammaticClipSource):
        note = (
            concept.safety_reason
            if concept.safety is not VisualSafety.ALLOW
            else (
                "nothing suitable was found to show, so this section states "
                "what is said"
                if descended
                else "this section is set as motion typography"
            )
        )
        return SourcedVisual(
            strategy=VisualStrategy.PROGRAMMATIC,
            rationale=_rationale(note, concept),
            spec=json.loads(source.spec.model_dump_json()),
            degradations=degradations[:6],
        )

    if isinstance(source, PlaceholderClipSource):
        raise VTVError(
            "every visual strategy for this section was exhausted",
            user_message=(
                source.message
                or "We could not create a visual for this part. Try a different "
                "option from the menu."
            ),
        )

    raise VTVError("the ladder produced a clip of an unexpected kind")


def _rationale(note: str, concept: VisualConcept) -> str:
    """One line for the inspector: what was done, and what it was read as."""
    if concept.source == "llm":
        return f"{note} — {concept.rationale}"[:400]
    return note[:400]


__all__ = ["Direction", "SourcedVisual", "VisualSourcingService"]
