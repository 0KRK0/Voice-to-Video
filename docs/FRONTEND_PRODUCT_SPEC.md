# Frontend product specification

**Status: specification only. No frontend code exists and none should be written
from this document without a design pass.** This describes *what* the interface
must do and *what data it has to work with*; the visual design is a separate
decision and is deliberately not made here.

Written against the backend as it actually is on 2026-08-18: every endpoint named
below exists, every field named below is returned, and
`tests/test_product_scenarios.py` drives them through the real application. Where
something is not yet built, it says so.

---

## 1. What the product is

Two ways in, one project.

```
🎙  SPEAK  ──▶ transcript ─┐
📝  SCRIPT ────────────────┼──▶  Script  ──▶  VisualUnits  ──▶  Timeline  ──▶  Video
📄  DOCUMENT ──▶ knowledge ┘
```

The user is **directing an intelligent visual storyteller**, not moving
rectangles. The system produces the first draft of everything — script, visuals,
timeline — and the user's job is to disagree with the parts they disagree with.

That framing decides the whole interface. Every screen should make the next
disagreement cheap to express: point at a line, point at a visual, say what is
wrong, get a different answer. It should never require the user to *build*
anything they did not ask to build.

### The two modes are not two products

There is no "automatic app" and "editor app". A project created by speaking is
the same object as a project created by pasting, with the same script, the same
units and the same timeline. After the AI has built a story, the user switches to
directing it — the same panels, now populated.

Concretely: `POST /v1/projects/{id}/script` and the voice upload path both end at
a `Script` with `blocks`, and everything downstream is identical. The only
difference the interface must surface is `script.origin`, because a **spoken**
script that the user edits diverges from the recording and forces a choice (§7).

---

## 2. Screens

Four, and no more for v1.

| Screen | Purpose |
| --- | --- |
| **Start** | Choose how to begin: speak, paste, upload |
| **Studio** | The main screen. Script, preview, timeline |
| **Projects** | List, open, delete |
| **Settings** | Voice, language, style defaults, billing |

Everything interesting happens in **Studio**. The others are plumbing and should
be as boring as possible.

---

