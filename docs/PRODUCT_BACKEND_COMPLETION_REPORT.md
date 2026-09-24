# Voice-to-Video — product backend completion report

**Date:** 18 August 2026
**Scope:** the product/platform phase — script mode, the visual director, the
editable timeline, pacing and target duration, partial rendering, and the
frontend handoff specification.
**Frontend:** deliberately not implemented. Four specification documents were
produced instead, as required.

A note on what this document is. It is written to be **checkable**, not
persuasive. Every claim is either backed by a test named in the text, or
labelled as not proven. Where something does not exist, it is named in §12
rather than left for someone to discover.

---

## Contents

1. [Verification status](#1-verification-status)
2. [What was built](#2-what-was-built)
3. [The two creation modes](#3-the-two-creation-modes)
4. [The script as a document](#4-the-script-as-a-document)
5. [Visual units](#5-visual-units)
6. [The editable timeline](#6-the-editable-timeline)
7. [Pacing and target duration](#7-pacing-and-target-duration)
8. [Partial rendering](#8-partial-rendering)
9. [API surface](#9-api-surface)
10. [Security and tenancy](#10-security-and-tenancy)
11. [The self-audit, and what it found](#11-the-self-audit-and-what-it-found)
12. [What does not exist](#12-what-does-not-exist)
13. [Known limitations that are design decisions](#13-known-limitations-that-are-design-decisions)
14. [Frontend handoff](#14-frontend-handoff)
15. [What to do next](#15-what-to-do-next)

---

## 1. Verification status

Every gate, run on the final state of the repository:

```
ruff check .                             All checks passed!
mypy                                     Success: no issues found in 135 source files
python -m vtv.schema_export --check      schemas are current (17 documents)
python -m unittest discover -s tests -q  Ran 885 tests ... OK
python -m vtv.evaluation.harness         cases: 10  passed: True
```

Scale: 170 Python files, ~38 900 lines of source, ~14 100 lines of tests.

**What these gates do and do not prove.** They prove the code type-checks,
lints, matches its published schemas, and satisfies 885 assertions including
six end-to-end product scenarios driven through the real HTTP application with
a real repository, a real durable queue and a real worker. They do **not**
prove behaviour against a real AI provider (no credentials are configured), a
real PostgreSQL server (none available in this environment), or a real ffmpeg
render of a filled timeline. Those are named individually in §12.

---

## 2. What was built

The phase added a **product layer** on top of the existing deterministic
pipeline. Nothing was restarted, rewritten or discarded: the contracts, ports,
adapters, queue, storage, security, grounding, Visual Bible, renderer,
ingestion and evaluation systems are the ones that were there, and the new work
sits on them.

New contracts:

| Module | What it models |
| --- | --- |
| `contracts/script.py` | `Script`, `ScriptBlock`, `RevisionProposal`, `TextChange`, `ScriptVersion` |
| `contracts/visual_unit.py` | `VisualUnit`, `VisualVersion`, ten statuses, ten regeneration intents |
| `contracts/tracks.py` | `EditTimeline`, `Track`, `TimelineClip`, ten track kinds |
| `contracts/pacing.py` | `PacingProfile`, `PacingPlan`, `FillAllocation`, four verdicts |
| `contracts/render_scope.py` | `RenderScope`, `RenderRegion` with boundary expansion |

New services:

| Module | Responsibility |
| --- | --- |
| `pipeline/scripting.py` | Build and maintain the script; sentence splitting |
| `pipeline/units.py` | Group blocks into units; build and flatten the timeline |
| `pipeline/editing.py` | Thirteen validated timeline operations |
| `pipeline/revision.py` | Propose / accept / reject a script revision |
| `pipeline/regeneration.py` | Regenerate one visual, under lock and gate |
| `pipeline/pacing.py` | Plan duration; allocate fill |
| `api/product.py` | Fifteen HTTP routes |
| `product_jobs.py` | Three job handlers |

---

## 3. The two creation modes

**Mode A — automatic.** The user speaks; the transcript becomes the script; the
director plans visuals. Unchanged from the previous phase, except that its
output is now a `Script` document rather than an internal transcript, which is
what makes the next sentence possible.

**Mode B — script / director.** The user writes or pastes text. That text is
the narration, verbatim.

**Both converge on one model.** There is one `Script`, one set of
`VisualUnit`s, one `EditTimeline`, one render path. Mode A is not a separate
product with a separate pipeline; it is the same pipeline entered from a
different door. A project created in mode A is fully directable afterwards —
tested by `ScenarioAAutomaticBecomesDirectable`.

**The system does not invent narration in mode B.** `ScriptService.from_text`
splits and estimates; it does not rewrite. Every AI text change is a
`RevisionProposal` — a stored document with an explicit `ACCEPTED` transition —
and `RevisionService.accept` is the only code path in the system that writes
`Script.current_text`. `source_text` is `frozen=True`, so "show me what I
actually wrote" is answerable after any number of accepted revisions.

Tested by `ScenarioBScriptIsTheSourceOfTruth` and
`TheScriptDocumentAgreesWithItself`.

---

## 4. The script as a document

* `source_text` — immutable, frozen at the contract level.
* `current_text` — what the video narrates.
* `blocks` — addressable lines, each carrying its offsets back into
  `source_text`, its estimated and measured timing, its visual unit, and a
  `timing_invalidated` flag.
* `version` + `history` — every change is a version, capped at 200 with the
  oldest dropped rather than the newest refused.

Editing a line marks its visual's timing stale rather than silently
recomputing: a measured timing is a fact and replacing it with an estimate
would be a downgrade presented as an update. Editing a line whose timing was
measured from a recording sets `diverged_from_recording`, which forces an
explicit product decision instead of shipping audio that says something other
than the captions.

---

## 5. Visual units

A `VisualUnit` is the bridge between the script and the timeline, and it is the
thing a user actually directs. It carries the script blocks it covers, its
span, its version history, its selected version, its status and its lock.

**Ten statuses**, including three that exist because the alternative is lying:
`degraded` (produced, but not as planned), `failed` (scoped to one unit) and
`timing_invalidated` (the narration under this changed).

**Per-line regeneration.** `POST /visual-units/{id}/regenerate` touches one
entry of one document. Every other unit is untouched, not re-planned, not
re-fetched, not re-charged — tested by `ScenarioCOnlyTheChosenVisualChanges`.

**Versioning.** Every attempt is kept forever, including ones the grounding
gate refused, with its own cost. A system that keeps only the newest has
discarded work the customer paid for.

**Locking.** A lock is a rule, not a hope:

* regeneration is refused, twice — at the API before a job is queued, and in
  `RegenerationService` where the rule lives
* re-planning **pins** the unit's segmentation, so a re-grouping cannot merge
  two locked shots and drop one
* every timeline operation on a locked clip is refused, with no exceptions
* an automatic repaint after regeneration skips locked clips
* removing a track that holds locked clips is refused

Tested by `ScenarioFALockIsARuleNotAHope`, `ALockedVisualSurvivesEveryRePlan`
and `LocksSurviveTheirContainer`.

**Failure isolation.** One visual failing leaves the previous version selected
and the project renderable. The guards catch `Exception`, not only `VTVError`,
because an adapter that lets a `TimeoutError` through is a bug whose cost must
be one visual rather than one project. Tested by
`ScenarioEOneBadVisualDoesNotDestroyAProject` and
`OneBadVisualDoesNotDestroyAProject`.

---

## 6. The editable timeline

`EditTimeline` is what a user manipulates; `Timeline` is what ffmpeg is told to
draw. `flatten()` is the only place that knows both.

**Ten track kinds**, six used and four reserved. Narration, visual, caption and
chapter are exclusive — two clips may not occupy one instant. Narration and
caption are **derived**: generated from the script, and every edit to them is
refused with a sentence saying to edit the script instead, because a hand edit
would be discarded at the next re-plan.

**Thirteen operations**, all through one endpoint, all atomic, all
version-checked. Each one deep-copies, mutates, then re-validates the *whole*
document — so an operation that produced an overlap or a negative span fails at
the edit rather than at the renderer. A batch is all-or-nothing and advances the
version once, which makes it one undo step.

**Optimistic concurrency.** `expected_version` turns a second editor's
overwrite into a visible conflict instead of silent data loss.

**Script ↔ unit ↔ clip linking** is materialised server-side in the timeline
response, so "click the line, seek the video" cannot be implemented two ways
that disagree.

**Preservation across a re-plan.** Clip identities are reused, and tracks the
user added — music, SFX, overlay — are carried across untouched.

---

## 7. Pacing and target duration

Five modes (`natural`, `tight`, `cinematic`, `educational`, `fast`) and four
verdicts.

The rules the product promised, and how they are kept:

| Promise | How |
| --- | --- |
| Never speed up the voice | `speaking_rate` is never a function of target, verdict or duration anywhere in `src/` |
| Never silently cut content | A target shorter than the narration returns `OVERRUN` with `allocations=[]` and a message naming shortening the script as the only route |
| Never silently truncate | Narration longer than one synthesis request is now **refused with a message**, where it used to be sliced to 20 000 characters in silence |
| A longer target is filled with visual time | `FILLED`, with the fill placed where the renderer can actually put it |

That last row carries the phase's most important correction, and it is worth
stating plainly. Interior fill — holding shot three two seconds longer —
**cannot be delivered**, because the narration is one continuous recording
placed at one offset and silence cannot be inserted into it after the fact.
Planning it anyway produced a video of the right length that drifted seconds
out of sync with the voice. `PacingPlanner._deliverable` therefore moves
interior time to the end, preserving the total so the target is still met;
`TimelineBuilder` lays the narration track contiguously; and `flatten()`
**refuses** any timeline whose narration has an interior gap, because a
subtly-out-of-sync video is worse than a job that failed loudly.

Interior pacing needs the narration rendered as several placed segments rather
than one file. That is a real feature and it is not built — §12.

Tested by `NarrationIsNeverDistorted`, `AFilledPlanCanBeRendered` and
`TheVideoNeverDriftsAwayFromTheVoice`.

---

## 8. Partial rendering

Four scopes: `clip`, `scene`, `range`, `full_project`. A scoped region is
expanded by one second either side so a transition spanning the boundary
renders whole. The render job renders from the *edited* timeline, so a user's
edits are what gets rendered.

**What is real:** the scope model, the region arithmetic, the boundary
expansion, the job plumbing, the rate-limit discount, and the response's
honest `fraction_of_project`.

**What is not:** the ffmpeg segment splice. A scoped render currently costs the
same as a full render. `product_jobs.py` says so in the code, and
`FRONTEND_PRODUCT_SPEC.md` §15 tells the frontend not to promise a saving.

---

## 9. API surface

Fifteen routes, mounted through the same `routes()` call that receives the
application's existing guard, rate limiter, audit log and ownership check.

```
POST      /v1/projects/{id}/script
GET       /v1/projects/{id}/script
PATCH     /v1/projects/{id}/script/blocks/{block_id}
POST      /v1/projects/{id}/script/revisions
GET       /v1/projects/{id}/script/revisions/{revision_id}
POST      /v1/projects/{id}/script/revisions/{revision_id}
GET       /v1/projects/{id}/visual-units
POST      /v1/projects/{id}/visual-units
PATCH     /v1/projects/{id}/visual-units/{unit_id}
POST      /v1/projects/{id}/visual-units/{unit_id}/regenerate
GET       /v1/projects/{id}/timeline
PATCH     /v1/projects/{id}/timeline
POST      /v1/projects/{id}/pacing
GET       /v1/projects/{id}/preview
POST      /v1/projects/{id}/render
```

Reads and structural edits are synchronous; anything that spends money or time
is a job returning `202 {job_id}`. The line is drawn by cost and latency, not
tidiness — an editor where dragging a clip round-trips through a queue feels
broken however correct it is.

**Idempotency.** Every expensive operation takes an `Idempotency-Key`. Keys are
namespaced by **tenant and operation**, and derived keys hash the whole
request-shaping payload. Both of those are corrections; see §11.

---

## 10. Security and tenancy

Every remediation from the previous phase remains in force, and the new routes
use the same architecture rather than a second one.

* **Authentication and authorisation.** All fifteen routes call the same
  `guard` as the rest of the API. Reads take `PROJECT_READ`; every mutating
  route takes `PROJECT_UPDATE`.
* **Tenant isolation.** Project resolution goes through `owned_project`, which
  puts the organisation into the SQL `WHERE` clause rather than into an `if`
  after it. Cross-tenant access answers 404, not 403 — the existence of another
  tenant's project is itself information.
* **Storage.** Media lives in object storage as `ObjectRef`s; no blob is
  written to the relational store. A client-supplied object reference is
  checked against the caller's tenant prefix **and** bucket before it can enter
  a timeline.
* **Input bounds.** JSON bodies are capped; scripts are capped at what the
  narration synthesiser can actually carry; timeline batches are capped at 200
  operations; client-supplied animation specs are validated at the door rather
  than at render time.
* **No free text to a model.** Revisions and regenerations take closed enums.
  A caller that could pass arbitrary instructions would have a prompt-injection
  surface where a menu was intended, reaching a model that decides what appears
  in someone's video.
* **No provider SDK in core.** Verified by import scanning across `contracts`,
  `pipeline` and `api`; enforced for `contracts` and `ports` by an allowlist
  test, and for `pipeline` by a denylist (see §13).
* **Internal economics stay internal.** Per-version `cost_usd` is
  customer-facing; margin, provider pricing and provider identity are not in
  any response.
* **Audit.** Lock, approve, version-select and revision decisions all write an
  audit record with the principal and the client IP.

---

## 11. The self-audit, and what it found

The phase ended with an adversarial audit whose brief was to **refute** the
guarantees, not confirm them. It found twenty defects. Every one is fixed and
carries a regression test in `tests/test_product_audit_regressions.py` (34
tests). They are listed here in full, because a completion report that only
lists successes is not a completion report.

### Severe — a stated guarantee was false

| # | Defect | Fix |
| --- | --- | --- |
| 1 | A re-plan could **delete a locked visual** and hand its lines to another unit's picture. Reachable by editing a line and re-planning, which the editor must do after every script change. | Locked units are **pinned in the segmentation**, so no group can merge across them; an explicit check raises rather than absorbing a loss if that ever fails |
| 2 | `replace_source` — the one operation that changes what a clip *shows* — ignored the clip lock | The `allow_locked` escape hatch is gone; there are no exceptions |
| 3 | Client-supplied idempotency keys were **not tenant-namespaced**. Two organisations sending `Idempotency-Key: retry-1` deduplicated against each other, and the second was handed the first's job id | Keys namespaced by tenant and by operation |
| 4 | A client-supplied `ObjectRef` was **not checked against the caller's tenant**, so a principal who learned another organisation's key could splice that asset into their own render | Key *and* bucket validated at the API boundary |
| 5 | A `FILLED` pacing plan was **unrenderable** — the render contract refused every clip past the end of the narration, so the target-duration feature could plan but never deliver | The render timeline models deliberate visual time explicitly; see §7 |
| 6 | Fixing #5 naively made it **worse**: fill landed between narration segments and the video played several seconds out of sync | Fill is collapsed to where a continuous recording permits it; `flatten()` refuses a timeline with an interior narration gap |
| 7 | Narration longer than 20 000 characters was **silently truncated** — the video stopped talking part way through with no error and no event | Refused with a message; the API's script limit now matches what the synthesiser can carry |

### Significant — a guarantee held by convention

| # | Defect | Fix |
| --- | --- | --- |
| 8 | Derived idempotency keys omitted fields that distinguish real attempts: two revision languages, two regeneration intents, or two render ranges collided | Keys hash the whole request-shaping payload |
| 9 | Every failure guard was `except VTVError`; a `TimeoutError` from an adapter aborted a whole batch, discarding units already regenerated and paid for | Guards catch `Exception`; `regenerate_project` guards per unit |
| 10 | A failed regeneration reset the status to `READY`, silently clearing a `TIMING_INVALIDATED` warning it had not addressed | The prior status is restored; the test is `is_usable`, not "a version exists" |
| 11 | `_retarget` repainted **locked clips** after a regeneration | Locked clips are skipped |
| 12 | Removing a track deleted the locked clips on it, and could delete a **derived** track outright | Both refused |
| 13 | A re-plan discarded every track the user had added | Non-derived tracks are carried across |
| 14 | There was **no route to read a revision proposal**, so a client could accept a change it had no way to display — the product's central promise had no transport | `GET /script/revisions/{id}` added |
| 15 | `set_block_status` re-rendered `current_text` on mute but not unmute, leaving the document disagreeing with itself, and skipped the version bump | Both paths re-render and version |
| 16 | The 200-entry history cap **raised after mutating** the script, leaving a half-applied document and a 500 on an ordinary edit | One `record_version` method; oldest dropped, newest never refused |
| 17 | `source_text` was documented as immutable and enforced by nobody | `frozen=True` |
| 18 | `run_revise_script` was the one job handler with **no tenant check** | Added |
| 19 | A malformed timeline operation escaped as a raw pydantic error, missing the API's handler and returning **500 for a client mistake** | Converted to a 400 with a readable message |
| 20 | `_json_body` had **no size limit**, so a JSON field could be used as free object storage in the relational store; and a client `spec` was stored unvalidated and crashed the render job an hour later | Body capped; specs validated at the door |

### The pattern

Worth naming, because it is the same finding as the previous phase's audit and
it will recur. **Where the rule was put on the object, it held under
adversarial probing.** `may_regenerate`, `RevisionProposal.status`,
`EditTimeline.model_validate`, the storage provider's tenant check — all
survived. **Where the rule was put in a docstring or in an `if` at one call
site, it did not.** The locked-unit contract in `plan()`, the `allow_locked`
exception, `except VTVError` as a universal boundary, `source_text`
immutability: every one of those was a guarantee-by-convention, and every one
of them was false.

The fixes move each rule to a chokepoint. `flatten()` refusing a
desynchronised timeline is the clearest example: the builder is written not to
create one and the planner is written not to ask for one, and the check exists
anyway, because the failure it prevents is invisible in the output.

---

## 12. What does not exist

Named so nobody plans around it.

| Gap | Status | Consequence |
| --- | --- | --- |
| Real image and video generation | No provider credential in this environment | Every regeneration returns typography. Adapters are real and test fixtures exist; **they have not been run against a live provider** |
| Live preview of unrendered edits | Not built | The player shows the last render; the preview endpoint returns metadata |
| Scoped rendering that is actually cheaper | Region model real, ffmpeg splice not built | A scene render costs a full render |
| Interior pacing fill | Not built | Requires the narration rendered as several placed segments; fill currently lands at the ends |
| Per-line accept of a revision | Not built | Accept is all-or-nothing |
| Voice selection and re-recording | Not built | The divergence banner has no re-record action |
| Music and SFX libraries | Tracks exist and are editable; there is nothing to put on them | Do not design a media browser |
| PostgreSQL execution | Schema, RLS policies and role DDL are written and structurally tested | **NOT PROVEN FROM STATIC INSPECTION** — no PostgreSQL server and no `asyncpg` available here |
| A real ffmpeg render of a filled timeline | The `adelay`/`apad`/`-shortest` argument shape was verified against ffmpeg 6.1.1 in isolation | The full path has not been rendered end to end in this environment |
| Natural-language semantic editing | Not built | "Make the third scene warmer" is not a supported instruction |
| 11 of the visual primitives | Not built | The animation engine covers the rest |
| Deleting a script line | Not exposed | Mute is the supported operation |
| Ripple/roll edits, keyframes, filters, nested sequences | Not in the contract | Not an editor gap to be filled later without a contract change |

---

## 13. Known limitations that are design decisions

Distinct from §12: these are things that will not be built, and why.

* **A visual clip's length is not a free parameter.** It covers its narration
  exactly, because the voice does not wait. Only the first and last shots can
  be extended.
* **`min_visual_seconds` is honoured by grouping, not by padding.** Short lines
  are merged into one shot before they reach the builder; padding a clip would
  only move the problem to the next one.
* **Derived tracks are not editable.** Not as a permission, as a consistency
  requirement — a hand edit would be discarded at the next re-plan.
* **The director and the reviser take closed enums.** No free-text prompt will
  be added; the injection surface is not worth the flexibility.
* **`force` is never honoured from the wire.** Overriding a lock is an operator
  action with an audit record.
* **The `pipeline` import guard is a denylist, not an allowlist.** `contracts`
  and `ports` have a true allowlist; `pipeline` legitimately needs pydantic and
  a growing set of internal modules, so it has a denylist of vendor SDKs — which
  means a provider SDK not on that list would pass. `api` is deliberately
  unenforced: it is where vendor code is supposed to live.
* **`Content-Length` bounds what is parsed, not what is allocated.** A chunked
  request is buffered before it is rejected. A streaming limit belongs at the
  ASGI server, not here.

---

## 14. Frontend handoff

No frontend was implemented, as instructed. Four documents were produced:

| Document | Lines | Covers |
| --- | --- | --- |
| `docs/FRONTEND_PRODUCT_SPEC.md` | 468 | Product framing, four screens, Studio layout, per-panel data, events, accessibility, mobile, what does not exist |
| `docs/EDITOR_INTERACTION_SPEC.md` | ~1000 | Every interaction from §52 — click, hover, drag, trim, split, regenerate, approve, lock, replace, play, seek, zoom, transition, duration, accept/reject — plus keyboard shortcuts, all four states per panel, the error-code mapping, undo, and a "never do this" checklist |
| `docs/TIMELINE_UI_SPEC.md` | 564 | The timeline's data, layout, ruler, clip rendering, gaps, transitions, playhead, snapping, zoom, virtualisation, live state, performance budget, ten invariants |
| `docs/DESIGN_HANDOFF.md` | 502 | Five promises the interface must keep, component and state inventories, semantic colour, typography, motion, copy and tone, accessibility acceptance criteria, real data ranges, eight open design questions, a build order |

The documents specify **no visual identity** — no palette, typeface, spacing
scale or radius. Those are the designer's. What they specify is everything that
constrains the choice: the states that must be distinguishable, the information
that must be present, and the promises the interface must keep.

---

## 15. What to do next

In order of what would most reduce risk.

1. **Run against a real provider.** Everything about generation quality, cost
   and latency is currently unverified. This is the single largest unknown.
2. **Execute the PostgreSQL schema.** The RLS policies are written and cannot
   be trusted until a server has run them.
3. **Render a filled timeline end to end.** The `adelay`/`apad` argument shape
   is verified in isolation; the full path is not.
4. **Build the Studio**, in the order in `DESIGN_HANDOFF.md` §16. Steps 1–9 of
   that sequence are a shippable product without the timeline.
5. **Segment the narration** so interior pacing becomes possible. This is the
   one architectural gap the product's own promises point at.
6. **Make scoped rendering actually cheap.** Until then, do not sell it.

---

## Sources

Everything in this document is checkable against:

* `src/vtv/api/product.py`, `src/vtv/product_jobs.py`
* `src/vtv/pipeline/{scripting,units,editing,revision,regeneration,pacing,narration}.py`
* `src/vtv/contracts/{script,visual_unit,tracks,pacing,render_scope,timeline}.py`
* `tests/test_product_scenarios.py` — scenarios A–F through the real application
* `tests/test_product_domain.py` — the arithmetic and the refusals
* `tests/test_product_audit_regressions.py` — one test per defect in §11
* `docs/FINAL_REPORT.md` — the previous phase
* `docs/AUDIT_2026-08-13.md` — the audit that started it
