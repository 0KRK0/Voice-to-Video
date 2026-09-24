# Scene model

## The rule everything else follows from

**One sentence is not one scene.**

This is Rule 7 in the engineering brief and it is the difference between a video
and a slideshow. A system that splits on punctuation produces a sequence of
unrelated images that happen to be adjacent. A system that groups by *idea*
produces something a viewer experiences as one argument.

## The worked example

The narration:

> "The transistor was invented in 1947 at Bell Labs. It was smaller, more
> efficient, and eventually replaced vacuum tubes."

The naive result — two sentences, two or three scenes:

```
SCENE 1   a picture of a transistor
SCENE 2   a picture of something small
SCENE 3   a picture of a vacuum tube
```

Nothing connects. Nothing accumulates. The viewer sees three stock images.

What this system produces instead:

```
1947 ──→ Bell Labs ──→ transistor ──→ vacuum tube becomes transistor
                                       (smaller, more efficient)
```

One through-line, developed. The full version is in
`src/vtv/examples/transistor.py`, where six spoken segments become six semantic
units become **five** scenes — and one of those scenes covers two whole sentences
because they make a single point.

## The four levels

```
speech            what was heard          Transcript.segments
   ↓
semantic units    what was meant          Understanding.units
   ↓
scenes            what is shown           SceneGraph.scenes
   ↓
shots             how it develops         Scene.shots
```

Each level regroups the one above it. The transformations are many-to-many in
both directions, and that is the point: a sentence can hold three ideas, three
sentences can hold one.

### Semantic units

The atom of meaning. Anchored to a time span, grounded in transcript segments,
carrying an **intent** — the thing the speaker is *doing*.

Intent turns out to predict visual form far better than topic does.
"X is defined as Y" wants type or a diagram whatever X is. "X grew from A to B"
wants a chart whatever X is. The full vocabulary is `SemanticIntent` in
`src/vtv/contracts/semantics.py`.

### Scenes

One coherent visual idea. A scene groups semantic units that:

- are adjacent in time,
- serve the same point in the argument,
- and would be *harmed* by being shown separately.

That last test is the useful one. "Smaller than a vacuum tube" and "a vacuum tube
was the size of a light bulb" are not two facts, they are one comparison stated
twice — splitting them destroys the comparison.

### Shots

A subdivision within a scene, for when one idea needs more than one image over
fifteen seconds. Shots let a long scene breathe without being torn into unrelated
scenes.

## Boundaries: when to split, when to merge

**Split when** the intent changes materially, a new entity takes the foreground,
the argument turns (a "but", a "so", a "which meant that"), or the scene would
otherwise exceed `MAX_SCENE_SECONDS` (25s) — though prefer shots to splitting.

**Merge when** the units share a subject and a purpose, one unit is an example or
elaboration of its neighbour, the resulting scene would fall below
`MIN_SCENE_SECONDS` (1.5s), or the intent is `FILLER` or `TRANSITION`, which
carry no visual content of their own.

The minimum is enforced in the type system: a `Scene` shorter than 1.5s cannot be
constructed. Sub-second scenes are the signature of punctuation-splitting, so the
contract refuses them and the Scene Engine has to merge instead.

## Continuity: coherence as data

The hardest thing to get right, and the easiest to lose, is that scene *n+1*
should feel like it follows scene *n*. Independent per-scene generation
guarantees it will not.

So continuity is stored, not hoped for. Each scene carries a `Continuity` block:

- `carried_entity_ids` — who or what is still on screen from before,
- `motifs` — recurring devices, e.g. "year card, lower left",
- `continues_previous_visual` — whether this shot should *evolve* from the last
  one rather than cut to something unrelated.

And the graph as a whole carries a `NarrativeArc`: the thesis, the beats, the
motifs meant to recur. Every scene can be checked against the thesis. A scene
that does not serve it is a signal that segmentation went wrong — and that is a
measurement, which means it can be improved (`docs/TESTING.md`, Stage 16).

## Purpose and visual goal

Two fields do most of the work of briefing the Visual Director.

**`ScenePurpose`** — the scene's role in the arc: `OPENING`, `CONTEXT`,
`DEFINITION`, `EVIDENCE`, `CONTRAST`, `TURNING_POINT`, `CONCLUSION` and others.
Purpose drives pacing and budget: an opening and a conclusion deserve production
value; a supporting detail does not.

**`VisualGoal`** — what the visual has to *accomplish*, stated in terms of
communication rather than medium: `SHOW_QUANTITY`, `SHOW_CHANGE_OVER_TIME`,
`SHOW_CONTRAST`, `SHOW_STRUCTURE`, `ILLUSTRATE_ABSTRACT`, and so on.

The Scene Engine says "show change over time". Whether that becomes a chart, a
timeline or a dissolve between two photographs is the Director's decision. This
split is what lets us improve segmentation and visual choice independently.

## Timing

Scene spans come from the narration and only from the narration. The Scene Engine
never invents a duration; it groups existing spans. Consequences:

- scenes are contiguous or have explicitly reported gaps (`SceneGraph.gaps()`),
- the video is exactly as long as the voice,
- and re-planning visuals can never desynchronise the audio, because visuals have
  no say in timing at all.

## Invariants the contract enforces

From `src/vtv/contracts/scene.py`:

- scene `index` matches its position in the list,
- scenes are in ascending time order and do not overlap,
- shots lie within their scene and do not overlap each other,
- no scene is shorter than 1.5 seconds,
- gaps in coverage are *reported*, so an uncovered stretch of narration is a
  decision rather than an accident.

Every one of these is tested in `tests/test_pipeline_contracts.py`.