## 3. The Studio layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│  PROJECT NAME    MODE   STYLE   LANGUAGE   PACING   TARGET      [EXPORT] │
├────────────────────────────────────┬─────────────────────────────────────┤
│                                    │                                     │
│  SCRIPT                            │            PREVIEW                  │
│  ───────────────────────────────   │  ┌───────────────────────────────┐  │
│                                    │  │                               │  │
│  ▸ 01  Before computer science…    │  │                               │  │
│        [thumb]  photo · v2  🔒     │  │                               │  │
│                                    │  │                               │  │
│  ▸ 02  Mechanical computation…     │  └───────────────────────────────┘  │
│        [thumb]  diagram · v1       │   ◀◀   ▶   ▶▶      02:14 / 07:00    │
│                                    │                                     │
│  ▸ 03  The transistor transformed… │   SELECTED: Visual 02               │
│        [thumb]  ⚠ needs attention  │   Strategy   diagram                │
│                                    │   Grounded   ✓                      │
│  ▸ 04  Integrated circuits…        │   Cost       $0.004                 │
│        [thumb]  typography · v1    │   [Regenerate ▾]  [Lock]  [v1 v2 ▾] │
│                                    │                                     │
├────────────────────────────────────┴─────────────────────────────────────┤
│  TIMELINE                                                     [− ▭ +]    │
│                                                                          │
│  Narration  ████████████████████████████████████████████████             │
│  Visuals    ████│██████│█████│████████│██████│█████│███████              │
│  Captions   ▪▪▪ ▪▪ ▪▪▪▪ ▪▪ ▪▪▪ ▪▪▪▪ ▪▪ ▪▪▪▪▪ ▪▪ ▪▪▪ ▪▪▪▪                │
│  Music      ─────────────────────────────────────────────                │
│  SFX        ─────────────────────────────────────────────                │
│                                                                          │
│  0:00      1:00      2:00      3:00      4:00      5:00      6:00   7:00 │
└──────────────────────────────────────────────────────────────────────────┘
```

### Why this arrangement

* **Script on the left, reading order.** The script is the spine of the product;
  in both modes it is the thing the user thinks in. It is a *list*, not a text
  area, because every line is separately addressable.
* **Preview top-right.** Adjacent to the script so the eye moves a short
  distance between "what it says" and "what it shows".
* **Inspector under the preview, not in a separate panel.** What the selected
  visual is, why it was chosen, what it cost, and the two buttons that matter.
* **Timeline full width along the bottom.** It is a time axis; anything narrower
  than the window wastes it.

### What this deliberately is not

It is **not** a traditional NLE. There is no media bin, no effects browser, no
keyframe editor, no track header stack with a dozen controls. Users do not
assemble a video here; they correct one. Anything that implies "build it
yourself" is working against the product.

---

## 4. The script panel

The most important panel. A vertical list of **script lines**, each showing:

| Element | Source |
| --- | --- |
| Index | `block.order + 1` |
| Text | `block.text` |
| Visual thumbnail | `GET /v1/projects/{id}/scenes/{scene_id}/thumbnail.png` |
| Strategy + version | `unit.versions[selected].strategy`, `unit.versions[…].version` |
| Status chip | `unit.status` |
| Lock badge | `unit.locked` |
| Stale-timing warning | `block.timing_invalidated` |

### Grouping

Several lines can share one visual. The interface must show this as a **bracket
or gutter rule** spanning the grouped lines with the thumbnail beside the group,
not one thumbnail repeated per line. `unit.script_block_ids` gives the grouping.

Repeating the thumbnail would teach the user that each line has its own visual,
and then "regenerate line 2" would look like it should change only line 2's
picture — which it cannot, because there is one picture for all three.

### Editing

* Click a line → it becomes editable in place, and the preview seeks to it.
* Blur or ⌘↵ → `PATCH /v1/projects/{id}/script/blocks/{block_id}`.
* Select text → an inline toolbar with the revision actions (§5).
* The **source text is never lost**. A "show original" affordance on an edited
  line reads `script.source_text` with `block.source_start`/`source_end`.

### Empty, loading and error states

| State | What to show |
| --- | --- |
| No script yet | The Start options, inline. Not an empty list. |
| Script exists, no units | Lines with a "planning visuals" shimmer where the thumbnail goes |
| Unit `SEARCHING` / `GENERATING` | Indeterminate progress on the thumbnail; the line stays readable and editable |
| Unit `FAILED` | Thumbnail slot shows the failure and a Retry button. **The line stays editable.** |
| Unit `DEGRADED` | Thumbnail with a small warning chip; hovering gives `unit.detail` |
| `timing_invalidated` | Amber rule on the line, tooltip: "This line changed — the timing will update on the next plan" |

---

## 5. Script revision

The user selects text or a line, and picks an action:

```
Fix grammar   Improve clarity   Enhance   Shorten   Expand
Make formal   Make cinematic    Make educational    Translate…
```

These map exactly to `RevisionKind`. **Do not offer a free-text prompt.** The
backend takes an enum, and the reason is stated in `pipeline/revision.py`: a
caller-chosen instruction reaching a language model is an injection surface,
and this one would be exposed to the internet.

### The flow

1. `POST /v1/projects/{id}/script/revisions` with `{kind, block_ids}` → `202
   {job_id}`.
2. Poll or listen for completion. The result is a **proposal**, not a change.
3. Show a diff:

```
┌──────────────────────────────────────────────────────────┐
│  Fix grammar · 3 lines                    +4s  (7:00 → 7:04) │
├──────────────────────────────────────────────────────────┤
│  01  This are a sentence with a error.                   │
│      This is a sentence with an error.                   │
│      ⓘ subject–verb agreement, article                   │
│                                                          │
│  [ Reject ]                              [ Accept all ]  │
└──────────────────────────────────────────────────────────┘
```

4. `POST /v1/projects/{id}/script/revisions/{revision_id}` with `{accept: true}`.

### Non-negotiable

* **Nothing changes until Accept.** The backend enforces this; the interface
  must not *appear* to have applied it either.
* **Show the duration delta.** `estimated_duration_after -
  estimated_duration_before`. It is the consequence the user cannot compute and
  will care about most.
* **Per-line accept is desirable, not required for v1.** The API accepts the
  proposal whole. Partial accept needs a backend change and is not built.

---

## 6. Visual units

### Selection

Selecting a unit — by clicking its thumbnail, its script group, or its timeline
clip — populates the inspector and seeks the preview to `unit.span.start`.

### The inspector

```
SELECTED  Visual 02                              PLANNED · SEARCHING · READY
──────────────────────────────────────────────────────────────────────────
Strategy      diagram
Why           "the sentence describes a process with three stages"
Grounded      ✓  supported by the narration
Consistency   ✓  matches the Visual Bible
Cost          $0.004  (this version)   ·   $0.012  (all versions)
Covers        lines 3–5      02:14 → 02:31

