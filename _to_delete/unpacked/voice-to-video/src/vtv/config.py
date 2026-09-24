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


class Settings(VTVModel):
    """Everything the process needs to know about where it is running."""

    env: str = "development"
    log_level: str = "info"

    # Storage -------------------------------------------------------------
    storage_root: Path = Path("./var/storage")
    storage_bucket: str = "vtv-media-dev"
    storage_endpoint: str | None = None
    storage_region: str = "us-east-1"

    # Datastores ----------------------------------------------------------
    database_url: str = "sqlite:///./var/vtv.db"
    redis_url: str | None = None

    # Providers -----------------------------------------------------------
    # Credentials only. *Which* provider handles a request is a routing
    # decision made from declared capabilities, never a hard-coded choice.
    speech_to_text_api_key: str | None = None
    speech_to_text_endpoint: str | None = None
    text_generation_api_key: str | None = None
    text_generation_endpoint: str | None = None
    text_generation_model: str = "unset"
    image_generation_api_key: str | None = None
    image_generation_endpoint: str | None = None
    video_generation_api_key: str | None = None
    video_generation_endpoint: str | None = None
    asset_search_endpoint: str = "https://api.openverse.org/v1"

    # Budgets and limits --------------------------------------------------
    max_project_cost_usd: float = 1.00
    max_recording_seconds: float = 1800.0
    temporary_retention_hours: int = 24
    max_upload_bytes: int = 200 * 1024 * 1024

    # Rendering -----------------------------------------------------------
    renderer: str = "ffmpeg"  # ffmpeg | remotion
    remotion_project_dir: Path = Path("./apps/remotion")

    # Security ------------------------------------------------------------
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    signed_url_seconds: int = 900

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        source = environ if environ is not None else dict(os.environ)

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
            database_url=get("DATABASE_URL") or "sqlite:///./var/vtv.db",
            redis_url=get("REDIS_URL"),
            speech_to_text_api_key=get("SPEECH_TO_TEXT_API_KEY"),
            speech_to_text_endpoint=get("SPEECH_TO_TEXT_ENDPOINT"),
            text_generation_api_key=get("TEXT_GENERATION_API_KEY"),
            text_generation_endpoint=get("TEXT_GENERATION_ENDPOINT"),
            text_generation_model=get("TEXT_GENERATION_MODEL") or "unset",
            image_generation_api_key=get("IMAGE_GENERATION_API_KEY"),
            image_generation_endpoint=get("IMAGE_GENERATION_ENDPOINT"),
            video_generation_api_key=get("VIDEO_GENERATION_API_KEY"),
            video_generation_endpoint=get("VIDEO_GENERATION_ENDPOINT"),
            asset_search_endpoint=get("ASSET_SEARCH_ENDPOINT")
            or "https://api.openverse.org/v1",
            max_project_cost_usd=number("MAX_PROJECT_COST_USD", 1.00),
            max_recording_seconds=number("MAX_RECORDING_SECONDS", 1800.0),
            temporary_retention_hours=int(number("TEMPORARY_RETENTION_HOURS", 24)),
            renderer=get("RENDERER") or "ffmpeg",
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
            if key.endswith("_api_key"):
                payload[key] = "***set***" if payload[key] else None
        return payload


__all__ = ["Settings"]
