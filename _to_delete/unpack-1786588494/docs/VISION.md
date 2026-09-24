# Vision

## What we are building

A system that listens to a person think out loud and turns what they *meant*
into something worth watching.

The pipeline, stated in one line:

```
speech → understanding → story → visual intelligence → video
```

The product a user sees is a microphone button. They press it, talk for a few
minutes, stop, and get back a coherent video whose visuals were chosen because of
what they actually said.

## What we are not building

We are not building a text-to-video wrapper. That distinction is not marketing —
it determines every engineering decision in this repository.

A text-to-video wrapper takes a sentence and asks a model for a picture. It has
no opinion about whether a picture is the right answer, no memory of the previous
shot, no way to be correct, and no economics: every second of output costs real
money regardless of whether the content deserved it.

What we are building asks a different question for every idea in the recording:

> Given this meaning, what is the best way to show it?

Sometimes the answer is a photograph. Sometimes it is a chart, because the
speaker made a quantitative claim and a chart is exactly true where a generated
image would be approximately wrong. Sometimes it is animated type, because the
sentence is a statement and the words are the point. Occasionally it is generated
footage, because the idea is abstract and nothing real depicts it.

Choosing well among those is a reasoning problem. That is the product.

## The moat

Not the models. Every foundation model we use is replaceable, and should be —
that is why they sit behind ports (`docs/AI_PROVIDER_POLICY.md`). Not the
renderer, not the storage, not the queue.

The defensible asset is **visual intelligence**: a growing, tested, proprietary
mapping from *kinds of meaning* to *kinds of visual*, plus the library of visual
forms we can render reliably and beautifully.

That asset compounds in three ways a model licence never does.

1. **The visual vocabulary grows.** Every primitive we add
   (`src/vtv/contracts/visual_language.py`) is a form of meaning we can now
   express better than a general-purpose generator can.
2. **The decisions get measured.** Every visual decision carries a rationale and
   an outcome. That is a labelled dataset of "what worked" that nobody else has,
   because nobody else recorded the decision separately from the pixels.
3. **The economics improve.** Every idea we learn to draw instead of generate
   moves a cost from dollars to fractions of a cent, permanently.

## The economic argument

A five-minute explainer contains roughly thirty visual ideas. Generated video at
current prices puts that video somewhere between fifteen and sixty dollars, at
several minutes of latency per shot, with no guarantee the result is factually
right.

The same video, planned well — most ideas drawn programmatically, some met with
licensed photography, a handful generated where generation genuinely adds
something — costs cents, renders in minutes, and is *more* accurate, because a
chart of the numbers the speaker said cannot contradict the numbers the speaker
said.

Better and cheaper at the same time is rare. It happens here because the naive
approach is spending money to approximate something we could have computed
exactly.

## Where this goes

The creator product is the entry point, not the company.

```
                  VISUAL INTELLIGENCE PLATFORM
                              |
        +--------------+------+------+--------------+
        |              |             |              |
     Creator       Education    Enterprise         API
```

The same engine that turns a creator's voice memo into a YouTube video turns a
lecture into a visual lesson, a support article into a training video, and — via
`POST /v1/visualize` — somebody else's product into a customer of ours.

We are not building that yet. We are making sure nothing we build now prevents
it: no assumption that there is one user, one video, one style, one language, or
one way in.

## What we hold ourselves to

- **We never fabricate what was said.** Visuals illustrate the narration; they do
  not add claims to it. A timeline shows the years the speaker mentioned.
- **We know where every pixel came from.** Provenance is a required field, not an
  aspiration (`docs/ASSET_PROVENANCE.md`).
- **We do not keep what we do not need.** Voice is intimate. Temporary is the
  default (`docs/STORAGE_POLICY.md`).
- **We degrade, we do not collapse.** One failed visual costs one shot, never the
  project (`docs/ERROR_MODEL.md`).

## Further reading

| Document | What it settles |
| --- | --- |
| [PRODUCT.md](PRODUCT.md) | The experience and what "done" means |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the system is put together |
| [SCENE_MODEL.md](SCENE_MODEL.md) | How speech becomes a story |
| [VISUAL_DIRECTOR.md](VISUAL_DIRECTOR.md) | How meaning becomes an image |
| [ROADMAP.md](ROADMAP.md) | The order we build it in |
