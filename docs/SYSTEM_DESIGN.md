# Voice to Video — system design

Every number, name and ordering in this document was read out of the source. Where
something is not implemented, it says so.

---

## 1. Tech stack

### Backend

| Layer | Choice | Why this and not the obvious alternative |
|---|---|---|
| Language | Python 3.11 | The media and ML ecosystem is here. Nothing else was close. |
| HTTP | **Starlette / ASGI**, `uvicorn` | Not FastAPI. FastAPI's value is generating request models from type hints, and every request shape here is already a Pydantic contract with `extra="forbid"`. Starlette is FastAPI minus the layer we do not use. |
| Contracts | **Pydantic v2**, `extra="forbid"`, `validate_assignment=True` | A field nobody declared is a typo, not a feature. Assignment validation means an invalid object cannot exist even briefly. |
| Persistence | **SQLite** today; **PostgreSQL + RLS** schema written and proven | Two tables, JSON payloads. See §7. |
| Queue | **Durable SQLite queue**, `BEGIN IMMEDIATE` claim | Not Redis or Celery. A job queue whose state disappears on restart is not durable, and Redis-as-a-queue means two systems of record. One file, one atomic claim, reclaim-on-death. |
| Object storage | Local disk provider (real); **S3 adapter is a complete, hand-rolled implementation** — no boto3 | Behind a port, so the choice of backend is one wiring line. See §7. |
| Media | **ffmpeg** + **Pillow** | The renderer is a single ffmpeg pass with frames piped on stdin. |
| Identifiers | Prefixed Crockford base32 (`prj_`, `vun_`, `clp_`) | Survives being read aloud, copied out of a log, or typed into a support ticket. No I, L, O or U. |
| Tests | stdlib `unittest` | No pytest dependency. 1,080 tests. See §10. |

**Zero web framework magic.** No ORM, no dependency-injection container, no
metaclass registry. `wiring.py` is one function that constructs everything
explicitly, so "what talks to what" is answerable by reading 400 lines.

### Frontend

| Layer | Choice | Why |
|---|---|---|
| Language | **TypeScript**, `strict` + `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes` | The strictest settings that exist. |
| Framework | **None.** Native ES modules. | The npm registry is unreachable from the build environment; React 19 ships no browser ESM build. Rather than fake it, the app was written to need nothing. **Runtime dependencies: none.** |
| Reactivity | ~240 lines of signals / derived / effects | Synchronous notification. A scheduler batches better and makes "did my change land" unanswerable inside a drag handler. |
| Build | 200-line script: `tsc` → content-hash → rewrite imports | No bundler. 34 native modules over HTTP/2 is a handful of multiplexed requests against a cache that survives a partial deploy. |
| Styling | Plain CSS, design tokens | Five stylesheets, 3,247 lines. |
| Tests | `node --test` + Playwright | 64 unit/contract tests, 60 end-to-end browser checks. |

**Total shipped: 34 assets, 383 KB.** For comparison, an empty
create-react-app is larger.

---

## 2. Topology

```
Browser ──HTTPS──▶ API ──enqueue──▶ Queue ──claim──▶ Workers ──▶ ffmpeg
                    │                 │                 │
                    └──────── shared state ─────────────┘
                    repository · object storage · usage meter · audit
```

**The API never renders and never calls a model.** It validates, authorises,
enqueues, and serves. That single rule is why a slow provider is a slow *job*
rather than a slow *website*, and why you can run two API replicas and four
workers without either knowing the other exists.

State is shared, not owned: rate-limit buckets, the queue, and usage
reservations all live in files every replica reads. N replicas with N private
rate-limit buckets enforce N times the limit — that was a real defect, and it is
why this is stated as a rule rather than assumed.

---

## 3. What happens when you speak a sentence

Say you record: *"The transistor was invented at Bell Labs in 1947, and within a
decade the vacuum tube was a museum piece."*

### Stage 1 — Capture

The bytes are stored and **probed with `ffprobe`**. The duration is measured,
never taken from the upload. A claimed duration is not a duration, and every
timing downstream is built on this number.

Retention: `EPHEMERAL`. Your raw recording is not kept forever.

### Stage 2 — Transcription

The only stage that turns sound into words. Words plus per-segment timings.
Nothing is invented: a transcript with made-up timings would corrupt every stage
after it, so a provider response that will not parse cleanly is dropped rather
than coerced.

If no transcription provider is configured, `/health` says so **before you press
record**, and the Start screen shows the caveat.

### Stage 3 — Understanding

