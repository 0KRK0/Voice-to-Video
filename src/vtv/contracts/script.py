"""The script — narration as a first-class, editable, versioned document.

Until now narration was a *derivative*: something the system produced from a
recording or a document, and which the user could not address. That is fine for
one of the two products and wrong for the other.

There are two ways into this platform and they are not two products:

* **Speak.** The user talks; the transcript becomes the script.
* **Script.** The user already has the words and pastes them.

In the second case the supplied text is **the narration source of truth**. The
system does not get to invent a better one. It may *propose* — grammar, clarity,
concision, tone — and every proposal is a `RevisionProposal` the user accepts or
rejects. `source_text` is never overwritten, so "show me what I actually wrote"
is answerable after any number of accepted revisions.

That constraint is the whole design. It is why:

* `Script.source_text` and `Script.current_text` are separate fields
* a revision is a document, not a mutation
* `ScriptBlock` carries `source_range` back into `source_text`
* every accepted revision bumps `version` and appends to the history

## Why blocks, and why they are not sentences

A `ScriptBlock` is the smallest unit a user can point at, edit, and see a
visual for. It is roughly a sentence, because that is the granularity at which
people say "no, not that one".

It is deliberately *not* the granularity of a scene. Three consecutive lines
about the same idea should share one visual — the eye does not want a cut per
sentence — so several blocks map to one `VisualUnit`. The block survives that
grouping: the user still edits line 2 on its own and still sees which visual it
belongs to.

## Timing

A block's `estimated_seconds` is a speaking-rate estimate, replaced by measured
timing once narration exists. Both are kept: the estimate is what lets the
product answer "how long will this be?" before spending a cent on synthesis,
and the measurement is what the timeline is actually built on.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Id,
    IdPrefix,
    RootDocument,
    Seconds,
    Timestamped,
    VTVModel,
    new_id,
    utc_now,
)
from vtv.contracts.scale import MAX_SCRIPT_BLOCKS, MAX_SCRIPT_CHARS

#: Words per minute for a measured, explanatory delivery. The same constant the
#: text-entry path already used; named here because it is now a *product*
#: number — it decides what duration the user is shown before anything is
#: synthesised, and being wrong about it is visible.
SPEAKING_WPM = 145.0

#: A pause after each block, as a speaker would take.
BLOCK_PAUSE_SECONDS = 0.35

#: How many past versions of the script are kept. A bound, because the document
#: is stored whole and an unbounded history would grow it without limit — but a
#: bound that *drops the oldest* rather than refusing the newest, since the
#: session long enough to reach it must not be the session where saving breaks.
MAX_HISTORY = 200


def estimate_seconds(text: str, *, words_per_minute: float = SPEAKING_WPM) -> Seconds:
    """How long this text takes to say aloud.

    An estimate, and labelled as one everywhere it surfaces. Real speech varies
    with language, voice and content; this is within about 15% for English
    exposition, which is enough to plan with and not enough to bill on.
    """
    words = max(1, len(text.split()))
    return round((words / words_per_minute) * 60.0 + BLOCK_PAUSE_SECONDS, 3)


class ScriptOrigin(str, Enum):
    """Where the words came from. Decides what the system may do to them."""

    #: Transcribed from the user's own voice. The recording is the authority;
    #: editing the text desynchronises it from the audio, which is a product
    #: decision the user has to make consciously (see `NarrationSource`).
    SPOKEN = "spoken"
    #: Typed or pasted by the user. Source of truth; never rewritten silently.
    AUTHORED = "authored"
    #: Derived from an ingested document. The system wrote it, so it may
    #: rewrite it — but only with the same visible-proposal flow, because by
    #: the time a user is looking at it they have started to own it.
    DERIVED = "derived"
    #: Produced by the story engine in automatic mode.
    GENERATED = "generated"


class BlockStatus(str, Enum):
    DRAFT = "draft"
    #: The user has looked at this block and is content with it.
    APPROVED = "approved"
    #: Excluded from narration and from the timeline without being deleted, so
    #: "put that line back" does not mean retyping it.
    MUTED = "muted"


class RevisionKind(str, Enum):
    """What the user asked for. Not a free-text prompt.

    An enum rather than a prompt string because these reach a language model,
    and a caller that can pass arbitrary instructions has an injection surface
    where a menu was intended.
    """

    FIX_GRAMMAR = "fix_grammar"
    IMPROVE_CLARITY = "improve_clarity"
    ENHANCE = "enhance"
    SHORTEN = "shorten"
    EXPAND = "expand"
    MAKE_FORMAL = "make_formal"
    MAKE_CINEMATIC = "make_cinematic"
    MAKE_EDUCATIONAL = "make_educational"
    MAKE_CONCISE = "make_concise"
    #: Translation into another language, preserving meaning.
    TRANSLATE = "translate"


class RevisionStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    #: The underlying text moved on before the user decided.
    SUPERSEDED = "superseded"


class ScriptBlock(VTVModel):
    """One addressable line of narration.

    The unit the user points at. Everything downstream — the visual, the clip,
    the caption, the seek target — is reachable from a block id, and that is
    what makes "I don't like that bit" a solvable request rather than a
    conversation.
    """

    block_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SCRIPT_BLOCK))
    #: Position in the narration, zero-based and contiguous. Enforced by the
    #: parent `Script`, not here, because a block does not know its siblings.
    order: int = Field(ge=0)

    text: str = Field(min_length=1, max_length=4000)

    #: Character offsets into `Script.source_text`, when this block came from
    #: parsing it. `None` for a block the user added afterwards. This is what
    #: lets an editor highlight the original passage behind an edited line.
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)

    #: Speaking-rate estimate. Present before anything is synthesised.
    estimated_seconds: Seconds = 0.0
    #: Measured from real narration audio. Authoritative once set.
    measured_start: Seconds | None = None
    measured_end: Seconds | None = None

    #: The visual this block is shown under. Several blocks may share one.
    visual_unit_id: Id | None = None

    status: BlockStatus = BlockStatus.DRAFT
    #: True when the block's text changed after timings were measured, so the
    #: timeline built from it is stale. Never silently kept — see
    #: `docs/EDITOR_INTERACTION_SPEC.md`.
    timing_invalidated: bool = False

    @property
    def is_narrated(self) -> bool:
        return self.status is not BlockStatus.MUTED and bool(self.text.strip())

    @property
    def duration_seconds(self) -> Seconds:
        """Measured if we have it, estimated if we do not."""
        if self.measured_start is not None and self.measured_end is not None:
            return round(self.measured_end - self.measured_start, 3)
        return self.estimated_seconds

    @model_validator(mode="after")
    def _source_range_is_ordered(self) -> ScriptBlock:
        if (
            self.source_start is not None
            and self.source_end is not None
            and self.source_end <= self.source_start
        ):
            raise ValueError("a source range must advance")
        if (self.source_start is None) != (self.source_end is None):
            raise ValueError("a source range needs both ends or neither")
        if (
            self.measured_start is not None
            and self.measured_end is not None
            and self.measured_end <= self.measured_start
        ):
            raise ValueError("measured timing must advance")
        if (self.measured_start is None) != (self.measured_end is None):
            raise ValueError("measured timing needs both ends or neither")
        return self


class TextChange(VTVModel):
    """One difference between the original and the proposal.

    Kept as structured data rather than a rendered diff so the editor can show
    it however it likes, and so a reviewer can see *what* changed without
    reading two paragraphs side by side.
    """

    block_id: Id
    original: str = Field(max_length=4000)
    proposed: str = Field(max_length=4000)
    #: Why, in the model's own words. Shown to the user. A change nobody can
    #: explain is a change nobody should accept.
    reason: str = Field(default="", max_length=400)

    @property
    def is_change(self) -> bool:
        return self.original.strip() != self.proposed.strip()


class RevisionProposal(RootDocument, Timestamped):
    """A proposed edit to the script, awaiting a decision.

    A document rather than a mutation, and that is the entire point. The audit's
    recurring lesson was that a guarantee which depends on a caller remembering
    is not a guarantee; "we will show the user before we change their words" is
    exactly that shape of promise. Making the proposal a persisted object with
    an explicit `ACCEPTED` transition is what makes it enforceable.
    """

    document_name = "revision_proposal"

    revision_id: Id = Field(default_factory=lambda: new_id(IdPrefix.REVISION))
    organisation_id: Id
    project_id: Id
    script_id: Id
    #: The script version this was computed against. If the script has moved on,
    #: the proposal is `SUPERSEDED` rather than applied to text it never saw.
    based_on_version: int = Field(ge=1)

    kind: RevisionKind
    #: Present only for `TRANSLATE`.
    target_language: str | None = Field(default=None, max_length=16)

    changes: list[TextChange] = Field(default_factory=list, max_length=2000)
    status: RevisionStatus = RevisionStatus.PROPOSED

    #: What accepting this would do to the running time. The number the user
    #: actually cares about, because it decides whether their video still fits.
    estimated_duration_before: Seconds = 0.0
    estimated_duration_after: Seconds = 0.0

    #: Which provider produced it, for cost attribution and for the audit trail.
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=96)

    @property
    def duration_delta_seconds(self) -> Seconds:
        return round(self.estimated_duration_after - self.estimated_duration_before, 3)

    @property
    def changed_block_ids(self) -> list[str]:
        return [change.block_id for change in self.changes if change.is_change]

    @property
    def is_noop(self) -> bool:
        """Nothing actually changed. Worth saying so rather than showing an
        empty diff and letting the user wonder what they missed."""
        return not self.changed_block_ids


class ScriptVersion(VTVModel):
    """A point the script can be restored to.

    Full text rather than a delta. Scripts are kilobytes; the storage cost of
    keeping every version outright is nothing next to the cost of a restore path
    that has to replay deltas correctly under every edge case.
    """

    version: int = Field(ge=1)
    text: str = Field(max_length=MAX_SCRIPT_CHARS)
    #: What produced this version. `None` for the initial one.
    revision_id: Id | None = None
    kind: RevisionKind | None = None
    at: object = Field(default_factory=utc_now)


class Script(RootDocument, Timestamped):
    """The narration, as an editable document with a history.

    `source_text` is what the user gave us and never changes. `current_text` is
    what the video will say. Both are kept because "what did I originally
    write?" is a question users ask after three rounds of AI enhancement, and a
    system that cannot answer it has quietly taken ownership of their words.
    """

    document_name = "script"

    script_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SCRIPT))
    organisation_id: Id
    project_id: Id

    origin: ScriptOrigin = ScriptOrigin.AUTHORED
    language: str = Field(default="en", max_length=16)

    #: The user's own words, exactly as supplied. **Frozen**, so it is immutable
    #: by construction rather than by everyone remembering — the whole value of
    #: this field is that there is always something to compare a revision
    #: against and always something to restore to, and a field that any code
    #: path can quietly overwrite provides neither.
    source_text: str = Field(default="", max_length=MAX_SCRIPT_CHARS, frozen=True)
    #: What the video will actually narrate.
    current_text: str = Field(default="", max_length=MAX_SCRIPT_CHARS)

    blocks: list[ScriptBlock] = Field(
        default_factory=list, max_length=MAX_SCRIPT_BLOCKS
    )

    version: int = Field(default=1, ge=1)
    #: Capped at `MAX_HISTORY`. Callers append through
    #: `Script.record_version`, which drops the oldest entry rather than
    #: raising — an editing session long enough to fill it must not be the
    #: session where saving starts failing.
    history: list[ScriptVersion] = Field(
        default_factory=list, max_length=MAX_HISTORY
    )

    #: Set when the script no longer matches the recording it came from — the
    #: user edited spoken narration. Forces an explicit choice between
    #: re-recording and synthesis rather than shipping audio that says something
    #: different from the captions.
    diverged_from_recording: bool = False

    # -- mutation ---------------------------------------------------------

    def record_version(
        self,
        *,
        revision_id: str | None = None,
        kind: RevisionKind | None = None,
    ) -> None:
        """Advance the version and remember the text, dropping the oldest.

        The one place the version and the history move, so they cannot move
        apart. Every caller used to inline `version += 1` followed by a list
        append — three sites, each of which would raise on the two-hundred-and-
        first edit **after** already having mutated the text and the version,
        leaving the caller holding a half-applied script and the user a 500 on
        an ordinary edit.
        """
        self.version += 1
        entry = ScriptVersion(
            version=self.version,
            text=self.current_text,
            revision_id=revision_id,
            kind=kind,
        )
        self.history = [*self.history, entry][-MAX_HISTORY:]

    # -- queries ----------------------------------------------------------

    def block(self, block_id: str) -> ScriptBlock | None:
        for item in self.blocks:
            if item.block_id == block_id:
                return item
        return None

    @property
    def narrated_blocks(self) -> list[ScriptBlock]:
        return [item for item in self.blocks if item.is_narrated]

    @property
    def estimated_duration_seconds(self) -> Seconds:
        return round(
            sum(item.estimated_seconds for item in self.narrated_blocks), 3
        )

    @property
    def measured_duration_seconds(self) -> Seconds | None:
        """`None` until every narrated block has real timing.

        Deliberately all-or-nothing: a partial measurement mixed with estimates
        is a number that looks authoritative and is not.
        """
        blocks = self.narrated_blocks
        if not blocks or any(item.measured_end is None for item in blocks):
            return None
        return round(max(item.measured_end or 0.0 for item in blocks), 3)

    @property
    def has_stale_timing(self) -> bool:
        return any(item.timing_invalidated for item in self.blocks)

    @property
    def word_count(self) -> int:
        return sum(len(item.text.split()) for item in self.narrated_blocks)

    # -- validation -------------------------------------------------------

    @model_validator(mode="after")
    def _blocks_are_ordered_and_unique(self) -> Script:
        orders = [item.order for item in self.blocks]
        if orders != sorted(orders):
            raise ValueError("script blocks must be in ascending order")
        if len(set(orders)) != len(orders):
            raise ValueError("script block order must be unique")
        identifiers = [item.block_id for item in self.blocks]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("script block ids must be unique")
        versions = [item.version for item in self.history]
        if versions != sorted(versions):
            raise ValueError("script history must be in ascending order")
        return self


__all__ = [
    "BLOCK_PAUSE_SECONDS",
    "SPEAKING_WPM",
    "BlockStatus",
    "RevisionKind",
    "RevisionProposal",
    "RevisionStatus",
    "Script",
    "ScriptBlock",
    "ScriptOrigin",
    "ScriptVersion",
    "TextChange",
    "estimate_seconds",
]
