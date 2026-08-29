from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.v1 import (
    ContextQuery,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)
from recall_origin.retrieval.packing import render_untrusted_context


def _remember(
    engine: MemoryEngine,
    scope: PartitionRef,
    *,
    content: str,
    external_event_id: str,
) -> str:
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            external_event_id=external_event_id,
            origin=OriginContext(producer_id="recall-test"),
        )
    ).claim_id


def _length_counting_engine(
    tmp_path: Path,
    id_factory: Callable[[str], str],
) -> MemoryEngine:
    return MemoryEngine.local(
        tmp_path / "bounded-context.sqlite3",
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        id_factory=id_factory,
        token_counter=len,
    ).initialize()


def test_fts_search_is_safe_and_reindex_preserves_results(
    engine: MemoryEngine,
    scope: PartitionRef,
) -> None:
    claim_id = _remember(
        engine,
        scope,
        content="发布流程要求 CI 通过后再执行 release。",
        external_event_id="release-workflow",
    )

    before = engine.search(SearchRequest(query="CI release", scope=scope))
    hostile = engine.search(SearchRequest(query='" OR * NOT (', scope=scope))
    first_reindex = engine.reindex()
    second_reindex = engine.reindex()
    after = engine.search(SearchRequest(query="CI release", scope=scope))

    assert [item.claim_id for item in before] == [claim_id]
    assert hostile == ()
    assert [item.claim_id for item in after] == [claim_id]
    assert first_reindex["indexed"] == second_reindex["indexed"] == 1


def test_context_budget_counts_the_exact_untrusted_rendering(
    tmp_path: Path,
    id_factory: Callable[[str], str],
    scope: PartitionRef,
) -> None:
    engine = _length_counting_engine(tmp_path, id_factory)
    short_claim = _remember(
        engine,
        scope,
        content="release short",
        external_event_id="short",
    )
    long_claim = _remember(
        engine,
        scope,
        content="release " + ("x" * 1_000),
        external_event_id="long",
    )

    pack = engine.context(
        ContextQuery(
            query="release",
            scope=scope,
            token_budget=320,
            limit=8,
        )
    )
    rendered = render_untrusted_context(pack.items)

    assert pack.token_count == len(rendered)
    assert pack.token_count <= pack.token_budget
    assert [item.claim_id for item in pack.items] == [short_claim]
    assert [(item.claim_id, item.reason) for item in pack.omitted] == [(long_claim, "token_budget")]


@pytest.mark.parametrize("budget", [32, 64, 96])
def test_every_accepted_budget_is_a_hard_limit_even_for_an_empty_pack(
    tmp_path: Path,
    id_factory: Callable[[str], str],
    scope: PartitionRef,
    budget: int,
) -> None:
    engine = _length_counting_engine(tmp_path, id_factory)

    pack = engine.context(
        ContextQuery(
            query="there are no matching memories",
            scope=scope,
            token_budget=budget,
        )
    )

    assert pack.items == ()
    assert pack.token_count == len(render_untrusted_context(pack.items))
    assert pack.token_count <= budget
