"""Stage 23 — building and applying the Visual Bible.

Two operations, deliberately separate.

**Build.** After understanding and before composition, decide once what each
recurring entity looks like. Only entities that appear in more than one scene
get a binding: a thing mentioned once cannot be inconsistent with itself, and
binding it would spend a colour from a small palette for no benefit.

**Apply.** During composition, every visual consults the Bible before choosing.
An entity with a binding gets that binding — the same asset, the same colour,
the same label — and the use is recorded so continuity can be audited afterwards.

The colour assignment deserves a note. Colours come from the project's own
palette, in a fixed order, skipping any already reserved. Deterministic on
purpose: the same project rendered twice produces the same colours, which is
what makes "re-render after an edit" produce a video the user recognises.
"""

from __future__ import annotations

from dataclasses import dataclass

from vtv.contracts.consistency import (
    BindingKind,
    BindingSource,
    ContinuityIssue,
    EntityBinding,
    VisualBible,
)
from vtv.contracts.errors import Status
from vtv.contracts.scene import SceneGraph
from vtv.contracts.semantics import Entity, Understanding
from vtv.contracts.style import StyleProfile
from vtv.observability.events import EventName, EventSink

#: An entity must carry at least this much of the narrative to earn a binding.
#: Below it, binding costs a palette slot and buys nothing.
MIN_SALIENCE = 0.3

#: Appearing in this many scenes makes an entity recurring, and recurring is
#: the only case where inconsistency is possible.
MIN_SCENES = 2

#: How many distinct colours the palette can carry before they stop reading as
#: distinct. Beyond this, later entities get an asset binding but no colour.
MAX_RESERVED_COLOURS = 8


@dataclass
class ConsistencyEngine:
    """Builds the Visual Bible and answers questions against it."""

    events: EventSink

    def build(
        self,
        *,
        scene_graph: SceneGraph,
        understanding: Understanding,
        style: StyleProfile,
        existing: VisualBible | None = None,
    ) -> VisualBible:
        """Decide what must stay constant across this project.

        An existing Bible is carried forward rather than rebuilt, so that a
        second render after an edit keeps every decision the user has seen —
        and every decision they locked.
        """
        bible = existing or VisualBible(project_id=scene_graph.project_id)

        appearances: dict[str, list[str]] = {}
        by_name: dict[str, Entity] = {}
        for scene in scene_graph.scenes:
            for entity_id in scene.entity_ids:
                entity = understanding.entity_by_id(entity_id)
                if entity is None:
                    continue
                name = entity.search_name.strip().lower()
                by_name.setdefault(name, entity)
                appearances.setdefault(name, [])
                if scene.scene_id not in appearances[name]:
                    appearances[name].append(scene.scene_id)

        recurring = [
            (name, scenes)
            for name, scenes in appearances.items()
            if len(scenes) >= MIN_SCENES
            and by_name[name].salience >= MIN_SALIENCE
        ]
        # Most salient first, so the strongest entities get the clearest
        # colours rather than whatever is left.
        recurring.sort(key=lambda item: -by_name[item[0]].salience)

        palette = _palette(style)
        for name, scenes in recurring:
            entity = by_name[name]
            existing_binding = bible.binding_for(name, kind=BindingKind.COLOUR)
            if existing_binding is not None and existing_binding.is_locked:
                continue
            if len(bible.palette.used) >= MAX_RESERVED_COLOURS:
                break
            colour = next(
                (item for item in palette if bible.palette.is_free(item)), None
            )
            if colour is None:
                break
            bible.bind(
                EntityBinding(
                    entity_id=entity.entity_id,
                    canonical_name=entity.search_name,
                    aliases=list(entity.aliases),
                    kind=BindingKind.COLOUR,
                    source=BindingSource.AUTOMATIC,
                    colour=colour,
                    used_in_scenes=list(scenes),
                    confidence=entity.salience,
                )
            )
            # The label is bound too: the same entity called by the same name
            # every time it appears on screen.
            bible.bind(
                EntityBinding(
                    entity_id=entity.entity_id,
                    canonical_name=entity.search_name,
                    aliases=list(entity.aliases),
                    kind=BindingKind.LABEL,
                    source=BindingSource.AUTOMATIC,
                    label=entity.name[:120],
                    used_in_scenes=list(scenes),
                )
            )

        bible.status = Status.READY
        self.events.emit(
            EventName.VISUAL_BIBLE_BUILT,
            project_id=scene_graph.project_id,
            data={
                "bindings": len(bible.bindings),
                "recurring_entities": len(recurring),
                "locked": len(bible.locked_names),
                "coverage": bible.coverage(),
            },
        )
        return bible

    def review(
        self, *, bible: VisualBible, scene_graph: SceneGraph
    ) -> list[ContinuityIssue]:
        """Find continuity problems worth reporting.

        Reported, not corrected. A deck that deliberately recolours a diagram
        is indistinguishable from a mistake at this level, and quietly
        "fixing" the deliberate case is worse than flagging both.
        """
        issues: list[ContinuityIssue] = []
        scene_ids = {scene.scene_id for scene in scene_graph.scenes}

        for binding in bible.bindings:
            if binding.kind is not BindingKind.COLOUR:
                continue
            stale = [
                scene_id
                for scene_id in binding.used_in_scenes
                if scene_id not in scene_ids
            ]
            if stale:
                issues.append(
                    ContinuityIssue(
                        entity_name=binding.canonical_name,
                        problem=(
                            f"bound to {len(stale)} scene(s) that no longer exist; "
                            f"the binding may be from a previous edit"
                        ),
                        severity="info",
                    )
                )
            if len(binding.used_in_scenes) < MIN_SCENES:
                issues.append(
                    ContinuityIssue(
                        entity_name=binding.canonical_name,
                        problem="bound but appears in fewer than two scenes",
                        severity="info",
                    )
                )

        # Two entities sharing a colour is the failure the palette lock exists
        # to prevent, so it is checked rather than assumed.
        seen: dict[str, str] = {}
        for binding in bible.bindings:
            if binding.kind is not BindingKind.COLOUR or not binding.colour:
                continue
            owner = seen.get(binding.colour.lower())
            if owner is not None and owner != binding.canonical_name:
                issues.append(
                    ContinuityIssue(
                        entity_name=binding.canonical_name,
                        problem=f"shares colour {binding.colour} with {owner}",
                        severity="warning",
                    )
                )
            seen[binding.colour.lower()] = binding.canonical_name

        bible.issues = issues[:200]
        return issues


def _palette(style: StyleProfile) -> list[str]:
    """Colours available for entity binding, in a fixed order.

    Deterministic so that re-rendering a project produces the video the user
    already approved rather than one that merely resembles it.
    """
    theme = getattr(style, "palette", None)
    candidates: list[str] = []
    for attribute in ("accent", "secondary", "primary", "highlight"):
        value = getattr(theme, attribute, None)
        if isinstance(value, str) and value.startswith("#"):
            candidates.append(value)
    # A spread of hues that stay distinguishable in the common forms of colour
    # blindness — a chart nobody can read is not an accessible chart.
    candidates.extend(
        [
            "#1f77b4", "#d95f02", "#7570b3", "#66a61e",
            "#e7298a", "#e6ab02", "#a6761d", "#666666",
        ]
    )
    seen: set[str] = set()
    ordered: list[str] = []
    for colour in candidates:
        if colour.lower() not in seen:
            seen.add(colour.lower())
            ordered.append(colour)
    return ordered


__all__ = [
    "MAX_RESERVED_COLOURS",
    "MIN_SALIENCE",
    "MIN_SCENES",
    "ConsistencyEngine",
]
