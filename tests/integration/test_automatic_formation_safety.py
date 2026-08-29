from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import (
    IDEMPOTENCY_KEY_REUSED,
    NOT_FOUND,
    SCOPE_DENIED,
    RecallOriginError,
)
from recall_origin.contracts.v1 import (
    CaptureRequest,
    FeedbackRequest,
    ForgetRequest,
    ForgetTarget,
    FormationCandidate,
    GovernRequest,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
    SubjectRef,
)
from recall_origin.domain.enums import (
    FeedbackType,
    ForgetTargetType,
    FormationOperation,
    GovernAction,
    MemoryKind,
    MemorySubtype,
)
from recall_origin.providers import StructuredEventProvider


def _request(
    scope: PartitionRef,
    *,
    external_event_id: str,
    producer_id: str = "safety-test",
    idempotency_key: str | None = None,
    marker: str = "sensitive formation marker",
    subjects: tuple[SubjectRef, ...] = (),
) -> CaptureRequest:
    return CaptureRequest(
        scope=scope,
        external_event_id=external_event_id,
        idempotency_key=idempotency_key,
        event_type="host_observation",
        origin=OriginContext(producer_id=producer_id),
        subjects=subjects,
        payload={
            "memory_candidates": [
                {
                    "operation": "add",
                    "kind": "semantic",
                    "subtype": "fact",
                    "memory_key": f"safety.{external_event_id}",
                    "content": marker,
                    "reason": "Safety test candidate.",
                }
            ]
        },
    )


def _rows_containing(engine: MemoryEngine, marker: str) -> dict[str, int]:
    queries = {
        "event_payloads": "SELECT count(*) FROM event_payloads WHERE content LIKE ?",
        "evidence_bodies": (
            "SELECT count(*) FROM evidence_bodies WHERE body LIKE ? OR excerpt LIKE ?"
        ),
        "outbox_messages": ("SELECT count(*) FROM outbox_messages WHERE payload_json LIKE ?"),
        "derived_candidates": (
            "SELECT count(*) FROM derived_candidates WHERE content LIKE ? OR reason LIKE ?"
        ),
        "claim_contents": "SELECT count(*) FROM claim_contents WHERE content LIKE ?",
    }
    needle = f"%{marker}%"
    with engine.store.connection() as connection:
        return {
            table: int(
                connection.execute(
                    query,
                    (needle, needle) if query.count("?") == 2 else (needle,),
                ).fetchone()[0]
            )
            for table, query in queries.items()
        }


def _storage_counts(engine: MemoryEngine) -> dict[str, int]:
    tables = (
        "ledger_transactions",
        "events",
        "event_payloads",
        "evidence_bodies",
        "outbox_messages",
        "claim_contents",
        "memory_revisions",
        "memory_feedback",
        "retrieval_runs",
    )
    with engine.store.connection() as connection:
        return {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        }


class _CandidateProvider:
    fingerprint = "safety-provider:v1"

    def __init__(self, callback: Any | None = None) -> None:
        self.callback = callback

    def extract(self, event: object) -> tuple[FormationCandidate, ...]:
        if self.callback is not None:
            self.callback()
        return (
            FormationCandidate(
                operation=FormationOperation.ADD,
                kind=MemoryKind.SEMANTIC,
                subtype=MemorySubtype.FACT,
                memory_key="safety.concurrent",
                content="only one worker may commit this candidate",
                reason="Concurrency safety test.",
            ),
        )


class _FailingProvider:
    fingerprint = "opaque-failure-provider:v1"

    def extract(self, event: object) -> tuple[FormationCandidate, ...]:
        del event
        raise RuntimeError("secret provider traceback must not be persisted")


class _TooManyCandidatesProvider:
    fingerprint = "too-many-candidates:v1"

    def extract(self, event: object) -> tuple[FormationCandidate, ...]:
        del event
        return tuple(
            FormationCandidate(
                operation=FormationOperation.ADD,
                kind=MemoryKind.SEMANTIC,
                subtype=MemorySubtype.FACT,
                memory_key=f"safety.candidate-limit.{index}",
                content=f"bounded candidate {index}",
                reason="Candidate-count boundary test.",
            )
            for index in range(33)
        )


