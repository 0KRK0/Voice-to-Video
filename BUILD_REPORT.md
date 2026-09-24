# Build report — Universal Visual Intelligence Platform

> **Current state: `docs/FINAL_GAP_AUDIT.md` (2026-08-19).** That document is
> the source of truth for what is built, what was executed, and what is
> blocked. It classifies every gap as IMPLEMENTED / RUNTIME-WIRED / TESTED /
> EXTERNALLY VERIFIED / PARTIAL / BLOCKED / NOT IMPLEMENTED, and where this
> report or any other disagrees with it, **it wins**.
>
> Headline numbers as of that audit: **1,080 backend tests** and **55 frontend
> unit tests** passing, plus **9 live wire-contract tests run against a running
> API process**. A real production image was built and a two-worker deployment
> validated — seven concurrent renders, split 3/4 across workers, zero
> double-processing, a real 96,418-byte MP4 downloaded over HTTP, and a worker
> drained and exited 0 on SIGTERM mid-render.
>
> What remains blocked is stated there and not softened here: **no vendor AI
> provider has ever been called**, so every cost figure in this repository is
> adapter-declared rather than measured, the pricing tiers are proposed rather
> than validated, and **no human has evaluated output quality**.

> **Earlier supersession: `docs/FINAL_REPORT.md`.** All eleven P0 findings and
> six of seven P1 findings from the 2026-08-13 audit were closed there; 740
> tests passed and `mypy --strict` was clean across 122 modules. Retained for
> the record; the gap audit above is newer.

> ## ⚠️ SUPERSEDED IN PART — read this first
>
> A static architecture audit on **2026-08-13** (`docs/AUDIT_2026-08-13.md`)
> found that several capabilities described below as complete were built as
> libraries and **never wired into the request path**. Where this report and the
> audit disagree, **the audit is correct**.
>
> Specifically, the following claims in this document are **withdrawn**:
>
> | Claim below | Reality |
> | --- | --- |
> | Stage 24 grounding "IMPLEMENTED, TESTED" | `LlmVisualDirector` **bypasses the gate entirely**, and wiring makes it primary when a text credential exists |
> | Stage 23 consistency "IMPLEMENTED, TESTED" | The Visual Bible is built and reviewed, then **never applied or persisted** |
> | Stages 27–28 durability | `DurableJobQueue` has **zero production callers**; the API uses the in-process queue and renders inside its own event loop |
> | Stage 25 "tenant isolation enforced at the query" | `owned_project` **fails open** when `organisation_id` is null; `security/paths.py` has zero callers |
> | Stage 26 "concurrency cannot overshoot a quota" | True in one process only; reservations are a Python dict |
>
> Overall production readiness assessed at **31/100**. The stage board and the
> remediation plan live in **`docs/REMEDIATION.md`**, which is the current
> source of truth for what is done.
>
> Everything below is preserved as the record of what was *implemented*. It is
> accurate about the components. It is optimistic about the composition.


Stages 0 through 30. Written to be checkable rather than impressive: every claim
below either points at a test that fails when it breaks, or says explicitly that
it does not.

## Verification, run on the final state

```
ruff check .                     All checks passed
mypy --strict src/vtv            Success: no issues found in 110 source files
python -m unittest discover      Ran 500 tests — OK
schema drift                     18 exported JSON Schemas, current
evaluation corpus                10 / 10 cases pass
```

131 Python files, ~34,300 lines including tests. 18 documents.

### Two real videos, produced by this code, in this environment

**Voice path.** Synthetic recording → 3 scenes, 5 caption cues, 25.8s MP4,
H.264 + AAC, $0.00, 1 entity bound in the Visual Bible, 0 continuity issues.

**Document path.** A real PDF written by reportlab → 5 blocks → 4 scenes,
8 caption cues, 25.7s MP4, H.264 + AAC, $0.00. The audio track is **silence**,
one recorded `DegradationStep` says why, and the API tells the caller before the
job is queued.

---

## What was built in this session

### Stage 21 — Universal input layer · IMPLEMENTED, TESTED

Six parsers behind one port: markdown/plain text, HTML, CSV/JSON/XLSX, DOCX,
PPTX, PDF. Each normalises into a `SourceDocument` of typed blocks that becomes
a `Transcript` — so a PDF and a recording are indistinguishable from stage 3
onward.

Format is decided by magic bytes. A PDF named `.txt` is parsed as a PDF, and
there is a test for exactly that.

