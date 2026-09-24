"""Speech turned into timestamped text.

The transcript is the **source representation** of what the user actually said.
Everything downstream — semantics, scenes, captions, the timeline — points back
into it by segment id and time span. It is therefore append-only in spirit: later
stages produce *derived* documents rather than editing the transcript in place
(Rule 12). If a user corrects a word in the UI, that correction is stored as an
overlay on the transcript, not as a destructive edit to it.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from vtv.contracts.base import (
    TIME_EPSILON,
    Confidence,
    Id,
    IdPrefix,
    RootDocument,
    TimeSpan,
    Timestamped,
    VTVModel,
    new_id,
)
from vtv.contracts.errors import Status


class TranscriptWord(VTVModel):
    """A single word with its own timing.

    Word-level timing is optional at the provider level but is what makes
    karaoke-style caption highlighting possible later (Stage 11), so we model it
    from the start rather than retrofitting it.
    """

    text: str = Field(min_length=1, max_length=128)
    span: TimeSpan
    confidence: Confidence | None = None


class TranscriptSegment(VTVModel):
    """A contiguous chunk of speech, as emitted by the speech-to-text provider.

    A segment is a *transcription* unit, not a *meaning* unit and not a *scene*.
    Providers segment on pauses and their own heuristics; the semantic layer is
    responsible for regrouping this into ideas (Rule 7).
    """

    segment_id: Id = Field(default_factory=lambda: new_id(IdPrefix.SEGMENT))
    span: TimeSpan
    text: str = Field(min_length=1, max_length=8000)
    confidence: Confidence | None = None
    speaker: str | None = Field(
        default=None,
        max_length=64,
        description="Diarisation label if the provider supplies one.",
    )
    words: list[TranscriptWord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _words_inside_segment(self) -> TranscriptSegment:
        for word in self.words:
            if not self.span.contains(word.span):
                raise ValueError(
                    f"word {word.text!r} at {word.span.start:.3f}-{word.span.end:.3f} "
                    f"falls outside its segment {self.span.start:.3f}-{self.span.end:.3f}"
                )
        return self


class Transcript(RootDocument, Timestamped):
    """The full timestamped transcript of one recording."""

    document_name = "transcript"

    transcript_id: Id = Field(default_factory=lambda: new_id(IdPrefix.TRANSCRIPT))
    recording_id: Id
    #: The tenant this belongs to. Carried on every project-scoped document so
    #: that a service deep in the pipeline can name the storage namespace it is
    #: allowed to write to without an ambient lookup — `LocalStorageProvider`
    #: refuses any key outside `orgs/<organisation_id>/`.
    organisation_id: Id
    project_id: Id

    #: BCP-47 language actually detected by the provider.
    language: str = Field(default="en", max_length=16)
    segments: list[TranscriptSegment] = Field(default_factory=list)

    #: Which provider and model produced this, for reproducibility and for the
    #: evaluation system. Provider *identity* is data; provider *code* never
    #: appears in this package.
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)

    status: Status = Status.PENDING

    @model_validator(mode="after")
    def _segments_are_ordered_and_disjoint(self) -> Transcript:
        previous: TranscriptSegment | None = None
        for segment in self.segments:
            if previous is not None:
                if segment.span.start < previous.span.start - TIME_EPSILON:
                    raise ValueError("transcript segments must be in ascending order")
                if segment.span.start < previous.span.end - TIME_EPSILON:
                    raise ValueError(
                        "transcript segments must not overlap: "
                        f"{previous.span.end:.3f} > {segment.span.start:.3f}"
                    )
            previous = segment
        return self

    @property
    def text(self) -> str:
        """The full narration as one string. Derived, never stored."""
        return " ".join(segment.text.strip() for segment in self.segments).strip()

    @property
    def span(self) -> TimeSpan | None:
        if not self.segments:
            return None
        return TimeSpan(
            start=self.segments[0].span.start, end=self.segments[-1].span.end
        )

    def segment_by_id(self, segment_id: str) -> TranscriptSegment | None:
        return next((s for s in self.segments if s.segment_id == segment_id), None)

    def segments_in(self, span: TimeSpan) -> list[TranscriptSegment]:
        """Every segment that overlaps the given span."""
        return [s for s in self.segments if s.span.overlaps(span)]

    def text_in(self, span: TimeSpan) -> str:
        return " ".join(s.text.strip() for s in self.segments_in(span)).strip()


__all__ = ["Transcript", "TranscriptSegment", "TranscriptWord"]