[ Regenerate ▾ ]   [ Lock ]   [ Versions: v1  ●v2  v3 ]
```

`Why` is `version.rationale`. Showing it is a product decision, not decoration:
a user who disagrees with a choice can see the reasoning, which turns an
argument with a black box into a conversation.

### Regenerate

A split button. The default is "same idea"; the menu offers the rest of
`RegenerationIntent`:

```
Same idea              More cinematic
More realistic         More educational
Simpler                Use a real source
Use animation          Generate an image
Generate a video       Use typography
```

`POST /v1/projects/{id}/visual-units/{unit_id}/regenerate` with `{intent}` →
`202 {job_id}`.

While it runs, the unit is `REGENERATING` and **the previous version stays on
screen**. A failed regeneration must never blank the preview.

### Versions

Every regeneration adds a version and keeps the old ones. The version selector
shows all of them; choosing one is `PATCH …/visual-units/{unit_id}` with
`{version_id}` and is **free and instant** — no job, no cost. Say so in the
interface, because users who do not know it is free will not experiment.

A version with `usable: false` (grounding refused) is shown, greyed, with the
reason. The user paid for it; hiding it is worse than showing why it was
rejected.

### Lock

`PATCH …/visual-units/{unit_id}` with `{locked: true}`.

A locked unit:

* is skipped by project-wide regeneration
* refuses individual regeneration with a 403 and a message
* keeps its clip pinned on the timeline
* survives a re-plan, including a pacing change

The lock badge must be **visible at rest**, not on hover. It is a promise the
system makes, and a promise the user cannot see is one they will not rely on.

---

## 7. Voice, script and divergence

If the project came from a recording (`script.origin == "spoken"`) and the user
edits a line, the backend sets `script.diverged_from_recording`. The audio still
says the old words.

The interface **must** surface this and force a choice:

```
⚠  Your recording no longer matches your script.

   [ Re-record this section ]   [ Use a synthesised voice ]   [ Keep as is ]
