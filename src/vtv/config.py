"""Runtime configuration.

Read from the environment once, at process start, and passed down explicitly.
Nothing deeper in the system reads ``os.environ`` — a module that reaches for a
global at import time cannot be tested twice with different settings.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from vtv.contracts.base import VTVModel

#: Any field whose name ends in one of these is a credential and is replaced in
#: `Settings.redacted`. A suffix rule rather than a list of field names, because
#: the list version only redacts the secrets somebody remembered to add to it —
#: `storage_secret_key` was added long after `redacted` was written and would
#: have been logged in the clear on every boot.
_SECRET_SUFFIXES = ("_api_key", "_key", "_secret", "_token", "_password")

#: Where `read_env_file` looks when nothing overrides it.
ENV_FILE = ".env"

#: Overrides the location. Read from the real environment, never from the file
#: itself — a file that can redirect which file is read is a loop.
ENV_FILE_VAR = "VTV_ENV_FILE"


def _flag(raw: str | None, *, default: bool) -> bool:
    """A boolean from the environment. Anything unrecognised keeps the default.

    Deliberately strict about what counts as true: these flags carry
    data-protection assertions, and a typo must not read as consent.
    """
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    return default


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse a dotenv file into a mapping. Missing file is an empty mapping.

    **Why this exists at all.** It did not, for a long time, and that was a
    quiet, expensive bug: `.env.example` opens with "Copy to `.env` for local
    development", every runbook says to do it, `deploy/docker-compose.yml`
    passes one — and nothing in this codebase had ever read one. `Settings`
    read `os.environ` and only `os.environ`. So an operator would set
    `VTV_TEXT_GENERATION_API_KEY` in `.env`, restart, and get a system that
    reported the capability as unconfigured with no indication why. The file
    was documentation that looked like configuration.

    Docker never hit this because `--env-file` is read by Docker, not by us,
    which is exactly why it survived a production deployment validation.

    **Precedence: the real environment always wins.** A value exported in the
    shell, injected by a secrets manager, or set by `--env-file` must beat a
    stale line in a checked-out file — the file is the fallback, never the
    authority. `Settings.from_env` applies that ordering; this function only
    parses.

    Deliberately not a dependency. `python-dotenv` is a fine library and this
    is twenty lines; the format we accept is the format the example file uses.
    """
    target = path or Path(os.environ.get(ENV_FILE_VAR) or ENV_FILE)
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A missing file is the normal case in a container. An unreadable one
        # is not worth crashing over either: the environment may well carry
        # everything needed, and refusing to boot over a file we invented a
        # convention for would be worse than starting without it.
        return {}

    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        name, separator, value = stripped.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        # Strip one matched pair of quotes, so `KEY="v"` and `KEY=v` agree.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


