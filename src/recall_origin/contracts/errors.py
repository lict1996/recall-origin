"""Stable public error codes and exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    code: str
    exit_code: int
    retryable: bool = False


VALIDATION_ERROR = ErrorSpec("VALIDATION_ERROR", 2)
SCOPE_DENIED = ErrorSpec("SCOPE_DENIED", 3)
NOT_FOUND = ErrorSpec("NOT_FOUND", 4)
REVISION_CONFLICT = ErrorSpec("REVISION_CONFLICT", 5)
IDEMPOTENCY_KEY_REUSED = ErrorSpec("IDEMPOTENCY_KEY_REUSED", 6)
TEMPORARY_FAILURE = ErrorSpec("TEMPORARY_FAILURE", 7, retryable=True)
FEATURE_NOT_ENABLED = ErrorSpec("FEATURE_NOT_ENABLED", 8)
PURGE_REGISTRY_REQUIRED = ErrorSpec("PURGE_REGISTRY_REQUIRED", 9)


class RecallOriginError(Exception):
    """An expected, machine-readable application failure."""

    def __init__(
        self,
        spec: ErrorSpec,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.spec = spec
        self.message = message
        self.details = details or {}
