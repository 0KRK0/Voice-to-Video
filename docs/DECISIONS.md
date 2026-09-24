# Decisions

Choices made during Stage 0 that a reader might otherwise assume were accidents,
each with the reasoning and the alternative that was rejected.

---

## D1 — The strategy vocabulary is split in two

**Decision.** `VisualStrategy` answers *how do we obtain this visual*
(existing / programmatic / licensed / generated image / generated video /
composite). `VisualPrimitive` answers *what do we draw* (typography, chart,
timeline, network, comparison, map).

**Why.** The brief lists eleven flat strategies, six of which — diagram, chart,
timeline, map, typography, animation — are things our own engine draws, not ways
of obtaining a visual. Flattening them would force the Director to re-learn the
same "draw it ourselves" decision six times, and would hide the one ratio that
determines our unit economics: drawn versus generated.

**Rejected.** The flat eleven-value enum. The full mapping is preserved in
`docs/VISUAL_DIRECTOR.md`, so nothing from the brief is lost.

---

## D2 — Rights are tri-state and fail closed

**Decision.** `Permission` is `ALLOWED | DENIED | UNKNOWN`, and `UNKNOWN` behaves
as `DENIED` at every commercial decision point.

**Why.** A boolean forces every unparsed licence into either a false permission
or a false prohibition. The tri-state lets us record "we have not established
this" honestly and then behave safely. Commercial use additionally requires
modification rights, because this pipeline modifies everything it touches.

**Rejected.** `commercial_use: bool`.

---

## D3 — `WEB_SCRAPE` exists in the enum so it can be refused by name

**Decision.** The forbidden source is named and then rejected by a validator.

**Why.** Naming it makes the prohibition testable. Omitting it would mean scraped
material eventually arrives mislabelled as something permitted, with nothing to
catch it.

---

## D4 — Animation specs are data; nothing model-authored is executed

**Decision.** A language model selects a primitive and fills in typed fields. It
never emits React, SVG, CSS or any other executable or injectable content.

**Why.** Security — nothing model-authored is run. Quality — every visual the
system can produce is one we designed and tested. Strategy — the vocabulary
becomes a compounding proprietary asset rather than a prompt.

**Cost.** The Director can only express what we have built. That is the point.

---

## D5 — The narration is the only clock

**Decision.** Video duration equals narration duration. Visuals are fitted to the
voice; the voice is never stretched or cut to accommodate a visual.

**Why.** It removes an entire class of desynchronisation bugs. Regenerating a
shot cannot shift a caption, because visuals have no say in timing at all.

**Consequence.** Every clip must declare a `FitPolicy` for what to do when its
media is shorter or longer than its slot. The contract refuses combinations that
cannot fill their slot.

---

## D6 — Programmatic visuals are stored as specifications, not files

**Decision.** `ProgrammaticClipSource` carries the spec; the renderer draws it at
render time.

**Why.** Resolution-independent, instantly re-editable, free to regenerate, and
for a typical explainer it turns roughly two thirds of the project's visual
payload from megabytes of PNG into kilobytes of JSON.

---

## D7 — Keep the reasoning, not the pixels

**Decision.** A saved project stores transcript, understanding, scene graph,
visual plan and timeline. It does not store the finished MP4 by default.

**Why.** Those documents are a few hundred kilobytes and can rebuild the video at
any resolution, in any aspect ratio, with any shot replaced. `Project.is_regenerable`
asserts the property holds.

---

## D8 — Cache keys exclude everything that does not determine the output

**Decision.** `GenerationRequest.cache_key()` hashes kind and params only —
never the request id, timestamps, scene, budget or provider hint.

**Why.** Two scenes needing the same image pay once; re-rendering after an
unrelated edit costs nothing for unchanged shots; evaluation runs are nearly free
after the first pass. At scale this is the difference between a viable margin and
an unviable one.

---

## D9 — Provider data policy is a routing input, not a document

**Decision.** `DataPolicy` defaults to `trains_on_input=True`,
`dpa_in_place=False`, and raw voice may only go to a provider passing
`is_acceptable_for_user_voice`.

**Why.** A privacy commitment enforced by a field comparison at dispatch is a
commitment. One enforced by a paragraph in a policy PDF is an intention.

---

## D10 — One Python package, not a monorepo of empty directories

**Decision.** `src/vtv/{contracts,ports,examples}` today. `services/`,
`packages/`, `apps/`, `infrastructure/` when something goes in them.

