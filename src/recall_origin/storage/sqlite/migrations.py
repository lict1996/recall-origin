"""Deterministic resource-backed SQLite migrations."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Literal
from uuid import uuid4

from recall_origin.storage.sqlite.errors import MigrationError

MigrationMode = Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"]

_MIGRATION_FILE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable migration embedded in the wheel."""

    version: int
    name: str
    sql: str
    checksum: str


@contextmanager
def sqlite_transaction(
    connection: sqlite3.Connection,
    mode: MigrationMode = "IMMEDIATE",
) -> Iterator[sqlite3.Connection]:
    """Run an atomic SQLite transaction, nesting safely through a savepoint."""

    if mode not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
        raise ValueError(f"unsupported SQLite transaction mode: {mode!r}")

    if connection.in_transaction:
        savepoint = f"recall_origin_{uuid4().hex}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield connection
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return

    connection.execute(f"BEGIN {mode}")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def load_migrations(package: str) -> tuple[Migration, ...]:
    """Load and validate all ``NNNN_name.sql`` files from a resource package."""

    loaded: list[Migration] = []
    root = resources.files(package)
    for item in sorted(root.iterdir(), key=lambda candidate: candidate.name):
        match = _MIGRATION_FILE.fullmatch(item.name)
        if match is None:
            continue
        sql = item.read_text(encoding="utf-8")
        loaded.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )

    if not loaded:
        raise MigrationError(f"no migrations found in resource package {package!r}")

    versions = [migration.version for migration in loaded]
    expected = list(range(1, len(loaded) + 1))
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 1; found {versions}, expected {expected}"
        )
    return tuple(loaded)


def _iter_statements(sql: str) -> Iterator[str]:
    """Split a migration without breaking trigger bodies or quoted semicolons."""

    pending: list[str] = []
    for line in sql.splitlines(keepends=True):
        pending.append(line)
        candidate = "".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            pending.clear()

    remainder = "".join(pending).strip()
    if remainder:
        raise MigrationError("migration ends with an incomplete SQL statement")


def _ensure_history_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version >= 1),
            name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL CHECK (length(checksum) = 64),
            applied_at INTEGER NOT NULL
        ) STRICT
        """
    )


def run_migrations(
    connection: sqlite3.Connection,
    *,
    package: str,
    applied_at: int,
) -> int:
    """Verify prior checksums and apply all pending migrations atomically.

    Returns the latest embedded schema version.
    """

    migrations = load_migrations(package)
    _ensure_history_table(connection)

    applied_rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    embedded_by_version = {migration.version: migration for migration in migrations}

    for row in applied_rows:
        version = int(row["version"])
        migration = embedded_by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"database schema version {version} is newer than this RecallOrigin build"
            )
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise MigrationError(
                f"migration {version:04d} does not match the embedded immutable checksum"
            )

    applied_versions = {int(row["version"]) for row in applied_rows}
    for migration in migrations:
        if migration.version in applied_versions:
            continue
        try:
            with sqlite_transaction(connection, "IMMEDIATE"):
                for statement in _iter_statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        applied_at,
                    ),
                )
                connection.execute(f"PRAGMA user_version = {migration.version}")
        except (sqlite3.DatabaseError, OSError) as exc:
            raise MigrationError(
                f"failed to apply migration {migration.version:04d}_{migration.name}: {exc}"
            ) from exc

    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    latest = migrations[-1].version
    if user_version > latest:
        raise MigrationError(
            f"database PRAGMA user_version {user_version} is newer than supported {latest}"
        )
    if user_version != latest:
        connection.execute(f"PRAGMA user_version = {latest}")
    return latest
