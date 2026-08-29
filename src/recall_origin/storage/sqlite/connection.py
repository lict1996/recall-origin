"""Authoritative SQLite connection profile and fail-closed store lifecycle."""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from recall_origin.storage.sqlite.errors import (
    PurgeRegistryError,
    SQLiteFeatureError,
    SQLiteStorageError,
)
from recall_origin.storage.sqlite.migrations import (
    MigrationMode,
    run_migrations,
    sqlite_transaction,
)
from recall_origin.storage.sqlite.purge_registry import (
    PurgeRegistry,
    decode_registry_key,
)

MAIN_MIGRATIONS_PACKAGE = "recall_origin.storage.sqlite._migrations"
PURGE_KEY_ENV = "RECALL_ORIGIN_PURGE_KEY"


def _now_micros() -> int:
    return time.time_ns() // 1_000


@dataclass(frozen=True, slots=True)
class SQLiteConnectionProfile:
    """Durability and concurrency settings applied to every main connection."""

    durable: bool = True
    busy_timeout_ms: int = 5_000

    @property
    def synchronous(self) -> str:
        return "FULL" if self.durable else "NORMAL"


def configure_connection(
    connection: sqlite3.Connection,
    profile: SQLiteConnectionProfile,
) -> None:
    """Apply and verify the RecallOrigin SQLite connection profile."""

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {profile.busy_timeout_ms}")
    journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
    if journal_mode.lower() != "wal":
        raise SQLiteFeatureError(f"SQLite refused WAL journal mode and returned {journal_mode!r}")
    connection.execute(f"PRAGMA synchronous = {profile.synchronous}")
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    if profile.durable:
        # SQLite ignores these pragmas on platforms/VFSes that do not expose
        # the stronger fsync primitive.
        connection.execute("PRAGMA fullfsync = ON")
        connection.execute("PRAGMA checkpoint_fullfsync = ON")

    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    secure_delete = int(connection.execute("PRAGMA secure_delete").fetchone()[0])
    if foreign_keys != 1:
        raise SQLiteFeatureError("SQLite foreign-key enforcement could not be enabled")
    if secure_delete != 1:
        raise SQLiteFeatureError("SQLite secure_delete could not be enabled")