Extracts **entities** (`Bell Labs` → organisation, `transistor` → product,
`1947` → date), **quantities** (`a decade` → 10 years), and **relations**
(invented-at, superseded-by).

This stage is what makes everything later possible. A chart can only plot a
number you actually said — and this is where "what did you actually say" becomes
structured data the grounding gate can check against.

### Stage 4 — Scene planning

Groups the transcript into **scenes: one idea, not one sentence.** Your sentence
is two ideas — an invention, and a consequence — so it becomes two scenes. The
eye does not want a cut per sentence.

Pauses in your speech are a strong signal here. That is why the recorder tells
you *"pauses are where the system finds your scenes."*

### Stage 5 — Visual direction — **the whole product**

For each scene the director asks one question: *what is this sentence trying to
show?* Then it answers in a fixed order. This is an if-chain, evaluated top to
bottom:

| # | If the scene… | It draws | Confidence |
|---|---|---|---|
| 0 | has matched table data with ≥2 points | a **chart** | 0.95 |
| 1 | shows a quantity or a change over time, with ≥2 numbers | a **chart** | 0.92 |
| 2 | shows change over time with datable entities | a **timeline** | 0.88 |
| 3 | shows a contrast that splits in two | a **comparison** | 0.90 |
| 4 | shows structure, process or cause-and-effect with ≥2 entities | a **network** | 0.85 |
| 5 | shows a place the gazetteer can locate | a **map** | 0.87 |
| 6 | shows a nameable entity — person, org, product, location, work, event | **searches for a real photograph** | 0.80 |
| 7 | illustrates something abstract **and** importance ≥ 0.6 | **generates an image** | 0.70 |
| 8 | anything else | **typography** — your own words | 0.75 |

For our sentence: scene one names `Bell Labs`, a real organisation → **rule 6, a
photograph from the commons.** Scene two has a date and a change → **rule 2, a
drawn timeline.**

Neither called an image generator. That is the normal case, not a lucky one.

### The gate — grounding (mandatory, not a stage)

Sits between direction and composition. `Pipeline` cannot be *constructed*
without one — the field has no default — and it is called unconditionally, with
no flag.

It walks every rung of every scene's ladder and checks any **drawn** visual
against the evidence assembled from your words, the understanding layer's
quantities, and any source tables:

| Primitive | Checked |
|---|---|
| Chart | **every plotted value** must trace to a number in evidence — one invented point refuses the whole chart |
| Timeline | each event's year must be in evidence; labels checked at 0.6 similarity |
| Map | every marker label at 0.5 |
| Typography | numbers inside the headline must trace |
| Comparison | side titles at 0.5; figures inside points must trace |
| Network | **not checked** — it asserts structure, not values |

Photographs and generated images are **not** checked, deliberately: only drawn
visuals make precise claims.

A refused visual is dropped from the ladder, recorded as a degradation with
reason `SAFETY_REFUSED` — explicitly not a provider failure — and the next rung
runs. If every rung is refused, the scene falls back to **typography of your own
words**, which cannot misstate you because it only repeats you.

This is the answer to the question every AI video tool dodges: *what stops it
putting a number on screen that I never said?* Nothing generative. A gate that
cannot be bypassed, and a fallback that is your own sentence.

### Stage 6 — Asset resolution

For scene one, the search runs against **Openverse** (`api.openverse.org/v1`)
and Wikimedia Commons.

Licence filtering happens **twice**: once server-side via
`license=cc0,pdm,by,by-sa` and `license_type=commercial,modification`, and again
locally against an explicit SPDX table. Anything not in that table is
`Permission.UNKNOWN` and **unusable** — the default is refusal, not permission.
Minimum 1280×720. Attribution is carried through to the credits and burned into
the video.

The fetch itself is SSRF-guarded and capped at 40 MB.

Retention: `PROJECT`.

### Stage 7 — Composition

Realises each shot, descending the ladder when a rung fails:

```
generated video → generated image → licensed photograph → typography
```

A failure at any rung is recorded on that visual and the next rung runs. **One
bad shot never fails the project** — that is the difference between a tool you
can ship with and a tool that throws away four minutes of work because shot
seven timed out.

### Stage 8 — Rendering

One ffmpeg process, single pass, frames piped on stdin:

```
ffmpeg -f rawvideo -pix_fmt rgb24 -s WxH -r fps -i -   \
       -i narration                                     \
       -c:v libx264 -preset veryfast -crf {18|23|30}    \
       -af "adelay={ms}:all=1,apad" -c:a aac -shortest  \
       render.mp4
```

