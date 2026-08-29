from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import recall_origin.application.engine as engine_module
from recall_origin import MemoryEngine
from recall_origin.application.evidence import MAX_PACK_BYTES
from recall_origin.contracts.errors import NOT_FOUND, REVISION_CONFLICT, RecallOriginError
from recall_origin.contracts.v1 import (
    ContextQuery,
    ForgetRequest,
    ForgetTarget,
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberRequest,
    SubjectRef,
)
from recall_origin.domain.enums import Capability, ForgetTargetType, PrincipalType


def _engine(
    database: Path,
    pack_root: Path,
    *,
    clock: object | None = None,
    principal: PrincipalContext | None = None,
    allowed_partitions: list[PartitionRef] | None = None,
) -> MemoryEngine:
    kwargs: dict[str, object] = {"managed_pack_root": pack_root}
    if clock is not None:
        kwargs["clock"] = clock
    if principal is not None:
        kwargs["principal"] = principal
    if allowed_partitions is not None:
        kwargs["allowed_partitions"] = allowed_partitions
    return MemoryEngine.local(database, **kwargs).initialize()


def _remember(
    engine: MemoryEngine,
    scope: PartitionRef,
    *,
    content: str,
    event_id: str,
    memory_key: str | None = None,
    subject: SubjectRef | None = None,
) -> object:
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            external_event_id=event_id,
            memory_key=memory_key,
            subject=subject,
            origin=OriginContext(producer_id="evidence-pack-test"),
        )
    )


def _build(
    engine: MemoryEngine,
    scope: PartitionRef,
    query: str,
    *,
    token_budget: int = 800,
) -> object:
    return engine.evidence_context(
        ContextQuery(
            query=query,
            scope=scope,
            token_budget=token_budget,
        )
    )


def _pack_root(engine: MemoryEngine, pack_id: str) -> Path:
    with engine.store.connection() as connection:
        row = connection.execute(
            "SELECT root_path FROM managed_packs WHERE pack_id = ?",
            (pack_id,),
        ).fetchone()
    assert row is not None
    return Path(str(row[0]))


def _sqlite_backup(source: Path, destination: Path) -> None:
    with (
        sqlite3.connect(source) as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def test_pack_directory_manifest_integrity_resource_uris_and_real_budget(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    engine = _engine(tmp_path / "memory.sqlite3", tmp_path / "packs")
    receipt = _remember(
        engine,
        scope,
        content="Evidence Pack directory contract needle.",
        event_id="pack-layout",
    )

    pack = _build(engine, scope, "directory contract needle", token_budget=128)
    root = _pack_root(engine, pack.pack_id)
    record = engine.get(receipt.claim_id)
    evidence_ids = {evidence.evidence_id for evidence in record.evidence if evidence.available}
    expected_paths = {
        "MANIFEST.md",
        "inspector.html",
        "manifest.json",
        "retrieval.json",
        f"memories/{receipt.claim_id}.md",
        *(f"sources/{evidence_id}.json" for evidence_id in evidence_ids),
    }
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}

    assert actual_paths == expected_paths
    assert set(pack.resource_uris) == {
        f"memory://packs/{pack.pack_id}/{relative}" for relative in expected_paths
    }
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert hashlib.sha256(manifest_bytes).hexdigest() == pack.integrity_sha256
    assert manifest["format"] == "recall-origin-evidence-pack"
    assert manifest["pack_id"] == pack.pack_id
    assert manifest["retrieval_id"] == pack.retrieval_id
    assert manifest["token_budget"] == 128
    assert manifest["token_count"] == pack.token_count
    assert manifest["integrity"]["algorithm"] == "sha256"
    for relative, digest in manifest["integrity"]["resources"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
    for relative in expected_paths:
        assert (
            engine.read_evidence_pack_resource(pack.pack_id, relative)
            == (root / relative).read_bytes()
        )

    assert pack.token_count <= pack.token_budget
    assert sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) <= (
        MAX_PACK_BYTES
    )
    serialized = json.dumps(manifest, ensure_ascii=False).lower()
    assert "benchmark" not in serialized
    assert not any("benchmark" in relative.lower() for relative in actual_paths)
    inspector = (root / "inspector.html").read_text(encoding="utf-8")
    assert "No benchmark measurements supplied" in inspector
    assert '"metrics":[]' in inspector


def test_pack_contains_only_live_current_revision_evidence_excerpt(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    engine = _engine(tmp_path / "memory.sqlite3", tmp_path / "packs")
    first = _remember(
        engine,
        scope,
        content="current evidence needle",
        event_id="old-evidence-event",
        memory_key="evidence.current",
    )
    second = _remember(
        engine,
        scope,
        content="current evidence needle",
        event_id="live-evidence-event",
        memory_key="evidence.current",
    )
    assert second.claim_id == first.claim_id
    before = engine.get(first.claim_id)
    old_evidence = next(
        evidence for evidence in before.evidence if evidence.event_id == first.event_id
    )
    live_evidence = next(
        evidence for evidence in before.evidence if evidence.event_id == second.event_id
    )
    engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.EVENT,
                target_id=first.event_id,
            ),
            idempotency_key="delete-old-pack-evidence",
            cascade_policy="safe",
        )
    )

    current = engine.get(first.claim_id)
    assert current.revision_id != before.revision_id
    assert [evidence.evidence_id for evidence in current.evidence] == [live_evidence.evidence_id]
    pack = _build(engine, scope, "current evidence needle")
    root = _pack_root(engine, pack.pack_id)
    source_paths = {path.relative_to(root).as_posix() for path in (root / "sources").iterdir()}

    assert source_paths == {f"sources/{live_evidence.evidence_id}.json"}
    assert not (root / f"sources/{old_evidence.evidence_id}.json").exists()
    source = json.loads(
        engine.read_evidence_pack_resource(
            pack.pack_id,
            f"sources/{live_evidence.evidence_id}.json",
        )
    )
    assert source["event_id"] == second.event_id
    assert source["available"] is True


