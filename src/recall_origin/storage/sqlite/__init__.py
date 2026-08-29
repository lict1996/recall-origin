"""High-reliability SQLite implementation."""

from recall_origin.storage.sqlite.connection import (
    SQLiteConnectionProfile,
    SQLiteStore,
    configure_connection,
)
from recall_origin.storage.sqlite.errors import (
    MigrationError,
    PurgeRegistryError,
    SQLiteFeatureError,
    SQLiteStorageError,
)
from recall_origin.storage.sqlite.migrations import (
    Migration,
    load_migrations,
    run_migrations,
    sqlite_transaction,
)
from recall_origin.storage.sqlite.purge_registry import (
    PurgeRegistry,
    PurgeTombstone,
    PurgeVerification,
)

__all__ = [
    "Migration",
    "MigrationError",
    "PurgeRegistry",
    "PurgeRegistryError",
    "PurgeTombstone",
    "PurgeVerification",
    "SQLiteConnectionProfile",
    "SQLiteFeatureError",
    "SQLiteStorageError",
    "SQLiteStore",
    "configure_connection",
    "load_migrations",
    "run_migrations",
    "sqlite_transaction",
]