class _OversizedCandidatesProvider:
    fingerprint = "oversized-candidates:v1"

    def extract(self, event: object) -> tuple[FormationCandidate, ...]:
        del event
        return tuple(
            FormationCandidate(
                operation=FormationOperation.ADD,
                kind=MemoryKind.SEMANTIC,
                subtype=MemorySubtype.FACT,
                memory_key=f"safety.candidate-bytes.{index}",
                content=f"{index}-" + ("x" * 600_000),
                reason="Candidate-byte boundary test.",
            )
            for index in range(2)
        )


@pytest.mark.parametrize(
    "provider",
    [_TooManyCandidatesProvider(), _OversizedCandidatesProvider()],
)
def test_provider_output_limits_fail_before_formation_commit(
    engine: MemoryEngine,
    scope: PartitionRef,
    provider: Any,
) -> None:
    captured = engine.capture(_request(scope, external_event_id=f"bounded-{provider.fingerprint}"))

    receipt = engine.process_formation_job(
        provider,
        job_id=captured.job_id,
        max_attempts=2,
    )

    assert receipt is not None
    assert receipt.status == "retry_wait"
    assert receipt.error_code == "PROVIDER_OUTPUT_INVALID"
    with engine.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM formation_runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM derived_candidates").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_claims").fetchone()[0] == 0


def test_capture_outbox_dedupe_is_fixed_length_and_tuple_unambiguous(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    first = engine.capture(
        _request(
            scope,
            external_event_id="c",
            producer_id="a:b",
        )
    )
    second = engine.capture(
        _request(
            scope,
            external_event_id="b:c",
            producer_id="a",
        )
    )
    longest = engine.capture(
        _request(
            scope,
            external_event_id="e" * 512,
            producer_id="p" * 256,
        ),
        formation_version="v" * 128,
    )

    assert len({first.job_id, second.job_id, longest.job_id}) == 3
    with engine.store.connection() as connection:
        keys = [
            str(row["dedupe_key"])
            for row in connection.execute(
                "SELECT dedupe_key FROM outbox_messages ORDER BY outbox_id"
            )
        ]
    assert len(keys) == 3
    assert len(set(keys)) == 3
    assert all(len(key) < 128 for key in keys)


def test_expired_lease_is_fenced_when_another_worker_takes_over(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    now = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]
    database = tmp_path / "multi-worker.sqlite3"
    first = MemoryEngine.local(database, clock=lambda: now[0]).initialize()
    second = MemoryEngine.local(database, clock=lambda: now[0]).initialize()
    captured = first.capture(_request(scope, external_event_id="lease-takeover"))
    takeover_receipt = None

    def take_over() -> None:
        nonlocal takeover_receipt
        now[0] += timedelta(seconds=2)
        takeover_receipt = second.process_formation_job(
            _CandidateProvider(),
            job_id=captured.job_id,
            worker_id="worker-b",
            lease_seconds=1,
        )

    stale_receipt = first.process_formation_job(
        _CandidateProvider(take_over),
        job_id=captured.job_id,
        worker_id="worker-a",
        lease_seconds=1,
    )

    assert takeover_receipt is not None and takeover_receipt.status == "done"
    assert stale_receipt is not None
    assert stale_receipt.status == "cancelled"
    assert stale_receipt.error_code == "STALE_LEASE"
    with first.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM formation_runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM derived_candidates").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM memory_claims").fetchone()[0] == 1


@pytest.mark.parametrize("target", ["event", "partition"])
def test_deletion_after_provider_return_wins_before_worker_commit(
    engine: MemoryEngine,
    scope: PartitionRef,
    target: str,
) -> None:
    captured = engine.capture(_request(scope, external_event_id=f"race-{target}"))

    def delete_after_extract() -> None:
        forget_target = (
            ForgetTarget(
                target_type=ForgetTargetType.EVENT,
                target_id=captured.event_id or "",
            )
            if target == "event"
            else ForgetTarget(
                target_type=ForgetTargetType.PARTITION,
                target_id=scope.serialize(),
            )
        )
        engine.forget(
            ForgetRequest(
                target=forget_target,
                scope=scope if target == "partition" else None,
                idempotency_key=f"race-delete-{target}",
                cascade_policy="purge",
            )
        )

    receipt = engine.process_formation_job(
        _CandidateProvider(delete_after_extract),
        job_id=captured.job_id,
    )

    assert receipt is not None and receipt.status == "cancelled"
    with engine.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM formation_runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM derived_candidates").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_claims").fetchone()[0] == 0


