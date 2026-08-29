from __future__ import annotations

import base64
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from recall_origin.storage.sqlite.connection import SQLiteStore
from recall_origin.storage.sqlite.errors import PurgeRegistryError
from recall_origin.storage.sqlite.purge_registry import (
    ZERO_DIGEST,
    PurgeRegistry,
    decode_registry_key,
)

REGISTRY_KEY = b"k" * 32
DATABASE_ID = "db_purge_boundary"


def _initialized_registry(path: Path, *, database_id: str = DATABASE_ID) -> PurgeRegistry:
    registry = PurgeRegistry(path, REGISTRY_KEY)
    verification = registry.initialize(database_id=database_id)
    assert verification.database_id == database_id
    return registry


def _initialized_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(
        tmp_path / "main.sqlite3",
        purge_registry_path=tmp_path / "purge.sqlite3",
        purge_registry_key=REGISTRY_KEY,
    )
    store.initialize()
    return store


@contextmanager
def _main_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        (b"b" * 32, b"b" * 32),
        ("r" * 32, b"r" * 32),
        ("é" * 16, "é".encode() * 16),
        ("hex:" + (b"h" * 32).hex(), b"h" * 32),
        (
            "base64:" + base64.urlsafe_b64encode(b"u" * 32).decode("ascii"),
            b"u" * 32,
        ),
    ],
)
def test_decode_registry_key_accepts_each_supported_exact_32_byte_form(
    encoded: bytes | str,
    expected: bytes,
) -> None:
    assert decode_registry_key(encoded) == expected


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        ("hex:not-hex", "purge registry hex key is invalid"),
        ("base64:%%%", "purge registry base64 key is invalid"),
        ("base64:é", "purge registry base64 key is invalid"),
    ],
)
def test_decode_registry_key_rejects_malformed_explicit_encodings(
    encoded: str,
    message: str,
) -> None:
    with pytest.raises(PurgeRegistryError) as raised:
        decode_registry_key(encoded)

    assert raised.value.message == message


@pytest.mark.parametrize("encoded", [b"s" * 31, "s" * 31])
def test_decode_registry_key_rejects_sub_256_bit_material(encoded: bytes | str) -> None:
    with pytest.raises(PurgeRegistryError) as raised:
        decode_registry_key(encoded)

    assert raised.value.message == "purge registry key must contain at least 32 bytes"
    assert raised.value.details == {"minimum_bytes": 32, "actual_bytes": 31}


def test_opaque_digest_is_deterministic_and_domain_separated(tmp_path: Path) -> None:
    registry = PurgeRegistry(tmp_path / "purge.sqlite3", REGISTRY_KEY)

    first = registry.opaque_digest("retrieval-query", "low-entropy-value")
    replay = registry.opaque_digest("retrieval-query", "low-entropy-value")
    another_purpose = registry.opaque_digest("audit-field", "low-entropy-value")
    maximum_purpose = registry.opaque_digest("p" * 128, "low-entropy-value")

    assert first == replay
    assert first != another_purpose
    assert len(first) == 64
    assert len(maximum_purpose) == 64


@pytest.mark.parametrize("purpose", ["", "p" * 129])
def test_opaque_digest_rejects_empty_or_overlong_purpose(
    tmp_path: Path,
    purpose: str,
) -> None:
    registry = PurgeRegistry(tmp_path / "purge.sqlite3", REGISTRY_KEY)

    with pytest.raises(
        ValueError,
        match="opaque digest purpose must contain 1-128 characters",
    ):
        registry.opaque_digest(purpose, "value")


@pytest.mark.parametrize("create_empty_file", [False, True])
def test_verify_rejects_absent_and_empty_sidecars(
    tmp_path: Path,
    *,
    create_empty_file: bool,
) -> None:
    path = tmp_path / "purge.sqlite3"
    if create_empty_file:
        path.touch()
    registry = PurgeRegistry(path, REGISTRY_KEY)

    with pytest.raises(PurgeRegistryError) as raised:
        registry.verify()

    assert raised.value.message == "purge registry sidecar is missing"
    assert raised.value.details == {"registry_path": str(path)}


