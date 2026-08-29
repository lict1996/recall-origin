from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import (
    IDEMPOTENCY_KEY_REUSED,
    NOT_FOUND,
    RecallOriginError,
)
from recall_origin.contracts.v1 import (
    ContextQuery,
    FeedbackRequest,
    ForgetRequest,
    ForgetTarget,
    GovernRequest,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
    SubjectRef,
)
from recall_origin.domain.enums import (
    DeletionState,
    FeedbackType,
    ForgetTargetType,
    GovernAction,
)
from recall_origin.storage.sqlite.errors import PurgeRegistryError


def _remember(
    engine: MemoryEngine,
    scope: PartitionRef,
    *,
    content: str,
    external_event_id: str,
    memory_key: str | None = None,
) -> object:
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            memory_key=memory_key,
            external_event_id=external_event_id,
            origin=OriginContext(producer_id="deletion-test"),
        )
    )


def _delete_event(
    engine: MemoryEngine,
    event_id: str,
    *,
    idempotency_key: str,
) -> object:
    return engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.EVENT,
                target_id=event_id,
            ),
            idempotency_key=idempotency_key,
        )
    )


def _sqlite_backup(source: Path, destination: Path) -> None:
    with (
        sqlite3.connect(source) as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def test_event_deletion_rebuilds_a_multi_evidence_claim_and_is_idempotent(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    first = _remember(
        engine,
        scope,
        content="This workspace uses pnpm.",
        external_event_id="source-one",
        memory_key="workspace.package_manager",
    )
    second = _remember(
        engine,
        scope,
        content="This workspace uses pnpm.",
        external_event_id="source-two",
        memory_key="workspace.package_manager",
    )
    assert first.claim_id == second.claim_id
    assert second.source_count == 2

    deletion = _delete_event(engine, first.event_id, idempotency_key="delete-source-one")
    replay = _delete_event(engine, first.event_id, idempotency_key="delete-source-one")
    current = engine.get(first.claim_id)

    assert deletion.state is DeletionState.LOGICALLY_HIDDEN
    assert replay.replayed is True
    assert replay.deletion_id == deletion.deletion_id
    assert current.source_count == 1
    assert [item.claim_id for item in engine.search(SearchRequest(query="pnpm", scope=scope))] == [
        first.claim_id
    ]

    _delete_event(engine, second.event_id, idempotency_key="delete-source-two")
    assert engine.search(SearchRequest(query="pnpm", scope=scope)) == ()
    with pytest.raises(RecallOriginError) as missing:
        engine.get(first.claim_id)
    assert missing.value.spec is NOT_FOUND

    with pytest.raises(RecallOriginError) as replay_deleted_event:
        _remember(
            engine,
            scope,
            content="This workspace uses pnpm.",
            external_event_id="source-one",
            memory_key="workspace.package_manager",
        )
    assert replay_deleted_event.value.spec is IDEMPOTENCY_KEY_REUSED


def test_deletion_idempotency_key_cannot_be_reused_for_another_target(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    first = _remember(
        engine,
        scope,
        content="first deletion target",
        external_event_id="first-target",
    )
    second = _remember(
        engine,
        scope,
        content="second deletion target",
        external_event_id="second-target",
    )
    _delete_event(engine, first.event_id, idempotency_key="one-delete-key")

    with pytest.raises(RecallOriginError) as mismatch:
        _delete_event(engine, second.event_id, idempotency_key="one-delete-key")

    assert mismatch.value.spec is IDEMPOTENCY_KEY_REUSED
    assert engine.get(second.claim_id).content == "second deletion target"


def test_unknown_managed_pack_deletion_is_not_found(
    engine: MemoryEngine,
) -> None:
    with pytest.raises(RecallOriginError) as missing:
        engine.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.MANAGED_PACK,
                    target_id="pack-1",
                ),
                idempotency_key="delete-pack",
            )
        )

    assert missing.value.spec is NOT_FOUND