def test_retry_backoff_and_dead_letter_are_bounded_and_opaque(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    now = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]
    engine = MemoryEngine.local(
        tmp_path / "retry.sqlite3",
        clock=lambda: now[0],
    ).initialize()
    captured = engine.capture(_request(scope, external_event_id="retry-dlq"))

    first = engine.process_formation_job(
        _FailingProvider(),
        job_id=captured.job_id,
        max_attempts=2,
    )
    assert first is not None and first.status == "retry_wait"
    assert (
        engine.process_formation_job(
            _FailingProvider(),
            job_id=captured.job_id,
            max_attempts=2,
        )
        is None
    )

    now[0] += timedelta(seconds=1)
    second = engine.process_formation_job(
        _FailingProvider(),
        job_id=captured.job_id,
        max_attempts=2,
    )
    assert second is not None and second.status == "dead_letter"
    assert second.attempt == 2
    assert second.error_code == "PROVIDER_OUTPUT_INVALID"

    with engine.store.connection() as connection:
        row = connection.execute(
            """
            SELECT status, attempt, last_error_code, completed_at
            FROM outbox_messages WHERE outbox_id = ?
            """,
            (captured.job_id,),
        ).fetchone()
        assert tuple(row) == (
            "dead_letter",
            2,
            "PROVIDER_OUTPUT_INVALID",
            int(now[0].timestamp() * 1_000_000),
        )
        persisted = " ".join(
            str(value)
            for value in connection.execute(
                """
                SELECT coalesce(last_error_code, ''), coalesce(dedupe_key, '')
                FROM outbox_messages WHERE outbox_id = ?
                """,
                (captured.job_id,),
            ).fetchone()
        )
        assert "secret provider traceback" not in persisted


@pytest.mark.parametrize("formed", [False, True])
def test_event_purge_scrubs_pending_and_derived_text(
    engine: MemoryEngine,
    scope: PartitionRef,
    formed: bool,
) -> None:
    marker = f"event purge marker formed={formed}"
    captured = engine.capture(
        _request(scope, external_event_id=f"event-purge-{formed}", marker=marker)
    )
    if formed:
        engine.process_formation_job(
            StructuredEventProvider(),
            job_id=captured.job_id,
        )
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.EVENT,
                target_id=captured.event_id or "",
            ),
            idempotency_key=f"delete-event-{formed}",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)

    assert _rows_containing(engine, marker) == {
        "event_payloads": 0,
        "evidence_bodies": 0,
        "outbox_messages": 0,
        "derived_candidates": 0,
        "claim_contents": 0,
    }


def test_partition_purge_scrubs_pending_and_derived_text(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    pending_marker = "partition pending secret"
    derived_marker = "partition derived secret"
    engine.capture(_request(scope, external_event_id="partition-pending", marker=pending_marker))
    formed = engine.capture(
        _request(scope, external_event_id="partition-derived", marker=derived_marker)
    )
    engine.process_formation_job(StructuredEventProvider(), job_id=formed.job_id)
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.PARTITION,
                target_id=scope.serialize(),
            ),
            scope=scope,
            idempotency_key="delete-partition",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)

    for marker in (pending_marker, derived_marker):
        rows = _rows_containing(engine, marker)
        assert rows == dict.fromkeys(rows, 0)


def test_subject_purge_scrubs_its_derived_text(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    marker = "subject scoped private marker"
    subject = SubjectRef(subject_type="user", subject_id="subject-42")
    captured = engine.capture(
        _request(
            scope,
            external_event_id="subject-derived",
            marker=marker,
            subjects=(subject,),
        )
    )
    engine.process_formation_job(StructuredEventProvider(), job_id=captured.job_id)
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.SUBJECT,
                target_id=subject.subject_id,
            ),
            scope=scope,
            idempotency_key="delete-subject",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)

    rows = _rows_containing(engine, marker)
    assert rows == dict.fromkeys(rows, 0)


