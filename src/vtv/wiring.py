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

from collections.abc import Callable
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
from vtv.adapters.storage.s3 import S3StorageProvider
from vtv.adapters.text.http_llm import HttpTextGenerationProvider
from vtv.adapters.video.openai_video import OpenAIVideoGenerationProvider
from vtv.billing.plans import QuotaKind, plan_for
from vtv.billing.usage import (
    GeneratedAssetAllowance,
    QuotaExceeded,
    UsageMeter,
    UsagePeriod,
)
from vtv.config import Settings
from vtv.contracts.base import ObjectRef
from vtv.contracts.errors import ErrorCode, VTVError
from vtv.contracts.execution import ExecutionTarget, Machine, Registry
from vtv.contracts.generation import GenerationKind
from vtv.observability.bridge import correlated_log_handler, metrics_handler
from vtv.observability.events import EventSink
from vtv.observability.metrics import Metrics, register_defaults
from vtv.observability.trace import build_trace
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
from vtv.pipeline.plan_gate import PlanGate
from vtv.pipeline.scenes import SceneEngine
from vtv.pipeline.transcription import TranscriptionService
from vtv.pipeline.understanding import (
    HeuristicUnderstandingEngine,
    LlmUnderstandingEngine,
    UnderstandingService,
)
from vtv.ports.base import DataPolicy
from vtv.ports.storage import StorageProvider
from vtv.security.audit import AuditLog
from vtv.security.authz import Authorizer
from vtv.security.directory import Directory
from vtv.security.limits import RateLimiter
from vtv.security.shared_state import SqliteSharedStore


def _var_root(settings: Settings) -> Path:
    return settings.storage_root.parent


