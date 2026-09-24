# Verifying Phase C on a real machine

Everything below was run end to end in a Linux container against a real uvicorn
server, real sockets and real files before it was written down. It found five
bugs, all of them invisible to the unit suite, all now fixed. See
**What this found** at the end.

What it does *not* cover is your graphics card: the container has none, so every
render below was drawn on the processor. Step 10 is the part only your machine
can answer.

## Before you start

Three terminals, all in `D:\Voice to Video`, all with the venv activated:

```powershell
cd "D:\Voice to Video"
.\.venv\Scripts\Activate.ps1
```

Call them **T1 (server)**, **T2 (desktop)** and **T3 (commands)**.

### Environment

The server, the seeder and the desktop must agree on where the databases and
storage live, or they will each quietly create their own and nothing will find
anything. Run this in **all three** terminals:

```powershell
$env:VTV_ENV = "development"
$env:VTV_STORAGE_ROOT = "D:\Voice to Video\var\verify\storage"
$env:VTV_DATABASE_URL = "sqlite:///D:/Voice to Video/var/verify/vtv.db"
$env:VTV_SIGNING_KEY = "verify-only-not-a-real-secret"
$env:VTV_DESKTOP_HOME = "D:\Voice to Video\var\verify\desktop"
$env:PYTHONPATH = "D:\Voice to Video\src"
```

`VTV_ENV=development` matters: the API only serves unauthenticated requests as
the development tenant when it is *not* production. Without it every command
below returns `Please sign in to continue`.

`VTV_DESKTOP_HOME` keeps the device identity inside the repo's `var` directory
instead of your real user profile, so this whole exercise is throwaway.

### Which terminals actually need it

**T1 and T3 do. T2 does not.**

The server and the seeding script read the database and storage from the
environment, so those two must agree or they use different databases. The
desktop reads neither — it talks to the server over HTTP and is told the URL on
the command line. All `VTV_DESKTOP_HOME` changes for T2 is *where it keeps its
own identity and part-finished work*.

So the simplest thing, and the one least able to go wrong: **leave T2's
environment alone entirely.** Its identity and workspace then live in the
platform default, it prints that path in its banner every time it starts, and
you inspect that path. A T2 that is sometimes configured and sometimes not is
how a resume ends up running in a different directory from the checkpoints it
was supposed to reuse — which looks like a broken resume and is not one.

Whatever you choose, **read the `workspace :` line in T2's banner** and inspect
*that* directory. It is printed for exactly this reason.

---

# Test 1 — the happy path

## 1. Seed a project (T3)

Phase C verifies the *device* path, not the content pipeline, so this writes a
real timeline directly rather than transcribing anything. What the device
receives afterwards is indistinguishable from a pipeline-produced job — same
document, same reader.

```powershell
python scripts\seed_device_job.py --seconds 30 --photos
```

**Expected:** an organisation id, a project id, a timeline id, and a `curl`
line. **Copy the project id.**

`--photos` matters: without it the video is typography only, and the parts you
most want to exercise — signed asset URLs, digest verification, the download
cache, and photographs routing to the graphics card — never run.

## 2. Start the API (T1)

```powershell
python -m uvicorn --factory vtv.api.app:create_app --host 127.0.0.1 --port 8000
```

**Expected:** `Uvicorn running on http://127.0.0.1:8000`.

## 3. Do you need a worker? (No.)

Not for this. A device *is* the worker — it claims from the same queue by the
same mechanism a cloud worker uses. Leave the worker out; if a job only
completes when a worker is running, that is a finding worth reporting.

## 4. Check the machine, then get a pairing code (T2, then T3)

**T2:**
```powershell
python -m vtv.desktop.cli hardware
```

**Expected:** your platform, cores, memory, and either
`graphics : NVIDIA GeForce GTX 1650 (opengl) — verified` or a line saying
exactly why not. `will run` should list `This computer's graphics card`.

If it says **NOT usable**, stop and paste that line — the rest of the test will
run on the processor and step 10 will prove nothing.

**T3:**
```powershell
curl.exe -s -X POST http://127.0.0.1:8000/v1/devices/codes
```

**Expected:** `{"code":"ABC-DEF","expires_at":"...","instructions":"..."}`.
The code lasts ten minutes and works once.