Structure survives, which is the whole point. A heading stays a heading so the
scene engine sees the author's own topic boundary. A table stays a table, so the
Visual Director charts the author's own figures rather than prose about them.
Provenance survives too: every block knows its page or slide.

**The judgement call that matters most:** on a slide with speaker notes, the
notes become the narration and the bullets are demoted to on-screen material.
Narrating the bullets produces a video that reads its own captions aloud.

*Tested against real files written by python-docx, python-pptx and reportlab —
not fixtures we hand-crafted to match our own assumptions.*

### Stage 22 — Real-time voice · PARTIAL

**Built:** a `SpeechSynthesisProvider` port, an HTTP adapter, and a narration
service routed through the same generation router — so synthesis is cached,
costed, budgeted and subject to the fallback ladder. When real synthesised audio
disagrees with estimated timings, the transcript is scaled onto the audio,
because the audio is what the viewer hears.

**Not built:** streaming transport. Live partial transcripts are a transport
problem before they are a pipeline problem, and no streaming provider is
reachable here.

**The honest fallback:** with no synthesiser, document input renders with
correctly-timed silence. Provider name `silent-narration`,
`real_speech_synthesis: false` in `/health`, a recorded degradation, and a note
in the 202 response. It never pretends.

### Stage 23 — Visual consistency engine · IMPLEMENTED, TESTED

`VisualBible`: one colour and one label per recurring entity, for the length of
a project. Colours are drawn from a fixed palette in a deterministic order, so
re-rendering reproduces the video the user approved rather than one resembling
it.

Only entities appearing in two or more scenes are bound — a thing mentioned once
cannot be inconsistent with itself.

A binding marked `USER` or `BRAND` is **never** revised by the pipeline. That
one rule is what makes iterative editing safe.

Continuity problems are **reported, not corrected**. A deck that deliberately
recolours a diagram is indistinguishable from a mistake at this level, and
quietly "fixing" the deliberate case is worse than flagging both.

### Stage 24 — Factuality and grounding · IMPLEMENTED, TESTED

Every number, date and named place a programmatic visual draws must trace to the
narration, an extracted quantity, or a source table.

- A chart with one invented value is refused **whole** — plotting the supported
  subset would silently change the shape of the claim.
- An extrapolated trend is refused. It is the most tempting invention.
- Rounding is accepted ("about 8 billion" ≈ 8,045,311,447); a different number
  is not.
- Spoken numbers count: transcripts contain "eight billion" and "nineteen forty
  seven", not digits.
- Photographs and generated images are deliberately **not** checked. They make
  no precise quantitative claim.

Refusal is a degradation. If every rung is refused, the scene sets the narration
as type, which cannot misstate itself.

**This gate caught nothing on the way in — it was caught itself.** The first
version built evidence from extracted entities alone and refused a chart
correctly labelled "percent". The evaluation corpus failed. The fix was to feed
in the narration, and a regression test now guards it. A gate this strict only
earns its keep if its false-positive rate is measured.

### Stage 25 — Security and privacy hardening · IMPLEMENTED, TESTED

`src/vtv/security/` — one directory a reviewer reads end to end. 89 unit tests
plus 27 that drive the real HTTP application.

| | Proven by |
| --- | --- |
| Tenant isolation | Every project subresource returns 403/404 to another tenant |
| Denials leak nothing | Wrong-tenant and no-capability produce identical messages |
| RBAC separation of duties | An admin cannot touch billing; a service key cannot manage members |
| Key secrets never stored | Minted secret is absent from the record, the repr, the audit log and the database file |
| SSRF | Metadata endpoint, DNS-poisoned hostname, second resolved address, IPv4-mapped IPv6, redirect chain — all refused |
| Path traversal | `..`, absolute components, null bytes, symlinks out of the root |
| Uploads | Executables, zip bombs, zip-slip, active SVG, bytes/extension mismatch |
| Audit immutability | A real `UPDATE` against a real file raises `IntegrityError` |

**Two capabilities refuse rather than pretend.** `verify_password` raises
`NotImplementedError` because Argon2 and bcrypt are not installable and a
hand-rolled KDF is worse than the absence. `require_malware_scan=True` with no
scanner **refuses the upload** — saying "clean" without looking would be worse
than useless.

**A real bug this test suite caught:** the first wiring charged every successful
API call to the login-attempt bucket, throttling legitimate clients at the
credential-stuffing rate. Authenticating is not attempting to log in.

### Stage 26 — Billing and usage · PARTIAL (metering real, no processor)