def test_restoring_a_pre_deletion_snapshot_applies_the_current_purge_registry(
    tmp_path: Path,
    id_factory: Callable[[str], str],
    scope: PartitionRef,
) -> None:
    database = tmp_path / "live.sqlite3"
    registry = tmp_path / "purge.sqlite3"
    registry_key = b"p" * 32
    engine = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        id_factory=id_factory,
    ).initialize()
    target = _remember(
        engine,
        scope,
        content="restore must never reveal ultraviolet-badger-4821",
        external_event_id="restore-target",
    )
    survivor = _remember(
        engine,
        scope,
        content="this record survives restore",
        external_event_id="restore-survivor",
    )
    old_snapshot = tmp_path / "before-deletion.sqlite3"
    _sqlite_backup(database, old_snapshot)

    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=target.claim_id,
            ),
            idempotency_key="delete-before-restore",
            expected_revision_id=target.revision_id,
        )
    )
    assert deletion.state is DeletionState.LOGICALLY_HIDDEN

    restored_path = tmp_path / "restored.sqlite3"
    _sqlite_backup(old_snapshot, restored_path)
    restored = MemoryEngine.local(
        restored_path,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    ).initialize()

    assert restored.search(SearchRequest(query="ultraviolet", scope=scope)) == ()
    with pytest.raises(RecallOriginError) as hidden:
        restored.get(target.claim_id)
    assert hidden.value.spec is NOT_FOUND
    assert restored.get(survivor.claim_id).content == "this record survives restore"

    restored.reindex()
    assert restored.search(SearchRequest(query="ultraviolet", scope=scope)) == ()