```

"Keep as is" is allowed — sometimes captions and audio differing is fine — but it
has to be chosen. Shipping audio that says something other than the captions
without asking is the kind of thing that ends up in a screenshot.

**Not yet built:** re-recording a section, and voice selection for synthesis.
The flags and the decision point exist; the two actions do not.

---

## 8. Pacing and target duration

In the header:

```
PACING  [ Natural ▾ ]     TARGET  [ 7:00 ]     ACTUAL  6:58
```

`POST /v1/projects/{id}/pacing` with `{pacing, target_seconds}` returns a plan
with a `verdict`:

| Verdict | What to show |
| --- | --- |
| `on_target` | Nothing. It worked. |
| `filled` | A quiet note: "Added 1m 12s of visual time. Your narration is unchanged." |
| `underfilled` | A warning with `message`, and a suggestion to expand the script |
| `overrun` | A **blocking** notice: the script is longer than the target, with `message` and a "Shorten the script" action that opens the `shorten` revision |

The rule the interface must never violate: **the narration is never sped up or
slowed to hit a target.** If a designer proposes a "fit to duration" slider that
changes playback rate, the answer is no — and the reason is in
`pipeline/pacing.py`.

---

## 9. Preview

`GET /v1/projects/{id}/preview?at={seconds}` returns what is on screen at a
moment: the clip, the unit, the script lines. It is a **metadata** endpoint — it
does not render a frame.

For v1, the preview surface is:

* the rendered video, once one exists (`GET /v1/projects/{id}/video`)
* before that, the selected unit's thumbnail, held for its span

**Not yet built:** live preview of an unrendered edit. The user's timeline
changes are not visible in the preview until a render runs. This is the largest
gap between this specification and a finished editor, and it is named here rather
than discovered.

---

## 10. Export

```
[ EXPORT ▾ ]  →  Render video   ·   Download captions   ·   Download storyboard
```

`POST /v1/projects/{id}/render` with `{scope: "full_project"}` → `202 {job_id}`.

Before rendering, show anything the project would ship with:

* units that are `FAILED` or `DEGRADED`
* `outcome == "degraded"` on a previous render
* `narration.has_speech == false` — the video will be silent
* grounding refusals

The system reports `degraded` rather than `ready` for these, and the export
dialogue must repeat that. A user who exports a silent video without being told
it is silent will believe the product is broken, and they will be right.

---

## 11. Data each panel needs

| Panel | Endpoint | Refresh |
| --- | --- | --- |
| Script | `GET /v1/projects/{id}/script` | after any edit, revision accept, or plan |
| Units | `GET /v1/projects/{id}/visual-units` | after plan, regenerate, lock, version select |
| Timeline | `GET /v1/projects/{id}/timeline` | after plan, pacing, any timeline edit |
| Preview meta | `GET /v1/projects/{id}/preview?at=` | on seek |
| Progress | `GET /v1/projects/{id}/events` (SSE) | continuous while a job runs |
| Project | `GET /v1/projects/{id}` | on open, and after a render |

`GET /v1/projects/{id}/timeline` includes a materialised `links` array mapping
every visual clip to its unit and its script lines. **Use it.** Deriving the
mapping client-side means two implementations of the same relationship, and they
will disagree.

---

## 12. Progress and jobs

Long operations return `202 {job_id}`. Progress arrives over the existing
server-sent event stream at `GET /v1/projects/{id}/events`.

Relevant event names:

```
script.created            visual.units.planned
script.edited             visual.unit.regenerated
script.revision.proposed  visual.unit.locked
script.revision.accepted  visual.unit.failed
script.revision.rejected  grounding.refused
pacing.planned            timeline.edited
render.started            render.progress          render.completed
```

Every event carries `project_id`; job-scoped events carry the job in `data`.

---

## 13. Accessibility

Not optional and not a later pass.

* **Every action reachable by keyboard.** The shortcut table is in
  `EDITOR_INTERACTION_SPEC.md`.
* **The timeline is not the only way to do anything.** Everything a user can do
  by dragging must also be doable from the script panel or the inspector.
  Drag-only functionality excludes people who cannot drag.
* **Captions are always exported**, regardless of the burn-in setting. The
  backend already guarantees this; do not add a UI toggle that appears to.
* **Colour is never the only signal.** Locked, degraded, failed and stale all
  need an icon or text as well as a colour.
* **Announce status changes** to a live region: a unit going from
  `GENERATING` to `READY` is information a screen-reader user needs.
* **Respect `prefers-reduced-motion`** in the timeline and the preview.

---

## 14. Mobile

v1 is **desktop-first and desktop-only for editing**. A timeline with
frame-level trimming on a 390px screen is a worse product than no timeline.

What mobile should support:

* start a project by speaking
* watch progress
* review the script and the visuals as a scrolling list
* approve, lock and regenerate individual visuals
* export and share

What it should not attempt: the timeline, trimming, splitting, drag.

---

## 15. What does not exist yet

Named so the frontend does not plan around it:

| Gap | Consequence for the interface |
| --- | --- |
| Live preview of unrendered edits | The preview lags the timeline until a render |
| Scoped rendering actually being cheaper | A scene render costs a full render today; do not promise a saving |
| Per-line accept of a revision | Accept is all-or-nothing |
| Voice selection and re-recording | The divergence prompt has no "re-record" action to call |
| Music and SFX libraries | The tracks exist and are editable; there is nothing to put on them |
| Real image and video generation | With no provider credential, every regeneration returns typography |

---

## 16. Sources

* `src/vtv/api/product.py` — every endpoint and every field in this document
* `src/vtv/contracts/script.py`, `visual_unit.py`, `tracks.py`, `pacing.py`
* `tests/test_product_scenarios.py` — the promises, driven through the real app
* `docs/EDITOR_INTERACTION_SPEC.md` — per-interaction behaviour
* `docs/TIMELINE_UI_SPEC.md` — the timeline in detail
* `docs/DESIGN_HANDOFF.md` — what a designer needs before drawing anything
