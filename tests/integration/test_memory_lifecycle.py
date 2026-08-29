from __future__ import annotations

import pytest
from pydantic import ValidationError

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import (
    IDEMPOTENCY_KEY_REUSED,
    NOT_FOUND,
    REVISION_CONFLICT,
    RecallOriginError,
)
from recall_origin.contracts.v1 import (
    ForgetRequest,
    ForgetTarget,
    GovernRequest,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)
from recall_origin.domain.enums import (
    DeletionState,
    ForgetTargetType,
    GovernAction,
    MemoryStatus,
)


def _remember(
    engine: MemoryEngine,
    scope: PartitionRef,
    content: str,
    external_event_id: str,
    *,
    producer_id: str = "test",
    memory_key: str | None = None,
) -> object:
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            memory_key=memory_key,
            external_event_id=external_event_id,
            origin=OriginContext(producer_id=producer_id),
        )
    )


def test_remember_replay_reinforcement_and_key_supersede(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    first = _remember(
        engine,
        scope,
        "This workspace uses pnpm.",
        "event-1",
        memory_key="workspace.package_manager",
    )
    replay = _remember(
        engine,
        scope,
        "This workspace uses pnpm.",
        "event-1",
        memory_key="workspace.package_manager",
    )
    reinforced = _remember(
        engine,
        scope,
        "This workspace uses pnpm.",
        "event-2",
        memory_key="workspace.package_manager",
    )

    assert replay.replayed is True
    assert replay.claim_id == first.claim_id
    assert replay.revision_id == first.revision_id
    assert reinforced.claim_id == first.claim_id
    assert reinforced.revision_id != first.revision_id
    assert reinforced.source_count == 2

    replacement = _remember(
        engine,
        scope,
        "This workspace uses uv.",
        "event-3",
        memory_key="workspace.package_manager",
    )
    assert replacement.claim_id != first.claim_id
    with pytest.raises(RecallOriginError) as error:
        engine.get(first.claim_id)
    assert error.value.spec is NOT_FOUND
    hits = engine.search(SearchRequest(query="package manager", scope=scope))
    assert [hit.claim_id for hit in hits] == [replacement.claim_id]


def test_same_external_id_is_namespaced_by_producer(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    one = _remember(engine, scope, "one", "same-id", producer_id="producer-a")
    two = _remember(engine, scope, "two", "same-id", producer_id="producer-b")
    assert one.event_id != two.event_id


def test_changed_idempotent_payload_is_rejected(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    _remember(engine, scope, "one", "same-id")
    with pytest.raises(RecallOriginError) as error:
        _remember(engine, scope, "different", "same-id")
    assert error.value.spec is IDEMPOTENCY_KEY_REUSED


def test_remember_idempotency_key_reuse_is_stable_and_scoped(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    first = engine.remember(
        RememberRequest(
            content="first idempotent memory",
            scope=scope,
            external_event_id="idempotency-event-one",
            idempotency_key="shared-remember-key",
            origin=OriginContext(producer_id="producer-a"),
        )
    )
    with engine.store.connection() as connection:
        before = {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("ledger_transactions", "events", "memory_claims")
        }

    with pytest.raises(RecallOriginError) as reused:
        engine.remember(
            RememberRequest(
                content="different idempotent memory",
                scope=scope,
                external_event_id="idempotency-event-two",
                idempotency_key="shared-remember-key",
                origin=OriginContext(producer_id="producer-a"),
            )
        )

    assert reused.value.spec is IDEMPOTENCY_KEY_REUSED
    with engine.store.connection() as connection:
        assert {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("ledger_transactions", "events", "memory_claims")
        } == before

    other_producer = engine.remember(
        RememberRequest(
            content="the same key is isolated by producer",
            scope=scope,
            external_event_id="idempotency-event-other-producer",
            idempotency_key="shared-remember-key",
            origin=OriginContext(producer_id="producer-b"),
        )
    )
    other_partition = engine.remember(
        RememberRequest(
            content="the same key is isolated by partition",
            scope=PartitionRef.workspace("beta"),
            external_event_id="idempotency-event-other-partition",
            idempotency_key="shared-remember-key",
            origin=OriginContext(producer_id="producer-a"),
        )
    )

    assert other_producer.claim_id != first.claim_id
    assert other_partition.claim_id not in {first.claim_id, other_producer.claim_id}


def test_governance_uses_revision_compare_and_swap(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    receipt = _remember(engine, scope, "verified fact", "event-1")
    governed = engine.govern(
        GovernRequest(
            claim_id=receipt.claim_id,
            expected_revision_id=receipt.revision_id,
            action=GovernAction.QUARANTINE,
            reason="Needs source review.",
        )
    )
    assert governed.status is MemoryStatus.QUARANTINED

    with pytest.raises(RecallOriginError) as error:
        engine.govern(
            GovernRequest(
                claim_id=receipt.claim_id,
                expected_revision_id=receipt.revision_id,
                action=GovernAction.CONFIRM,
                reason="Stale confirmation.",
            )
        )
    assert error.value.spec is REVISION_CONFLICT


def test_claim_deletion_is_immediately_invisible_then_purged(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    receipt = _remember(engine, scope, "delete this", "event-1")
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=receipt.claim_id,
            ),
            idempotency_key="delete-1",
            expected_revision_id=receipt.revision_id,
        )
    )
    assert deletion.state is DeletionState.LOGICALLY_HIDDEN
    assert engine.search(SearchRequest(query="delete this", scope=scope)) == ()
    with pytest.raises(RecallOriginError) as error:
        engine.get(receipt.claim_id)
    assert error.value.spec is NOT_FOUND

    completed = engine.purge(deletion.deletion_id)
    assert completed.state.value == "completed"


def test_purged_event_cannot_be_replayed(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    receipt = _remember(engine, scope, "source-backed claim", "event-1")
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.EVENT,
                target_id=receipt.event_id,
            ),
            idempotency_key="delete-event-1",
        )
    )
    engine.purge(deletion.deletion_id)

    with pytest.raises(RecallOriginError) as error:
        _remember(engine, scope, "source-backed claim", "event-1")
    assert error.value.spec is IDEMPOTENCY_KEY_REUSED
    assert error.value.details["deleted"] is True


def test_stale_delete_does_not_append_purge_registry(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    receipt = _remember(engine, scope, "keep this", "event-1")
    head_before = engine.store.purge_registry.verify().generation

    with pytest.raises(RecallOriginError) as error:
        engine.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.CLAIM,
                    target_id=receipt.claim_id,
                ),
                idempotency_key="stale-delete",
                expected_revision_id="rev_stale",
            )
        )

    assert error.value.spec is REVISION_CONFLICT
    assert engine.store.purge_registry.verify().generation == head_before