def test_capture_idempotency_identity_combinations(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    original = engine.capture(
        _request(
            scope,
            external_event_id="external-1",
            idempotency_key="idempotency-1",
        )
    )
    replay = engine.capture(
        _request(
            scope,
            external_event_id="external-1",
            idempotency_key="idempotency-1",
        )
    )
    assert replay.replayed is True
    assert (replay.event_id, replay.job_id) == (original.event_id, original.job_id)

    with pytest.raises(RecallOriginError) as reused_external:
        engine.capture(
            _request(
                scope,
                external_event_id="external-1",
                idempotency_key="idempotency-2",
            )
        )
    assert reused_external.value.spec is IDEMPOTENCY_KEY_REUSED

    with pytest.raises(RecallOriginError) as reused_key:
        engine.capture(
            _request(
                scope,
                external_event_id="external-2",
                idempotency_key="idempotency-1",
            )
        )
    assert reused_key.value.spec is IDEMPOTENCY_KEY_REUSED

    other_producer = engine.capture(
        _request(
            scope,
            external_event_id="external-1",
            producer_id="other-producer",
            idempotency_key="idempotency-1",
        )
    )
    assert other_producer.event_id != original.event_id

    other_partition = engine.capture(
        _request(
            PartitionRef.workspace("beta"),
            external_event_id="external-1",
            idempotency_key="idempotency-1",
        )
    )
    assert other_partition.event_id not in {original.event_id, other_producer.event_id}


def test_pending_subject_capture_can_be_forgotten_and_fully_purged(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    marker = "pending subject must be physically erased"
    subject = SubjectRef(subject_type="user", subject_id="pending-subject")
    captured = engine.capture(
        _request(
            scope,
            external_event_id="pending-subject-event",
            marker=marker,
            subjects=(subject,),
        )
    )

    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.SUBJECT,
                target_id=subject.subject_id,
            ),
            scope=scope,
            idempotency_key="delete-pending-subject",
            cascade_policy="purge",
        )
    )
    with engine.store.connection() as connection:
        job = connection.execute(
            "SELECT status, last_error_code FROM outbox_messages WHERE outbox_id = ?",
            (captured.job_id,),
        ).fetchone()
        assert tuple(job) == ("cancelled", "DELETION_FENCE")

    engine.purge(deletion.deletion_id)

    assert _rows_containing(engine, marker) == {
        "event_payloads": 0,
        "evidence_bodies": 0,
        "outbox_messages": 0,
        "derived_candidates": 0,
        "claim_contents": 0,
    }
    assert (
        engine.process_formation_job(
            StructuredEventProvider(),
            job_id=captured.job_id,
        )
        is None
    )


def test_partition_deletion_permanently_retires_persistent_write_scope(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    seed = engine.capture(
        _request(
            scope,
            external_event_id="partition-retirement-seed",
            marker="partition retirement seed",
        )
    )
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.PARTITION,
                target_id=scope.serialize(),
            ),
            scope=scope,
            idempotency_key="retire-partition",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)
    before = _storage_counts(engine)

    with pytest.raises(RecallOriginError) as remember_denied:
        engine.remember(
            RememberRequest(
                content="partition retirement must reject new explicit content",
                scope=scope,
                external_event_id="partition-retired-remember",
                idempotency_key="partition-retired-remember",
                origin=OriginContext(producer_id="safety-test"),
            )
        )
    with pytest.raises(RecallOriginError) as capture_denied:
        engine.capture(
            _request(
                scope,
                external_event_id="partition-retired-capture",
                idempotency_key="partition-retired-capture",
                marker="partition retirement must reject new captured content",
            )
        )

    assert seed.job_id is not None
    assert remember_denied.value.spec is SCOPE_DENIED
    assert capture_denied.value.spec is SCOPE_DENIED
    no_store = engine.capture(
        _request(
            scope,
            external_event_id="partition-retired-no-store",
            marker="no-store content is never persisted",
        ).model_copy(update={"persist": False})
    )
    assert no_store.status == "no_store"
    assert no_store.event_id is None
    assert no_store.job_id is None
    search = engine.search_result(SearchRequest(query="partition retirement", scope=scope))
    assert search.items == ()
    assert search.retrieval_id is None
    assert _storage_counts(engine) == before


