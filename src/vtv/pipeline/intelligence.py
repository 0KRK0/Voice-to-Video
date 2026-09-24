"""How much thinking a project may buy, and why longer videos buy less per minute.

## The economics this exists to fix

Model work was **linear in the number of shots**. A seven-hundred-line script
made forty model calls; a three-thousand-line one would make a hundred and
sixty. Every additional minute of video cost the same amount of intelligence as
the first minute did.

That is the wrong shape for this product, and the reason is not really about
money — it is about what the extra calls *buy*. The first minute of a video
needs careful direction because it establishes everything. The thirty-seventh
minute of a lecture about the same subject, four hundred lines in, is mostly
elaboration of decisions already taken. Asking a model afresh about each of its
lines pays full price for an answer that is usually "the same as the line
before".

## The shape instead

Intelligence per minute **falls** as a video grows:

    5 minutes    read every line          ~1.0 calls a minute
    30 minutes   read every second line   ~0.3 calls a minute
    60 minutes   read every third line    ~0.2 calls a minute
    4 hours      read every sixth line    ~0.07 calls a minute

A four-hour video costs about fourteen times less thinking per minute than a
five-minute one, and produces a video that is not fourteen times worse — because
what the skipped lines get instead is not nothing.

## What the skipped lines get

Two things, in order.

**Their own deterministic signal, which is often stronger than a reading.** A
line stating two quantities is a chart whether or not a model looked at it: the
Draughtsman found the numbers. Sampling never overrides evidence — see
`treatment.decide`, whose structural rules run for every line regardless.

**The treatment of the nearest read line in the same passage.** Adjacent
sentences in a narrative are about the same thing; that is what makes them
adjacent. A paragraph explaining one idea wants one visual approach, and
directing its first sentence directs the paragraph. This is the same assumption
chapter-level planning makes, expressed as sampling rather than as a second
planning vocabulary — which keeps one pipeline instead of two.

## What this is not

It is not a quality setting the user chooses. It is a statement about where
judgement is worth buying, and the user's control over quality is the budget and
the fidelity, which govern what is *made* rather than what is *thought about*.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Lines per minute of finished narration, measured.
#:
#: A real forty-minute render produced 736 visual units over 2 399 seconds —
#: about 18.4 a minute. Rounded down, because over-estimating the density makes
#: the budget think a video is longer than it is and read less of it.
LINES_PER_MINUTE = 18.0

#: (minutes, stride) — read one line in `stride`, in projects up to `minutes`
#: long. The last entry applies to everything longer.
#:
#: The curve is deliberately gentle at the short end. A five-minute video is
#: somebody's first impression of this product and the marginal call is cheap;
#: a four-hour lecture is a different economic object and the marginal call is
#: one of three thousand.
_LADDER: tuple[tuple[float, int], ...] = (
    (15.0, 1),
    (30.0, 2),
    (60.0, 3),
    (120.0, 4),
    (float("inf"), 6),
)


def stride_for(minutes: float) -> int:
    """Read one line in this many.

    `1` means read everything, which is what every project did before this
    existed and what short projects still do.
    """
    for limit, stride in _LADDER:
        if minutes <= limit:
            return stride
    return _LADDER[-1][1]


@dataclass(frozen=True)
class Budget:
    """What a project may spend on thinking, and what that works out to."""

    lines: int
    minutes: float
    stride: int
    #: Lines that will actually be read by a model.
    read: int
    #: Lines that will inherit from a neighbour or from their own structure.
    inferred: int

    @classmethod
    def of(cls, lines: int, *, minutes: float | None = None) -> Budget:
        """The budget for a script of this many lines.

        `minutes` is taken from the pacing plan where one exists; otherwise it
        is estimated from the line count, because the budget has to be decided
        *before* narration is synthesised and the true duration is not known
        until after.
        """
        estimated = minutes if minutes is not None else lines / LINES_PER_MINUTE
        stride = stride_for(estimated)
        read = (lines + stride - 1) // stride if lines else 0
        return cls(
            lines=lines,
            minutes=round(estimated, 2),
            stride=stride,
            read=read,
            inferred=lines - read,
        )

    def calls_for(self, per_call: int) -> int:
        """Model calls this needs, at a given batch size."""
        if per_call <= 0 or not self.read:
            return 0
        return (self.read + per_call - 1) // per_call

    def calls_per_minute(self, per_call: int) -> float:
        return self.calls_for(per_call) / self.minutes if self.minutes else 0.0

    @property
    def reads_everything(self) -> bool:
        return self.stride == 1

    def sample(self, lines: list[str]) -> list[int]:
        """Which line indices to send to a model.

        Always includes the first line: a video's opening shot is the one a
        viewer judges it by, and it is also the line every other line in its
        passage will inherit from.
        """
        return list(range(0, len(lines), self.stride))

    def headline(self) -> str:
        return (
            f"{self.lines} lines over {self.minutes:.0f} minutes: reading "
            f"{self.read} of them (1 in {self.stride}), inferring {self.inferred}."
        )

    def as_json(self) -> dict[str, object]:
        return {
            "lines": self.lines,
            "minutes": self.minutes,
            "stride": self.stride,
            "read": self.read,
            "inferred": self.inferred,
        }


def spread(
    decisions: dict[int, str], total: int, *, boundaries: frozenset[int] | None = None
) -> list[str]:
    """Give every line a treatment, from the ones that were read.

    ## The rule

    A line that was not read inherits from the nearest read line **at or before
    it**, never from one after. Reading forwards is how a person reads: the
    decision for a passage is taken at its first sentence and holds until
    something changes it.

    ## Why boundaries matter

    `boundaries` names indices where a new passage starts — a paragraph break, a
    new section. Inheritance never crosses one. Without that, the last line of a
    chapter about quantities would hand its chart to the first line of a chapter
    about people, which is exactly the failure that makes naive sampling look
    stupid rather than economical.

    A line with no read line before it in its own passage gets `""`, which means
    "no suggestion" — and the director's own rules decide, as they always do.
    """
    out: list[str] = []
    current = ""
    edges = boundaries or frozenset()
    for index in range(total):
        if index in edges:
            # A new passage inherits nothing from the last one.
            current = decisions.get(index, "")
        elif index in decisions:
            current = decisions[index]
        out.append(current)
    return out


__all__ = ["LINES_PER_MINUTE", "Budget", "spread", "stride_for"]