@pytest.mark.parametrize(
    "target_type",
    [
        ForgetTargetType.EVENT,
        ForgetTargetType.SUBJECT,
        ForgetTargetType.PARTITION,
        ForgetTargetType.MANAGED_PACK,
    ],
)
def test_expected_revision_is_rejected_for_non_claim_deletions(
    target_type: ForgetTargetType,
) -> None:
    with pytest.raises(ValidationError, match="only valid for claim deletion"):
        ForgetRequest(
            target=ForgetTarget(
                target_type=target_type,
                target_id="workspace:alpha",
            ),
            scope=PartitionRef.workspace("alpha"),
            idempotency_key=f"invalid-cas-{target_type.value}",
            expected_revision_id="rev_not_applicable",
        )


def test_chinese_and_fts_syntax_are_safe_and_searchable(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    receipt = _remember(engine, scope, "用户偏好中文回答", "event-zh")
    assert engine.search(SearchRequest(query="中文回答", scope=scope))[0].claim_id == (
        receipt.claim_id
    )
    assert engine.search(SearchRequest(query='" OR * NOT (', scope=scope)) == ()


def test_reindex_preserves_search_semantics(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    receipt = _remember(engine, scope, "Release after CI passes.", "event-release")
    before = engine.search(SearchRequest(query="Release", scope=scope))
    result = engine.reindex()
    after = engine.search(SearchRequest(query="Release", scope=scope))
    assert result["indexed"] == 1
    assert [hit.claim_id for hit in before] == [receipt.claim_id]
    assert [hit.claim_id for hit in after] == [receipt.claim_id]
