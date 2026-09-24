"""Stage 24 — factuality: nothing on screen that is not in the source.

The failure this prevents is specific and severe. A chart is the most
authoritative-looking object a video can contain: a viewer who would question a
sentence will accept a bar chart without reading the axis. So a chart built from
numbers the system inferred, rounded, extrapolated or invented does more damage
than no chart at all — it launders a guess into evidence.

The rule is therefore simple and absolute: **every number, date and named place
drawn on screen must trace to a span of the source.** Not "be plausible", not
"be consistent with" — trace, to a specific quantity the speaker stated or a
specific cell the document contained.

What that means in practice:

* A `ChartSpec` whose values are not all found in the transcript or a source
  table is refused, and the scene degrades to a visual that makes no
  quantitative claim.
* A `TimelineSpec` whose dates were not stated is refused.
* A `MapSpec` whose places were not named is refused.
* A `TypographySpec` quoting the speaker is checked against what they said.

Refusal is a degradation, recorded like any other (Rule 8), so the video still
renders and the event stream says why the chart is missing. Silence would be
worse than the chart.

**What this is not.** It is not a fact checker. It does not know whether the
speaker was right — only whether the *system* added anything they did not say.
That distinction is worth stating plainly to customers: we guarantee fidelity to
the source, not truth about the world.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from vtv.contracts.errors import DegradationReason, DegradationStep
from vtv.contracts.semantics import Quantity, Understanding
from vtv.contracts.source import TableData
from vtv.contracts.transcript import Transcript
from vtv.contracts.visual_language import (
    AnimationSpec,
    ChartSpec,
    ComparisonSpec,
    MapSpec,
    TimelineSpec,
    TypographySpec,
    VisualPrimitive,
)

#: Two values are the same claim if they agree to this relative tolerance. Not
#: zero: a speaker says "about eight billion" and the extractor stores
#: 8_000_000_000, while a table says 8_045_311_447. Both are the same claim.
#: Wide enough to survive rounding, far too narrow to admit a different number.
RELATIVE_TOLERANCE = 0.02

#: Below this magnitude a relative tolerance is meaningless, so compare
#: absolutely: 0.02 and 0.021 are different claims, not rounding.
ABSOLUTE_FLOOR = 1.0


class GroundingVerdict(str, Enum):
    """How well a visual is anchored to the source."""

    #: Every claim traces to the source.
    GROUNDED = "grounded"
    #: The visual makes no factual claim at all — an abstract background, a
    #: mood shot. Nothing to ground, and refusing it would be nonsense.
    NO_CLAIM = "no_claim"
    #: At least one claim does not trace. The visual is refused.
    UNGROUNDED = "ungrounded"


@dataclass(frozen=True)
class GroundingResult:
    """The verdict plus the evidence for it."""

    verdict: GroundingVerdict
    #: Claims that traced, as ``(claim, where it was found)``.
    supported: tuple[tuple[str, str], ...] = ()
    #: Claims that did not. Non-empty exactly when the verdict is UNGROUNDED.
    unsupported: tuple[str, ...] = ()

    @property
    def is_acceptable(self) -> bool:
        return self.verdict is not GroundingVerdict.UNGROUNDED

    def reason(self) -> str:
        if self.is_acceptable:
            return "every claim traces to the source"
        listed = ", ".join(self.unsupported[:4])
        more = "" if len(self.unsupported) <= 4 else f" and {len(self.unsupported) - 4} more"
        return f"not stated in the source: {listed}{more}"

    def as_degradation(self, *, from_strategy: str, to_strategy: str) -> DegradationStep:
        return DegradationStep(
            from_strategy=from_strategy,
            to_strategy=to_strategy,
            # A refusal on factual grounds is a safety refusal, not a provider
            # failure: nothing broke, we declined.
            reason=DegradationReason.SAFETY_REFUSED,
        )


@dataclass
class Evidence:
    """Everything the source actually asserts, indexed for lookup.

    Built once per project. Numbers, years and place names are extracted from
    the transcript, the understanding layer's quantities, and any tables the
    document carried — the three places a claim can legitimately come from.
    """

    numbers: list[float] = field(default_factory=list)
    #: Where each number came from, parallel to `numbers`.
    number_sources: list[str] = field(default_factory=list)
    years: set[int] = field(default_factory=set)
    #: Lowercased significant words, for checking names and labels.
    words: set[str] = field(default_factory=set)
    #: The raw text, for quote checking.
    text: str = ""

    @classmethod
    def build(
        cls,
        *,
        transcript: Transcript | None = None,
        narration: str | None = None,
        understanding: Understanding | None = None,
        tables: list[TableData] | None = None,
    ) -> Evidence:
        """Index everything the source asserts.

        ``narration`` exists because the Visual Director receives a scene graph
        rather than a transcript, and building evidence without the spoken words
        produced a real false positive: a chart correctly labelled "percent" was
        refused because "percent" appeared nowhere in the extracted entities.
        The words the speaker used are the primary evidence and must always be
        supplied — from the transcript, or from the scenes' narration.
        """
        evidence = cls()
        spoken_parts = [
            part
            for part in (
                " ".join(segment.text for segment in transcript.segments)
                if transcript is not None
                else None,
                narration,
            )
            if part
        ]
        if spoken_parts:
            spoken = " ".join(spoken_parts)
            evidence.text = spoken
            evidence.words |= _significant_words(spoken)
            for value, raw in _numbers_in(spoken):
                evidence.numbers.append(value)
                evidence.number_sources.append(f"spoken: {raw}")
            evidence.years |= _years_in(spoken)

        if understanding is not None:
            for unit in understanding.units:
                for quantity in unit.quantities:
                    evidence.numbers.append(quantity.value)
                    evidence.number_sources.append(
                        f"stated: {_describe_quantity(quantity)}"
                    )
            for entity in understanding.entities:
                evidence.words |= _significant_words(entity.name)
                if entity.canonical_name:
                    evidence.words |= _significant_words(entity.canonical_name)
                for alias in entity.aliases:
                    evidence.words |= _significant_words(alias)

        for index, table in enumerate(tables or []):
            evidence.words |= _significant_words(
                " ".join([table.caption or "", *table.headers])
            )
            for row in table.rows:
                evidence.words |= _significant_words(" ".join(row))
                for cell in row:
                    for value, raw in _numbers_in(cell):
                        evidence.numbers.append(value)
                        evidence.number_sources.append(f"table {index + 1}: {raw}")
                    evidence.years |= _years_in(cell)
        return evidence

    def supports_number(self, value: float) -> str | None:
        """Where this value came from, or ``None`` if it did not."""
        for candidate, origin in zip(
            self.numbers, self.number_sources, strict=False
        ):
            if _same_value(candidate, value):
                return origin
        return None

    def supports_year(self, year: int) -> bool:
        return year in self.years

    def supports_phrase(self, phrase: str, *, threshold: float = 0.6) -> bool:
        """Whether a label is made of words the source used.

        A threshold rather than an exact match because a label is a shortened
        form of what was said — "Q1 revenue" for "revenue in the first quarter"
        — and demanding equality would refuse every legitimate label. Words the
        source never used at all are what this catches.
        """
        words = _significant_words(phrase)
        if not words:
            return True
        overlap = len(words & self.words) / len(words)
        return overlap >= threshold


def check_spec(spec: AnimationSpec, evidence: Evidence) -> GroundingResult:
    """Whether a drawn visual claims anything the source did not say."""
    checker = _CHECKERS.get(spec.primitive)
    if checker is None:
        return GroundingResult(GroundingVerdict.NO_CLAIM)
    return checker(spec, evidence)


def _check_chart(spec: AnimationSpec, evidence: Evidence) -> GroundingResult:
    """The most important check in the system.

    A chart is read as evidence. Every plotted value must trace, and a chart
    with even one invented point is refused whole — plotting the supported
    subset would silently change the shape of the claim.
    """
    assert isinstance(spec, ChartSpec)
    supported: list[tuple[str, str]] = []
    unsupported: list[str] = []

    for series in spec.series:
        for point in series.points:
            origin = evidence.supports_number(point.value)
            claim = f"{point.label}={point.value:g}"
            if origin is None:
                unsupported.append(claim)
            else:
                supported.append((claim, origin))

    # Axis and series labels are checked too, but only for invented vocabulary:
    # a chart titled "Projected growth" over historical data is a claim the
    # source did not make.
    for label in (spec.title, spec.y_label, spec.x_label):
        if label and not evidence.supports_phrase(label, threshold=0.5):
            unsupported.append(f"label {label!r}")

    return _verdict(supported, unsupported)


def _check_timeline(spec: AnimationSpec, evidence: Evidence) -> GroundingResult:
    assert isinstance(spec, TimelineSpec)
    supported: list[tuple[str, str]] = []
    unsupported: list[str] = []
    for event in spec.events:
        year = _year_of(event.when) or _year_of(str(event.sort_value))
        if year is not None and not evidence.supports_year(year):
            unsupported.append(f"{event.label} ({event.when})")
            continue
        if not evidence.supports_phrase(event.label):
            unsupported.append(f"event {event.label!r}")
            continue
        supported.append((f"{event.label} ({event.when})", "spoken"))
    return _verdict(supported, unsupported)


def _check_map(spec: AnimationSpec, evidence: Evidence) -> GroundingResult:
    """A map places a claim on the earth, which is a strong assertion.

    A marker for a place the speaker never named is the system deciding where
    something happened.
    """
    assert isinstance(spec, MapSpec)
    supported: list[tuple[str, str]] = []
    unsupported: list[str] = []
    for marker in spec.markers:
        if evidence.supports_phrase(marker.label, threshold=0.5):
            supported.append((marker.label, "named"))
        else:
            unsupported.append(f"place {marker.label!r}")
    return _verdict(supported, unsupported)


def _check_typography(spec: AnimationSpec, evidence: Evidence) -> GroundingResult:
    """Words on screen attributed to the speaker must be the speaker's words."""
    assert isinstance(spec, TypographySpec)
    claims = [line for line in (spec.headline, spec.subline) if line]
    supported: list[tuple[str, str]] = []
    unsupported: list[str] = []
    for line in claims:
        # A number on screen is a claim even inside a headline.
        for value, raw in _numbers_in(line):
            origin = evidence.supports_number(value)
            if origin is None:
                unsupported.append(f"figure {raw!r}")
            else:
                supported.append((raw, origin))
        if not evidence.supports_phrase(line, threshold=0.5):
            unsupported.append(f"text {line[:40]!r}")
        else:
            supported.append((line[:40], "spoken"))
    return _verdict(supported, unsupported)