## 5. Pair (T2)

```powershell
python -m vtv.desktop.cli pair --code ABC-DEF --server http://127.0.0.1:8000 --name "Konar's desktop"
```

**Expected:**
```
paired as Konar's desktop
identity stored at ...\var\verify\desktop\device.json
Start rendering with:  vtv-desktop run
```

## 6. Verify what the device can and cannot do (T3)

```powershell
curl.exe -s http://127.0.0.1:8000/v1/devices
curl.exe -s http://127.0.0.1:8000/v1/devices/capacity
```

**Expected:** one device, `"state": "idle"`, your real hardware, and
`"targets": ["local_gpu","local_cpu"]` if the card verified. Capacity should
read `{"paired":1,"available":1,"accelerated":1,...}`.

Now prove the device cannot reach anything else. Take the token out of
`var\verify\desktop\device.json` and try to read the project with it:

```powershell
$tok = (Get-Content "$env:VTV_DESKTOP_HOME\device.json" | ConvertFrom-Json).token
curl.exe -s -o NUL -w "%{http_code}`n" -H "authorization: Bearer $tok" http://127.0.0.1:8000/v1/projects/<PROJECT_ID>
```

**Expected: `401`.** A device token is not an account credential. If this
returns 200, stop and tell me — that is the one failure in this document that
would be serious.

## 7. Start the desktop *before* queueing (T2)

```powershell
python -m vtv.desktop.cli run
```

**Expected:** a banner, your hardware, then `waiting for work`.

Order matters. A job is only queued for computers that have been *seen
recently* — deliberately, because queuing work for a switched-off machine
produces a render that never happens and never fails. If you queue first you
will correctly get "No computer on this account is available to render right
now."

## 8. Queue the render (T3)

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/v1/projects/<PROJECT_ID>/render/device
```

**Expected:** `{"render_job_id":"rnd_...","job_id":"prj_..."}`.

Within about five seconds T2 should start working. **T1** should show, in order:

```
POST /v1/devices/claim HTTP/1.1" 200 OK
POST /v1/devices/progress HTTP/1.1" 200 OK      (every ~20s)
POST /v1/devices/complete HTTP/1.1" 200 OK
```

Steps 9, 10 and 11 all happen inside this one run: the assets are downloaded
through the expiring signed URLs, verified against their digests, drawn by the
same `FfmpegRenderer` the cloud uses with the same per-segment routing, and the
finished file is uploaded through the signed upload URL.

## 12. Progress and completion, from the server's side (T3)

While it runs:

```powershell
curl.exe -s http://127.0.0.1:8000/v1/devices | python -m json.tool
```

**Expected:** `"state": "busy"` during the render, `"idle"` after, and
`last_seen_at` advancing.

Open a browser at **http://127.0.0.1:8000** if you want to see the app; the
device flow above is API-only, so the browser is optional here.

## 13. Verify the file (T3)

```powershell
$mp4 = Get-ChildItem "$env:VTV_STORAGE_ROOT" -Recurse -Filter *.mp4 | Sort-Object LastWriteTime | Select-Object -Last 1
ffprobe -v error -print_format json -show_format -show_streams $mp4.FullName
```

**Expected:** duration within a few hundredths of 30 seconds, one `h264` video
stream at 1920x1080, one `aac` audio stream. In the container this produced
exactly `duration: 24.000000`, `h264 1920x1080`, `aac`.

## 14. Device status (T3)

```powershell
curl.exe -s http://127.0.0.1:8000/v1/devices/capacity
```

**Expected:** `available: 1`, and `accelerated: 1` if your card verified.

## 15. Revoke (T3), then watch T2

```powershell
$dev = (curl.exe -s http://127.0.0.1:8000/v1/devices | ConvertFrom-Json).devices[0].device_id
curl.exe -s -X DELETE "http://127.0.0.1:8000/v1/devices/$dev"
curl.exe -s http://127.0.0.1:8000/v1/devices/capacity
```

**Expected:** the delete returns `"state": "revoked"`, capacity drops to
`paired: 0`, and **T2 stops** with a message telling you to pair again. It must
not sit there polling — a revoked device that kept retrying would look alive to
its owner forever.

---

# Tests 2 and 3 — run them with one command

    python scripts\verify_recovery.py