def test_subject_deletion_retires_only_matching_subject_writes(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    retired = SubjectRef(subject_type="user", subject_id="retired-subject")
    survivor = SubjectRef(subject_type="user", subject_id="surviving-subject")
    engine.remember(
        RememberRequest(
            content="subject retirement seed",
            scope=scope,
            subject=retired,
            external_event_id="subject-retirement-seed",
            idempotency_key="subject-retirement-seed",
            origin=OriginContext(producer_id="safety-test"),
        )
    )
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.SUBJECT,
                target_id=retired.subject_id,
            ),
            scope=scope,
            idempotency_key="retire-subject",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)
    before = _storage_counts(engine)

    with pytest.raises(RecallOriginError) as remember_denied:
        engine.remember(
            RememberRequest(
                content="retired subject explicit content",
                scope=scope,
                subject=retired,
                external_event_id="retired-subject-remember",
                idempotency_key="retired-subject-remember",
                origin=OriginContext(producer_id="safety-test"),
            )
        )
    with pytest.raises(RecallOriginError) as capture_denied:
        engine.capture(
            _request(
                scope,
                external_event_id="retired-subject-capture",
                idempotency_key="retired-subject-capture",
                marker="retired subject captured content",
                subjects=(retired,),
            )
        )

    assert remember_denied.value.spec is SCOPE_DENIED
    assert capture_denied.value.spec is SCOPE_DENIED
    assert _storage_counts(engine) == before

    remembered = engine.remember(
        RememberRequest(
            content="another subject remains writable",
            scope=scope,
            subject=survivor,
            external_event_id="surviving-subject-remember",
            idempotency_key="surviving-subject-remember",
            origin=OriginContext(producer_id="safety-test"),
        )
    )
    captured = engine.capture(
        _request(
            scope,
            external_event_id="surviving-subject-capture",
            idempotency_key="surviving-subject-capture",
            marker="surviving subject captured content",
            subjects=(survivor,),
        )
    )

    assert remembered.claim_id
    assert captured.job_id


@pytest.mark.parametrize(
    "target_type",
    [
        ForgetTargetType.CLAIM,
        ForgetTargetType.PARTITION,
        ForgetTargetType.SUBJECT,
    ],
)
def test_retired_claim_scope_rejects_governance_and_feedback_without_writes(
    engine: MemoryEngine,
    scope: PartitionRef,
    target_type: ForgetTargetType,
) -> None:
    subject = SubjectRef(subject_type="user", subject_id=f"{target_type.value}-subject")
    remembered = engine.remember(
        RememberRequest(
            content=f"{target_type.value} governance retirement seed",
            scope=scope,
            subject=subject,
            external_event_id=f"{target_type.value}-governance-retirement",
            idempotency_key=f"{target_type.value}-governance-retirement",
            origin=OriginContext(producer_id="safety-test"),
        )
    )
    target_ids = {
        ForgetTargetType.CLAIM: remembered.claim_id,
        ForgetTargetType.PARTITION: scope.serialize(),
        ForgetTargetType.SUBJECT: subject.subject_id,
    }
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=target_type,
                target_id=target_ids[target_type],
            ),
            scope=(
                scope
                if target_type in {ForgetTargetType.PARTITION, ForgetTargetType.SUBJECT}
                else None
            ),
            idempotency_key=f"retire-{target_type.value}-governance",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)
    before = _storage_counts(engine)

    with pytest.raises(RecallOriginError) as govern_denied:
        engine.govern(
            GovernRequest(
                claim_id=remembered.claim_id,
                expected_revision_id=remembered.revision_id,
                action=GovernAction.QUARANTINE,
                reason="must not be persisted after scope retirement",
            )
        )
    with pytest.raises(RecallOriginError) as feedback_denied:
        engine.feedback(
            FeedbackRequest(
                claim_id=remembered.claim_id,
                revision_id=remembered.revision_id,
                feedback_type=FeedbackType.INCORRECT,
                reason="must not be persisted after scope retirement",
            )
        )

    assert govern_denied.value.spec is NOT_FOUND
    assert feedback_denied.value.spec is NOT_FOUND
    assert _storage_counts(engine) == before