def test_initialize_refuses_to_trust_unsigned_schema_for_existing_database(
    tmp_path: Path,
) -> None:
    registry = PurgeRegistry(tmp_path / "purge.sqlite3", REGISTRY_KEY)

    with pytest.raises(PurgeRegistryError) as raised:
        registry.initialize(database_id=DATABASE_ID, allow_create_state=False)

    assert raised.value.message == "existing authoritative database requires signed purge state"


def test_initialize_rejects_registry_bound_to_another_database(tmp_path: Path) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")

    with pytest.raises(PurgeRegistryError) as raised:
        registry.initialize(database_id="db_foreign")

    assert raised.value.message == ("purge registry belongs to a different authoritative database")
    assert raised.value.details == {
        "expected_database_id": "db_foreign",
        "registry_database_id": DATABASE_ID,
    }


def test_initialize_wraps_non_sqlite_sidecar_as_untrusted(tmp_path: Path) -> None:
    path = tmp_path / "purge.sqlite3"
    path.write_bytes(b"this is not a SQLite database")
    registry = PurgeRegistry(path, REGISTRY_KEY)

    with pytest.raises(PurgeRegistryError) as raised:
        registry.initialize(database_id=DATABASE_ID)

    assert raised.value.message == "purge registry could not be initialized"
    assert raised.value.details["registry_path"] == str(path)
    assert raised.value.details["reason"]


@pytest.mark.parametrize(
    ("tamper_sql", "message"),
    [
        (
            "DROP TABLE schema_migrations",
            "purge registry has no valid migration history",
        ),
        (
            "DELETE FROM schema_migrations",
            "purge registry schema version is incomplete or unsupported",
        ),
        (
            f"UPDATE schema_migrations SET checksum = '{'f' * 64}'",
            "purge registry migration checksum verification failed",
        ),
    ],
)
def test_verify_rejects_tampered_migration_history(
    tmp_path: Path,
    tamper_sql: str,
    message: str,
) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    with sqlite3.connect(registry.path, isolation_level=None) as connection:
        connection.execute(tamper_sql)

    with pytest.raises(PurgeRegistryError) as raised:
        registry.verify()

    assert raised.value.message == message


def test_verify_rejects_registry_without_signed_state(tmp_path: Path) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    with sqlite3.connect(registry.path, isolation_level=None) as connection:
        connection.execute("DELETE FROM registry_state")

    with pytest.raises(PurgeRegistryError) as raised:
        registry.verify()

    assert raised.value.message == "purge registry has no signed state"


def test_verify_rejects_expected_database_identity_mismatch(tmp_path: Path) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")

    with pytest.raises(PurgeRegistryError) as raised:
        registry.verify(expected_database_id="db_foreign")

    assert raised.value.message == "purge registry database identity does not match"
    assert raised.value.details == {
        "expected_database_id": "db_foreign",
        "registry_database_id": DATABASE_ID,
    }


def test_verify_rejects_another_registry_key(tmp_path: Path) -> None:
    path = tmp_path / "purge.sqlite3"
    _initialized_registry(path)
    wrong_key_registry = PurgeRegistry(path, b"w" * 32)

    with pytest.raises(PurgeRegistryError) as raised:
        wrong_key_registry.verify()

    assert raised.value.message == "purge registry key fingerprint does not match"


def test_verify_rejects_tampered_signed_state(tmp_path: Path) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    with sqlite3.connect(registry.path, isolation_level=None) as connection:
        connection.execute("UPDATE registry_state SET updated_at = updated_at + 1")

    with pytest.raises(PurgeRegistryError) as raised:
        registry.verify()

    assert raised.value.message == "purge registry state signature is invalid"


