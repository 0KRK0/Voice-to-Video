# Runbook — running Voice to Video on Windows

For a single developer machine, Windows 10 or 11, from `D:\Voice to Video`.

Every command below was executed against this exact revision before it was
written down. Where a step has a known caveat on Windows, the caveat is stated
rather than discovered by you at 1 a.m.

**What you end up with:** the Studio in your browser at `http://localhost:8000`,
and a real `.mp4` you can watch and judge.

---

## 0. What this run will and will not show you

Read this first, because it decides what you are actually evaluating.

With no AI provider credentials configured — which is the state of this
repository — the system runs at **two of the five rungs** of its visual sourcing
ladder:

| Rung | Cost | Available in this run |
| --- | --- | --- |
| 1. Draw it ourselves (charts, timelines, diagrams, maps) | $0 | **yes** |
| 2. Photograph from Openverse / Wikimedia commons | $0 | **yes** (no credential needed) |
| 3. Generated image | $0.04 | no — no credential |
| 4. Generated video | $0.25/s | no — no credential |
| 5. Typography | $0 | **yes** |

**Rung 2 only started working recently, and it is worth knowing why.** It was
configured, wired and unreachable: `SceneComposer` called the asset resolver
without `organisation_id`, which raised `TypeError` rather than a `VTVError`, so
the ladder could not even descend past it — and every test covering that rung
used a stub that accepted any arguments, so nothing failed. Until that was
fixed, `VTV_ASSET_SEARCH_ENDPOINT` was a setting nothing could use, and every
section of every video fell through to rung 5. If your earlier runs came out as
nothing but title cards, that is the reason, and it was not your configuration.

A licensed photograph brings an obligation with it: CC-BY requires a credit, the
renderer draws one over the shot, and the inspector now shows it under
**Credit** so you can see it before you publish rather than after.

Also unavailable without credentials: **transcription** (so the microphone path
will ask you to paste what you said) and **speech synthesis** (so narration is a
silent track of the right length, and the video has no voice).

So: you are evaluating **composition, pacing, typography, the drawn visuals, the
timeline and the editor** — which is most of the product — and you are *not* yet
evaluating generated imagery or voice. `/health` reports exactly this, and the
start screen says it on the page rather than letting you find out from a silent
video.

---

## 1. Prerequisites

Install these once. Run each command in **PowerShell**.

### Python 3.11 or newer

```powershell
winget install --id Python.Python.3.12 -e
```

Close and reopen PowerShell, then confirm:

```powershell
python --version
```

If `python` opens the Microsoft Store instead of printing a version, turn off
the alias: **Settings → Apps → Advanced app settings → App execution aliases →**
switch off both `python.exe` and `python3.exe` entries.

### ffmpeg *and* ffprobe

This is a hard requirement, not an optional extra — it is what encodes every
video. The code checks for **both binaries** and refuses to render if either is
missing.

```powershell
winget install --id Gyan.FFmpeg -e
```

Close and reopen PowerShell, then confirm **both**:

```powershell
ffmpeg -version
ffprobe -version
```

Both must print a version. If not, add the ffmpeg `bin` folder to your `PATH`
and reopen PowerShell.

### Node.js 20 or newer, and TypeScript

Node builds the frontend. The frontend ships **zero runtime dependencies**, but
it does need the TypeScript compiler to build, and `package.json` deliberately
declares no dependencies of its own — so `tsc` is installed globally.

```powershell
winget install --id OpenJS.NodeJS.LTS -e
```

Reopen PowerShell, then:

```powershell
node --version
npm install -g typescript
tsc --version
```

---

## 2. One-time setup