Plans as a table, so a pricing change is not a code change. Reserve-then-settle,
so ten concurrent requests cannot each pass a check only one should. Idempotency
as a unique index, so a retried job bills once. Every usage record carries what
the providers actually charged, so gross margin per customer is a subtraction —
and that number is stripped from the customer-facing endpoint.

Every plan, including Enterprise, bounds provider spend. A contract without a
ceiling cannot be capacity-planned.

**Not built:** payment processor, proration, tax, dunning. `invoice_lines()`
produces lines; nothing is charged.

### Stages 27–28 — Scalability, reliability, DR · PARTIAL

A durable SQLite-backed job queue with 27 tests:

- A job survives the process that enqueued it — proven by discarding the queue
  object and building a new one on the same file.
- Two workers, one database, 40 jobs: each executed **exactly once**. The claim
  is a single guarded `UPDATE`, so the database arbitrates rather than the
  process.
- Retries with capped exponential backoff and deterministic jitter, dead letters
  preserving the final error, `replay()`.
- Idempotency survives restart because it is an index, not a dictionary.
- Crash recovery via heartbeat staleness; a job past its attempts goes straight
  to dead letter rather than being resurrected.

**Not built:** cross-region replication, and the rate limiter is in-process —
behind a load balancer each process enforces the limit independently, which the
module states.

### Stage 29 — Enterprise foundation · PARTIAL

Organisations, users, memberships, roles, capabilities, API keys, audit export,
data residency and retention — as contracts the repositories enforce.
`organisation_id` is a column and a query parameter, not a convention callers
must remember.

**Not built:** an actual OIDC integration. `User.sso_subject` is the field it
would populate.

### Stage 30 — Production readiness

`docs/PRODUCTION_READINESS.md`: what is proven, what is written but unexecutable
here, what is missing and named, a nine-item pre-launch checklist and an honest
risk register.

---

## What the deterministic pipeline is still for

It was kept, as instructed, and it is not legacy:

- **The fallback floor.** With no provider reachable, the golden path still
  produces a real MP4. That is what makes every degradation test executable.
- **The evaluation baseline.** Ten scored corpus cases run in seconds with no
  network and no cost. Model-backed engines are measured against it.
- **The development environment.** `make demo` works on a fresh checkout with no
  credentials.
- **The cheap tier.** A drawn chart of stated numbers is more accurate than a
  generated image of them, and costs nothing (Rule 6).
- **Offline capability.** Which is a product, not a limitation, for customers
  who cannot send content to a third party.

Nothing was rewritten. Every stage from 21 onward extended the existing shape;
none required a new layer.

---

## Blocked by this environment — stated, not hidden

No network, no PyPI, no npm, no GPU. Six capabilities are therefore **written
but not executed**: speech-to-text, speech synthesis, text generation, image and
video generation, asset search, and remote media fetch. Each has a real adapter
behind a port, each carries a `STATUS:` line, and in every case the system
reports the *absence* rather than fabricating a success.

Two further absences are deliberate refusals rather than environment limits:
password hashing and malware scanning both raise or refuse.

---

## Where I would not yet put a paying customer

Ordered honestly.

1. **Any language other than English has never been read by a native speaker.**
   The font coverage report is a machine's opinion about glyph presence. RTL
   word ordering is explicitly marked unverified.
2. **Nothing has run against a real AI provider.** Every adapter is written to a
   published contract and none has seen a real response. Provider responses
   drift; the parsing is defensive, but it is untested against reality.
3. **The rate limiter is single-node.** The second API process doubles every
   limit.
4. **No load test exists.** Concurrency correctness is proven; throughput is
   not measured.
5. **The animation engine has six primitives of a planned seventeen.** The
   output is good and narrow.

---

## What I am most confident in

The architecture. Ten stages were added in this session — universal input,
synthesis, consistency, grounding, security, billing, durability, tenancy — and
not one required a new layer, a new dependency direction, or a rewrite of
anything earlier. Stage 24's refusal reuses Stage 8's degradation mechanism.
Stage 26's metering reads a cost ledger Stage 17 already maintained. Stage 27's
durable queue is a second implementation of a port written at Stage 15.

That is what the contracts-first discipline was for, and it paid.

And the honesty holds: the document path ships a mute video and says so, the
grounding gate refuses charts it cannot justify, the upload path refuses to call
anything clean without looking, and `verify_password` would rather raise than
hash badly. Nothing in this repository claims a capability it does not have.