**Why.** Empty scaffolding produces a repository that lies about what exists. The
seams that matter are enforced by the import-boundary tests, which work regardless
of directory layout and will keep working when the layout grows.

---

## D11 — Tests are `unittest.TestCase`, run by pytest

**Decision.** Test classes use the standard library's `unittest`; `pytest` is the
configured runner and discovers them natively.

**Why.** The suite runs in any environment, including one where installing pytest
is impossible. That is not hypothetical — Stage 0 was developed in exactly such
an environment — and it cost nothing to guarantee.

---

## D12 — Identifiers are prefixed Crockford base32

**Decision.** `scn_01h9zk3m...`, 24 characters from an alphabet excluding
`i`, `l`, `o` and `u`.

**Why.** The prefix makes logs, traces and database rows self-describing and
makes it impossible to pass a scene id where an asset id belongs. The alphabet
survives being read aloud, retyped from a screenshot, or pasted into a support
ticket. No coordination is required to mint one.

---

## D13 — `extra="forbid"` on every contract

**Decision.** Unknown fields are a validation error everywhere.

**Why.** The single highest-value line of configuration in the contracts. It
turns a hallucinated field from a language model, or a client on a stale schema,
into a loud error at the boundary instead of silently dropped data discovered
missing four stages later.

---

## D14 — Both a `rationale` and an `estimate` on every visual directive

**Decision.** Every decision states why it was made and what it is expected to
cost.

**Why.** The rationale makes decisions reviewable by humans, explainable to users
and gradeable by the evaluation system. The estimate — supplied by the router
from real provider capability data, not guessed by a model — makes the cost
trade-off an actual computation.

---

## D15 — Rules first, models second, everywhere

**Decision.** Understanding and visual direction each have a complete,
deterministic implementation (`HeuristicUnderstandingEngine`,
`RuleBasedVisualDirector`) and a model-backed one behind the same interface. The
rules are the default; the model engine falls back to them.

**Why.** Rules are free, instant, reproducible and cannot hallucinate. They are
also the baseline the model has to beat, which is what makes the evaluation
system able to measure anything. And when somebody says "from one billion to
eight billion", a chart of those two numbers is not an approximation of the best
visual — it *is* the best visual, and no model improves on it.

**Cost.** The rules are English-first and say so.

---

## D16 — The renderer draws every frame itself

**Decision.** `FfmpegRenderer` composes each frame in PIL and pipes raw pixels
into one ffmpeg process that muxes the narration. No per-clip segments, no
concat, no subtitle burner.

**Why.** Transitions become a two-frame blend rather than a filter graph;
captions and attributions are drawn with the same type system as every other
visual; and because every frame's timestamp derives from the narration clock,
audio drift has nowhere to enter.

**Rejected.** Per-clip encode plus concat, which is the obvious approach and
makes crossfades and burnt-in captions substantially harder.

---

## D17 — No mock ever stands in for a missing provider in the product path

**Decision.** With no image-generation credential configured, no image provider
is registered, the router raises `PROVIDER_UNAVAILABLE`, and the scene descends
its fallback ladder to something the system draws. Stubs exist
(`vtv.adapters.testing`) and are wired only by tests.

**Why.** A mock in the product path hides a real failure, which is precisely the
failure mode the fallback ladder exists to make visible. The honest behaviour
also exercises Rule 8 on every run rather than only in tests.

**Consequence.** A development install produces a video with no generated
imagery in it, and `GET /health` says why.

---

## D18 — The development transcriber refuses to invent words

**Decision.** `ScriptedSpeechToTextProvider` aligns a *supplied* script to the
real speech regions of real audio. Without a script it raises.

**Why.** There is no honest way to fake transcription. What can be done honestly
is to take the real timings from the real audio — ffmpeg's silence detection
finds them — and align known text to them, which exercises Stages 3 to 20
against genuine timing data. Every transcript it produces is stamped
`provider: "scripted-dev"`, so it can never be mistaken for the real thing.

---

## D19 — Six primitives, not seventeen

**Decision.** The animation engine implements typography, chart, timeline,
network, comparison and map. The brief lists roughly seventeen.

**Why.** Each primitive is a design problem, not a code problem: it has to look
good at every aspect ratio, in every style, at every progress point. Six that
render beautifully are worth more than seventeen that render adequately, and the
`VisualPrimitive` enum is where the remainder get added deliberately.

