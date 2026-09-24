"""Stage 3 — Semantic understanding.

Transcript to `Understanding`: what the speaker actually meant, as a validated
graph rather than prose.

Two engines implement the same interface.

`HeuristicUnderstandingEngine` is deterministic, free, instant and always
available. It is genuinely useful — dates, quantities, proper nouns and
discourse cues do not need a language model — and it is the baseline that the
model engine is measured against in Stage 16.

`LlmUnderstandingEngine` uses a `TextGenerationProvider` and produces a richer
reading. Its output is validated against the same contract; a response that does
not fit is repaired conservatively and then rejected if it still does not fit.
A model gets to propose. The schema disposes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from vtv.contracts.base import TimeSpan
from vtv.contracts.errors import ErrorCode, Status, ValidationFailed, VTVError
from vtv.contracts.generation import GenerationKind, GenerationRequest, TextParams
from vtv.contracts.semantics import (
    Entity,
    EntityType,
    Quantity,
    Relation,
    SemanticIntent,
    SemanticUnit,
    Understanding,
)
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.observability.events import EventName, EventSink, Timer
from vtv.pipeline import nlp

#: Version stamps on derived output. When the engine improves, old projects can
#: still be identified as having been produced by the old one
#: (docs/DECISIONS.md, data versioning).
UNDERSTANDING_VERSION = "heuristic-1.0"
LLM_UNDERSTANDING_VERSION = "llm-1.0"

#: Clause boundaries used to split a long segment into several ideas. A single
#: spoken sentence frequently contains two propositions joined by "and" or "but",
#: and treating it as one unit loses one of them.
_CLAUSE = re.compile(
    r"(?<=[.!?])\s+|(?:,\s+(?:and|but|which|while|whereas|so|because)\s+)"
)

#: Below this, a clause is a fragment rather than an idea and is kept with its
#: neighbour.
MIN_CLAUSE_CHARS = 24


class UnderstandingEngine(Protocol):
    """Turns a transcript into a validated semantic graph."""

    version: str

    async def understand(self, transcript: Transcript) -> Understanding: ...


# ---------------------------------------------------------------------------
# Deterministic engine
# ---------------------------------------------------------------------------

class HeuristicUnderstandingEngine:
    """Rule-based extraction. No model, no network, no cost, no hallucination."""

    version = UNDERSTANDING_VERSION

    async def understand(self, transcript: Transcript) -> Understanding:
        entities: dict[str, Entity] = {}
        relations: list[Relation] = []
        units: list[SemanticUnit] = []

        # One pass over the whole transcript first. A recording's real subject
        # is usually an ordinary lowercase noun repeated throughout, which no
        # amount of per-sentence analysis will ever surface.
        vocabulary = nlp.salient_terms(transcript.text)

        for segment in transcript.segments:
            for span, text in self._clauses(segment):
                analysis = nlp.analyse(text, vocabulary=vocabulary)
                unit_entities = [
                    self._register(entities, mention.name, mention.type)
                    for mention in analysis.entities[:8]
                ]
                unit_relations = self._relations_for(
                    analysis.predicates, unit_entities, relations
                )
                units.append(
                    SemanticUnit(
                        span=span,
                        segment_ids=[segment.segment_id],
                        text=text,
                        proposition=_proposition(text),
                        intent=analysis.intent,
                        entity_ids=[e.entity_id for e in unit_entities],
                        relation_ids=[r.relation_id for r in unit_relations],
                        quantities=[
                            Quantity(
                                value=q.value,
                                unit=q.unit,
                                of_what=(q.of_what or "")[:200] or None,
                            )
                            for q in analysis.quantities[:8]
                        ],
                        keyphrases=analysis.keyphrases,
                        salience=_salience(analysis, text),
                        # Deliberately absent. A rule has no calibrated
                        # confidence, and inventing one would mislead every
                        # consumer that reads it.
                        confidence=None,
                    )
                )

        self._score_salience(entities, units)
        sentences = [unit.text for unit in units if unit.is_visualisable]
        return Understanding(
            organisation_id=transcript.organisation_id,
            project_id=transcript.project_id,
            transcript_id=transcript.transcript_id,
            topic=nlp.topic_of(
                [
                    nlp.MentionedEntity(e.name, e.type, int(e.salience * 10) + 1)
                    for e in entities.values()
                ],
                fallback=(sentences[0][:80] if sentences else ""),
            ),
            summary=nlp.summarise(sentences)[:2000],
            units=units,
            entities=list(entities.values()),
            relations=relations,
            provider=None,
            model=self.version,
            status=Status.READY,
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _clauses(segment: TranscriptSegment) -> list[tuple[TimeSpan, str]]:
        """Split a segment into clause-sized ideas, timed proportionally.

        Timing within a segment is apportioned by character count. That is an
        approximation, but it is bounded by the segment's own real boundaries,
        so it can never drift away from the audio.
        """
        pieces = [p.strip() for p in _CLAUSE.split(segment.text) if p and p.strip()]
        merged: list[str] = []
        for piece in pieces:
            if merged and len(piece) < MIN_CLAUSE_CHARS:
                merged[-1] = f"{merged[-1]} {piece}"
            else:
                merged.append(piece)
        if not merged:
            return [(segment.span, segment.text)]
        if len(merged) == 1:
            return [(segment.span, merged[0])]

        total = sum(len(piece) for piece in merged)
        out: list[tuple[TimeSpan, str]] = []
        cursor = segment.span.start
        for index, piece in enumerate(merged):
            share = (len(piece) / total) * segment.span.duration
            end = segment.span.end if index == len(merged) - 1 else cursor + share
            if end - cursor < 0.15:
                end = min(segment.span.end, cursor + 0.15)
            if end <= cursor:
                continue
            out.append((TimeSpan.of(round(cursor, 3), round(end, 3)), piece))
            cursor = end
        return out or [(segment.span, segment.text)]

    @staticmethod
    def _register(
        registry: dict[str, Entity], name: str, entity_type: EntityType
    ) -> Entity:
        key = name.lower()
        existing = registry.get(key)
        if existing:
            return existing
        entity = Entity(
            name=name,
            type=entity_type,
            canonical_name=name.strip(),
            salience=0.5,
        )
        registry[key] = entity
        return entity

    @staticmethod
    def _relations_for(
        predicates: list[str],
        unit_entities: list[Entity],
        sink: list[Relation],
    ) -> list[Relation]:
        """Attach predicates to entity pairs.

        Deliberately conservative: a relation is only created when the sentence
        offers a plausible subject and object, and dates take the ``_in_year``
        form rather than being treated as the object of an action. A wrong edge
        draws a wrong diagram, which is worse than drawing no diagram.
        """
        if not predicates or len(unit_entities) < 2:
            return []

        # "The transistor was invented at Bell Labs" — the thing invented is the
        # subject, the laboratory is where. Picking the first entity by mention
        # count gets this backwards whenever the organisation is named first.
        subjects = [
            e
            for e in unit_entities
            if e.type
            not in {
                EntityType.DATE,
                EntityType.ORGANIZATION,
                EntityType.PERSON,
                EntityType.LOCATION,
            }
        ] or [e for e in unit_entities if e.type is not EntityType.DATE]
        if not subjects:
            return []
        subject = subjects[0]

        created: list[Relation] = []
        for predicate in predicates[:3]:
            candidates = [e for e in unit_entities if e.entity_id != subject.entity_id]
            if predicate in {"invented_at", "created_by"}:
                preferred = [
                    e
                    for e in candidates
                    if e.type in {EntityType.ORGANIZATION, EntityType.PERSON, EntityType.LOCATION}
                ]
                date = next((e for e in candidates if e.type is EntityType.DATE), None)
                if date is not None:
                    created.append(
                        Relation(
                            subject_id=subject.entity_id,
                            predicate=f"{predicate.split('_')[0]}_in_year",
                            object_id=date.entity_id,
                        )
                    )
                candidates = preferred
            else:
                candidates = [e for e in candidates if e.type is not EntityType.DATE]
            if not candidates:
                continue
            created.append(
                Relation(
                    subject_id=subject.entity_id,
                    predicate=predicate,
                    object_id=candidates[0].entity_id,
                )
            )

        sink.extend(created)
        return created

    @staticmethod
    def _score_salience(
        entities: dict[str, Entity], units: list[SemanticUnit]
    ) -> None:
        """Salience by mention count, normalised against the most-mentioned."""
        counts: dict[str, int] = {}
        for unit in units:
            for entity_id in unit.entity_ids:
                counts[entity_id] = counts.get(entity_id, 0) + 1
        if not counts:
            return
        highest = max(counts.values())
        for entity in entities.values():
            entity.salience = round(
                min(1.0, 0.25 + 0.75 * (counts.get(entity.entity_id, 0) / highest)), 3
            )


def _proposition(text: str) -> str:
    """A one-line restatement. Extractive, so it can never add a claim."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= 200:
        return cleaned
    return cleaned[:197].rsplit(" ", 1)[0] + "..."


