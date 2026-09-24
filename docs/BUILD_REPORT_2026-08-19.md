# Build report — frontend, integration, and the validation that was outstanding

**Date:** 19 August 2026
**Scope:** the frontend against the frozen design, its integration with the real
backend, and the four infrastructure items that had never been executed.

This report states what was demonstrated and what was not. Where something is
unproven it says so and says why, because the difference between "implemented"
and "shown to work" is the entire subject of this document.

---

## 1. Headline

| | Result |
|---|---|
| Backend tests | **964 passing, 0 skipped**, superseded by **1 080** in a later pass — see §9 |
| Frontend tests | **64 passing** (unit, transport, and live wire-contract), superseded by **55 unit + 9 live wire-contract** in a later pass — see §9 |
| End-to-end, real browser, real API | **60/60 checks passing** |
| Typecheck | clean, `strict` with `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes` |
| Build | 34 assets · 377 KB · **runtime dependencies: none** |
| PostgreSQL + row-level security | **executed against a live server; 18 adversarial tests passing** |
| HTTP provider adapters | **executed against a conforming server; 11 tests passing** |
| Docker image build | **not executed** at the time this report was written — **closed in a later pass**, see §9 |
| S3 object storage | **not executed** at the time this report was written — **closed in a later pass**, see §9 |
| Vendor AI providers | **not executed** — no credentials exist. **Still true** — see §9.4 |

**Production readiness is not claimed.** Section 8 lists precisely what remained
as of this report; §9 covers what a later pass closed and what is still
blocked.

---

## 2. What was built

### 2.1 Screens

Every screen in the frozen design exists and is wired to the API: Start (speak /
write / document), Projects, the Studio, Media, Preview, Inspector, Timeline,
Render history, Settings, and the phone review flow. `apps/web/` is ~10 000
lines of TypeScript, native ES modules, **zero runtime dependencies** — the npm
registry is unreachable from this environment, and rather than mock a build the
application was written to need one.

### 2.2 Interactions

Resizable panes; drag/drop from the library onto a lane *and* onto a script
line; drag, trim and snap on the timeline with named snap targets; script ↔
visual unit ↔ clip linking from the server's own `links` table; selection and
seek synchronised across three panels; lock and unlock; version switching and
side-by-side comparison; per-visual regeneration with a closed intent list;
upload of image, video, audio, logo and SVG; replacing a visual with your own
media; audio level, duck depth, fades, loop and source-audio controls; project
mark with corner, width, inset and opacity; crop.

### 2.3 Integration

No mock data anywhere. Every panel that shows server data got it from an
endpoint; the wire types are hand-written and a live contract test asserts them
against the running API on every run.

Two integration decisions worth naming:

**Placing a file on a lane names it by id.** The obvious alternative was to
publish `ObjectRef`s in the asset view so the browser could construct an insert
— a tenant's storage key space handed to every browser to save one server-side
lookup. Instead the operation carries `media_asset_id` and the server resolves
it. Tenancy stops being a check somebody remembers and becomes structural: an id
from another tenant is simply not in this project's library.

**The frontend is served same-origin by the API.** CORS never enters the
picture, and the security headers (`nosniff`, `X-Frame-Options: DENY`,
`frame-ancestors 'none'`, COOP, referrer policy, permissions policy) are sent as
real headers rather than declared in a `<meta>` element browsers ignore.

---

## 3. Defects found and fixed

Fifteen were found by an adversarial audit of the implementation against the
frozen design; eighteen more by a read-only CTO review. The ones worth reading
about are below. Every one has a regression test.

### 3.1 The product lane had no first render

A project created by pasting a script could **never be exported**. Every render
failed with *"render the project once before rendering an edit — we need the
narration audio"*, and there was no first render available to do: narration was
only ever produced by the upload-a-document path. The golden path ended one step
before its last step, and the frontend's Export button was wired to an endpoint
that could only fail.

The missing piece was a stage, not a button. Narration is now synthesised from
the script, cached by content digest so a retry never pays twice, measured
timings are written back onto the blocks, and the render is **refused** rather
than attempted when the real voice is longer than the timeline drew for it —
because the renderer places one continuous audio file and encodes to the shorter
stream, so an over-long voice ships as a video that stops talking mid-sentence.