**Enforced.** `test_every_declared_primitive_has_a_renderer` fails if the
Director can choose something the engine cannot draw.

---

## D20 — Metrics withhold a verdict on small samples

**Decision.** `scene_compression` and `entity_grounding` report their value but
set no target below four semantic units.

**Why.** A one-sentence script produces one scene, and a compression ratio of
1.0 is correct rather than a failure. The alternative — weakening the target
until short cases pass — would have made the metric unable to catch the failure
it exists to catch. A metric that cannot fail measures nothing; a metric that
fails on cases it does not apply to trains people to ignore it.

---

## D21 — Documents become transcripts, not a second pipeline

**Decision.** A PDF, deck, page or spreadsheet is parsed into a `SourceDocument`
and then converted into a `Transcript`. Stages 3 through 30 never learn which
kind of input they are serving.

**Why.** The alternative — a parallel path for written input — doubles every
subsequent stage and guarantees the two drift. The transcript already carries
everything the pipeline needs: ordered text with timings and a language. A
document can produce one, so it does.

**What it cost.** Written input has no timings, so they are synthesised at a
speaking rate and the transcript is stamped `provider: "synthetic-document"`.
Nothing downstream may mistake an estimate for measured audio.

**Enforced.** `tests/test_language_and_ingestion.py` parses a real file of each
of six formats, written by the real library that produces that format.

---

## D22 — Speaker notes are the narration; bullets are on-screen text

**Decision.** On a slide that has speaker notes, the notes become the narration
and the bullets are demoted to visual material.

**Why.** This is the single most valuable judgement in the ingestion path. A
deck's bullets were written to be *read*; the notes are the talk the author
already wrote out. Narrating the bullets produces a video that reads its own
captions aloud.

**Enforced.** `test_pptx_speaker_notes_become_the_narration` builds a real
`.pptx` with python-pptx and asserts the bullets do not appear in the spoken
text.

---

## D23 — No speech synthesiser, so silence — labelled

**Decision.** With no TTS provider configured, document input still renders. The
narration track is correctly-timed silence, produced by ffmpeg, and the run
records a `DegradationStep`.

**Why.** The narration track is the pipeline's clock (Rule 5); without something
occupying the time axis the timeline collapses. Producing silence keeps one code
path for voice and documents, so the day a synthesiser is configured nothing
downstream changes. What it must never do is pretend: the provider is named
`silent-narration`, `capabilities.real_speech_synthesis` stays false, and the
API tells the caller the video will be mute before the job is queued.

**Rejected.** Refusing document input entirely — it would have made Stage 21
undemonstrable. Generating a robotic voice from a phoneme table — worse than
silence and dishonest about what it is.

---

## D24 — Roles are coarse, capabilities are fine, and the table is explicit

**Decision.** `ROLE_CAPABILITIES` is a hand-written table. Code asks
`principal.can(Capability.PROJECT_DELETE)`, never `role == "admin"`.

**Why.** A table a reviewer can read line by line during a security review is
worth more than a derivation from an ordering, and every privilege-escalation
bug hides in the clever version. The capability question is also the only one
that stays correct when a new role appears.

**Enforced.** `tests/test_security.py` asserts what each role *cannot* do,
including that an admin cannot change billing and a service key cannot manage
members.

---

## D25 — The tenant check and the capability check are one function

**Decision.** Every access decision goes through `vtv.security.authz.require`,
which checks tenant membership *before* capability and raises identical errors
for both failures.

**Why.** Thirty inline comparisons cannot be audited; one function with one test
file can. Identical errors matter because "that project exists but is not yours"
confirms the id to an attacker who is guessing.

**Enforced.** `tests/test_api_security.py` drives the real HTTP application and
asserts every project subresource returns 403 or 404 to another tenant. That
test exists because a perfect authorisation function one route forgets to call
is indistinguishable from no authorisation on that route.

---

## D26 — API key secrets are never stored, and login limits apply to failures

**Decision.** Only a SHA-256 digest and a public prefix are stored. The secret
exists once, in the response that created it. The per-address login limit is
charged on *failed* authentication only.

**Why.** A database dump must not be a list of working credentials, and the
prefix is what makes "revoke the key ending 7f3a" a usable instruction. The
second half was a bug this repository's own test suite caught: charging every
successful call to the login bucket throttled legitimate clients at the
credential-stuffing rate. Authenticating is not attempting to log in.

