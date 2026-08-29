from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import NOT_FOUND, SCOPE_DENIED, RecallOriginError
from recall_origin.contracts.v1 import (
    CaptureRequest,
    ContextQuery,
    FeedbackRequest,
    ForgetRequest,
    ForgetTarget,
    GovernRequest,
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberRequest,
    SearchRequest,
    SubjectRef,
)
from recall_origin.domain.enums import (
    Capability,
    FeedbackType,
    ForgetTargetType,
    GovernAction,
    PrincipalType,
)
from recall_origin.providers import StructuredEventProvider


def _principal(
    tenant_id: str,
    capabilities: frozenset[Capability],
) -> PrincipalContext:
    return PrincipalContext(
        tenant_id=tenant_id,
        principal_id=f"{tenant_id}-principal",
        principal_type=PrincipalType.HUMAN,
        capabilities=capabilities,
    )


def _engine(
    database: Path,
    pack_root: Path,
    *,
    tenant_id: str,
    capabilities: frozenset[Capability],
    allowed_partitions: list[PartitionRef],
) -> MemoryEngine:
    return MemoryEngine.local(
        database,
        managed_pack_root=pack_root,
        principal=_principal(tenant_id, capabilities),
        allowed_partitions=allowed_partitions,
    ).initialize()


def _remember(engine: MemoryEngine, scope: PartitionRef, suffix: str = "primary") -> Any:
    return engine.remember(
        RememberRequest(
            content=f"tenant isolated memory {suffix}",
            scope=scope,
            external_event_id=f"tenant-event-{suffix}",
            origin=OriginContext(producer_id="security-boundary-test"),
        )
    )


def _capture(engine: MemoryEngine, scope: PartitionRef, suffix: str) -> Any:
    return engine.capture(
        CaptureRequest(
            scope=scope,
            external_event_id=f"formation-{suffix}",
            event_type="host_observation",
            origin=OriginContext(producer_id="security-boundary-test"),
            payload={
                "memory_candidates": [
                    {
                        "operation": "add",
                        "kind": "semantic",
                        "content": f"formed memory {suffix}",
                        "reason": "Formation authorization regression.",
                    }
                ]
            },
        )
    )


class _CountingProvider(StructuredEventProvider):
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, event: Any) -> Any:
        self.calls += 1
        return super().extract(event)


def test_formation_worker_without_write_capability_cannot_lease_or_read_payload(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("alpha")
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "packs"
    owner = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(Capability),
        allowed_partitions=[scope],
    )
    captured = _capture(owner, scope, "no-capability")
    worker = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(),
        allowed_partitions=[scope],
    )
    provider = _CountingProvider()

    assert worker.process_formation_job(provider, job_id=captured.job_id) is None
    assert provider.calls == 0
    status = owner.formation_job_status(captured.job_id or "")
    assert status.status == "pending"
    assert status.attempt == 0


def test_formation_worker_only_leases_an_exact_authorized_partition(
    tmp_path: Path,
) -> None:
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "packs"
    counters: dict[str, int] = {}

    def ids(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]:08d}"

    owner = MemoryEngine.local(
        database,
        managed_pack_root=pack_root,
        principal=_principal("tenant-a", frozenset(Capability)),
        allowed_partitions=[alpha, beta],
        id_factory=ids,
    ).initialize()
    beta_job = _capture(owner, beta, "beta-first")
    alpha_job = _capture(owner, alpha, "alpha-second")
    worker = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset({Capability.WRITE}),
        allowed_partitions=[alpha],
    )

    processed = worker.process_formation_job(StructuredEventProvider())

    assert processed is not None
    assert processed.job_id == alpha_job.job_id
    assert owner.formation_job_status(beta_job.job_id or "").status == "pending"


def test_formation_worker_cannot_cross_tenants_with_the_same_scope_name(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("shared")
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "packs"
    owner = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(Capability),
        allowed_partitions=[scope],
    )
    captured = _capture(owner, scope, "tenant-a")
    other_tenant = _engine(
        database,
        pack_root,
        tenant_id="tenant-b",
        capabilities=frozenset({Capability.WRITE}),
        allowed_partitions=[scope],
    )
    provider = _CountingProvider()

    assert other_tenant.process_formation_job(provider, job_id=captured.job_id) is None
    assert provider.calls == 0
    assert owner.formation_job_status(captured.job_id or "").attempt == 0