`apad` before `-shortest` is deliberate: without the pad, `-shortest` truncates
at the end of the *audio* and throws away deliberate visual time after the voice
stops.

Outputs: **MP4** (`EPHEMERAL`) and **WebVTT captions** (`PROJECT`). Captions are
written whether or not burn-in is on — accessibility must not depend on a render
flag.

### Then the Studio

Everything above ran once, unattended. From here it stops being a pipeline and
becomes an editor:

- **Regenerate one visual** — the other forty are not re-planned, not re-fetched, not re-charged.
- **Lock it** — a lock is a rule, not a hope. Locked units are *pinned in the segmentation*, so a re-plan physically cannot merge them away.
- **Replace it with your own file** — which locks it by default.
- **Switch versions** — free and instant, and it repoints the clip ffmpeg actually reads.

---

## 4. "We don't completely use AI, right?"

**Correct, and it is the central design decision.**

Of the eight director rules, **six draw the picture ourselves** with no model and
no network. One searches real photographs. One generates.

| Source | When | Cost | Verifiable? |
|---|---|---|---|
| **Drawn in-process** — chart, timeline, network, comparison, map, typography | you gave us data | **$0.00** | yes — the grounding gate checks every value |
| **Real photograph** — Openverse, Wikimedia | you named a real thing | **$0.00** | yes — SPDX licence, creator, attribution |
| **Generated image** | abstract idea, importance ≥ 0.6 | $0.04 | no — labelled AI-generated everywhere it appears |
| **Generated video** | motion carries the meaning; off by default | $0.25/s | no — same labelling |

The animation engine draws exactly six primitives from a **validated Pydantic
discriminated union**. No model-authored code is ever executed. A chart is a
`ChartSpec`, not a script.

Every asset carries its origin in four places — the library card, the script
line, the timeline clip, and the inspector — using one vocabulary: *your media*,
*drawn by the system*, *licensed source*, *AI generated*. **No asset is ever
shown without it.**

---

## 5. Cost model

**Every figure below is adapter-declared, not measured against a bill.** These
are constructor defaults written into the HTTP adapters, chosen to resemble
public list prices at the time of writing. No vendor provider has ever been
called from this environment — there are no credentials to call one with — so
no number in this section has been checked against an invoice. They drive the
budget ceiling and the spend breaker, which means the *enforcement* built on
top of them is real while the *numbers it enforces against* are estimates.
See `docs/FINAL_GAP_AUDIT.md` §7.

### Declared defaults, with their source

| Call | Charged as | Declared default | Source |
|---|---|---|---|
| Transcription | per minute of audio | **$0.006** | `http_stt.py:58` |
| Speech synthesis | per 1,000 characters | **$0.015** | `http_tts.py:71` |
| Language model | per 1M input / 1M output tokens | **$3.00 / $15.00** | `http_llm.py:57-58` |
| Image generation | per image | **$0.040** | `http_image.py:56` |
| Video generation | per second | **$0.250** | `http_image.py:202` |
| Asset search (Openverse / Wikimedia) | per search | **$0.000** | no credential, no charge |
| Animation engine | per shot | **$0.000** | rendered locally by ffmpeg, no provider call |
| Rendering | per render | **$0.000** | our CPU, no provider call |

### Worked example — a five-minute explainer, nine visuals

| Scenario | Provider cost |
|---|---|
| Director draws 6, photographs 3 (**the normal case**) | **$0.03** |
| Every shot generated | $0.39 |

**Thirteen times cheaper, and the cheap one is the one the grounding gate can
verify.** The economics and the safety argument point the same way, which is why
neither is a compromise.

### How spending is controlled

Four mechanisms, and I will be precise about which are real:

1. **Before planning — real.** The director sorts scenes by cost and demotes expensive primaries until the project's **worst case** (the whole ladder, not just the primaries) fits a **$1.00** ceiling. Least-important scenes are demoted first.
2. **Before the call — real.** The router excludes any provider that cannot afford the request or whose typical latency exceeds the budget.
3. **After the call — real.** An overspend is *still recorded* — the money is gone — and raises so the ladder descends. A control on reuse, not on spending.
4. **Caching — real.** The cache key hashes kind + params only, excluding ids, timestamps and budgets. The same image across scenes or across re-renders is paid for once, and a cached result is forced to `$0.00`.