**Not done.** Password verification raises `NotImplementedError`. Argon2 and
bcrypt are not installable here and a hand-rolled KDF is worse than the absence
it replaces.

---

## D27 — Quotas reserve before spending; rate limits are a separate thing

**Decision.** `UsageMeter.reserve` holds an allowance before the provider calls
are made and `settle` records the *measured* figure afterwards. Rate limiting
lives in `vtv.security.limits` and answers a different question.

**Why.** A quota checked after the work is done is an accounting entry, not a
control, and a quota checked without reserving lets ten concurrent requests each
pass a check only one of them should. Rate limits protect the service and reset
continuously; quotas protect the business and reset on a billing boundary.
Conflating them throttles a paying customer for being fast.

**Enforced.** `test_concurrent_requests_cannot_overshoot_a_quota` and
`test_settling_bills_the_measured_figure_not_the_estimate`.

---

## D28 — The grounding gate refuses, and the refusal is a degradation

**Decision.** Every number, date and place drawn by a programmatic visual must
trace to the transcript, the extracted quantities or a source table. A spec that
fails is refused and the fallback ladder descends; if everything is refused, the
scene sets the narration as type, which cannot misstate itself.

**Why.** A chart is the most authoritative object a video can contain. A viewer
who would question a sentence accepts a bar chart without reading the axis, so a
chart of inferred numbers launders a guess into evidence. Fetched photographs
and generated images are deliberately *not* checked: they make no precise
quantitative claim, and holding them to this standard would refuse every
illustrative shot in the product.

**What it is not.** Not a fact checker. It knows whether the *system* added
anything the source did not say; it does not know whether the speaker was right.
That distinction is stated to customers rather than blurred.

**Caught a real bug.** The first version built its evidence from the extracted
entities alone and refused a chart correctly labelled "percent". The evaluation
corpus failed, and `test_a_correctly_labelled_chart_survives_the_gate` now
guards it.

---

## D29 — The Visual Bible binds recurring entities only, and locks are final

**Decision.** An entity appearing in two or more scenes with sufficient salience
gets one colour and one label for the whole project. A binding marked `USER` or
`BRAND` is never revised by the pipeline.

**Why.** A thing mentioned once cannot be inconsistent with itself, and binding
it spends a slot from a small palette for nothing. The lock rule is the one that
makes iterative editing safe: a user who pressed "keep this" and watched it
change on the next render has learned the control does not work, and no
cleverness elsewhere recovers from that.

**Enforced.** `test_a_locked_binding_is_never_overwritten` and
`test_building_twice_produces_the_same_decisions`.

---

## D30 — The audit log is append-only in the database, not by convention

**Decision.** SQLite triggers refuse `UPDATE` on any audit row and `DELETE` on
any row still inside its retention window.

**Why.** An actor who can alter the record of what they did has not been
audited. An application-level rule is broken by an application bug, a careless
migration or a compromised process; a trigger stops all three at the same wall.
Security-relevant entries are kept 400 days against 90 for routine ones, because
an incident is usually discovered long after it began.

**Enforced.** `test_entries_cannot_be_modified` executes a real `UPDATE` against
a real file and asserts `IntegrityError`.

---

## D31 — S3 signed by hand, not by boto3

**Decision.** `S3StorageProvider` (`adapters/storage/s3.py`) implements the S3
REST API and Signature Version 4 itself, using only `hmac`, `hashlib` and
`httpx`.

**Why.** No package index is reachable from this environment, so `boto3` is
not installable here, and SigV4 is about eighty lines once written out
directly. A hand-rolled signer written against the published algorithm and
checked against AWS's own `get-vanilla` test vector is a stronger claim than
an unauditable dependency would have been anyway: the canonical request, the
string-to-sign and the final signature are each verified byte-exact against a
value AWS itself publishes, rather than trusted because a well-known library
produced it.

**Cost.** Two of the vector constants in the first version of that test were
wrong — the signer was right and the assertions were fiction. They were
corrected against the published values and cross-checked against an
independent implementation before the "externally verified" claim was made.

**Not claimed.** The signature is correct against the S3 REST contract. No
request has been made to a real bucket — AWS, R2 or MinIO — so "correct
against the contract" and "works against AWS" remain different claims.

---

## D32 — The storage backend is chosen by credentials, not by a name

**Decision.** `wiring.storage_provider` selects local disk or S3 by whether
`storage_access_key` and `storage_secret_key` are both set. There is no
`STORAGE_BACKEND` setting.