def _salience(analysis: nlp.Analysis, text: str) -> float:
    """How much this idea deserves the viewer's attention."""
    score = 0.45
    if analysis.intent in {
        SemanticIntent.CONCLUSION,
        SemanticIntent.DEFINITION,
        SemanticIntent.NUMERIC_FACT,
        SemanticIntent.COMPARISON,
    }:
        score += 0.25
    if analysis.intent in {SemanticIntent.FILLER, SemanticIntent.TRANSITION}:
        score -= 0.3
    score += min(0.2, 0.05 * len(analysis.entities))
    score += min(0.1, 0.05 * len(analysis.quantities))
    if len(text) > 120:
        score += 0.05
    return round(max(0.0, min(1.0, score)), 3)


# ---------------------------------------------------------------------------
# Model-backed engine
# ---------------------------------------------------------------------------

_INSTRUCTION = """\
You are the semantic understanding stage of a voice-to-video system.

Read the timestamped transcript and return the meaning as structured data that
validates against the `understanding` schema you have been given.

Rules:
- Every semantic unit must cover a real time span from the transcript and must
  cite the segment ids it draws from. Never invent timings.
- A sentence may contain several units; several sentences may form one unit.
- `proposition` restates the idea in one line. It must not add any claim the
  speaker did not make.
- Extract entities, relations and quantities only where the speaker stated them.
- Use the intent vocabulary exactly as defined by the schema.
- Return only JSON. No commentary, no markdown fence.
"""


