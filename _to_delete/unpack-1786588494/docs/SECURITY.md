# Security and privacy

## The premise

People will speak into this product about their work, their research, their
students, their customers and occasionally their private lives. Voice is not
neutral input. It carries identity, health, emotion and things the speaker did
not intend to record.

A product built on that has to earn the right to it, and the way you earn it is
by wanting less data rather than more.

## Principles

1. **Minimum necessary.** Collect what the pipeline needs and nothing else.
2. **Temporary by default.** Retention is a choice the user makes, not a default
   that happens to them.
3. **No silent training.** User content is never used to improve the product
   without explicit consent, and the consent travels with the data.
4. **Deletion is real.** Not a tombstone.
5. **Fail closed.** Unverified provider, unknown licence, unestablished
   permission — all mean no.

## Consent

`ProcessingConsent` on every project, defaulting to the most conservative
setting:

```python
improve_product = False   # may we retain this to improve the system?
human_review    = False   # may a person look at it for quality?
```

Both off unless chosen. The flags live on the project, so a future
training-data export cannot accidentally include material that was never
offered — it filters on the same field the user set.

## Retention

| Data | Default |
| --- | --- |
| Raw audio | 24 hours (`EPHEMERAL`) |
| Transcript, understanding, scenes, plan | project lifetime |
| Generated and fetched assets | project lifetime, or 24 hours if temporary |
| Rendered video | 24 hours unless downloaded and saved |
| Provenance and licence records | `ARCHIVE` — needed for compliance |

Enforced by storage lifecycle rules, not by a cleanup job
(`docs/STORAGE_POLICY.md`).

## Encryption

- TLS in transit, everywhere, including to storage and to every provider.
- Server-side encryption at rest on every bucket and on the database.
- Signed URLs expire in fifteen minutes and are scoped to a single object.
- Uploads go from the browser directly to storage, so the most sensitive payload
  in the product never transits our API.

## Third-party providers

The most important privacy decision in the system is which provider hears the
user's voice.

`DataPolicy` defaults are pessimistic — assume retention, assume training, assume
no agreement. Raw audio may only be sent to a provider where:

```python
not policy.trains_on_input and policy.dpa_in_place
```

This is a field comparison at dispatch time, not a paragraph in a policy PDF.
Before any provider is added to the voice path, someone verifies the policy,
records it in the adapter's capabilities, and signs the agreement.

Downstream stages are less sensitive but not insensitive: a transcript is still
the user's words. The same check applies to text generation over transcript
content. Image generation receives only prompts derived from the plan, which is
the least sensitive payload in the pipeline.

## Secrets

- Never in the repository. `.gitignore` excludes `.env`, `*.pem`, `*.key`.
- `.env.example` documents the shape and contains no values.
- In deployed environments, injected at runtime from a secrets manager.
- Rotatable without a code change, because credentials are configuration and
  provider *selection* is capability-driven rather than hard-coded.

## Logging

What may be logged: identifiers, durations, counts, statuses, error codes,
provider names, costs, latencies.

What may never be logged: transcript text, prompts, user content, credentials,
signed URLs, `raw_metadata` blobs.

`ErrorInfo` splits this deliberately. `message` is for engineers and goes to
logs. `user_message` is for the person who spoke and must never leak provider
names, prompts, stack traces or internal identifiers. `context` is for structured
non-sensitive facts — ids, durations, counts — and nothing else.

## Model output is untrusted input

Anything a language model returns is treated exactly like a request from the
public internet.

- It validates against a pydantic contract or it is rejected — `extra="forbid"`,
  so an invented field is an error rather than silently dropped data.
- It never becomes code. Animation specifications are data consumed by renderers
  we wrote (`docs/VISUAL_DIRECTOR.md`).
- It never becomes a filesystem path. Object keys are validated against traversal.
- It never becomes a URL we fetch without the adapter's own allowlist.

The threat is not only a malicious model; it is a *confused* one, driven by a
user who spoke a prompt injection out loud on purpose.

## Tenancy

Every document carries `project_id`, and `Project` carries `owner_id`. Authorisation
arrives with authentication in a later stage, but the fields exist now so that
nothing has to be migrated and no query has to be rewritten when it does.

Object keys are namespaced by project (`projects/{project_id}/...`), so
storage-level isolation is structural rather than enforced by application logic
remembering to filter.

## Content safety

Two directions, both real.

**Input.** Someone will describe something we should not visualise. Refusals from
providers are a first-class outcome (`ProviderRefused`,
`DegradationReason.SAFETY_REFUSED`) that degrade the shot rather than crash the
project.

**Output.** Generated imagery depicting real people, places or events is labelled
(`depicts_reality` → `illustrative_label`). We do not put an invented image of a
real event on screen without saying so.

## What we have not built yet

Stated plainly so nobody assumes otherwise: authentication, authorisation,
rate limiting, audit logs, abuse detection, SOC 2 controls, data-residency
routing. All are Stage 15 and beyond.

What Stage 0 guarantees is that none of them require reshaping the data model —
consent, ownership, retention, provenance and data policy are already fields on
the right documents.

---

# Stage 25 — the security layer as built

`src/vtv/security/` is one directory, so that a reviewer asking "how does this
system decide who may do what, and what does it refuse to accept" reads one
place rather than following a thread through the codebase.