def test_restored_subject_fence_hides_stale_event_subject_revision(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "subject-source.sqlite3"
    registry = tmp_path / "subject-purge.sqlite3"
    registry_key = b"s" * 32
    owner = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    original = _remember(
        owner,
        scope,
        content="Alice prefers dark mode.",
        external_event_id="subject-source-one",
        memory_key="preference.theme",
    )
    reinforced = owner.remember(
        RememberRequest(
            content="ALICE PREFERS DARK MODE.",
            scope=scope,
            memory_key="preference.theme",
            subject=SubjectRef(subject_type="person", subject_id="alice"),
            external_event_id="subject-source-two",
            origin=OriginContext(producer_id="deletion-test"),
        )
    )
    assert reinforced.claim_id == original.claim_id
    assert reinforced.source_count == 2
    with owner.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM claim_subjects WHERE claim_id = ?",
                (original.claim_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
                SELECT count(*) FROM event_subjects
                WHERE event_id = ? AND subject_id = 'alice'
                """,
                (reinforced.event_id,),
            ).fetchone()[0]
            == 1
        )

    snapshot = tmp_path / "subject-before-deletion.sqlite3"
    _sqlite_backup(database, snapshot)
    owner.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.SUBJECT,
                target_id="alice",
            ),
            scope=scope,
            idempotency_key="delete-subject-before-restore",
        )
    )
    assert owner.get(original.claim_id).source_count == 1

    restored_path = tmp_path / "subject-restored.sqlite3"
    _sqlite_backup(snapshot, restored_path)
    restored = MemoryEngine.local(
        restored_path,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    with restored.store.connection() as connection:
        stale_head = str(
            connection.execute(
                "SELECT current_revision_id FROM claim_heads WHERE claim_id = ?",
                (original.claim_id,),
            ).fetchone()[0]
        )
        stale_revision_count = int(
            connection.execute(
                "SELECT count(*) FROM memory_revisions WHERE claim_id = ?",
                (original.claim_id,),
            ).fetchone()[0]
        )

    assert restored.search(SearchRequest(query="dark mode", scope=scope)) == ()
    with pytest.raises(RecallOriginError) as hidden_get:
        restored.get(original.claim_id)
    assert hidden_get.value.spec is NOT_FOUND
    assert restored.context(ContextQuery(query="dark mode", scope=scope)).items == ()

    restored.reindex()
    assert restored.search(SearchRequest(query="dark mode", scope=scope)) == ()

    with pytest.raises(RecallOriginError) as hidden_govern:
        restored.govern(
            GovernRequest(
                claim_id=original.claim_id,
                expected_revision_id=reinforced.revision_id,
                action=GovernAction.QUARANTINE,
                reason="A restored subject fence must block stale governance.",
            )
        )
    assert hidden_govern.value.spec is NOT_FOUND
    with pytest.raises(RecallOriginError) as hidden_feedback:
        restored.feedback(
            FeedbackRequest(
                claim_id=original.claim_id,
                revision_id=reinforced.revision_id,
                feedback_type=FeedbackType.INCORRECT,
                reason="A restored subject fence must block stale feedback.",
            )
        )
    assert hidden_feedback.value.spec is NOT_FOUND

    replacement = _remember(
        restored,
        scope,
        content="Alice prefers dark mode.",
        external_event_id="subject-safe-replacement",
        memory_key="preference.theme",
    )
    assert replacement.claim_id != original.claim_id
    assert restored.get(replacement.claim_id).source_count == 1
    with pytest.raises(RecallOriginError) as still_hidden:
        restored.get(original.claim_id)
    assert still_hidden.value.spec is NOT_FOUND
    with restored.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT current_revision_id FROM claim_heads WHERE claim_id = ?",
                (original.claim_id,),
            ).fetchone()[0]
            == stale_head
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_revisions WHERE claim_id = ?",
                (original.claim_id,),
            ).fetchone()[0]
            == stale_revision_count
        )


@pytest.mark.parametrize(
    "target_type",
    [ForgetTargetType.CLAIM, ForgetTargetType.EVENT],
)
def test_restored_fence_cannot_reinforce_or_supersede_a_deleted_claim(
    tmp_path: Path,
    scope: PartitionRef,
    target_type: ForgetTargetType,
) -> None:
    database = tmp_path / f"{target_type.value}-source.sqlite3"
    registry = tmp_path / f"{target_type.value}-purge.sqlite3"
    registry_key = b"r" * 32
    owner = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    deleted = _remember(
        owner,
        scope,
        content="a restored fence must never revive this claim",
        external_event_id=f"{target_type.value}-deleted-source",
        memory_key="restore.fenced-memory-key",
    )
    snapshot = tmp_path / f"{target_type.value}-before-deletion.sqlite3"
    _sqlite_backup(database, snapshot)
    target_id = deleted.claim_id if target_type is ForgetTargetType.CLAIM else deleted.event_id
    deletion = owner.forget(
        ForgetRequest(
            target=ForgetTarget(target_type=target_type, target_id=target_id),
            idempotency_key=f"delete-{target_type.value}-before-restore",
            expected_revision_id=(
                deleted.revision_id if target_type is ForgetTargetType.CLAIM else None
            ),
        )
    )
    owner.purge(deletion.deletion_id)

    restored_path = tmp_path / f"{target_type.value}-restored.sqlite3"
    _sqlite_backup(snapshot, restored_path)
    restored = MemoryEngine.local(
        restored_path,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    with restored.store.connection() as connection:
        old_head = str(
            connection.execute(
                "SELECT current_revision_id FROM claim_heads WHERE claim_id = ?",
                (deleted.claim_id,),
            ).fetchone()[0]
        )
        old_revision_count = int(
            connection.execute(
                "SELECT count(*) FROM memory_revisions WHERE claim_id = ?",
                (deleted.claim_id,),
            ).fetchone()[0]
        )

    replacement = _remember(
        restored,
        scope,
        content="a restored fence must never revive this claim",
        external_event_id=f"{target_type.value}-replacement-source",
        memory_key="restore.fenced-memory-key",
    )

    assert replacement.claim_id != deleted.claim_id
    assert restored.get(replacement.claim_id).content == (
        "a restored fence must never revive this claim"
    )
    with pytest.raises(RecallOriginError) as hidden:
        restored.get(deleted.claim_id)
    assert hidden.value.spec is NOT_FOUND
    with restored.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT current_revision_id FROM claim_heads WHERE claim_id = ?",
                (deleted.claim_id,),
            ).fetchone()[0]
            == old_head
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_revisions WHERE claim_id = ?",
                (deleted.claim_id,),
            ).fetchone()[0]
            == old_revision_count
        )


def test_restore_fails_closed_without_the_current_purge_registry(
    tmp_path: Path,
    id_factory: Callable[[str], str],
    scope: PartitionRef,
) -> None:
    database = tmp_path / "source.sqlite3"
    registry = tmp_path / "source-purge.sqlite3"
    registry_key = b"p" * 32
    engine = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
        id_factory=id_factory,
    ).initialize()
    _remember(
        engine,
        scope,
        content="snapshot needs its purge registry",
        external_event_id="snapshot-source",
    )
    restored_path = tmp_path / "orphaned-restore.sqlite3"
    _sqlite_backup(database, restored_path)

    with pytest.raises(PurgeRegistryError):
        MemoryEngine.local(
            restored_path,
            purge_registry_path=tmp_path / "missing-purge.sqlite3",
            purge_registry_key=registry_key,
        ).initialize()


def test_restart_recovers_purge_lifecycle_after_sidecar_only_crash(
    tmp_path: Path,
    scope: PartitionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "memory.sqlite3"
    registry = tmp_path / "purge.sqlite3"
    registry_key = b"p" * 32
    owner = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    target = _remember(
        owner,
        scope,
        content="sidecar-only deletion must retain a purge entry point",
        external_event_id="sidecar-only-crash-target",
    )

    def fail_after_sidecar_append(*_: object, **__: object) -> int:
        raise RuntimeError("injected failure after sidecar append")

    monkeypatch.setattr(owner, "_next_tx", fail_after_sidecar_append)
    with pytest.raises(RuntimeError, match="injected failure after sidecar append"):
        owner.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.CLAIM,
                    target_id=target.claim_id,
                ),
                idempotency_key="sidecar-only-crash",
                expected_revision_id=target.revision_id,
            )
        )

    with sqlite3.connect(registry) as registry_connection:
        deletion_id = str(
            registry_connection.execute("SELECT deletion_id FROM purge_tombstones").fetchone()[0]
        )
    with sqlite3.connect(database) as main_connection:
        assert main_connection.execute("SELECT count(*) FROM deletion_requests").fetchone()[0] == 0
        assert main_connection.execute("SELECT count(*) FROM deletion_layers").fetchone()[0] == 0
        assert main_connection.execute("SELECT count(*) FROM tombstone_fences").fetchone()[0] == 0
        assert main_connection.execute("SELECT count(*) FROM claim_contents").fetchone()[0] == 1

    recovered = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    receipt = recovered.deletion_status(deletion_id)

    assert receipt.target.target_type is ForgetTargetType.CLAIM
    assert receipt.target.target_id == target.claim_id
    assert receipt.state is DeletionState.LOGICALLY_HIDDEN
    assert {layer.layer: layer.state for layer in receipt.layers} == {
        "authoritative_text": DeletionState.ACCEPTED,
        "fts": DeletionState.ACCEPTED,
        "logical_visibility": DeletionState.COMPLETED,
        "managed_packs": DeletionState.ACCEPTED,
    }

    completed = recovered.purge(deletion_id)
    assert completed.state is DeletionState.COMPLETED
    with sqlite3.connect(database) as main_connection:
        assert main_connection.execute("SELECT count(*) FROM claim_contents").fetchone()[0] == 0
        assert (
            main_connection.execute(
                """
                SELECT count(*) FROM ledger_transactions
                WHERE operation = 'recover:purge-registry'
                """
            ).fetchone()[0]
            == 1
        )

    reopened = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    assert reopened.deletion_status(deletion_id).state is DeletionState.COMPLETED
    with sqlite3.connect(database) as main_connection:
        assert (
            main_connection.execute(
                """
                SELECT count(*) FROM ledger_transactions
                WHERE operation = 'recover:purge-registry'
                """
            ).fetchone()[0]
            == 1
        )


def test_retry_after_sidecar_only_crash_reuses_recovered_deletion_lifecycle(
    tmp_path: Path,
    scope: PartitionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "retry-memory.sqlite3"
    registry = tmp_path / "retry-purge.sqlite3"
    registry_key = b"q" * 32
    owner = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    target = _remember(
        owner,
        scope,
        content="retry must adopt the recovered deletion lifecycle",
        external_event_id="retry-sidecar-only-target",
    )
    request = ForgetRequest(
        target=ForgetTarget(
            target_type=ForgetTargetType.CLAIM,
            target_id=target.claim_id,
        ),
        idempotency_key="retry-sidecar-only",
        expected_revision_id=target.revision_id,
    )

    def fail_after_sidecar_append(*_: object, **__: object) -> int:
        raise RuntimeError("injected retry crash after sidecar append")

    monkeypatch.setattr(owner, "_next_tx", fail_after_sidecar_append)
    with pytest.raises(RuntimeError, match="injected retry crash after sidecar append"):
        owner.forget(request)
    with sqlite3.connect(registry) as registry_connection:
        original_deletion_id = str(
            registry_connection.execute("SELECT deletion_id FROM purge_tombstones").fetchone()[0]
        )

    recovered = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    retry = recovered.forget(request)

    assert retry.deletion_id == original_deletion_id
    assert retry.replayed is True
    completed = recovered.purge(retry.deletion_id)
    assert completed.state is DeletionState.COMPLETED
    with recovered.store.connection() as connection:
        assert (
            connection.execute(
                """
                SELECT count(*)
                FROM deletion_requests
                WHERE partition_id = (
                    SELECT partition_id FROM memory_claims WHERE claim_id = ?
                )
                  AND target_type = 'claim'
                  AND target_id = ?
                """,
                (target.claim_id, target.claim_id),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
                SELECT count(*)
                FROM deletion_requests
                WHERE target_type = 'claim'
                  AND target_id = ?
                  AND state <> 'completed'
                """,
                (target.claim_id,),
            ).fetchone()[0]
            == 0
        )


