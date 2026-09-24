# Product

## The primary experience

```
        +---------------------------------+
        |                                 |
        |        Speak your idea          |
        |                                 |
        |               O                 |
        |                                 |
        |             02:14               |
        |                                 |
        |       [  Stop & Create  ]       |
        |                                 |
        +---------------------------------+
```

One button. The user talks. They stop. They get a video.

**Voice is the primary input.** Uploading an audio file is a secondary path that
exists for people who already have a recording, and it must never become the
default the product is designed around. The distinction is recorded in the data
(`CaptureSource` in `src/vtv/contracts/recording.py`) so that we can see, rather
than assume, whether it stays true.

The reason this matters is not aesthetic. Speaking is the fastest way a person
can get an idea out of their head, and it carries information that typing loses:
emphasis, hesitation, the order somebody naturally reaches for. A product built
around a text box would be a different product with a worse input.

## What the user gets back

A video where:

- **the visuals follow the meaning**, not the sentence boundaries;
- **the timing follows the voice** exactly — their pauses are the edit;
- **captions are synchronised** from real word timings, not estimated;
- **credits are correct**, because every external asset carries its licence.

## The flow, first version

```
Press record → speak → stop → process → watch
```

Reliable first, real-time later. The first implementation processes after the
recording ends. This is a deliberate sequencing choice: streaming transcription
and speculative scene preparation are genuinely valuable and genuinely difficult,
and building them before the pipeline is correct would mean debugging two hard
problems at once.

The architecture does not preclude it. Because every stage consumes a document
and produces a document, and because scenes are anchored to time spans rather
than to positions in a list, the same pipeline can later be driven incrementally
from a partial transcript. That is Stage 15 territory, not Stage 1.

## What "processing" shows the user

Never an undifferentiated spinner. The pipeline stages are named
(`PipelineStage` in `src/vtv/contracts/project.py`) and progress is real:

```
Listening to what you said          done
Understanding your idea             done
Building the story                  done
Choosing visuals                    12 of 18
Putting it together
Rendering
```

Honest progress is a product feature. A user who can see that "choosing visuals"
is the slow part understands the product better and forgives it more.

## Editing: meaning, not frames

The storyboard (Stage 12) shows the *story*, not a timeline of clips:

```
SCENE 02  ·  5.2s – 11.6s
--------------------------------------------
"The transistor was invented in 1947 at Bell Labs."

[ historical photograph ]

Source: Wikimedia Commons · CC BY-SA 3.0
Why this: a real object, so a photograph is truer than a generated image.

[ Regenerate ]  [ Replace ]  [ Change approach ]
```

Two things are unusual here and both are deliberate.

**The system explains itself.** Every visual decision carries a rationale
(`VisualDirective.rationale`). A user who disagrees can see *why* the system
chose what it did, which turns an argument with a black box into a conversation.

**"Change approach" is a first-class action.** The user is not limited to
re-rolling the same generation. They can say "draw this instead of finding a
photo", because strategy is data, not an implementation detail.

We are not building a frame-accurate editor. Someone who wants to keyframe has
better tools. Our user wants to change what a shot *means* and have the system
handle the rest.

## Persistence: two honest modes

**Temporary** (default). Record, process, render, download. The audio and
intermediate artefacts are swept on a short clock. Most people making one video
do not want an account's worth of their voice sitting on our disks, and we should
not want it either.

**Saved.** The reasoning is kept — transcript, understanding, scenes, plan,
timeline — so the project can be reopened, edited and re-rendered. Note what is
*not* kept by default: the finished MP4. Storing gigabytes of video to avoid
re-rendering something we can rebuild in minutes is the wrong trade
(`docs/STORAGE_POLICY.md`).

## Who this is for

The first user is a creator, teacher or expert who can explain something well out
loud and cannot or will not spend a day in an editor. That person exists in
enormous numbers and is badly served today: their choice is a talking head, a
slide deck read aloud, or hiring someone.

Later the same engine serves education, enterprise training, and other people's
products through an API. Those are the same technology with different packaging,
which is why the architecture assumes multi-tenancy and provenance from the
start even though the first product needs neither.

## The first success criterion

A person presses a microphone button, speaks for between one and five minutes
about something they know, stops, waits, and watches a video they would be
willing to publish.

Everything before that is scaffolding.

## What we are explicitly not building yet

Authentication, payments, teams, dashboards, mobile apps, marketplaces, custom
foundation models, collaborative editing, Kubernetes. Each is a real thing a real
company needs, and each is a way to spend a year not answering the only question
that matters right now: *can we reliably turn speech into a coherent visual
story?*