def test_pack_resource_rejects_path_traversal_cross_scope_and_expiry(
    tmp_path: Path,
) -> None:
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    now = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "packs"
    owner = _engine(database, pack_root, clock=lambda: now[0])
    _remember(
        owner,
        alpha,
        content="expiring pack needle",
        event_id="expiring-pack",
    )
    pack = owner.evidence_context(
        ContextQuery(query="expiring pack", scope=alpha, token_budget=800),
        ttl_seconds=60,
    )

    for unsafe in (
        "../manifest.json",
        "/etc/passwd",
        "memories/../../manifest.json",
        ".",
        "",
    ):
        with pytest.raises(RecallOriginError) as traversal:
            owner.read_evidence_pack_resource(pack.pack_id, unsafe)
        assert traversal.value.spec is NOT_FOUND

    beta_reader = _engine(
        database,
        pack_root,
        clock=lambda: now[0],
        principal=PrincipalContext(
            tenant_id="local",
            principal_id="beta-reader",
            principal_type=PrincipalType.AGENT,
            capabilities=frozenset({Capability.READ}),
        ),
        allowed_partitions=[beta],
    )
    with pytest.raises(RecallOriginError) as cross_scope:
        beta_reader.read_evidence_pack_resource(pack.pack_id, "manifest.json")
    assert cross_scope.value.spec is NOT_FOUND

    now[0] += timedelta(seconds=61)
    with pytest.raises(RecallOriginError) as expired:
        owner.read_evidence_pack_resource(pack.pack_id, "manifest.json")
    assert expired.value.spec is NOT_FOUND


@pytest.mark.parametrize(
    "target_type",
    [
        ForgetTargetType.CLAIM,
        ForgetTargetType.EVENT,
        ForgetTargetType.SUBJECT,
        ForgetTargetType.PARTITION,
    ],
)
def test_domain_forget_immediately_hides_pack_and_purge_removes_directory(
    tmp_path: Path,
    scope: PartitionRef,
    target_type: ForgetTargetType,
) -> None:
    database = tmp_path / f"{target_type.value}.sqlite3"
    engine = _engine(database, tmp_path / f"packs-{target_type.value}")
    subject = SubjectRef(subject_type="user", subject_id=f"user-{target_type.value}")
    receipt = _remember(
        engine,
        scope,
        content=f"{target_type.value} deletion pack needle",
        event_id=f"pack-delete-{target_type.value}",
        subject=subject,
    )
    pack = _build(engine, scope, f"{target_type.value} deletion")
    root = _pack_root(engine, pack.pack_id)
    target_ids = {
        ForgetTargetType.CLAIM: receipt.claim_id,
        ForgetTargetType.EVENT: receipt.event_id,
        ForgetTargetType.SUBJECT: subject.subject_id,
        ForgetTargetType.PARTITION: scope.serialize(),
    }
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=target_type,
                target_id=target_ids[target_type],
            ),
            scope=scope
            if target_type
            in {
                ForgetTargetType.SUBJECT,
                ForgetTargetType.PARTITION,
            }
            else None,
            idempotency_key=f"delete-pack-via-{target_type.value}",
            cascade_policy="purge",
        )
    )

    assert root.is_dir()
    with pytest.raises(RecallOriginError) as hidden:
        engine.read_evidence_pack_resource(pack.pack_id, "manifest.json")
    assert hidden.value.spec is NOT_FOUND

    engine.purge(deletion.deletion_id)
    assert not root.exists()
    with engine.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT status FROM managed_packs WHERE pack_id = ?",
                (pack.pack_id,),
            ).fetchone()[0]
            == "purged"
        )


