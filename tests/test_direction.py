"""The reader/director inversion: decide the kind, *then* do the work.

## What this is about

Every stage that costs money or a network round trip used to run before anyone
had decided whether it was wanted. A forty-minute essay searched Openverse and
Wikimedia for all seven hundred and thirty-six of its lines and sent every
result to a judge, so that the director could then rule that most of those lines
were typography and discard the lot.

The order is now:

    narration → understand → DIRECTOR DECIDES KIND
                                 ├── typography → set the words. Stop.
                                 ├── chart / diagram → draw it. Stop.
                                 ├── user media → use it. Stop.
                                 ├── photograph → search, judge, download
                                 └── generated → generate

The tests below are the executable form of the word **stop** in that diagram.
Counting what the stubs were asked for is the only way to show it: no assertion
about the finished visual can distinguish "chose typography" from "searched two
providers, judged the results, and then chose typography".
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.testing import StubImageGenerationProvider
from vtv.contracts.base import TimeSpan
from vtv.contracts.generation import GenerationKind
from vtv.contracts.style import StyleProfile
from vtv.observability.events import EventSink
from vtv.pipeline.assets import AssetResolver
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.drawing import Draughtsman
from vtv.pipeline.generation import GenerationRouter
from vtv.pipeline.sourcing import Direction, VisualSourcingService
from vtv.pipeline.treatment import Treatment
from vtv.pipeline.visual_intent import ConceptReader

from tests.test_visual_sourcing import Commons, Fetcher

ORG = "org_0000000000000000000000"
PROJECT = "prj_0000000000000000000001"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class DirectionCase(unittest.TestCase):
    """A real service with every rung stubbed and counted."""

    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-direction-")
        self.storage = LocalStorageProvider(
            root=Path(self._dir.name), signing_key="test-key"
        )
        self.router = GenerationRouter(events=EventSink())
        self.image = StubImageGenerationProvider(storage=self.storage)
        self.router.register(self.image, GenerationKind.IMAGE)
        self.commons = Commons()
        self.service = VisualSourcingService(
            composer=SceneComposer(
                storage=self.storage,
                events=EventSink(),
                router=self.router,
                asset_resolver=AssetResolver(
                    storage=self.storage,
                    events=EventSink(),
                    providers=[self.commons],
                    fetcher=Fetcher(),
                ),
            ),
            concepts=ConceptReader(events=EventSink()),
            events=EventSink(),
            draughtsman=Draughtsman(events=EventSink()),
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def direct(self, narration: str, *, seconds: float = 5.0) -> Direction:
        concept = run(
            self.service.concepts.read(
                narration, organisation_id=ORG, project_id=PROJECT
            )
        )
        return run(
            self.service.direct(
                unit_id="vun_0000000000000000000002",
                narration=narration,
                concept=concept,
                style=StyleProfile(),
                span=TimeSpan.of(0.0, seconds),
                organisation_id=ORG,
                project_id=PROJECT,
            )
        )


class TheDecisionComesBeforeTheWork(DirectionCase):
    """Directing costs nothing — rules over the reader's output and the
    Draughtsman's, no network, no model, no money. That is the property that
    makes deciding-first affordable at four hours as well as at four minutes."""

    def test_directing_asks_no_provider_anything(self) -> None:
        self.direct("Revenue rose from 12 million to 48 million in three years.")
        self.assertEqual(self.commons.queries, [])
        self.assertEqual(self.image.calls, 0)

    def test_a_line_with_numbers_is_charted_and_wants_no_photograph(self) -> None:
        """The clearest case, and the one the old fixed ladder got most
        expensively wrong: it searched the commons, failed, and paid to
        generate an impression of growth."""
        direction = self.direct(
            "Revenue rose from 12 million to 48 million in three years."
        )
        self.assertIs(direction.decision.treatment, Treatment.CHART)
        self.assertFalse(direction.wants_photograph)

    def test_a_shot_too_short_to_read_wants_no_photograph(self) -> None:
        direction = self.direct("And so it goes.", seconds=1.2)
        self.assertIs(direction.decision.treatment, Treatment.TYPOGRAPHY)
        self.assertFalse(direction.wants_photograph)

    def test_a_photographable_line_does_want_one(self) -> None:
        """The gate has to let real work through, or it is just an off switch."""
        direction = self.direct("A busy city street at dawn, seen from above.")
        self.assertIs(direction.decision.treatment, Treatment.FOUND_MEDIA)
        self.assertTrue(direction.wants_photograph)

    def test_a_place_becomes_a_map_rather_than_a_stock_photograph(self) -> None:
        """Not a case I set out to test — the gate found it. A line naming a
        city is a map, the Draughtsman can draw one, and drawing it is both
        free and more truthful than whichever photograph of Paris a stock
        library happens to rank first."""
        direction = self.direct(
            "The Eiffel Tower was completed in Paris in 1889 for the World's Fair."
        )
        self.assertIs(direction.decision.treatment, Treatment.MAP)
        self.assertFalse(direction.wants_photograph)


class TheGateIsFirstReachableNotAnywhereInTheLadder(DirectionCase):
    """The rule took two attempts, and the first one was useless.

    "Is a photograph anywhere in the ladder" lets everything through, because
    almost every ladder ends in one — a chart whose numbers will not parse has
    to become something. These tests pin the distinction that makes the gate
    worth having.
    """

    CHART = "Revenue rose from 12 million to 48 million in three years."

    def test_a_ladder_ending_in_a_photograph_is_not_enough(self) -> None:
        direction = self.direct(self.CHART)
        self.assertIn(Treatment.FOUND_MEDIA, direction.decision.ladder)
        self.assertFalse(direction.wants_photograph)

    def test_because_the_drawing_above_it_is_already_in_hand(self) -> None:
        """The Draughtsman produced a spec. Realising it is local arithmetic,
        so nothing below that rung will ever run."""
        direction = self.direct(self.CHART)
        self.assertIsNotNone(direction.drawn)
        self.assertFalse(direction.drawn.is_typography)

    def test_a_reachable_photograph_is_searched_for(self) -> None:
        """Without a drawing to stop the descent, the same ladder does want
        one — otherwise the fallback ships the first hit with a clear licence,
        which is how a sentence about AI agents got a military rank insignia."""
        direction = self.direct(self.CHART)
        undrawn = Direction(
            unit_id=direction.unit_id, decision=direction.decision, drawn=None
        )
        self.assertTrue(undrawn.wants_photograph)


class OneDecisionPerLine(DirectionCase):
    """Two call sites that each assemble their own `Brief` are two directors,
    and they disagree the moment one is given a field the other was not. That
    is not hypothetical — this brief was once built without the Draughtsman's
    primitive, and a sentence stating two numbers went to the commons."""

    def test_the_ladder_uses_the_decision_it_was_handed(self) -> None:
        narration = "Revenue rose from 12 million to 48 million in three years."
        direction = self.direct(narration)
        concept = run(
            self.service.concepts.read(
                narration, organisation_id=ORG, project_id=PROJECT
            )
        )

        # A decision the director could not have reached on its own, so the
        # only way the ladder can produce it is by using the one it was given.
        class Elsewhere:
            treatment = Treatment.GENERATED_IMAGE
            ladder = (Treatment.GENERATED_IMAGE, Treatment.TYPOGRAPHY)
            why = "handed in"
            decided_by = "test"

        rungs = self.service._ladder(
            concept,
            None,
            StyleProfile(),
            TimeSpan.of(0.0, 5.0),
            direction.drawn,
            True,
            True,
            direction=Direction(
                unit_id="vun_0000000000000000000002",
                decision=Elsewhere(),
                drawn=direction.drawn,
            ),
        )
        self.assertEqual(rungs[0].strategy.value, "generated_image")

    def test_a_vetoed_photograph_forces_the_decision_to_be_taken_again(self) -> None:
        """The up-front pass directed this line believing a photograph was
        available. If the selector has since looked at every photograph the
        commons offer and judged that none depicts it, that belief is false —
        and reusing the decision would put the vetoed rung back at the top,
        which is the exact defect the veto exists to prevent."""
        narration = "A busy city street at dawn, seen from above."
        direction = self.direct(narration)
        self.assertTrue(direction.wants_photograph)
        concept = run(
            self.service.concepts.read(
                narration, organisation_id=ORG, project_id=PROJECT
            )
        )
        rungs = self.service._ladder(
            concept,
            None,
            StyleProfile(),
            TimeSpan.of(0.0, 5.0),
            direction.drawn,
            True,
            False,  # the selector vetoed every candidate
            direction=direction,
        )
        self.assertNotIn(
            "licensed_media", [rung.strategy.value for rung in rungs]
        )


class DirectingIsNotRequired(DirectionCase):
    """Every failure in the new pass has to leave the render as good as it was.
    A line nobody directed is sourced exactly as it was before any of this
    existed — a missing decision must never be read as a refusal."""

    def test_the_ladder_still_decides_for_itself_with_no_direction(self) -> None:
        narration = "Revenue rose from 12 million to 48 million in three years."
        concept = run(
            self.service.concepts.read(
                narration, organisation_id=ORG, project_id=PROJECT
            )
        )
        rungs = self.service._ladder(
            concept, None, StyleProfile(), TimeSpan.of(0.0, 5.0), None, True, True
        )
        self.assertTrue(rungs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