@dataclass
class LlmUnderstandingEngine:
    """Semantic understanding via a text generation provider."""

    provider: object  # TextGenerationProvider
    fallback: UnderstandingEngine | None = None
    version: str = LLM_UNDERSTANDING_VERSION

    async def understand(self, transcript: Transcript) -> Understanding:
        request = GenerationRequest(
            organisation_id=transcript.organisation_id,
            project_id=transcript.project_id,
            kind=GenerationKind.TEXT,
            params=TextParams(
                instruction=_INSTRUCTION,
                input_json={
                    "language": transcript.language,
                    "segments": [
                        {
                            "segment_id": segment.segment_id,
                            "start": segment.span.start,
                            "end": segment.span.end,
                            "text": segment.text,
                        }
                        for segment in transcript.segments
                    ],
                },
                response_schema="understanding",
                temperature=0.1,
                max_output_tokens=16_000,
            ),
        )

        try:
            result = await self.provider.generate(request)  # type: ignore[attr-defined]
            if result.status is not Status.READY or not result.structured_output:
                raise ValidationFailed("understanding model returned no output")
            understanding = self._validate(
                result.structured_output, transcript, result.provider, result.model
            )
        except VTVError:
            if self.fallback is None:
                raise
            # Understanding is on the critical path: a failure here has no
            # per-scene blast radius to contain it. Falling back to the
            # deterministic engine produces a simpler video rather than none.
            return await self.fallback.understand(transcript)

        return understanding

    def _validate(
        self,
        payload: dict[str, object],
        transcript: Transcript,
        provider: str | None,
        model: str | None,
    ) -> Understanding:
        """Coerce a model response into the contract, or reject it.

        The repairs performed here are the conservative ones: strip a code
        fence, fill in the ids that identify the document, clamp timings into
        the recording. Anything requiring a *judgement* — a missing entity, an
        invented relation — is not repaired, because a plausible guess is how
        wrong data gets into a system that otherwise validates everything.
        """
        payload = dict(payload)
        payload["project_id"] = transcript.project_id
        payload["transcript_id"] = transcript.transcript_id
        payload["provider"] = provider
        payload["model"] = model
        payload["status"] = Status.READY.value
        payload.pop("schema_version", None)
        payload.pop("understanding_id", None)

        known_segments = {segment.segment_id for segment in transcript.segments}
        limit = transcript.span.end if transcript.span else 0.0
        units = payload.get("units")
        if isinstance(units, list):
            cleaned: list[dict[str, object]] = []
            cursor = 0.0
            for raw in units:
                if not isinstance(raw, dict):
                    continue
                span = raw.get("span")
                if not isinstance(span, dict):
                    continue
                try:
                    start = max(float(span["start"]), cursor)
                    end = min(float(span["end"]), limit)
                except (KeyError, TypeError, ValueError):
                    continue
                if end - start < 0.05:
                    continue
                raw["span"] = {"start": round(start, 3), "end": round(end, 3)}
                raw["segment_ids"] = [
                    s for s in (raw.get("segment_ids") or []) if s in known_segments
                ]
                raw.pop("unit_id", None)
                cleaned.append(raw)
                cursor = start
            payload["units"] = cleaned

        try:
            return Understanding.model_validate(payload)
        except Exception as exc:
            raise ValidationFailed(
                f"understanding response failed validation: {exc}",
                code=ErrorCode.UNDERSTANDING_FAILED,
            ) from exc