| Module | Answers |
| --- | --- |
| `authz.py` | May this principal do this, in this tenant? |
| `keys.py` | Minting and verifying API keys |
| `directory.py` | Organisations, users, memberships, key storage |
| `limits.py` | Rate limits and per-request bounds |
| `net.py` | Is this URL safe to fetch? (SSRF) |
| `paths.py` | Is this path or storage key safe to use? |
| `uploads.py` | Are these bytes safe to accept? |
| `audit.py` | What happened, append-only, with secrets redacted |

## Authentication

A credential is resolved **once, at the edge**, into a `Principal`. Nothing
below the API re-authenticates; everything below receives the principal and asks
it questions. That is what makes authorisation testable without an HTTP client
and what stops a second, subtly different check appearing three layers down.

API keys carry 256 bits of `secrets.token_urlsafe` entropy. Only a SHA-256
digest is stored, so a database dump is not a list of working credentials. The
public prefix (`vtv_live_7f3a`) *is* stored, so a key can be named in a log, an
audit entry and a UI list without being recoverable.

Verification is `hmac.compare_digest`. Every authentication failure — unknown
prefix, wrong secret, revoked key, suspended tenant — returns the same error,
because distinguishing them tells an attacker which guess was closest.

**Passwords are not implemented.** `Directory.verify_password` raises. Argon2
and bcrypt are not installable in this environment and a hand-rolled KDF is
worse than the absence it replaces.

## Authorisation

Two checks, always both, always in this order:

1. **Tenant** — does this principal belong to the organisation that owns the
   thing? A capability check that skips this is how one customer reads another
   customer's projects.
2. **Capability** — does this principal hold the specific right?

Both failures raise the same error with the same user-facing message. "That
project exists but is not yours" confirms the id.

Roles are coarse and assigned by humans; capabilities are fine and asked for by
code. `ROLE_CAPABILITIES` is an explicit table (D24). Notable refusals it
encodes: an **admin cannot change billing or delete the organisation**, and a
**service key cannot manage members or keys** — so a leaked CI credential cannot
escalate itself.

## Tenant isolation

Every project carries `organisation_id`. The repository takes it as a query
parameter rather than leaving the check to the caller: a caller who forgets an
`if` is a data leak, and a query that cannot match is not.

Storage keys are namespaced through `tenant_key()`, which refuses any component
that could escape the prefix.

## Uploads

The threat model is the stated one: assume users upload malicious files.

- Identified by **magic bytes**. The declared type and the extension are hints
  from the client and both are routinely wrong, sometimes deliberately.
- Executables (`MZ`, `ELF`, Mach-O, `#!`) are never accepted.
- A file whose bytes disagree with its name is refused — that is a
  parser-confusion attack, not a confused user.
- Zip bombs are refused from the **declared** uncompressed size, before anything
  is extracted, which is the only safe order.
- Zip-slip entries (`../` in a member name) are refused.
- SVG with `<script>`, `on*=` or `javascript:` is refused. SVG executes; it is
  an HTML upload wearing an image's name.
- **Malware scanning is not implemented.** `require_malware_scan=True` with no
  scanner configured **refuses the upload**. Saying "clean" without looking
  would be worse than useless.

## Outbound requests (SSRF)

The platform fetches URLs it does not control: media inside a provider's
response, a page a user asked about, an image a document referenced.

- `http`/`https` only. No `file:`, `gopher:`, `data:`.
- **Every** resolved address is checked, not just the first — round-robin DNS
  with one poisoned record defeats a first-only check.
- IPv4-mapped IPv6 (`::ffff:127.0.0.1`), 6to4 and Teredo are unwrapped.
- **Redirects are re-checked.** This is the most commonly missed step: an
  attacker's own server answers 302 to wherever it likes.
- Credentials in the URL and unusual ports are refused.

**Residual risk:** DNS rebinding between validation and connection is not
solved. `UrlGuard.resolved_addresses()` exposes what was validated so a client
that can pin a connection avoids the window. Stated rather than hidden.

## Rate limits and request bounds

Token buckets, not fixed windows — a fixed window permits a full allowance at
the end of one window and again at the start of the next.

Buckets are per tenant, sized by plan. Authentication *failures* are limited per
address; successes are not, which was a bug this repository's tests caught (D26).
Rendering carries its own bucket because it costs real money per call.

**The limiter is in-process.** Behind a load balancer each process enforces the
limit independently. The module says so.

## The audit log

Append-only, enforced by SQLite triggers rather than by convention: `UPDATE` is
refused outright, and `DELETE` is refused for anything still inside its
retention window. An actor who can alter the record of what they did has not
been audited.

Every value written passes through `redact()` first. Keys whose *name* suggests
a secret are replaced wholesale; values matching a key, bearer token, JWT or PEM
shape are replaced by pattern. Denied attempts are recorded, not filtered — they
are the evidence that matters during an incident.

Retention: 90 days for routine entries, 400 for security-relevant ones, because
an incident is usually discovered long after it began.

## Consent and training

`Organisation.allow_product_improvement` defaults to **false**, alongside the
per-project `ProcessingConsent`. A training-data export filters on the field the
customer set, not on the absence of an objection. Customer content is never used
to improve the product without an explicit, recorded choice.
