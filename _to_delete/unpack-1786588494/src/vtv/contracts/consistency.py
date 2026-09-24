"""Stage 23 — the Visual Bible.

The defect this exists to prevent is the one viewers notice first and trust
least: the same thing looking different in scene two and scene seven. A
transistor drawn grey then blue, a company logo in two positions, a "before"
column that swaps sides. Each shot is individually defensible and the video as a
whole looks assembled by committee.

The fix is a project-level record of every decision that must hold across
scenes, made once and consulted thereafter. Three kinds of binding:

**Entity bindings.** "The transistor" resolved to one asset, one colour and one
descriptive phrase. Every later scene that mentions it reuses the binding rather
than resolving again — which is also, incidentally, a large cost saving, because
resolution is the expensive step.

**Slot assignments.** In a comparison, "before" is always the left side and
always the same colour. Swapping them mid-video is a continuity error even when
each individual frame is correct.

**Locks.** A binding can be *locked* by the user ("keep this one"), and a locked
binding is never revised by a later stage. That is what makes iterative editing
safe: a change to scene four must not silently redraw scene one.

The Bible is data, not behaviour. It is built by the pipeline, stored with the
project, and consulted by the Director and the Composer. That makes consistency
inspectable — a reviewer can read what the system decided — and reproducible,
because re-rendering with the same Bible produces the same video.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Confidence,
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import Status


class BindingKind(str, Enum):
    """What kind of decision is being held constant."""

    #: A real image or clip that stands for this entity everywhere.
    ASSET = "asset"
    #: A colour reserved for this entity across every drawn visual.
    COLOUR = "colour"
    #: A drawn icon or glyph.
    ICON = "icon"
    #: The phrase used on screen. "The 1947 transistor" every time, not
    #: sometimes "Bardeen's device".
    LABEL = "label"
    #: A fixed position: which side of a comparison, which end of a timeline.
    POSITION = "position"


class BindingSource(str, Enum):
    """Where the decision came from. Determines whether it may be revised."""

    #: Chosen by the pipeline. Revisable.
    AUTOMATIC = "automatic"
    #: The user said "keep this". Never revised, only replaced by the user.
    USER = "user"
    #: From a brand kit or organisation style. Never revised by the pipeline.
    BRAND = "brand"
    #: Present in the source document — a figure on page 4 that *is* the thing.
    SOURCE = "source"


class EntityBinding(VTVModel):
    """One entity's fixed visual identity for the length of a project."""

    binding_id: Id = Field(default_factory=lambda: new_id(IdPrefix.ENTITY))
    #: The entity this binds. Matching is by id first and canonical name second,
    #: because the understanding stage may produce a fresh id on re-analysis
    #: while the name stays stable.
    entity_id: Id | None = None
    canonical_name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=16)

    kind: BindingKind = BindingKind.ASSET
    source: BindingSource = BindingSource.AUTOMATIC

    #: The bound asset, when the binding is an asset.
    asset: ObjectRef | None = None
    asset_id: Id | None = None
    #: Hex colour, when the binding reserves one.
    colour: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    #: The on-screen phrase.
    label: str | None = Field(default=None, max_length=120)
    #: Fixed slot: "left", "right", "start", "end".
    position: str | None = Field(default=None, max_length=16)

    #: Scenes where this binding has been used, for auditing continuity.
    used_in_scenes: list[Id] = Field(default_factory=list, max_length=200)
    #: How confident the pipeline is that this asset really depicts the entity.
    #: A low-confidence binding is still consistent — the same wrong picture
    #: everywhere is better than three different wrong pictures — but it is
    #: flagged for review rather than presented as settled.
    confidence: Confidence | None = None
    note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _binding_carries_its_payload(self) -> EntityBinding:
        required = {
            BindingKind.ASSET: self.asset is not None or self.asset_id is not None,
            BindingKind.COLOUR: self.colour is not None,
            BindingKind.ICON: self.asset is not None or self.label is not None,
            BindingKind.LABEL: self.label is not None,
            BindingKind.POSITION: self.position is not None,
        }
        if not required[self.kind]:
            raise ValueError(f"a {self.kind.value} binding must carry its value")
        return self

    @property
    def is_locked(self) -> bool:
        """Whether a later stage may revise this.

        User and brand decisions are final. That is the property that makes
        "change scene four" safe: it cannot silently redraw scene one.
        """
        return self.source in {BindingSource.USER, BindingSource.BRAND}

    def matches(self, name: str) -> bool:
        """Whether a name refers to this binding's entity."""
        candidate = name.strip().lower()
        if not candidate:
            return False
        return candidate == self.canonical_name.strip().lower() or candidate in {
            alias.strip().lower() for alias in self.aliases
        }


