from __future__ import annotations

from pathlib import Path

from recall_origin import MemoryEngine


def test_v7_database_upgrades_to_v8_and_repeated_initialize_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upgrade.sqlite3"
    original = MemoryEngine.local(database).initialize()
    with original.store.transaction() as connection:
        connection.execute("DROP TABLE managed_pack_evidence")
        connection.execute("DROP TABLE managed_pack_claims")
        connection.execute("DROP INDEX managed_packs_expiry")
        connection.execute("DROP TABLE managed_packs")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
        connection.execute("PRAGMA user_version = 7")
        connection.execute("UPDATE storage_meta SET value = '7' WHERE key = 'schema_version'")

    first = MemoryEngine.local(database).initialize()
    second = MemoryEngine.local(database).initialize()

    assert first.store.schema_version >= 8
    assert second.store.schema_version == first.store.schema_version
    with second.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = 8"
            ).fetchone()[0]
            == 1
        )
        tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'managed_pack%'
                """
            )
        }
        assert tables == {
            "managed_packs",
            "managed_pack_claims",
            "managed_pack_evidence",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
