"""The visual language: what our animation engine knows how to draw.

This is the beginning of the proprietary layer described in Section 5 of the
engineering brief. Over time this vocabulary — not any particular model — is what
makes the product hard to copy: a growing library of visual forms that reliably
communicate specific kinds of meaning.

**The hard rule of this module: a specification is data, never code.**

A language model may choose a primitive and fill in its parameters. It may never
emit React, SVG, CSS, shader source, or anything else that we would execute or
inject. Rendering code is written by us, reviewed by us, and tested by us; the
model only ever selects and parameterises it. This is simultaneously a security
boundary (nothing model-authored is executed) and a quality boundary (every
visual the system can produce is one we have deliberately designed).

Stage 0 defines the envelope and six representative primitives. Stage 8 extends
the union; the envelope does not change.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vtv.contracts.base import Duration, VTVModel


class VisualPrimitive(str, Enum):
    """Every visual form the animation engine can render.

    Adding a member here is a deliberate product decision: it means committing
    to design it, render it well, and teach the Visual Director when to pick it.
    """

    TYPOGRAPHY = "typography"
    CHART = "chart"
    TIMELINE = "timeline"
    NETWORK = "network"
    COMPARISON = "comparison"
    MAP = "map"


class CameraMotion(str, Enum):
    """Motion applied to a still image so it does not sit dead on screen."""

    NONE = "none"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    PAN_UP = "pan_up"
    PAN_DOWN = "pan_down"
    KEN_BURNS = "ken_burns"


class Emphasis(str, Enum):
    """How strongly a visual should assert itself against the narration."""

    SUBTLE = "subtle"
    NORMAL = "normal"
    STRONG = "strong"


class BaseSpec(VTVModel):
    """Fields shared by every animation specification."""

    #: How long the animation should ideally run. The Timeline Engine is free to
    #: stretch or compress within the primitive's declared tolerance, so this is
    #: a request rather than a guarantee.
    preferred_duration: Duration = 5.0
    emphasis: Emphasis = Emphasis.NORMAL


class TypographySpec(BaseSpec):
    """Animated text. The correct answer far more often than it feels like.

    A plain factual statement, a definition, a punchy conclusion — these are
    communicated better and more cheaply by well-set moving type than by a
    literal image of the words' subject.
    """

    primitive: Literal[VisualPrimitive.TYPOGRAPHY] = VisualPrimitive.TYPOGRAPHY
    headline: str = Field(min_length=1, max_length=120)
    subline: str | None = Field(default=None, max_length=200)
    #: Words to give visual weight, matched case-insensitively in the headline.
    highlight: list[str] = Field(default_factory=list, max_length=8)
    reveal: Literal["fade", "typewriter", "word_by_word", "slide"] = "word_by_word"


class ChartKind(str, Enum):
    LINE = "line"
    BAR = "bar"
    COLUMN = "column"
    AREA = "area"
    PIE = "pie"
    SCATTER = "scatter"


class DataPoint(VTVModel):
    label: str = Field(min_length=1, max_length=64)
    value: float


class ChartSeries(VTVModel):
    name: str = Field(min_length=1, max_length=64)
    points: list[DataPoint] = Field(min_length=1, max_length=200)


class ChartSpec(BaseSpec):
    """A quantitative claim, drawn.

    "The population went from one billion to eight billion" is a chart. Asking a
    video model for it costs orders of magnitude more, takes far longer, and is
    less accurate (Rule 6).
    """

    primitive: Literal[VisualPrimitive.CHART] = VisualPrimitive.CHART
    kind: ChartKind
    title: str | None = Field(default=None, max_length=120)
    x_label: str | None = Field(default=None, max_length=64)
    y_label: str | None = Field(default=None, max_length=64)
    series: list[ChartSeries] = Field(min_length=1, max_length=8)
    #: Draw values in as the narration reaches them rather than all at once.
    animate_in: bool = True

    @model_validator(mode="after")
    def _pie_has_one_series(self) -> ChartSpec:
        if self.kind is ChartKind.PIE and len(self.series) != 1:
            raise ValueError("a pie chart takes exactly one series")
        return self


class TimelineEvent(VTVModel):
    label: str = Field(min_length=1, max_length=120)
    #: The date as spoken, kept verbatim; the renderer formats it. Storing the
    #: speaker's own phrasing avoids inventing a precision they did not claim.
    when: str = Field(min_length=1, max_length=64)
    #: Sort key. Years before the common era are negative.
    sort_value: float
    detail: str | None = Field(default=None, max_length=200)


class TimelineSpec(BaseSpec):
    """Events in order. The default form for anything historical."""

    primitive: Literal[VisualPrimitive.TIMELINE] = VisualPrimitive.TIMELINE
    title: str | None = Field(default=None, max_length=120)
    events: list[TimelineEvent] = Field(min_length=1, max_length=12)
    orientation: Literal["horizontal", "vertical"] = "horizontal"

    @model_validator(mode="after")
    def _events_sorted(self) -> TimelineSpec:
        values = [event.sort_value for event in self.events]
        if values != sorted(values):
            raise ValueError("timeline events must be supplied in chronological order")
        return self


class NetworkNode(VTVModel):
    key: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=64)
    group: str | None = Field(default=None, max_length=32)


class NetworkEdge(VTVModel):
    source: str = Field(min_length=1, max_length=48)
    target: str = Field(min_length=1, max_length=48)
    label: str | None = Field(default=None, max_length=48)
    directed: bool = True


class NetworkSpec(BaseSpec):
    """Nodes and edges: relationships, systems, architectures, distributed ideas.

    This is the direct rendering of the relation graph from the semantic layer,
    which is why relations are modelled as first-class data rather than prose.
    """

    primitive: Literal[VisualPrimitive.NETWORK] = VisualPrimitive.NETWORK
    title: str | None = Field(default=None, max_length=120)
    nodes: list[NetworkNode] = Field(min_length=2, max_length=24)
    edges: list[NetworkEdge] = Field(default_factory=list, max_length=64)
    layout: Literal["force", "radial", "layered", "circular"] = "force"
    animation: Literal["appear", "propagate", "build", "pulse"] = "build"

    @model_validator(mode="after")
    def _edges_reference_nodes(self) -> NetworkSpec:
        keys = {node.key for node in self.nodes}
        if len(keys) != len(self.nodes):
            raise ValueError("network node keys must be unique")
        for edge in self.edges:
            for role, ref in (("source", edge.source), ("target", edge.target)):
                if ref not in keys:
                    raise ValueError(f"edge {role} {ref!r} is not a declared node")
        return self


class ComparisonSide(VTVModel):
    title: str = Field(min_length=1, max_length=64)
    points: list[str] = Field(default_factory=list, max_length=6)
    #: Optional asset to illustrate this side, resolved by the asset engine.
    asset_query: str | None = Field(default=None, max_length=200)


class ComparisonSpec(BaseSpec):
    """Two things held against each other. Split screen, then difference."""

    primitive: Literal[VisualPrimitive.COMPARISON] = VisualPrimitive.COMPARISON
    left: ComparisonSide
    right: ComparisonSide
    #: Rendered between the two sides, e.g. "→" for replacement, "vs" for rivalry.
    connector: Literal["versus", "arrow", "none"] = "versus"


class MapMarker(VTVModel):
    label: str = Field(min_length=1, max_length=64)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class MapSpec(BaseSpec):
    """Where something happened."""

    primitive: Literal[VisualPrimitive.MAP] = VisualPrimitive.MAP
    markers: list[MapMarker] = Field(min_length=1, max_length=12)
    scope: Literal["world", "continent", "country", "region", "city"] = "world"
    #: Draw a line between markers in order, e.g. for journeys or trade routes.
    connect_markers: bool = False


#: Discriminated union of every animation specification. Pydantic selects the
#: right model from the ``primitive`` field, so a malformed or half-hallucinated
#: spec fails validation instead of being coerced into the wrong shape.
AnimationSpec = Annotated[
    TypographySpec
    | ChartSpec
    | TimelineSpec
    | NetworkSpec
    | ComparisonSpec
    | MapSpec,
    Field(discriminator="primitive"),
]

#: Every concrete spec class, for tests and for the JSON Schema exporter.
ANIMATION_SPEC_TYPES: tuple[type[BaseSpec], ...] = (
    TypographySpec,
    ChartSpec,
    TimelineSpec,
    NetworkSpec,
    ComparisonSpec,
    MapSpec,
)


__all__ = [
    "ANIMATION_SPEC_TYPES",
    "AnimationSpec",
    "BaseSpec",
    "CameraMotion",
    "ChartKind",
    "ChartSeries",
    "ChartSpec",
    "ComparisonSide",
    "ComparisonSpec",
    "DataPoint",
    "Emphasis",
    "MapMarker",
    "MapSpec",
    "NetworkEdge",
    "NetworkNode",
    "NetworkSpec",
    "TimelineEvent",
    "TimelineSpec",
    "TypographySpec",
    "VisualPrimitive",
]