def test_restored_sidecar_fence_hides_pre_crash_retrieval_trace(
    tmp_path: Path,
    scope: PartitionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "trace-memory.sqlite3"
    registry = tmp_path / "trace-purge.sqlite3"
    registry_key = b"t" * 32
    owner = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    target = _remember(
        owner,
        scope,
        content="restored deletion fences must hide retrieval trace identifiers",
        external_event_id="trace-sidecar-only-target",
    )
    search = owner.search_result(SearchRequest(query="retrieval trace identifiers", scope=scope))
    assert search.retrieval_id is not None
    assert owner.retrieval_trace(search.retrieval_id).selected_claim_ids == (target.claim_id,)

    def fail_after_sidecar_append(*_: object, **__: object) -> int:
        raise RuntimeError("injected trace crash after sidecar append")

    monkeypatch.setattr(owner, "_next_tx", fail_after_sidecar_append)
    with pytest.raises(RuntimeError, match="injected trace crash after sidecar append"):
        owner.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.CLAIM,
                    target_id=target.claim_id,
                ),
                idempotency_key="trace-sidecar-only",
                expected_revision_id=target.revision_id,
            )
        )

    recovered = MemoryEngine.local(
        database,
        purge_registry_path=registry,
        purge_registry_key=registry_key,
    ).initialize()
    with pytest.raises(RecallOriginError) as hidden:
        recovered.retrieval_trace(search.retrieval_id)
    assert hidden.value.spec is NOT_FOUND