def test_event_deletion_does_not_retire_a_claim_with_live_evidence(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    first = engine.remember(
        RememberRequest(
            content="event deletion leaves this claim governable",
            scope=scope,
            memory_key="safety.event-deletion-governance",
            external_event_id="event-deletion-governance-one",
            origin=OriginContext(producer_id="safety-test"),
        )
    )
    second = engine.remember(
        RememberRequest(
            content="event deletion leaves this claim governable",
            scope=scope,
            memory_key="safety.event-deletion-governance",
            external_event_id="event-deletion-governance-two",
            origin=OriginContext(producer_id="safety-test"),
        )
    )
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.EVENT,
                target_id=first.event_id,
            ),
            idempotency_key="delete-one-live-evidence-source",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)
    current = engine.get(second.claim_id)

    governed = engine.govern(
        GovernRequest(
            claim_id=current.claim_id,
            expected_revision_id=current.revision_id,
            action=GovernAction.QUARANTINE,
            reason="the surviving source can still be governed",
        )
    )
    feedback = engine.feedback(
        FeedbackRequest(
            claim_id=current.claim_id,
            revision_id=governed.revision_id,
            feedback_type=FeedbackType.INCORRECT,
            reason="the surviving source can still receive feedback",
        )
    )

    assert governed.claim_id == second.claim_id
    assert feedback.claim_id == second.claim_id


def test_v5_to_v6_backfills_formed_and_pending_event_subject_lineage(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "upgrade-v5.sqlite3"
    before_upgrade = MemoryEngine.local(database).initialize()
    formed_marker = "v5 formed subject marker"
    pending_marker = "v5 pending subject marker"
    formed_subject = SubjectRef(subject_type="user", subject_id="formed-v5")
    pending_subject = SubjectRef(subject_type="user", subject_id="pending-v5")
    formed = before_upgrade.capture(
        _request(
            scope,
            external_event_id="v5-formed-event",
            marker=formed_marker,
            subjects=(formed_subject,),
        )
    )
    formed_job = before_upgrade.process_formation_job(
        StructuredEventProvider(),
        job_id=formed.job_id,
    )
    assert formed_job is not None and formed_job.status == "done"
    pending = before_upgrade.capture(
        _request(
            scope,
            external_event_id="v5-pending-event",
            marker=pending_marker,
            subjects=(pending_subject,),
        )
    )

    # Recreate the exact schema boundary presented to migration 0006.  The
    # formed event retains v5 claim_subject/evidence lineage; the pending event
    # has only its strict CaptureRequest JSON.
    with before_upgrade.store.transaction() as connection:
        connection.execute("DROP TABLE event_subjects")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.execute("PRAGMA user_version = 5")
        connection.execute("UPDATE storage_meta SET value = '5' WHERE key = 'schema_version'")
        connection.execute(
            """
            DELETE FROM subjects
            WHERE partition_id = (
                SELECT partition_id FROM events WHERE event_id = ?
            )
              AND subject_type = ?
              AND subject_id = ?
            """,
            (
                pending.event_id,
                pending_subject.subject_type,
                pending_subject.subject_id,
            ),
        )

    upgraded = MemoryEngine.local(database).initialize()
    assert upgraded.store.schema_version >= 6
    with upgraded.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM schema_migrations WHERE version = 6"
            ).fetchone()[0]
            == 1
        )
        lineage = {
            (str(row["event_id"]), str(row["subject_id"]))
            for row in connection.execute(
                """
                SELECT event_id, subject_id
                FROM event_subjects
                WHERE event_id IN (?, ?)
                """,
                (formed.event_id, pending.event_id),
            )
        }
    assert lineage == {
        (formed.event_id, formed_subject.subject_id),
        (pending.event_id, pending_subject.subject_id),
    }

    formed_deletion = upgraded.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.SUBJECT,
                target_id=formed_subject.subject_id,
            ),
            scope=scope,
            idempotency_key="delete-upgraded-formed-subject",
            cascade_policy="purge",
        )
    )
    pending_deletion = upgraded.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.SUBJECT,
                target_id=pending_subject.subject_id,
            ),
            scope=scope,
            idempotency_key="delete-upgraded-pending-subject",
            cascade_policy="purge",
        )
    )
    with upgraded.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT status FROM outbox_messages WHERE outbox_id = ?",
                (pending.job_id,),
            ).fetchone()[0]
            == "cancelled"
        )

    upgraded.purge(formed_deletion.deletion_id)
    upgraded.purge(pending_deletion.deletion_id)

    for marker in (formed_marker, pending_marker):
        rows = _rows_containing(upgraded, marker)
        assert rows == dict.fromkeys(rows, 0)