@pytest.mark.parametrize(
    ("set_clause", "message"),
    [
        (
            "database_id = 'db_foreign'",
            "purge tombstone has a foreign database identity",
        ),
        (
            "generation = 2",
            "purge registry generation sequence has a gap",
        ),
        (
            f"previous_digest = '{'f' * 64}'",
            "purge registry hash chain is broken",
        ),
        (
            f"entry_digest = '{'f' * 64}'",
            "purge tombstone digest is invalid",
        ),
        (
            f"signature = '{'f' * 64}'",
            "purge tombstone signature is invalid",
        ),
    ],
)
def test_verify_rejects_tombstone_chain_tampering(
    tmp_path: Path,
    set_clause: str,
    message: str,
) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    registry.record(
        "claim",
        "claim-1",
        "deletion-1",
        scope_key="workspace:alpha",
        recorded_at=1_000,
    )
    with sqlite3.connect(registry.path, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER purge_tombstones_no_update")
        connection.execute(f"UPDATE purge_tombstones SET {set_clause}")

    with pytest.raises(PurgeRegistryError) as raised:
        registry.verify()

    assert raised.value.message == message


def test_verify_rejects_deleted_tombstone_even_when_signed_head_remains(
    tmp_path: Path,
) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    registry.record("claim", "claim-1", "deletion-1", recorded_at=1_000)
    with sqlite3.connect(registry.path, isolation_level=None) as connection:
        connection.execute("DROP TRIGGER purge_tombstones_no_delete")
        connection.execute("DELETE FROM purge_tombstones")

    with pytest.raises(PurgeRegistryError) as raised:
        registry.verify()

    assert raised.value.message == ("purge registry signed generation does not match its entries")
    assert raised.value.details == {"state_generation": 1, "entry_count": 0}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"target_type": "unknown", "target_id": "id", "deletion_id": "delete"},
            "unsupported purge target type: 'unknown'",
        ),
        (
            {"target_type": "claim", "target_id": "", "deletion_id": "delete"},
            "target_id must contain between 1 and 512 characters",
        ),
        (
            {"target_type": "claim", "target_id": "x" * 513, "deletion_id": "delete"},
            "target_id must contain between 1 and 512 characters",
        ),
        (
            {"target_type": "claim", "target_id": "id", "deletion_id": ""},
            "deletion_id must contain between 1 and 256 characters",
        ),
        (
            {"target_type": "claim", "target_id": "id", "deletion_id": "x" * 257},
            "deletion_id must contain between 1 and 256 characters",
        ),
        (
            {
                "target_type": "claim",
                "target_id": "id",
                "deletion_id": "delete",
                "scope_key": "",
            },
            "scope_key must contain between 1 and 512 characters",
        ),
        (
            {
                "target_type": "claim",
                "target_id": "id",
                "deletion_id": "delete",
                "scope_key": "x" * 513,
            },
            "scope_key must contain between 1 and 512 characters",
        ),
        (
            {
                "target_type": "claim",
                "target_id": "id",
                "deletion_id": "delete",
                "expected_generation": -1,
            },
            "expected_generation cannot be negative",
        ),
    ],
)
def test_record_rejects_invalid_boundaries_before_creating_sidecar(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    registry = PurgeRegistry(tmp_path / "purge.sqlite3", REGISTRY_KEY)

    with pytest.raises(ValueError) as raised:
        registry.record(**arguments)  # type: ignore[arg-type]

    assert str(raised.value) == message
    assert not registry.path.exists()


def test_record_accepts_documented_maximum_identifier_lengths(tmp_path: Path) -> None:
    registry = PurgeRegistry(tmp_path / "purge.sqlite3", REGISTRY_KEY)

    tombstone = registry.record(
        "managed_pack",
        "t" * 512,
        "d" * 256,
        scope_key="s" * 512,
        expected_generation=0,
        recorded_at=1_000,
    )

    assert tombstone.target_id == "t" * 512
    assert tombstone.deletion_id == "d" * 256
    assert tombstone.scope_key == "s" * 512
    assert tombstone.generation == 1
    assert registry.verify().tombstone_count == 1


def test_record_replay_returns_original_before_generation_compare_and_swap(
    tmp_path: Path,
) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    original = registry.record(
        "claim",
        "claim-1",
        "deletion-1",
        scope_key="workspace:alpha",
        expected_generation=0,
        recorded_at=1_000,
    )

    replay = registry.record(
        "claim",
        "claim-1",
        "deletion-1",
        scope_key="workspace:alpha",
        expected_generation=999,
        recorded_at=2_000,
    )

    assert replay == original
    assert replay.recorded_at == 1_000
    assert registry.verify().to_dict() == {
        "database_id": DATABASE_ID,
        "generation": 1,
        "head_digest": original.entry_digest,
        "key_fingerprint": registry.key_fingerprint,
        "tombstone_count": 1,
    }


def test_record_generation_compare_and_swap_failure_does_not_append(
    tmp_path: Path,
) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    original = registry.record(
        "claim",
        "claim-1",
        "deletion-1",
        expected_generation=0,
        recorded_at=1_000,
    )

    with pytest.raises(PurgeRegistryError) as raised:
        registry.record(
            "claim",
            "claim-2",
            "deletion-2",
            expected_generation=0,
            recorded_at=2_000,
        )

    assert raised.value.message == "purge registry generation compare-and-swap failed"
    assert raised.value.details == {"expected_generation": 0, "actual_generation": 1}
    verification = registry.verify()
    assert verification.generation == 1
    assert verification.head_digest == original.entry_digest
    assert verification.tombstone_count == 1


@pytest.mark.parametrize(
    ("setup_sql", "message"),
    [
        (None, "authoritative database has no RecallOrigin identity"),
        (
            "CREATE TABLE storage_meta (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)",
            "authoritative database identity is missing",
        ),
        (
            """
            CREATE TABLE storage_meta (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);
            INSERT INTO storage_meta VALUES ('database_id', 'db_foreign', 0);
            """,
            "purge registry does not belong to this authoritative database",
        ),
    ],
)
def test_apply_fences_requires_matching_authoritative_database_identity(
    tmp_path: Path,
    setup_sql: str | None,
    message: str,
) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")
    with _main_connection(tmp_path / "main.sqlite3") as connection:
        if setup_sql is not None:
            connection.executescript(setup_sql)

        with pytest.raises(PurgeRegistryError) as raised:
            registry.apply_fences(connection)

    assert raised.value.message == message


@pytest.mark.parametrize(
    ("update_sql", "message"),
    [
        (
            "UPDATE purge_registry_checkpoint SET database_id = 'db_foreign'",
            "authoritative database has a foreign purge checkpoint",
        ),
        (
            "UPDATE purge_registry_checkpoint SET applied_generation = 1",
            "purge registry is older than the authoritative checkpoint",
        ),
        (
            f"UPDATE purge_registry_checkpoint SET head_digest = '{'f' * 64}'",
            "purge checkpoint head disagrees with the registry",
        ),
    ],
)
def test_apply_fences_rejects_checkpoint_identity_generation_and_head_disagreement(
    tmp_path: Path,
    update_sql: str,
    message: str,
) -> None:
    store = _initialized_store(tmp_path)
    with _main_connection(store.path) as connection:
        connection.execute(update_sql)

        with pytest.raises(PurgeRegistryError) as raised:
            store.purge_registry.apply_fences(connection)

        checkpoint = connection.execute(
            "SELECT applied_generation FROM purge_registry_checkpoint"
        ).fetchone()

    assert raised.value.message == message
    assert checkpoint is not None


def test_apply_fences_uses_latest_generation_per_distinct_target(
    tmp_path: Path,
) -> None:
    store = _initialized_store(tmp_path)
    first = store.purge_registry.record(
        "claim",
        "claim-1",
        "deletion-1",
        recorded_at=1_000,
    )
    latest = store.purge_registry.record(
        "claim",
        "claim-1",
        "deletion-2",
        recorded_at=2_000,
    )
    another = store.purge_registry.record(
        "event",
        "event-1",
        "deletion-3",
        recorded_at=3_000,
    )

    with _main_connection(store.path) as connection:
        represented = store.purge_registry.apply_fences(connection)
        rows = connection.execute(
            """
            SELECT target_type, target_id, generation, deletion_id, registry_digest
            FROM tombstone_fences
            ORDER BY target_type, target_id
            """
        ).fetchall()

    assert represented == 2
    assert [tuple(row) for row in rows] == [
        ("claim", "claim-1", latest.generation, "deletion-2", latest.entry_digest),
        ("event", "event-1", another.generation, "deletion-3", another.entry_digest),
    ]
    assert first.generation < latest.generation


def test_apply_fences_keeps_fence_when_restored_database_lacks_partition(
    tmp_path: Path,
) -> None:
    store = _initialized_store(tmp_path)
    tombstone = store.purge_registry.record(
        "claim",
        "claim-from-newer-snapshot",
        "deletion-from-newer-snapshot",
        scope_key="partition-not-in-old-snapshot",
        recorded_at=1_000,
    )

    with _main_connection(store.path) as connection:
        represented = store.purge_registry.apply_fences(connection)
        request_count = connection.execute("SELECT count(*) FROM deletion_requests").fetchone()[0]
        fence = connection.execute(
            """
            SELECT scope_key, target_id, generation, registry_digest
            FROM tombstone_fences
            """
        ).fetchone()

    assert represented == 1
    assert request_count == 0
    assert fence is not None
    assert tuple(fence) == (
        "partition-not-in-old-snapshot",
        "claim-from-newer-snapshot",
        tombstone.generation,
        tombstone.entry_digest,
    )


def test_apply_fences_rejects_existing_deletion_lifecycle_for_another_target(
    tmp_path: Path,
) -> None:
    store = _initialized_store(tmp_path)
    store.purge_registry.record(
        "claim",
        "registry-claim",
        "shared-deletion",
        recorded_at=1_000,
    )
    with _main_connection(store.path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO ledger_transactions(tx_id, operation, committed_at)
            VALUES ('tx-existing-deletion', 'forget', 1)
            """
        )
        assert cursor.lastrowid is not None
        connection.execute(
            """
            INSERT INTO deletion_requests(
                deletion_id, partition_id, target_type, target_id,
                idempotency_key, request_hash, cascade_policy, state,
                requested_at, logically_hidden_at, completed_at, tx_seq,
                external_copies_json
            ) VALUES (
                'shared-deletion', NULL, 'claim', 'authoritative-claim',
                'existing-deletion', ?, 'safe', 'accepted',
                1, NULL, NULL, ?, '[]'
            )
            """,
            ("0" * 64, int(cursor.lastrowid)),
        )

        with pytest.raises(PurgeRegistryError) as raised:
            store.purge_registry.apply_fences(connection)

    assert raised.value.message == ("authoritative deletion lifecycle disagrees with registry")
    assert raised.value.details == {"deletion_id": "shared-deletion"}


def test_apply_fences_rejects_newer_conflicting_local_fence(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    tombstone = store.purge_registry.record(
        "claim",
        "claim-1",
        "deletion-1",
        recorded_at=1_000,
    )
    with _main_connection(store.path) as connection:
        connection.execute(
            """
            INSERT INTO tombstone_fences(
                scope_key, target_type, target_id, generation,
                deletion_id, registry_entry_id, registry_digest,
                recorded_at, applied_at
            ) VALUES ('*', 'claim', 'claim-1', 99, 'local-deletion',
                      'local-entry', ?, 1, 1)
            """,
            ("f" * 64,),
        )

        with pytest.raises(PurgeRegistryError) as raised:
            store.purge_registry.apply_fences(connection)

        local = connection.execute(
            """
            SELECT generation, deletion_id, registry_digest
            FROM tombstone_fences
            WHERE scope_key = '*' AND target_type = 'claim' AND target_id = 'claim-1'
            """
        ).fetchone()

    assert raised.value.message == ("authoritative tombstone fence disagrees with registry")
    assert raised.value.details == {
        "scope_key": "*",
        "target_type": "claim",
        "target_id": "claim-1",
    }
    assert local is not None
    assert tuple(local) == (99, "local-deletion", "f" * 64)
    assert tombstone.generation == 1


def test_apply_fences_wraps_authoritative_schema_failure(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path)
    store.purge_registry.record(
        "claim",
        "claim-1",
        "deletion-1",
        recorded_at=1_000,
    )
    with _main_connection(store.path) as connection:
        connection.execute("DROP TABLE tombstone_fences")

        with pytest.raises(PurgeRegistryError) as raised:
            store.purge_registry.apply_fences(connection)

    assert raised.value.message == (
        "purge registry could not be merged into the authoritative database"
    )
    assert "tombstone_fences" in raised.value.details["reason"]


def test_new_registry_verification_starts_at_zero_digest(tmp_path: Path) -> None:
    registry = _initialized_registry(tmp_path / "purge.sqlite3")

    assert registry.verify().to_dict() == {
        "database_id": DATABASE_ID,
        "generation": 0,
        "head_digest": ZERO_DIGEST,
        "key_fingerprint": registry.key_fingerprint,
        "tombstone_count": 0,
    }
