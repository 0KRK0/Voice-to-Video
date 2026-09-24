# Timeline UI specification

**Status:** specification for a frontend that does not exist yet. Every field,
rule and refusal quoted here is implemented and tested in the backend today.

`EDITOR_INTERACTION_SPEC.md` says what happens when a user does something to the
timeline. This document says what the timeline *is*: its data, its layout, its
rendering rules, its performance budget, and the invariants a correct
implementation cannot violate.

---

## Table of contents

1. [What this timeline is for](#1-what-this-timeline-is-for)
2. [The data model, as the client sees it](#2-the-data-model-as-the-client-sees-it)
3. [Tracks](#3-tracks)
4. [Layout](#4-layout)
5. [The ruler and the time mapping](#5-the-ruler-and-the-time-mapping)
6. [Clip rendering](#6-clip-rendering)
7. [Gaps, overlaps and adjacency](#7-gaps-overlaps-and-adjacency)
8. [Transitions](#8-transitions)
9. [The playhead](#9-the-playhead)
10. [Selection and multi-selection](#10-selection-and-multi-selection)
11. [Snapping](#11-snapping)
12. [Zoom and virtualisation](#12-zoom-and-virtualisation)
13. [Live state: generating, failed, degraded, stale](#13-live-state-generating-failed-degraded-stale)
14. [The render-scope overlay](#14-the-render-scope-overlay)
15. [Keyboard operation of the timeline](#15-keyboard-operation-of-the-timeline)
16. [Performance budget](#16-performance-budget)
17. [Invariants](#17-invariants)
18. [What the timeline does not do](#18-what-the-timeline-does-not-do)

---

## 1. What this timeline is for

It is **not** a general non-linear editor. It is a view of a plan that a
director produced from a script, with the specific edits a user actually wants
to make afterwards: hold this shot longer, swap that picture, move the music,
cut the gap.

Three consequences that should shape every design decision:

* **The script is upstream.** Narration and captions are generated from it and
  cannot be edited here. A timeline that lets you drag a caption is a timeline
  that desynchronises from the document that produced it.
* **Most projects are small.** A five-minute explainer is roughly 40–60 visual
  clips. The design should be excellent at that scale, not compromised for a
  hypothetical four-thousand-clip feature film (the contract caps a track at
  4000 clips; that is a safety limit, not a target).
* **Every clip means something.** A clip is not an anonymous rectangle — it
  carries a visual unit, which carries script lines. The timeline is the third
  view of one object, and it must always agree with the other two.

---

## 2. The data model, as the client sees it

One `GET /v1/projects/{id}/timeline` returns everything the timeline needs:

```json
{
  "edit_timeline_id": "etl_…",
  "version": 7,
  "duration": 184.2,
  "target_seconds": 180.0,
  "tracks": [
    {
      "track_id": "trk_…",
      "kind": "visual",
      "name": "",
      "muted": false,
      "locked": false,
      "exclusive": true,
      "derived": false,
      "gaps": [{"start": 92.4, "end": 96.6}],
      "clips": [ … ]
    }
  ],
  "links": [
    {"visual_unit_id": "vun_…", "clip_id": "clp_…",
     "start": 12.4, "end": 18.1, "script_block_ids": ["blk_…"]}
  ],
  "script_version": 4
}
```

Per clip:

```json
{
  "clip_id": "clp_…",
  "track_id": "trk_…",
  "visual_unit_id": "vun_…",
  "start": 12.4, "end": 18.1, "duration": 5.7,
  "source_kind": "object",
  "locked": false,
  "label": "",
  "transition_in": "cut",
  "transition_out": "fade"
}
```

Two things to note.

**`links` is the join table, materialised server-side.** Build `block_id → link`
and `clip_id → link` indexes from it and use them for every cross-panel
selection. Do not infer the relationship from time spans; that is a second
implementation of a relationship the backend owns, and the two will disagree the
first time a clip is split or moved.

**`version` is the concurrency token.** Send it as `expected_version` on every
mutation. See `EDITOR_INTERACTION_SPEC.md` §10.

The clip view deliberately omits `object`, `spec`, `asset_id`, `generation_id`,
`z_index`, `gain` and transition durations. The timeline does not need bytes to
draw a rectangle. Thumbnails and per-version detail come from
`GET /v1/projects/{id}/visual-units`, keyed by `visual_unit_id`.

---

## 3. Tracks

Ten kinds exist. Six are used today; four are reserved so that adding them later
is a value rather than a migration.

| Kind | Exclusive | Derived | Editable | Present by default |
| --- | --- | --- | --- | --- |
| `narration` | yes | **yes** | no | yes |
| `visual` | yes | no | yes | yes |
| `caption` | yes | **yes** | no | yes |
| `music` | no | no | yes | on demand |
| `sfx` | no | no | yes | on demand |
| `overlay` | no | no | yes | on demand |
| `chapter` | yes | no | yes | reserved |
| `broll` | no | no | yes | reserved |
| `graphics` | no | no | yes | reserved |
| `secondary_voice` | no | no | yes | reserved |

**Exclusive** means two clips may not occupy the same instant. Visual is
obvious: two pictures at once is a composite, not a timeline. Narration too —
two voices talking over each other is a mixing decision, and a user who wants
that wants `secondary_voice`, which permits it.

**Derived** means the system owns it. `narration` and `caption` are generated
from the script; a hand edit here would be discarded at the next re-plan, so the
backend refuses it rather than accepting a change it will silently throw away.

### Track order, top to bottom

```
overlay        (topmost visually, so topmost in the stack)
graphics
visual         ← the primary editing lane, given the most height
broll
caption
narration
secondary_voice
sfx
music          (bottom: the bed under everything)
chapter        (a thin strip pinned above the ruler, not in the stack)
```

Order follows compositing order where that is meaningful, so that "above" on
screen means "in front of" in the output.

### Track header

Fixed-width (roughly 160px), pinned left, never scrolls horizontally. Contains:

* the track name (or the kind, when unnamed)
* mute (audio tracks) — client-side and server-persisted via the track's `muted`
* lock — a locked track refuses every mutation
* a **derived** marker on `narration` and `caption`: a small "from script" chip
  that is also the affordance explaining why the lane cannot be dragged
* height control on `visual` only

Derived tracks are rendered with a distinct fill (a low-contrast hatch) and no
drag handles. That is a courtesy; the refusal from the backend is the guarantee.

---

## 4. Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ⏮ ⏯ ⏭   00:01:24 / 00:03:04      [fit] [−][+]    scope: ▓ 0:42–0:58     │  toolbar 40px
├────────────┬─────────────────────────────────────────────────────────────┤
│            │ 0:00      0:30      1:00      1:30      2:00      2:30      │  ruler 28px
├────────────┼─────────────────────────────────────────────────────────────┤
│ Overlay    │                    ▭▭▭▭                                     │  32px
│ Visual   ⬍ │ ▮▮▮▮▮▮ ▮▮▮▮▮▮▮ ▮▮▮🔒▮ ▮▮▮▮   ░░gap░░ ▮▮▮▮▮▮ ▮▮▮⚠▮▮        │  72px
│ Caption  📄│ ▬▬▬ ▬▬▬▬ ▬▬ ▬▬▬▬▬ ▬▬▬ ▬▬▬▬ ▬▬ ▬▬▬▬▬▬ ▬▬▬ ▬▬▬▬ ▬▬▬        │  24px
│ Narration📄│ ████████████████████████████████████████████████            │  40px
│ Music    🔇│ ▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭                │  32px
└────────────┴─────────────────────────────────────────────────────────────┘
                        ▲ playhead spans all tracks
```

* The whole timeline is one horizontal scroll context; track headers are
  sticky-left, the ruler is sticky-top.
* Default total height ~280px, resizable by dragging the divider above the
  toolbar. The visual track gets the extra height when the user grows the panel;
  a caption lane does not become more useful at 60px.
* Minimum useful height is roughly 160px (ruler + visual + narration + one
  more). Below that, collapse to visual + ruler only rather than showing five
  8px slivers.

---

## 5. The ruler and the time mapping

One function owns time↔pixel conversion, both directions, and everything else
calls it:

```
x(t) = (t - viewportStart) * pixelsPerSecond
t(x) = viewportStart + x / pixelsPerSecond
```

No component may keep its own copy. Two conversion implementations is how a
clip ends up drawn one pixel from where the playhead says it is.

**Quantisation.** The backend rounds every time to milliseconds
(`PRECISION = 3`). The client should round the same way before sending, and
must re-render from the response rather than from its own arithmetic — the
authoritative value is the one that came back.

### Tick density

Adapt to `pixelsPerSecond`, targeting a labelled tick roughly every 100px:

| pps | Major tick | Minor | Label |
| --- | --- | --- | --- |
| < 2 | 60s | 10s | `m:ss` |
| 2–10 | 30s | 5s | `m:ss` |
| 10–40 | 10s | 1s | `m:ss` |
| 40–150 | 1s | 0.1s | `m:ss` |
| > 150 | 0.5s | frame | `m:ss.mmm` |

The project's `target_seconds`, when set, is drawn on the ruler as a marker with
a label. A user who asked for three minutes should be able to see three minutes.

---

## 6. Clip rendering

A clip is a rounded rectangle spanning `x(start)` to `x(end)`, inset 1px so
adjacent clips are visually separable without appearing to overlap.

### Minimum width

`MIN_CLIP_SECONDS` is 0.04s — at 20 pixels/second that is under a pixel. Clips
narrower than **6px** render as a fixed 6px marker so they remain visible and
clickable. Below 3px of available space, collapse consecutive tiny clips into a
single "N clips" marker that zooms in on click. Never render a zero-width
clickable element.

### Contents, by available width

| Width | Shows |
| --- | --- |
| < 24px | fill and status colour only |
| 24–60px | plus the status icon |
| 60–120px | plus the unit index ("4") |
| > 120px | plus the label or source description ("Stock footage"), truncated with an ellipsis |

Thumbnails on the visual track are desirable but optional, and come from the
unit payload. They must never be the only way to tell two clips apart — a
thumbnail can be a plain gradient.

### Fill by source kind

| `source_kind` | Treatment |
| --- | --- |
| `object` | solid fill, thumbnail if available |
| `programmatic` | solid fill with a subtle motion glyph — there are no bytes until render time, so there is nothing to thumbnail |
| `text` | the text itself, truncated |
| `empty` | outlined, not filled: this is a deliberate hold on black, and it must be distinguishable from a gap (§7) |

### Locked clips

A padlock badge, drawn **at rest**, plus a heavier border. Not on hover. A user
scanning a forty-clip project must see what is protected without touching
anything. Locked clips also lose their drag and trim handles.

### Audio clips

`narration`, `music`, `sfx`, `secondary_voice` render a waveform when one is
available and a flat bar when it is not. A muted track renders at reduced
opacity with the mute icon in the header — never by hiding its clips, which
would look like data loss.

---

## 7. Gaps, overlaps and adjacency

**Adjacency is not overlap.** Spans are half-open: a clip ending at 4.0 and one
starting at 4.0 are adjacent, not conflicting. The client's hit-testing and
snapping must use the same rule, or every contiguous timeline will look like a
conflict.

**Gaps are legal.** The response gives `gaps` per track. On an exclusive track
render them explicitly — a hatched region with its duration — rather than as
empty background. A gap on the visual track is black screen, which is sometimes
what the user wants and sometimes a bug; the interface's job is to make it
visible, not to refuse it.

An `empty` clip and a gap look different and mean different things: the first is
a deliberate hold the user placed, the second is uncovered time. Do not render
them identically.

**Overlap on non-exclusive tracks is allowed** and is drawn by stacking, ordered
by `z_index`. The clip view does not currently expose `z_index`; until it does,
draw in array order and do not offer a reorder control that has nothing behind
it.

---

## 8. Transitions

`transition_in` and `transition_out` are one of `cut`, `fade`, `dissolve`,
`wipe`, `push`. Their durations are capped at **half the clip's duration** —
beyond that the transition consumes the whole shot, and the backend refuses.

Rendering: a triangular wedge at the affected edge, sized to the transition
duration, **with the duration written numerically** on hover and in the
inspector. A transition drawn as a gradient with no number is unmeasurable, and
therefore uneditable by anyone who cannot see it.

`cut` draws nothing — it is the absence of a transition, not a zero-length one.

The clip view returned by the timeline endpoint omits transition *durations*;
fetch them from the inspector when the user opens a transition control, or draw
a fixed-width wedge as an indicator only.

---

## 9. The playhead

* A 1px line spanning every track, with a grab handle in the ruler.
* Always visible: if playback or a seek moves it outside the viewport, scroll to
  follow. Auto-follow suspends for five seconds after any manual scroll, so that
  looking ahead while playing is possible.
* Dragging it scrubs. Scrubbing calls
  `GET /v1/projects/{id}/preview?at=` on a **150ms trailing debounce**, never
  per frame. A scrub across a three-minute project must not produce three
  hundred requests.
* The current time is displayed numerically in the toolbar as `h:mm:ss.mmm`,
  selectable text — a user reporting a problem needs to be able to copy a
  timestamp.

---

## 10. Selection and multi-selection

Single selection is shared across all three panels (`EDITOR_INTERACTION_SPEC.md`
§2).

Multi-selection is timeline-local: `Shift`+click extends a range along a track,
`Cmd/Ctrl`+click toggles individual clips, marquee-drag on empty track space
selects intersecting clips. When more than one clip is selected:

* the script panel highlights the union of their lines
* the inspector shows a multi-selection summary (count, total duration, how many
  are locked), not the first clip's detail
* operations apply to all of them, batched into **one** `PATCH` — which makes it
  one undo step (`EDITOR_INTERACTION_SPEC.md` §11)

A batch is all-or-nothing. If one clip in a selection is locked, the whole batch
is refused with the lock message. The client should say which clip refused, and
offer to repeat the operation on the rest — it must not silently apply to some.

---

## 11. Snapping

Client-side only; the backend snaps to nothing.

Snap targets, in priority order when several are within threshold:

1. the playhead
2. clip edges on the same track
3. clip edges on the visual track (for clips on other tracks)
4. script block boundaries, from `links[].start` / `links[].end`
5. the project start, the project end, and `target_seconds`

Threshold is **8 screen pixels**, converted to seconds through the current
`pixelsPerSecond` — a fixed time threshold snaps unusably at high zoom.

Hold `Alt` to suppress. Show the active snap as a vertical guide with its target
named; a snap the user cannot see is a snap they will fight.

---

## 12. Zoom and virtualisation

**Range:** from whole-project-fits down to roughly 25ms/pixel. Below the minimum
clip length there is nothing further to resolve.

**Anchor:** zoom keeps the **playhead** at its screen position, not the viewport
left edge. If the playhead is off-screen, anchor on the pointer. Zooming away
from where the user is looking is disorienting.

**Controls:** `Cmd/Ctrl`+scroll, pinch, `-`/`=`, and a zoom-to-fit
(`Shift+Z`). `Shift`+scroll pans.

**Virtualisation:** render only clips intersecting the viewport plus one
viewport of margin on each side. At 4000 clips per track (the contract's cap)
naive rendering is tens of thousands of DOM nodes. The threshold to introduce
virtualisation is around 200 visible clips; below that it is complexity for
nothing.

For very dense timelines, an optional overview strip beneath the toolbar shows
the whole project with the viewport as a draggable window.

---

## 13. Live state: generating, failed, degraded, stale

The timeline shows unit state, because the timeline is where a user looks to
understand the whole project at once. State comes from the unit payload, joined
by `visual_unit_id`.

| Unit status | Clip treatment |
| --- | --- |
| `planned` | outlined, dimmed, "not generated yet" |
| `searching` / `generating` | animated indeterminate stripe, respecting `prefers-reduced-motion` (a static "working" badge instead) |
| `ready` | normal |
| `approved` | a check badge |
| `locked` | padlock badge and heavy border, at rest |
| `regenerating` | **the existing visual still drawn**, with a "new version coming" badge. The clip must not blank — the old version is what would be exported right now |
| `failed` | a warning badge and a distinct border. The clip still occupies its span; the project is not broken |
| `degraded` | a "fell back" badge, in an informational treatment, not an error one — it worked, just not as planned |
| `timing_invalidated` | a hatched overlay and a stale badge, plus the panel-level "re-plan" bar |

Every one of these carries an icon or text as well as a colour. Colour is never
the only signal.

A failed unit affects **one clip**. Neighbouring clips stay fully interactive,
and the timeline must not enter a global error state. One bad visual does not
destroy a project — that is a tested guarantee of the backend, and the interface
has to reflect it.

---

## 14. The render-scope overlay

When a user selects a render scope (`EDITOR_INTERACTION_SPEC.md` §9), the
timeline draws the region the render will actually cover.

Two things must be right:

* Draw the region using the `start`/`end` **the response returned**, not the
  ones sent. The backend expands scoped regions by one second either side
  (`BOUNDARY_PADDING_SECONDS`) so a transition spanning the boundary renders
  whole. Drawing the requested region would mislabel the edges.
* Label it as coverage — "Rendering 0:42–0:58" — never as a saving. Scoped
  rendering does not currently cost less than a full render
  (`FRONTEND_PRODUCT_SPEC.md` §15), and `fraction_of_project` describes the
  region, not the price.

In and out points for a `range` render are the same in/out pair used for loop
playback. One control, two uses.

---

## 15. Keyboard operation of the timeline

The timeline is a composite widget: one tab stop, with arrow keys navigating
inside it. Full shortcut table in `EDITOR_INTERACTION_SPEC.md` §12; the
timeline-specific requirements are:

* `Up`/`Down` move focus between tracks; `Left`/`Right` between clips on a track
  when a clip has focus, and seek when the ruler has focus. Which of the two is
  active must be visible.
* Every drag interaction has a keyboard equivalent: `Alt+Left/Right` nudges the
  focused clip by one frame, `Shift+Alt+Left/Right` by one second, `[`/`]`
  trim the in and out points of the focused clip.
* Trim, split, remove, lock, and transition are all reachable from the focused
  clip's context menu, opened with `Menu` or `Shift+F10`.
* Focus is visible on clips, tracks, the ruler and the playhead — an outline
  that survives against every clip fill.
* Status changes announce to a live region: "Visual 4 ready", "Visual 7 failed:
  no licensed asset found".
* Nothing is drag-only. This is the rule the timeline is most likely to break,
  and it is not negotiable.

---

## 16. Performance budget

| Interaction | Budget |
| --- | --- |
| Drag or trim, frame time | ≤ 16ms — local state, no request during the drag |
| Zoom, frame time | ≤ 16ms |
| Drop → server confirmation | one `PATCH`, and the clip must not visibly jump when it returns unless the server changed it |
| Scrub → preview metadata | 150ms trailing debounce |
| Initial timeline paint | ruler and tracks immediately; clips may fade in |

Drags are local until release. Sending an operation per pointer-move would
produce hundreds of versioned mutations, defeat the undo model (one `PATCH` =
one undo step) and make conflict detection meaningless.

---

## 17. Invariants

A correct implementation never violates these. They are worth writing as client
assertions in development builds.

1. **Every clip drawn has a `clip_id` from the server.** No client-invented
   clips.
2. **`x(clip.start)` and `x(clip.end)` are computed by the one time-mapping
   function.** No component does its own arithmetic.
3. **The script ↔ unit ↔ clip mapping comes from `links`.** Never derived.
4. **No two clips on an exclusive track are drawn overlapping.** If the server
   returns such a state, it is a bug in the server and should be reported, not
   papered over.
5. **A locked clip has no drag or trim handles**, and a refusal is still handled
   if one arrives.
6. **A derived track has no editing affordances at all.**
7. **`expected_version` accompanies every mutation.**
8. **The clip under the playhead matches `preview.clip.clip_id`** for the same
   `at`. If they disagree, the client's time mapping is wrong.
9. **A clip's visible state matches its unit's `status`.** No clip shows as
   ready while its unit is failed.
10. **Nothing is drag-only.**

---

## 18. What the timeline does not do

Named so nobody designs around them.

| Not supported | Why |
| --- | --- |
| Editing narration or captions directly | They are derived from the script; the backend refuses |
| Frame-accurate preview of unrendered edits | There is no live preview; the player shows the last render |
| Nested sequences, compound clips | Not in the contract |
| Per-clip colour correction, filters, effects | Not in the contract |
| Keyframes | Not in the contract |
| Audio ducking, per-clip fades on audio | Only `gain` exists, and the timeline view does not yet expose it |
| Ripple edit / roll edit | Operations are absolute and single-clip; a ripple would be a client-side batch, and is not specified here |
| Drag-and-drop of media from outside | Ingestion is a separate flow |
| Music and SFX content | The tracks are editable and empty; there is no library |
| A cheaper scoped render | The scope machinery is real; the cost saving is not, yet |

---

## Sources

* `src/vtv/contracts/tracks.py` — `Track`, `TimelineClip`, exclusivity,
  derivation, `gaps()`, `MIN_CLIP_SECONDS`, `PRECISION`
* `src/vtv/contracts/timeline.py` — `TransitionKind`
* `src/vtv/contracts/render_scope.py` — `RenderRegion.expand`,
  `BOUNDARY_PADDING_SECONDS`
* `src/vtv/pipeline/editing.py` — the operations and their refusals
* `src/vtv/pipeline/units.py` — how the timeline is built and rebuilt
* `src/vtv/api/product.py` — `_timeline_view`, `_clip_view`, `links`
* `tests/test_product_domain.py` — the arithmetic and the refusals
* `docs/EDITOR_INTERACTION_SPEC.md` — per-interaction behaviour
* `docs/FRONTEND_PRODUCT_SPEC.md` — the screen this panel lives in
