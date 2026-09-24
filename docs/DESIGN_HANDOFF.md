# Design handoff

**Status:** the backend described here is built and tested. The interface is
not. This document is what a designer needs in order to start drawing, and what
they need to know so that what they draw can actually be built against the
system that exists.

Companions:

* `FRONTEND_PRODUCT_SPEC.md` — what the product is, what the screens are
* `EDITOR_INTERACTION_SPEC.md` — what happens on every interaction
* `TIMELINE_UI_SPEC.md` — the timeline in detail

This document deliberately specifies **no visual identity**: no palette, no
typeface, no spacing scale, no radius. Those are the designer's to choose. What
it does specify is everything that constrains that choice — the states that must
be distinguishable, the information that must be present, the promises the
interface must keep.

---

## Table of contents

1. [The one-paragraph brief](#1-the-one-paragraph-brief)
2. [Who is using this](#2-who-is-using-this)
3. [The five promises the interface must keep](#3-the-five-promises-the-interface-must-keep)
4. [Screen inventory](#4-screen-inventory)
5. [Component inventory](#5-component-inventory)
6. [State inventory](#6-state-inventory)
7. [Semantic colour and iconography](#7-semantic-colour-and-iconography)
8. [Typography needs](#8-typography-needs)
9. [Motion](#9-motion)
10. [Copy and tone](#10-copy-and-tone)
11. [Accessibility acceptance criteria](#11-accessibility-acceptance-criteria)
12. [Responsive scope](#12-responsive-scope)
13. [Real data to design against](#13-real-data-to-design-against)
14. [What does not exist, and must not be drawn](#14-what-does-not-exist-and-must-not-be-drawn)
15. [Open design questions](#15-open-design-questions)
16. [Suggested build order](#16-suggested-build-order)
17. [Definition of done](#17-definition-of-done)

---

## 1. The one-paragraph brief

A person speaks, or writes, or pastes a script. The system produces a video: it
finds or generates a visual for each stretch of narration, lays them on a
timeline, and renders. The interface's job is to let that person **see what the
system decided, understand why, and change any part of it** — one line, one
picture, one clip at a time — without ever being surprised by something the
system changed on its own.

The hard part is not the editor. It is the honesty: showing what failed, what
degraded, what is stale, and what the video will actually contain, at the moment
the user needs to know.

---

## 2. Who is using this

Three people, in rough order of volume.

**The person who just wants a video.** Recorded a voice note, wants something
watchable. Will accept the system's choices, might swap one or two pictures,
will never open the timeline. Everything they need must be reachable from the
script panel and the visual list.

**The person with a script.** A marketer, an educator, a creator. Wrote the
words, cares about them, will not accept the system rewriting them. Uses
revisions as suggestions. Approves and locks visuals. Might open the timeline to
fix pacing.

**The person who edits.** Comes from Premiere or CapCut, expects the timeline to
behave. Will find every place the interface lies to them. Designs should be
checked against this person for correctness and against the first person for
approachability — in that order, because a confusing tool is recoverable and a
dishonest one is not.

---

## 3. The five promises the interface must keep

If a design breaks one of these it is wrong, however good it looks.

**1. Nothing the user owns changes on its own.** Approved, locked, and
version-selected are decisions. No automatic process crosses them. A lock is
visible at rest — a padlock on the card and on every clip, always, not on hover.

**2. The script is never silently altered.** Every AI text change arrives as a
proposal the user reads and accepts or rejects, showing before, after, and the
duration delta. Accept is not the default focus.

**3. Failure is scoped and stated.** One visual failing shows as one visual
failing. The project keeps working. Degraded output says what it fell back to.
Neither is hidden at export.

**4. Time is never misrepresented.** If the narration is longer than the target,
the interface says so and offers two honest options — shorten the script (as a
proposal) or accept the longer video. It never implies the voice was sped up or
content was cut, because neither happens.

**5. Cost and progress are truthful.** A regeneration costs money and shows its
cost. A scoped render covers a region; it is not currently cheaper, and must not
be sold as such. A spinner never stands in for a queued job, and a completed
state never stands in for a queued one.

---

## 4. Screen inventory

Four screens. Full descriptions in `FRONTEND_PRODUCT_SPEC.md` §2–3.

| Screen | Purpose | Design weight |
| --- | --- | --- |
| **Start** | Record, or write/paste a script. The mode-A / mode-B fork. | High — it is the whole first impression, and neither option may be the visual default |
| **Projects** | List, status, resume, export | Low |
| **Studio** | Everything: script, preview, inspector, timeline | Very high — this is the product |
| **Export** | Format, disclosures, download | Medium — the disclosure design matters more than the layout |

The Studio layout (script left, preview top-right, inspector beneath it,
full-width timeline below) is specified in `FRONTEND_PRODUCT_SPEC.md` §3 with the
reasoning. Treat it as a strong default, not a law — but any alternative must
keep script, preview and timeline simultaneously visible, because the product's
core idea is that they are three views of one object.

---

## 5. Component inventory

Every component the specs require. Each needs a full state set (§6).

### Script

* Script line — read, hover, focus, selected, editing, invalid, stale-timing
* Line gutter — duration, unit index, unit-grouping bracket
* Grouping bracket — the vertical rule spanning the lines one visual covers
* Script toolbar — revision menu (10 kinds), re-plan, pacing
* Divergence banner — voice no longer matches script
* Stale-timing bar — "N visuals need re-planning" with a re-plan action
* Revision diff dialogue — per-line before/after, duration delta, affected count,
  two equally weighted buttons

### Visual

* Unit card — index, status badge, thumbnail-or-placeholder, lock badge
* Unit list — scrolling, virtualised at length
* Inspector — status, covered script lines, selected version detail, rationale
  prose, version list, cost per version, lock, approve, regenerate split-button
* Regenerate split-button — primary action plus a 10-item intent menu
* Version list row — number, strategy, intent, grounding, consistency, cost
* Status badge — ten statuses, each icon + text

### Timeline

* Toolbar — transport, time display, zoom, fit, render-scope indicator
* Ruler — adaptive ticks, target-duration marker, in/out points
* Track header — name, mute, lock, derived chip, height handle
* Track lane — clips, gaps, drop indicator
* Clip — five width tiers of content, four source-kind fills, all live states
* Gap — hatched, with duration
* Transition wedge — with numeric duration
* Playhead — line, handle, follow behaviour
* Snap guide — line plus named target
* Render-scope overlay — region with label
* Overview strip (optional, dense projects)

### Preview

* Player — with stale marker, render-needed state, failed state
* Preview metadata strip — current clip, current unit, current lines

### Cross-cutting

* Job progress — inline, per-unit, and project-level
* Toast / inline error — with retry where retryable, without where not
* Empty state — one per panel, each with an action
* Skeleton loader — script lines, unit cards, clips
* Command palette — every action in `EDITOR_INTERACTION_SPEC.md`
* Shortcut reference sheet
* Export disclosure list — failed, degraded, diverged, overrun

---

## 6. State inventory

Design every one of these. They are not edge cases; on a system that calls
external AI providers they are the normal week.

### Per unit — ten statuses

`planned` · `searching` · `generating` · `ready` · `approved` · `locked` ·
`regenerating` · `failed` · `degraded` · `timing_invalidated`

Two need particular care:

* **`regenerating`** — the *existing* visual is still shown. It is what would be
  exported right now. Design a treatment that says "a replacement is coming"
  without implying the current one is gone.
* **`degraded`** — it worked, just not as planned. This is not an error
  treatment. It is an informational one, naming what it fell back to.

### Per panel — four states each

Empty, loading, error, and the panel's own "needs attention" state. Specified per
panel in `EDITOR_INTERACTION_SPEC.md` §13.

### Per pacing verdict — four

`on_target` (quiet), `filled` (normal), `underfilled` (warning with the
shortfall), `overrun` (blocking decision with two options).

### Project-level

* nothing yet — no script
* script, no visuals planned
* planning
* generating (N of M)
* ready, never rendered
* rendered, timeline unchanged
* rendered, timeline changed since (the "stale preview" state)
* rendering
* render failed
* has failed units
* has degraded units
* script diverged from recording

Several can be true at once. The design needs a way to present two or three
simultaneous project-level conditions without a stack of banners consuming the
screen.

---

## 7. Semantic colour and iconography

The palette is the designer's. The **semantics** are not.

| Meaning | Where it appears | Requirement |
| --- | --- | --- |
| Working | generating, searching, regenerating, rendering | Motion or a distinct fill; must have a static form under `prefers-reduced-motion` |
| Ready | ready | Neutral. Most things are ready; it must not shout |
| User-approved | approved | Distinct from ready and from locked |
| **Locked** | locked units, locked clips, locked tracks | Padlock, visible at rest, plus a non-colour treatment (border weight) |
| Degraded | degraded | Informational, not error |
| Failed | failed | Error, but scoped — must not read as "the project is broken" |
| Stale | timing_invalidated, stale preview | A hatch or pattern; distinct from both degraded and failed |
| Derived / not editable | narration and caption tracks | A pattern plus a chip; must read as "owned elsewhere", not "disabled" |
| Gap | uncovered time on an exclusive track | Distinct from an `empty` clip, which is a deliberate hold |
| Needs your decision | overrun, divergence, revision proposal | The only treatment allowed to be visually loud |

**Colour is never the only signal.** Every state above needs an icon, a pattern,
or text as well. This is not only for colour-vision deficiency: these states are
read at a glance across forty clips, and shape parses faster than hue.

Distinguishability requirements worth testing explicitly:

* locked vs approved
* degraded vs failed
* stale vs degraded
* an `empty` clip vs a gap
* a derived track vs a locked track

---

## 8. Typography needs

* **Script text** is the product's primary content. It is read at length, edited
  in place, and diffed. It needs a comfortable reading measure and a size that
  survives an hour of work. Do not treat it as UI text.
* **Timecodes are tabular.** `0:09:03.482` next to `0:10:00.000` must not
  shimmer. Tabular figures, monospaced or a proportional face with tabular
  numerals.
* **Diff rendering** needs before/after to be distinguishable without colour —
  weight, a prefix marker, or position.
* **Truncation is everywhere**: clip labels, version strategies, script lines in
  the inspector. Specify the truncation rule (ellipsis, position, tooltip) once
  rather than per component.
* **Provider and technical terms** (`stock_footage`, `use_typography`) must be
  humanised in the interface. Design the mapping table alongside engineering;
  the enums are closed and short, so a complete table is achievable.

---

## 9. Motion

* **Nothing moves for decoration.** Every animation either shows progress,
  shows a relationship, or is absent.
* **`prefers-reduced-motion` is honoured everywhere**, including the timeline
  playhead follow, the generating stripe, and panel transitions. Each needs a
  designed static equivalent, not merely a disabled animation.
* **The playhead is exempt** — it must move during playback, because it is
  representing time. Its *follow-scroll* is not exempt and should jump rather
  than smooth-scroll under reduced motion.
* **Drag is 60fps and local.** No network round-trip during a drag.
* **Do not animate the arrival of a failure.** A failure sliding in draws the eye
  and reads as more catastrophic than it is; one visual failed, and the tone
  should match.

---

## 10. Copy and tone

The backend already writes user-facing sentences for every refusal, and the
interface shows them verbatim. Examples, unedited:

> That clip is locked. Unlock it to change it.

> The caption track comes from your script. Edit the script instead — a change
> here would be lost the next time the project is re-planned.

> That would overlap another clip on the visual track. Move or trim it first.

> Someone else changed this timeline. Reload and try again.

> Visual 4 is locked. Unlock it first if you want it regenerated.

The house style these establish:

* **Say what happened, then what to do.** Both, in that order, in one or two
  sentences.
* **Second person, active voice.** "You locked this", not "this has been locked".
* **No blame and no apology.** A refusal is the system working.
* **Never expose internals.** No error codes, no provider names, no stack
  traces, no internal cost or margin. A correlation id on a 500 is the one
  exception, and it is presented as something to quote, not something to read.
* **Never promise what does not exist.** No "instantly", no "free", no "faster"
  for a scoped render.

Designers should write copy for the states the backend does not supply — empty
states, onboarding, the export disclosures, the overrun decision — in the same
register.

---

## 11. Accessibility acceptance criteria

Testable, not aspirational. A design that cannot meet these is not done.

1. **Every action has a keyboard path**, and it is discoverable — the command
   palette and the shortcut sheet are part of the design, not extras.
2. **Nothing is drag-only.** Move, trim, split, reorder, set in/out all have
   non-drag equivalents in the inspector or via the keyboard.
3. **Focus is always visible**, including on timeline clips against every fill,
   on the playhead, and on script lines in edit mode.
4. **Colour is never the only signal** for any state in §7.
5. **Status changes announce** to a live region: "Visual 4 ready", "Visual 7
   failed". Announcements are polite, not assertive, except for states that
   require a decision.
6. **`prefers-reduced-motion` has a designed static form** for every animation.
7. **Contrast** meets WCAG AA for text and 3:1 for the non-text state indicators
   in §7 — including clip fills against the track background.
8. **The timeline is one tab stop** with documented arrow-key navigation inside.
9. **Text scales to 200%** without loss of function. The timeline may scroll;
   the script panel must reflow.
10. **Captions are always exported.** There is no UI toggle that could appear to
    turn them off, because the backend always emits them.

---

## 12. Responsive scope

**Editing is desktop-only in v1.** A timeline with frame-level trimming on a
390px screen is a worse product than no timeline.

| Breakpoint | Scope |
| --- | --- |
| ≥ 1440px | Full Studio, comfortable |
| 1100–1440px | Full Studio, inspector may collapse to a drawer |
| 768–1100px | Script + preview + unit list. Timeline collapses to a read-only overview strip |
| < 768px | Mobile: start by speaking, watch progress, review script and visuals as a list, approve / lock / regenerate individual visuals, export and share |

Mobile explicitly does **not** attempt: the timeline, trimming, splitting, drag,
transitions, or track management. Design it as a review-and-approve companion,
not a shrunken editor.

---

## 13. Real data to design against

Design against these shapes, not against three tidy items.

| Thing | Typical | Design for |
| --- | --- | --- |
| Script length | 300–900 words | 20 words, and 60,000 (the cap is 400,000 characters) |
| Script line length | 8–20 words | 1 word, and 60 words with no punctuation |
| Visual units | 15–60 | 1, and 400 |
| Versions per unit | 1 | 1, and 12 |
| Clips per track | 15–60 | 1, and 4000 (the contract cap) |
| Project duration | 60–300s | 8s, and 90 minutes |
| Clip duration | 3–8s | 0.04s (the minimum) and 120s |
| Failed units | 0 | 1, several scattered, and all of them |
| Languages | English | RTL scripts, CJK, and long-word languages — the script panel must not assume Latin metrics |

Two specific cases worth an explicit mock each, because they are common and ugly:

* **A 400-clip timeline at whole-project zoom**, where most clips are under 6px.
* **A project where a third of the visuals are `degraded`** because a provider
  credential is missing — which is the current default state of the system.

---

## 14. What does not exist, and must not be drawn

Drawing these creates a promise engineering cannot keep.

| Gap | Design consequence |
| --- | --- |
| Live preview of unrendered edits | The player shows the **last render**. Design the stale marker; do not design a live scrub |
| Cheaper scoped rendering | Show coverage ("Rendering 0:42–0:58"), never a saving or a speed-up |
| Per-line accept of a revision | The diff dialogue has no per-line checkboxes |
| Voice re-recording | The divergence banner has no re-record action |
| Music and SFX libraries | The tracks exist and are editable; there is nothing to put on them. Do not design a browser |
| Real image and video generation | Without a provider credential every regeneration returns typography. The degraded state is the common case, not the rare one |
| Free-text prompting | Revisions and regenerations take closed menus, on purpose. No prompt field anywhere |
| Deleting a script line | Not an operation the API exposes |
| Ripple / roll edits, keyframes, filters, nested sequences | Not in the contract |

---

## 15. Open design questions

Genuine forks. Each needs a decision before the Studio can be built.

1. **How does grouping read in the script panel?** A visual covers several lines.
   `FRONTEND_PRODUCT_SPEC.md` proposes a gutter bracket rather than a repeated
   thumbnail per line. Is a bracket legible at 40 units?
2. **Where does the "needs your decision" surface live** when two are true at
   once — an overrun *and* a voice divergence? A banner stack, a single
   consolidated bar, or a persistent side rail?
3. **Inspector as a panel or a drawer?** A panel is always visible and costs
   width the timeline wants. A drawer is roomier and hides the rationale, which
   is the thing users most need to see.
4. **How is a version history presented** — a list, a filmstrip, or a
   before/after toggle? Users compare versions visually, and a text list of
   strategies is not how anyone compares pictures.
5. **What does a 400-clip timeline look like at fit-zoom** such that it is still
   navigable? Overview strip, density collapse, or something else.
6. **Does the unit list and the timeline coexist**, or are they two views of the
   same thing the user toggles? Both show every visual, in order.
7. **How prominent is cost?** Per-version `cost_usd` is honest and can be
   anxiety-inducing. Always visible, on hover, or in a session total?
8. **Onboarding for mode B.** Someone who pastes a script has never seen a
   visual unit. What is the minimum explanation, and where does it go?

---

## 16. Suggested build order

Sequenced so that each stage is independently usable and testable against the
real backend.

1. **Start screen + projects list** — proves auth, project creation, the
   mode-A/mode-B fork.
2. **Script panel, read-only** — proves the script endpoint, blocks, timing,
   the divergence flag.
3. **Script editing + re-plan** — proves `PATCH` blocks, stale timing, planning.
4. **Unit list + inspector, read-only** — proves the unit payload, statuses,
   rationale, versions.
5. **Lock, approve, version select** — the first user-owned state. Small, and it
   proves the promise in §3.1.
6. **Revision propose / diff / accept / reject** — the first job, the first
   event-stream consumer, the first proposal dialogue.
7. **Regenerate** — the first spend, idempotency keys, per-unit progress and
   per-unit failure.
8. **Pacing** — the four verdicts, including the blocking overrun decision.
9. **Preview + render (full project)** — the player, the stale marker, export
   disclosures.
10. **Timeline, read-only** — ruler, tracks, clips, gaps, links, selection
    across three panels.
11. **Timeline editing** — operations, refusals, warnings, optimistic
    concurrency, undo.
12. **Scoped render** — in/out, region overlay, expanded boundaries.
13. **Mobile review companion.**

Steps 1–9 are a complete, shippable product for the first two of the three users
in §2. The timeline is the last third, not the first.

---

## 17. Definition of done

A screen is done when:

* every state in §6 that applies to it is designed, not just the happy one
* every accessibility criterion in §11 is met and has been checked, not assumed
* every string is written — including empty states, refusals the backend does
  not supply, and the export disclosures
* nothing from §14 appears anywhere in it
* the five promises in §3 survive a hostile read of the design by someone
  looking for a place where the interface says something the system will not do

---

## Sources

* `docs/FRONTEND_PRODUCT_SPEC.md` — screens and product framing
* `docs/EDITOR_INTERACTION_SPEC.md` — per-interaction behaviour, error mapping
* `docs/TIMELINE_UI_SPEC.md` — timeline data, layout and invariants
* `src/vtv/api/product.py` — every endpoint and payload the interface consumes
* `src/vtv/contracts/{script,visual_unit,tracks,pacing,render_scope}.py` — the
  state names and their meanings
* `tests/test_product_scenarios.py` — the promises in §3, driven through the
  real application
