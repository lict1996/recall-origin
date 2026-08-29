"""Signed, append-only deletion registry kept outside the authoritative database."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from recall_origin.storage.sqlite.errors import PurgeRegistryError
from recall_origin.storage.sqlite.migrations import (
    load_migrations,
    run_migrations,
    sqlite_transaction,
)

PURGE_MIGRATIONS_PACKAGE = "recall_origin.storage.sqlite._purge_migrations"
ZERO_DIGEST = "0" * 64
SUPPORTED_TARGET_TYPES = frozenset({"event", "claim", "subject", "partition", "managed_pack"})


def _now_micros() -> int:
    return time.time_ns() // 1_000


def decode_registry_key(value: bytes | str) -> bytes:
    """Decode an explicit registry key and enforce a 256-bit minimum."""

    if isinstance(value, bytes):
        key = value
    elif value.startswith("hex:"):
        try:
            key = bytes.fromhex(value[4:])
        except ValueError as exc:
            raise PurgeRegistryError("purge registry hex key is invalid") from exc
    elif value.startswith("base64:"):
        try:
            key = base64.b64decode(
                value[7:].encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
            raise PurgeRegistryError("purge registry base64 key is invalid") from exc
    else:
        key = value.encode("utf-8")

    if len(key) < 32:
        raise PurgeRegistryError(
            "purge registry key must contain at least 32 bytes",
            details={"minimum_bytes": 32, "actual_bytes": len(key)},
        )
    return key


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PurgeTombstone:
    """One authenticated, append-only deletion fence."""

    entry_id: str
    database_id: str
    generation: int
    scope_key: str
    target_type: str
    target_id: str
    deletion_id: str
    recorded_at: int
    previous_digest: str
    entry_digest: str
    signature: str

    @property
    def digest(self) -> str:
        """Compatibility alias used by deletion receipts and main-DB fences."""

        return self.entry_digest


@dataclass(frozen=True, slots=True)
class PurgeVerification:
    """Verified sidecar head and identity."""

    database_id: str
    generation: int
    head_digest: str
    key_fingerprint: str
    tombstone_count: int

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


class PurgeRegistry:
    """Independent HMAC-authenticated deletion ledger.

    The sidecar must not be included in ordinary authoritative-database
    snapshots. A deletion is recorded here *before* the main-database fence is
    committed; :meth:`apply_fences` then makes a restored old database respect
    every current tombstone.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        key: bytes | str,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self._key = decode_registry_key(key)
        self.busy_timeout_ms = busy_timeout_ms

    @property
    def key_fingerprint(self) -> str:
        """A non-secret identifier suitable for diagnostics."""

        return hashlib.sha256(self._key).hexdigest()

    def opaque_digest(self, purpose: str, value: str) -> str:
        """Return a domain-separated HMAC for low-entropy diagnostic values."""

        if not purpose or len(purpose) > 128:
            raise ValueError("opaque digest purpose must contain 1-128 characters")
        material = purpose.encode("utf-8") + b"\0" + value.encode("utf-8")
        return hmac.new(
            self._key,
            b"opaque-v1\0" + material,
            hashlib.sha256,
        ).hexdigest()

    def _state_signature(
        self,
        *,
        database_id: str,
        generation: int,
        head_digest: str,
        key_fingerprint: str,
        created_at: int,
        updated_at: int,
    ) -> str:
        payload = _canonical_json(
            {
                "created_at": created_at,
                "database_id": database_id,
                "generation": generation,
                "head_digest": head_digest,
                "key_fingerprint": key_fingerprint,
                "updated_at": updated_at,
                "version": 1,
            }
        )
        return hmac.new(self._key, b"state-v1\0" + payload, hashlib.sha256).hexdigest()

    def _entry_material(
        self,
        *,
        entry_id: str,
        database_id: str,
        generation: int,
        scope_key: str,
        target_type: str,
        target_id: str,
        deletion_id: str,
        recorded_at: int,
        previous_digest: str,
    ) -> bytes:
        return _canonical_json(
            {
                "database_id": database_id,
                "deletion_id": deletion_id,
                "entry_id": entry_id,
                "generation": generation,
                "previous_digest": previous_digest,
                "recorded_at": recorded_at,
                "scope_key": scope_key,
                "target_id": target_id,
                "target_type": target_type,
                "version": 1,
            }
        )

    def _entry_auth(
        self,
        *,
        entry_id: str,
        database_id: str,
        generation: int,
        scope_key: str,
        target_type: str,
        target_id: str,
        deletion_id: str,
        recorded_at: int,
        previous_digest: str,
    ) -> tuple[str, str]:
        material = self._entry_material(
            entry_id=entry_id,
            database_id=database_id,
            generation=generation,
            scope_key=scope_key,
            target_type=target_type,
            target_id=target_id,
            deletion_id=deletion_id,
            recorded_at=recorded_at,
            previous_digest=previous_digest,
        )
        digest = hashlib.sha256(material).hexdigest()
        signature = hmac.new(
            self._key,
            b"entry-v1\0" + material,
            hashlib.sha256,
        ).hexdigest()
        return digest, signature

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            raise PurgeRegistryError(
                "purge registry sidecar is missing",
                details={"registry_path": str(self.path)},
            )
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=ro",
            uri=True,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA query_only = ON")
        return connection

    def initialize(
        self,
        database_id: str | None = None,
        *,
        allow_create_state: bool = True,
    ) -> PurgeVerification:
        """Create/upgrade a registry and validate its database binding.

        ``allow_create_state=False`` is used once a main database already
        exists. It permits signed-schema upgrades but refuses to turn a blank
        or replaced sidecar into a new trusted registry.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = _now_micros()
        try:
            with closing(self._connect()) as connection:
                run_migrations(
                    connection,
                    package=PURGE_MIGRATIONS_PACKAGE,
                    applied_at=now,
                )
                row = connection.execute(
                    "SELECT * FROM registry_state WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    if not allow_create_state:
                        raise PurgeRegistryError(
                            "existing authoritative database requires signed purge state"
                        )
                    bound_database_id = database_id or f"db_{uuid4().hex}"
                    state_signature = self._state_signature(
                        database_id=bound_database_id,
                        generation=0,
                        head_digest=ZERO_DIGEST,
                        key_fingerprint=self.key_fingerprint,
                        created_at=now,
                        updated_at=now,
                    )
                    with sqlite_transaction(connection, "IMMEDIATE"):
                        connection.execute(
                            """
                            INSERT INTO registry_state (
                                singleton, database_id, generation, head_digest,
                                key_fingerprint, created_at, updated_at, state_signature
                            ) VALUES (1, ?, 0, ?, ?, ?, ?, ?)
                            """,
                            (
                                bound_database_id,
                                ZERO_DIGEST,
                                self.key_fingerprint,
                                now,
                                now,
                                state_signature,
                            ),
                        )
                elif database_id is not None and row["database_id"] != database_id:
                    raise PurgeRegistryError(
                        "purge registry belongs to a different authoritative database",
                        details={
                            "expected_database_id": database_id,
                            "registry_database_id": row["database_id"],
                        },
                    )
        except PurgeRegistryError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise PurgeRegistryError(
                "purge registry could not be initialized",
                details={"registry_path": str(self.path), "reason": str(exc)},
            ) from exc
        return self.verify(expected_database_id=database_id)

    def _verify_migration_history(self, connection: sqlite3.Connection) -> None:
        embedded = load_migrations(PURGE_MIGRATIONS_PACKAGE)
        try:
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PurgeRegistryError("purge registry has no valid migration history") from exc

        if len(rows) != len(embedded):
            raise PurgeRegistryError(
                "purge registry schema version is incomplete or unsupported",
                details={
                    "applied_versions": len(rows),
                    "expected_versions": len(embedded),
                },
            )
        for row, migration in zip(rows, embedded, strict=True):
            if (
                row["version"] != migration.version
                or row["name"] != migration.name
                or row["checksum"] != migration.checksum
            ):
                raise PurgeRegistryError(
                    "purge registry migration checksum verification failed",
                    details={"version": row["version"]},
                )

    def _verify_connection(
        self,
        connection: sqlite3.Connection,
        *,
        expected_database_id: str | None = None,
    ) -> PurgeVerification:
        self._verify_migration_history(connection)
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity_messages = [str(row[0]) for row in integrity_rows]
        if integrity_messages != ["ok"]:
            raise PurgeRegistryError(
                "purge registry integrity check failed",
                details={"integrity": integrity_messages[:8]},
            )

        state = connection.execute("SELECT * FROM registry_state WHERE singleton = 1").fetchone()
        if state is None:
            raise PurgeRegistryError("purge registry has no signed state")
        if expected_database_id is not None and state["database_id"] != expected_database_id:
            raise PurgeRegistryError(
                "purge registry database identity does not match",
                details={
                    "expected_database_id": expected_database_id,
                    "registry_database_id": state["database_id"],
                },
            )
        if not hmac.compare_digest(state["key_fingerprint"], self.key_fingerprint):
            raise PurgeRegistryError("purge registry key fingerprint does not match")

        expected_state_signature = self._state_signature(
            database_id=state["database_id"],
            generation=state["generation"],
            head_digest=state["head_digest"],
            key_fingerprint=state["key_fingerprint"],
            created_at=state["created_at"],
            updated_at=state["updated_at"],
        )
        if not hmac.compare_digest(state["state_signature"], expected_state_signature):
            raise PurgeRegistryError("purge registry state signature is invalid")

        rows = connection.execute("SELECT * FROM purge_tombstones ORDER BY generation").fetchall()
        expected_generation = 1
        previous_digest = ZERO_DIGEST
        for row in rows:
            if row["database_id"] != state["database_id"]:
                raise PurgeRegistryError("purge tombstone has a foreign database identity")
            if row["generation"] != expected_generation:
                raise PurgeRegistryError(
                    "purge registry generation sequence has a gap",
                    details={
                        "expected_generation": expected_generation,
                        "actual_generation": row["generation"],
                    },
                )
            if row["previous_digest"] != previous_digest:
                raise PurgeRegistryError(
                    "purge registry hash chain is broken",
                    details={"generation": row["generation"]},
                )
            digest, signature = self._entry_auth(
                entry_id=row["entry_id"],
                database_id=row["database_id"],
                generation=row["generation"],
                scope_key=row["scope_key"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                deletion_id=row["deletion_id"],
                recorded_at=row["recorded_at"],
                previous_digest=row["previous_digest"],
            )
            if not hmac.compare_digest(row["entry_digest"], digest):
                raise PurgeRegistryError(
                    "purge tombstone digest is invalid",
                    details={"generation": row["generation"]},
                )
            if not hmac.compare_digest(row["signature"], signature):
                raise PurgeRegistryError(
                    "purge tombstone signature is invalid",
                    details={"generation": row["generation"]},
                )
            previous_digest = digest
            expected_generation += 1

        if state["generation"] != len(rows):
            raise PurgeRegistryError(
                "purge registry signed generation does not match its entries",
                details={
                    "state_generation": state["generation"],
                    "entry_count": len(rows),
                },
            )
        if not hmac.compare_digest(state["head_digest"], previous_digest):
            raise PurgeRegistryError("purge registry signed head does not match its chain")

        return PurgeVerification(
            database_id=state["database_id"],
            generation=state["generation"],
            head_digest=state["head_digest"],
            key_fingerprint=state["key_fingerprint"],
            tombstone_count=len(rows),
        )

    def verify(self, expected_database_id: str | None = None) -> PurgeVerification:
        """Verify schema checksums, SQLite integrity, HMACs, and the hash chain."""

        try:
            with closing(self._connect_read_only()) as connection:
                return self._verify_connection(
                    connection,
                    expected_database_id=expected_database_id,
                )
        except PurgeRegistryError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise PurgeRegistryError(
                "purge registry verification failed",
                details={"registry_path": str(self.path), "reason": str(exc)},
            ) from exc

    def record(
        self,
        target_type: str,
        target_id: str,
        deletion_id: str,
        *,
        scope_key: str = "*",
        expected_generation: int | None = None,
        recorded_at: int | None = None,
    ) -> PurgeTombstone:
        """Durably append one deletion before writing the main-database fence."""

        target_type = str(target_type)
        if target_type not in SUPPORTED_TARGET_TYPES:
            raise ValueError(f"unsupported purge target type: {target_type!r}")
        if not target_id or len(target_id) > 512:
            raise ValueError("target_id must contain between 1 and 512 characters")
        if not deletion_id or len(deletion_id) > 256:
            raise ValueError("deletion_id must contain between 1 and 256 characters")
        if not scope_key or len(scope_key) > 512:
            raise ValueError("scope_key must contain between 1 and 512 characters")
        if expected_generation is not None and expected_generation < 0:
            raise ValueError("expected_generation cannot be negative")

        self.initialize()
        timestamp = recorded_at if recorded_at is not None else _now_micros()
        try:
            with (
                closing(self._connect()) as connection,
                sqlite_transaction(connection, "IMMEDIATE"),
            ):
                verified = self._verify_connection(connection)
                existing = connection.execute(
                    """
                    SELECT *
                    FROM purge_tombstones
                    WHERE database_id = ?
                      AND scope_key = ?
                      AND target_type = ?
                      AND target_id = ?
                      AND deletion_id = ?
                    """,
                    (
                        verified.database_id,
                        scope_key,
                        target_type,
                        target_id,
                        deletion_id,
                    ),
                ).fetchone()
                if existing is not None:
                    return PurgeTombstone(**dict(existing))

                if expected_generation is not None and verified.generation != expected_generation:
                    raise PurgeRegistryError(
                        "purge registry generation compare-and-swap failed",
                        details={
                            "expected_generation": expected_generation,
                            "actual_generation": verified.generation,
                        },
                    )

                generation = verified.generation + 1
                entry_id = f"purge_{uuid4().hex}"
                digest, signature = self._entry_auth(
                    entry_id=entry_id,
                    database_id=verified.database_id,
                    generation=generation,
                    scope_key=scope_key,
                    target_type=target_type,
                    target_id=target_id,
                    deletion_id=deletion_id,
                    recorded_at=timestamp,
                    previous_digest=verified.head_digest,
                )
                connection.execute(
                    """
                    INSERT INTO purge_tombstones (
                        entry_id, database_id, generation, scope_key, target_type,
                        target_id, deletion_id, recorded_at, previous_digest,
                        entry_digest, signature
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        verified.database_id,
                        generation,
                        scope_key,
                        target_type,
                        target_id,
                        deletion_id,
                        timestamp,
                        verified.head_digest,
                        digest,
                        signature,
                    ),
                )
                state = connection.execute(
                    "SELECT created_at FROM registry_state WHERE singleton = 1"
                ).fetchone()
                if state is None:
                    raise PurgeRegistryError("purge registry signed state disappeared")
                state_signature = self._state_signature(
                    database_id=verified.database_id,
                    generation=generation,
                    head_digest=digest,
                    key_fingerprint=self.key_fingerprint,
                    created_at=state["created_at"],
                    updated_at=timestamp,
                )
                connection.execute(
                    """
                    UPDATE registry_state
                    SET generation = ?, head_digest = ?, updated_at = ?, state_signature = ?
                    WHERE singleton = 1 AND generation = ?
                    """,
                    (
                        generation,
                        digest,
                        timestamp,
                        state_signature,
                        verified.generation,
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PurgeRegistryError(
                        "purge registry head changed during append",
                        details={"expected_generation": verified.generation},
                    )
                tombstone = PurgeTombstone(
                    entry_id=entry_id,
                    database_id=verified.database_id,
                    generation=generation,
                    scope_key=scope_key,
                    target_type=target_type,
                    target_id=target_id,
                    deletion_id=deletion_id,
                    recorded_at=timestamp,
                    previous_digest=verified.head_digest,
                    entry_digest=digest,
                    signature=signature,
                )
        except PurgeRegistryError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise PurgeRegistryError(
                "purge tombstone append failed",
                details={"registry_path": str(self.path), "reason": str(exc)},
            ) from exc

        self.verify(expected_database_id=tombstone.database_id)
        return tombstone

    def _tombstones(self) -> tuple[PurgeTombstone, ...]:
        with closing(self._connect_read_only()) as connection:
            rows = connection.execute(
                "SELECT * FROM purge_tombstones ORDER BY generation"
            ).fetchall()
        return tuple(PurgeTombstone(**dict(row)) for row in rows)

    def _recover_deletion_lifecycle(
        self,
        connection: sqlite3.Connection,
        tombstone: PurgeTombstone,
        *,
        recovered_at: int,
    ) -> None:
        """Materialize a purge entry point after a sidecar-first crash.

        The registry intentionally commits before the main database. If the
        process dies between those commits, the authenticated tombstone is the
        durable deletion intent. Reconstruct only the minimum lifecycle needed
        for status and purge, without overwriting a request that committed
        successfully before the crash.
        """

        existing = connection.execute(
            """
            SELECT scope_key, target_type, target_id
            FROM deletion_requests
            WHERE deletion_id = ?
            """,
            (tombstone.deletion_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["scope_key"]) != tombstone.scope_key
                or str(existing["target_type"]) != tombstone.target_type
                or str(existing["target_id"]) != tombstone.target_id
            ):
                raise PurgeRegistryError(
                    "authoritative deletion lifecycle disagrees with registry",
                    details={"deletion_id": tombstone.deletion_id},
                )
            return

        if tombstone.scope_key == "*":
            return
        partition = connection.execute(
            "SELECT 1 FROM partitions WHERE partition_id = ?",
            (tombstone.scope_key,),
        ).fetchone()
        if partition is None:
            # A restore can predate creation of the referenced partition. The
            # fence remains authoritative; there is no local target to purge.
            return

        hidden_at = max(recovered_at, tombstone.recorded_at)
        request_hash = hashlib.sha256(
            _canonical_json(
                {
                    "deletion_id": tombstone.deletion_id,
                    "entry_id": tombstone.entry_id,
                    "recovered_from": "purge_registry",
                    "scope_key": tombstone.scope_key,
                    "target_id": tombstone.target_id,
                    "target_type": tombstone.target_type,
                }
            )
        ).hexdigest()
        cursor = connection.execute(
            """
            INSERT INTO ledger_transactions(tx_id, operation, committed_at)
            VALUES (?, 'recover:purge-registry', ?)
            """,
            (f"tx_{uuid4().hex}", hidden_at),
        )
        if cursor.lastrowid is None:
            raise PurgeRegistryError("recovered deletion has no ledger sequence")
        connection.execute(
            """
            INSERT INTO deletion_requests(
                deletion_id, partition_id, target_type, target_id,
                idempotency_key, request_hash, cascade_policy, state,
                requested_at, logically_hidden_at, completed_at, tx_seq,
                external_copies_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'safe', 'logically_hidden', ?, ?, NULL, ?, ?)
            """,
            (
                tombstone.deletion_id,
                tombstone.scope_key,
                tombstone.target_type,
                tombstone.target_id,
                f"recovered:{tombstone.entry_id}",
                request_hash,
                tombstone.recorded_at,
                hidden_at,
                int(cursor.lastrowid),
                json.dumps(
                    [
                        "User exports, OS or cloud snapshots, offline backups, "
                        "and provider retention are outside engine control."
                    ]
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO deletion_layers(
                deletion_id, layer, state, attempt, last_verified_at,
                error_code, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                (
                    tombstone.deletion_id,
                    "logical_visibility",
                    "completed",
                    1,
                    hidden_at,
                    hidden_at,
                ),
                (
                    tombstone.deletion_id,
                    "authoritative_text",
                    "accepted",
                    0,
                    None,
                    hidden_at,
                ),
                (tombstone.deletion_id, "fts", "accepted", 0, None, hidden_at),
                (
                    tombstone.deletion_id,
                    "managed_packs",
                    "accepted",
                    0,
                    None,
                    hidden_at,
                ),
            ),
        )

    def apply_fences(self, connection: sqlite3.Connection) -> int:
        """Merge the current registry into a main DB, including an old restore.

        Returns the number of distinct target fences represented by the
        registry. The method fails closed on identity, signature, generation,
        or checkpoint disagreement.
        """

        verification = self.verify()
        try:
            database_row = connection.execute(
                "SELECT value FROM storage_meta WHERE key = 'database_id'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise PurgeRegistryError("authoritative database has no RecallOrigin identity") from exc
        if database_row is None:
            raise PurgeRegistryError("authoritative database identity is missing")
        database_id = str(database_row[0])
        if database_id != verification.database_id:
            raise PurgeRegistryError(
                "purge registry does not belong to this authoritative database",
                details={
                    "database_id": database_id,
                    "registry_database_id": verification.database_id,
                },
            )

        tombstones = self._tombstones()
        latest_by_target: dict[tuple[str, str, str], PurgeTombstone] = {}
        for tombstone in tombstones:
            latest_by_target[(tombstone.scope_key, tombstone.target_type, tombstone.target_id)] = (
                tombstone
            )

        now = _now_micros()
        try:
            with sqlite_transaction(connection, "IMMEDIATE"):
                checkpoint = connection.execute(
                    "SELECT * FROM purge_registry_checkpoint WHERE singleton = 1"
                ).fetchone()
                if checkpoint is not None:
                    if checkpoint["database_id"] != database_id:
                        raise PurgeRegistryError(
                            "authoritative database has a foreign purge checkpoint"
                        )
                    if checkpoint["applied_generation"] > verification.generation:
                        raise PurgeRegistryError(
                            "purge registry is older than the authoritative checkpoint",
                            details={
                                "checkpoint_generation": checkpoint["applied_generation"],
                                "registry_generation": verification.generation,
                            },
                        )
                    if checkpoint[
                        "applied_generation"
                    ] == verification.generation and not hmac.compare_digest(
                        checkpoint["head_digest"],
                        verification.head_digest,
                    ):
                        raise PurgeRegistryError(
                            "purge checkpoint head disagrees with the registry"
                        )

                for tombstone in tombstones:
                    self._recover_deletion_lifecycle(
                        connection,
                        tombstone,
                        recovered_at=now,
                    )

                for tombstone in latest_by_target.values():
                    connection.execute(
                        """
                        INSERT INTO tombstone_fences (
                            scope_key, target_type, target_id, generation,
                            deletion_id, registry_entry_id, registry_digest,
                            recorded_at, applied_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (scope_key, target_type, target_id) DO UPDATE SET
                            generation = excluded.generation,
                            deletion_id = excluded.deletion_id,
                            registry_entry_id = excluded.registry_entry_id,
                            registry_digest = excluded.registry_digest,
                            recorded_at = excluded.recorded_at,
                            applied_at = excluded.applied_at
                        WHERE excluded.generation > tombstone_fences.generation
                        """,
                        (
                            tombstone.scope_key,
                            tombstone.target_type,
                            tombstone.target_id,
                            tombstone.generation,
                            tombstone.deletion_id,
                            tombstone.entry_id,
                            tombstone.entry_digest,
                            tombstone.recorded_at,
                            now,
                        ),
                    )

                for key, tombstone in latest_by_target.items():
                    local = connection.execute(
                        """
                        SELECT generation, registry_digest
                        FROM tombstone_fences
                        WHERE scope_key = ? AND target_type = ? AND target_id = ?
                        """,
                        key,
                    ).fetchone()
                    if (
                        local is None
                        or local["generation"] != tombstone.generation
                        or not hmac.compare_digest(local["registry_digest"], tombstone.entry_digest)
                    ):
                        raise PurgeRegistryError(
                            "authoritative tombstone fence disagrees with registry",
                            details={
                                "scope_key": tombstone.scope_key,
                                "target_type": tombstone.target_type,
                                "target_id": tombstone.target_id,
                            },
                        )

                connection.execute(
                    """
                    INSERT INTO purge_registry_checkpoint (
                        singleton, database_id, applied_generation, head_digest, verified_at
                    ) VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT (singleton) DO UPDATE SET
                        database_id = excluded.database_id,
                        applied_generation = excluded.applied_generation,
                        head_digest = excluded.head_digest,
                        verified_at = excluded.verified_at
                    """,
                    (
                        database_id,
                        verification.generation,
                        verification.head_digest,
                        now,
                    ),
                )
        except PurgeRegistryError:
            raise
        except sqlite3.DatabaseError as exc:
            raise PurgeRegistryError(
                "purge registry could not be merged into the authoritative database",
                details={"reason": str(exc)},
            ) from exc
        return len(latest_by_target)
