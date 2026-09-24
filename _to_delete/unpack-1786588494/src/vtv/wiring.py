"""Assembling the system from configuration.

This is the only module that knows which concrete adapter implements which port.
Everything else receives what it needs and never looks it up — which is why the
pipeline can be tested against stubs without a single conditional inside it.

The wiring is deliberately honest about what is missing. If no speech-to-text
credential is configured, no speech provider is registered and the pipeline says
so; it does not quietly substitute something that produces plausible-looking
words. Same for image and video generation: with no provider registered the
Visual Director's ladder descends to a drawn visual, the video still renders, and
the event stream records the degradation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vtv.adapters.assets.http_fetcher import HttpFetcher
from vtv.adapters.assets.library import LocalLibraryAssetSearchProvider
from vtv.adapters.assets.openverse import (
    OpenverseAssetSearchProvider,
    WikimediaAssetSearchProvider,
)
from vtv.adapters.images.http_image import (
    HttpImageGenerationProvider,
    HttpVideoGenerationProvider,
)
from vtv.adapters.ingest.documents import ParserRegistry
from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
from vtv.adapters.speech.http_stt import HttpSpeechToTextProvider
from vtv.adapters.speech.http_tts import HttpSpeechSynthesisProvider
from vtv.adapters.speech.scripted import ScriptedSpeechToTextProvider
from vtv.adapters.speech.silent import SilentNarrationProvider
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.adapters.text.http_llm import HttpTextGenerationProvider
from vtv.billing.usage import UsageMeter
from vtv.config import Settings
from vtv.contracts.base import ObjectRef
from vtv.contracts.generation import GenerationKind
from vtv.observability.events import EventSink, json_log_handler
from vtv.pipeline.assets import AssetResolver, LocalFileFetcher
from vtv.pipeline.captions import CaptionBuilder
from vtv.pipeline.capture import CaptureService
from vtv.pipeline.composition import SceneComposer
from vtv.pipeline.consistency import ConsistencyEngine
from vtv.pipeline.costs import CostLedger
from vtv.pipeline.director import LlmVisualDirector, RuleBasedVisualDirector
from vtv.pipeline.generation import GenerationRouter
from vtv.pipeline.ingestion import IngestionService
from vtv.pipeline.narration import NarrationService
from vtv.pipeline.orchestrator import Pipeline
from vtv.pipeline.scenes import SceneEngine
from vtv.pipeline.transcription import TranscriptionService
from vtv.pipeline.understanding import (
    HeuristicUnderstandingEngine,
    LlmUnderstandingEngine,
    UnderstandingService,
)
from vtv.security.audit import AuditLog
from vtv.security.authz import Authorizer
from vtv.security.directory import Directory
from vtv.security.limits import RateLimiter


@dataclass
class Capabilities:
    """What this deployment can actually do, decided at wiring time.

    Surfaced through the API's health endpoint so nobody has to guess whether a
    given install has real transcription, and reported in the golden path so a
    development run is never mistaken for a production one.
    """

    real_transcription: bool = False
    real_understanding: bool = False
    real_visual_direction: bool = False
    real_asset_search: bool = False
    real_image_generation: bool = False
    real_video_generation: bool = False
    #: False means document input still produces a video, but a mute one. It is
    #: reported rather than discovered by the user pressing play.
    real_speech_synthesis: bool = False
    document_ingestion: bool = True
    rendering: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "real_transcription": self.real_transcription,
            "real_understanding": self.real_understanding,
            "real_visual_direction": self.real_visual_direction,
            "real_asset_search": self.real_asset_search,
            "real_image_generation": self.real_image_generation,
            "real_video_generation": self.real_video_generation,
            "real_speech_synthesis": self.real_speech_synthesis,
            "document_ingestion": self.document_ingestion,
            "rendering": self.rendering,
        }


@dataclass
class Assembly:
    """The wired system, plus the pieces the API needs to reach directly."""

    settings: Settings
    events: EventSink
    storage: LocalStorageProvider
    router: GenerationRouter
    ledger: CostLedger
    pipeline: Pipeline
    capabilities: Capabilities
    ingestion: IngestionService
    #: Stage 25 and 26. Present in every deployment: a system that can be run
    #: with authorisation switched off is a system that will be.
    directory: Directory
    audit: AuditLog
    authorizer: Authorizer
    limiter: RateLimiter
    usage: UsageMeter
    scripts: dict[str, str] = field(default_factory=dict)

    def register_script(self, ref: ObjectRef, script: str) -> None:
        """Attach a development transcript to a recording (see below)."""
        self.scripts[ref.key] = script


def build(
    settings: Settings | None = None,
    *,
    events: EventSink | None = None,
    log_events: bool = False,
    workdir: Path | None = None,
) -> Assembly:
    """Wire the system from settings."""
    settings = settings or Settings.from_env()
    events = events or EventSink()
    if log_events:
        events.subscribe(json_log_handler())

    storage = LocalStorageProvider(
        settings.storage_root, bucket=settings.storage_bucket
    )
    ledger = CostLedger()
    router = GenerationRouter(events=events, ledger=ledger)
    capabilities = Capabilities()
    scripts: dict[str, str] = {}

    # -- speech to text ---------------------------------------------------
    if settings.speech_to_text_endpoint and settings.speech_to_text_api_key:
        speech: object = HttpSpeechToTextProvider(
            storage=storage,
            endpoint=settings.speech_to_text_endpoint,
            api_key=settings.speech_to_text_api_key,
        )
        capabilities.real_transcription = True
    else:
        # No credential: the development aligner, which needs a script and says
        # so loudly rather than inventing words.
        speech = ScriptedSpeechToTextProvider(storage, lambda ref: scripts.get(ref.key))
    router.register(speech, GenerationKind.SPEECH_TO_TEXT)

    # -- speech synthesis -------------------------------------------------
    # Needed by every input that is not a voice recording: a document has words
    # but no clock, and the timeline is built against narration (Rule 5).
    silent_narration = SilentNarrationProvider(storage=storage)
    if settings.speech_synthesis_endpoint and settings.speech_synthesis_api_key:
        router.register(
            HttpSpeechSynthesisProvider(
                storage=storage,
                endpoint=settings.speech_synthesis_endpoint,
                api_key=settings.speech_synthesis_api_key,
                model=settings.speech_synthesis_model,
                default_voice=settings.speech_synthesis_voice,
            ),
            GenerationKind.SPEECH,
        )
        capabilities.real_speech_synthesis = True
    # The silent provider is deliberately *not* registered with the router. It
    # must never be a candidate that competes on price with a real synthesiser
    # — it would win every time, being free. It is reachable only as the last
    # rung of the narration ladder, and taking it is recorded as a degradation.

    # -- text generation --------------------------------------------------
    text_provider = None
    if settings.text_generation_endpoint and settings.text_generation_api_key:
        text_provider = HttpTextGenerationProvider(
            endpoint=settings.text_generation_endpoint,
            api_key=settings.text_generation_api_key,
            model=settings.text_generation_model,
            schema_dir=Path(__file__).resolve().parents[2] / "schemas",
        )
        router.register(text_provider, GenerationKind.TEXT)
        capabilities.real_understanding = True
        capabilities.real_visual_direction = True

    # -- image and video generation ---------------------------------------
    if settings.image_generation_endpoint and settings.image_generation_api_key:
        router.register(
            HttpImageGenerationProvider(
                storage=storage,
                endpoint=settings.image_generation_endpoint,
                api_key=settings.image_generation_api_key,
                model="default",
            ),
            GenerationKind.IMAGE,
        )
        capabilities.real_image_generation = True
    if settings.video_generation_endpoint and settings.video_generation_api_key:
        router.register(
            HttpVideoGenerationProvider(
                storage=storage,
                endpoint=settings.video_generation_endpoint,
                api_key=settings.video_generation_api_key,
                model="default",
            ),
            GenerationKind.VIDEO,
        )
        capabilities.real_video_generation = True

    # -- asset search -----------------------------------------------------
    library_root = settings.storage_root.parent / "library"
    providers: list[object] = [LocalLibraryAssetSearchProvider(library_root)]
    if settings.asset_search_endpoint:
        providers.append(
            OpenverseAssetSearchProvider(endpoint=settings.asset_search_endpoint)
        )
        providers.append(WikimediaAssetSearchProvider())
        capabilities.real_asset_search = True

    asset_resolver = AssetResolver(
        storage=storage,
        events=events,
        providers=providers,
        fetcher=_CompositeFetcher(
            local=LocalFileFetcher([library_root]),
            remote=HttpFetcher(),
        ),
    )

    # -- intelligence -----------------------------------------------------
    heuristic = HeuristicUnderstandingEngine()
    understanding_engine = (
        LlmUnderstandingEngine(provider=text_provider, fallback=heuristic)
        if text_provider is not None
        else heuristic
    )
    rules_director = RuleBasedVisualDirector(events=events)
    director = (
        LlmVisualDirector(provider=text_provider, events=events, fallback=rules_director)
        if text_provider is not None
        else rules_director
    )

    narration = NarrationService(
        router=router,
        events=events,
        silent_fallback=silent_narration,
        voice=settings.speech_synthesis_voice,
    )
    ingestion = IngestionService(registry=ParserRegistry(), events=events)

    # -- tenancy, audit, limits and metering ------------------------------
    var = settings.storage_root.parent
    directory = Directory(path=var / "directory.db")
    audit = AuditLog(path=var / "audit.db", events=events)
    authorizer = Authorizer(record=audit, suspended=directory.suspended_ids())
    limiter = RateLimiter()
    usage = UsageMeter(path=var / "usage.db", tier_of=directory.tier_of)

    pipeline = Pipeline(
        capture=CaptureService(
            storage=storage,
            events=events,
            max_bytes=settings.max_upload_bytes,
            max_seconds=settings.max_recording_seconds,
        ),
        # Through the router, not around it: transcription is cached and
        # costed like every other provider call.
        transcription=TranscriptionService(provider=router, events=events),
        understanding=UnderstandingService(engine=understanding_engine, events=events),
        scenes=SceneEngine(events=events),
        director=director,
        composer=SceneComposer(
            storage=storage,
            events=events,
            router=router,
            asset_resolver=asset_resolver,
            captions=CaptionBuilder(),
        ),
        renderer=FfmpegRenderer(storage=storage, events=events, workdir=workdir),
        events=events,
        ledger=ledger,
        narration=narration,
        consistency=ConsistencyEngine(events=events),
    )

    return Assembly(
        settings=settings,
        events=events,
        storage=storage,
        router=router,
        ledger=ledger,
        pipeline=pipeline,
        capabilities=capabilities,
        ingestion=ingestion,
        directory=directory,
        audit=audit,
        authorizer=authorizer,
        limiter=limiter,
        usage=usage,
        scripts=scripts,
    )


@dataclass
class _CompositeFetcher:
    """Routes by scheme: local library files locally, everything else over HTTPS."""

    local: LocalFileFetcher
    remote: HttpFetcher

    async def fetch(self, url: str) -> tuple[bytes, str]:
        if url.startswith("file://"):
            return await self.local.fetch(url)
        return await self.remote.fetch(url)


__all__ = ["Assembly", "Capabilities", "build"]