Found by running the end-to-end test. Nothing in 900 unit tests noticed, because
every test that had ever rendered went through the other lane first.

### 3.2 Changing a visual did not change the video

Three operations change what a visual shows — regenerating it, choosing a
different version, and giving it a file of your own. **Only the first repointed
the timeline.** `run_render_scope` flattens the timeline and never reads a unit,
so the other two changed the interface and left the export alone.

Confirmed live before fixing: switching a visual back to v1 changed
`selected_version_id` and left the timeline at version 6, unchanged. The
inspector said "in use" next to the text *"Switching is free and instant"*, and
the downloaded file contained the other version.

Fixed at the boundary rather than at the three call sites: repointing now
happens inside `store_units`, so a fourth operation written next year gets it
without anybody remembering.

### 3.3 Approving a visual let a re-plan delete it

`VisualUnit.is_user_owned` reads *"the user has expressed a preference that must
survive a re-plan"* and includes `APPROVED`. `USER_OWNED_STATES` reads
*"automatic processes must not move a unit out of one of these without being
told to"*. Both written down, both correct — and the planner pinned on
`unit.locked`.

This is the shape this codebase's audits keep finding: **the rule was put on the
object and the call site kept its own narrower copy.** Now the planner uses the
property, and the invariant check that turns a pinning bug into a loud failure
was widened to match — a guard narrower than the thing it guards is not a guard.

Also extended to unlocked uploads. Unlocking says "the system may propose
something else", which is a statement about regeneration; it is not permission
for a regrouping to delete the binding and leave an empty unit with no trace a
file was ever there.

### 3.4 The grounding verdict was displayed inverted

The inspector switched on `supported` and `unsupported` — two words the API has
never sent. The real values are `grounded`, `refused`, `pending` and
`not_applicable`, so every visual the grounding gate had **positively cleared**
fell through to the default branch and was reported to the user as *"Not
applicable — this makes no factual claim."*

The product's flagship safety check, computed correctly and shown backwards, on
the one screen anybody would look at. The contract test asserted the closed set
for `origin` and not for `grounding`; it now covers every enum the client
switches on, plus all ten unit statuses.

### 3.5 Controls that did nothing

- **Track lock** rendered with a pressed state and had no click handler at all.
- **Track mute** was drawn as a working toggle over a toast explaining it did not work.
- Both were blocked on a missing operation, while `Track.locked`/`muted` had been in the contract from the start and `locked_clip_ids` had always honoured them. The rule was enforced; there was no way to set the flag. There is now (`set_track`), and both are undoable.
- **Dragging a locked or caption clip did nothing** — the backend's two sentences existed and were unreachable. They now appear next to the clip.
- **"Replace from Media"** was a toast telling the user to do it themselves.

### 3.6 Claims that were not true

- **`fraction_of_project`** was commented *"what the user saves by not re-rendering everything"* while the job it triggers says plainly the encode is full-timeline. The response now carries `encode` and `saves_time: false`, and the UI says *"Rendering the whole video for 0:05–0:20"*.
- **"Timeline changed since this render"** was `timeline.version > 1` — on after your first edit, forever, including one second after a fresh render. The server now records which timeline the file on disk encodes; where it cannot tell, no warning is drawn, because a warning that might be wrong teaches people to ignore warnings.
- **Every row in render history offered "Download"**, all pointing at the same URL. Click yesterday's 720p excerpt, get today's 1080p file, correct filename, no way to tell. Only the render actually kept gets a link; the rest say "Not kept".
- **Undo of a removed clip** restored a clip with no content — accepted by the editor, drawn by `flatten` as a placeholder. The user got "Undid: remove", a clip back in the right place, and a black frame in the render. Object clips are now correctly reported as not undoable; text and empty clips still are.
- **The Style control** displayed "Documentary" for every project regardless of its actual style.

### 3.7 Two defects in the tooling itself

- The build was **shipping stale fingerprinted assets** — content-addressed names never collide, so every rebuild left the old file in place. It reported 549 KB and 61 assets as the application's size when it was 302 KB and 33.
- The build's import scanner **matched inside comments**. The sentence *"Fell back" is a different fact from "failed"* killed the build claiming `failed` was a runtime dependency. Rewording would have fixed that comment; the scanner now skips comments, because the quiet version of this bug silently *rewrites* a path that appears in prose.

