"""The error and lifecycle model for the whole pipeline.

Two ideas carry most of the weight here.

**Every artefact has an explicit status.** Nothing in this system is ever merely
absent; it is ``PENDING``, ``PROCESSING``, ``READY``, ``FAILED``, ``RETRYING``,
``EXPIRED`` or ``DELETED``. A missing visual with a reason attached can be
degraded around. A missing visual with no reason attached is a bug that only
shows up in the final MP4.

**Failure is local by default.** One scene failing to acquire a visual must not
fail the project (Rule 8). The degradation ladder in :class:`DegradationStep`
encodes how far the system fell back, so the UI can tell the user *"this shot
used a simpler visual because generation timed out"* rather than showing a blank
frame or, worse, silently dropping the narration.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from vtv.contracts.base import VTVModel, utc_now


class Status(str, Enum):
    """Lifecycle status shared by recordings, scenes, assets and render jobs."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    RETRYING = "retrying"
    EXPIRED = "expired"
    DELETED = "deleted"

    @property
    def is_terminal(self) -> bool:
        return self in {Status.READY, Status.FAILED, Status.EXPIRED, Status.DELETED}

    @property
    def is_usable(self) -> bool:
        """Whether the artefact can be consumed by the next pipeline stage."""
        return self is Status.READY


class ErrorCategory(str, Enum):
    """Coarse classification that determines how the system reacts."""

    #: Input or model output failed schema validation. Never retry blindly.
    VALIDATION = "validation"
    #: An upstream provider misbehaved: 5xx, timeout, malformed response.
    PROVIDER = "provider"
    #: Provider is up but refused: quota, rate limit, content filter.
    PROVIDER_REFUSED = "provider_refused"
    #: Our own rules said no: licence not permissive, budget exceeded.
    POLICY = "policy"
    #: The thing being referenced does not exist or has expired.
    NOT_FOUND = "not_found"
    #: Operation exceeded its latency budget.
    TIMEOUT = "timeout"
    #: A bug on our side.
    INTERNAL = "internal"


class ErrorCode(str, Enum):
    """Stable, machine-readable error codes.

    These are part of the public contract: they appear in API responses and in
    metrics, so they may be added to but must not be renamed.
    """

    SCHEMA_INVALID = "schema_invalid"
    AUDIO_UNREADABLE = "audio_unreadable"
    AUDIO_TOO_LONG = "audio_too_long"
    AUDIO_SILENT = "audio_silent"
    TRANSCRIPTION_FAILED = "transcription_failed"
    UNDERSTANDING_FAILED = "understanding_failed"
    SCENE_PLANNING_FAILED = "scene_planning_failed"
    VISUAL_PLANNING_FAILED = "visual_planning_failed"
    ASSET_NOT_FOUND = "asset_not_found"
    ASSET_LICENSE_UNACCEPTABLE = "asset_license_unacceptable"
    GENERATION_FAILED = "generation_failed"
    GENERATION_REFUSED = "generation_refused"
    BUDGET_EXCEEDED = "budget_exceeded"
    LATENCY_EXCEEDED = "latency_exceeded"
    RENDER_FAILED = "render_failed"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    OBJECT_EXPIRED = "object_expired"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    NOT_AUTHENTICATED = "not_authenticated"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    UPLOAD_REFUSED = "upload_refused"
    TENANT_SUSPENDED = "tenant_suspended"
    INTERNAL_ERROR = "internal_error"


#: Which categories are worth retrying with the same inputs.
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {ErrorCategory.PROVIDER, ErrorCategory.TIMEOUT}
)


