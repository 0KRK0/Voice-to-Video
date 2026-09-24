"""Stages 19 and 20 — entry points that are not a microphone.

The voice product is the wedge; the engine underneath it takes meaning from
anywhere. Because every stage consumes a document and produces a document, text
input needs no new pipeline — it needs a `Transcript`, which is the only thing
the first two stages exist to produce.

That is the whole of the argument for the architecture in one function: a
business customer's documentation, a teacher's lesson notes and a developer's
API call all become the same `Transcript` and then take the identical path
through understanding, scenes, direction, composition and rendering.

Timing for text is synthesised at a natural speaking rate, and is *labelled as
synthesised*. It is not pretending to be a recording.
"""

from __future__ import annotations

from typing import Any

from vtv.contracts.base import IdPrefix, TimeSpan, new_id
from vtv.contracts.errors import Status, ValidationFailed
from vtv.contracts.project import PersistenceMode, Project
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.pipeline.captions import CaptionBuilder

#: Words per minute of unhurried explanation. Used only to time synthetic
#: transcripts; a real recording always uses its own measured timing.
SPEAKING_WPM = 145.0

#: A short pause after each sentence, as a speaker would take.
SENTENCE_PAUSE = 0.35


def transcript_from_text(
    text: str, *, organisation_id: str, project_id: str, language: str = "en"
) -> Transcript:
    """Turn written text into a timed transcript at speaking pace."""
    from vtv.adapters.speech.scripted import split_sentences

    sentences = split_sentences(text)
    if not sentences:
        raise ValidationFailed("input contained no sentences")

    segments: list[TranscriptSegment] = []
    cursor = 0.0
    for sentence in sentences:
        words = max(1, len(sentence.split()))
        duration = max(0.9, (words / SPEAKING_WPM) * 60.0)
        segments.append(
            TranscriptSegment(
                span=TimeSpan.of(round(cursor, 3), round(cursor + duration, 3)),
                text=sentence,
            )
        )
        cursor += duration + SENTENCE_PAUSE

    return Transcript(
        organisation_id=organisation_id,
        project_id=project_id,
        recording_id=new_id(IdPrefix.RECORDING),
        language=language,
        segments=segments,
        provider="synthetic-text",
        model="speaking-rate-1.0",
        status=Status.READY,
    )


async def visualise_text(
    assembly: Any,
    content: str,
    *,
    style: StyleProfile | None = None,
    render: bool = False,
    organisation_id: str | None = None,
) -> dict[str, Any]:
    """`POST /v1/visualize` — text to a visual story.

    Rendering is opt-in. Most API callers want the *plan* — the scenes, the
    strategies, the reasoning — which costs nothing and returns in milliseconds.
    Producing a file is a separate, slower, more expensive thing to ask for.
    """
    style = style or StyleProfile()
    project = Project(
        organisation_id=organisation_id or SYSTEM_ORGANISATION_ID,
        style=style,
        persistence=PersistenceMode.TEMPORARY,
    )

    transcript = transcript_from_text(
        content,
        organisation_id=project.organisation_id,
        project_id=project.project_id,
    )
    duration = transcript.span.end if transcript.span else 0.0

    understanding = await assembly.pipeline.understanding.understand(transcript)
    scene_graph = assembly.pipeline.scenes.build(
        transcript=transcript,
        understanding=understanding,
        style=style,
        total_duration=duration,
    )
    visual_plan = await assembly.pipeline.director.direct(
        scene_graph=scene_graph, understanding=understanding
    )

    payload: dict[str, Any] = {
        "project_id": project.project_id,
        "topic": understanding.topic,
        "thesis": scene_graph.narrative.thesis,
        "duration_seconds": round(duration, 3),
        "strategy_mix": visual_plan.strategy_mix(),
        "estimated_cost_usd": round(visual_plan.expected_cost_usd, 4),
        "scenes": [
            {
                "scene_id": scene.scene_id,
                "start": scene.span.start,
                "end": scene.span.end,
                "narration": scene.narration,
                "purpose": scene.purpose.value,
                "visual_goal": scene.visual_goal.value,
                "strategy": plan.primary.strategy.value if plan else None,
                "rationale": plan.primary.rationale if plan else None,
                "visual": _describe(plan),
            }
            for scene, plan in (
                (scene, visual_plan.plan_for(scene.scene_id))
                for scene in scene_graph.scenes
            )
        ],
        "captions": [
            {"start": cue.span.start, "end": cue.span.end, "text": cue.text}
            for cue in CaptionBuilder().build(transcript, limit=duration)
        ],
        "rendered": False,
        "notes": [
            "Timings are synthesised from a speaking rate, not measured from audio.",
        ],
    }

    if render:
        # Stated rather than silently ignored: a video needs a voice track, and
        # text input has none. Synthesising speech is a Stage 19 decision the
        # product has not taken yet.
        payload["notes"].append(
            "Rendering text input requires a narration track; supply audio via "
            "the recordings endpoint to produce a video."
        )
    return payload


def _describe(plan: Any) -> dict[str, Any] | None:
    """A provider-neutral description of the chosen visual."""
    if plan is None:
        return None
    requirements = plan.primary.requirements
    spec = getattr(requirements, "spec", None)
    if spec is not None:
        return {"kind": "programmatic", "primitive": spec.primitive.value}
    query = getattr(requirements, "query", None)
    if query:
        return {"kind": "media_search", "query": query}
    prompt = getattr(requirements, "prompt", None)
    if prompt:
        return {"kind": "generated", "prompt": prompt}
    return {"kind": requirements.strategy.value}


__all__ = [
    "SENTENCE_PAUSE",
    "SPEAKING_WPM",
    "transcript_from_text",
    "visualise_text",
]