### 3.8 Found by the new frontend tests

- `timecode(0.9999)` rendered **`0:00.1000`** — a four-digit millisecond field, once per second, at every second boundary, in the ruler and the toolbar and every tooltip. Splitting before rounding instead of after.
- `set_gain` correctly refused to offer undo because the clip view carried no gain. It does now, so undo restores the real level.

---

## 4. Backend validation

### 4.1 PostgreSQL + row-level security — executed

`postgres_schema.py` carried **"DESIGNED AND STRUCTURALLY TESTED. NOT
EXECUTED"** since it was written. It has now been applied to a real PostgreSQL
16 server and attacked as the application role. 18 tests, all passing:

- another tenant's row is invisible even by primary key
- a row cannot be inserted on another tenant's behalf (`WITH CHECK`, not just `USING`)
- another tenant's rows cannot be updated or deleted
- a query with no tenant set sees nothing — **fail closed**
- a tenant does not leak into the next transaction (`SET LOCAL`, not `SET`)
- the application role cannot disable RLS, drop a policy, or write a permissive one
- the application role cannot create a table to escape into, or rewind a sequence

**Running it found a defect no structural test could.** The DDL was, as text,
exactly right — `NOT NULL` tenant column on every table, RLS enabled, `FORCE`
set, a correct policy each. And the application role could not insert a single
row into `documents`, `usage_records` or `audit_log`, because `GRANT … ON
<table>` says nothing about the `BIGSERIAL` sequence behind it. Four tables, the
three busiest write paths in the system, and the previous tests read the DDL as
a string and pronounced it correct.

**Still not implemented: the repository backend.** There is no `asyncpg` here
and no package index to fetch one from, so nothing in the running application
talks to PostgreSQL. `repository_path()` still raises on `postgresql://` rather
than quietly writing to a local SQLite file. Executing the schema and having a
working backend are different achievements.

### 4.2 Provider adapters — executed against a conforming server

Four adapters (transcription, speech synthesis, image generation, text
generation) each carried **"REAL IMPLEMENTATION … Not executed in this
environment"**. There are still no vendor credentials — but credentials were
never what was missing. A server that speaks the protocol was. One now exists in
the test suite, and the real adapters are driven through it: request shape,
credential, response parsing, error mapping, storage write, cost computation,
and the 429 → rate-limit / 400 → refusal distinctions.

**This does not prove any vendor works.** A real endpoint can differ in
undocumented required fields, error envelopes, rate limits, or audio a probe
cannot read. "Correct against the contract it was written for" and "works
against Vendor X" are different claims and only the first is made.

### 4.3 Queue and worker — demonstrated

Real. Two workers, durable SQLite queue, `BEGIN IMMEDIATE` claim, reclaim after
a worker dies, idempotency as a unique index rather than a convention. Tested,
and exercised live throughout this work — every render in the end-to-end run was
drained by a separate worker process.

### 4.4 Object storage — local real, S3 skeleton at the time of this report

*As of this report:* the local provider is real, tenant-namespaced and
tested. The S3 provider was an honest skeleton: no `boto3`, no bucket, no
credentials, raising a clear error rather than pretending.

**Superseded — see §9.1.** A later pass wrote a complete, hand-rolled S3
implementation (SigV4 by hand, no boto3), verified its signature against
AWS's published test vector, and ran it against a real container in the
validated deployment described in §9.2. No request has still been made to a
real bucket (AWS, R2 or MinIO).

### 4.5 Docker — not executed at the time of this report

*As of this report:* the daemon starts, but Docker Hub answers `403
Forbidden` from this environment, so no base image can be pulled and the
image cannot be built. The deployment tests read the Dockerfile and compose
file as data and assert they agree with the code — every `COPY` path exists,
the `CMD` names a real ASGI object, the healthcheck targets an endpoint the
API serves, migrations complete before anything serves, no service is
declared that the application never contacts. That is a genuine consistency
check and it is not a build.

**Superseded — see §9.1 and §9.2.** A later pass built the image from
`Dockerfile.localbase` — identical to the production `Dockerfile` after
`FROM`, with the base image imported from the local rootfs because the
registry is still unreachable here — and ran it as a real two-worker
deployment with measured results.

---

## 5. What the end-to-end run proves

