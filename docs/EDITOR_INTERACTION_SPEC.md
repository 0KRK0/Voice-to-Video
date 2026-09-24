# Editor interaction specification

**Status:** specification for a frontend that does not exist yet. Every endpoint,
field, refusal and state name in this document is implemented and tested in the
backend today; nothing here describes a UI that has been built.

Read `FRONTEND_PRODUCT_SPEC.md` first — it says what the screens are and why.
This document says what happens when a user does a specific thing. It is written
as a contract: for each interaction, what the user did, what the client sends,
what the backend does, what comes back, and what the interface must show —
including when the answer is "no".

---

## Table of contents

1. [Principles](#1-principles)
2. [Selection and the three-panel link](#2-selection-and-the-three-panel-link)
3. [Script interactions](#3-script-interactions)
4. [Revision interactions](#4-revision-interactions)
5. [Visual unit interactions](#5-visual-unit-interactions)
6. [Timeline interactions](#6-timeline-interactions)
7. [Playback interactions](#7-playback-interactions)
8. [Pacing and duration interactions](#8-pacing-and-duration-interactions)
9. [Render and export interactions](#9-render-and-export-interactions)
10. [Optimistic concurrency and conflict](#10-optimistic-concurrency-and-conflict)
11. [Undo](#11-undo)
12. [Keyboard shortcuts](#12-keyboard-shortcuts)
13. [Loading, empty, error and degraded states](#13-loading-empty-error-and-degraded-states)
14. [Error code to interface mapping](#14-error-code-to-interface-mapping)
15. [What the client must never do](#15-what-the-client-must-never-do)

---

## 1. Principles

Five rules that decide every ambiguous case below.

**1. Free and instant, or costly and queued — never in between.** Structural
edits (move a clip, lock a visual, retype a line) are synchronous `PATCH`
requests that return the new state. Anything that spends money or calls a
provider (propose a revision, regenerate a visual, render) returns
`202 {job_id}` and finishes on the event stream. The interface must not show a
spinner for the first kind or a completed state for the second.

**2. The server decides; the client displays.** The backend refuses illegal
edits with a sentence written for a human. The client shows that sentence. It
does not implement its own copy of the rules in order to grey out a button —
that produces two rule engines that will drift, and the one the user is looking
at will be the wrong one. The client *may* disable a control when the state it
already has makes the answer certain (a locked clip cannot be trimmed), but it
must still handle the refusal when it comes.

**3. A user-owned state is never crossed silently.** Approved, locked, and
selected-version are decisions. No automatic process moves them, and no
interaction in this document moves them without the user having asked for that
specific thing.

**4. Nothing is drag-only.** Every action reachable by dragging is also
reachable from the keyboard and from the inspector. This is an accessibility
requirement, not a nicety.

**5. Stale is shown, not hidden.** When a script edit invalidates timing, the
affected visuals are marked. The interface never presents stale timing as
current.

---

## 2. Selection and the three-panel link

The Studio has one selection, shared by three panels. Selecting anything selects
the corresponding thing everywhere else.

The mapping is **served, not derived**. `GET /v1/projects/{id}/timeline` returns
a `links` array:

```json
{"visual_unit_id": "vun_…", "clip_id": "clp_…",
 "start": 12.4, "end": 18.1, "script_block_ids": ["blk_…", "blk_…"]}
```

The client builds two indexes from it once per timeline load — `block_id → link`
and `clip_id → link` — and uses them for every selection. Deriving the
relationship from spans or ordering is forbidden: it is a second implementation
of a relationship the backend already owns.

| User selects | Script panel | Preview | Inspector | Timeline |
| --- | --- | --- | --- | --- |
| A script line | line highlighted, scrolled into view | seeks to `link.start` | shows the line's unit | clip highlighted, scrolled into view |
| A visual clip | every `script_block_ids` line highlighted | seeks to `clip.start` | shows the clip's unit | clip highlighted |
| A unit in the inspector | its lines highlighted | seeks to unit `start` | already showing | its clips highlighted |

**A line with no unit yet** (script created, units not planned) selects in the
script panel only. The preview does not seek, the inspector shows the
"not planned yet" empty state, and the timeline shows nothing selected. It must
not appear broken; it is not planned yet, and the panel says so.

**A clip with `visual_unit_id: null`** — legal, e.g. a music clip — selects in
the timeline and the inspector, and clears the script selection rather than
leaving a stale line highlighted.

---

## 3. Script interactions

### 3.1 Click a script line

Selects it (§2). Does not enter edit mode. A single click that started editing
would make every navigational click a potential accidental edit.

### 3.2 Double-click, or Enter on a focused line — edit the line

Enters inline edit on that block only. The rest of the script stays read-only:
this editor addresses **blocks**, not a free-flowing document, because the block
id is what carries the link to a visual and to a clip.

On commit (blur, or `Cmd/Ctrl+Enter`):

```
PATCH /v1/projects/{id}/script/blocks/{block_id}
{"text": "…"}
→ 200 {…script…, "timing_invalidated_units": ["vun_…"]}
```

Synchronous. On return the client must:

* re-render the script from the response (the block's `estimated_seconds` will
  have changed, and so will the script `version`)
* mark every unit in `timing_invalidated_units` as stale in the inspector and
  the timeline
* show a non-blocking bar: *"Timing changed for N visuals. Re-plan to update the
  timeline."* with a **Re-plan** action calling
  `POST /v1/projects/{id}/visual-units`

The client must **not** auto-re-plan. Re-planning rebuilds the timeline, and
doing that on every keystroke-commit would move clips under a user who was only
fixing a typo.

**Escape** cancels the edit and restores the previous text without a request.

**Empty text** is a validation failure — *"a line cannot be empty; mute it
instead"* — not a delete. Deleting a line is not an operation this API exposes;
see §15.

**Unchanged text** is a no-op: the backend returns the script with the same
version. The client should not show a "saved" confirmation for it, and must not
assume the version advanced.

Editing a line whose timing was **measured from a recording** (mode A) sets
`diverged_from_recording`. The audio still says the old words. See §3.3.

### 3.3 The spoken-script divergence prompt

When the script's `origin` is `spoken` and `diverged_from_recording` is `true`,
the script panel shows a persistent (not dismissable-forever) banner:

> Your recorded voice no longer matches this script. The video will use your
> original recording unless you re-record.

There is no "re-record" action to call yet (`FRONTEND_PRODUCT_SPEC.md` §15). The
banner is informational and must not pretend otherwise. Showing it is
non-negotiable: exporting audio that says something other than the captions,
without having said so, is the single worst failure this product can have.

### 3.4 Hover a script line

Shows, in the gutter, the line's `estimated_seconds` and — if it has one — a
small thumbnail-free indicator of its unit index ("Visual 4"). No request. Hover
never triggers a fetch; a user reading down a script would fire one per line.

---

## 4. Revision interactions

The rule from the product prompt, restated because it governs everything here:
**the system must not silently alter the user's script.** A revision is a
*document* the user reads and decides on, never a mutation that has already
happened.

### 4.1 Request a revision

From the script panel toolbar, or the per-line menu for a scoped revision.

```
POST /v1/projects/{id}/script/revisions
Idempotency-Key: <client uuid>
{"kind": "improve_clarity", "block_ids": ["blk_…"], "target_language": null}
→ 202 {"job_id": "job_…"}
```

`kind` is one of ten fixed values — `fix_grammar`, `improve_clarity`, `enhance`,
`shorten`, `expand`, `make_formal`, `make_cinematic`, `make_educational`,
`make_concise`, `translate`. **The UI must present these as a menu.** There is
no free-text instruction field, deliberately: free text here reaches a language
model that decides what a user's video says.

Omitting `block_ids` revises the whole script. Sending them scopes it.

While the job runs the script panel shows a subdued "Reviewing your script…"
state. The script stays **fully editable** during it; if the user edits, the
proposal that arrives will be based on an older version and must be handled as
superseded (§4.4).

### 4.2 The proposal dialogue

`script.revision.proposed` arrives on the event stream carrying the
`revision_id`. The client renders a **diff dialogue**, not an applied change:

```
┌─ Suggested revision — Improve clarity ─────────────────┐
│                                                        │
│  – The thing about the ocean is that it's really big   │
│  + The ocean is vast.                                  │
│                                                        │
│  – It has a lot of water in it and stuff                │
│  + It holds most of the water on Earth.                │
│                                                        │
│  Estimated duration:  1:42  →  1:31   (−11s)           │
│  Affects 3 visuals — their timing will need updating.  │
│                                                        │
│               [ Reject ]        [ Accept ]             │
└────────────────────────────────────────────────────────┘
```

Required elements:

* **Per-line before and after**, in that order, visually distinguished by more
  than colour.
* **The duration delta**, signed. A revision that shortens a script by eleven
  seconds changes the video, and a user is entitled to know before accepting.
* **The count of affected visuals.**
* Two equally-weighted buttons. Accept is not the default focus. The safe action
  and the destructive action must not be one keystroke apart.

### 4.3 Accept and reject

```
POST /v1/projects/{id}/script/revisions/{revision_id}
{"accept": true}    → 200 {"revision_id", "status": "accepted", "script": {…}}
{"accept": false}   → 200 {"revision_id", "status": "rejected", "script": {…}}
```

**Accept** is the only path in the entire system that changes `current_text`.
The response carries the new script; the client re-renders from it and shows the
same "N visuals need re-planning" bar as §3.2.

**Reject** changes nothing. The script in the response is byte-identical to what
the client already had. The dialogue closes, and the client shows a brief
confirmation — silence after a reject reads as a failure.

Accept is **all or nothing**. Per-line accept is not implemented
(`FRONTEND_PRODUCT_SPEC.md` §15). The dialogue must not show per-line
checkboxes, because there is nothing behind them.

### 4.4 A superseded proposal

If the user edited the script while the proposal was being produced, the
proposal's `based_on_version` no longer matches and its status becomes
`superseded`. The client shows:

> Your script changed while this suggestion was being prepared. Ask again to get
> a suggestion for the current text.

with a **Try again** button that re-issues §4.1 with a **new** idempotency key.
Reusing the old key returns the old, stale job.

---

## 5. Visual unit interactions

### 5.1 Click a visual

Selects it (§2) and opens it in the inspector.

### 5.2 Hover a visual

Shows a tooltip with the unit's `status`, its span, and — if it is not
`ready`/`approved` — the reason. No request.

Specifically:

| Status | Tooltip |
| --- | --- |
| `planned` | "Planned — not generated yet" |
| `searching` / `generating` | "Working…" plus elapsed time |
| `ready` | the selected version's `strategy`, e.g. "Stock footage" |
| `approved` | "Approved by you" |
| `locked` | "Locked — automatic changes will skip this" |
| `regenerating` | "Making a new version. The current one stays until it succeeds." |
| `failed` | the failure reason, verbatim from `detail` |
| `degraded` | what it fell back to and why |
| `timing_invalidated` | "The narration under this changed. Re-plan to update." |

### 5.3 The inspector

For the selected unit, from `GET /v1/projects/{id}/visual-units`:

* index and status badge (icon + text, never colour alone)
* the script lines it covers, as text
* the **selected version**: strategy, `grounding`, `consistency`, `rationale`
* a version list, newest first, each with its number, strategy, intent and
  `cost_usd`
* a lock control
* an approve control
* a regenerate split-button

`rationale` is shown as prose, not hidden behind an info icon. "Why did it
choose this?" is the question users ask most about an AI director, and the
answer exists.

`cost_usd` shown per version is the customer-facing cost. Internal margin and
provider pricing are not in this payload and must never be surfaced.

### 5.4 Regenerate

The split-button's primary action is "Regenerate"; its menu is the ten
`RegenerationIntent` values — `same_idea`, `more_cinematic`, `more_realistic`,
`more_educational`, `simpler`, `use_real_source`, `use_animation`,
`use_generated_image`, `use_generated_video`, `use_typography`. Again: a closed
menu, not a text field.

```
POST /v1/projects/{id}/visual-units/{unit_id}/regenerate
Idempotency-Key: <client uuid>
{"intent": "more_cinematic"}
→ 202 {"job_id": "job_…", "visual_unit_id": "vun_…"}
```

Interface behaviour while it runs:

* the unit shows `regenerating`
* **the existing visual stays on screen and on the timeline.** It is still the
  selected version until the new one succeeds. An interface that blanks the clip
  during regeneration is lying about what would be exported right now.
* other units are untouched and remain fully interactive

On `visual.unit.regenerated`, the client refetches the unit, shows the new
version selected, and surfaces the version list so the user can see there is now
a v2 to go back from.

On `visual.unit.failed`, the unit shows `failed` with the reason — **and the
previous version is still selected and still renderable**. The failure is scoped
to one unit; the project is not in an error state and must not be presented as
one.

**Regenerating a locked unit is refused** before any job is queued:

```
403 {"error": {"code": "permission_denied",
     "message": "Visual 4 is locked. Unlock it first if you want it regenerated.",
     "retryable": false}}
```

The client shows that message with an **Unlock** action next to it. It must not
auto-unlock: the whole value of a lock is that nothing removes it except the
person who set it.

### 5.5 Approve

```
PATCH /v1/projects/{id}/visual-units/{unit_id}
{"approved": true}
→ 200 {…unit…}
```

Approval is an opinion: it records that a human looked and said yes. It does not
prevent automatic replacement — that is what a lock does. The interface must
describe them differently, because a user who believes approval protects
something will lose work.

Approving an already-locked unit is a validation failure ("that visual is
already locked"). Show it inline; a lock is the stronger statement and there is
nothing to add.

### 5.6 Lock and unlock

```
PATCH /v1/projects/{id}/visual-units/{unit_id}
{"locked": true}
→ 200 {…unit…, "locked": true, "status": "locked"}
```

**A lock is visible at rest.** Not on hover, not in a menu — a padlock on the
unit card and on every timeline clip belonging to it, permanently. A user
scanning a forty-visual project must be able to see what is protected without
touching anything.

What a lock guarantees, and what the interface should say it guarantees:

* regeneration is refused (§5.4)
* re-planning after a script edit preserves the unit and its claim on its lines
* timeline edits to its clips are refused (§6.7)

Unlock is the same call with `false`. It is immediate and needs no confirmation
— it is reversible, and confirmation dialogues on reversible actions train
people to dismiss dialogues.

### 5.7 Choose a version (replace)

```
PATCH /v1/projects/{id}/visual-units/{unit_id}
{"version_id": "vvr_…"}
→ 200 {…unit…}
```

Selecting a different version swaps what the unit shows. On the timeline this
surfaces as `replace_source` on the clip: **identity and span are preserved**.
The clip id, the script link, the lock and the seek target all survive, because
changing a picture must not renumber the edit.

The version list must show every version, including ones the user paid for and
rejected. Discarding versions discards work the customer was charged for.

### 5.8 Change style

There is no separate "style" endpoint. Style is expressed two ways:

* **Per unit** — a regeneration intent (§5.4). "Make this one more cinematic."
* **Per project** — the pacing mode (§8), which carries the project's shot-length
  and fill character.

The interface should present per-unit style as part of the regenerate menu, not
as a separate control that implies a free instant restyle. Every style change is
a regeneration, costs money, and takes time. Saying otherwise sets up a
disappointment.

---

## 6. Timeline interactions

All timeline mutations go through one endpoint:

```
PATCH /v1/projects/{id}/timeline
{"expected_version": 7, "operations": [ … ]}
→ 200 {"version": 8, "changed_clip_ids": [...],
       "affected_unit_ids": [...], "warnings": [...], "duration": 184.2}
```

Synchronous, atomic, at most 200 operations per request. A batch either fully
applies or fully fails; the version advances **once** per request, which makes a
batch one undo step.

`expected_version` should always be sent. See §10.

The interface never sends `force: true` — the backend strips it from the wire
regardless (overriding a user's lock is an operator action with an audit record,
not a client flag).

### 6.1 Hover a timeline clip

Tooltip: the unit index, the source kind, the duration, and the lock state.
Handles (trim edges, transition markers) appear on hover but must also appear on
keyboard focus.

### 6.2 Click a timeline clip

Selects (§2). Does not seek the playhead — clicking a clip to inspect it should
not lose the user's playback position. Double-click seeks to the clip start.

### 6.3 Drag a clip (move)

During the drag the client shows the proposed position locally. On drop:

```json
{"kind": "move", "clip_id": "clp_…", "start": 42.5}
```

Duration is preserved by the backend; only `start` is sent. The client must not
compute and send a new `end` — that is how a drag silently becomes a trim.

Snapping is a **client** behaviour: snap to clip edges, to the playhead, and to
script-block boundaries, with `Alt` to suppress. The backend quantises to
milliseconds (`PRECISION = 3`) and will return the quantised value; the client
re-renders from the response rather than from its own arithmetic.

**On an exclusive track** (`narration`, `visual`, `caption`, `chapter`) a move
that would overlap is refused:

> That would overlap another clip on the visual track. Move or trim it first.

The client reverts the clip to its original position and shows that sentence
near the clip, not in a global toast the user has to hunt for.

### 6.4 Trim a clip

Dragging an edge:

```json
{"kind": "trim", "clip_id": "clp_…", "start": 42.5, "end": 47.0}
```

Either bound may be omitted to leave it unchanged. Minimum clip length is
**0.04s** (`MIN_CLIP_SECONDS`); below it the backend refuses:

> A clip must last at least 0.04s; remove it instead of trimming it to nothing.

The client should stop the drag at the minimum rather than let the user drag
into a refusal.

"Hold this two seconds longer" is `extend`, which takes a delta:

```json
{"kind": "extend", "clip_id": "clp_…", "at": 2.0}
```

Prefer `extend` for keyboard and inspector duration changes — making a user
compute an absolute end from a start they cannot see is how off-by-one edits
happen.

### 6.5 Change duration from the inspector

The inspector's duration field is the non-drag path to §6.4, and it is the one
that must work on a screen reader. It sends `extend` with the signed delta.

A duration change on a **visual** clip does not change the narration under it —
the narration track is derived and cannot be hand-edited (§6.7). What it changes
is how long that picture holds. If the result opens a gap, the response carries
a warning (§6.9).

### 6.6 Split a clip

```json
{"kind": "split", "clip_id": "clp_…", "at": 45.0}
```

The split point must be strictly inside the clip and both halves must clear the
minimum. The right half gets a **new clip id and keeps the visual unit** — both
halves show the same picture and both must still highlight the same script line
when clicked. The client must refetch or apply `changed_clip_ids` rather than
guessing which id is which.

### 6.7 Editing a derived track

The `caption` and `narration` tracks are generated from the script. Any
operation on them is refused:

> The caption track comes from your script. Edit the script instead — a change
> here would be lost the next time the project is re-planned.

The interface should render derived tracks with a distinct treatment (hatched
background, no drag handles) **and** still show the refusal if an edit is
attempted some other way. The visual treatment is a courtesy; the refusal is the
guarantee.

Similarly the `narration` and `visual` tracks cannot be removed — "mute it
instead" — and a locked track or locked clip refuses every mutation with a
sentence naming the lock.

### 6.8 Add a transition

```json
{"kind": "set_transition", "clip_id": "clp_…",
 "transition_in": "fade", "transition_out": "fade", "transition_seconds": 0.5}
```

A transition may not exceed **half the clip's duration** — beyond that it
consumes the whole shot. The client should cap its slider accordingly and
surface the refusal if it arrives anyway.

Transitions are drawn on the clip edges, and their duration is shown
numerically. A transition drawn as a gradient with no number is unmeasurable and
therefore uneditable by anyone who cannot see it.

### 6.9 Warnings

`warnings` in the response are non-fatal observations — most commonly:

> removing this leaves a 4.2s gap on the visual track

Show them as an inline, dismissable note attached to the region concerned, with
the gap rendered on the timeline itself. A gap is legal. It is also almost
always a mistake, and the interface should make it obvious without refusing it.

`GET /v1/projects/{id}/timeline` returns a `gaps` array per track; render them.

### 6.10 Zoom

Purely client-side. No request. Requirements:

* zoom range from whole-project-fits to roughly 40ms per pixel (below the
  minimum clip length there is nothing more to see)
* keep the **playhead** anchored while zooming, not the viewport left edge —
  zooming away from where you are looking is disorienting
* `Cmd/Ctrl` + scroll, pinch, and the `-` / `=` keys all zoom
* `Shift` + scroll pans horizontally
* the ruler's tick density adapts: minutes, then 10s, then seconds, then frames

### 6.11 Add and remove tracks

```json
{"kind": "add_track", "track_kind": "music", "track_name": "Score"}
{"kind": "remove_track", "track_id": "trk_…"}
```

There may be only one narration, visual and caption track. Music, SFX and
overlay tracks are unlimited. Music and SFX tracks are editable and **empty** —
there is no library to populate them from yet (`FRONTEND_PRODUCT_SPEC.md` §15),
so the UI should not advertise "Add music" as though a catalogue exists behind
it.

---

## 7. Playback interactions

### 7.1 What the preview actually is

`GET /v1/projects/{id}/preview?at=12.5` is a **metadata** endpoint. It returns
what is on screen at that moment and what produced it:

```json
{"at": 12.5, "duration": 184.2,
 "clip": {…}, "visual_unit": {…},
 "script_lines": [{"block_id": "blk_…", "text": "The ocean is vast."}]}
```

It does not render a frame. There is no live preview of unrendered edits
(`FRONTEND_PRODUCT_SPEC.md` §15). What the player shows is the **last rendered
output**, plus this metadata layered over it.

The interface must be honest about that. When the timeline version is ahead of
the last render, the player shows a persistent marker:

> Preview is from your last render. N edits since.

### 7.2 Play / pause

`Space` toggles. Playing plays the last rendered output. If nothing has been
rendered yet, the play control is replaced by **Render preview**, not disabled —
a disabled play button with no explanation is a dead end.

While playing, the playhead moves, the current clip is highlighted, and the
current script line is highlighted and auto-scrolled. Auto-scroll suspends for
five seconds after any manual scroll, so that reading ahead is possible.

### 7.3 Seek

Clicking the ruler, dragging the playhead, or arrow keys. Seeking is local; the
client calls the preview endpoint at most **once per 150ms of settled position**
(trailing debounce), not per frame of a drag. A scrub across a three-minute
project must not produce three hundred requests.

`,` and `.` step one frame. `Left`/`Right` step one second. `Shift` +
`Left`/`Right` steps ten. `Home`/`End` go to the ends. `[` and `]` jump to the
previous and next clip boundary — this is the keyboard equivalent of clicking
clips along the timeline and is required for keyboard parity.

### 7.4 Loop a selection

Client-side. Sets an in/out pair and loops playback between them. The same in/out
pair is what a **range render** uses (§9), so the control is shared: select a
range, then either loop it or render it.

---

## 8. Pacing and duration interactions

```
POST /v1/projects/{id}/pacing
{"pacing": "cinematic", "target_seconds": 180}
→ 200 {"pacing": {…}, "timeline": {…summary…}}
```

Synchronous. It re-plans the timeline, so it is not something to fire on every
slider tick — commit on release.

Modes: `natural`, `tight`, `cinematic`, `educational`, `fast`. (`custom` exists
in the contract but has no UI affordance yet; do not expose it.)

The response's `verdict` drives the interface, and this is where the product's
duration rules become visible:

| Verdict | Meaning | Interface |
| --- | --- | --- |
| `on_target` | within 2s of the target | quiet confirmation, no warning |
| `filled` | target longer; visual holds absorbed it | show the plan; normal state |
| `underfilled` | target longer than pacing can plausibly stretch to | warning with `shortfall_seconds`: *"We can reach 4:10 of the 5:00 you asked for. Add more script, or accept the shorter video."* |
| `overrun` | target **shorter** than the narration | **blocking decision**, see below |

**`overrun` requires a user decision and offers exactly two honest options:**

> Your narration is 4:32. You asked for 3:00. We will not speed up your voice or
> cut your words without you saying so.
>
> **Shorten the script** — suggest cuts to review · **Keep 4:32**

The system does not arbitrarily speed up speech, and it does not silently cut
content. "Shorten the script" opens a `shorten` revision (§4) — a proposal the
user reads and accepts, not a change already made. `needs_user_decision` in the
payload is `true` for exactly this case; the interface must not let export
proceed as though the target was met.

The pacing panel also shows the fill allocations: which units are holding
longer, and by how much. A user who asked for five minutes and got a plan should
be able to see where the extra time went.

---

## 9. Render and export interactions

```
POST /v1/projects/{id}/render
Idempotency-Key: <client uuid>
{"scope": "scene", "visual_unit_id": "vun_…"}
→ 202 {"job_id", "scope", "start", "end", "fraction_of_project": 0.08}
```

Four scopes:

| Scope | Body | Offered when |
| --- | --- | --- |
| `clip` | `clip_id` | a clip is selected |
| `scene` | `visual_unit_id` | a visual is selected |
| `range` | `start`, `end` | an in/out range is set (§7.4) |
| `full_project` | — | always |

Scoped regions are **expanded by one second either side** by the backend so that
a transition spanning the boundary renders whole. The response's `start`/`end`
are the expanded values; the client should draw the rendered region using those,
not the values it sent, or the highlighted region will be wrong at the edges.

**`fraction_of_project` is a description of the region, not a promise of a
saving.** Scoped rendering does not currently cost less than a full render
(`FRONTEND_PRODUCT_SPEC.md` §15). The interface must not display it as "8% of
the cost" or "12× faster". Show it as coverage: "Rendering 0:42–0:58".

Render is rate-limited more heavily than other operations. A `429` should be
shown as *"Too many renders just now — try again in a moment"*, with the retry
delay if the response supplies one.

### Export warnings

Before export the client must surface, prominently:

* any unit in `failed` — "N visuals could not be generated and will appear as
  their previous version or as a title card"
* any unit in `degraded` — what it fell back to
* `diverged_from_recording` — the audio says something different from the script
* `overrun` — the video is longer than the target the user set

These are not blockers. They are disclosures. Exporting a degraded video is
fine; exporting one while presenting it as complete is not.

---

## 10. Optimistic concurrency and conflict

Every timeline mutation should send `expected_version`. When it does not match:

```
403 {"error": {"code": "schema_invalid", "retryable": false,
     "message": "Someone else changed this timeline. Reload and try again."}}
```

(The backend raises a `PolicyViolation` carrying `SCHEMA_INVALID`, which maps to
403 through its policy category; see §14.)

Client behaviour:

1. Do **not** silently reload and re-apply. The user's edit was computed against
   a timeline that no longer exists; re-applying it can move the wrong clip.
2. Show the message with a **Reload** action.
3. On reload, refetch the timeline, restore the user's *selection* (by clip id),
   and discard the pending operation.

The script has a version too. A revision proposal carries `based_on_version`;
see §4.4.

---

## 11. Undo

The backend has no undo endpoint. Undo is a **client-side inverse-operation
stack**, and this is what makes the batching rule in §6 matter: one `PATCH` is
one undo step.

For each operation the client pushes its inverse before sending:

| Operation | Inverse |
| --- | --- |
| `move` | `move` back to the previous `start` |
| `trim` / `extend` | `trim` to the previous `start`/`end` |
| `insert` | `remove` the returned clip id |
| `remove` | `insert` with the removed clip's full description |
| `split` | `remove` the right half, `trim` the left back to the original end |
| `replace_source` | `replace_source` with the previous source |
| `set_transition` / `set_gain` | the same with previous values |
| `lock` / `unlock` | the opposite |
| `add_track` / `remove_track` | the opposite |

Undo applies the inverse through the same endpoint with the **current** version,
so an undo can itself conflict (§10) — which is correct, and better than an undo
that quietly overwrites someone else's work.

Script edits, revision accept/reject, approve, lock and version selection are
**not** on the timeline undo stack. They have their own affordances (retype the
line, accept the opposite, toggle the control) and mixing them into one stack
produces an undo whose behaviour nobody can predict.

---

## 12. Keyboard shortcuts

Every action below is also reachable through a visible control. These are
accelerators, not the only path.

### Global

| Key | Action |
| --- | --- |
| `Space` | play / pause |
| `Cmd/Ctrl+Z` | undo (timeline stack, §11) |
| `Cmd/Ctrl+Shift+Z` | redo |
| `Cmd/Ctrl+S` | no-op — everything is already saved; show "Saved automatically" |
| `Cmd/Ctrl+K` | command palette (every action in this document, searchable) |
| `?` | shortcut reference |
| `Esc` | cancel the current edit / close the dialogue |
| `Tab` / `Shift+Tab` | move between panels in a documented order: script → preview → inspector → timeline |

### Script panel

| Key | Action |
| --- | --- |
| `Up` / `Down` | previous / next line |
| `Enter` | edit the focused line |
| `Cmd/Ctrl+Enter` | commit the edit |
| `Esc` | cancel the edit |
| `Cmd/Ctrl+R` | request a revision for the selection |
| `Cmd/Ctrl+Shift+P` | re-plan visuals |

### Visual / inspector

| Key | Action |
| --- | --- |
| `A` | approve the selected visual |
| `L` | lock / unlock the selected visual |
| `G` | regenerate with `same_idea` |
| `Shift+G` | open the regenerate intent menu |
| `V` | open the version list |
| `1`…`9` | select version N |

### Timeline

| Key | Action |
| --- | --- |
| `Left` / `Right` | seek ∓1s |
| `Shift+Left` / `Shift+Right` | seek ∓10s |
| `,` / `.` | step one frame |
| `[` / `]` | previous / next clip boundary |
| `Home` / `End` | start / end |
| `I` / `O` | set in / out point |
| `S` | split at the playhead |
| `Delete` / `Backspace` | remove the selected clip |
| `-` / `=` | zoom out / in |
| `Shift+Z` | zoom to fit |
| `Alt` (held) | suppress snapping |
| `Cmd/Ctrl+Shift+R` | render the selected scope |

Shortcuts must not fire while a text field has focus. `L` inside a line being
edited types the letter L.

---

## 13. Loading, empty, error and degraded states

Every panel needs all four. Specified here so they are designed rather than
improvised.

### Script panel

| State | Treatment |
| --- | --- |
| **Empty** — no script | Two equal choices: *Record your voice* and *Write or paste a script*. This is the mode-A/mode-B fork and neither is the default. |
| **Loading** | Skeleton lines at plausible lengths, not a spinner. The shape of a script is recognisable and reduces perceived wait. |
| **Transcribing** | Lines appear as they are produced, with a progress note. Do not block the whole panel. |
| **Revising** | Subdued overlay note, panel stays editable (§4.1). |
| **Error** | The message, a **Retry**, and — critically — the script text still visible. Never replace content with an error. |
| **Stale timing** | Affected lines carry a marker; the bar from §3.2 offers **Re-plan**. |

### Visual units / inspector

| State | Treatment |
| --- | --- |
| **Empty** — script but no units | *"Plan visuals"* with a one-line explanation of what planning does. |
| **Loading** | Per-unit skeletons in the known unit count. |
| **Generating** | Per-unit progress; other units remain interactive. |
| **Failed unit** | The unit shows failed with its reason and a **Try again** action. Neighbouring units are unaffected and must not be greyed. |
| **Degraded unit** | Shown as delivered-but-not-as-planned, naming the fallback. Not an error colour; it worked. |
| **All units failed** | Project-level message, but each unit still individually retryable. |

### Timeline

| State | Treatment |
| --- | --- |
| **Empty** — no timeline | Ruler and empty tracks with their names. Not a blank rectangle: the track structure is information. |
| **Loading** | Ruler renders immediately; clips fade in. |
| **Conflict** | §10. |
| **Refused edit** | Clip reverts to its previous position with the refusal near it. Never leave the clip where the user dropped it while showing an error elsewhere — the two disagree and the user believes the picture. |

### Preview

| State | Treatment |
| --- | --- |
| **Nothing rendered** | *Render preview* in place of the player. |
| **Stale** | Player plus the "N edits since your last render" marker (§7.1). |
| **Rendering** | Progress from `render.progress`; the previous render stays playable. |
| **Render failed** | Reason, **Retry**, and the previous render still playable. |

---

## 14. Error code to interface mapping

The backend's error envelope is `{"error": {"code", "message", "retryable"}}`.
`message` is written for a human and should be shown verbatim; the client should
not paraphrase it.

Status is chosen by code first, then by the error's category. Six codes have an
explicit status because the distinction matters to a client: 401 means "get a
credential", 403 "that credential is not enough", 402 "pay", 429 "wait".

| Code | HTTP | Retryable | Interface |
| --- | --- | --- | --- |
| `schema_invalid` (validation) | 400 | no | Inline, next to the control. Revert optimistic state. |
| `schema_invalid` (policy refusal) | 403 | no | A lock, an overlap, a derived-track edit or a version conflict. Show the sentence; offer the named remedy. |
| `not_authenticated` | 401 | no | Re-authenticate, preserving unsaved local state. |
| `permission_denied` | 403 | no | Inline with the offered remedy (e.g. **Unlock**). Never auto-remediate. |
| `quota_exceeded` | 402 | no | Name the limit and the way out. Not a retry. |
| `tenant_suspended` | 403 | no | Account-level; take the user out of the editor. |
| `rate_limited` | 429 | yes | "Try again in a moment", using the `Retry-After` header. |
| `not_found` / `asset_not_found` | 404 | no | Refetch the parent rather than showing a raw error. |
| `asset_license_unacceptable` / `budget_exceeded` | 403 | no | Surface at the unit, not as a modal. |
| `generation_refused` | 422 | no | The provider declined this content. Show the reason; offer a different intent. Not a bug and not a retry. |
| `provider_unavailable` | 502 | yes | Offer a retry; do not lose the user's request. |
| `latency_exceeded` | 504 | no | "That took too long, so we moved on." Show what was delivered instead. |
| `render_failed` / `internal_error` | 500 | no | Apology, a correlation id the user can quote, a retry. Never a stack trace. |

Note the two rows for `schema_invalid`: a malformed request and a refused-but-
well-formed edit share a code and differ by status. Branch on the status, and
show the `message` either way — every policy refusal in the editor carries a
written sentence naming its remedy.

`retryable: false` means the retry button must not be shown. A retry button on a
non-retryable refusal is an invitation to click something that will fail again.

The optimistic-concurrency refusal (§10) arrives as a 403 `schema_invalid` with
a message about reloading. Treat any refused timeline mutation that carried an
`expected_version` as a potential conflict and offer **Reload**, rather than
rendering a generic "invalid request".

---

## 15. What the client must never do

A checklist, because each of these breaks a guarantee the backend spent real
complexity to provide.

1. **Never apply a revision locally before the user accepts it.** The proposal
   is a document. `accept` is the only thing that changes the script.
2. **Never derive the script ↔ unit ↔ clip mapping.** Use `links`.
3. **Never send `force: true`.** It is stripped anyway; sending it means the
   client believes it can override a lock.
4. **Never auto-unlock to satisfy a refused operation.**
5. **Never blank a visual during regeneration.** The old version is still what
   would be exported.
6. **Never present a scoped render as cheaper or faster.** It is not, yet.
7. **Never hide a degradation, a failure, or a script/voice divergence at
   export.**
8. **Never surface internal cost or margin.** `cost_usd` on a version is
   customer-facing; nothing else is.
9. **Never offer a free-text instruction to the director or the reviser.**
   Both take closed enums, on purpose — they reach a model that decides what
   appears in someone's video.
10. **Never auto-re-plan after a script edit.** Mark stale, offer the action.
11. **Never make anything drag-only.**
12. **Never show colour as the only signal** for locked, degraded, failed or
    stale.
13. **Never retry a non-idempotent operation without reusing the original
    `Idempotency-Key`.** That is how a user is charged twice for one
    regeneration.

---

## Sources

* `src/vtv/api/product.py` — every endpoint, payload and refusal quoted here
* `src/vtv/pipeline/editing.py` — the thirteen operations and their rules
* `src/vtv/pipeline/regeneration.py` — lock enforcement, version selection
* `src/vtv/pipeline/revision.py` — propose / accept / reject
* `src/vtv/contracts/{script,visual_unit,tracks,pacing,render_scope}.py`
* `tests/test_product_scenarios.py` — scenarios A–F, driven through the real app
* `docs/FRONTEND_PRODUCT_SPEC.md` — the screens these interactions live in
* `docs/TIMELINE_UI_SPEC.md` — the timeline's rendering and layout rules