**Both gaps previously documented here are closed.** `GenerationRequest._budget_is_bounded`
now fills a per-kind ceiling on every request built through validation, and
`GenerationRouter._authorised_ceiling` raises `BudgetExceeded` when
`max_cost_usd` is still `None` — the rule is on the object, so a request cannot
be constructed without a bound, and image/video calls no longer reach the
router unbudgeted. `PROVIDER_SPEND_USD` is enforced by `MeteredSpendAuthoriser`,
injected into `GenerationRouter.spend_authoriser` in `wiring.build` and
consulted per call, with in-flight tracking so a looping job cannot pass the
same stale check twice — it is a breaker, not only a meter. Both are
implemented, wired into the running system, and covered by tests; see
`docs/FINAL_GAP_AUDIT.md` §1.

### Reservation → settle

Between "may this render start" and "this render used 4.2 minutes" there is a
window in which N concurrent requests each pass the same check. So:

1. **`reserve`** — check quota, then insert a row that *occupies* the allowance.
2. Work runs.
3. **`settle`** — atomically take the reservation (`BEGIN IMMEDIATE`, so two workers cannot both hold it) and write the **measured** figure. The estimate is discarded.
4. **`release`** on failure — the allowance comes back, costs nothing.
5. Unclaimed holds expire after **1 hour**, swept every 30 seconds.

Reservations are read from the **table**, not a process dictionary, so replicas
see each other's holds. Idempotency is a unique index, not a convention.

---

## 6. Tier model — proposed, not validated

**These tiers are proposed, not validated,** for two independent reasons.
First, every cost figure they are built on is adapter-declared rather than
measured against a bill (§5) — the margin arithmetic below is real arithmetic
on assumed numbers. Second, they were proposed at a time when several of the
metering dimensions they price against were unenforced. Two of those,
`generated_assets` and `seats`, were closed in the pass documented in
`docs/FINAL_GAP_AUDIT.md`; `storage_gb` remains open. See §7 of that document
for the cost caveat and §6 for the metering state.

| | **Free** | **Starter** | **Professional** | **Business** | **Enterprise** |
|---|---:|---:|---:|---:|---:|
| Price / month | $0 | $29 | $99 | $499 | negotiated |
| Rendered minutes | 10 | 120 | 600 | 3,000 | 50,000 |
| Transcribed minutes | 20 | 240 | 1,200 | 6,000 | 100,000 |
| Documents | 20 | 300 | 2,000 | 20,000 | 500,000 |
| **Generated assets** | **0** | 200 | 1,500 | 10,000 | 200,000 |
| Provider spend ceiling | $2 | $40 | $250 | $1,500 | $25,000 |
| Storage | 1 GB | 25 GB | 200 GB | 1 TB | 20 TB |
| Seats | 1 | 3 | 10 | 50 | 1,000 |
| API keys | 1 | 5 | 20 | 100 | 500 |
| Overage / minute | — | — | $0.20 | $0.15 | $0.10 |
| Retention ceiling | 7 d | 90 d | 365 d | 1,095 d | 3,650 d |
| Custom branding | — | — | ✓ | ✓ | ✓ |
| Priority rendering | — | — | — | ✓ | ✓ |
| SSO · audit export | ○ | ○ | ○ | ○ | ○ |
| Data residency | ○ | ○ | ○ | ○ | ○ |

**○ means the feature does not exist yet.** SSO, audit export and data residency
were previously shown as ✓ on Business and Enterprise, which is the exact shape
of claim this document exists to prevent: a tier table is read as a list of what
a customer gets for their money, and a ✓ beside a feature that is P2 on the
roadmap is a sales promise nothing in the codebase can keep. They stay in the
table — removing them would hide the intended shape of the tiers — but they are
marked as unbuilt on every tier, including the ones that would pay for them.

**Free gets zero generated assets, on purpose.** It is not a crippled tier — the
first two rungs of the ladder cost nothing, so a free user still gets real
charts, real timelines, real maps, real photographs from the commons, and
typography. They get everything except the one rung that costs us money. That is
a free tier we can afford at any scale, and it is a genuinely good product.

**Overage applies only to rendered minutes,** and only on Professional and
above. Nothing else can go over on any tier.

Unknown tier resolves to **Free** — fails closed.

**Metering state:** seven of the eight dimensions above are enforced —
`rendered_minutes`, `transcribed_minutes`, `documents`, `generated_assets`,
`provider_spend_usd`, `seats` and `api_keys`. `generated_assets` and `seats`
were the two closed in this pass: `generated_assets` is now checked by an
`AssetAuthoriser` before dispatch inside the router rather than counted only
after the fact, and `seats` is now checked by a `SeatAuthoriser` on
`Directory.add_member`. `storage_gb` is the one exception — it is genuinely
unmeasured, `/v1/usage` returns `enforced: false` for it with that explanation
attached, and the tier figures for it above are not currently applied to
anything. See `docs/FINAL_GAP_AUDIT.md` §6.