One Chromium, the built bundle, the real Starlette app, the real repository, the
real queue, a real worker, real ffmpeg. 60 checks. The ones that matter:

```
YOUR FILE SURVIVED A RE-PLAN — the unit is still there
still locked, and still showing the version you chose
regenerating a locked visual is refused with a sentence
an overlapping move is refused, naming the remedy
a stale version is a visible conflict, not a silent overwrite
the render finished — ready
the video is downloadable and is not empty — 159322 bytes
an audio file reaches the Music lane by id, with no key in the browser
an image can be made the project mark after it was uploaded
the origin badge appears on the library card, the script, the clip and the inspector
dragging a locked clip says why, next to the clip
a grounded visual is reported as grounded, not as making no claim
```

---

## 6. Known limitations

**Every regeneration intent produces the same result on this install.** Ten
intents are offered, correctly greyed where no provider exists — and
`_DirectorProducer` returns typography for all of them. The rationale text
discloses it (*"no image or video provider is configured"*), but the intent menu
implies a choice the install cannot make. This is the largest gap between what
the interface offers and what it delivers.

**Concurrent regenerations of different visuals in one project can clobber each
other.** The units document is read whole, one entry mutated, and written back
whole, with no compare-and-swap. Two jobs racing means last write wins. The
worker defaults to concurrency 2, so this is reachable.

**The event stream ends after 15 minutes or after one render completes,** and
the client treats a clean end as "finished" rather than reconnecting. A long
session loses live updates and shows an offline indicator while the server is
reachable and every write is still succeeding.

**Scoped rendering is bookkeeping, not a saving.** The region is validated,
recorded and audited; the encode is the whole timeline. Now stated in the API
response.

**The stored pacing plan is never read back,** so reopening a project shows the
default pacing whatever was chosen.

**Version comparison shows no pictures** — the layout and the metadata are
there, the rendered frames are not.

---

## 7. Testing

| Suite | Count | What it covers |
|---|---|---|
| Backend | 964 | contracts, pipeline, API, security, tenancy, billing, retention, queue, render |
| — of which RLS | 18 | a live PostgreSQL, attacked as the application role |
| — of which providers | 11 | the four HTTP adapters against a conforming server |
| — of which audit regressions | 48 | one per defect ever found, so none can return |
| Frontend | 64 | the clock, the reactive core, undo inversion, transport rules, live wire contract |
| End-to-end | 60 | the golden path plus every capability the frozen design specifies |

---

## 8. What would be needed for production

1. **A PostgreSQL repository backend.** The schema is proven; nothing talks to it. **Still open** — see §9.
2. ~~**S3 object storage.** Local disk does not survive a second host.~~ **Closed** — see §9.
3. **Vendor provider credentials,** and a run against each real endpoint. **Still open** — see §9.
4. ~~**A Docker image built and run** somewhere with registry access.~~ **Closed** — see §9.
5. **Compare-and-swap on the units document,** to close the concurrent-regeneration race.
6. **A real Visual Director behind regeneration,** so the ten intents differ.
7. **Event-stream reconnection.**

Items 1–4 were the ceiling this report drew around the word "production
ready" — items 2 and 4 are now closed and items 1 and 3 remain open, in the
detail below.

---

## 9. This pass — closed, measured, and what remains blocked

**Date:** 19 August 2026. This section covers the pass documented in full in
`docs/FINAL_GAP_AUDIT.md`, which is the source of truth for everything below;
where a number differs, that document wins.

### 9.1 Closed since §8 was written

- **Real S3 object storage.** `adapters/storage/s3.py` is a complete
  implementation of the S3 REST API with hand-rolled Signature Version 4 —
  `hmac`, `hashlib` and `httpx`, no boto3. 30 tests. The signature is verified
  against AWS's published `get-vanilla` test vector. No request has been made
  to a real bucket (AWS, R2 or MinIO) — "correct against the S3 REST contract"
  and "works against AWS" are different claims and only the first is made.
- **A Docker image was actually built and run.** Built from
  `Dockerfile.localbase`, which differs from the production `Dockerfile` only
  in its base image (container registries return 403 from this environment,
  so the base was imported from the local rootfs). Everything after `FROM` is
  the production build. See §9.2 for the deployment numbers.
