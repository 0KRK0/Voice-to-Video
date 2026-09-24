"""Foundational value types shared by every Voice-to-Video contract.

This module is deliberately dependency-light: standard library + pydantic only.
Nothing here may import from :mod:`vtv.ports` or from any vendor SDK.

Design notes
------------
* **Time is seconds, relative to the recording.** The user's narration is the
  primary temporal reference for the whole system (see ``docs/ARCHITECTURE.md``
  and Stage 10 in ``docs/ROADMAP.md``). Every ``TimeSpan`` in the pipeline is
  expressed on that one clock so that transcript, scenes, visuals and captions
  can never silently drift apart.
* **Storage is referenced, never pathed.** Domain models carry an
  :class:`ObjectRef`, not a filesystem path, so business logic stays independent
  of where bytes physically live (Rule 14).
* **Identifiers are prefixed and typed.** ``scn_...`` can never be accidentally
  passed where an ``ast_...`` is expected, and the prefix makes logs, traces and
  database rows self-describing.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, ClassVar, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Contract versioning
# ---------------------------------------------------------------------------

#: Version of the contract bundle as a whole. Bump the minor component for
#: backwards-compatible additions, the major component for breaking changes.
#: Every persisted root document records the version it was written with so that
#: old projects remain regenerable (Rule 12).
CONTRACTS_VERSION: Final[str] = "1.0"


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

class IdPrefix(str, Enum):
    """Registry of every identifier prefix used in the system."""

    PROJECT = "prj"
    RECORDING = "rec"
    TRANSCRIPT = "tsc"
    SEGMENT = "seg"
    SEMANTIC_UNIT = "sem"
    ENTITY = "ent"
    RELATION = "rel"
    SCENE_GRAPH = "sgr"
    SCENE = "scn"
    SHOT = "sht"
    VISUAL_PLAN = "vpl"
    ASSET = "ast"
    GENERATION = "gen"
    TIMELINE = "tml"
    CLIP = "clp"
    CAPTION = "cap"
    RENDER_JOB = "rnd"


_ID_RE = re.compile(r"^[a-z]{3}_[0-9a-hjkmnp-tv-z]{20,26}$")

#: An opaque, prefixed, URL-safe identifier such as ``scn_01h9zk3m...``.
Id = Annotated[str, StringConstraints(pattern=_ID_RE.pattern, strip_whitespace=True)]

# Crockford-style base32 alphabet: no I, L, O or U, so ids survive being read
# aloud, copied out of a log, or typed into a support ticket.
_ALPHABET: Final[str] = "0123456789abcdefghjkmnpqrstvwxyz"


def new_id(prefix: IdPrefix | str, *, rng: secrets.SystemRandom | None = None) -> str:
    """Mint a new prefixed identifier.

    The random component is 24 characters of Crockford base32 (~120 bits), which
    is collision-free for any volume this system will ever see and requires no
    coordination between services.
    """
    p = prefix.value if isinstance(prefix, IdPrefix) else prefix
    if not re.fullmatch(r"[a-z]{3}", p):
        raise ValueError(f"id prefix must be exactly three lowercase letters, got {p!r}")
    source = rng or secrets.SystemRandom()
    body = "".join(source.choice(_ALPHABET) for _ in range(24))
    return f"{p}_{body}"


def id_prefix_of(value: str) -> str:
    """Return the three-letter prefix of an identifier."""
    return value.split("_", 1)[0]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

#: A point in time, in seconds from the start of the recording.
Seconds = Annotated[float, Field(ge=0.0, le=24 * 60 * 60)]

#: A duration in seconds. Must be strictly positive.
Duration = Annotated[float, Field(gt=0.0, le=24 * 60 * 60)]

#: Tolerance used when comparing float timings. One millisecond is far finer
#: than any frame rate we render at, so anything below it is noise.
TIME_EPSILON: Final[float] = 1e-3


class VTVModel(BaseModel):
    """Base class for every contract model.

    ``extra="forbid"`` is the single most valuable setting in this file: it turns
    a hallucinated or misspelled field coming back from a language model into a
    loud validation error instead of a value that is silently ignored three
    stages later.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        ser_json_timedelta="float",
    )

    def canonical_json(self) -> str:
        """Deterministic JSON encoding, used for fingerprinting and caching."""
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self) -> str:
        """Stable content hash of this model. Equal models hash equally."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class RootDocument(VTVModel):
    """A model that is persisted on its own and therefore carries a version."""

    schema_version: str = Field(
        default=CONTRACTS_VERSION,
        description="Contract bundle version this document was written with.",
    )

    #: Overridden by subclasses; used by the JSON Schema exporter.
    document_name: ClassVar[str] = "document"


class TimeSpan(VTVModel):
    """A half-open interval ``[start, end)`` on the recording clock."""

    start: Seconds
    end: Seconds

    @model_validator(mode="after")
    def _check_order(self) -> TimeSpan:
        if self.end <= self.start:
            raise ValueError(
                f"time span must advance: start={self.start} end={self.end}"
            )
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: TimeSpan, *, epsilon: float = TIME_EPSILON) -> bool:
        return (self.start < other.end - epsilon) and (other.start < self.end - epsilon)

    def contains(self, other: TimeSpan, *, epsilon: float = TIME_EPSILON) -> bool:
        return (self.start <= other.start + epsilon) and (
            self.end >= other.end - epsilon
        )

    def intersection(self, other: TimeSpan) -> TimeSpan | None:
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if end - start <= TIME_EPSILON:
            return None
        return TimeSpan(start=start, end=end)

    @classmethod
    def of(cls, start: float, end: float) -> TimeSpan:
        return cls(start=start, end=end)


def utc_now() -> datetime:
    """Timezone-aware current time. Never use ``datetime.utcnow()``."""
    return datetime.now(UTC)


class Timestamped(VTVModel):
    """Mixin for models that record their own creation and update times."""

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Storage references
# ---------------------------------------------------------------------------

class RetentionClass(str, Enum):
    """How long the bytes behind an :class:`ObjectRef` are allowed to live.

    Retention is a property of the object itself so that lifecycle rules can be
    applied by the storage layer without it having to understand the domain
    (see ``docs/STORAGE_POLICY.md``).
    """

    #: Deleted shortly after the render completes. The default for raw voice.
    EPHEMERAL = "ephemeral"
    #: Kept for as long as the project is saved by the user.
    PROJECT = "project"
    #: Kept beyond project lifetime; only for things with a legal or billing
    #: reason to persist, such as license receipts.
    ARCHIVE = "archive"


class ObjectRef(VTVModel):
    """A pointer to a blob in object storage.

    Business logic never learns a filesystem path, a bucket URL, or a vendor
    name from this type. Turning an ``ObjectRef`` into readable bytes or a signed
    URL is the sole responsibility of the ``StorageProvider`` port.
    """

    bucket: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=1024)
    content_type: str = Field(
        min_length=3,
        max_length=128,
        description="IANA media type, e.g. 'audio/webm' or 'image/png'.",
    )
    size_bytes: int | None = Field(default=None, ge=0)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retention: RetentionClass = RetentionClass.EPHEMERAL

    @field_validator("key")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("object key must be relative and free of '..' segments")
        return value

    @property
    def uri(self) -> str:
        """A storage-agnostic identifier, safe to put in logs."""
        return f"obj://{self.bucket}/{self.key}"


# ---------------------------------------------------------------------------
# Money, cost and confidence
# ---------------------------------------------------------------------------

#: US dollars. Costs in this system are small, so a float is honest enough for
#: estimation and budgeting; anything that bills a customer must use the ledger
#: in a later stage rather than this field.
UsdAmount = Annotated[float, Field(ge=0.0)]

#: A model's own confidence in a decision, in ``[0, 1]``.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

#: Relative importance in ``[0, 1]``; drives how much budget a scene deserves.
Importance = Annotated[float, Field(ge=0.0, le=1.0)]


class Budget(VTVModel):
    """A hard ceiling attached to a unit of work.

    Every request that can spend money or time carries one of these. A provider
    that cannot satisfy the budget must decline rather than overspend, which is
    what makes the cost strategy in ``docs/AI_PROVIDER_POLICY.md`` enforceable
    instead of aspirational.
    """

    max_cost_usd: UsdAmount | None = None
    max_latency_seconds: Duration | None = None

    def allows(self, *, cost_usd: float = 0.0, latency_seconds: float = 0.0) -> bool:
        if self.max_cost_usd is not None and cost_usd > self.max_cost_usd:
            return False
        return not (
            self.max_latency_seconds is not None
            and latency_seconds > self.max_latency_seconds
        )


def stable_fingerprint(payload: Any) -> str:
    """SHA-256 of any JSON-serialisable payload, with deterministic key order."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


__all__ = [
    "CONTRACTS_VERSION",
    "TIME_EPSILON",
    "Budget",
    "Confidence",
    "Duration",
    "Id",
    "IdPrefix",
    "Importance",
    "ObjectRef",
    "RetentionClass",
    "RootDocument",
    "Seconds",
    "TimeSpan",
    "Timestamped",
    "UsdAmount",
    "VTVModel",
    "id_prefix_of",
    "new_id",
    "stable_fingerprint",
    "utc_now",
]
