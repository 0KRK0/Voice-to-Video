# Deployment

## What this substantiates, and what it does not

The architectural claim this directory exists to prove is narrow and important:
**the application runs correctly as more than one process.** Before the
2026-08-13 audit it did not — the API rendered inside its own event loop, held
finished videos in a Python dict, and kept rate-limit buckets and quota
reservations in process memory. Two replicas would have enforced two independent
rate limits and returned 404 for each other's videos.

It is **not** a production cloud topology. There is no managed database, no S3,
no CDN, no autoscaling, no TLS termination and no secret manager. Those are
environment decisions.

It is also not a *multi-machine* topology, and that limit is real rather than
stylistic — see **Scaling ceiling** below.

## Topology

```
                    ┌──────────┐
   clients ────────▶│  ingress │
                    └────┬─────┘
                         ▼
              ┌────────────────────┐   stateless, N replicas
              │  api  (×2 default) │   enqueue + read only
              └───┬────────────┬───┘
       enqueue    │            │  read
                  ▼            ▼
        ┌──────────────────────────────────────┐
        │  one shared volume: /var/lib/vtv     │
        │                                      │
        │  queue.db    durable jobs            │
        │  vtv.db      projects, documents     │
        │  shared.db   rate-limit buckets      │
        │  storage/    rendered media          │
        └──────┬───────────────────────────────┘
               │ claim
               ▼
      ┌───────────────────┐
      │ worker (×2)       │
      │ ingest → ground   │
      │ → narrate → render│───▶ storage/
      └───────────────────┘
```

Everything correctness-critical is in that one shared volume. That is the whole
design: no process owns state, so any replica can answer any request and any
worker can claim any job.

## Running it

```sh
export VTV_SIGNING_KEY=$(openssl rand -hex 32)
docker compose -f deploy/docker-compose.yml up --build
```

`VTV_SIGNING_KEY` has no default and the compose file refuses to start without
it. It must be **the same value on every replica**: it signs media URLs, so a
per-process key means a download succeeds or 403s depending on which pod the
load balancer picked. `wiring.build` raises in production when it is missing,
rather than generating one and appearing to work.

`migrate` runs to completion before `api` or `worker` start. Both then run two
replicas, which is the point.

## Health endpoints

| Path | Answers | Used by | Failure means |
| --- | --- | --- | --- |
| `/health/live` | can this process answer at all | container `HEALTHCHECK`, K8s liveness | restart me |
| `/health/ready` | should it receive traffic | compose healthcheck, K8s readiness, load balancer | stop routing to me |
| `/health` | full capability report | operators, humans | nothing automatic |

They are separate deliberately. A process that is alive but cannot reach its
database should stop receiving traffic, not be killed and restarted — restarting
it does not fix the database and does lose in-flight work. A liveness probe that
touches the database converts a dependency's bad minute into a cluster-wide
restart loop.

`/health/ready` checks: the repository responds, the queue responds, storage is
writable, and **no migrations are outstanding**. That last one matters — a
replica that rolled out ahead of the migration job serving requests against a
schema its code does not expect is how a partially-applied migration becomes a
data-corruption incident instead of a 503.

## Scaling

- **API**: stateless. Add replicas. Nothing correctness-critical lives in
  process memory any more, which is what made this possible.
- **Worker**: add replicas or raise `--concurrency`. Rendering is CPU-bound; one
  concurrent render per core is a sensible ceiling.
- **Queue**: at-least-once delivery with idempotent effects. Adding workers
  cannot duplicate work — the claim is a single guarded `UPDATE`, and billing
  settles against an idempotency key.

### Scaling ceiling — stated plainly

**This topology scales to one machine and no further.**

The queue, the rate limiter and the repository are SQLite files on a shared
volume. That is genuinely correct across processes on one host: every
correctness-critical operation is a single atomic statement under `BEGIN
IMMEDIATE`, and the tests in `tests/test_worker_runtime.py` drive real separate
API and worker objects over shared files to prove it.

It is **not** correct across a network filesystem with weak locking (NFS, most
CSI drivers), and it will contend under write load long before a real database
would. Adding a second node to this compose file would not fail loudly; it would
produce silent lock timeouts and, on a filesystem that lies about `fcntl`,
corruption.

Removing that ceiling is **P1-5** in `docs/REMEDIATION.md` — PostgreSQL with
row-level security. The correctness properties above are already written the way
Postgres wants them, which is why it is P1 and not a rewrite.

An earlier draft of `docker-compose.yml` declared `postgres` and `redis`
services and set `VTV_DATABASE_URL: postgresql://…`. The code implements
neither, and `repository_path()` silently fell back to a local SQLite file — so
that file looked like a distributed deployment while every replica wrote to its
own private database. It has been removed, `repository_path()` now raises on any
scheme it does not implement, and `tests/test_deployment.py` asserts the compose
file starts no service the application never contacts.

## Shutdown

`SIGTERM` stops the worker claiming new jobs and waits up to
`SHUTDOWN_GRACE_SECONDS` (120s) for in-flight ones. `stop_grace_period` is 150s,
deliberately larger, so the orchestrator does not kill a worker that is still
draining; `tests/test_deployment.py` asserts that inequality holds.

Work that outlives the grace period is reclaimed by the next worker, so a
rolling deploy exercises the same recovery path a crash does — on every deploy
rather than only during an incident.

## Images and supply chain

`requirements.txt` pins every runtime dependency to the exact version the test
suite ran against. Hash verification (`pip install --require-hashes`) is **not**
in place; `requirements.lock.md` records why (this build environment has no
package index) and the two-line change that closes it.

Two further reproducibility gaps, named so they are not mistaken for done:

- the base image is pinned by tag, not by digest — a tag moves;
- `apt-get install` resolves ffmpeg and the Noto fonts against Debian's current
  archive at build time, so builds a month apart can differ.

## What is not proven here

`tests/test_deployment.py` checks that these artifacts agree with the code:
the entrypoints resolve to real objects, the healthcheck targets routes the API
serves, the install step reads a file that exists, the compose file starts
nothing the code never contacts, and the grace periods are ordered correctly.

It does **not** run Docker — Docker is unavailable in this environment. So the
following are **NOT PROVEN**, and are the first things to check on a machine
that has a daemon:

- the image actually builds
- ffmpeg and the Noto font set are present and sufficient at runtime
- two API replicas and two workers come up healthy against one volume
- a rolling restart of a worker mid-render completes the render