class PaletteLock(VTVModel):
    """Colours reserved across the whole project.

    Separate from the style profile because these are *semantic* assignments —
    this colour means this thing — rather than aesthetic ones. Reusing a
    reserved colour for an unrelated element is the subtle version of the
    inconsistency this module exists to prevent.
    """

    #: Canonical entity name to hex colour.
    reserved: dict[str, str] = Field(default_factory=dict)
    #: Colours already spent, so the next assignment does not collide.
    used: list[str] = Field(default_factory=list, max_length=64)

    def colour_for(self, name: str) -> str | None:
        return self.reserved.get(name.strip().lower())

    def is_free(self, colour: str) -> bool:
        return colour.lower() not in {value.lower() for value in self.used}


class ContinuityIssue(VTVModel):
    """A consistency problem found during review.

    Recorded rather than silently corrected. Some are genuine defects and some
    are deliberate — a deck that intentionally recolours a diagram — and the
    system is not in a position to tell the difference, so it reports.
    """

    scene_id: Id | None = None
    entity_name: str = Field(max_length=200)
    problem: str = Field(max_length=300)
    severity: str = Field(default="warning", max_length=16)


class VisualBible(RootDocument, Timestamped):
    """Every visual decision that must hold across a whole project.

    Built after visual direction and before composition, then stored with the
    project so a re-render reproduces the same video rather than a similar one.
    """

    document_name = "visual_bible"

    visual_bible_id: Id = Field(default_factory=lambda: new_id(IdPrefix.VISUAL_PLAN))
    project_id: Id

    bindings: list[EntityBinding] = Field(default_factory=list, max_length=400)
    palette: PaletteLock = Field(default_factory=PaletteLock)
    issues: list[ContinuityIssue] = Field(default_factory=list, max_length=200)
    status: Status = Status.PENDING

    def binding_for(
        self, name: str, *, kind: BindingKind = BindingKind.ASSET
    ) -> EntityBinding | None:
        """The binding governing this entity, if one exists."""
        for binding in self.bindings:
            if binding.kind is kind and binding.matches(name):
                return binding
        return None

    def bind(self, binding: EntityBinding) -> EntityBinding:
        """Add or replace a binding, refusing to overwrite a locked one.

        Refusing rather than merging is deliberate. A user who pressed "keep
        this" and then watched it change on the next render has learned that the
        control does not work, and no amount of cleverness elsewhere recovers
        from that.
        """
        existing = self.binding_for(binding.canonical_name, kind=binding.kind)
        if existing is not None:
            if existing.is_locked and not binding.is_locked:
                return existing
            self.bindings = [item for item in self.bindings if item is not existing]
        self.bindings = [*self.bindings, binding]
        if binding.kind is BindingKind.COLOUR and binding.colour:
            self.palette.reserved[binding.canonical_name.strip().lower()] = (
                binding.colour
            )
            if binding.colour not in self.palette.used:
                self.palette.used = [*self.palette.used, binding.colour]
        return binding

    def note_use(self, name: str, scene_id: str, *, kind: BindingKind) -> None:
        """Record that a scene used a binding, so continuity can be audited."""
        binding = self.binding_for(name, kind=kind)
        if binding is not None and scene_id not in binding.used_in_scenes:
            binding.used_in_scenes = [*binding.used_in_scenes, scene_id]

    @property
    def locked_names(self) -> set[str]:
        return {
            binding.canonical_name.lower()
            for binding in self.bindings
            if binding.is_locked
        }

    def coverage(self) -> dict[str, int]:
        """How much of the project is governed, for the storyboard UI."""
        counts: dict[str, int] = {}
        for binding in self.bindings:
            counts[binding.kind.value] = counts.get(binding.kind.value, 0) + 1
        return counts


__all__ = [
    "BindingKind",
    "BindingSource",
    "ContinuityIssue",
    "EntityBinding",
    "PaletteLock",
    "VisualBible",
]
