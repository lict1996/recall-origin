"""RecallOrigin quality cases executed through the public MemoryEngine API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recall_origin import MemoryEngine
from recall_origin.contracts.v1 import (
    ForgetRequest,
    ForgetTarget,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)
from recall_origin.domain.enums import ForgetTargetType

_FIXED_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def deterministic_id_factory() -> Callable[[str], str]:
    """Return an isolated, deterministic ID source for reproducible raw cases."""
    counters: dict[str, int] = {}

    def make_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]:012d}"

    return make_id


def _engine(path: Path) -> MemoryEngine:
    return MemoryEngine.local(
        path,
        clock=lambda: _FIXED_NOW,
        id_factory=deterministic_id_factory(),
    ).initialize()


def _remember(
    engine: MemoryEngine,
    scope: PartitionRef,
    *,
    content: str,
    event_id: str,
    memory_key: str | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> Any:
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            memory_key=memory_key,
            external_event_id=event_id,
            origin=OriginContext(producer_id="offline-benchmark"),
            valid_from=valid_from,
            valid_to=valid_to,
        )
    )


def _hit_result(hit: Any) -> dict[str, Any]:
    return {
        "claim_id": hit.claim_id,
        "rank": hit.rank,
        "content": hit.content,
        "exact_score": hit.exact_score,
        "lexical_score": hit.lexical_score,
        "vector_score": hit.vector_score,
        "rrf_score": hit.rrf_score,
    }


def exact_and_lexical_case(root: Path) -> dict[str, Any]:
    """Verify the built-in exact and lexical retrieval signals."""
    scope = PartitionRef.workspace("quality-exact-lexical")
    engine = _engine(root / "exact-lexical.sqlite3")
    exact = _remember(
        engine,
        scope,
        content="The release train uses the silver kestrel checklist.",
        event_id="exact-source",
    )
    lexical = _remember(
        engine,
        scope,
        content="Operators consult the kinetic marmot handbook before rollback.",
        event_id="lexical-source",
    )
    exact_hits = engine.search(
        SearchRequest(
            query="The release train uses the silver kestrel checklist.",
            scope=scope,
            limit=3,
        )
    )
    lexical_hits = engine.search(
        SearchRequest(query="kinetic marmot handbook", scope=scope, limit=3)
    )
    passed = bool(
        exact_hits
        and lexical_hits
        and exact_hits[0].claim_id == exact.claim_id
        and exact_hits[0].exact_score is not None
        and lexical_hits[0].claim_id == lexical.claim_id
        and lexical_hits[0].lexical_score is not None
    )
    return {
        "case_id": "exact_and_lexical",
        "description": "Exact content and lexical phrase retrieve their labeled claims.",
        "passed": passed,
        "expected": {
            "exact_claim_id": exact.claim_id,
            "lexical_claim_id": lexical.claim_id,
        },
        "actual": {
            "exact_hits": [_hit_result(hit) for hit in exact_hits],
            "lexical_hits": [_hit_result(hit) for hit in lexical_hits],
        },
    }


def bitemporal_case(root: Path) -> dict[str, Any]:
    """Verify valid-time and transaction-time filtering together."""
    scope = PartitionRef.workspace("quality-bitemporal")
    engine = _engine(root / "bitemporal.sqlite3")
    old = _remember(
        engine,
        scope,
        content="Project Atlas used the cobalt protocol.",
        event_id="protocol-old",
        memory_key="project.atlas.protocol",
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        valid_to=datetime(2025, 6, 1, tzinfo=UTC),
    )
    known_before_replacement = int(engine.doctor()["ledger_head"])
    new = _remember(
        engine,
        scope,
        content="Project Atlas uses the amber protocol.",
        event_id="protocol-new",
        memory_key="project.atlas.protocol",
        valid_from=datetime(2025, 6, 1, tzinfo=UTC),
    )
    historical_hits = engine.search(
        SearchRequest(
            query="cobalt protocol",
            scope=scope,
            valid_at=datetime(2025, 3, 1, tzinfo=UTC),
            known_at_seq=known_before_replacement,
            limit=3,
        )
    )
    current_hits = engine.search(
        SearchRequest(
            query="amber protocol",
            scope=scope,
            valid_at=datetime(2026, 1, 1, tzinfo=UTC),
            limit=3,
        )
    )
    passed = bool(
        historical_hits
        and current_hits
        and historical_hits[0].claim_id == old.claim_id
        and current_hits[0].claim_id == new.claim_id
    )
    return {
        "case_id": "bitemporal_valid_and_known_time",
        "description": "Historical knowledge and real-world validity select the intended revision.",
        "passed": passed,
        "expected": {
            "historical_claim_id": old.claim_id,
            "current_claim_id": new.claim_id,
            "known_at_seq": known_before_replacement,
            "valid_at_historical": "2025-03-01T00:00:00+00:00",
            "valid_at_current": "2026-01-01T00:00:00+00:00",
        },
        "actual": {
            "historical_hits": [_hit_result(hit) for hit in historical_hits],
            "current_hits": [_hit_result(hit) for hit in current_hits],
        },
    }


def partition_isolation_case(root: Path) -> dict[str, Any]:
    """Verify that a sentinel in one exact partition never appears in another."""
    engine = _engine(root / "partition-isolation.sqlite3")
    alpha = PartitionRef.workspace("quality-alpha")
    beta = PartitionRef.workspace("quality-beta")
    beta_memory = _remember(
        engine,
        beta,
        content="partition sentinel ultravioletbadger4821",
        event_id="beta-only-source",
    )
    alpha_hits = engine.search(SearchRequest(query="ultravioletbadger4821", scope=alpha, limit=5))
    beta_hits = engine.search(SearchRequest(query="ultravioletbadger4821", scope=beta, limit=5))
    passed = not alpha_hits and bool(beta_hits and beta_hits[0].claim_id == beta_memory.claim_id)
    return {
        "case_id": "partition_isolation",
        "description": "A beta-only sentinel is absent from alpha and present in beta.",
        "passed": passed,
        "expected": {
            "alpha_claim_ids": [],
            "beta_claim_id": beta_memory.claim_id,
        },
        "actual": {
            "alpha_hits": [_hit_result(hit) for hit in alpha_hits],
            "beta_hits": [_hit_result(hit) for hit in beta_hits],
        },
    }


def deletion_non_resurrection_case(root: Path) -> dict[str, Any]:
    """Verify delete, purge, reindex, and reopen do not restore a claim."""
    database = root / "deletion.sqlite3"
    scope = PartitionRef.workspace("quality-deletion")
    engine = _engine(database)
    target = _remember(
        engine,
        scope,
        content="nonresurrection sentinel obsidianotter7391",
        event_id="deletion-source",
    )
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.CLAIM,
                target_id=target.claim_id,
            ),
            idempotency_key="quality-delete-claim",
            expected_revision_id=target.revision_id,
        )
    )
    completed = engine.purge(deletion.deletion_id)
    reindex_result = engine.reindex()
    after_purge = engine.search(SearchRequest(query="obsidianotter7391", scope=scope, limit=5))
    reopened = _engine(database)
    after_reopen = reopened.search(SearchRequest(query="obsidianotter7391", scope=scope, limit=5))
    passed = completed.state.value == "completed" and not after_purge and not after_reopen
    return {
        "case_id": "deletion_non_resurrection",
        "description": "Purged content remains absent after reindex and engine reopen.",
        "passed": passed,
        "expected": {
            "deletion_state": "completed",
            "post_purge_claim_ids": [],
            "post_reopen_claim_ids": [],
        },
        "actual": {
            "deletion_id": deletion.deletion_id,
            "deletion_state": completed.state.value,
            "reindex": reindex_result,
            "post_purge_hits": [_hit_result(hit) for hit in after_purge],
            "post_reopen_hits": [_hit_result(hit) for hit in after_reopen],
        },
    }


def run_quality_suite(root: Path) -> dict[str, Any]:
    """Run all required behavioral cases into isolated local databases."""
    root.mkdir(parents=True, exist_ok=True)
    cases = [
        exact_and_lexical_case(root),
        bitemporal_case(root),
        partition_isolation_case(root),
        deletion_non_resurrection_case(root),
    ]
    return {
        "passed": all(bool(case["passed"]) for case in cases),
        "passed_count": sum(bool(case["passed"]) for case in cases),
        "case_count": len(cases),
        "cases": cases,
    }
