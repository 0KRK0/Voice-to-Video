"""The canonical worked example: forty-three seconds about the transistor.

This is the example from the engineering brief, carried all the way through every
contract. It exists to demonstrate, concretely rather than rhetorically, the
decisions that distinguish this system from a text-to-video wrapper.

**Rule 7 in action.** Six transcript segments become six semantic units become
*five* scenes. Segments three and four — "It was smaller and far more efficient
than the vacuum tubes that came before it" and "A vacuum tube was the size of a
light bulb, a transistor could be smaller than a grain of rice" — are two
sentences making one point, and they become one scene with one comparison
visual. A system that split on punctuation would have produced two unrelated
shots and lost the argument.

**Rule 6 in action.** Of five scenes, three are drawn by our own animation engine
at essentially zero marginal cost, one is a licensed historical photograph, and
exactly one is generated. The expensive option is used where it earns its place —
an abstract closing image with no photographic equivalent — and nowhere else. The
whole project's generation cost is a few cents rather than several dollars.

**Rule 8 in action.** Every scene carries a fallback ladder ending in typography,
which cannot fail. There is no input for which this project renders nothing.

**Provenance in action.** The historical photograph carries a full origin record
and an attribution line that the renderer will place on screen.

Every number here — every timing, every span — is internally consistent, and the
test suite asserts that. If a contract changes in a way that breaks this example,
the build fails.
"""

from __future__ import annotations

from typing import NamedTuple

from vtv.contracts import (
    STAGE_ORDER,
    AspectRatio,
    Asset,
    AssetClipSource,
    AssetDimensions,
    AssetKind,
    AssetProvenance,
    AssetSource,
    AudioFormat,
    AudioProperties,
    CameraMotion,
    CaptionCue,
    CaptureSource,
    ComparisonSide,
    ComparisonSpec,
    Continuity,
    CostEstimate,
    Entity,
    EntityType,
    ExternalReference,
    FitPolicy,
    IdPrefix,
    ImageGenerationRequirements,
    License,
    LicensedMediaRequirements,
    NarrationTrack,
    NarrativeArc,
    ObjectRef,
    Permission,
    PersistenceMode,
    ProgrammaticClipSource,
    ProgrammaticRequirements,
    Project,
    Quantity,
    Recording,
    Relation,
    RetentionClass,
    Scene,
    SceneGraph,
    ScenePurpose,
    SceneVisualPlan,
    SemanticIntent,
    SemanticUnit,
    StageState,
    Status,
    StyleProfile,
    Timeline,
    TimelineEvent,
    TimelineSpec,
    TimeSpan,
    Transcript,
    TranscriptSegment,
    Transition,
    TransitionKind,
    TypographySpec,
    Understanding,
    VisualClip,
    VisualDirective,
    VisualGoal,
    VisualPlan,
    VisualStrategy,
    VisualStyle,
    new_id,
)
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.visual_language import Emphasis

NARRATION_DURATION = 43.0


class ExampleProject(NamedTuple):
    """A complete, internally consistent project graph."""

    project: Project
    recording: Recording
    transcript: Transcript
    understanding: Understanding
    scene_graph: SceneGraph
    visual_plan: VisualPlan
    assets: list[Asset]
    timeline: Timeline