**Why.** A name and a credential can disagree, and the dangerous direction is
`STORAGE_BACKEND=s3` with the secret unset: the function would have to either
crash or silently fall back to disk, and a silent fallback here means a
deployment that believes it is on S3 while writing customer footage to a
container filesystem discarded on the next deploy, with nothing in the logs to
say so. Credentials cannot disagree with themselves — reading them directly
removes the entire class of bug.

**What was wrong the first time.** An earlier version of this function refused
local disk storage outright in production. That was wrong, not conservative:
the validated deployment is two workers sharing one Docker volume, and SQLite
already requires exactly that shared filesystem for `queue.db`. Refusing local
object storage on that same volume while happily running the queue and the
database off it was an arbitrary distinction, not a safety rule — it forbade a
configuration that was already safe and already required elsewhere in the same
deployment.

**What is actually refused.** A `postgresql://` database URL combined with
local disk storage. Postgres is only chosen when the replicas were separated,
and separated replicas do not share a disk — that combination renders on the
worker and 404s on the API every time, and it now fails at boot instead.

**Enforced.** `wiring.storage_provider` raises before the process can serve a
single request in that configuration, rather than leaving it to be discovered
by a customer's failed download.

---

## D33 — Credential redaction is a suffix rule, not a field-name list

**Decision.** `Settings.redacted()` replaces any field whose name ends in
`_api_key`, `_key`, `_secret`, `_token` or `_password`. It previously matched
`_api_key` and the literal `signing_key`.

**Why.** A list of field names only redacts the secrets somebody remembered to
add to the list. `storage_secret_key` was added to `Settings` long after
`redacted()` was written, matched neither pattern, and would have been printed
in full in the boot log — one field away from a credential leak. A suffix rule
generalises: any future field ending in one of these five suffixes is
redacted automatically, with no second commit required to keep the log safe.

**Enforced.** Every credential is replaced with `"***set***"` or `None`, never
truncated — a truncated secret is still a leaked secret.

---

## D34 — The circuit breaker cools down; it does not exclude forever

**Decision.** A provider that trips the circuit breaker is skipped for a
cooldown window (`CIRCUIT_COOLDOWN_SECONDS`), stamped by `opened_at`, and then
gets one half-open probe. A failed probe pushes the window forward again
rather than reopening the door on every subsequent call.

**Why.** The breaker already existed and was consulted at the single correct
place — inside `GenerationRouter.candidates()`, the router's one selection
point. But once tripped, a provider was excluded *forever*: the only thing
that reset the failure count was a success it could never again be given the
chance to attempt. A provider having one bad minute would otherwise be
permanently unreachable for the lifetime of the process, which turns a
transient fault into a manual-restart-shaped outage.

**Rejected.** Permanent exclusion after the threshold, and a fixed-interval
retry with no back-off, which would hammer a provider that is down for longer
than one interval.

**Enforced.** `tests/test_provider_resilience.py`.

---

## D35 — A worker releases its claimed rows on SIGTERM

**Decision.** `DurableJobQueue.release_claimed(worker_id)` is called during
worker shutdown and hands every row still claimed by that worker straight back
to `pending`, rather than leaving them `running` for the reclaim sweep to find.

**Why.** Stop-claiming-and-wait-for-in-flight was already correct, but the gap
was what happened when the grace period expired with a job still running: the
row stayed `running` with a stale heartbeat until `reclaim_after_seconds`
(900 s) noticed it. A job caught mid-deploy could be unclaimable for up to
fifteen minutes for no reason better than "nobody told the queue yet." Since
the worker already knows it is shutting down, it can say so immediately
instead of making every other worker wait out a timeout tuned for the case
where a worker dies without telling anyone.

**Why this is safe.** `_settle_blocking`'s existing `claimed_by` guard
discards a late outcome from the released attempt — if the original attempt
finishes and reports in after its row has already been handed to a new
claimant, the stale report is dropped rather than double-settling the job.

**Rejected.** Leaving the reclaim timeout as the only recovery path. It is
still needed for the case a worker dies without a chance to run its shutdown
handler at all — SIGTERM handling is an optimisation for the graceful case,
not a replacement for the timeout.

**Enforced.** Measured under the real deployment, not just in a test: a
worker sent SIGTERM mid-render stopped claiming, finished what it held, and
exited 0 after 55.6 s, with every in-flight render reaching `ready`. See
`docs/FINAL_GAP_AUDIT.md` §8 and §9.
