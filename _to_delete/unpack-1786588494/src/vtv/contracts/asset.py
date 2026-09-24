"""Assets and their provenance.

Two commitments are encoded here as executable rules rather than as prose in a
policy document.

**Nothing is usable until proven usable (Rule 5).** Rights are tri-state:
``ALLOWED``, ``DENIED``, ``UNKNOWN``. ``UNKNOWN`` behaves exactly like
``DENIED`` for every commercial decision the system makes. An image found on the
open web with no licence metadata is not "probably fine", it is unusable. This
is the single most expensive mistake a media company can make at scale, and it
is cheap to prevent on day one and ruinous to retrofit.

**Every external asset can answer "where did you come from?"** Provenance is a
required field, not an optional annotation. When an enterprise customer asks
where a frame came from — and they will — the answer is a database lookup, not
an investigation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Duration,
    Id,
    IdPrefix,
    ObjectRef,
    RootDocument,
    Timestamped,
    VTVModel,
    new_id,
    utc_now,
)
from vtv.contracts.errors import Status


class AssetKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    VECTOR = "vector"
    FONT = "font"


class AssetSource(str, Enum):
    """Where an asset came from.

    ``WEB_SCRAPE`` exists in this enum only so that it can be *rejected* by
    name. Naming the forbidden case makes the prohibition testable; leaving it
    out would just mean someone eventually mislabels scraped material as
    ``OPENVERSE``.
    """

    #: Assets we created or licensed outright and hold in our own library.
    INTERNAL_LIBRARY = "internal_library"
    #: Supplied by the user for their own project.
    USER_UPLOAD = "user_upload"
    #: Produced by our own animation engine from an AnimationSpec.
    PROGRAMMATIC = "programmatic"
    #: Produced by an image or video generation provider.
    GENERATED = "generated"
    #: Open-licence aggregators and commons collections.
    OPENVERSE = "openverse"
    WIKIMEDIA_COMMONS = "wikimedia_commons"
    #: A commercial stock provider we hold a contract with.
    STOCK_PARTNER = "stock_partner"
    #: Never permitted. Present so that it can be explicitly refused.
    WEB_SCRAPE = "web_scrape"


#: Sources that originate outside our own systems and therefore must carry
#: provenance and a licence before they may be placed in a video.
EXTERNAL_SOURCES: frozenset[AssetSource] = frozenset(
    {
        AssetSource.OPENVERSE,
        AssetSource.WIKIMEDIA_COMMONS,
        AssetSource.STOCK_PARTNER,
    }
)

#: Sources the system refuses outright.
FORBIDDEN_SOURCES: frozenset[AssetSource] = frozenset({AssetSource.WEB_SCRAPE})


class Permission(str, Enum):
    """Tri-state right. ``UNKNOWN`` is treated as ``DENIED`` at every decision
    point; it exists as a distinct value only so that we can tell "we checked and
    the answer was no" apart from "we never established the answer"."""

    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = "unknown"

    @property
    def is_permitted(self) -> bool:
        return self is Permission.ALLOWED


class License(VTVModel):
    """The terms under which an asset may be used."""

    #: SPDX identifier where one exists, e.g. ``CC-BY-4.0``, ``CC0-1.0``.
    spdx_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2048)

    commercial_use: Permission = Permission.UNKNOWN
    modification: Permission = Permission.UNKNOWN
    attribution_required: bool = True
    share_alike: bool = False
    #: Some licences forbid implying endorsement or use in certain contexts.
    restrictions: list[str] = Field(default_factory=list, max_length=16)

    @property
    def is_commercially_usable(self) -> bool:
        """The gate every external asset must pass.

        Modification permission is required as well as commercial permission,
        because the pipeline crops, colour-grades, animates and composites
        everything it touches — we are never using an asset unmodified.
        """
        return (
            self.commercial_use.is_permitted
            and self.modification.is_permitted
        )


