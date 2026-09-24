"""What the reader/director inversion actually removes, on a real script.

    python scripts/measure_direction.py path/to/narration.txt

Directs every line of a script — with no model, no network and no money — and
reports how many of them would have been sent to the commons under the old
order, and how many are sent under the new one.

The number that matters is `searches avoided`. Each one was two provider
requests and a slot in the selector's judging call, spent to answer a question
the shape of the sentence had already settled.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import TimeSpan
from vtv.contracts.style import StyleProfile
from vtv.observability.events import EventSink
from vtv.pipeline.assets import AssetResolver
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.drawing import Draughtsman
from vtv.pipeline.generation import GenerationRouter
from vtv.pipeline.sourcing import VisualSourcingService
from vtv.pipeline.treatment import Census
from vtv.pipeline.visual_intent import ConceptReader

ORG = "org_0000000000000000000000"
PROJECT = "prj_0000000000000000000001"
#: Seconds a line is held. Roughly what the pacing planner allocates.
SECONDS = 3.3


class NoSearch:
    """A provider that exists so `can_search_media` is true, and is never called."""

    name = "measurement-only"

    async def search(self, **_: object) -> list:
        raise AssertionError("directing must not search")


async def main(path: Path) -> None:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if len(line.strip()) > 12
    ]
    with TemporaryDirectory() as tmp:
        storage = LocalStorageProvider(root=Path(tmp), signing_key="k")
        service = VisualSourcingService(
            composer=SceneComposer(
                storage=storage,
                events=EventSink(),
                router=GenerationRouter(events=EventSink()),
                asset_resolver=AssetResolver(
                    storage=storage, events=EventSink(), providers=[NoSearch()]
                ),
            ),
            concepts=ConceptReader(events=EventSink()),
            events=EventSink(),
            draughtsman=Draughtsman(events=EventSink()),
        )
        # The reader with no model configured falls back to its rules, which is
        # what makes this measurable offline. The treatments it suggests are the
        # conservative ones, so the saving reported here is a floor.
        concepts = await service.concepts.read_many(
            lines, organisation_id=ORG, project_id=PROJECT
        )
        directions = [
            await service.direct(
                unit_id=f"vun_{index:024d}",
                narration=line,
                concept=concept,
                style=StyleProfile(),
                span=TimeSpan.of(index * SECONDS, (index + 1) * SECONDS),
                organisation_id=ORG,
                project_id=PROJECT,
            )
            for index, (line, concept) in enumerate(zip(lines, concepts, strict=True))
        ]

    census = Census.of([d.decision for d in directions])
    searched = sum(1 for d in directions if d.wants_photograph)
    total = len(directions)

    print(f"{path.name}: {total} lines, about {total * SECONDS / 60:.0f} minutes\n")
    print("  treatment chosen")
    for name, count in Counter(census.by_treatment).most_common():
        print(f"    {name:<18} {count:>5}  {count / total * 100:5.1f}%")
    print(f"\n  free of charge      {census.free_fraction * 100:5.1f}%")
    print("\n  commons searches")
    print(f"    before (every line) {total:>5}")
    print(f"    after  (gated)      {searched:>5}")
    print(
        f"    avoided             {total - searched:>5}"
        f"  ({(total - searched) / total * 100:.0f}% fewer)"
    )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