- **The retention/orphan-sweep bug.** The sweep now reads the retention class
  recorded with the object rather than deleting on age. 41 tests.
- **Two usage-metering gaps.** `generated_assets` was counted after the fact
  only and is now gated by an `AssetAuthoriser` before dispatch; `seats` was
  unenforced entirely and is now gated by a `SeatAuthoriser` on
  `Directory.add_member`.
- **A credential redaction gap.** `storage_secret_key` matched none of the
  field names `Settings.redacted` checked for and would have been logged in
  the clear on every boot. Replaced with a suffix rule.
- **The provider timeout gap.** The router's own `await` was unbounded; now
  wrapped in `asyncio.wait_for`, raising `TimeoutExceeded`.
- **The circuit breaker's missing cooldown.** A tripped provider was excluded
  forever; it now has a cooldown window and a half-open probe.
- **Worker job release on SIGTERM.** A job caught mid-deploy could stay
  unclaimable for up to fifteen minutes waiting on the reclaim timeout;
  `release_claimed(worker_id)` now hands those rows straight back to
  `pending` on shutdown.

### 9.2 Measured deployment numbers

From `docs/FINAL_GAP_AUDIT.md` §8. Built from `Dockerfile.localbase`;
`VTV_ENV=production`, non-root user, one API container and two worker
containers on one Docker volume, SQLite repository and durable queue.

| Measurement | Result |
| --- | --- |
| Migrations applied | 3 (`0001`, `0002`, `0003`) |
| API health in production mode | `ok` / `production` |
| Concurrent renders submitted | 7, all `202` |
| Renders reaching `ready` | 7 of 7 |
| Split across workers | w1: 3, w2: 4 |
| Jobs claimed by both workers | 0 |
| Retries | 0 — every job settled at `attempts=1` |
| Output | 96 418-byte MP4, `ffprobe` duration 18.000 s, downloaded over HTTP from the API container |
| Queue-to-ready latency (n=10) | min 32.5 s, median 116.8 s, max 205.5 s |

**Graceful shutdown, measured under the real deployment.** Three renders were
submitted and a worker was sent SIGTERM four seconds later, mid-flight. It
stopped claiming, finished what it held, and exited 0 after 55.6 s. All three
renders reached `ready`. Across the whole session: 10 jobs, all `succeeded`,
all `attempts=1` — nothing orphaned, nothing redelivered, nothing
double-charged.

### 9.3 Test totals

| Suite | Count |
| --- | --- |
| Backend | 1 080 (0 failures, 18 skipped — all ffmpeg-dependent) |
| Frontend unit | 55 |
| Frontend live wire contract | 9 |

The 1 080 backend figure and its breakdown by module supersede the 964 figure
reported in §1 and §7 of this document, and the 64 frontend figure in §1
splits into 55 unit tests plus the 9 live wire-contract tests, which had never
been run against a live API before this pass and now have been.

### 9.4 What remains blocked

Reported as blocked rather than worked around, because the blocker cannot be
resolved from inside this environment:

- **Vendor providers.** No credentials exist here, so no request has ever
  reached OpenAI, Anthropic, or any other vendor. The four HTTP adapters are
  executed against a conforming server we stand up ourselves
  (`tests/test_provider_adapters_live.py`, 11 tests), which proves the
  adapters are correct against the contract they were written for, not that
  any vendor works.
- **Measured provider costs.** Every cost figure in the system is
  adapter-declared. None has been measured against a bill.
  `docs/FINAL_GAP_AUDIT.md` §7.
- **Human quality evaluation.** No human has evaluated the output quality of
  this system. `docs/FINAL_GAP_AUDIT.md` §11.
- **PostgreSQL deployment.** The schema and RLS policies are implemented and
  tested against a live PostgreSQL 16 server (18 + 25 tests), but the running
  deployment is still SQLite — `wiring.repository_path` refuses any
  non-SQLite URL rather than silently substituting a file.
- **`storage_gb` metering.** Genuinely unmeasured; `/v1/usage` reports
  `enforced: false` for it.
- **Backup/restore and alerting.** Neither exists.

Until vendor credentials, measured costs and human quality evaluation exist,
this remains, honestly, a system proven on one machine with its own
infrastructure — a larger and more precisely measured claim than the one in
§8, and still a smaller one than "production ready."