def extract_json(text: str) -> dict[str, object]:
    """Pull a JSON object out of a model response.

    Models wrap JSON in fences and prose no matter how firmly they are asked not
    to. This tolerates that and nothing more: if there is no balanced object in
    the response, it raises rather than guessing.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return dict(json.loads(stripped))
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    if start == -1:
        raise ValidationFailed("model response contained no JSON object")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return dict(json.loads(stripped[start : index + 1]))
                except json.JSONDecodeError as exc:
                    raise ValidationFailed("model response was not valid JSON") from exc
    raise ValidationFailed("model response contained an unterminated JSON object")


@dataclass
class UnderstandingService:
    """Runs an engine and reports on it."""

    engine: UnderstandingEngine
    events: EventSink

    async def understand(self, transcript: Transcript) -> Understanding:
        self.events.emit(
            EventName.UNDERSTANDING_STARTED,
            project_id=transcript.project_id,
            data={"segments": len(transcript.segments)},
        )
        timer = Timer()
        understanding = await self.engine.understand(transcript)
        self.events.emit(
            EventName.UNDERSTANDING_COMPLETED,
            project_id=transcript.project_id,
            duration_ms=timer.elapsed_ms,
            data={
                "engine": self.engine.version,
                "units": len(understanding.units),
                "entities": len(understanding.entities),
                "relations": len(understanding.relations),
            },
        )
        return understanding


__all__ = [
    "LLM_UNDERSTANDING_VERSION",
    "UNDERSTANDING_VERSION",
    "HeuristicUnderstandingEngine",
    "LlmUnderstandingEngine",
    "UnderstandingEngine",
    "UnderstandingService",
    "extract_json",
]
