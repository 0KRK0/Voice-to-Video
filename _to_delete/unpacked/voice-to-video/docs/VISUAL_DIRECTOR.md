# The Visual Director

The central intelligence of the system. For each scene it answers one question:

> What is the best way to visually communicate this idea?

It returns a **decision**, not a picture. It never fetches, never generates,
never renders. That separation is what makes the decision testable on its own —
we can evaluate whether "chart" was right for a sentence about population growth
without spending a cent.

Contract: `src/vtv/contracts/visual_plan.py`.

## The strategy vocabulary

The engineering brief lists eleven strategies:

```
existing_asset, licensed_media, programmatic_animation, diagram, chart,
timeline, map, typography, generated_image, generated_video, composite
```

Six of those — diagram, chart, timeline, map, typography, and most of what
"animation" means — are not different ways of *obtaining* a visual. They are
different things our own animation engine *draws*. Modelling them as sibling
strategies would force the Director to re-learn the same "draw it ourselves"
decision six separate times, and would make "should we draw or generate?" —
the question that determines our unit economics — invisible in the data.

So the vocabulary is split along its real seam:

**`VisualStrategy`** — how we obtain the visual:

| Strategy | Meaning |
| --- | --- |
| `existing_asset` | Something we already hold: library, user upload, or an asset fetched earlier in this project |
| `programmatic` | Drawn by our animation engine from a spec |
| `licensed_media` | Retrieved from an open-licence or contracted source |
| `generated_image` | An image model, usually plus camera motion |
| `generated_video` | A video model. Slowest, priciest, rarest |
| `composite` | Two to four of the above, layered |

**`VisualPrimitive`** — what we draw, when the strategy is `programmatic`:
`typography`, `chart`, `timeline`, `network`, `comparison`, `map` today; the
union grows in Stage 8.

The original eleven map onto that pair without loss:

| Brief's strategy | Here |
| --- | --- |
| `existing_asset` | `EXISTING_ASSET` |
| `licensed_media` | `LICENSED_MEDIA` |
| `programmatic_animation` | `PROGRAMMATIC` + any primitive |
| `diagram` | `PROGRAMMATIC` + `NETWORK` |
| `chart` | `PROGRAMMATIC` + `CHART` |
| `timeline` | `PROGRAMMATIC` + `TIMELINE` |
| `map` | `PROGRAMMATIC` + `MAP` |
| `typography` | `PROGRAMMATIC` + `TYPOGRAPHY` |
| `generated_image` | `GENERATED_IMAGE` |
| `generated_video` | `GENERATED_VIDEO` |
| `composite` | `COMPOSITE` |

## What the Director considers

```
semantic suitability   does this form actually communicate this meaning?
visual clarity         will a viewer read it in the time available?
accuracy               can this be wrong? a chart of stated numbers cannot
cost                   what does this shot cost, against its importance?
latency                will the user still be waiting?
licence                may we use the result commercially?
style consistency      does it belong to the same film as its neighbours?
reliability            how often does this approach actually work?
```

## Default preferences

In order, all else equal — and all else is frequently not equal:

1. **Something we already have.** Free, instant, already on-brand.
2. **Draw it.** Near-free, deterministic, resolution-independent, factually
   exact, always in the project's style.
3. **Licensed media.** Real photography and footage, cheap, and truthful about
   things that actually happened.
4. **Generate an image.** When nothing real depicts the idea.
5. **Generate video.** Only when *motion itself* carries the meaning.

The strongest version of this preference: **generation is what we reach for when
we cannot be exact.** If the speaker said "from one billion to eight billion",
a chart is not the cheap option, it is the *correct* one. A generated image of a
crowd is an expensive approximation of a fact we already possess.

## Worked decisions