```powershell
cd "D:\Voice to Video"

python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked with a script-execution error, allow it for your user
once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Then install the runtime dependencies and the package itself:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

`requirements.txt` is exact-pinned to the versions this repository is tested
against. It covers pydantic, starlette, uvicorn, python-multipart, pillow, numpy
and httpx. It does **not** cover ffmpeg — that is a system binary, installed in
step 1.

### Build the frontend

```powershell
npm --prefix apps\web run build
```

You should see it finish with something like `built 34 assets · 382.7 KB total`
and `runtime dependencies: none`. If you skip this step the API answers with a
503 page telling you to run exactly this command.

### Create your configuration

```powershell
Copy-Item .env.example .env
```

This file is read at startup. That is worth stating plainly because for a long
time it was not: `.env` was documented everywhere and loaded by nothing, so
credentials put there reached the process only if you also exported them in the
shell. Real environment variables still win over the file.

You do **not** need to edit `.env` for a local run — every value has a working
development default. Leave every provider credential blank; that is what
produces the honest degradation described in section 0.

---

## 3. Initialise the databases

```powershell
python -m vtv.migrate
```

Expect three lines, each naming the migration and the defect it closes:

```
applied 0001 projects_have_a_tenant — ...
applied 0002 organisations_denormalise_hot_columns — ...
applied 0003 projects_record_their_outcome — ...
```

This creates `var\vtv.db`, `var\queue.db`, `var\shared.db`, `var\directory.db`,
`var\audit.db` and `var\usage.db`, plus `var\storage\` for the media itself.
Reasoning lives in the databases; pixels live on disk.

---

## 4. Start the two processes

**You need both.** The API deliberately registers no job handlers — it can only
*enqueue* work — so with no worker running, a render is accepted and then sits
in the queue forever. This is by design: it makes it impossible for the API to
quietly do heavy work inside a request.

### Terminal 1 — the API

```powershell
cd "D:\Voice to Video"
.\.venv\Scripts\Activate.ps1
python -m uvicorn --factory vtv.api.app:create_app --host 127.0.0.1 --port 8000
```

### Terminal 2 — the worker

```powershell
cd "D:\Voice to Video"
.\.venv\Scripts\Activate.ps1
python -m vtv.worker
```

The worker prints a startup line listing the job kinds it has bound:
`regenerate_visual`, `render_document`, `render_recording`, `render_scope`,
`retention_sweep`, `revise_script`.

### Confirm the system agrees with you about what it can do

Open a third terminal:

```powershell
curl.exe http://127.0.0.1:8000/health
```

You should see `"status":"ok"`, `"environment":"development"`,
`"rendering":true`, `"real_asset_search":true`, and the generation capabilities
reported as `false`. That last part is the system being honest, not broken.

---

## 5. Get into the Studio

The browser client authenticates with a bearer API key — there is no cookie
session. In development the API also accepts unauthenticated local calls as a
"Development" organisation on the Enterprise tier, which is how you mint your
first key with no bootstrap script:

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/api-keys -H "Content-Type: application/json" -d "{\"name\":\"local studio\",\"role\":\"admin\"}"
```

**`"role":"admin"` matters.** The default is `service`, which is the right
default for a machine integration and the wrong one for a person sitting in the
Studio: it grants read, create and update but *not* delete, so the Delete link
in the project list answers "You do not have permission to do that." every time.
The server is right to refuse it; the interface should not be offering it, and
that is a known gap.

Copy the `secret` from the response. It is shown once and never again.

Then open your browser at:

```
http://localhost:8000/?token=PASTE_THE_SECRET_HERE
```

The token is consumed from the address bar and stored, so the URL cleans itself
up. You can also just open `http://localhost:8000` and paste the key into the
sign-in field.

Because the Enterprise tier is used for the development organisation, you will
not hit a quota while evaluating.

---

## 6. Make a video

This is the path that works end to end with no AI credentials.

1. On the start screen, choose **"Write or paste a script"**.
2. Paste several paragraphs, **separated by blank lines** — each paragraph
   becomes a narration block and gets its own visual. Three to six paragraphs is
   a good first test. Use something with a date, a number, a place and a
   comparison in it; that is what exercises the drawn-visual rungs rather than
   falling through to typography.
3. Click **Open the editor**. You land in the Studio: script on the left, the
   visual for each line beside it, the timeline below, the inspector on the
   right.
4. The visuals start as `PLANNED`. Let the planner settle, then press
   **Render** (top right).
5. Watch the render progress. On a normal laptop, expect roughly **40–60
   seconds for about 30 seconds of finished video** at standard quality,
   1920×1080, 30 fps.
6. When it reports ready, use **Export** to download the `.mp4`.

If you would rather not use the browser at all, the same path over HTTP is:

```powershell
$B = "http://127.0.0.1:8000"
$H = @{ Authorization = "Bearer PASTE_THE_SECRET_HERE"; "Content-Type" = "application/json" }

$p = (Invoke-RestMethod -Method Post -Uri "$B/v1/projects" -Headers $H -Body '{"title":"First run"}').project_id
Invoke-RestMethod -Method Post -Uri "$B/v1/projects/$p/script" -Headers $H -Body '{"text":"First paragraph.\n\nSecond paragraph.\n\nThird paragraph."}' | Out-Null
Invoke-RestMethod -Method Post -Uri "$B/v1/projects/$p/visual-units" -Headers $H -Body '{}' | Out-Null
Invoke-RestMethod -Method Post -Uri "$B/v1/projects/$p/render" -Headers $H -Body '{}'

# poll until status is "ready"
Invoke-RestMethod -Uri "$B/v1/projects/$p/renders" -Headers $H

# then download
Invoke-WebRequest -Uri "$B/v1/projects/$p/video" -Headers $H -OutFile "$env:USERPROFILE\Downloads\vtv.mp4"
Invoke-WebRequest -Uri "$B/v1/projects/$p/captions.vtt" -Headers $H -OutFile "$env:USERPROFILE\Downloads\vtv.vtt"
```

The step that is easy to miss is `visual-units` — the render needs a visual plan
to exist first, and without it you get a `not_found` rather than a video.

---

## 7. What to judge, and what to ignore

Since you are the one evaluating the output, these are the things this run can
honestly tell you about:

**Judge these.** Does each visual actually mean what its line says? Do the
transitions land on the sentence boundaries? Is the pacing right, or does a line
sit too long? Are the drawn visuals — timelines, comparisons, charts — good
enough to put in front of someone? Is the typography good? Does the timeline
edit the way you expect, and does undo work? When it refuses something, is the
refusal clear?

**Do not judge these yet.** There is no voice, so the audio is silent and the
timing comes from a speaking-rate estimate rather than measured speech. There is
no generated imagery, so anything that would have been a generated photo or
video has degraded to a drawn or typographic visual instead. Both are stated in
`/health` and on the start screen.

**Judge whether the degradation is honest.** This is worth its own line. When the
system cannot do something, does it tell you, or does it hand you something
plausible and quiet? That behaviour is testable today and is arguably the most
important thing to check on a first run.

---

## 8. Stopping, restarting, resetting

Stop either process with **Ctrl+C**. The worker drains: it stops claiming new
jobs, finishes what it is holding, and hands anything unfinished back to the
queue so the next worker picks it up. Give it a few seconds rather than killing
the window.

To start over from nothing:

```powershell
Remove-Item -Recurse -Force var
python -m vtv.migrate
```

That deletes every project, render and uploaded file. There is no backup
mechanism — see the limitations below.

---

## 9. If something goes wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| Browser shows "The editor is not built" | frontend never compiled | `npm --prefix apps\web run build` |
| Render accepted, never finishes | no worker running | start Terminal 2 |
| `rendering: false` in `/health` | ffmpeg or ffprobe not on `PATH` | reinstall, reopen PowerShell, check **both** binaries |
| `not_found` when rendering | no visual plan yet | POST `/v1/projects/{id}/visual-units` first, or use the editor |
| `python` opens the Microsoft Store | Windows app alias | Settings → App execution aliases → turn off `python.exe` |
| `.venv\Scripts\Activate.ps1` blocked | execution policy | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| `tsc: not found` during build | TypeScript not global | `npm install -g typescript` |
| Port 8000 in use | something else has it | add `--port 8001` to uvicorn and use that port in the URL |

For anything else, the API's `/health` endpoint and the worker's log lines are
structured JSON and name the specific thing that failed.

---

## 10. Where this run sits against production

Accurate as of `docs/FINAL_GAP_AUDIT.md`, and consistent with your own summary:

| | State |
| --- | --- |
| Core software platform | Built. 1,080 backend tests, 55 frontend unit tests, 9 live wire-contract tests. |
| Production runtime | Validated. Real image, two workers, 7 concurrent renders split 3/4, zero double-processing, graceful drain on SIGTERM. |
| Real cloud database deployment | Not done. PostgreSQL and its row-level security are implemented and tested; the running deployment is SQLite. |
| Operational readiness | Not done. No backup or restore, no alerting, no load testing. |
| Commercial enforcement | 7 of 8 quota dimensions enforced. `storage_gb` is unmetered and the API reports `enforced: false` rather than showing a limit it does not apply. |
| Actual AI | Not connected. No vendor call has ever been made, so every cost figure is adapter-declared and the pricing tiers are proposed. |
| Actual video quality | Not evaluated. This runbook exists so you can be the first to do it. |

**A local run cannot change the bottom two rows.** Running this on your machine
tells you about composition, pacing and the editor. It cannot tell you whether
generated imagery is good, because none is generated, and it cannot tell you
what a project costs, because nothing is billed. Those need credentials and an
invoice, in that order.