That is the whole thing. No environment to set, no terminal that has to have
been prepared, nothing to time by hand — it starts its own server on its own
port with its own throwaway database, so it can be run while your own server is
up and it will not touch your sandbox. It seeds a project, pairs a device, runs
the desktop as a child process, kills it on a clock, waits out the lease,
resumes, and prints PASS or FAIL for each test.

Run one at a time with `--test resume` or `--test offline`. Expect about
fifteen minutes for both on a machine with a working graphics card, longer on
one without.

Three attempts at doing this by hand failed, and not one of them failed because
the software was wrong. They failed on the kill landing too late, on two
terminals holding different environments, and on a measurement taken after a
delivered job had already deleted the directory being measured. None of those
is what is under test, so none of them should be a person's job.

**What the resume test checks that a person watching would not.** A delivered
job removes its own scratch directory, so looking afterwards finds nothing —
which is what defeated two of the attempts. Instead it samples the finished
segments' modification times four times a second for the whole of the resume
and fails if any of them ever moves. It also records the most segments ever in
the workspace at once, and fails if that never exceeds what the kill left
behind: on the fourth attempt the resume ran with a different workspace and
redrew all 120 seconds while the checkpoints sat untouched somewhere else, and
a checkpoints-untouched check on its own calls that a pass.

**What the offline test does and does not simulate.** It stops the server
rather than disabling an adapter, so the device gets connection-refused where a
real Wi-Fi drop would hang until timeout. It is the same code path — an
exception out of the HTTP call — but not the same timing, so the manual adapter
version below is still worth one run.

The manual procedure is kept below, because when the script says FAIL the next
question is which step, and that is answered by doing it a step at a time.

---

# Test 2 by hand — kill the desktop mid-render

## Set it up so the kill can actually land

Three things have to be true or this test measures nothing, and all three were
got wrong on the first attempt:

**A longer video.** A 30-second render finishes in ~21 seconds on eight cores.
There is almost no window to kill it in, and if it completes the job directory
is deleted — correctly — leaving nothing to inspect. Seed two minutes.

**One worker.** Without `--workers 1` the segments render in parallel, so a
kill leaves several half-written `.part` files and no clean "this one is
finished, that one is not". Sequential segments make the checkpoint obvious.

**One job in the queue.** Every `render/device` call queues another. Several
pending jobs means the resume run may claim a *different* one and finish it,
which looks like success and proves nothing about resuming.

```powershell
. .\scriptserify-env.ps1 -Fresh          # T1, then re-run without -Fresh in T2/T3
python scripts\seed_device_job.py --seconds 120 --photos
```

Start the server, pair, start `run`, and queue **exactly one** job.

## A. Let it render for about 45 seconds

## B. Kill it — and Ctrl-C will not do it

Ctrl-C now asks the loop to **finish the current job** and then stop, which is
the correct behaviour and the opposite of what this test needs. Kill it for
real: close the terminal window, or use the trash-can icon in VS Code's
terminal, or `Stop-Process` on the python process from another terminal.

## C. Look at the checkpoint

Use the path T2 printed as `workspace :`, not one you assume:

```powershell
$work = "C:\Users\$env:USERNAME\AppData\Local\VoiceToVideo\work"   # or whatever T2 printed
Get-ChildItem "$work\jobs" -Recurse -Filter seg-*.mp4 | Select Name, Length, LastWriteTime
```

**Expected:** finished `seg-0000N.mp4` files and one `seg-....part.mp4`. The
`.part` suffix is the whole point — a half-written segment can never be
mistaken for a finished one.

**Write down `seg-00000.mp4`'s `LastWriteTime` to the second.**

## D. Wait 70 seconds, then resume

The server will not hand the job to anybody while the previous claim might
still be alive. Then:

```powershell
python -m vtv.desktop.cli run --workers 1 --max-jobs 1
```

**Expected:** it claims the *same* job, and when it finishes:

```powershell
Get-ChildItem "$env:VTV_DESKTOP_HOME\work\jobs" -Recurse -Filter seg-*.mp4 |
  Select Name, LastWriteTime
```

**`seg-00000.mp4`'s `LastWriteTime` must be unchanged.** That is the assertion.
Not "it finished" — finishing proves only that it rendered. An unchanged
timestamp proves it *reused* the segment rather than redrawing it, which is the
difference between a closed laptop costing forty seconds and costing the whole
render.