def test_managed_pack_typed_deletion_removes_only_the_selected_pack(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    engine = _engine(tmp_path / "memory.sqlite3", tmp_path / "packs")
    _remember(
        engine,
        scope,
        content="two independent managed packs",
        event_id="two-packs",
    )
    first = _build(engine, scope, "independent managed packs")
    second = _build(engine, scope, "independent managed packs")
    first_root = _pack_root(engine, first.pack_id)
    second_root = _pack_root(engine, second.pack_id)

    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.MANAGED_PACK,
                target_id=first.pack_id,
            ),
            idempotency_key="delete-only-first-pack",
            cascade_policy="purge",
        )
    )

    with pytest.raises(RecallOriginError) as hidden:
        engine.read_evidence_pack_resource(first.pack_id, "manifest.json")
    assert hidden.value.spec is NOT_FOUND
    assert engine.read_evidence_pack_resource(second.pack_id, "manifest.json")
    engine.purge(deletion.deletion_id)

    assert not first_root.exists()
    assert second_root.is_dir()
    assert engine.read_evidence_pack_resource(second.pack_id, "manifest.json")
    with engine.store.connection() as connection:
        statuses = dict(
            connection.execute(
                "SELECT pack_id, status FROM managed_packs WHERE pack_id IN (?, ?)",
                (first.pack_id, second.pack_id),
            )
        )
    assert statuses == {first.pack_id: "purged", second.pack_id: "active"}


def test_revision_race_before_registration_leaves_no_orphan_pack_directory(
    tmp_path: Path,
    scope: PartitionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_root = tmp_path / "packs"
    engine = _engine(tmp_path / "memory.sqlite3", pack_root)
    original = _remember(
        engine,
        scope,
        content="revision race pack needle",
        event_id="race-original",
        memory_key="race.pack",
    )
    real_write = engine_module.write_managed_pack

    def write_then_change_revision(**kwargs: object) -> object:
        written = real_write(**kwargs)  # type: ignore[arg-type]
        reinforced = _remember(
            engine,
            scope,
            content="revision race pack needle",
            event_id="race-reinforcement",
            memory_key="race.pack",
        )
        assert reinforced.claim_id == original.claim_id
        assert reinforced.revision_id != original.revision_id
        return written

    monkeypatch.setattr(engine_module, "write_managed_pack", write_then_change_revision)

    with pytest.raises(RecallOriginError) as raced:
        _build(engine, scope, "revision race pack")
    assert raced.value.spec is REVISION_CONFLICT
    assert list(pack_root.iterdir()) == []
    with engine.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM managed_packs").fetchone()[0] == 0


def test_export_is_integrity_checked_private_and_explicitly_outside_purge(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    engine = _engine(tmp_path / "memory.sqlite3", tmp_path / "packs")
    receipt = _remember(
        engine,
        scope,
        content="External exports have an explicit deletion boundary.",
        event_id="export-boundary",
    )
    pack = _build(engine, scope, "explicit deletion boundary")
    managed_root = _pack_root(engine, pack.pack_id)
    destination = tmp_path / "portable-copy"

    exported = engine.export_evidence_pack(pack.pack_id, destination)

    assert exported == destination
    assert {
        path.relative_to(exported).as_posix() for path in exported.rglob("*") if path.is_file()
    } == {
        path.relative_to(managed_root).as_posix()
        for path in managed_root.rglob("*")
        if path.is_file()
    }
    assert all(
        path.stat().st_mode & 0o077 == 0
        for path in [exported, *(item for item in exported.rglob("*") if item.is_file())]
    )
    with pytest.raises(RecallOriginError) as existing:
        engine.export_evidence_pack(pack.pack_id, destination)
    assert existing.value.spec is REVISION_CONFLICT

    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=receipt.claim_id,
            ),
            idempotency_key="export-boundary-delete",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)

    assert not managed_root.exists()
    assert (exported / "manifest.json").is_file()


def test_export_rejects_manifest_and_resource_tampered_together(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    engine = _engine(tmp_path / "memory.sqlite3", tmp_path / "packs")
    _remember(
        engine,
        scope,
        content="The database anchors the Evidence Pack manifest digest.",
        event_id="manifest-anchor",
    )
    pack = _build(engine, scope, "manifest digest anchor")
    managed_root = _pack_root(engine, pack.pack_id)
    manifest_path = managed_root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    resource_path = managed_root / "MANIFEST.md"
    forged_content = b"# Forged Evidence Pack\n"

    resource_path.write_bytes(forged_content)
    manifest["integrity"]["resources"]["MANIFEST.md"] = hashlib.sha256(forged_content).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RecallOriginError) as tampered:
        engine.export_evidence_pack(pack.pack_id, tmp_path / "forged-export")

    assert tampered.value.spec is NOT_FOUND
    assert "manifest failed its integrity check" in str(tampered.value)
    assert not (tmp_path / "forged-export").exists()


