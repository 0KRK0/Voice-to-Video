# Storage policy

## Three principles

1. **Application logic never sees a path.** It holds an `ObjectRef` and asks the
   `StorageProvider` port for bytes or a signed URL (Rule 14).
2. **Keep the reasoning, not the pixels.** A project is regenerable from small
   documents. Storing finished video by default is the expensive way to avoid a
   cheap re-render.
3. **Temporary is the default.** Voice is intimate. We should want less of it on
   our disks, not more.

## Object references

```python
ObjectRef(
    bucket="vtv-media",
    key="projects/prj_.../recording.webm",
    content_type="audio/webm",
    size_bytes=612_344,
    checksum_sha256="...",
    retention=RetentionClass.EPHEMERAL,
)
```

Nothing here names a cloud, a region or a URL. `ref.uri` gives
`obj://vtv-media/projects/.../recording.webm` for logs. Turning that into
readable bytes is the adapter's problem, which is what makes local disk in
development and object storage in production the same code path.

Keys are validated: no leading slash, no `..` segments. Path traversal in a media
system is not a theoretical concern when part of the key can derive from user
input.

## Retention classes

Retention is a property of the object, so the storage layer can apply lifecycle
rules without understanding the domain.

| Class | Lifetime | What lives here |
| --- | --- | --- |
| `EPHEMERAL` | hours | Raw recordings, intermediate renders, generation outputs for temporary projects |
| `PROJECT` | as long as the project is saved | Assets a saved project depends on |
| `ARCHIVE` | beyond project lifetime | Only things with a legal or billing reason: licence receipts, audit records |

Expiry is implemented as a **bucket lifecycle rule**, not a cleanup job. Data
disappears because the storage system was told to expire it, not because a cron
task remembered to run. A cleanup job that silently stops running is a data
retention incident that nobody notices for a year.

Consequently, "object expired" is a normal, expected outcome that the code
handles (`ErrorCode.OBJECT_EXPIRED`), not an exception to be surprised by.

## The two persistence modes

### Temporary — the default

```
record → process → render → download → swept
```

Everything is `EPHEMERAL`. After the retention window (24 hours by default) the
audio, the assets and the render are gone.

This is the right default for someone making one video. It is also the right
default commercially: storage of material we do not need is pure cost carrying
pure risk.

### Saved

The user asked to come back to it. We keep:

```
project             the state machine and references
transcript          what was said, with timings
understanding       what it meant
scene graph         how it became a story
visual plan         what each scene should look like
asset references    plus full provenance
timeline            everything placed in time
```

Total: a few hundred kilobytes of JSON.

We do **not** keep the finished MP4 by default. From the documents above the
video can be rebuilt — at a different resolution, in a different aspect ratio,
with one shot replaced. Keeping the reasoning is both cheaper and strictly more
useful than keeping the pixels.

`Project.is_regenerable` asserts this property holds. A storage change that
breaks it is a bug, not an optimisation.

## What is never stored

- Raw audio beyond its retention window, unless the user saved the project *and*
  consented.
- API keys or credentials anywhere in the repository or in any document.
- Transcript text in log lines or metrics. Identifiers and durations only.
- Prompts or user content in `ErrorInfo.context`.

## Deletion

`StorageProvider.delete()` is real deletion, idempotent, and is the operation
behind a user's right to erasure. Deleting a project deletes:

```
project record  →  recording  →  transcript  →  understanding
                →  scene graph →  visual plan →  timeline
                →  render outputs
                →  project-scoped assets
```

Shared library assets survive, because they were never the user's data.

Generation cache entries keyed on content are the one subtlety: a cached image
produced from a user's prompt is derived from their content. Cache entries are
scoped so that project deletion evicts anything derived from that project's
inputs, even though the cache is otherwise global.

## Uploads go direct

`StorageProvider.signed_upload_url()` lets the browser send audio straight to
object storage.

The API never touches the bytes. That removes a bandwidth bottleneck, and it
means the request path handling the largest and most sensitive payload in the
product is one we do not operate. Signed URLs are short-lived — fifteen minutes
by default — and size-capped.

## Encryption

- **In transit:** TLS everywhere, including to the storage endpoint.
- **At rest:** server-side encryption on every bucket.
- **Signed URLs:** fifteen minutes by default. Long enough to download, short
  enough that a leaked link is close to worthless.

See `docs/SECURITY.md` for access control and consent.

## Cost

Media is the dominant storage cost in any video product, and the pipeline
produces a lot of it per project. The measures that keep this sane are all
structural rather than operational:

- ephemeral by default, so most projects cost nothing after a day;
- content-addressed generation caching, so the same image is paid for once
  (`GenerationRequest.cache_key()`);
- checksums on every object, so identical assets deduplicate;
- programmatic visuals stored as **specifications, not files** — a `Timeline`
  carries the animation spec and the renderer draws it, so three quarters of a
  typical project's visuals occupy a few kilobytes of JSON rather than megabytes
  of PNG.

That last one is a real architectural win and worth stating plainly: for the
majority of shots in a typical explainer, the "asset" is a paragraph of data.
