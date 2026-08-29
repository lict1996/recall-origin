"""SQLite adapter failures with stable application-level semantics."""

from __future__ import annotations

from typing import Any

from recall_origin.contracts.errors import (
    PURGE_REGISTRY_REQUIRED,
    RecallOriginError,
)


class SQLiteStorageError(RuntimeError):
    """Base class for authoritative-store failures."""


class MigrationError(SQLiteStorageError):
    """The on-disk migration history is missing, changed, or cannot be applied."""


class SQLiteFeatureError(SQLiteStorageError):
    """The linked SQLite runtime does not provide a required feature."""


class PurgeRegistryError(RecallOriginError):
    """The independent deletion registry cannot be trusted.

    This is deliberately fail-closed and maps to the public
    ``PURGE_REGISTRY_REQUIRED`` error code.
    """

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(PURGE_REGISTRY_REQUIRED, message, details=details)
