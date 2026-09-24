# Verifying Phase D

    cd "D:\Voice to Video"
    .\.venv\Scripts\Activate.ps1
    python scripts\verify_phase_d.py

One command, one terminal, **no environment to set**. It starts its own server
on port 8941 with its own throwaway database, so it can run while your own
server is up and it will not touch your sandbox. Seventeen checks, about eight
minutes on your card.

There is no manual version of this document. Phase C had one because the
procedure came first and the harness second; here the harness came first, and
writing out a dozen steps for a person to mistype would be inventing the problem
that took four attempts to solve last time.

---

## What Phase D actually changed

Phase C proved a computer could be handed a render and give one back. Almost
none of it was reachable from the editor.

**The Studio could not see a render its own server had not drawn.** This is the
one that mattered. `GET /projects/{id}/video` serves `render_job.output`, and
the device path settled a queue row and wrote no `render_job` document at all.
Phase C was verified by running `ffprobe` on the file in storage — which proved
the render and quietly skipped the question of whether the product could find
it. It could not. A customer's own computer would draw their video, upload it,
and the editor would show nothing to play.

**There was no cloud to choose instead.** `render_timeline` was a job kind only
a device could claim; nothing in the cloud could run one. "Cloud" in a picker
would have been a button that queued work no worker would ever take.

**Pairing needed an administrator first.** Somebody already signed in, on
another machine, minting a code to carry over. Right for provisioning a render
farm, wrong for one person with a laptop.

**An idle computer asked for work every five seconds, forever.** About
seventeen thousand requests a day, each answered "nothing".

---

## The four things the harness checks

### 1. Where a render runs

With no computer paired, Auto resolves to the cloud and says so. "On my
computer" is refused — not silently redirected — with the sentence saying why.
Start the desktop and Auto resolves to that machine **by name**.

The picker lives on the Studio's own `POST /v1/projects/{id}/render`, which is
where the render button already goes. The first attempt added a second route at
exactly that path and it could never fire: Starlette matches the first route,
the Studio's was already there, and the new one looked like the feature while
being unreachable.

### 2. The Studio can play what the desktop drew

The check Phase C did not make. It queues a device render, waits, then asks
`GET /projects/{id}/video` over HTTP and runs `ffprobe` on the bytes that come
back. It also checks the render appears in the project's history.

### 3. A settled machine wakes when somebody presses Render

**This check has been wrong twice, and both times the fix was to the
measurement.**

It first counted claims in a window after Render against a quiet window — which
cannot work, because a device that has just taken a job spends the next two
minutes rendering and makes no claims at all. The busy window always counted
fewer. It measured the opposite of what it claimed to.

Timing the *wake* instead is what found the real defect. The first design was an
adaptive poll ladder: widen from five seconds towards a minute when nothing is
happening. It hit its traffic target — a real machine was measured settling to a
52-second gap — and then took **49 seconds** to start a render somebody was
watching for. A `Retry-After` hint cannot shorten a sleep the device is already
in; it can only affect the poll *after* the next one, which is exactly the poll
whose lateness was the problem.

So the claim is now held open server-side for up to 25 seconds. The machine is
already connected when the job appears. The same measurement reads **0.3
seconds**, and an idle computer makes about 3,400 requests a day instead of
17,000.

The ladder is still there underneath, because a server that ignores the hold
answers immediately and the device must not then hammer it.

### 4. A render nobody claimed reaches the cloud

Two things had never run: the cloud handler, and the only path that produces
work for it.

The cloud job kind was added with a handler, a registration, and a test
asserting the two spellings agreed — and **nothing anywhere enqueued one**. It
could only be reached by a path that did not exist. Every one of those tests
passed on a feature that was unreachable, which is a more comfortable kind of
wrong than a failure and a worse one.

What gives it a real producer is the case the picker cannot cover on its own:
"Auto" asks whether a computer is available at the moment somebody presses
Render, which is the right question then and stops being the right answer ninety
seconds later, when the laptop is shut and put in a bag. The job sits pending —
not failed, not running, not wrong in any way the system can detect — and the
person watches a spinner until they give up.

So after `VTV_DEVICE_STRANDED_SECONDS` (seven minutes by default) of nobody
taking it, the work goes to the cloud. The check queues a render for a computer,
kills that computer before it claims, starts a worker, and waits for a playable
video.

### 5. Signing in through a browser

`vtv-desktop pair` with no `--code` now works the way desktop applications
normally sign in: it prints a URL and a short code, opens a browser, and waits.
You approve at `/devices/approve?code=…`, and the computer collects its
credential.

Shaped like the device authorisation grant every other desktop app uses — a
device code the computer keeps and never displays, a short user code the person
reads, and polling until somebody says yes. **No token is ever stored waiting to
be collected**: approval records only that a person said yes and for which
account, and the credential is minted when the waiting computer proves it holds
the device code.

`--code` still works, and is what a machine with no browser needs.

---

## What a real run found that 1713 unit tests did not

**A device job could retry forever.** A device handing a job back says "not my
fault, try somebody else". The queue took that as an instruction rather than a
request and re-queued the row unconditionally — no attempt ceiling on that path
at all. `recover()` had one, but `recover()` is for a worker that *crashes*; the
polite path around it had none, which is the worse shape of the same bug.

In the run that found it, a fixture produced a timeline two seconds short of its
own narration. The device drew it, failed, was handed it straight back, and did
that **493 times** — at full processor, on a machine that would have kept going
all night. Nothing reported it, because a pending job and a busy computer are
both entirely normal things.

**And the failure was classified as the machine's fault.** `RenderFailed`
defaults to an internal category, which is what a device consults to decide
between "this machine cannot" and "this cannot be done". A timeline that does
not cover its own narration is wrong identically on every computer in the world.

**The verification tool lied in the safe direction.** Its HTTP helper decoded
every response as UTF-8, which is right for JSON and fatal for an MP4. Polling
`/video` raised a decode error on the first success, the helper swallowed it as
a network failure, and the harness reported that the Studio could not find a
render the server log showed it serving with a 200 every two seconds. A false
negative in a verification tool is its own kind of lie, and slower to notice
than a false positive.

**A signed upload URL expired during the render it was issued for.** Minted
when the job was claimed and good for an hour. Fine for a thirty-minute video;
impossible for a longer one. A real sixty-minute render on the GTX 1650 took
seventy-one minutes, drew all 296 segments correctly, and died on
`403 object_expired`. The URL is now asked for at the moment there is a file to
send — an hour that starts when it is needed.

**A device's uploaded render had no retention class at all.** Every other object
reaches storage through `put`, which requires one; a signed upload lands bytes
on disk directly and was landing them unclassified. An object with no class is
one the sweep will never delete, so every video a customer's own computer
rendered was being kept forever — by a system whose stated design is that it
does not become a storage company. The class now travels inside the signed
token, so an upload URL cannot exist without one.

**The seed fixture rounded the wrong way.** `--seconds 20` with six-second shots
made three clips covering eighteen seconds. Every length used until then divided
exactly, so a broken fixture looked like working code for as long as nobody
typed an awkward number.

---

## If a check fails

Paste the whole output. Each check prints its evidence either way, so a failure
carries the sentence, status code or measurement that produced it. `--keep`
leaves the sandbox on disk and prints where; `server.log` inside it is the full
request log.