| The speaker says | Chosen | Why |
| --- | --- | --- |
| "Population grew from 1 billion to 8 billion" | `programmatic` + `chart` | The numbers are given. A chart is exact; generation would approximate. |
| "The transistor was invented in 1947 at Bell Labs" | `licensed_media` | A real historical object. Photography is truer than an invented likeness. |
| "A vacuum tube was the size of a light bulb; a transistor, a grain of rice" | `programmatic` + `comparison` | The point *is* the size relationship. Drawn to true relative scale, it proves itself. |
| "The internet is a network of networks" | `programmatic` + `network` | The structure is the meaning. |
| "This changed everything" | `programmatic` + `typography` | A statement. The words are the content. |
| "A futuristic city floating above Mars" | `generated_image` | Nothing real depicts it. Generation earns its cost. |
| "The wave crashes and pulls back" | `generated_video` | Motion carries the meaning; a still cannot. |

## The output

```json
{
  "scene_id": "scn_...",
  "primary": {
    "strategy": "programmatic",
    "requirements": {
      "strategy": "programmatic",
      "spec": {
        "primitive": "timeline",
        "title": "Twenty years",
        "events": [
          { "label": "Transistor invented", "when": "1947", "sort_value": 1947 },
          { "label": "Vacuum tubes displaced", "when": "within twenty years", "sort_value": 1967 }
        ]
      }
    },
    "rationale": "A span of time with two endpoints, both stated by the speaker.",
    "estimate": { "usd": 0.0, "latency_seconds": 0.3 },
    "confidence": 0.9
  },
  "fallbacks": [ { "strategy": "programmatic", "...": "typography" } ]
}
```

Three things to notice.

**The rationale is mandatory.** A decision without a reason cannot be reviewed by
a human, explained to a user, or graded by an evaluator. It is a field, not a
comment.

**The estimate is supplied, not guessed.** Cost and latency come from provider
capability data (`ProviderCapabilities`), so the economic trade-off is a
computation over real numbers rather than a language model's intuition.

**The plan is provider-independent.** No vendor, model or API appears anywhere.
Selection happens later, in the generation router.

## Fallback ladders

`fallbacks` is not decoration. It is the ordered descent the system takes when
the first choice fails, costs too much or takes too long.

```
generated_image      first choice
      ↓  provider failed / over budget / too slow
licensed_media       a real photograph of the same idea
      ↓  nothing suitably licensed exists
programmatic         typography — cannot fail
```

Rules the contract enforces:

- each rung must offer a genuinely different approach, not a retry of the same
  expensive call;
- the ladder should terminate in something that cannot fail (in practice,
  typography);
- budgets are checked against `worst_case_estimate`, the sum of the whole ladder,
  not the primary alone — otherwise a project can quietly cost several times its
  ceiling.

`tests/test_example_transistor.py` asserts that every expensive shot in the
worked example has a way down, and that the last rung is programmatic.

## Specifications are data, never code

A language model may **choose** a primitive and **parameterise** it. It may never
emit React, SVG, CSS, shader source or anything else we would execute or inject.

This is a security boundary — nothing model-authored is ever run — and a quality
boundary: every visual the system can produce is one we deliberately designed,
reviewed and tested. It is also why the visual vocabulary is an asset. Each
primitive we add is a form of meaning we can now express better than a
general-purpose generator can.

`tests/test_visual_plan.py` asserts that no animation spec exposes a field named
`code`, `html`, `svg`, `script`, `javascript`, `css` or `template`.

## Style consistency

Every directive is produced against the project's `StyleProfile`, and generation
prompts carry `style.as_prompt_fragment()`. Generated images may also reference a
previously generated asset (`style_reference_asset_id`) so that separately
generated shots look like they belong to the same film rather than to five
different ones.

## Accuracy

A visual must not assert something the speaker did not.

- Charts use the numbers that were said. If none were, we do not invent axes.
- Timelines use the dates that were said, in the speaker's own phrasing
  (`TimelineEvent.when` is stored verbatim, so we never manufacture a precision
  they did not claim).
- Generated imagery depicting real people, places or events sets
  `depicts_reality`, which propagates to the timeline as `illustrative_label`
  and puts a label on screen.

The general principle: when in doubt, prefer the form that *cannot* be wrong.
That is usually the cheap one, which is a pleasant coincidence and not one we
should waste.
