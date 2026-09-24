# Grounding

## The claim we make, and the one we do not

**We guarantee fidelity to the source. We do not guarantee truth about the
world.**

That sentence is the product boundary, and blurring it would be the most
damaging thing this system could do. If a speaker says something false, the
video says it too. What the system will not do is *add* a number, a date or a
place that the source did not contain.

## Why charts specifically

A chart is the most authoritative-looking object a video can contain. A viewer
who would question a sentence accepts a bar chart without reading the axis. So a
chart built from numbers the system inferred, rounded up, extrapolated or
invented does more damage than no chart at all — it launders a guess into
evidence, with our name on it.

## The rule

Every number, date and named place drawn by a **programmatic** visual must trace
to one of exactly three places:

1. words in the transcript or the scene narration,
2. a `Quantity` the understanding layer extracted,
3. a cell in a table the source document contained.

Not "be plausible". Not "be consistent with". Trace.

## What is checked, and what deliberately is not

| Primitive | Checked | Why |
| --- | --- | --- |
| Chart | every value, plus title and axis labels | It reads as evidence |
| Timeline | every year | It asserts when something happened |
| Map | every marker | It asserts where |
| Typography | figures in the text, and the vocabulary | Words attributed to the speaker |
| Comparison | side titles and bullet figures | It asserts a contrast |
| Network | nothing numeric | It asserts structure, not measurement |
| Fetched photo | nothing | Makes no precise quantitative claim |
| Generated image | nothing | Same |

Holding an illustrative photograph to this standard would refuse every
non-diagram shot in the product. The gate is aimed precisely at the visuals that
speak with the authority of data.

## Tolerance

Two values are the same claim if they agree within 2% relative — because a
speaker says "about eight billion" while a table says 8,045,311,447, and those
are the same claim. Below a magnitude of 1.0 the comparison is absolute, because
0.02 and 0.021 are different claims, not rounding.

Wide enough to survive how people speak. Far too narrow to admit a different
number.

## Refusal is a degradation, not an error

A refused spec drops out of the fallback ladder and the next rung is used. If
*everything* is refused, the scene sets the narration itself as type — which, by
construction, cannot misstate the source.

The video still renders. `GROUNDING_REFUSED` appears in the event stream with
the specific unsupported claims, so the missing chart is explained rather than
mysterious. Silence would be worse than the chart.

## Spoken numbers

Transcripts contain "eight billion" and "nineteen forty seven", not
`8000000000` and `1947`. The extractor handles the forms a narrator actually
uses — scale words, compound tens, spoken years — rather than attempting general
English number parsing, which is a research project with a poor return.

## The bug this caught

The first implementation built its evidence from the extracted entities alone
and refused a chart whose axis was correctly labelled "percent", because that
word appeared in no entity. The evaluation corpus failed on
`numbers-and-units`.

The fix was to feed the narration in, which is the primary evidence and should
never have been omitted. `test_a_correctly_labelled_chart_survives_the_gate`
now guards it. A gate this strict earns its keep only if its false-positive rate
is measured, and the evaluation corpus is how it is measured.

## Provenance

Every `SourceBlock` records its page, slide or offset. A claim in a finished
video can therefore be traced to page 14 of the source. That is what an
enterprise customer asks for during procurement, and it is a property of the
ingestion design rather than a feature bolted on afterwards.