def transistor_project() -> ExampleProject:
    """Build the worked example. Deterministic apart from generated identifiers."""

    project_id = new_id(IdPrefix.PROJECT)
    # The worked example belongs to a tenant like everything else. Using the
    # reserved system organisation rather than inventing one keeps it out of
    # any real customer namespace while still exercising the real path.
    organisation_id = SYSTEM_ORGANISATION_ID
    style = StyleProfile(
        style=VisualStyle.DOCUMENTARY,
        aspect_ratio=AspectRatio.LANDSCAPE_16_9,
        direction="restrained, archival, warm neutrals",
    )

    # -- Stage 1: the voice ------------------------------------------------
    audio = ObjectRef(
        bucket="vtv-media",
        key=f"orgs/{organisation_id}/projects/{project_id}/recording.webm",
        content_type="audio/webm",
        size_bytes=612_344,
        retention=RetentionClass.EPHEMERAL,
    )
    recording = Recording(
        organisation_id=organisation_id,
        project_id=project_id,
        source=CaptureSource.MICROPHONE,
        format=AudioFormat.WEBM_OPUS,
        audio=audio,
        declared_language="en",
        properties=AudioProperties(
            duration_seconds=NARRATION_DURATION,
            sample_rate_hz=48_000,
            channels=1,
            peak_dbfs=-3.2,
            silence_ratio=0.11,
        ),
        status=Status.READY,
    )

    # -- Stage 2: what was said -------------------------------------------
    spoken = [
        (0.0, 5.2, "Let me tell you about the single most important invention "
                   "of the twentieth century."),
        (5.2, 11.6, "The transistor was invented in 1947 at Bell Labs."),
        (11.6, 19.4, "It was smaller and far more efficient than the vacuum tubes "
                     "that came before it."),
        (19.4, 27.0, "A vacuum tube was the size of a light bulb. A transistor "
                     "could be smaller than a grain of rice."),
        (27.0, 35.5, "Within twenty years, transistors had replaced vacuum tubes "
                     "almost everywhere."),
        (35.5, 43.0, "Everything you are holding right now is built out of them."),
    ]
    segments = [
        TranscriptSegment(span=TimeSpan.of(start, end), text=text, confidence=0.96)
        for start, end, text in spoken
    ]
    transcript = Transcript(
        organisation_id=organisation_id,
        project_id=project_id,
        recording_id=recording.recording_id,
        language="en",
        segments=segments,
        status=Status.READY,
    )

    # -- Stage 3: what it meant -------------------------------------------
    transistor = Entity(
        name="transistor",
        type=EntityType.TECHNOLOGY,
        salience=1.0,
        external=ExternalReference(namespace="wikidata", identifier="Q5339"),
    )
    bell_labs = Entity(
        name="Bell Labs",
        type=EntityType.ORGANIZATION,
        salience=0.7,
        external=ExternalReference(namespace="wikidata", identifier="Q217580"),
    )
    vacuum_tube = Entity(
        name="vacuum tube",
        type=EntityType.TECHNOLOGY,
        salience=0.8,
        external=ExternalReference(namespace="wikidata", identifier="Q183218"),
    )
    year_1947 = Entity(name="1947", type=EntityType.DATE, salience=0.6)

    invented_at = Relation(
        subject_id=transistor.entity_id,
        predicate="invented_at",
        object_id=bell_labs.entity_id,
        confidence=0.97,
    )
    invented_in = Relation(
        subject_id=transistor.entity_id,
        predicate="invented_in_year",
        object_id=year_1947.entity_id,
        confidence=0.97,
    )
    replaced = Relation(
        subject_id=transistor.entity_id,
        predicate="replaced",
        object_id=vacuum_tube.entity_id,
        confidence=0.94,
    )

    units = [
        SemanticUnit(
            span=TimeSpan.of(0.0, 5.2),
            segment_ids=[segments[0].segment_id],
            text=spoken[0][2],
            proposition="The speaker is introducing the most important invention "
                        "of the twentieth century.",
            intent=SemanticIntent.CLAIM,
            salience=0.9,
            keyphrases=["most important invention", "twentieth century"],
        ),
        SemanticUnit(
            span=TimeSpan.of(5.2, 11.6),
            segment_ids=[segments[1].segment_id],
            text=spoken[1][2],
            proposition="The transistor was invented at Bell Labs in 1947.",
            intent=SemanticIntent.EVENT_NARRATION,
            entity_ids=[transistor.entity_id, bell_labs.entity_id, year_1947.entity_id],
            relation_ids=[invented_at.relation_id, invented_in.relation_id],
            salience=1.0,
            keyphrases=["transistor", "1947", "Bell Labs"],
        ),
        SemanticUnit(
            span=TimeSpan.of(11.6, 19.4),
            segment_ids=[segments[2].segment_id],
            text=spoken[2][2],
            proposition="The transistor was smaller and more efficient than the "
                        "vacuum tube.",
            intent=SemanticIntent.COMPARISON,
            entity_ids=[transistor.entity_id, vacuum_tube.entity_id],
            salience=0.9,
            keyphrases=["smaller", "more efficient"],
        ),
        SemanticUnit(
            span=TimeSpan.of(19.4, 27.0),
            segment_ids=[segments[3].segment_id],
            text=spoken[3][2],
            proposition="A vacuum tube was light-bulb sized; a transistor could be "
                        "smaller than a grain of rice.",
            intent=SemanticIntent.EXAMPLE,
            entity_ids=[transistor.entity_id, vacuum_tube.entity_id],
            salience=0.8,
            keyphrases=["light bulb", "grain of rice"],
        ),
        SemanticUnit(
            span=TimeSpan.of(27.0, 35.5),
            segment_ids=[segments[4].segment_id],
            text=spoken[4][2],
            proposition="Within twenty years transistors had displaced vacuum tubes "
                        "almost everywhere.",
            intent=SemanticIntent.EVENT_NARRATION,
            entity_ids=[transistor.entity_id, vacuum_tube.entity_id],
            relation_ids=[replaced.relation_id],
            quantities=[Quantity(value=20, unit="years", of_what="time to displacement")],
            salience=0.9,
        ),
        SemanticUnit(
            span=TimeSpan.of(35.5, 43.0),
            segment_ids=[segments[5].segment_id],
            text=spoken[5][2],
            proposition="Every device the listener owns is built from transistors.",
            intent=SemanticIntent.CONCLUSION,
            entity_ids=[transistor.entity_id],
            salience=1.0,
        ),
    ]

    understanding = Understanding(
        organisation_id=organisation_id,
        project_id=project_id,
        transcript_id=transcript.transcript_id,
        topic="The invention and impact of the transistor",
        summary=(
            "The transistor, invented at Bell Labs in 1947, was smaller and more "
            "efficient than the vacuum tube, displaced it within two decades, and "
            "underpins every device we now use."
        ),
        units=units,
        entities=[transistor, bell_labs, vacuum_tube, year_1947],
        relations=[invented_at, invented_in, replaced],
        status=Status.READY,
    )

    # -- Stage 4: the story ------------------------------------------------
    # Note the third scene: two spoken sentences, four semantic units' worth of
    # comparison, one visual idea. This is the whole point of Rule 7.
    scenes = [
        Scene(
            index=0,
            span=TimeSpan.of(0.0, 5.2),
            narration=spoken[0][2],
            semantic_unit_ids=[units[0].unit_id],
            purpose=ScenePurpose.OPENING,
            visual_goal=VisualGoal.EMPHASISE_STATEMENT,
            visual_brief="Open on the claim itself, set as type. No image can "
                         "carry 'most important invention' better than the words.",
            importance=0.9,
        ),
        Scene(
            index=1,
            span=TimeSpan.of(5.2, 11.6),
            narration=spoken[1][2],
            semantic_unit_ids=[units[1].unit_id],
            entity_ids=[transistor.entity_id, bell_labs.entity_id],
            purpose=ScenePurpose.CONTEXT,
            visual_goal=VisualGoal.SHOW_ENTITY,
            visual_brief="Show the first transistor as a real object, in 1947, at "
                         "Bell Labs. A photograph carries the history; a generated "
                         "image would fabricate it.",
            importance=1.0,
            continuity=Continuity(motifs=["year card, lower left"]),
        ),
        Scene(
            index=2,
            span=TimeSpan.of(11.6, 27.0),
            narration=f"{spoken[2][2]} {spoken[3][2]}",
            semantic_unit_ids=[units[2].unit_id, units[3].unit_id],
            entity_ids=[transistor.entity_id, vacuum_tube.entity_id],
            purpose=ScenePurpose.CONTRAST,
            visual_goal=VisualGoal.SHOW_CONTRAST,
            visual_brief="Hold the vacuum tube and the transistor against each "
                         "other at true relative scale. One shot, two sentences: "
                         "the size difference is the argument.",
            importance=1.0,
            continuity=Continuity(
                carried_entity_ids=[transistor.entity_id],
                continues_previous_visual=True,
            ),
        ),
        Scene(
            index=3,
            span=TimeSpan.of(27.0, 35.5),
            narration=spoken[4][2],
            semantic_unit_ids=[units[4].unit_id],
            entity_ids=[transistor.entity_id, vacuum_tube.entity_id],
            purpose=ScenePurpose.TURNING_POINT,
            visual_goal=VisualGoal.SHOW_CHANGE_OVER_TIME,
            visual_brief="Two decades, drawn as a timeline: 1947 invention, "
                         "displacement complete by the late sixties. Only what "
                         "was actually said.",
            importance=0.8,
            continuity=Continuity(motifs=["year card, lower left"]),
        ),
        Scene(
            index=4,
            span=TimeSpan.of(35.5, 43.0),
            narration=spoken[5][2],
            semantic_unit_ids=[units[5].unit_id],
            entity_ids=[transistor.entity_id],
            purpose=ScenePurpose.CONCLUSION,
            visual_goal=VisualGoal.ILLUSTRATE_ABSTRACT,
            visual_brief="Close on the abstract scale of it: a silicon die, "
                         "impossibly dense. Nothing photographic says 'billions of "
                         "these, in your hand' — this is where generation earns "
                         "its cost.",
            importance=1.0,
        ),
    ]

    scene_graph = SceneGraph(
        organisation_id=organisation_id,
        project_id=project_id,
        understanding_id=understanding.understanding_id,
        transcript_id=transcript.transcript_id,
        style=style,
        narrative=NarrativeArc(
            thesis="The transistor replaced the vacuum tube and became the "
                   "substrate of modern life.",
            beats=[
                "a claim is made",
                "the invention is placed in time",
                "the old and new are compared",
                "the replacement happens",
                "the consequence is now",
            ],
            motifs=["year card, lower left", "warm archival grade"],
        ),
        scenes=scenes,
        status=Status.READY,
    )

    # -- Stage 5: what each scene should look like -------------------------
    def typography_fallback(headline: str, subline: str | None = None) -> VisualDirective:
        """The bottom of every ladder. Costs nothing and cannot fail."""
        return VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(
                spec=TypographySpec(
                    headline=headline,
                    subline=subline,
                    preferred_duration=5.0,
                    emphasis=Emphasis.STRONG,
                )
            ),
            rationale="Guaranteed terminal fallback: type always renders.",
            estimate=CostEstimate(usd=0.0, latency_seconds=0.2),
            confidence=0.5,
        )

    plan_opening = SceneVisualPlan(
        scene_id=scenes[0].scene_id,
        primary=VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(
                spec=TypographySpec(
                    headline="The most important invention of the twentieth century",
                    highlight=["most important"],
                    reveal="word_by_word",
                    preferred_duration=5.2,
                    emphasis=Emphasis.STRONG,
                )
            ),
            rationale="A claim with no referent yet. Set the words; introduce the "
                      "subject in the next shot.",
            estimate=CostEstimate(usd=0.0, latency_seconds=0.2),
            confidence=0.9,
        ),
    )

    plan_invention = SceneVisualPlan(
        scene_id=scenes[1].scene_id,
        primary=VisualDirective(
            strategy=VisualStrategy.LICENSED_MEDIA,
            requirements=LicensedMediaRequirements(
                query="first transistor Bell Labs 1947",
                alternate_queries=[
                    "point-contact transistor Bardeen Brattain",
                    "Bell Telephone Laboratories 1947",
                ],
                must_depict="the original point-contact transistor or Bell Labs "
                            "in the late 1940s",
                camera_motion=CameraMotion.KEN_BURNS,
            ),
            rationale="A real historical object. Photography is both cheaper and "
                      "more truthful than generating an invented likeness.",
            estimate=CostEstimate(usd=0.0, latency_seconds=1.5),
            confidence=0.85,
        ),
        fallbacks=[
            VisualDirective(
                strategy=VisualStrategy.PROGRAMMATIC,
                requirements=ProgrammaticRequirements(
                    spec=TimelineSpec(
                        title="1947",
                        events=[
                            TimelineEvent(
                                label="Transistor invented at Bell Labs",
                                when="1947",
                                sort_value=1947,
                            )
                        ],
                        preferred_duration=6.4,
                    )
                ),
                rationale="If no correctly-licensed photograph exists, state the "
                          "fact as a dated card rather than inventing an image of "
                          "a real event.",
                estimate=CostEstimate(usd=0.0, latency_seconds=0.2),
                confidence=0.7,
            ),
            typography_fallback("1947", "Bell Labs"),
        ],
    )

    plan_contrast = SceneVisualPlan(
        scene_id=scenes[2].scene_id,
        primary=VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(
                spec=ComparisonSpec(
                    left=ComparisonSide(
                        title="Vacuum tube",
                        points=["Size of a light bulb", "Hot, fragile, power-hungry"],
                        asset_query="vacuum tube electronics",
                    ),
                    right=ComparisonSide(
                        title="Transistor",
                        points=["Smaller than a grain of rice", "Cool, solid, cheap"],
                        asset_query="transistor component macro",
                    ),
                    connector="arrow",
                    preferred_duration=15.4,
                    emphasis=Emphasis.STRONG,
                )
            ),
            rationale="The speaker's point is a size comparison. Drawing both at "
                      "true relative scale states it exactly; a generated image "
                      "would get the proportions wrong and prove nothing.",
            estimate=CostEstimate(usd=0.0, latency_seconds=0.3),
            confidence=0.95,
        ),
        fallbacks=[
            typography_fallback(
                "Smaller. Cooler. Cheaper.", "A light bulb, versus a grain of rice"
            )
        ],
    )

    plan_replacement = SceneVisualPlan(
        scene_id=scenes[3].scene_id,
        primary=VisualDirective(
            strategy=VisualStrategy.PROGRAMMATIC,
            requirements=ProgrammaticRequirements(
                spec=TimelineSpec(
                    title="Twenty years",
                    events=[
                        TimelineEvent(
                            label="Transistor invented",
                            when="1947",
                            sort_value=1947,
                        ),
                        TimelineEvent(
                            label="Vacuum tubes displaced almost everywhere",
                            when="within twenty years",
                            sort_value=1967,
                        ),
                    ],
                    preferred_duration=8.5,
                )
            ),
            rationale="A span of time with two endpoints, both stated by the "
                      "speaker. A timeline is the exact visual form of that "
                      "sentence, and it invents no facts.",
            estimate=CostEstimate(usd=0.0, latency_seconds=0.3),
            confidence=0.9,
        ),
        fallbacks=[typography_fallback("Twenty years", "and the tubes were gone")],
    )

    plan_conclusion = SceneVisualPlan(
        scene_id=scenes[4].scene_id,
        primary=VisualDirective(
            strategy=VisualStrategy.GENERATED_IMAGE,
            requirements=ImageGenerationRequirements(
                prompt=(
                    "Extreme macro photograph of a silicon die, dense geometric "
                    "circuitry receding into the distance, warm neutral archival "
                    "grade, shallow depth of field, documentary style"
                ),
                negative_prompt="text, watermark, people, logos",
                aspect_ratio=AspectRatio.LANDSCAPE_16_9,
                camera_motion=CameraMotion.ZOOM_OUT,
                depicts_reality=False,
            ),
            rationale="An abstract closing idea — density beyond counting — with "
                      "no specific photographic referent. This is the one shot in "
                      "the project where generation buys something real.",
            estimate=CostEstimate(usd=0.04, latency_seconds=12.0),
            confidence=0.75,
        ),
        fallbacks=[
            VisualDirective(
                strategy=VisualStrategy.LICENSED_MEDIA,
                requirements=LicensedMediaRequirements(
                    query="silicon wafer microchip macro",
                    camera_motion=CameraMotion.ZOOM_OUT,
                ),
                rationale="A real wafer photograph carries the same idea if "
                          "generation is unavailable or over budget.",
                estimate=CostEstimate(usd=0.0, latency_seconds=1.5),
                confidence=0.7,
            ),
            typography_fallback("Everything you are holding", "is built out of them"),
        ],
    )

    visual_plan = VisualPlan(
        organisation_id=organisation_id,
        project_id=project_id,
        scene_graph_id=scene_graph.scene_graph_id,
        scene_plans=[
            plan_opening,
            plan_invention,
            plan_contrast,
            plan_replacement,
            plan_conclusion,
        ],
        status=Status.READY,
    )

    # -- Stage 6/7: the assets that realised the plan ----------------------
    historical_photo = Asset(
        project_id=project_id,
        kind=AssetKind.IMAGE,
        source=AssetSource.WIKIMEDIA_COMMONS,
        object=ObjectRef(
            bucket="vtv-media",
            key=f"orgs/{organisation_id}/projects/{project_id}/assets/first-transistor.jpg",
            content_type="image/jpeg",
            size_bytes=1_204_912,
            retention=RetentionClass.PROJECT,
        ),
        dimensions=AssetDimensions(width=2400, height=1800),
        provenance=AssetProvenance(
            source=AssetSource.WIKIMEDIA_COMMONS,
            source_id="File:Replica-of-first-transistor.jpg",
            original_url="https://commons.wikimedia.org/wiki/File:Replica-of-first-transistor.jpg",
            title="Replica of the first transistor",
            creator="Unitronic",
            license=License(
                spdx_id="CC-BY-SA-3.0",
                name="Creative Commons Attribution-ShareAlike 3.0",
                url="https://creativecommons.org/licenses/by-sa/3.0/",
                commercial_use=Permission.ALLOWED,
                modification=Permission.ALLOWED,
                attribution_required=True,
                share_alike=True,
            ),
        ),
        description="Replica of the first point-contact transistor, Bell Labs 1947",
        status=Status.READY,
    )

    generated_die = Asset(
        project_id=project_id,
        kind=AssetKind.IMAGE,
        source=AssetSource.GENERATED,
        generation_id=new_id(IdPrefix.GENERATION),
        object=ObjectRef(
            bucket="vtv-media",
            key=f"orgs/{organisation_id}/projects/{project_id}/assets/silicon-die.png",
            content_type="image/png",
            size_bytes=2_811_004,
            retention=RetentionClass.PROJECT,
        ),
        dimensions=AssetDimensions(width=1920, height=1080),
        description="Generated macro image of a silicon die",
        status=Status.READY,
    )

    # -- Stage 9/10: everything placed in time -----------------------------
    fade = Transition(kind=TransitionKind.DISSOLVE, duration_seconds=0.5)

    clips = [
        VisualClip(
            scene_id=scenes[0].scene_id,
            span=TimeSpan.of(0.0, 5.2),
            source=ProgrammaticClipSource(
                spec=plan_opening.primary.requirements.spec  # type: ignore[union-attr]
            ),
            fit=FitPolicy.STILL,
        ),
        VisualClip(
            scene_id=scenes[1].scene_id,
            span=TimeSpan.of(5.2, 11.6),
            source=AssetClipSource(
                asset_id=historical_photo.asset_id,
                object=historical_photo.object,  # type: ignore[arg-type]
                attribution=historical_photo.attribution_line(),
            ),
            fit=FitPolicy.STILL,
            camera_motion=CameraMotion.KEN_BURNS,
            transition_in=fade,
        ),
        VisualClip(
            scene_id=scenes[2].scene_id,
            span=TimeSpan.of(11.6, 27.0),
            source=ProgrammaticClipSource(
                spec=plan_contrast.primary.requirements.spec  # type: ignore[union-attr]
            ),
            fit=FitPolicy.STILL,
            transition_in=fade,
        ),
        VisualClip(
            scene_id=scenes[3].scene_id,
            span=TimeSpan.of(27.0, 35.5),
            source=ProgrammaticClipSource(
                spec=plan_replacement.primary.requirements.spec  # type: ignore[union-attr]
            ),
            fit=FitPolicy.STILL,
            transition_in=fade,
        ),
        VisualClip(
            scene_id=scenes[4].scene_id,
            span=TimeSpan.of(35.5, 43.0),
            source=AssetClipSource(
                asset_id=generated_die.asset_id,
                object=generated_die.object,  # type: ignore[arg-type]
            ),
            fit=FitPolicy.STILL,
            camera_motion=CameraMotion.ZOOM_OUT,
            transition_in=fade,
        ),
    ]

    captions = [
        CaptionCue(span=TimeSpan.of(start, end), text=text)
        for start, end, text in spoken
    ]

    timeline = Timeline(
        organisation_id=organisation_id,
        project_id=project_id,
        scene_graph_id=scene_graph.scene_graph_id,
        narration=NarrationTrack(audio=audio, duration_seconds=NARRATION_DURATION),
        clips=clips,
        captions=captions,
        style=style,
        aspect_ratio=AspectRatio.LANDSCAPE_16_9,
        status=Status.READY,
    )

    project = Project(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=project_id,
        title="How the transistor replaced the vacuum tube",
        style=style,
        persistence=PersistenceMode.SAVED,
        recording_id=recording.recording_id,
        transcript_id=transcript.transcript_id,
        understanding_id=understanding.understanding_id,
        scene_graph_id=scene_graph.scene_graph_id,
        visual_plan_id=visual_plan.visual_plan_id,
        timeline_id=timeline.timeline_id,
        stages=[
            StageState(stage=stage, status=Status.READY)
            for stage in STAGE_ORDER
        ],
        total_cost_usd=0.04,
    )

    return ExampleProject(
        project=project,
        recording=recording,
        transcript=transcript,
        understanding=understanding,
        scene_graph=scene_graph,
        visual_plan=visual_plan,
        assets=[historical_photo, generated_die],
        timeline=timeline,
    )


__all__ = ["NARRATION_DURATION", "ExampleProject", "transistor_project"]
