"""How long a video may be, in one place.

## Why this module exists

"How long a project may be" was expressed in six different places, in four
different units, and they disagreed:

| where | said | in effect |
|---|---|---|
| `Script.source_text` | 400 000 characters | about seven hours |
| `Script.blocks` | 2 000 blocks | about twenty minutes |
| `SpeechParams.text` | 20 000 characters | about twenty minutes |
| `SpeechParams.segment_texts` | 2 000 segments | about twenty minutes |
| `Timeline.clips` | 800 clips | about an hour |
| `Track.clips` | 4 000 clips | several hours |

The smallest of those wins, silently, wherever a project happens to reach it
first. A user pasting a thirty-minute narration got `400 Bad Request` and a
toast reading "Something in that request did not look right" — which is true,
unhelpful, and mentions none of the six numbers above.

Worse, the 20 000-character speech cap was **obsolete**. It dated from when the
whole script went to the synthesiser as one request. Narration has been
synthesised one sentence at a time since the caption-drift fix; the vendor's
real limit applies per sentence and no sentence is anywhere near it. The cap was
enforcing a constraint that no longer existed, and it was the binding one.

## The rule

One number — `MAX_PROJECT_MINUTES` — and everything else derived from it. A cap
that is a division of a stated duration cannot silently disagree with another
cap that is a different division of the same duration, and when the ceiling
moves, it moves everywhere at once.

## Why four hours

Not a technical ceiling; a considered one. Narration, timeline and render all
work above it, and the honest limits at that length are money and patience:
about 2 900 visuals, and a render measured at roughly four times real time.
A user who needs longer should be splitting the work into parts they can review
separately, and the error says so.
"""

from __future__ import annotations

#: The longest project the *contracts* will hold, in minutes of finished video.
#:
#: Everything below is derived from it. This is the engineering ceiling — the
#: point past which the collection sizes, the batching and the render time stop
#: being things anyone has measured — and it is deliberately generous.
#:
#: It is **not** the number a deployment should enforce. See `ceiling_minutes`.
MAX_PROJECT_MINUTES = 240


#: Characters of narration per minute of speech.
#:
#: Deliberately conservative — measured English narration runs nearer 1 100 —
#: so the character cap permits *more* minutes than it claims rather than fewer.
#: A limit that under-delivers on its own promise is the failure being fixed.
SPEAKING_CHARS_PER_MINUTE = 900

#: Seconds each visual holds, at the fastest pacing profile. Used only to size
#: the clip caps; the pacing planner decides the real number.
MIN_VISUAL_SECONDS = 5.0

#: Longest script accepted in one request, in characters.
MAX_SCRIPT_CHARS = MAX_PROJECT_MINUTES * SPEAKING_CHARS_PER_MINUTE

#: Most addressable lines a script may have.
#:
#: Measured at about 47 characters a line on real narration — short declarative
#: sentences, one per line — with generous headroom, because a script of very
#: short lines is a legitimate style and not something to refuse.
MAX_SCRIPT_BLOCKS = 20_000

#: Most sentences one narration may carry. Same shape as the blocks: one
#: synthesis request per sentence, so this bounds requests, not request size.
MAX_SPEECH_SEGMENTS = MAX_SCRIPT_BLOCKS

#: Most characters one *sentence* may carry.
#:
#: This is the only cap here that is a real vendor limit rather than a product
#: decision: OpenAI's speech endpoint accepts 4 096 characters per request, and
#: since the caption-drift fix each sentence *is* a request. A sentence longer
#: than this is not a sentence, and refusing it names the line so the user can
#: split it — which is a fixable problem, unlike "your script is too long".
MAX_SEGMENT_CHARS = 4_000

#: Most visual clips a timeline may carry.
MAX_TIMELINE_CLIPS = int(MAX_PROJECT_MINUTES * 60 / MIN_VISUAL_SECONDS)


def ceiling_minutes(configured: float | None) -> float:
    """The shorter of the deployment's limit and the engineering one.

    ## Why the lesser, always

    Two different questions wear the same clothes here.

    *What can this system do?* is `MAX_PROJECT_MINUTES` — a fact about
    collection sizes, batch arithmetic and render time. Nobody should be able to
    raise it from a `.env` file, because setting it to 600 does not make the
    contracts hold 600 minutes; it makes them raise a validation error somewhere
    a user cannot act on.

    *What may this customer do?* is the configured number — a product decision.
    A free tier gets fifteen minutes, a paid one gets four hours, and an
    operator running this privately gets whatever they set.

    Taking the lesser means the deployment can only ever tighten. An operator
    who sets 600 gets 240 and the system stays within what it has been measured
    to do; an operator who sets 15 gets 15 and their free tier is enforced.
    Neither can produce a limit that lies.

    `None` means "no deployment limit", which is the engineering ceiling.
    """
    if configured is None or configured <= 0:
        return float(MAX_PROJECT_MINUTES)
    return min(float(configured), float(MAX_PROJECT_MINUTES))


def ceiling_chars(configured_minutes: float | None) -> int:
    """The same ceiling, in the characters an API actually checks."""
    return int(ceiling_minutes(configured_minutes) * SPEAKING_CHARS_PER_MINUTE)


def minutes_for(characters: int) -> float:
    """Roughly how many minutes of speech this many characters is.

    For error messages. A user told "216 000 characters" learns nothing; a user
    told "about four hours" knows immediately whether their script is close.
    """
    return characters / SPEAKING_CHARS_PER_MINUTE


def describe(characters: int) -> str:
    """A length in the units a person thinks in."""
    minutes = minutes_for(characters)
    if minutes < 90:
        return f"about {minutes:.0f} minutes"
    return f"about {minutes / 60:.1f} hours"


__all__ = [
    "MAX_PROJECT_MINUTES",
    "MAX_SCRIPT_BLOCKS",
    "MAX_SCRIPT_CHARS",
    "MAX_SEGMENT_CHARS",
    "MAX_SPEECH_SEGMENTS",
    "MAX_TIMELINE_CLIPS",
    "SPEAKING_CHARS_PER_MINUTE",
    "ceiling_chars",
    "ceiling_minutes",
    "describe",
    "minutes_for",
]