def storage_provider(settings: Settings) -> StorageProvider:
    """Local disk or S3, decided by whether S3 credentials are present.

    One function, called by `build`, so the API and the worker cannot end up on
    different backends — which is not a hypothetical: they are separate
    processes reading the same environment, and the failure looks like a render
    that succeeds and then 404s on download.

    **There is no `STORAGE_BACKEND` setting**, deliberately. A name and a
    credential can disagree, and the direction they disagree in is the dangerous
    one: `STORAGE_BACKEND=s3` with an unset secret would have to either crash or
    fall back to disk, and a fallback here means a production deployment writing
    customer footage to a container filesystem that is discarded on the next
    deploy, with nothing in the logs. Credentials cannot disagree with
    themselves.

    **Local disk is not refused in production**, and the first version of this
    function was wrong to. Local disk is per-replica, so it is only correct when
    every replica shares a filesystem — but that is exactly the deployment this
    system already requires for SQLite: `queue.db` is a file that the API and
    every worker open, and the multi-worker run in `docs/DEPLOYMENT.md` is two
    workers on one Docker volume. Refusing local storage there while happily
    running the queue and the database off the same volume would be arbitrary.

    What *is* refused is the combination that cannot be right. A `postgresql://`
    database URL means the replicas were separated — that is the only reason to
    move off SQLite — and separated replicas do not share a disk. So Postgres
    plus local storage is a deployment where the render succeeds on the worker
    and 404s on the API, every time, and it fails at boot instead.
    """
    if settings.storage_access_key and settings.storage_secret_key:
        return S3StorageProvider(
            bucket=settings.storage_bucket,
            endpoint=settings.storage_endpoint,
            region=settings.storage_region,
            access_key=settings.storage_access_key,
            secret_key=settings.storage_secret_key,
            path_style=settings.storage_path_style,
        )
    if not settings.database_url.startswith("sqlite:"):
        raise VTVError(
            "STORAGE_ACCESS_KEY and STORAGE_SECRET_KEY are required when "
            f"DATABASE_URL is {settings.database_url.split(':', 1)[0]!r}: a "
            "shared database means the replicas do not share a filesystem, so "
            "media written by one is missing on every other",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return LocalStorageProvider(
        settings.storage_root,
        bucket=settings.storage_bucket,
        signing_key=settings.signing_key,
    )


def repository_path(settings: Settings) -> Path:
    """Where projects and documents live.

    A single function so the API and the worker cannot disagree about it, which
    they would eventually do if each parsed the URL itself.
    """
    url = settings.database_url
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", ""))
    # No silent fallback. This used to return a local SQLite file for any URL it
    # did not recognise, which meant configuring `postgresql://…` produced a
    # working system that quietly ignored the database you pointed it at — every
    # replica writing to its own private file, with no error anywhere. A
    # deployment that has chosen an unimplemented backend must fail to start.
    raise VTVError(
        f"unsupported database URL scheme: {url.split(':', 1)[0]!r}. "
        "Only sqlite:/// is implemented; PostgreSQL is P1-5 in docs/REMEDIATION.md",
        code=ErrorCode.SCHEMA_INVALID,
    )


def queue_path(settings: Settings) -> Path:
    """Where the durable queue lives. Shared by every API replica and worker."""
    return _var_root(settings) / "queue.db"


def shared_state_path(settings: Settings) -> Path:
    """Where rate-limit buckets live. Shared by every API replica.

    Correctness-critical state must not be process-local: N replicas with N
    private buckets enforce N times the limit, which is how scaling out used to
    weaken this system's authentication.
    """
    return _var_root(settings) / "shared.db"


def directory_path(settings: Settings) -> Path:
    """Tenants, users, memberships and API keys."""
    return _var_root(settings) / "directory.db"


def audit_path(settings: Settings) -> Path:
    """The append-only record of who did what."""
    return _var_root(settings) / "audit.db"


def usage_path(settings: Settings) -> Path:
    """Metered usage and quota reservations."""
    return _var_root(settings) / "usage.db"


#: Every database this deployment owns, as ``name -> path``. One table, so the
#: migrator cannot know about a subset — which it did: an earlier version listed
#: three of the six, and the migration that denormalises `organisations` was
#: pointed at the repository database, where that table does not live. It failed
#: on any fresh deployment, and only a test that ran `upgrade()` for real found
#: it.
DATABASES: dict[str, Callable[[Settings], Path]] = {
    "repository": repository_path,
    "queue": queue_path,
    "shared": shared_state_path,
    "directory": directory_path,
    "audit": audit_path,
    "usage": usage_path,
}


@dataclass
class MeteredSpendAuthoriser:
    """Turns ``QuotaKind.PROVIDER_SPEND_USD`` from a meter into a breaker.

    Implements `vtv.pipeline.generation.SpendAuthoriser`. It lives here, in the
    composition root, because the router must not learn what a plan is — it
    knows only that something can veto a spend.

    The subtlety is ``_pending``. Provider spend reaches the usage meter once,
    when a render job settles, which is *after* every call that job made. A
    check against the meter alone would therefore authorise a runaway loop a
    thousand times over from the same stale figure — the exact failure
    `billing/plans.py` claims this quota prevents. So spend the router reports
    is held here as uncommitted, counted against the allowance immediately, and
    drained as the meter's committed total catches up at settlement. Draining
    rather than clearing is what stops the tenant being charged twice for the
    same dollar once the job boundary records it.

    Two honest limitations, neither of which fails open by more than one job:

    * the tally is per process, so two workers do not see each other's in-flight
      spend — each is still bounded by the committed figure they share;
    * `vtv.jobs._settle` records the job's total spend a second time under its
      own idempotency key. Until that record is removed the committed figure
      double-counts, which blocks a tenant *earlier* than their true spend.
    """

    meter: UsageMeter
    #: ``(organisation, period) -> USD spent but not yet in the meter``.
    _pending: dict[tuple[str, str], float] = field(default_factory=dict)
    #: The committed total each key was last reconciled against.
    _committed_seen: dict[tuple[str, str], float] = field(default_factory=dict)

    def authorise(self, *, organisation_id: str, amount_usd: float) -> None:
        verdict = self.meter.check(
            organisation_id=organisation_id,
            kind=QuotaKind.PROVIDER_SPEND_USD,
            requested=amount_usd + self._uncommitted(organisation_id),
        )
        if verdict.allowed:
            return
        raise QuotaExceeded(
            f"provider spend for {organisation_id} is {verdict.used:.4f} of a "
            f"{verdict.limit:g} USD monthly allowance; this call could cost "
            f"{amount_usd:.4f} more",
            user_message=(
                "This account has reached its monthly limit for AI generation "
                "spend. Upgrade your plan to raise the limit, or wait for it to "
                "reset at the start of next month."
            ),
        )

    def note_spend(self, *, organisation_id: str, amount_usd: float) -> None:
        if amount_usd <= 0:
            return
        key = (organisation_id, UsagePeriod.containing().key)
        self._pending[key] = self._pending.get(key, 0.0) + amount_usd

    def _uncommitted(self, organisation_id: str) -> float:
        """In-flight spend, after crediting anything the meter now knows about."""
        period = UsagePeriod.containing()
        key = (organisation_id, period.key)
        committed = self.meter.total(
            organisation_id=organisation_id,
            kind=QuotaKind.PROVIDER_SPEND_USD,
            period=period,
        )
        settled_since = committed - self._committed_seen.get(key, 0.0)
        if settled_since > 0:
            self._pending[key] = max(0.0, self._pending.get(key, 0.0) - settled_since)
            self._committed_seen[key] = committed
        return self._pending.get(key, 0.0)


@dataclass
class PlanSeatAuthoriser:
    """Turns ``QuotaKind.SEATS`` from a declaration into a gate.

    Implements `vtv.security.directory.SeatAuthoriser`. Seats are a *level*,
    not a monthly sum of events — the same distinction `UsageMeter.check_level`
    exists for — so this asks the plan directly rather than the usage meter:
    removing a member must give the seat back immediately, and a period-scoped
    sum of "member added" events never would, and it would also reset every
    month, letting a one-seat plan add a second member every time the billing
    period turns.

    No in-flight tally like `MeteredSpendAuthoriser` needs one. Spend and
    generated assets are metered *after the fact*, from a job that can run many
    provider calls before anything is recorded, which is what makes a stale
    check dangerous. Adding a member is a single synchronous call through
    `Directory.add_member` with nothing in between it and the row it writes, so
    the count `Directory` passes in is never stale.
    """

    #: Resolves a tenant's plan. `directory.tier_of`, always — see `build` —
    #: kept as a callable rather than a `Directory` reference so this class
    #: does not need to know what a `Directory` is, only how to ask one thing.
    tier_of: Callable[[str], object]

    def authorise(self, *, organisation_id: str, current_members: int) -> None:
        plan = plan_for(self.tier_of(organisation_id))  # type: ignore[arg-type]
        if plan.allows(QuotaKind.SEATS, current_members, adding=1.0):
            return
        raise QuotaExceeded(
            f"organisation {organisation_id} already has {current_members:g} "
            f"of {plan.limit(QuotaKind.SEATS):g} seats its plan allows",
            user_message=(
                "This account has used all the seats its plan includes. "
                "Upgrade your plan to add another member, or remove one who "
                "is not using their seat."
            ),
        )


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
    #: P1-6. Shared by every component in this process and scraped by
    #: `/metrics`. One registry per assembly, never a module-level global:
    #: two tests in one interpreter must not see each other's numbers.
    metrics: Metrics
    storage: StorageProvider
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
    #: The local provider trace, or a null object. Held on the assembly so the
    #: worker can wrap each job in it and print what that job spent.
    trace: object = field(default_factory=lambda: None)
    scripts: dict[str, str] = field(default_factory=dict)

    def register_script(self, ref: ObjectRef, script: str) -> None:
        """Attach a development transcript to a recording (see below)."""
        self.scripts[ref.key] = script

    def register_script_for_key(self, key: str, script: str) -> None:
        """Same, addressed by storage key.

        The worker holds a key from a job payload rather than a reference, and
        the development aligner is looked up by key.
        """
        self.scripts[key] = script


def execution_registry(settings: Settings) -> Registry:
    """Which execution targets this assembly can actually serve.

    Built here because the composition root is the only place that knows what
    was assembled. `contracts/execution.py` defines what a target *is*;
    `adapters/media/compute.py` reports what the hardware *has*; neither of
    them may decide what exists, because neither of them builds anything.

    Exactly one target is registered today: the CPU renderer, on whichever
    machine this worker happens to be. That is the honest answer, and the
    reason a "use my graphics card" control is not offered — a target with no
    backend behind it is never listed as ready.

    The unavailable targets carry a reason, because "no GPU compositor is built
    yet" and "this machine has no supported card" send a user to two different
    actions, and an interface that renders both as a disabled button sends them
    to neither.
    """
    from vtv.adapters.media import compute
    from vtv.adapters.render.cpu_backend import CpuRenderBackend, GpuRenderBackend

    here = (
        Machine.LOCAL
        if settings.execution_machine == Machine.LOCAL.value
        else Machine.CLOUD
    )
    found = compute.detect()
    cpu = (
        ExecutionTarget.LOCAL_CPU if here is Machine.LOCAL else ExecutionTarget.CLOUD_CPU
    )
    registry = Registry()
    # A real backend, not the renderer class. The registry answers "what can
    # draw a segment here", and the thing that draws a segment is a
    # `RenderBackend` — registering the whole renderer would have made the
    # first caller to actually use one fail at the call.
    registry.register(
        cpu,
        CpuRenderBackend(
            where=cpu,
            hardware_encoder=(
                found.hardware_encoders[0] if found.hardware_encoders else ""
            ),
        ),
    )
    if found.hardware_encoders:
        names = ", ".join(compute.label_for(name) for name in found.hardware_encoders)
        gpu = (
            f"{names} can encode here, but composition — about 77% of render "
            "time — has no GPU path yet"
        )
    else:
        gpu = (
            "no GPU compositor is built yet, and composition is about 77% of "
            "render time"
        )
    # A GPU target is registered only when a device initialised, compiled the
    # pipelines and drew a calibration frame that matched the CPU reference.
    # Anything less and it stays a note explaining why — the Phase A rule,
    # applied to the thing Phase B2 adds.
    from vtv.adapters.render.gpu_probe import probe

    seen = probe()
    if seen.available:
        gpu_target = (
            ExecutionTarget.LOCAL_GPU if here is Machine.LOCAL
            else ExecutionTarget.CLOUD_GPU
        )
        registry.register(
            gpu_target,
            GpuRenderBackend(
                where=gpu_target,
                hardware_encoder=(
                    found.hardware_encoders[0] if found.hardware_encoders else ""
                ),
            ),
        )
        gpu = f"{seen.vendor} {seen.device} via {seen.api}".strip()
    else:
        gpu = seen.reason or gpu
    registry.notes.setdefault(ExecutionTarget.LOCAL_GPU, gpu)
    registry.notes.setdefault(ExecutionTarget.CLOUD_GPU, gpu)
    absent = (
        ExecutionTarget.CLOUD_CPU if cpu is ExecutionTarget.LOCAL_CPU
        else ExecutionTarget.LOCAL_CPU
    )
    registry.notes[absent] = (
        "the desktop engine is not installed"
        if absent is ExecutionTarget.LOCAL_CPU
        else "this build has no cloud execution configured"
    )
    return registry


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

    # P1-6. Metrics are derived from the event stream rather than instrumented
    # separately, so a new event reaches its counter or is visibly missing from
    # one table. Always on: a metrics registry that a deployment can forget to
    # enable is a dashboard that is empty during the incident it was built for.
    metrics = register_defaults(Metrics())
    events.subscribe(metrics_handler(metrics))
    if log_events:
        events.subscribe(correlated_log_handler())

    # A per-process signing key means replica A mints a media URL replica B
    # rejects, so downloads fail at a rate equal to the chance of hitting a
    # different pod. Development may generate one; production must be given one.
    if settings.is_production and not settings.signing_key:
        raise VTVError(
            "VTV_SIGNING_KEY is required in production: without a shared key, "
            "signed media URLs are only valid on the replica that issued them",
            code=ErrorCode.SCHEMA_INVALID,
        )
    storage = storage_provider(settings)
    ledger = CostLedger()
    # The tenant directory and the usage meter are built before the router
    # because the router now depends on them: there is no assembly in which a
    # provider call is authorised by nothing. Constructing the router without a
    # spend authoriser is possible in a unit test and impossible here.
    directory = Directory(path=directory_path(settings))
    # QuotaKind.SEATS used to be declared with a per-tier limit and checked by
    # nothing: `Directory.add_member` inserted a row and moved on regardless of
    # the plan. Attaching the authoriser after construction, rather than passing
    # it in, is what lets it close over `directory.tier_of` without `Directory`
    # needing to exist before its own constructor returns.
    directory.seat_authoriser = PlanSeatAuthoriser(tier_of=directory.tier_of)
    usage = UsageMeter(path=usage_path(settings), tier_of=directory.tier_of)
    trace = build_trace(settings)
    router = GenerationRouter(
        events=events,
        ledger=ledger,
        # Attached to the router rather than to a service, because the router is
        # the one object every paid call passes through. A trace anywhere else
        # would be a trace with holes in it.
        trace=trace,
        spend_authoriser=MeteredSpendAuthoriser(meter=usage),
        # QuotaKind.GENERATED_ASSETS was declared with a per-tier limit — zero
        # on Free — and checked nowhere: `jobs._record_generated_assets` only
        # counts an asset after a render job settles, which is after every
        # asset that job bought. Wiring this here is what turns "checked and
        # recorded" into "checked before it happens", the same fix already
        # applied to PROVIDER_SPEND_USD above.
        asset_authoriser=GeneratedAssetAllowance(meter=usage),
    )
    capabilities = Capabilities()
    scripts: dict[str, str] = {}

    # -- speech to text ---------------------------------------------------
    if settings.speech_to_text_endpoint and settings.speech_to_text_api_key:
        # The policy the operator has asserted about this vendor. Passed
        # explicitly, because the default is "assume it retains and trains on
        # what we send", and the router excludes such a provider from the voice
        # path — correctly, and permanently, since nothing here ever said
        # otherwise. Every recording died at transcription with "no provider
        # available … within data-policy constraints" while `/health` reported
        # transcription as working.
        policy = DataPolicy(
            trains_on_input=settings.speech_to_text_trains_on_input,
            retains_input=settings.speech_to_text_retains_input,
            dpa_in_place=settings.speech_to_text_dpa_in_place,
        )
        speech: object = HttpSpeechToTextProvider(
            storage=storage,
            endpoint=settings.speech_to_text_endpoint,
            api_key=settings.speech_to_text_api_key,
            data_policy=policy,
        )
        # A credential is not the same thing as a usable provider, and this is
        # where the two used to be confused. `real_transcription` was set from
        # the credential alone, so `/health` reported transcription as working
        # while the router refused every request for it — the audio was
        # accepted, the job ran, and it died at the second stage with "no
        # provider available … within data-policy constraints".
        #
        # The first fix here refused to boot. That was too blunt: an operator
        # trying transcription on their own laptop should not have to assert a
        # signed agreement before the process will start. Reporting the
        # capability honestly is enough — the Studio already tells the user
        # transcription is unavailable on this install, and now it is telling
        # them the truth.
        capabilities.real_transcription = policy.is_acceptable_for_user_voice
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
                model=settings.image_generation_model,
                quality=settings.image_generation_quality or None,
            ),
            GenerationKind.IMAGE,
        )
        # A credential is not a working provider — the same confusion
        # `real_transcription` used to carry, and the same fix. This used to
        # construct the provider with the literal string "default" as the
        # vendor's `model` field, which is not a valid model name for any
        # image API, so every request was rejected by the vendor while
        # `/health` reported `real_image_generation: true`. The provider is
        # still registered with no model configured — setting
        # `VTV_IMAGE_GENERATION_MODEL` later takes effect on the next boot
        # without anything else changing — but the capability is reported
        # honestly rather than claimed.
        capabilities.real_image_generation = settings.image_generation_model != "unset"
    if settings.video_generation_endpoint and settings.video_generation_api_key:
        # Which contract the endpoint speaks is configured, not inferred. The
        # two are different APIs — different create path, different poll path,
        # and OpenAI hands back bytes where the other hands back a URL — so a
        # guess from the URL would be wrong silently, and the symptom would be
        # "video is configured and nothing ever appears". See
        # `Settings.video_generation_dialect`.
        video: object
        if settings.video_generation_dialect == "openai":
            video = OpenAIVideoGenerationProvider(
                storage=storage,
                endpoint=settings.video_generation_endpoint,
                api_key=settings.video_generation_api_key,
                model=settings.video_generation_model,
            )
        else:
            video = HttpVideoGenerationProvider(
                storage=storage,
                endpoint=settings.video_generation_endpoint,
                api_key=settings.video_generation_api_key,
                model=settings.video_generation_model,
            )
        router.register(video, GenerationKind.VIDEO)
        # Same reasoning as `real_image_generation` above.
        capabilities.real_video_generation = settings.video_generation_model != "unset"

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
    # Paths come from the table above, never from a literal here: a second
    # place that names a database file is a second place that can disagree with
    # the migrator about where it is.
    # `directory` and `usage` are built above, before the router that spends
    # against them.
    audit = AuditLog(path=audit_path(settings), events=events)
    authorizer = Authorizer(record=audit, is_suspended=directory.is_suspended)
    limiter = RateLimiter(store=SqliteSharedStore(shared_state_path(settings)))

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
        renderer=FfmpegRenderer(
            storage=storage,
            events=events,
            # Beside the storage root rather than in a temporary directory that
            # is deleted when the render ends. Half-finished segments are the
            # only reason a crashed four-hour encode does not start again from
            # frame zero, so they have to outlive the process that made them.
            workdir=workdir or settings.storage_root.parent / "renders",
            workers=settings.render_workers,
        ),
        events=events,
        plan_gate=PlanGate(events=events),
        ledger=ledger,
        narration=narration,
        consistency=ConsistencyEngine(events=events),
    )

    return Assembly(
        settings=settings,
        events=events,
        metrics=metrics,
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
        trace=trace,
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
