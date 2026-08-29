from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import NOT_FOUND, RecallOriginError
from recall_origin.contracts.v1 import (
    GovernRequest,
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberRequest,
    SearchRequest,
)
from recall_origin.domain.enums import (
    Capability,
    Confirmation,
    GovernAction,
    MemoryKind,
    MemoryStatus,
    MemorySubtype,
    PrincipalType,
)


def _agent(database: Path, scope: PartitionRef) -> MemoryEngine:
    return MemoryEngine.local(
        database,
        principal=PrincipalContext(
            tenant_id="local",
            principal_id="agent-writer",
            principal_type=PrincipalType.AGENT,
            capabilities=frozenset({Capability.READ, Capability.WRITE}),
        ),
        allowed_partitions=[scope],
    ).initialize()


def _remember(
    engine: MemoryEngine,
    scope: PartitionRef,
    content: str,
    event_id: str,
    *,
    kind: MemoryKind = MemoryKind.SEMANTIC,
    subtype: MemorySubtype = MemorySubtype.FACT,
    valid_from: datetime | None = None,
) -> object:
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            kind=kind,
            subtype=subtype,
            memory_key="workspace.package_manager",
            external_event_id=event_id,
            origin=OriginContext(producer_id="trust-transition-test"),
            valid_from=valid_from,
        )
    )


def test_agent_proposal_requires_human_confirmation_before_superseding(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    scope = PartitionRef.workspace("alpha")
    human = MemoryEngine.local(database).initialize()
    agent = _agent(database, scope)
    confirmed = _remember(human, scope, "Use pnpm.", "human-pnpm")

    proposal = _remember(agent, scope, "Use npm.", "agent-npm")

    assert proposal.claim_id != confirmed.claim_id
    assert proposal.status is MemoryStatus.CANDIDATE
    assert proposal.confirmation is Confirmation.UNVERIFIED
    assert [hit.claim_id for hit in human.search(SearchRequest(query="Use", scope=scope))] == [
        confirmed.claim_id
    ]
    candidate_ids = {
        hit.claim_id
        for hit in human.search(SearchRequest(query="Use", scope=scope, include_candidates=True))
    }
    assert candidate_ids == {confirmed.claim_id, proposal.claim_id}
    assert human.get(confirmed.claim_id).status is MemoryStatus.ACTIVE

    governed = human.govern(
        GovernRequest(
            claim_id=proposal.claim_id,
            expected_revision_id=proposal.revision_id,
            action=GovernAction.CONFIRM,
            reason="The local operator accepted this replacement.",
        )
    )

    assert governed.status is MemoryStatus.ACTIVE
    assert governed.confirmation is Confirmation.USER_CONFIRMED
    with pytest.raises(RecallOriginError) as old:
        human.get(confirmed.claim_id)
    assert old.value.spec is NOT_FOUND
    assert [hit.claim_id for hit in human.search(SearchRequest(query="Use", scope=scope))] == [
        proposal.claim_id
    ]


def test_agent_same_content_does_not_inherit_human_confirmation(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    scope = PartitionRef.workspace("alpha")
    human = MemoryEngine.local(database).initialize()
    agent = _agent(database, scope)
    confirmed = _remember(human, scope, "Use pnpm.", "human-pnpm")

    proposal = _remember(agent, scope, "USE PNPM.", "agent-pnpm")

    assert proposal.claim_id != confirmed.claim_id
    assert proposal.status is MemoryStatus.CANDIDATE
    assert proposal.confirmation is Confirmation.UNVERIFIED
    assert human.get(confirmed.claim_id).status is MemoryStatus.ACTIVE
    assert human.get(confirmed.claim_id).confirmation is Confirmation.USER_CONFIRMED


def test_agent_candidate_reinforcement_requires_complete_claim_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    scope = PartitionRef.workspace("alpha")
    agent = _agent(database, scope)
    original = _remember(agent, scope, "Use pnpm.", "agent-original")

    incompatible = _remember(
        agent,
        scope,
        "USE PNPM.",
        "agent-incompatible",
        kind=MemoryKind.PROCEDURAL,
        subtype=MemorySubtype.WORKFLOW,
        valid_from=datetime(2030, 1, 1, tzinfo=UTC),
    )
    reinforced = _remember(agent, scope, "USE PNPM.", "agent-compatible")

    assert incompatible.claim_id != original.claim_id
    assert incompatible.status is MemoryStatus.CANDIDATE
    assert incompatible.confirmation is Confirmation.UNVERIFIED
    assert reinforced.claim_id == original.claim_id
    assert reinforced.source_count == 2
    assert reinforced.status is MemoryStatus.CANDIDATE
    assert reinforced.confirmation is Confirmation.UNVERIFIED
