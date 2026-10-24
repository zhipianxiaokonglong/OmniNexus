"""Stable public error codes and exception types.

Codes are part of the HTTP contract documented in docs/api.md. Add new codes,
never rename existing ones without a contract version bump.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Input / validation
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_MODALITY = "UNSUPPORTED_MODALITY"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    EVENT_DUPLICATE = "EVENT_DUPLICATE"

    # Session / resources
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    PROMPT_NOT_FOUND = "PROMPT_NOT_FOUND"
    PROMPT_VERSION_NOT_FOUND = "PROMPT_VERSION_NOT_FOUND"
    PROMPT_BUILTIN_PROTECTED = "PROMPT_BUILTIN_PROTECTED"
    PROMPT_INVALID = "PROMPT_INVALID"

    # Models / providers
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    NO_PROVIDER_AVAILABLE = "NO_PROVIDER_AVAILABLE"
    PROVIDER_AUTH = "PROVIDER_AUTH"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_BAD_RESPONSE = "PROVIDER_BAD_RESPONSE"
    ALL_PROVIDERS_FAILED = "ALL_PROVIDERS_FAILED"
    GENERATION_INTERRUPTED = "GENERATION_INTERRUPTED"

    # Memory pipeline (local adapter errors; the wire contract has MEM_* codes)
    MEMORY_UNAVAILABLE = "MEMORY_UNAVAILABLE"
    MEMORY_TIMEOUT = "MEMORY_TIMEOUT"
    MEMORY_BAD_RESPONSE = "MEMORY_BAD_RESPONSE"

    # Search
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
    SEARCH_NOT_CONFIGURED = "SEARCH_NOT_CONFIGURED"

    # Generic
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_FOUND = "NOT_FOUND"


class OmniError(Exception):
    """Base exception that is safe to map to an HTTP response.

    ``message`` must never contain API keys or conversation content.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            error["details"] = self.details
        return {"error": error}


class InvalidInputError(OmniError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_INPUT, message, status_code=422, details=details)


class NotFoundError(OmniError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(code, message, status_code=404)


class ProviderError(OmniError):
    """A provider call failed. ``kind`` drives retry/fallback policy."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.PROVIDER_ERROR,
        status_code: int = 502,
        retryable: bool = True,
        retry_after_s: float | None = None,
        provider: str | None = None,
        model: str | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            status_code=status_code,
            retryable=retryable,
            details={
                k: v
                for k, v in {
                    "provider": provider,
                    "model": model,
                    "upstream_status": upstream_status,
                }.items()
                if v is not None
            },
        )
        self.retry_after_s = retry_after_s
        self.provider = provider
        self.model = model


class AllProvidersFailedError(OmniError):
    def __init__(self, attempts: list[dict[str, Any]]) -> None:
        super().__init__(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "All candidate models failed. Check provider configuration and try again.",
            status_code=503,
            retryable=False,
            details={"attempts": attempts},
        )
