from __future__ import annotations

import pytest

from recall_origin.benchmarks.corpus import (
    corpus_sha256,
    document_at,
    generate_queries,
    hash_documents,
    iter_documents,
    query_indices,
)


def test_corpus_and_queries_are_deterministic() -> None:
    first_documents = list(iter_documents(scale=8, seed=42))
    second_documents = list(iter_documents(scale=8, seed=42))
    first_queries = generate_queries(scale=8, count=5, seed=42)
    second_queries = generate_queries(scale=8, count=5, seed=42)

    assert first_documents == second_documents
    assert first_queries == second_queries
    assert corpus_sha256(scale=8, seed=42) == corpus_sha256(scale=8, seed=42)
    assert corpus_sha256(scale=8, seed=42) != corpus_sha256(scale=8, seed=43)
    assert len({query.relevant_doc_ids for query in first_queries}) == 5
    assert {query.query_type for query in first_queries} == {"exact", "lexical"}


def test_query_selection_is_version_stable_and_full_cycle() -> None:
    assert query_indices(scale=10, count=6, seed=42) == (3, 4, 5, 6, 7, 8)
    assert query_indices(scale=7, count=20, seed=42) == tuple(range(7))
    assert query_indices(scale=1, count=20, seed=42) == (0,)


def test_streamed_and_materialized_corpus_hashes_are_identical() -> None:
    documents = list(iter_documents(scale=8, seed=42))

    assert hash_documents(documents) == corpus_sha256(scale=8, seed=42)


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda: document_at(-1, seed=42), "index must be non-negative"),
        (lambda: list(iter_documents(scale=0, seed=42)), "scale must be positive"),
        (lambda: query_indices(scale=0, count=1, seed=42), "scale must be positive"),
        (lambda: query_indices(scale=1, count=0, seed=42), "query count must be positive"),
    ],
)
def test_corpus_generators_reject_invalid_bounds(
    operation: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        operation()  # type: ignore[operator]