In the container this was sampled forty times across the entire resume and
never moved once.

If the directory is empty when you look, the job finished before you killed it
— a delivered job deletes its own scratch. Seed a longer video and try again.

# Test 3 by hand — pull the network mid-render

Re-pair, start `run`, queue a job, and let it claim.

**B.** Disable your network adapter (or unplug) **after** T2 has claimed —
after you see `POST /v1/devices/claim` in T1.

**C. What continues, and what does not.** Stated in advance so this is a
prediction and not a description:

| | Continues offline? |
|---|---|
| Drawing frames and encoding segments | **Yes** — the renderer touches no network |
| Assets already downloaded | **Yes** — they are on disk, digest-verified |
| Assets *not* yet downloaded | **No** — the job fails and is handed back |
| Progress reports | **No** — they fail silently, by design |
| The lease | **Expires after ~60s of silence** |
| Uploading the result | **No** — retried when the network returns |
| Reporting completion | **Retried up to 8 times with backoff** |

So a short outage is invisible. An outage longer than the lease means the server
gives the job to somebody else, and this device's work is discarded when it
finally reports — correctly, because by then another machine owns it.

**D.** Re-enable the network.

**E. Expected:** if the outage was **shorter than ~60s**, the render finishes,
uploads, and T1 shows `complete 200 OK`. If it was **longer**, expect the
completion to be accepted as `false` — that is not an error, it means the lease
expired and the job was reassigned. Either way T2 must keep running and go back
to `waiting for work`.

Paste T2's last ten lines and T1's log either way.

---

# If something fails

Paste, in this order:

1. The **exact command** and its full output.
2. **T1's log** from the last `POST /v1/devices/claim` onwards.
3. `curl.exe -s http://127.0.0.1:8000/v1/devices | python -m json.tool`
4. `Get-ChildItem "$env:VTV_DESKTOP_HOME\work\jobs" -Recurse | Select Name,Length,LastWriteTime`
5. For a render failure, the `reason` line from T2 — it is one sentence and it
   is the actual cause.

---

# What this found

Six bugs, none of which the 1600-test suite could see, because every one of
them lives exactly where two correct pieces meet.

1. **Signed URLs are relative.** `/media/<bucket>/<key>?token=…`, because the
   server does not know its own public hostname. Handed to an HTTP client not
   bound to the server, that is not a URL. Every unit test substituted `fetch`.

2. **Nothing served the upload URL.** `signed_upload_url` minted
   `/media/upload/…` and the application had a download route and no upload
   route. Every device upload would have fallen through to the single-page-app
   handler and been recorded as a successful render of an HTML document.

3. **`httpx.AsyncClient` cannot stream an open file.** The obvious spelling
   type-checks and raises *"attempted to send an sync request with an
   AsyncClient instance"* on the first real upload.

4. **The offer route read the project id from the wrong place.** `owned_project`
   — the single tenancy check — reads `path_params`, and the id was in the body,
   so the first real request raised `KeyError`. Moving it to
   `/v1/projects/{id}/render/device` was the fix; adding a private ownership
   check here would have been the bug.

5. **Nothing reaped abandoned device jobs.** `recover()` is called by the cloud
   worker's maintenance loop and by nothing else — and the entire premise of
   local execution is a deployment with no cloud worker. A device killed
   mid-render held its job until somebody restarted the API. Claiming now reaps
   first, which puts recovery on the one path that cares.

6. **`python -m vtv.desktop.cli` did nothing at all.** No
   `if __name__ == "__main__":` guard, so running the module imported it,
   defined every function and exited — silently, status 0, no output whatsoever.
   The `vtv-desktop` console script hides it completely, so it is invisible to
   anyone who pip-installs the package and unavoidable for anyone running from a
   source checkout, which is exactly what this procedure asks for. Caught by a
   subprocess test, because importing the module and calling `main()` works fine
   either way — which is how it survived being looked at directly.

And one wrong-message defect: a customer who had simply not started their
desktop app was told *"We could not use that material under its licence."*
`PolicyViolation` carries a class-level `user_message`, and the sentence written
at the raise site never reached the browser. Worse than an unhelpful error — a
confident, specific, wrong diagnosis.