def _check_comparison(spec: AnimationSpec, evidence: Evidence) -> GroundingResult:
    assert isinstance(spec, ComparisonSpec)
    supported: list[tuple[str, str]] = []
    unsupported: list[str] = []
    for side in (spec.left, spec.right):
        if not evidence.supports_phrase(side.title, threshold=0.5):
            unsupported.append(f"side {side.title!r}")
            continue
        supported.append((side.title, "spoken"))
        for point in side.points:
            for value, raw in _numbers_in(point):
                if evidence.supports_number(value) is None:
                    unsupported.append(f"figure {raw!r}")
    return _verdict(supported, unsupported)


_CHECKERS = {
    VisualPrimitive.CHART: _check_chart,
    VisualPrimitive.TIMELINE: _check_timeline,
    VisualPrimitive.MAP: _check_map,
    VisualPrimitive.TYPOGRAPHY: _check_typography,
    VisualPrimitive.COMPARISON: _check_comparison,
}


def _verdict(
    supported: list[tuple[str, str]], unsupported: list[str]
) -> GroundingResult:
    if unsupported:
        return GroundingResult(
            GroundingVerdict.UNGROUNDED,
            supported=tuple(supported),
            unsupported=tuple(unsupported),
        )
    if not supported:
        return GroundingResult(GroundingVerdict.NO_CLAIM)
    return GroundingResult(GroundingVerdict.GROUNDED, supported=tuple(supported))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