class AssetProvenance(VTVModel):
    """The complete origin record for an external asset."""

    source: AssetSource
    #: The provider's own identifier for this item.
    source_id: str = Field(min_length=1, max_length=256)
    #: Where a human can go to verify this record.
    original_url: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    creator: str | None = Field(default=None, max_length=300)
    creator_url: str | None = Field(default=None, max_length=2048)
    license: License
    retrieved_at: datetime = Field(default_factory=utc_now)
    #: The raw metadata blob from the provider, kept verbatim. If our parsing of
    #: a licence field is ever wrong, this is what lets us re-derive it for every
    #: asset already in the system instead of losing the evidence.
    raw_metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _refuse_forbidden_sources(self) -> AssetProvenance:
        if self.source in FORBIDDEN_SOURCES:
            raise ValueError(
                f"assets from {self.source.value} may never enter the system"
            )
        return self

    def attribution_line(self) -> str | None:
        """The credit that must appear if this asset is used.

        Returned as data so that the renderer can place it on screen and the
        project can assemble a credits list, rather than each caller inventing
        its own format.
        """
        if not self.license.attribution_required:
            return None
        parts = [self.title or "Untitled"]
        if self.creator:
            parts.append(f"by {self.creator}")
        parts.append(f"({self.license.spdx_id or self.license.name})")
        return " ".join(parts)


class AssetDimensions(VTVModel):
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    duration_seconds: Duration | None = None
    frame_rate: float | None = Field(default=None, gt=0, le=240)

    @property
    def aspect(self) -> float:
        return self.width / self.height


class Asset(RootDocument, Timestamped):
    """A concrete piece of media the renderer can place on screen."""

    document_name = "asset"

    asset_id: Id = Field(default_factory=lambda: new_id(IdPrefix.ASSET))
    project_id: Id | None = Field(
        default=None,
        description="Null for shared library assets reusable across projects.",
    )

    kind: AssetKind
    source: AssetSource
    #: Where the bytes live. Absent while the asset is still being fetched or
    #: generated, which is why ``status`` exists.
    object: ObjectRef | None = None
    dimensions: AssetDimensions | None = None

    provenance: AssetProvenance | None = None
    #: For ``GENERATED`` assets: the generation record that produced them, so a
    #: generated frame is as traceable as a licensed one.
    generation_id: Id | None = None

    #: Free-text description used for search, deduplication and alt text.
    description: str | None = Field(default=None, max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=32)

    status: Status = Status.PENDING

    @model_validator(mode="after")
    def _enforce_provenance_and_licensing(self) -> Asset:
        if self.source in FORBIDDEN_SOURCES:
            raise ValueError(f"source {self.source.value} is not permitted")

        if self.source in EXTERNAL_SOURCES:
            if self.provenance is None:
                raise ValueError(
                    f"asset from {self.source.value} requires provenance; "
                    "external media without a recorded origin may not be used"
                )
            if self.provenance.source is not self.source:
                raise ValueError(
                    "asset.source and provenance.source must agree "
                    f"({self.source.value} vs {self.provenance.source.value})"
                )

        if self.source is AssetSource.GENERATED and self.generation_id is None:
            raise ValueError("a generated asset must reference its generation record")

        if self.status is Status.READY and self.object is None:
            raise ValueError("a READY asset must have an object reference")
        return self

    @property
    def is_commercially_usable(self) -> bool:
        """Whether this asset may appear in a video a customer will publish.

        Internal, user-supplied, programmatic and generated assets are governed
        by our own contracts and provider terms. External assets must present a
        licence that positively permits commercial use and modification; unknown
        rights fail closed.
        """
        if self.source in FORBIDDEN_SOURCES:
            return False
        if self.source in EXTERNAL_SOURCES:
            return self.provenance is not None and (
                self.provenance.license.is_commercially_usable
            )
        return True

    def attribution_line(self) -> str | None:
        return self.provenance.attribution_line() if self.provenance else None


__all__ = [
    "EXTERNAL_SOURCES",
    "FORBIDDEN_SOURCES",
    "Asset",
    "AssetDimensions",
    "AssetKind",
    "AssetProvenance",
    "AssetSource",
    "License",
    "Permission",
]
