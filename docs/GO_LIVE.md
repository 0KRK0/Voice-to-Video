# Go live — APIs, hosting, payments and the legal shell

Companion to `docs/RUNBOOK_WINDOWS.md` (which is how you run it on your own
machine) and `docs/FINAL_GAP_AUDIT.md` (which is what is actually built).

This document is about everything *outside* the code: what you buy, what you
sign up for, in what order, and what has to exist before money can move.

**Not legal or financial advice.** I am not a lawyer or an accountant. Below is
factual information about what these processes generally involve, gathered from
the providers' own current documentation. Confirm anything with a real CA or
company secretary before you file — it is cheap relative to getting it wrong.

---

## 1. Order of operations

The single most common way to waste money here is to buy things in the wrong
order. Buy nothing until the step above it is done.

| # | Step | Cost | Blocked by |
| --- | --- | --- | --- |
| 1 | Run it locally, watch a video, judge it | ₹0 | nothing |
| 2 | Add one AI key, judge again | a few hundred rupees of usage | step 1 |
| 3 | Measure real cost per project | metered usage | step 2 |
| 4 | Buy the domain | ~₹1,000/yr | step 3 (you should know it's worth it) |
| 5 | Rent a server, deploy, invite ten people | ~₹1,500–4,000/mo | step 4 |
| 6 | Build invoices and price versioning | your time | step 3 |
| 7 | Register the business, connect Razorpay | ~₹8,000–20,000 | step 6 |

Steps 1–3 need no company, no domain, no server and no lawyer.

---

## 2. The API keys — what each buys and why

None of these are needed for a local run. Each one lights up a capability that
is currently dark, and each starts costing money the moment it works.

### Language model — start here

**Sets:** `VTV_TEXT_GENERATION_ENDPOINT`, `VTV_TEXT_GENERATION_API_KEY`,
`VTV_TEXT_GENERATION_MODEL`

**Why it matters most.** Today the system understands your script with rules —
it looks for numbers, dates, places and comparisons using heuristics. With a
model, `LlmUnderstandingEngine` and `LlmVisualDirector` take over, and the
difference is qualitative: the rule-based director can only ever choose a column
chart or a line chart, so bar, area, scatter and pie renderers exist and are
never selected. The model reaches them.

**Important:** the LLM director is still bound by `Pipeline.plan_gate`. A
`Pipeline` cannot even be constructed without a grounding gate, so a model that
invents a fact gets refused rather than rendered. Connecting a model does not
loosen the anti-fabrication guarantee.

**Cost shape:** a few thousand tokens per project. Small models are around
$0.10–$0.60 per million tokens; flagship models around $2–$10. This is the
cheapest thing on the list and the one that changes the most.

### Speech to text

**Sets:** `VTV_SPEECH_TO_TEXT_ENDPOINT`, `VTV_SPEECH_TO_TEXT_API_KEY`

Turns the microphone on. Without it a spoken project asks you to paste what you
said, which rather defeats the product's name. Also gives you *measured*
timings from real audio instead of estimates from a reading speed — which is
what makes the visuals land on the right words.

**Cost shape:** around $0.005 per minute of audio.

### Text to speech

**Sets:** `VTV_SPEECH_SYNTHESIS_ENDPOINT`, `VTV_SPEECH_SYNTHESIS_API_KEY`,
`VTV_SPEECH_SYNTHESIS_MODEL`, `VTV_SPEECH_SYNTHESIS_VOICE`

Gives the video a voice. Currently narration is a correctly-timed silent track.

**Cost shape:** priced per thousand characters.

### Image generation — rung 3

**Sets:** `VTV_IMAGE_GENERATION_ENDPOINT`, `VTV_IMAGE_GENERATION_API_KEY`,
`VTV_IMAGE_GENERATION_MODEL`

The model has no default — `/health` reports `real_image_generation: false`
until it is set, even with a working endpoint and key. There is no one right
value to default it to, and the request body an image vendor will accept
(whether it wants `response_format`, and which sizes it allows) is decided
from this name; see `vtv/adapters/images/http_image.py`.

The ladder's third rung. Needed for content with no chart, no photograph and no
diagram in it — most abstract or emotional material. This will be your largest
variable cost. Meter it before you price against it.

### Video generation — rung 4

**Sets:** `VTV_VIDEO_GENERATION_ENDPOINT`, `VTV_VIDEO_GENERATION_API_KEY`,
`VTV_VIDEO_GENERATION_MODEL`

Optional, and expensive enough that it should stay behind a per-project ceiling
on every plan. `VTV_MAX_PROJECT_COST_USD` already enforces one.

**Also sets:** `VTV_VIDEO_GENERATION_DIALECT` — and it is not optional if you
are using OpenAI.

There are two video contracts and no way to tell them apart from a URL:

| Dialect | Create | Poll | Download |
| --- | --- | --- | --- |
| `poll` (default) | `POST …/video/generations` | `GET …/video/generations/{id}` | follow the `url` on the finished job |
| `openai` | `POST …/videos` | `GET …/videos/{id}` | `GET …/videos/{id}/content` — bytes, no URL |

The download step alone makes them incompatible, so pointing the default
adapter at `https://api.openai.com/v1` requests `…/v1/video/generations` and
gets a 404 on every attempt. The symptom is not an error you would recognise:
video is configured, `/health` says the capability is on, and no video ever
appears.

The OpenAI adapter is written against OpenAI's published reference and
exercised against a local server implementing it (`tests/test_openai_video.py`).
It has **never been called against OpenAI**. Two values it is deliberately
cautious about: at the time of writing OpenAI's API reference and its
video-generation guide disagreed about what `seconds` and `size` accept, so the
adapter sends only values appearing in both — `"8"`, and `1280x720` /
`720x1280`. If the vendor rejects them, its own message is surfaced rather than
replaced, because a guessed enum, a model your account cannot reach and a
revoked key all look identical from here.

**Budget, before you enable it.** A shot may spend a quarter of
`VTV_MAX_PROJECT_COST_USD`. At the default ceiling of $1.00 that is $0.25, and
an eight-second clip is estimated at $1.20 — so the video rung is skipped
before any call is made, and asking for one explicitly returns a message naming
the ceiling rather than quietly substituting a photograph. That check is
deliberately *before* the call: the router's own budget check compares a price
per second against a whole-shot ceiling, so it would let the request through
and refuse the eighty-cent result after the vendor had already charged for it.

### Object storage — not AI, but required to scale

**Sets:** `VTV_STORAGE_ACCESS_KEY`, `VTV_STORAGE_SECRET_KEY`,
`VTV_STORAGE_ENDPOINT`, `VTV_STORAGE_BUCKET`, `VTV_STORAGE_REGION`

Setting both credentials switches the whole system from local disk to S3. Works
against AWS S3, Cloudflare R2, MinIO, Backblaze B2, Ceph and Wasabi — the
adapter speaks the REST API directly with no vendor SDK.

**Recommendation: Cloudflare R2**, because it charges nothing for egress. Video
is the worst possible workload for per-gigabyte egress pricing, and on S3 your
bandwidth bill will exceed your storage bill by a wide margin.

**Caveat you must hold onto:** the S3 adapter's signature is verified against
AWS's own published test vector, and it has never made a request to a real
bucket. The first thing to do after setting those keys is upload one file and
download it back.

### Openverse — already on, needs nothing

Rung 2 of the ladder — photographs from the commons — needs no credential and is
live in your local run.

It was not, until recently, *reachable*. `SceneComposer` called
`AssetResolver.resolve` without `organisation_id`, which the method requires, so
the rung raised `TypeError` — not a `VTVError`, so the ladder's handler did not
catch it. Two things hid that: the resolver was typed `object` with a
`# type: ignore` on the call, so no checker read it, and every test that reached
the rung used a double declared `async def resolve(self, **_: object)`, which
accepts anything. `tests/test_asset_resolution.py` now drives it through the
real resolver.

**The commercial point of this rung is that it is free.** A search of the
commons costs nothing and returns a real photograph with a real licence; a
generated image costs about four cents and returns something that never
existed. The sourcing ladder therefore tries the commons first for every
section and only generates when nothing usable comes back, which is the single
largest lever you have on cost per video.

**The obligation that comes with it:** CC-BY requires attribution. The renderer
draws the credit over the shot, `Timeline.attributions` collects the full list,
and the Studio inspector shows it per visual. Do not strip it.

---

## 3. Hosting — what to actually buy

### What the system needs

- **One Linux host** with at least 4 vCPU and 8 GB RAM. Rendering is
  ffmpeg — it is CPU-bound and it will use everything you give it. Start at 4
  vCPU and watch it.
- **Docker.** The image is built and validated; `Dockerfile` is real.
- **A persistent volume** for `/var/lib/vtv`. This is not optional — it holds
  the database, the queue and (until you move to S3) the media.
- **A domain and TLS.**

### The shape that has been validated

The deployment that was actually run and measured: one API container, two worker
containers, one shared volume, SQLite for the database and queue. Seven
concurrent renders split 3/4 across the workers, zero double-processing, and a
worker drained cleanly on SIGTERM. That is a real, tested single-host
deployment and it will comfortably serve your first hundred users.

**Do not reach for Kubernetes.** Nothing here needs it, and it will cost you
more in operations than it saves.

### Concrete recommendation

A **Hetzner CPX41** or equivalent (8 vCPU, 16 GB) at roughly €25/month, or a
DigitalOcean / Vultr equivalent if you want Indian or Singapore regions for
latency. Add **Cloudflare** in front — free tier is fine — for TLS, caching of
the fingerprinted frontend assets, and basic DDoS protection.

### Deploying

```bash
# on the server, from the repo
docker build -t vtv:latest .
docker volume create vtvdata

docker run -d --name vtv-api --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  --env-file /etc/vtv.env \
  -v vtvdata:/var/lib/vtv vtv:latest

docker run -d --name vtv-w1 --restart unless-stopped \
  --env-file /etc/vtv.env \
  -v vtvdata:/var/lib/vtv vtv:latest python -m vtv.worker

docker run -d --name vtv-w2 --restart unless-stopped \
  --env-file /etc/vtv.env \
  -v vtvdata:/var/lib/vtv vtv:latest python -m vtv.worker
```

Then a reverse proxy (Caddy is the least work — it gets certificates by itself):

```
yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

### The production settings that are not optional

```
VTV_ENV=production
VTV_SIGNING_KEY=<openssl rand -hex 32>     # same value on API and every worker
VTV_ALLOWED_ORIGINS=https://yourdomain.com
```

`VTV_SIGNING_KEY` has no default in production and the process refuses to start
without it. That refusal is deliberate: with a per-process key, a media URL
signed by one replica is rejected by the next, and downloads fail roughly half
the time behind a load balancer.

In production the development bypass is gone — `development_principal()` returns
`None` when `is_production` — so every request needs a real API key. Mint the
first one by running the bootstrap inside the container, exactly as
`docs/RUNBOOK_WINDOWS.md` §5 does locally.

### What you are missing operationally, stated plainly

There is **no backup and no restore procedure**. If that volume dies, every
project dies with it. Before you have a single paying customer, at minimum:

```bash
# nightly, to somewhere that is not that server
docker run --rm -v vtvdata:/data -v /backups:/backup alpine \
  tar czf /backup/vtv-$(date +%F).tar.gz /data
```

There is also **no alerting**. `/metrics` is exposed in Prometheus format and
nothing consumes it. Point an uptime checker at `/health` on day one; it is five
minutes of work and it is the difference between hearing about an outage from a
monitor and hearing about it from a customer.

---

## 4. Payments — Razorpay

### Read this part before writing any payment code

I do not have your Lexora AI codebase in this session, so I cannot copy that
integration. More importantly, **the system is not ready to take money yet**,
and the reason is specific rather than vague.

The audit found that of the three things P2-3 needs — price versioning,
immutable invoices, per-tenant cost attribution — only the third exists. There
is no `Invoice` object anywhere in the repository, and `Plan` is a flat
current-state table with no version. Change a price and you have retroactively
changed every past charge, because nothing records what the price *was*.

Wiring a gateway on top of that produces charges you cannot reproduce. That is
not a theoretical concern: it is what you need when a customer disputes a
charge, when you file GST, and when anyone does diligence on the business.

**So the correct order is: build the billing model, then connect the gateway.**
I have deliberately not shipped payment code this session, because untested code
that moves money is the one category where "written but never run against the
real thing" is not acceptable — and it is the same standard I have applied to
everything else here.

### What needs building first (roughly a week of work)

1. **`PriceVersion`** — an immutable record of what a plan cost, from when. Every
   charge references one.
2. **`Invoice`** — immutable once issued. Line items, tax, totals, a sequential
   number that never repeats. Indian tax invoices have required fields; get
   these right the first time.
3. **Subscription state** on the organisation — active, past due, cancelled,
   and what happens to their projects in each.

### Then the Razorpay integration itself

It fits the existing architecture cleanly as a port plus an adapter, the same
shape as storage and the AI providers:

- **`ports/payments.py`** — a `PaymentProvider` protocol. Create an order,
  create a subscription, verify a webhook, issue a refund.
- **`adapters/payments/razorpay.py`** — the real one. Razorpay's REST API uses
  HTTP basic auth with your key id and secret, so no SDK is required; `httpx` is
  already a dependency.
- **A webhook route.** This is the security-critical part. Razorpay signs every
  webhook with HMAC-SHA256 over the raw request body using your webhook secret.
  You must verify that signature against the **raw bytes**, before parsing —
  parsing and re-serialising changes the bytes and the signature will not match.
  Compare with a constant-time comparison.
- **Never trust the client.** Payment success must be confirmed from the webhook
  or from a server-side fetch of the payment, never from what the browser says
  happened. This is the same rule already written into the codebase for tenant
  isolation, authorization and billing.
- **Idempotency.** Razorpay retries webhooks. The durable queue and its
  idempotency keys already exist for exactly this shape of problem — reuse them
  rather than inventing a second mechanism.

### Getting Razorpay keys

Good news: Razorpay onboards **Individual / Unregistered Business** as a
supported category, so you do not need a registered company just to accept a
payment. What you will need is PAN, address proof (Aadhaar or passport) for the
authorised signatory, and a bank account — and Razorpay's documentation is
explicit that the PAN and the address proof must belong to the *same person*.

You will still want to register before selling seriously, because an
unregistered individual cannot issue a proper GST tax invoice, cannot claim
input credit on your AI spend, and will struggle to sign a business customer.

---

## 5. The legal shell

Again: not legal advice, and worth an hour with a CA. This is the shape of it.

### Choosing a structure

| | Sole proprietorship | Private limited company |
| --- | --- | --- |
| Cost to set up | near zero | roughly ₹8,000–20,000 |
| Time | days | 1–2 weeks |
| Liability | yours, personally, without limit | limited to the company |
| Can raise investment | no | yes |
| Annual compliance | minimal | ongoing filings, an auditor |

If you are testing whether anyone wants this, a proprietorship is enough. The
moment you want outside money, a co-founder with equity, or an enterprise
customer, you need the private limited — and converting later is more work than
starting there.

For a private limited you will need: Digital Signature Certificates for the
directors, a Director Identification Number each, name approval, then
incorporation through the MCA's SPICe+ form, which issues the Certificate of
Incorporation, PAN and TAN together. A company secretary or CA does this
routinely.

### GST

Registration is generally required above the turnover threshold, and immediately
if you supply services across state lines or export. Exporting software services
has its own treatment — this is exactly the question to put to a CA rather than
to me.

### What must exist on the website

These are not optional and they are cheap:

- **Terms of Service** — what you are selling, what happens if it breaks, who
  owns the output. Say clearly that the customer owns their videos.
- **Privacy Policy** — required under the DPDP Act, and required by Razorpay
  before they activate an account.
- **Refund and cancellation policy** — Razorpay requires this to be published.
- **Contact page** with a real address and a working email.

### DPDP Act 2023 — what your product actually has to do

This one is worth taking seriously: the Act applies to every business processing
Indian users' personal data, with **no exemption for company size or stage**, and
penalties run to ₹250 crore for failing to implement reasonable security
safeguards.

What it requires you to build, and where you already stand:

| Obligation | Where you are |
| --- | --- |
| Privacy notice before collection | not written |
| Granular consent, withdrawable, timestamped | not built |
| Data retention and deletion, including backups | retention policy is built and tested; per-user deletion is not |
| Breach notification to the Board and to users | no process |
| Grievance officer, published, 30-day response | not appointed |
| Processing agreements with every vendor | not signed |
| Children's data — verifiable parental consent under 18 | not built |

The audit already flagged DSAR tooling as not built. That is the same gap: there
is per-project delete and a tenant-wide retention sweep, but no
"export everything about me" and no "delete me". You will need both.

The encouraging part is that the hard half is done. Tenant isolation is enforced
at the database with row-level security, media lives under a tenant-namespaced
prefix with the check on the object rather than at call sites, and there is an
append-only audit trail. Most startups at this stage have none of that and have
to retrofit it. You are adding paperwork and two endpoints, not architecture.

---

## 6. The shortest honest summary

**You can do today, for free:** run it, make videos, judge them, show them to
people, and find out whether the thing is good.

**You need keys for:** voice, transcription, generated imagery, and a model
smart enough to make the visual choices feel intelligent.

**You need to build before charging:** invoices, price versions, subscription
state.

**You need before launching publicly:** a backup, an uptime check, three legal
pages, and a decision about DPDP consent.

**You do not need yet:** Kubernetes, SSO, SCIM, an admin console, a KMS, or data
residency. Those are what enterprise buyers ask for, and you do not have an
enterprise buyer. Build them when someone is holding a contract and asking.
