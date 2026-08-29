from __future__ import annotations

import pytest

from recall_origin.benchmarks.metrics import mean_reciprocal_rank, percentile, recall_at_k


def test_retrieval_metrics_use_explicit_relevance_labels() -> None:
    observations = [
        (["a", "b", "c"], ["a"]),
        (["x", "b", "c"], ["b"]),
        (["x", "y", "z"], ["missing"]),
    ]

    assert recall_at_k(observations, k=1) == pytest.approx(1 / 3)
    assert recall_at_k(observations, k=2) == pytest.approx(2 / 3)
    assert mean_reciprocal_rank(observations) == pytest.approx((1 + 1 / 2) / 3)


def test_latency_percentiles_use_nearest_rank_and_preserve_empty_state() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 100.0]

    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == 100.0
    assert percentile(values, 99) == 100.0
    assert percentile([], 95) is None


def test_retrieval_metrics_validate_empty_and_malformed_observations() -> None:
    assert recall_at_k([], k=10) == 0.0
    assert mean_reciprocal_rank([]) == 0.0

    with pytest.raises(ValueError, match="k must be positive"):
        recall_at_k([(["a"], ["a"])], k=0)
    with pytest.raises(ValueError, match="at least one relevant id"):
        recall_at_k([(["a"], [])], k=1)
    with pytest.raises(ValueError, match="at least one relevant id"):
        mean_reciprocal_rank([(["a"], [])])


@pytest.mark.parametrize("value", [0, -1, 100.1])
def test_percentile_rejects_values_outside_its_declared_range(value: float) -> None:
    with pytest.raises(ValueError, match=r"percentile must be in \(0, 100]"):
        percentile([1.0], value)