class ErrorInfo(VTVModel):
    """Serialisable description of a failure, safe to store and to return.

    ``message`` is for engineers and goes to logs. ``user_message`` is for the
    person who spoke into the microphone and must never leak provider names,
    stack traces, prompts or internal identifiers.
    """

    code: ErrorCode
    category: ErrorCategory
    message: str = Field(max_length=2000)
    user_message: str | None = Field(default=None, max_length=400)
    retryable: bool = False
    attempt: int = Field(default=1, ge=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    #: Non-sensitive structured context: ids, durations, counts. Never prompts,
    #: transcript text, API keys or user content.
    context: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(
        cls,
        code: ErrorCode,
        category: ErrorCategory,
        message: str,
        **kwargs: Any,
    ) -> ErrorInfo:
        kwargs.setdefault("retryable", category in RETRYABLE_CATEGORIES)
        return cls(code=code, category=category, message=message, **kwargs)


class DegradationReason(str, Enum):
    """Why the system settled for something other than its first choice."""

    PROVIDER_FAILED = "provider_failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    LATENCY_EXCEEDED = "latency_exceeded"
    LICENSE_UNACCEPTABLE = "license_unacceptable"
    NO_SUITABLE_ASSET = "no_suitable_asset"
    QUALITY_REJECTED = "quality_rejected"
    SAFETY_REFUSED = "safety_refused"


class DegradationStep(VTVModel):
    """One rung of the fallback ladder that was taken for a scene.

    Recording these makes the pipeline auditable: given a finished video we can
    say exactly which shots are the system's first choice and which are
    compromises, which is the raw material for the evaluation system (Stage 16).
    """

    from_strategy: str
    to_strategy: str
    reason: DegradationReason
    error: ErrorInfo | None = None


class VTVError(Exception):
    """Base exception. Carries an :class:`ErrorInfo` so nothing is lost.

    Application code raises subclasses of this; boundaries (API handlers, job
    workers) catch ``VTVError`` and turn ``.info`` into a response or a status
    update. An exception that is not a ``VTVError`` reaching a boundary is by
    definition a bug and is reported as ``INTERNAL_ERROR``.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    category: ErrorCategory = ErrorCategory.INTERNAL
    user_message: str | None = None

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        category: ErrorCategory | None = None,
        user_message: str | None = None,
        context: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> None:
        super().__init__(message)
        self.info = ErrorInfo.of(
            code or self.code,
            category or self.category,
            message,
            user_message=user_message or self.user_message,
            context=context or {},
            attempt=attempt,
        )

    @property
    def retryable(self) -> bool:
        return self.info.retryable


class ValidationFailed(VTVError):
    code = ErrorCode.SCHEMA_INVALID
    category = ErrorCategory.VALIDATION
    user_message = "Something in that request did not look right."


class ProviderError(VTVError):
    code = ErrorCode.PROVIDER_UNAVAILABLE
    category = ErrorCategory.PROVIDER
    user_message = "A service we depend on is having trouble. We are retrying."


class ProviderRefused(VTVError):
    code = ErrorCode.GENERATION_REFUSED
    category = ErrorCategory.PROVIDER_REFUSED
    user_message = "We could not create that visual, so we used an alternative."


class PolicyViolation(VTVError):
    code = ErrorCode.ASSET_LICENSE_UNACCEPTABLE
    category = ErrorCategory.POLICY
    user_message = "We could not use that material under its licence."


class BudgetExceeded(VTVError):
    code = ErrorCode.BUDGET_EXCEEDED
    category = ErrorCategory.POLICY
    user_message = "This project reached its generation budget."


class NotFound(VTVError):
    code = ErrorCode.ASSET_NOT_FOUND
    category = ErrorCategory.NOT_FOUND
    user_message = "We could not find that."


class TimeoutExceeded(VTVError):
    code = ErrorCode.LATENCY_EXCEEDED
    category = ErrorCategory.TIMEOUT
    user_message = "That took too long, so we moved on."


class RenderFailed(VTVError):
    code = ErrorCode.RENDER_FAILED
    category = ErrorCategory.INTERNAL
    user_message = "We could not finish the video. Your project is saved."


__all__ = [
    "RETRYABLE_CATEGORIES",
    "BudgetExceeded",
    "DegradationReason",
    "DegradationStep",
    "ErrorCategory",
    "ErrorCode",
    "ErrorInfo",
    "NotFound",
    "PolicyViolation",
    "ProviderError",
    "ProviderRefused",
    "RenderFailed",
    "Status",
    "TimeoutExceeded",
    "VTVError",
    "ValidationFailed",
]