@pytest.mark.parametrize(
    "operation",
    [
        "get",
        "govern",
        "feedback",
        "formation_status",
        "retrieval_trace",
        "pack_resource",
        "pack_export",
        "forget",
    ],
)
def test_object_ids_never_cross_tenants_with_the_same_scope_name(
    tmp_path: Path,
    operation: str,
) -> None:
    scope = PartitionRef.workspace("shared")
    database = tmp_path / f"{operation}.sqlite3"
    pack_root = tmp_path / f"{operation}-packs"
    owner = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(Capability),
        allowed_partitions=[scope],
    )
    remembered = _remember(owner, scope)
    retrieval = owner.search_result(SearchRequest(query="tenant isolated", scope=scope))
    pack = owner.evidence_context(
        ContextQuery(query="tenant isolated", scope=scope, token_budget=800)
    )
    captured = _capture(owner, scope, "status")
    attacker = _engine(
        database,
        pack_root,
        tenant_id="tenant-b",
        capabilities=frozenset(Capability),
        allowed_partitions=[scope],
    )
    actions: dict[str, Callable[[], object]] = {
        "get": lambda: attacker.get(remembered.claim_id),
        "govern": lambda: attacker.govern(
            GovernRequest(
                claim_id=remembered.claim_id,
                expected_revision_id=remembered.revision_id,
                action=GovernAction.QUARANTINE,
                reason="Cross-tenant request must be hidden.",
            )
        ),
        "feedback": lambda: attacker.feedback(
            FeedbackRequest(
                claim_id=remembered.claim_id,
                revision_id=remembered.revision_id,
                feedback_type=FeedbackType.INCORRECT,
            )
        ),
        "formation_status": lambda: attacker.formation_job_status(captured.job_id or ""),
        "retrieval_trace": lambda: attacker.retrieval_trace(retrieval.retrieval_id or ""),
        "pack_resource": lambda: attacker.read_evidence_pack_resource(
            pack.pack_id, "manifest.json"
        ),
        "pack_export": lambda: attacker.export_evidence_pack(
            pack.pack_id, tmp_path / f"{operation}-export"
        ),
        "forget": lambda: attacker.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.CLAIM,
                    target_id=remembered.claim_id,
                ),
                idempotency_key="cross-tenant-forget",
                expected_revision_id=remembered.revision_id,
            )
        ),
    }

    with pytest.raises(RecallOriginError) as denied:
        actions[operation]()
    assert denied.value.spec is NOT_FOUND


@pytest.mark.parametrize("operation", ["status", "purge"])
def test_deletion_ids_never_cross_tenants(
    tmp_path: Path,
    operation: str,
) -> None:
    scope = PartitionRef.workspace("shared")
    database = tmp_path / f"deletion-{operation}.sqlite3"
    pack_root = tmp_path / f"deletion-{operation}-packs"
    owner = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(Capability),
        allowed_partitions=[scope],
    )
    remembered = _remember(owner, scope, suffix=operation)
    deletion = owner.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=remembered.claim_id,
            ),
            idempotency_key=f"tenant-a-delete-{operation}",
            expected_revision_id=remembered.revision_id,
        )
    )
    attacker = _engine(
        database,
        pack_root,
        tenant_id="tenant-b",
        capabilities=frozenset(Capability),
        allowed_partitions=[scope],
    )

    with pytest.raises(RecallOriginError) as denied:
        if operation == "status":
            attacker.deletion_status(deletion.deletion_id)
        else:
            attacker.purge(deletion.deletion_id)
    assert denied.value.spec is NOT_FOUND