def test_claim_forget_holds_revision_cas_lock_until_fence_and_tombstone_commit(
    tmp_path: Path,
    scope: PartitionRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "memory.sqlite3"
    owner = MemoryEngine.local(database).initialize()
    contender = MemoryEngine.local(database).initialize()
    target = _remember(
        owner,
        scope,
        content="claim forget CAS must be atomic with its fence",
        external_event_id="forget-cas-target",
    )
    transaction_attempted = threading.Event()
    transaction_acquired = threading.Event()
    governance_finished = threading.Event()
    governance_errors: list[BaseException] = []
    real_transaction = contender.store.transaction

    @contextmanager
    def instrumented_transaction(mode: Any = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
        transaction_attempted.set()
        with real_transaction(mode) as connection:
            transaction_acquired.set()
            yield connection

    monkeypatch.setattr(contender.store, "transaction", instrumented_transaction)
    real_record = owner.store.purge_registry.record
    governance_thread: threading.Thread | None = None

    def govern_concurrently() -> None:
        try:
            contender.govern(
                GovernRequest(
                    claim_id=target.claim_id,
                    expected_revision_id=target.revision_id,
                    action=GovernAction.QUARANTINE,
                    reason="Deterministic concurrent governance attempt.",
                )
            )
        except BaseException as exc:
            governance_errors.append(exc)
        finally:
            governance_finished.set()

    def record_after_governance_attempt(**kwargs: Any) -> Any:
        nonlocal governance_thread
        governance_thread = threading.Thread(target=govern_concurrently)
        governance_thread.start()
        assert transaction_attempted.wait(timeout=2)
        transaction_acquired.wait(timeout=0.25)
        return real_record(**kwargs)

    monkeypatch.setattr(owner.store.purge_registry, "record", record_after_governance_attempt)
    deletion = owner.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=target.claim_id,
            ),
            idempotency_key="claim-forget-cas",
            expected_revision_id=target.revision_id,
        )
    )

    assert governance_finished.wait(timeout=5)
    assert governance_thread is not None
    governance_thread.join(timeout=1)
    assert len(governance_errors) == 1
    assert isinstance(governance_errors[0], RecallOriginError)
    assert governance_errors[0].spec is NOT_FOUND
    assert owner.deletion_status(deletion.deletion_id).state is DeletionState.LOGICALLY_HIDDEN
    with owner.store.connection() as connection:
        assert (
            connection.execute(
                """
                SELECT count(*)
                FROM tombstone_fences
                WHERE target_type = 'claim' AND target_id = ?
                """,
                (target.claim_id,),
            ).fetchone()[0]
            == 1
        )
