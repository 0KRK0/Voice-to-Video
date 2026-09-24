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
