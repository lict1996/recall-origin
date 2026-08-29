from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import NOT_FOUND, RecallOriginError
from recall_origin.contracts.v1 import (
    ContextQuery,
    ForgetRequest,
    ForgetTarget,
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberRequest,
    SearchRequest,
)
from recall_origin.domain.enums import Capability, ForgetTargetType, PrincipalType
from recall_origin.retrieval.vector import VectorCandidate


def _remember(
    engine: MemoryEngine,
    scope: PartitionRef,
    content: str,
    external_event_id: str,
) -> object:
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            external_event_id=external_event_id,
            origin=OriginContext(producer_id="hybrid-test"),
        )
    )


class _FakeVector:
    name = "fake-vector:v1"
    supports_exact_partition_filter = True

    def __init__(self, candidates: tuple[VectorCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, str, int]] = []

    def search(
        self,
        *,
        query: str,
        partition_id: str,
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        self.calls.append((query, partition_id, limit))
        return self.candidates


class _NoExactPartitionVector:
    name = "unsafe-filter-vector:v1"
    supports_exact_partition_filter = False

    def search(
        self,
        *,
        query: str,
        partition_id: str,
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        del query, partition_id, limit
        raise AssertionError("an adapter without an exact partition filter must not be called")


class _BrokenVector:
    name = "broken-vector:v1"
    supports_exact_partition_filter = True

    def search(
        self,
        *,
        query: str,
        partition_id: str,
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        del query, partition_id, limit
        raise RuntimeError("remote vector service unavailable")


def test_exact_fts_and_vector_have_stable_rrf_order_and_score_breakdown(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "stable-rrf.sqlite3"
    writer = MemoryEngine.local(database).initialize()
    exact = _remember(writer, scope, "release pipeline", "rrf-exact")
    lexical = _remember(writer, scope, "release pipeline checklist", "rrf-lexical")
    vector_only = _remember(writer, scope, "deployment guard", "rrf-vector")
    vector = _FakeVector(
        (
            VectorCandidate(claim_id=lexical.claim_id, score=0.99),
            VectorCandidate(claim_id=vector_only.claim_id, score=0.90),
            VectorCandidate(claim_id=exact.claim_id, score=0.80),
        )
    )
    engine = MemoryEngine.local(database, vector_retriever=vector).initialize()
    request = SearchRequest(query="release pipeline", scope=scope, limit=3)

    first = engine.search_result(request)
    second = engine.search_result(request)

    expected = [exact.claim_id, lexical.claim_id, vector_only.claim_id]
    assert [item.claim_id for item in first.items] == expected
    assert [item.claim_id for item in second.items] == expected
    assert [item.rank for item in first.items] == [1, 2, 3]
    assert [item.rrf_score for item in first.items] == pytest.approx(
        [item.rrf_score for item in second.items]
    )
    by_id = {item.claim_id: item for item in first.items}
    assert by_id[exact.claim_id].exact_score == 1.0
    assert by_id[exact.claim_id].lexical_score is not None
    assert by_id[exact.claim_id].vector_score == 0.80
    assert by_id[lexical.claim_id].exact_score is None
    assert by_id[lexical.claim_id].lexical_score is not None
    assert by_id[lexical.claim_id].vector_score == 0.99
    assert by_id[vector_only.claim_id].exact_score is None
    assert by_id[vector_only.claim_id].lexical_score is None
    assert by_id[vector_only.claim_id].vector_score == 0.90
    assert all(item.rank_score > 0 and item.rrf_score > 0 for item in first.items)

    trace = engine.retrieval_trace(first.retrieval_id or "", scope=scope)
    trace_by_id = {candidate.claim_id: candidate for candidate in trace.candidates}
    assert trace_by_id[exact.claim_id].exact_rank == 1
    assert trace_by_id[exact.claim_id].lexical_rank is not None
    assert trace_by_id[exact.claim_id].vector_rank == 3
    assert trace_by_id[lexical.claim_id].vector_rank == 1
    assert trace_by_id[vector_only.claim_id].vector_rank == 2
    assert trace.selected_claim_ids == tuple(expected)
    assert trace.query_shape["rrf"] == {
        "k": 60,
        "weights": {"exact": 1.5, "lexical": 1.0, "vector": 1.0},
    }


def test_malicious_cross_partition_vector_hit_is_not_hydrated_or_traced(
    tmp_path: Path,
) -> None:
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    database = tmp_path / "vector-scope.sqlite3"
    writer = MemoryEngine.local(database).initialize()
    alpha_claim = _remember(writer, alpha, "alpha vector needle", "alpha-vector")
    beta_claim = _remember(writer, beta, "beta confidential payload", "beta-vector")
    vector = _FakeVector(
        (
            VectorCandidate(claim_id=beta_claim.claim_id, score=1_000.0),
            VectorCandidate(claim_id=alpha_claim.claim_id, score=0.1),
        )
    )
    reader = MemoryEngine.local(database, vector_retriever=vector).initialize()

    result = reader.search_result(SearchRequest(query="alpha vector needle", scope=alpha, limit=10))

    assert [item.claim_id for item in result.items] == [alpha_claim.claim_id]
    assert all(item.partition == alpha for item in result.items)
    trace = reader.retrieval_trace(result.retrieval_id or "", scope=alpha)
    assert beta_claim.claim_id not in {item.claim_id for item in trace.candidates}
    assert beta_claim.claim_id not in trace.selected_claim_ids


@pytest.mark.parametrize(
    ("adapter", "reason"),
    [
        (_NoExactPartitionVector(), "vector_exact_partition_filter_unavailable"),
        (_BrokenVector(), "vector_retriever_unavailable"),
    ],
)
def test_vector_degradation_is_explicit_while_fts_remains_available(
    tmp_path: Path,
    scope: PartitionRef,
    adapter: object,
    reason: str,
) -> None:
    database = tmp_path / f"{reason}.sqlite3"
    writer = MemoryEngine.local(database).initialize()
    claim = _remember(writer, scope, "lexical fallback remains useful", reason)
    engine = MemoryEngine.local(
        database,
        vector_retriever=adapter,  # type: ignore[arg-type]
    ).initialize()

    result = engine.search_result(SearchRequest(query="lexical fallback", scope=scope))

    assert [item.claim_id for item in result.items] == [claim.claim_id]
    assert result.items[0].lexical_score is not None
    assert result.items[0].vector_score is None
    assert result.degraded is True
    assert result.degradation_reasons == (reason,)
    trace = engine.retrieval_trace(result.retrieval_id or "", scope=scope)
    assert trace.degraded is True
    assert trace.degradation_reasons == (reason,)


def test_retrieval_trace_is_hmac_pseudonymous_scope_authorized_and_purged(
    tmp_path: Path,
) -> None:
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    database = tmp_path / "retrieval-trace.sqlite3"
    owner = MemoryEngine.local(database).initialize()
    query = "ultraviolet trace query 7f2a"
    claim = _remember(
        owner,
        alpha,
        f"prefix {query} suffix",
        "trace-memory",
    )
    result = owner.search_result(SearchRequest(query=query, scope=alpha))
    assert result.retrieval_id is not None
    trace = owner.retrieval_trace(result.retrieval_id, scope=alpha)

    assert query not in json.dumps(trace.model_dump(mode="json"), ensure_ascii=False)
    assert trace.keyed_query_hash == owner.store.purge_registry.opaque_digest(
        "retrieval-query",
        query,
    )
    assert trace.keyed_query_hash != hashlib.sha256(query.encode()).hexdigest()
    with owner.store.connection() as connection:
        row = connection.execute(
            """
            SELECT keyed_query_hash, query_shape_json, ranking_json,
                   degradation_reasons_json
            FROM retrieval_runs WHERE retrieval_id = ?
            """,
            (result.retrieval_id,),
        ).fetchone()
    assert query not in " ".join(str(value) for value in row)

    with pytest.raises(RecallOriginError) as wrong_scope:
        owner.retrieval_trace(result.retrieval_id, scope=beta)
    assert wrong_scope.value.spec is NOT_FOUND
    beta_reader = MemoryEngine.local(
        database,
        principal=PrincipalContext(
            tenant_id="local",
            principal_id="beta-reader",
            principal_type=PrincipalType.AGENT,
            capabilities=frozenset({Capability.READ}),
        ),
        allowed_partitions=[beta],
    ).initialize()
    with pytest.raises(RecallOriginError) as unauthorized:
        beta_reader.retrieval_trace(result.retrieval_id)
    assert unauthorized.value.spec is NOT_FOUND

    deletion = owner.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=claim.claim_id,
            ),
            idempotency_key="purge-traced-claim",
            cascade_policy="purge",
        )
    )
    owner.purge(deletion.deletion_id)
    with pytest.raises(RecallOriginError) as purged:
        owner.retrieval_trace(result.retrieval_id, scope=alpha)
    assert purged.value.spec is NOT_FOUND


def test_context_pack_propagates_retrieval_id_and_vector_degradation(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "context-provenance.sqlite3"
    writer = MemoryEngine.local(database).initialize()
    _remember(writer, scope, "context fallback memory", "context-fallback")
    engine = MemoryEngine.local(
        database,
        vector_retriever=_BrokenVector(),
    ).initialize()

    pack = engine.context(
        ContextQuery(
            query="context fallback",
            scope=scope,
            token_budget=800,
        )
    )

    assert pack.retrieval_id
    assert pack.degraded is True
    assert pack.degradation_reasons == ("vector_retriever_unavailable",)
    trace = engine.retrieval_trace(pack.retrieval_id, scope=scope)
    assert trace.selected_claim_ids == tuple(item.claim_id for item in pack.items)
    assert trace.degraded is True
