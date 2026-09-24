"""Meaning extracted from the transcript.

This layer answers *"what is the speaker actually saying?"* and nothing else. It
does not decide what anything should look like — that is the Visual Director's
job — and it does not decide where scene boundaries fall — that is the Scene
Engine's job. Keeping those three concerns apart is what lets each be tested,
evaluated and improved independently.

The output is a **structured, validated graph**, not free-form model prose
(Rule 2, Rule 9). A language model proposes; this schema disposes. Anything the
model returns that does not fit — a dangling entity reference, a unit that claims
a time range outside the recording — is rejected at the boundary rather than
quietly corrupting every stage downstream.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import Field, field_validator, model_validator

from vtv.contracts.base import (
    TIME_EPSILON,
    Confidence,
    Id,
    IdPrefix,
    Importance,
    RootDocument,
    TimeSpan,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import Status


class EntityType(str, Enum):
    """What kind of thing an entity is.

    The type is a strong prior for visual strategy — a ``PERSON`` suggests a
    portrait, a ``LOCATION`` suggests a map, a ``QUANTITY`` suggests a chart —
    but it is only a prior. The Visual Director makes the actual decision.
    """

    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    PRODUCT = "product"
    TECHNOLOGY = "technology"
    WORK = "work"
    EVENT = "event"
    CONCEPT = "concept"
    QUANTITY = "quantity"
    DATE = "date"
    OTHER = "other"


class SemanticIntent(str, Enum):
    """What the speaker is *doing* with a given stretch of speech.

    Intent drives visual grammar far more reliably than topic does. "X is
    defined as Y" wants typography or a diagram whatever X happens to be;
    "X grew from A to B" wants a chart whatever X happens to be.
    """

    DEFINITION = "definition"
    CLAIM = "claim"
    EXAMPLE = "example"
    COMPARISON = "comparison"
    PROCESS = "process"
    CAUSATION = "causation"
    ENUMERATION = "enumeration"
    NUMERIC_FACT = "numeric_fact"
    EVENT_NARRATION = "event_narration"
    QUESTION = "question"
    CONCLUSION = "conclusion"
    TRANSITION = "transition"
    ASIDE = "aside"
    FILLER = "filler"


#: Intents that carry no visualisable content of their own. The Scene Engine
#: absorbs these into neighbouring units instead of giving them their own scene.
NON_VISUAL_INTENTS: frozenset[SemanticIntent] = frozenset(
    {SemanticIntent.FILLER, SemanticIntent.TRANSITION}
)


_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


class ExternalReference(VTVModel):
    """A link to a public knowledge base, used to disambiguate entities.

    Resolving "Bell Labs" to a stable identifier is what later allows the asset
    engine to search for the *right* Bell Labs, and what allows a factual-accuracy
    check to exist at all (Stage 16).
    """

    namespace: str = Field(
        max_length=32, description="e.g. 'wikidata', 'wikipedia', 'openlibrary'"
    )
    identifier: str = Field(max_length=128)
    url: str | None = Field(default=None, max_length=2048)


class Quantity(VTVModel):
    """A number the speaker asserted, in a form a chart can consume directly."""

    value: float
    unit: str | None = Field(default=None, max_length=32)
    of_what: str | None = Field(
        default=None, max_length=200, description="What the number measures."
    )
    at: str | None = Field(
        default=None,
        max_length=64,
        description="When it applies, verbatim from speech, e.g. '1947'.",
    )


class Entity(VTVModel):
    """A thing the speaker referred to."""

    entity_id: Id = Field(default_factory=lambda: new_id(IdPrefix.ENTITY))
    name: str = Field(min_length=1, max_length=200)
    type: EntityType
    #: Normalised name used for asset search and deduplication.
    canonical_name: str | None = Field(default=None, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=16)
    salience: Importance = 0.5
    external: ExternalReference | None = None

    @property
    def search_name(self) -> str:
        return self.canonical_name or self.name


class Relation(VTVModel):
    """A directed statement connecting two entities.

    Relations are what make a *diagram* possible rather than a slideshow: they
    are the edges the animation engine draws.
    """

    relation_id: Id = Field(default_factory=lambda: new_id(IdPrefix.RELATION))
    subject_id: Id
    predicate: str = Field(
        min_length=2,
        max_length=48,
        description="snake_case verb phrase, e.g. 'invented_at', 'replaced'.",
    )
    object_id: Id
    confidence: Confidence | None = None

    @field_validator("predicate")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        normalised = value.strip().lower().replace(" ", "_").replace("-", "_")
        if not _PREDICATE_RE.match(normalised):
            raise ValueError(
                f"predicate must be lowercase snake_case, got {value!r}"
            )
        return normalised

    @model_validator(mode="after")
    def _no_self_loops(self) -> Relation:
        if self.subject_id == self.object_id:
            raise ValueError("a relation must connect two different entities")
        return self


class SemanticUnit(VTVModel):
    """One meaningful idea, grounded in a time span of the recording.

    This is the atom of understanding. It is **not** a sentence and **not** a
    scene. A single sentence can contain three units; three sentences can
    contain one (Rule 7).
    """

    unit_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SEMANTIC_UNIT))
    span: TimeSpan
    #: Transcript segments this unit draws from. The link back to the source is
    #: never broken, so every downstream artefact is traceable to spoken words.
    segment_ids: list[Id] = Field(default_factory=list)
    text: str = Field(
        min_length=1,
        max_length=4000,
        description="The narration this unit covers, verbatim from the transcript.",
    )
    #: A one-line restatement of the idea. This, not the verbatim text, is what
    #: the Visual Director reasons over.
    proposition: str = Field(min_length=1, max_length=400)
    intent: SemanticIntent
    entity_ids: list[Id] = Field(default_factory=list, max_length=32)
    relation_ids: list[Id] = Field(default_factory=list, max_length=32)
    quantities: list[Quantity] = Field(default_factory=list, max_length=32)
    keyphrases: list[str] = Field(default_factory=list, max_length=16)
    salience: Importance = 0.5
    confidence: Confidence | None = None

    @property
    def is_visualisable(self) -> bool:
        return self.intent not in NON_VISUAL_INTENTS


class Understanding(RootDocument, Timestamped):
    """The complete semantic reading of one transcript.

    Invariants enforced here are the contract between the understanding stage
    and everything after it. If this validates, the Scene Engine can rely on
    entity references resolving and on units being in narrative order.
    """

    document_name = "understanding"

    understanding_id: Id = Field(
        default_factory=lambda: new_id(IdPrefix.SEMANTIC_UNIT)
    )
    #: The tenant this belongs to. Carried on every project-scoped document so
    #: that a service deep in the pipeline can name the storage namespace it is
    #: allowed to write to without an ambient lookup — `LocalStorageProvider`
    #: refuses any key outside `orgs/<organisation_id>/`.
    organisation_id: Id
    project_id: Id
    transcript_id: Id

    #: The subject of the whole recording, in the speaker's own register.
    topic: str = Field(default="", max_length=200)
    #: A few sentences capturing the through-line. The Scene Engine uses this to
    #: keep scenes serving one story rather than drifting apart (Section 2).
    summary: str = Field(default="", max_length=2000)

    units: list[SemanticUnit] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)

    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    status: Status = Status.PENDING

    @model_validator(mode="after")
    def _referential_integrity(self) -> Understanding:
        entity_ids = {entity.entity_id for entity in self.entities}
        if len(entity_ids) != len(self.entities):
            raise ValueError("duplicate entity_id in understanding")

        relation_ids = {relation.relation_id for relation in self.relations}
        if len(relation_ids) != len(self.relations):
            raise ValueError("duplicate relation_id in understanding")

        for relation in self.relations:
            for role, ref in (("subject", relation.subject_id), ("object", relation.object_id)):
                if ref not in entity_ids:
                    raise ValueError(
                        f"relation {relation.relation_id} references unknown "
                        f"{role} entity {ref}"
                    )

        unit_ids: set[str] = set()
        previous: SemanticUnit | None = None
        for unit in self.units:
            if unit.unit_id in unit_ids:
                raise ValueError(f"duplicate unit_id {unit.unit_id}")
            unit_ids.add(unit.unit_id)

            for ref in unit.entity_ids:
                if ref not in entity_ids:
                    raise ValueError(
                        f"unit {unit.unit_id} references unknown entity {ref}"
                    )
            for ref in unit.relation_ids:
                if ref not in relation_ids:
                    raise ValueError(
                        f"unit {unit.unit_id} references unknown relation {ref}"
                    )
            if previous is not None and unit.span.start < previous.span.start - TIME_EPSILON:
                raise ValueError("semantic units must be in ascending time order")
            previous = unit
        return self

    def unit_by_id(self, unit_id: str) -> SemanticUnit | None:
        return next((u for u in self.units if u.unit_id == unit_id), None)

    def entity_by_id(self, entity_id: str) -> Entity | None:
        return next((e for e in self.entities if e.entity_id == entity_id), None)

    def relation_by_id(self, relation_id: str) -> Relation | None:
        return next((r for r in self.relations if r.relation_id == relation_id), None)

    def entities_for(self, unit: SemanticUnit) -> list[Entity]:
        index = {e.entity_id: e for e in self.entities}
        return [index[i] for i in unit.entity_ids if i in index]

    @property
    def span(self) -> TimeSpan | None:
        if not self.units:
            return None
        return TimeSpan(
            start=min(u.span.start for u in self.units),
            end=max(u.span.end for u in self.units),
        )


__all__ = [
    "NON_VISUAL_INTENTS",
    "Entity",
    "EntityType",
    "ExternalReference",
    "Quantity",
    "Relation",
    "SemanticIntent",
    "SemanticUnit",
    "Understanding",
]
