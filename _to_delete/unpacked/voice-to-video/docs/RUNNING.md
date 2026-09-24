# Running it

## What works with nothing installed but Python

```bash
pip install -e ".[dev,server,render,providers]"
make check     # lint, types, schema drift, 254 tests, evaluation corpus
make demo      # golden path end to end → ./var/demo/demo.mp4
make serve     # API + web UI on http://localhost:8000
```

`make demo` needs **ffmpeg** on the path. Everything else in that list runs with
Python alone.

## What is real without any credentials

More than you would expect, because most of the intelligence is deterministic:

| Stage | Without credentials |
| --- | --- |
| Voice capture | **Real** — browser `MediaRecorder`, magic-byte sniffing, ffprobe |
| Speech to text | **Not real** — see below |
| Understanding | **Real** — the rule-based engine, no model needed |
| Scene engine | **Real** — deterministic grouping |
| Visual Director | **Real** — the rule-based director |
| Asset search | Adapter written; needs network |
| Generation router | **Real** — cache, retry, fallback, budgets, cost ledger |
| Animation engine | **Real** — six primitives drawn with PIL |
| Composition | **Real** |
| Timeline | **Real** |
| Captions | **Real** — SRT and WebVTT |
| Rendering | **Real** — ffmpeg produces a playable MP4 |
| Storyboard UI | **Real** |
| Persistence | **Real** — SQLite |
| Evaluation | **Real** |

## Transcription without a provider

The system will not invent words. With no speech-to-text credential configured,
`ScriptedSpeechToTextProvider` is wired in, and it **refuses to run unless you
supply the text of what you said**. What it does is align that text to the real
pauses in your real audio, detected with ffmpeg.

That is a development path, and everything it produces is stamped
`provider: "scripted-dev"` so a development transcript cannot be mistaken for a
real one — in the database, in the event stream or in an evaluation run.

To get real transcription:

```bash
export VTV_SPEECH_TO_TEXT_ENDPOINT=https://.../v1/audio/transcriptions
export VTV_SPEECH_TO_TEXT_API_KEY=...
```

`HttpSpeechToTextProvider` speaks the widely-implemented OpenAI-compatible
transcription contract, which several hosted and self-hosted Whisper deployments
accept unchanged.

## Turning on the rest

Every capability is a pair of environment variables, and every one is optional.
`GET /health` reports exactly which are live:

```json
{
  "capabilities": {
    "real_transcription": false,
    "real_understanding": false,
    "real_visual_direction": false,
    "real_asset_search": true,
    "real_image_generation": false,
    "real_video_generation": false,
    "rendering": true
  }
}
```

```bash
# Understanding and visual direction via a language model
VTV_TEXT_GENERATION_ENDPOINT=https://.../v1
VTV_TEXT_GENERATION_API_KEY=...
VTV_TEXT_GENERATION_MODEL=...

# Image and video generation
VTV_IMAGE_GENERATION_ENDPOINT=https://.../v1
VTV_IMAGE_GENERATION_API_KEY=...
VTV_VIDEO_GENERATION_ENDPOINT=https://.../v1
VTV_VIDEO_GENERATION_API_KEY=...
```

With none of them set, the Visual Director's ladder simply descends to visuals
the system draws itself. The video still renders, the narration is still heard,
and every descent is recorded as a `DegradationStep` you can see in the
storyboard. That is the design working, not the design failing.

## The API

```
GET  /                                    web UI
GET  /health                              capabilities and provider health
POST /v1/projects                         create a project
POST /v1/projects/{id}/recordings         upload audio, returns a job id
GET  /v1/projects/{id}                    status and progress
GET  /v1/projects/{id}/events             server-sent progress events
GET  /v1/projects/{id}/storyboard         scenes, strategies, reasons, costs
GET  /v1/projects/{id}/scenes/{sid}/thumbnail.png
POST /v1/projects/{id}/scenes/{sid}/revise
GET  /v1/projects/{id}/video              the MP4
GET  /v1/projects/{id}/captions.vtt       sidecar captions
POST /v1/visualize                        text in, a visual plan out
POST /internal/sweep                      retention sweep
```

`POST /v1/visualize` is the Stage 20 foundation. It skips capture and
transcription, and returns the plan rather than a file:

```bash
curl -s localhost:8000/v1/visualize -H 'content-type: application/json' -d '{
  "input": {"type": "text", "content":
    "The world population grew from one billion people to eight billion people."}
}' | jq '.scenes[0].visual'
```

```json
{ "kind": "programmatic", "primitive": "chart" }
```

## Rendering

`FfmpegRenderer` is the working implementation and is the default. It composes
every frame itself and pipes raw pixels into a single ffmpeg process that muxes
the narration — so transitions, captions and attributions are drawn with the
same type system as everything else, and audio cannot drift.

`docs/ARCHITECTURE.md` names Remotion, and the `Renderer` port exists so that
choice stays open. Remotion needs a Node toolchain and an `npm install`; there is
no Remotion adapter in this repository yet, and there is a working renderer, so
adding one is a decision to make on quality grounds rather than a gap to fill.

## Production

Not yet. Specifically missing:

- authentication and authorisation (fields exist; enforcement does not);
- a real job queue (`InProcessJobQueue` loses running work on restart);
- PostgreSQL (the DDL is in `sqlite.py`; the adapter is SQLite);
- S3 (`S3StorageProvider` is a documented skeleton);
- rate limiting, metrics export, tracing.

Each sits behind a port that is already exercised by a working implementation,
which is the difference between "not built" and "not designed".