#: Numbers with optional thousands separators, decimals, percent and a scale
#: word. Deliberately permissive on input and strict on comparison.
_NUMBER = re.compile(
    r"(-?\d[\d,]*\.?\d*)\s*(percent|%|thousand|million|billion|trillion|k|m|bn)?",
    re.I,
)

_SCALES: dict[str, float] = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "m": 1e6,
    "billion": 1e9, "bn": 1e9,
    "trillion": 1e12,
}

#: Spoken numbers. A speaker says "nineteen forty seven", and a transcript
#: records those words, so a timeline of 1947 must be able to find it.
_SPOKEN_UNITS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
}

_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")

_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from", "had", "has", "have", "in", "into", "is", "it", "its", "of", "on", "or", "that", "the", "their", "there", "these", "this", "to", "was", "were", "which", "will", "with", "we", "you", "our", "your", "they", "he", "she", "her", "his", "them", "then", "than", "so", "if", "not", "no", "all", "can", "could", "would", "should", "about", "over", "under", "more", "most", "less", "least", "very", "just", "also"]
)


def _numbers_in(text: str) -> list[tuple[float, str]]:
    """Every numeric claim in a string, with the text it came from."""
    found: list[tuple[float, str]] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(0).strip()
        digits = match.group(1).replace(",", "")
        try:
            value = float(digits)
        except ValueError:
            continue
        scale = (match.group(2) or "").lower()
        if scale in _SCALES:
            value *= _SCALES[scale]
        found.append((value, raw))
    found.extend(_spoken_numbers(text))
    return found


