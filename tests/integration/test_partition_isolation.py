from __future__ import annotations

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import NOT_FOUND, RecallOriginError
from recall_origin.contracts.v1 import (
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberRequest,
    SearchRequest,
)
from recall_origin.domain.enums import Capability, PrincipalType


def test_exact_partition_authorization_prevents_cross_scope_reads(tmp_path) -> None:
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    owner = MemoryEngine.local(tmp_path / "memory.sqlite3").initialize()
    alpha_memory = owner.remember(
        RememberRequest(
            content="alpha needle",
            scope=alpha,
            external_event_id="alpha-1",
            origin=OriginContext(producer_id="owner"),
        )
    )
    beta_memory = owner.remember(
        RememberRequest(
            content="ultraviolet-badger-4821",
            scope=beta,
            external_event_id="beta-1",
            origin=OriginContext(producer_id="owner"),
        )
    )
    alpha_principal = PrincipalContext(
        tenant_id="local",
        principal_id="alpha-agent",
        principal_type=PrincipalType.AGENT,
        capabilities=frozenset({Capability.READ}),
    )
    reader = MemoryEngine.local(
        tmp_path / "memory.sqlite3",
        principal=alpha_principal,
        allowed_partitions=[alpha],
    ).initialize()

    assert reader.search(SearchRequest(query="alpha", scope=alpha))[0].claim_id == (
        alpha_memory.claim_id
    )
    with pytest.raises(RecallOriginError):
        reader.search(SearchRequest(query="ultraviolet", scope=beta))
    with pytest.raises(RecallOriginError) as denied:
        reader.get(beta_memory.claim_id)
    assert denied.value.spec is NOT_FOUND