---

## 7. Data model — "we don't keep big videos in the database, right?"

**Correct. Two tables, both JSON, no binary column anywhere.**

```sql
projects  (project_id, organisation_id, owner_id, persistence,
           status, expires_at, updated_at, payload)
documents (sequence, project_id, kind, document_id, created_at, payload)
```

The database holds **the reasoning**: transcript, understanding, scene graph,
visual plan, visual bible, timeline, render job, script, visual units, media
library. All JSON.

Object storage holds **the pixels**: recordings, fetched assets, generated
images, rendered MP4, caption VTT. Every key under
`orgs/<organisation_id>/…`, enforced on write — an object cannot land outside
its tenant's namespace. `put_file` streams rather than reading into memory,
because a rendered video is hundreds of megabytes.

**The rendered MP4 is `EPHEMERAL` by design.** It is an output, not an asset. The
reasoning that produced it is kept for the plan's retention ceiling, so the
project is *regenerable*: throw the video away and every decision that made it
is still here.

Deletion order is storage-prefix first, then the database row — deliberately. A
row pointing at nothing is recoverable; orphaned objects with no row are a
permanent leak nobody can find.

**Retention bug — closed.** The sweep used to delete every file in a tenant's
namespace older than 24 hours regardless of retention class or whether the
project was still live, which meant live media could disappear on any tier.
The sweep now reads the retention class recorded with the object itself
rather than deleting on age, and a backend that cannot report a class is
refused rather than swept. 41 tests in `tests/test_retention.py`. See
`docs/FINAL_GAP_AUDIT.md` §1.

### Object storage — the real S3 adapter and backend selection

`S3StorageProvider` (`adapters/storage/s3.py`) is a complete implementation of
the S3 REST API with Signature Version 4 written by hand — `hmac`, `hashlib`
and `httpx`, no boto3 — because no package index is reachable from this
environment and SigV4 is about eighty lines. It works unchanged against S3,
R2, MinIO, B2, Ceph RGW and Wasabi. The signature is checked against AWS's own
published `get-vanilla` test vector; the signing math is externally verified
against that vector, but no request has ever been made to AWS, R2 or MinIO —
"correct against the S3 REST contract" and "works against AWS" are different
claims and only the first is made. See `docs/FINAL_GAP_AUDIT.md` §5.

**Backend selection** (`wiring.storage_provider`) is decided by whether S3
credentials (`storage_access_key` and `storage_secret_key`) are present, not
by a `STORAGE_BACKEND` name. There is no such setting, deliberately: a name
and a credential can disagree, and the dangerous direction is a deployment
that believes it is on S3 while writing customer footage to a container
filesystem discarded on the next deploy. Credentials cannot disagree with
themselves.

Local disk is **not** refused in production, and an earlier version of this
function was wrong to refuse it: the validated deployment is two workers
sharing one Docker volume, and SQLite already requires exactly that shared
filesystem — refusing local storage there while happily running the queue and
the database off the same volume would have been arbitrary. What *is* refused
at boot is a `postgresql://` database URL combined with local disk storage,
because Postgres is only chosen when the replicas were separated, and
separated replicas have separate disks — that combination renders on the
worker and 404s on the API every time, so it fails at boot instead.

---

## 8. Tenant isolation

Three independent layers. Two would have to fail at the same time.

| Layer | Enforces | Proven by |
|---|---|---|
| **Application** | the tenant is in the SQL query, not in an `if` after it | the API and security suites |
| **Database** | row-level security, `FORCE`d, one policy per tenant table, `SET LOCAL` per transaction | **18 tests against a live PostgreSQL**, attacking as the application role |
| **Storage** | every key must start `orgs/<org>/` | the tenant-storage suite |

The RLS suite does not check that policies *exist* — it tries to break them:
read another tenant's row by primary key, insert one attributed to somebody
else, update and delete across the boundary, query with no tenant set, rely on a
tenant left over from a previous transaction, disable RLS, drop the policy,
write a permissive one, create a table to escape into. All refused.

`FORCE ROW LEVEL SECURITY` is the load-bearing word: without it the table owner
bypasses every policy, and the owner is usually the migration role — the single
most common way RLS is present and useless.

---