class Settings(VTVModel):
    """Everything the process needs to know about where it is running."""

    env: str = "development"
    log_level: str = "info"

    # Storage -------------------------------------------------------------
    #: Which backend runs is decided by whether credentials are present, not by
    #: a `STORAGE_BACKEND` string. A name and a credential can disagree —
    #: `STORAGE_BACKEND=s3` with no secret key is a deployment that believes it
    #: is on S3 and is writing to a container filesystem that disappears on the
    #: next deploy. Credentials cannot disagree with themselves. See
    #: `wiring.storage_provider`.
    storage_root: Path = Path("./var/storage")
    storage_bucket: str = "vtv-media-dev"
    storage_endpoint: str | None = None
    storage_region: str = "us-east-1"
    storage_access_key: str | None = None
    storage_secret_key: str | None = None
    #: Path-style (`host/bucket/key`). MinIO and Ceph need it; AWS and R2 accept
    #: it. Set false only for a bucket whose name is a valid DNS label and whose
    #: endpoint expects virtual-hosted style.
    storage_path_style: bool = True

    # Datastores ----------------------------------------------------------
    #: Only ``sqlite:///`` is implemented. `wiring.repository_path` raises on
    #: anything else rather than silently substituting a local file — see the
    #: comment there, and P1-5 in docs/REMEDIATION.md for PostgreSQL.
    database_url: str = "sqlite:///./var/vtv.db"

    # Providers -----------------------------------------------------------
    # Credentials only. *Which* provider handles a request is a routing
    # decision made from declared capabilities, never a hard-coded choice.
    speech_to_text_api_key: str | None = None
    speech_to_text_endpoint: str | None = None
    #: What the transcription vendor does with the audio, asserted by whoever
    #: deploys this.
    #:
    #: `DataPolicy`'s defaults are pessimistic on purpose — an unverified
    #: provider is assumed to retain and train on what it is sent, which
    #: excludes it from the voice path. That default was correct and
    #: unreachable: `wiring` built the provider with no policy at all, so every
    #: transcription request was refused by the router with "no provider
    #: available … within data-policy constraints" no matter which credential
    #: was configured, while `/health` cheerfully reported
    #: `real_transcription: true`.
    #:
    #: These are an assertion about a contract you have signed, not a switch to
    #: make an error go away. `wiring.build` refuses to start when a credential
    #: is configured and these are not set, so the failure is a sentence at boot
    #: rather than every recording dying at the second stage.
    speech_to_text_trains_on_input: bool = True
    speech_to_text_retains_input: bool = True
    speech_to_text_dpa_in_place: bool = False
    speech_synthesis_api_key: str | None = None
    speech_synthesis_endpoint: str | None = None
    speech_synthesis_model: str = "unset"
    speech_synthesis_voice: str = "alloy"
    text_generation_api_key: str | None = None
    text_generation_endpoint: str | None = None
    text_generation_model: str = "unset"
    image_generation_api_key: str | None = None
    image_generation_endpoint: str | None = None
    image_generation_model: str = "unset"
    #: `low`, `medium` or `high`, or blank to send nothing and let the vendor
    #: default apply.
    #:
    #: **Defaults to `low`, and that is a deliberate change of behaviour.** For
    #: the 1536x1024 a 16:9 shot lands on, OpenAI's published prices are $0.016
    #: at `low`, $0.063 at `medium` and $0.25 at `high` — a fifteen-fold spread.
    #: Blank sent nothing and got the vendor's default, which measured at $0.25
    #: a shot: a nine-image render cost $2.25, and an hour of video would be
    #: about $160.
    #:
    #: `low` is not a bad image. At this size it is still 1.5 megapixels, and it
    #: is a sixteenth of the price. Raising it is one line, and the right way to
    #: decide is to render the same script at both and look — which is a
    #: judgement about the product, not about the code, and not one this default
    #: should be making silently in the expensive direction.
    image_generation_quality: str = "low"
    video_generation_api_key: str | None = None
    video_generation_endpoint: str | None = None
    video_generation_model: str = "unset"
    #: Which video API the configured endpoint speaks.
    #:
    #: There is no way to tell from a URL, and guessing wrong is a 404 on every
    #: request that reads to the user as "video generation is configured and
    #: nothing ever appears". Two dialects exist because two genuinely different
    #: contracts do:
    #:
    #: * ``"poll"`` (the default) — create at ``…/video/generations``, poll by
    #:   id, download from a ``url`` on the finished job. Runway and Luma
    #:   published this shape.
    #: * ``"openai"`` — create at ``…/videos``, poll at ``…/videos/{id}``,
    #:   download the bytes from ``…/videos/{id}/content``. Set this for
    #:   ``https://api.openai.com/v1`` with a Sora model.
    video_generation_dialect: str = "poll"
    asset_search_endpoint: str = "https://api.openverse.org/v1"

    #: Write one line per provider call to a local file, and echo a readable
    #: line to the worker's output. Off by default and **refused in
    #: production**: the file contains prompt text, which is user content, and
    #: a file of user prompts sitting on a server is a data-protection incident
    #: waiting for a disk to be imaged. See `observability/trace.py`.
    trace_provider_calls: bool = False
    #: Where to write it. Blank means `provider-calls.jsonl` beside the
    #: databases, in the parent of VTV_STORAGE_ROOT.
    trace_path: str = ""

    # Budgets and limits --------------------------------------------------
    max_project_cost_usd: float = 1.00
    #: Longest video this deployment will accept, in minutes. `None` means the
    #: engineering ceiling in `contracts/scale.py` — currently four hours.
    #:
    #: Only ever *tightens*: `scale.ceiling_minutes` takes the lesser of this
    #: and that ceiling, because a `.env` file cannot make the contracts hold
    #: more than they hold. Set it to 15 for a free tier and the free tier is
    #: enforced; set it to 600 and you get 240, not a validation error a user
    #: cannot act on.
    max_project_minutes: float | None = None
    max_recording_seconds: float = 1800.0
    temporary_retention_hours: int = 24
    #: How long a render waits for the computer it was routed to before the
    #: cloud takes it instead.
    #:
    #: Configurable because the right number is a property of the deployment
    #: rather than of the code: an office where the machines are always on
    #: can afford to wait, and a consumer product where laptops close
    #: constantly cannot. Seven minutes is the default because it is well
    #: clear of a lease, so this never races the queue's own recovery of a
    #: machine that took a job and went quiet.
    device_stranded_seconds: float = 420.0
    max_upload_bytes: int = 200 * 1024 * 1024

    # Rendering -----------------------------------------------------------
    renderer: str = "ffmpeg"  # ffmpeg | remotion
    #: How many video segments to draw at once.
    #:
    #: `None` — the default — means "as many as this machine has cores, capped
    #: at `ffmpeg_renderer.MAX_WORKERS`". Frame composition is PIL and Python,
    #: so the work is CPU-bound and process-parallel; a forty-minute render
    #: measured at four times real time on one core, and that ratio is what this
    #: divides.
    #:
    #: Set it to `1` to draw inline with no pool — useful on a machine that is
    #: also serving the API, and what the tests use, because starting a worker
    #: costs more than a two-second video does.
    render_workers: int | None = None
    #: Whose computer this process runs on: `cloud` (ours) or `local` (the
    #: customer's, i.e. the desktop engine).
    #:
    #: It changes no behaviour today beyond which execution target the registry
    #: reports as ready — but it is the field the desktop build sets, and
    #: naming it now is what stops the desktop engine becoming a fork. See
    #: `contracts/execution.py`.
    execution_machine: str = "cloud"
    remotion_project_dir: Path = Path("./apps/remotion")

    # Security ------------------------------------------------------------
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    signed_url_seconds: int = 900

    #: HMAC key for signed media URLs. Must be identical across every replica:
    #: with a per-process key, a URL signed by one pod is rejected by the next,
    #: so a customer's video download fails roughly half the time behind a load
    #: balancer. `wiring.build` generates one in development and refuses to
    #: start without it in production — an ephemeral key is a correctness bug,
    #: not a convenience.
    signing_key: str | None = None

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        if environ is not None:
            # An explicit mapping is exactly what the caller said it is. Tests
            # pass one to get a known environment; reading a file underneath it
            # would make the test depend on the working directory.
            source = dict(environ)
        else:
            # File first, real environment second — so the environment wins on
            # every key it sets. See `read_env_file` for why this ordering is
            # the only safe one.
            source = {**read_env_file(), **os.environ}

        def get(key: str) -> str | None:
            value = source.get(f"VTV_{key}")
            return value if value else None

        def path(key: str, default: Path) -> Path:
            raw = get(key)
            return Path(raw) if raw else default

        def number(key: str, default: float) -> float:
            raw = get(key)
            return float(raw) if raw else default

        origins = get("ALLOWED_ORIGINS")
        return cls(
            env=get("ENV") or "development",
            log_level=get("LOG_LEVEL") or "info",
            storage_root=path("STORAGE_ROOT", Path("./var/storage")),
            storage_bucket=get("STORAGE_BUCKET") or "vtv-media-dev",
            storage_endpoint=get("STORAGE_ENDPOINT"),
            storage_region=get("STORAGE_REGION") or "us-east-1",
            storage_access_key=get("STORAGE_ACCESS_KEY"),
            storage_secret_key=get("STORAGE_SECRET_KEY"),
            storage_path_style=(get("STORAGE_PATH_STYLE") or "true").lower()
            not in {"false", "0", "no"},
            database_url=get("DATABASE_URL") or "sqlite:///./var/vtv.db",
            signing_key=get("SIGNING_KEY"),
            signed_url_seconds=int(number("SIGNED_URL_SECONDS", 900)),
            max_upload_bytes=int(number("MAX_UPLOAD_BYTES", 200 * 1024 * 1024)),
            speech_to_text_api_key=get("SPEECH_TO_TEXT_API_KEY"),
            speech_to_text_endpoint=get("SPEECH_TO_TEXT_ENDPOINT"),
            speech_to_text_trains_on_input=_flag(
                get("SPEECH_TO_TEXT_TRAINS_ON_INPUT"), default=True
            ),
            speech_to_text_retains_input=_flag(
                get("SPEECH_TO_TEXT_RETAINS_INPUT"), default=True
            ),
            speech_to_text_dpa_in_place=_flag(
                get("SPEECH_TO_TEXT_DPA"), default=False
            ),
            speech_synthesis_api_key=get("SPEECH_SYNTHESIS_API_KEY"),
            speech_synthesis_endpoint=get("SPEECH_SYNTHESIS_ENDPOINT"),
            speech_synthesis_model=get("SPEECH_SYNTHESIS_MODEL") or "unset",
            speech_synthesis_voice=get("SPEECH_SYNTHESIS_VOICE") or "alloy",
            text_generation_api_key=get("TEXT_GENERATION_API_KEY"),
            text_generation_endpoint=get("TEXT_GENERATION_ENDPOINT"),
            text_generation_model=get("TEXT_GENERATION_MODEL") or "unset",
            image_generation_api_key=get("IMAGE_GENERATION_API_KEY"),
            image_generation_endpoint=get("IMAGE_GENERATION_ENDPOINT"),
            image_generation_model=get("IMAGE_GENERATION_MODEL") or "unset",
            image_generation_quality=(
                get("IMAGE_GENERATION_QUALITY") or "low"
            ).strip().lower(),
            video_generation_api_key=get("VIDEO_GENERATION_API_KEY"),
            video_generation_endpoint=get("VIDEO_GENERATION_ENDPOINT"),
            video_generation_model=get("VIDEO_GENERATION_MODEL") or "unset",
            video_generation_dialect=(
                get("VIDEO_GENERATION_DIALECT") or "poll"
            ).strip().lower(),
            asset_search_endpoint=get("ASSET_SEARCH_ENDPOINT")
            or "https://api.openverse.org/v1",
            trace_provider_calls=_flag(
                get("TRACE_PROVIDER_CALLS"), default=False
            ),
            trace_path=get("TRACE_PATH") or "",
            max_project_cost_usd=number("MAX_PROJECT_COST_USD", 1.00),
            max_project_minutes=(
                number("MAX_PROJECT_MINUTES", 0.0) or None
            ),
            max_recording_seconds=number("MAX_RECORDING_SECONDS", 1800.0),
            temporary_retention_hours=int(number("TEMPORARY_RETENTION_HOURS", 24)),
            device_stranded_seconds=float(
                number("DEVICE_STRANDED_SECONDS", 420)
            ),
            renderer=get("RENDERER") or "ffmpeg",
            render_workers=(int(number("RENDER_WORKERS", 0.0)) or None),
            execution_machine=(get("EXECUTION_MACHINE") or "cloud").strip().lower(),
            remotion_project_dir=path("REMOTION_PROJECT_DIR", Path("./apps/remotion")),
            allowed_origins=(
                [o.strip() for o in origins.split(",")]
                if origins
                else ["http://localhost:8000"]
            ),
        )

    def redacted(self) -> dict[str, object]:
        """Loggable view. Every credential is replaced, never truncated —
        a truncated secret is still a leaked secret."""
        payload = self.model_dump(mode="json")
        for key in list(payload):
            if key.endswith(_SECRET_SUFFIXES):
                payload[key] = "***set***" if payload[key] else None
        return payload


__all__ = ["Settings"]