@pytest.mark.parametrize(
    "operation",
    ["pack_export", "forget_claim", "forget_event", "forget_pack"],
)
def test_opaque_ids_do_not_reveal_an_unallowed_same_tenant_partition(
    tmp_path: Path,
    operation: str,
) -> None:
    alpha = PartitionRef.workspace("secret-alpha")
    beta = PartitionRef.workspace("allowed-beta")
    database = tmp_path / f"{operation}.sqlite3"
    pack_root = tmp_path / f"{operation}-packs"
    owner = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(Capability),
        allowed_partitions=[alpha],
    )
    remembered = _remember(owner, alpha, suffix=operation)
    pack = owner.evidence_context(
        ContextQuery(query="tenant isolated", scope=alpha, token_budget=800)
    )
    attacker = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(Capability),
        allowed_partitions=[beta],
    )
    actions: dict[str, Callable[[], object]] = {
        "pack_export": lambda: attacker.export_evidence_pack(
            pack.pack_id,
            tmp_path / "unauthorized-export",
        ),
        "forget_claim": lambda: attacker.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.CLAIM,
                    target_id=remembered.claim_id,
                ),
                idempotency_key="unallowed-partition-claim",
                expected_revision_id=remembered.revision_id,
            )
        ),
        "forget_event": lambda: attacker.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.EVENT,
                    target_id=remembered.event_id,
                ),
                idempotency_key="unallowed-partition-event",
            )
        ),
        "forget_pack": lambda: attacker.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.MANAGED_PACK,
                    target_id=pack.pack_id,
                ),
                idempotency_key="unallowed-partition-pack",
            )
        ),
    }

    with pytest.raises(RecallOriginError) as denied:
        actions[operation]()

    assert denied.value.spec is NOT_FOUND
    assert denied.value.details == {}


def test_export_requires_export_capability_in_addition_to_read(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("alpha")
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "packs"
    owner = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset(Capability),
        allowed_partitions=[scope],
    )
    _remember(owner, scope)
    pack = owner.evidence_context(
        ContextQuery(query="tenant isolated", scope=scope, token_budget=800)
    )
    reader = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset({Capability.READ}),
        allowed_partitions=[scope],
    )

    assert reader.read_evidence_pack_resource(pack.pack_id, "manifest.json")
    with pytest.raises(RecallOriginError) as denied:
        reader.export_evidence_pack(pack.pack_id, tmp_path / "denied-export")
    assert denied.value.spec is SCOPE_DENIED

    exporter = _engine(
        database,
        pack_root,
        tenant_id="tenant-a",
        capabilities=frozenset({Capability.READ, Capability.EXPORT}),
        allowed_partitions=[scope],
    )
    assert exporter.export_evidence_pack(pack.pack_id, tmp_path / "allowed-export").is_dir()


def test_partition_forget_rejects_mismatched_target_and_scope_without_writes(
    engine: MemoryEngine,
) -> None:
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    alpha_memory = _remember(engine, alpha, suffix="partition-target-alpha")
    beta_memory = _remember(engine, beta, suffix="partition-target-beta")
    registry_generation = engine.store.purge_registry.verify().generation
    with engine.store.connection() as connection:
        deletion_count = int(
            connection.execute("SELECT count(*) FROM deletion_requests").fetchone()[0]
        )
        fence_count = int(connection.execute("SELECT count(*) FROM tombstone_fences").fetchone()[0])

    with pytest.raises(RecallOriginError) as denied:
        engine.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.PARTITION,
                    target_id=beta.serialize(),
                ),
                scope=alpha,
                idempotency_key="mismatched-partition-target-and-scope",
            )
        )

    assert denied.value.spec is NOT_FOUND
    assert engine.get(alpha_memory.claim_id).claim_id == alpha_memory.claim_id
    assert engine.get(beta_memory.claim_id).claim_id == beta_memory.claim_id
    assert engine.store.purge_registry.verify().generation == registry_generation
    with engine.store.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM deletion_requests").fetchone()[0]
            == deletion_count
        )
        assert (
            connection.execute("SELECT count(*) FROM tombstone_fences").fetchone()[0] == fence_count
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PrincipalContext(
            tenant_id="tenant\x00split",
            principal_id="principal",
            principal_type=PrincipalType.HUMAN,
        ),
        lambda: PartitionRef.workspace("scope\x00split"),
        lambda: SubjectRef(subject_type="user", subject_id="subject\x00split"),
        lambda: OriginContext(producer_id="producer\x00split"),
    ],
)
def test_identity_models_reject_control_character_ambiguity(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        factory()