## 9. Provider resilience

Three mechanisms sit between the router and a slow or flaky provider, all
implemented, wired into `GenerationRouter`, and tested
(`tests/test_provider_resilience.py`, 12 tests). See
`docs/FINAL_GAP_AUDIT.md` §9 for the detail on what was found and closed.

- **Router-level timeout.** Every HTTP adapter already passed `timeout=` to
  `httpx`, but nothing bounded the router's own `await` — a deadlocked SDK or
  an exhausted connection pool could hang the router indefinitely. The call is
  now wrapped in `asyncio.wait_for`, raising `TimeoutExceeded`
  (`LATENCY_EXCEEDED`, an existing retryable category rather than a new error
  invented for the occasion).
- **Circuit breaker with a cooldown.** The breaker is consulted at the single
  point where the router selects a provider (`candidates()`). It previously
  excluded a tripped provider *forever* — the only thing that reset the
  failure count was a success it could never again attempt. An `opened_at`
  stamp and a cooldown window were added, with a failed half-open probe
  pushing the window forward rather than reopening the door on every call.
- **Graceful worker drain on SIGTERM.** Stop-claiming and wait-for-in-flight
  were already correct. The gap was what happened when the grace period
  expired with a job still running: the row stayed `running` with a stale
  heartbeat until `reclaim_after_seconds` (900 s) noticed, so a job caught
  mid-deploy could be unclaimable for fifteen minutes.
  `DurableJobQueue.release_claimed(worker_id)` now hands those rows straight
  back to `pending` on shutdown, which is safe because `_settle_blocking`'s
  existing `claimed_by` guard discards a late outcome from the released
  attempt. Measured under the real deployment: a worker sent SIGTERM
  mid-render stopped claiming, finished what it held, and exited 0 after
  55.6 s, with every in-flight render reaching `ready`.

---

## 10. Testing

| Kind | Count | What it means |
|---|---:|---|
| **Contract tests** | — | Pydantic models with `extra="forbid"`; an invalid object cannot be constructed |
| **Unit tests** | — | pipeline stages, grounding, pacing, timeline arithmetic, scripting |
| **API tests** | — | driven through `create_app`, not the service — comprehensively-tested code can sit off the request path entirely |
| **Security tests** | — | authn, authz, tenancy, SSRF, upload inspection, path traversal, rate limits |
| **Architecture tests** | — | AST-parse every module and assert the dependency direction: contracts → ports → adapters → services → apps |
| **Audit regression tests** | 48 | one per defect ever found. None can return. |
| **RLS tests** | 18 | against a live PostgreSQL, adversarial |
| **Provider adapter tests** | 11 | the four HTTP adapters against a conforming server we stand up |
| **Schema drift tests** | — | committed JSON Schemas must match the code |
| **Deployment tests** | — | the Dockerfile and compose file read as data and asserted against the code |
| **Frontend unit tests** | 55 | the clock, the reactive core, undo inversion, transport retry/idempotency |
| **Live contract tests** | 9 | the hand-written wire types asserted against the running API, against a live running process |
| **End-to-end** | 60 | one Chromium, real API, real worker, real ffmpeg |
| **Total backend** | **1 080** | 0 failures, 18 skipped (all ffmpeg-dependent), 434 s. See `docs/FINAL_GAP_AUDIT.md` §13. |

Two test kinds worth calling out because they are unusual:

**Architecture tests** parse the AST of every module and fail the build if a
contract imports an adapter. Architecture that is documented drifts;
architecture that is asserted does not.

**Audit regression tests** are one per historical defect, each named for the
guarantee it broke — *"a lock is a rule, not a hope"*, *"we do not charge you
twice"*, *"we never silently cut your content"*. Every one of these was a
guarantee written down and believed while the code did the opposite.

---

## 11. The recurring lesson

Across every audit of this codebase, the same finding keeps appearing in
different clothes:

> **Where the rule was put on the object, it held. Where it was put in a
> docstring or in an `if` at one call site, it did not.**

`is_user_owned` said *"must survive a re-plan"* and the planner tested
`unit.locked`. Three operations changed what a visual shows and one of them
repointed the timeline. The grounding gate computed a verdict the inspector's
vocabulary could not express.

Every fix has been the same shape: move the rule to a boundary callers cannot
route around. Grounding is a constructor argument with no default. Repointing
happens inside `store_units`. Tenancy is in the SQL query and, now, in the
database's own policies. Idempotency is a unique index.

That is the design principle, and it is worth more than any individual feature
in this document.