def test_restored_pack_obeys_current_sidecar_fence_but_new_safe_pack_is_readable(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("alpha")
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    database = tmp_path / "live.sqlite3"
    registry = tmp_path / "purge.sqlite3"
    registry_key = b"p" * 32
    pack_root = tmp_path / "packs"
    engine = MemoryEngine.local(
        database,
        managed_pack_root=pack_root,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
        clock=lambda: now,
    ).initialize()
    target = _remember(
        engine,
        scope,
        content="deleted pack target ultraviolet-badger-4821",
        event_id="restore-pack-target",
    )
    _remember(
        engine,
        scope,
        content="safe post-fence amber-otter-7392",
        event_id="restore-pack-survivor",
    )
    deleted_pack = _build(engine, scope, "ultraviolet-badger-4821")
    snapshot = tmp_path / "before-pack-deletion.sqlite3"
    _sqlite_backup(database, snapshot)

    engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=target.claim_id,
            ),
            idempotency_key="delete-before-pack-restore",
            expected_revision_id=target.revision_id,
        )
    )

    restored_database = tmp_path / "restored.sqlite3"
    _sqlite_backup(snapshot, restored_database)
    restored = MemoryEngine.local(
        restored_database,
        managed_pack_root=pack_root,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
        clock=lambda: now,
    ).initialize()

    with pytest.raises(RecallOriginError) as hidden:
        restored.read_evidence_pack_resource(deleted_pack.pack_id, "manifest.json")
    assert hidden.value.spec is NOT_FOUND

    safe_pack = _build(restored, scope, "amber-otter-7392")
    assert restored.read_evidence_pack_resource(safe_pack.pack_id, "manifest.json")


def test_reconfigured_managed_root_cannot_read_a_registered_out_of_root_pack(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "root-binding.sqlite3"
    original_root = tmp_path / "original-packs"
    owner = _engine(database, original_root)
    _remember(
        owner,
        scope,
        content="managed pack reads are bound to the configured root",
        event_id="managed-root-binding",
    )
    pack = _build(owner, scope, "configured root")
    assert owner.read_evidence_pack_resource(pack.pack_id, "manifest.json")

    reopened = _engine(database, tmp_path / "different-packs")

    with pytest.raises(RecallOriginError) as denied:
        reopened.read_evidence_pack_resource(pack.pack_id, "manifest.json")
    assert denied.value.spec is NOT_FOUND


def test_initialize_removes_stale_pack_crash_orphan_after_atomic_rename(
    tmp_path: Path,
    scope: PartitionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "packs"
    engine = _engine(database, pack_root)
    _remember(
        engine,
        scope,
        content="crash orphan pack needle",
        event_id="crash-orphan",
    )
    real_write = engine_module.write_managed_pack

    def crash_after_rename(**kwargs: object) -> object:
        written = real_write(**kwargs)  # type: ignore[arg-type]
        raise SystemExit(str(written.root))

    monkeypatch.setattr(engine_module, "write_managed_pack", crash_after_rename)
    with pytest.raises(SystemExit):
        _build(engine, scope, "crash orphan")

    orphans = list(pack_root.glob("pack_*"))
    assert len(orphans) == 1
    os.utime(orphans[0], (0, 0))
    with engine.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM managed_packs").fetchone()[0] == 0

    _engine(database, pack_root)
    assert not orphans[0].exists()


def test_initialize_reconciles_only_stale_recognizable_unregistered_directories(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "packs"
    engine = _engine(database, pack_root)
    _remember(
        engine,
        scope,
        content="registered pack survives reconciliation",
        event_id="registered-pack",
    )
    registered = _build(engine, scope, "registered pack")
    registered_root = _pack_root(engine, registered.pack_id)
    stale_pack = pack_root / "pack_crash_orphan"
    stale_stage = pack_root / ".pack_crash_orphan.staging"
    recent_inflight = pack_root / "pack_recent_inflight"
    unrelated = pack_root / "user-content"
    external = tmp_path / "external-content"
    link = pack_root / "pack_symlink"
    for directory in (stale_pack, stale_stage, recent_inflight, unrelated, external):
        directory.mkdir()
    link.symlink_to(external, target_is_directory=True)
    for directory in (stale_pack, stale_stage):
        os.utime(directory, (0, 0))

    _engine(database, pack_root)

    assert registered_root.is_dir()
    assert not stale_pack.exists()
    assert not stale_stage.exists()
    assert recent_inflight.is_dir()
    assert unrelated.is_dir()
    assert link.is_symlink()
    assert external.is_dir()