def _spoken_numbers(text: str) -> list[tuple[float, str]]:
    """Numbers written as words.

    Handles the forms a narrator actually uses — "eight billion", "nineteen
    forty seven", "ninety nine percent" — rather than attempting general
    English number parsing, which is a research project with a poor return.
    """
    found: list[tuple[float, str]] = []
    words = re.findall(r"[a-z]+", text.lower())
    index = 0
    while index < len(words):
        word = words[index]
        if word not in _SPOKEN_UNITS:
            index += 1
            continue
        run = [word]
        value: float = float(_SPOKEN_UNITS[word])
        cursor = index + 1
        while cursor < len(words) and words[cursor] in _SPOKEN_UNITS:
            part = _SPOKEN_UNITS[words[cursor]]
            if part == 100 and value:
                value *= 100
            else:
                value += part
            run.append(words[cursor])
            cursor += 1
        scale = words[cursor] if cursor < len(words) else ""
        if scale in _SCALES:
            value *= _SCALES[scale]
            run.append(scale)
            cursor += 1
        found.append((float(value), " ".join(run)))
        # "nineteen forty seven" is also the year 1947, which is how a spoken
        # date reaches a timeline.
        if len(run) >= 2 and 19 <= _SPOKEN_UNITS.get(run[0], 0) <= 20:
            tail = sum(_SPOKEN_UNITS.get(part, 0) for part in run[1:])
            if 0 <= tail <= 99:
                found.append(
                    (float(_SPOKEN_UNITS[run[0]] * 100 + tail), " ".join(run))
                )
        index = cursor
    return found


def _years_in(text: str) -> set[int]:
    years = {int(match.group(1)) for match in _YEAR.finditer(text)}
    for value, _raw in _spoken_numbers(text):
        if 1000 <= value <= 2100 and float(value).is_integer():
            years.add(int(value))
    return years


def _year_of(text: str) -> int | None:
    match = _YEAR.search(text)
    if match:
        return int(match.group(1))
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if 1000 <= value <= 2100 else None


def _same_value(left: float, right: float) -> bool:
    if left == right:
        return True
    if abs(left) < ABSOLUTE_FLOOR or abs(right) < ABSOLUTE_FLOOR:
        return abs(left - right) < 1e-9
    return abs(left - right) / max(abs(left), abs(right)) <= RELATIVE_TOLERANCE


def _significant_words(text: str) -> set[str]:
    normalised = unicodedata.normalize("NFKD", text.lower())
    return {
        word
        for word in re.findall(r"[a-z0-9]+", normalised)
        if len(word) > 2 and word not in _STOPWORDS
    }


def _describe_quantity(quantity: Quantity) -> str:
    unit = f" {quantity.unit}" if quantity.unit else ""
    return f"{quantity.value:g}{unit}"


__all__ = [
    "ABSOLUTE_FLOOR",
    "RELATIVE_TOLERANCE",
    "Evidence",
    "GroundingResult",
    "GroundingVerdict",
    "check_spec",
]