class SQLiteStore:
    """Versioned SQLite authority with an independently signed purge ledger.

    ``initialize`` is safe to call repeatedly. New databases require no
    configuration: a 32-byte key is generated beside the sidecar with mode
    ``0600``. Once the authoritative database has an identity, a missing key,
    missing registry, invalid signature, stale generation, or identity mismatch
    prevents all normal connections from being yielded.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        durable: bool = True,
        purge_registry_path: str | os.PathLike[str] | None = None,
        *,
        purge_registry_key: bytes | str | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        raw_path = os.fspath(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise ValueError(
                "SQLiteStore requires a filesystem path so the purge sidecar "
                "can survive process restarts and database restores"
            )
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")

        self.path = Path(raw_path).expanduser().resolve()
        self.profile = SQLiteConnectionProfile(
            durable=durable,
            busy_timeout_ms=busy_timeout_ms,
        )
        if purge_registry_path is None:
            self.purge_registry_path = Path(f"{self.path}.purge.sqlite3")
        else:
            self.purge_registry_path = Path(purge_registry_path).expanduser().resolve()
        if self.purge_registry_path == self.path:
            raise ValueError("purge_registry_path must be separate from the main database")
        self.purge_key_path = Path(f"{self.purge_registry_path}.key")
        self._explicit_registry_key = purge_registry_key
        self._registry: PurgeRegistry | None = None
        self._key_source = "unresolved"
        self._database_id: str | None = None
        self._schema_version: int | None = None
        self._initialized = False
        self._initialize_lock = threading.RLock()

    @property
    def purge_registry(self) -> PurgeRegistry:
        if self._registry is None:
            raise SQLiteStorageError("SQLiteStore has not been initialized")
        return self._registry

    @property
    def database_id(self) -> str:
        if self._database_id is None:
            self.initialize()
        assert self._database_id is not None
        return self._database_id

    @property
    def schema_version(self) -> int:
        if self._schema_version is None:
            self.initialize()
        assert self._schema_version is not None
        return self._schema_version

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.profile.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        try:
            configure_connection(connection, self.profile)
        except BaseException:
            connection.close()
            raise
        return connection

    def _inspect_main_database(self) -> tuple[str, str | None]:
        """Classify the file as new, bound, partial-RecallOrigin, or foreign."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return "new", None
        try:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=self.profile.busy_timeout_ms / 1_000,
            )
            with closing(connection):
                rows = connection.execute(
                    """
                    SELECT name
                    FROM sqlite_schema
                    WHERE type IN ('table', 'view')
                      AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
                names = {str(row[0]) for row in rows}
                if not names:
                    return "new", None
                if "storage_meta" in names:
                    row = connection.execute(
                        "SELECT value FROM storage_meta WHERE key = 'database_id'"
                    ).fetchone()
                    if row is not None and row[0]:
                        return "bound", str(row[0])
                    return "partial", None
                if "schema_migrations" in names:
                    return "partial", None
                return "foreign", None
        except sqlite3.DatabaseError as exc:
            raise SQLiteStorageError(f"authoritative database cannot be inspected: {exc}") from exc

    def _registry_exists(self) -> bool:
        return self.purge_registry_path.is_file() and self.purge_registry_path.stat().st_size > 0

    def _load_key_file(self) -> bytes:
        try:
            if self.purge_key_path.is_symlink():
                raise PurgeRegistryError(
                    "purge registry key file must not be a symbolic link",
                    details={"key_path": str(self.purge_key_path)},
                )
            mode = stat.S_IMODE(self.purge_key_path.stat().st_mode)
            if os.name == "posix" and mode & 0o077:
                raise PurgeRegistryError(
                    "purge registry key file permissions are too broad",
                    details={
                        "key_path": str(self.purge_key_path),
                        "mode": oct(mode),
                        "required_mode": "0o600",
                    },
                )
            encoded = self.purge_key_path.read_text(encoding="ascii").strip()
            if not encoded.startswith("base64:"):
                raise PurgeRegistryError(
                    "purge registry key file has an unsupported encoding",
                    details={"key_path": str(self.purge_key_path)},
                )
            return decode_registry_key(encoded)
        except PurgeRegistryError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise PurgeRegistryError(
                "purge registry key file cannot be read",
                details={"key_path": str(self.purge_key_path), "reason": str(exc)},
            ) from exc

    def _generate_key_file(self) -> bytes:
        self.purge_key_path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        encoded = b"base64:" + base64.urlsafe_b64encode(key) + b"\n"
        try:
            file_descriptor = os.open(
                self.purge_key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            self._key_source = "file"
            return self._load_key_file()
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "wb", closefd=True) as key_file:
                key_file.write(encoded)
                key_file.flush()
                os.fsync(key_file.fileno())
        except BaseException:
            with suppress(OSError):
                os.close(file_descriptor)
            raise

        if hasattr(os, "O_DIRECTORY"):
            try:
                directory_fd = os.open(self.purge_key_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not permit directory fsync. The file
                # itself is still fsynced and correctly permissioned.
                pass
        self._key_source = "generated"
        return key

    def _resolve_registry_key(self, main_state: str) -> bytes:
        if self._explicit_registry_key is not None:
            self._key_source = "explicit"
            return decode_registry_key(self._explicit_registry_key)

        environment_key = os.environ.get(PURGE_KEY_ENV)
        if environment_key:
            self._key_source = "environment"
            return decode_registry_key(environment_key)

        if self.purge_key_path.is_file():
            self._key_source = "file"
            return self._load_key_file()

        registry_exists = self._registry_exists()
        if main_state != "new" or registry_exists:
            self._key_source = "missing"
            raise PurgeRegistryError(
                "existing RecallOrigin state requires its purge registry key",
                details={
                    "database_path": str(self.path),
                    "registry_path": str(self.purge_registry_path),
                    "key_path": str(self.purge_key_path),
                },
            )
        return self._generate_key_file()

    def initialize(self) -> SQLiteStore:
        """Create/upgrade the main DB, verify the sidecar, and merge all fences."""

        with self._initialize_lock:
            if self._initialized:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            main_state, existing_database_id = self._inspect_main_database()
            if main_state == "foreign":
                raise SQLiteStorageError(
                    "refusing to initialize over a non-RecallOrigin SQLite database"
                )

            registry_was_present = self._registry_exists()
            if main_state in {"bound", "partial"} and not registry_was_present:
                raise PurgeRegistryError(
                    "existing RecallOrigin database has no purge registry sidecar",
                    details={
                        "database_path": str(self.path),
                        "registry_path": str(self.purge_registry_path),
                    },
                )

            key = self._resolve_registry_key(main_state)
            registry = PurgeRegistry(
                self.purge_registry_path,
                key,
                busy_timeout_ms=self.profile.busy_timeout_ms,
            )

            if registry_was_present and main_state != "new":
                verification = registry.initialize(
                    database_id=existing_database_id,
                    allow_create_state=False,
                )
            else:
                planned_database_id = (
                    None if registry_was_present else existing_database_id or f"db_{uuid4().hex}"
                )
                verification = registry.initialize(database_id=planned_database_id)

            database_id = existing_database_id or verification.database_id
            if database_id != verification.database_id:
                raise PurgeRegistryError(
                    "main database and purge registry identity disagree",
                    details={
                        "database_id": database_id,
                        "registry_database_id": verification.database_id,
                    },
                )

            now = _now_micros()
            with closing(self._connect()) as connection:
                schema_version = run_migrations(
                    connection,
                    package=MAIN_MIGRATIONS_PACKAGE,
                    applied_at=now,
                )
                with sqlite_transaction(connection, "IMMEDIATE"):
                    row = connection.execute(
                        "SELECT value FROM storage_meta WHERE key = 'database_id'"
                    ).fetchone()
                    if row is not None and row["value"] != database_id:
                        raise PurgeRegistryError(
                            "authoritative database identity changed during initialization"
                        )
                    metadata = {
                        "database_id": database_id,
                        "created_at": str(now),
                        "schema_version": str(schema_version),
                        "purge_registry_required": "1",
                    }
                    for meta_key, value in metadata.items():
                        connection.execute(
                            """
                            INSERT INTO storage_meta (key, value, updated_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT (key) DO UPDATE SET
                                value = CASE
                                    WHEN storage_meta.key = 'created_at'
                                    THEN storage_meta.value
                                    ELSE excluded.value
                                END,
                                updated_at = excluded.updated_at
                            """,
                            (meta_key, value, now),
                        )
                registry.apply_fences(connection)

            self._registry = registry
            self._database_id = database_id
            self._schema_version = schema_version
            self._initialized = True
            return self

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection only after current fences are applied."""

        self.initialize()
        with closing(self._connect()) as connection:
            self.purge_registry.apply_fences(connection)
            yield connection

    @contextmanager
    def transaction(
        self,
        mode: MigrationMode = "IMMEDIATE",
    ) -> Iterator[sqlite3.Connection]:
        """Yield a short write transaction, committing or rolling back atomically."""

        if mode not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
            raise ValueError(f"unsupported SQLite transaction mode: {mode!r}")
        with self.connection() as connection, sqlite_transaction(connection, mode):
            yield connection

    def _feature_checks(self, connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        checks: dict[str, dict[str, Any]] = {}

        try:
            connection.execute("CREATE VIRTUAL TABLE temp.__mr_doctor_fts USING fts5(value)")
            connection.execute("INSERT INTO temp.__mr_doctor_fts(value) VALUES ('memory receipt')")
            matched = connection.execute(
                "SELECT count(*) FROM temp.__mr_doctor_fts WHERE value MATCH 'memory'"
            ).fetchone()[0]
            connection.execute("DROP TABLE temp.__mr_doctor_fts")
            checks["fts5"] = {"ok": matched == 1}
        except sqlite3.DatabaseError as exc:
            checks["fts5"] = {"ok": False, "detail": str(exc)}

        try:
            connection.execute(
                "CREATE TEMP TABLE __mr_doctor_strict(value INTEGER NOT NULL) STRICT"
            )
            rejected = False
            try:
                connection.execute(
                    "INSERT INTO __mr_doctor_strict(value) VALUES ('not-an-integer')"
                )
            except sqlite3.DatabaseError:
                rejected = True
            connection.execute("DROP TABLE __mr_doctor_strict")
            checks["strict"] = {"ok": rejected}
        except sqlite3.DatabaseError as exc:
            checks["strict"] = {"ok": False, "detail": str(exc)}

        try:
            connection.execute("CREATE TEMP TABLE __mr_doctor_returning(value INTEGER NOT NULL)")
            returned = connection.execute(
                "INSERT INTO __mr_doctor_returning(value) VALUES (7) RETURNING value"
            ).fetchone()[0]
            connection.execute("DROP TABLE __mr_doctor_returning")
            checks["returning"] = {"ok": returned == 7}
        except sqlite3.DatabaseError as exc:
            checks["returning"] = {"ok": False, "detail": str(exc)}

        try:
            integrity = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            checks["integrity"] = {
                "ok": integrity == ["ok"],
                "detail": integrity[:8],
            }
        except sqlite3.DatabaseError as exc:
            checks["integrity"] = {"ok": False, "detail": str(exc)}

        try:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            checks["foreign_key_check"] = {
                "ok": not violations,
                "violation_count": len(violations),
            }
        except sqlite3.DatabaseError as exc:
            checks["foreign_key_check"] = {"ok": False, "detail": str(exc)}
        return checks

    def doctor(self) -> dict[str, Any]:
        """Return non-secret runtime, schema, integrity, and sidecar diagnostics."""

        initialization_error: str | None = None
        try:
            self.initialize()
        except Exception as exc:  # Diagnostics must report fail-closed causes.
            initialization_error = str(exc)

        checks: dict[str, dict[str, Any]] = {}
        pragmas: dict[str, Any] = {}
        database_id = self._database_id
        schema_version = self._schema_version
        try:
            with closing(self._connect()) as connection:
                checks = self._feature_checks(connection)
                pragmas = {
                    "journal_mode": str(
                        connection.execute("PRAGMA journal_mode").fetchone()[0]
                    ).lower(),
                    "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                    "busy_timeout_ms": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                    "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                    "secure_delete": bool(connection.execute("PRAGMA secure_delete").fetchone()[0]),
                }
                if database_id is None:
                    row = connection.execute(
                        """
                        SELECT value FROM storage_meta
                        WHERE key = 'database_id'
                        """
                    ).fetchone()
                    database_id = None if row is None else str(row["value"])
                if schema_version is None:
                    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        except (OSError, sqlite3.DatabaseError, SQLiteStorageError) as exc:
            checks.setdefault("database_open", {"ok": False, "detail": str(exc)})

        purge_diagnostic: dict[str, Any] = {
            "ok": False,
            "path": str(self.purge_registry_path),
            "key_source": self._key_source,
        }
        if self._registry is not None:
            try:
                verification = self._registry.verify(expected_database_id=database_id)
                purge_diagnostic.update(verification.to_dict())
                purge_diagnostic["ok"] = True
            except PurgeRegistryError as exc:
                purge_diagnostic["detail"] = exc.message
        elif initialization_error is not None:
            purge_diagnostic["detail"] = initialization_error

        required_checks_ok = all(
            bool(check.get("ok"))
            for name, check in checks.items()
            if name in {"fts5", "strict", "returning", "integrity", "foreign_key_check"}
        )
        profile_ok = (
            pragmas.get("journal_mode") == "wal"
            and pragmas.get("foreign_keys") is True
            and pragmas.get("secure_delete") is True
            and pragmas.get("synchronous") == (2 if self.profile.durable else 1)
        )
        ok = (
            initialization_error is None
            and required_checks_ok
            and profile_ok
            and purge_diagnostic["ok"] is True
        )
        return {
            "ok": ok,
            "database": {
                "path": str(self.path),
                "database_id": database_id,
                "schema_version": schema_version,
                "sqlite_version": sqlite3.sqlite_version,
                "initialization_error": initialization_error,
            },
            "profile": {
                **asdict(self.profile),
                "synchronous_name": self.profile.synchronous,
                "pragmas": pragmas,
            },
            "checks": checks,
            "purge_registry": purge_diagnostic,
        }
