# Asset provenance and licensing

## The commitment

**Every external asset can answer where it came from and what we may do with it.**

Not "usually". Not "for the ones we remembered". The provenance record is a
required field on the model (`Asset` in `src/vtv/contracts/asset.py`), so an
external asset without one cannot be constructed, cannot enter a timeline, and
cannot reach a render.

This is cheap to build on day one and close to impossible to retrofit. When an
enterprise customer asks where a frame in their training video came from — and
they will, in the first serious sales conversation — the answer should be a
database lookup, not an investigation.

## Rights fail closed

Rights are tri-state:

```
ALLOWED    we established that we may
DENIED     we established that we may not
UNKNOWN    we did not establish anything
```

**`UNKNOWN` behaves exactly like `DENIED`.** An image on the open web with no
licence metadata is not "probably fine". It is unusable.

The two-value alternative — a boolean `commercial_use` — is worse in a specific
and dangerous way: it forces every unparsed licence to be recorded as either a
false permission or a false prohibition. The tri-state lets us say honestly "we
have not checked", and then behave safely.

Commercial use requires **two** permissions, both explicit:

```python
license.commercial_use == ALLOWED and license.modification == ALLOWED
```

Modification is not optional. This pipeline crops, colour-grades, animates and
composites everything it touches. We are never using an asset unmodified, so an
asset we may not modify is an asset we may not use.

## Scraping is structurally impossible

`AssetSource.WEB_SCRAPE` exists in the enum for exactly one reason: so that it
can be refused **by name**. Constructing provenance or an asset with that source
raises a validation error.

Naming the forbidden case makes the prohibition testable. Leaving it out of the
enum would just mean that scraped material eventually arrives mislabelled as
`OPENVERSE`, and nothing would catch it.

## Sources

| Source | Provenance required | Notes |
| --- | --- | --- |
| `internal_library` | no | We own or licensed it outright |
| `user_upload` | no | The user's own material, their project only |
| `programmatic` | no | Drawn by us from a spec |
| `generated` | no, but a `generation_id` is required | Traceable to the exact request |
| `openverse` | **yes** | Aggregator; licence per item, verify each |
| `wikimedia_commons` | **yes** | Licence per item; attribution nearly always required |
| `stock_partner` | **yes** | Under a contract we hold |
| `web_scrape` | refused | Never |

Openverse and Wikimedia Commons aggregate items under *many* different licences.
"Found on Openverse" tells you nothing about rights. Each item's own licence is
what governs, which is why the licence lives on the provenance record and not on
the source.

## Filtering happens in the adapter

`AssetSearchProvider.search()` must not return a candidate whose rights it cannot
establish. The Openverse adapter understands Openverse's licence fields; the
Visual Director should never have to.

An adapter that cannot determine a licence returns **fewer results**. It never
returns a result with `UNKNOWN` rights and hopes the caller checks. Returning an
empty list is a correct answer — the Director simply falls back to drawing the
scene itself.

## Attribution

Where a licence requires credit, the attribution line is produced once, centrally
(`AssetProvenance.attribution_line()`), and travels with the asset into the
timeline (`AssetClipSource.attribution`) and onto the screen.

```
Replica of the first transistor by Unitronic (CC-BY-SA-3.0)
```

`Timeline.attributions()` assembles the deduplicated credits list in first-use
order for the end card. Attribution is not something the renderer improvises or a
caller formats by hand.

## Share-alike

Copyleft licences (CC BY-SA and friends) can require derivative works to carry
the same licence. Whether that is acceptable depends on the customer: it is
usually fine for a personal video and usually not for enterprise material.

`License.share_alike` is recorded, and `MediaSearchConstraints.exclude_share_alike`
lets a project exclude it at search time. The default is to allow it; enterprise
tiers will invert that default. The important thing at Stage 0 is that the flag
exists and is populated, so the policy can change without re-crawling anything.

## Raw metadata is kept

`AssetProvenance.raw_metadata` stores the provider's response verbatim.

If our parsing of a licence field turns out to be wrong — and across enough
sources, one of them eventually is — this is what lets us re-derive rights for
every asset already in the system instead of having lost the evidence.

## Generated assets

Generated imagery has provenance too, of a different kind: `generation_id` links
it to the exact `GenerationRequest` and `GenerationResult` that produced it, and
therefore to the prompt, the provider, the model and the seed.

That matters for three separate reasons: reproducing a shot, honouring a
provider's terms about generated output, and answering "is this real?" honestly
when a generated image depicts something real (`depicts_reality` →
`illustrative_label`).

## What is tested

`tests/test_asset_licensing.py` asserts, among others:

- unknown commercial rights are not usable,
- unknown modification rights are not usable,
- a bare `License` defaults to unusable,
- scraped provenance cannot be constructed,
- external assets without provenance cannot be constructed,
- provenance source must agree with asset source,
- generated assets must reference their generation,
- attribution lines are formatted centrally.

These are not schema tests. Each one describes a way a media business gets itself
sued, and asserts that the type system refuses it.
