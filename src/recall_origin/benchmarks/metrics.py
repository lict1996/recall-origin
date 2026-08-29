"""Small, dependency-free RecallOrigin benchmark metric functions."""

from __future__ import annotations

import math
from collections.abc import Sequence


def recall_at_k(
    observations: Sequence[tuple[Sequence[str], Sequence[str]]],
    *,
    k: int,
) -> float:
    """Return macro-averaged Recall@k over ``(ranked, relevant)`` pairs."""
    if k < 1:
        msg = "k must be positive"
        raise ValueError(msg)
    if not observations:
        return 0.0
    recalls: list[float] = []
    for ranked, relevant in observations:
        relevant_set = set(relevant)
        if not relevant_set:
            msg = "every observation must contain at least one relevant id"
            raise ValueError(msg)
        found = relevant_set.intersection(ranked[:k])
        recalls.append(len(found) / len(relevant_set))
    return sum(recalls) / len(recalls)


def mean_reciprocal_rank(
    observations: Sequence[tuple[Sequence[str], Sequence[str]]],
) -> float:
    """Return mean reciprocal rank for the first relevant result."""
    if not observations:
        return 0.0
    reciprocal_ranks: list[float] = []
    for ranked, relevant in observations:
        relevant_set = set(relevant)
        if not relevant_set:
            msg = "every observation must contain at least one relevant id"
            raise ValueError(msg)
        reciprocal = next(
            (1.0 / rank for rank, item in enumerate(ranked, start=1) if item in relevant_set),
            0.0,
        )
        reciprocal_ranks.append(reciprocal)
    return sum(reciprocal_ranks) / len(reciprocal_ranks)


def percentile(values: Sequence[float], percentile_value: float) -> float | None:
    """Return the nearest-rank percentile, or ``None`` for no observations."""
    if not 0 < percentile_value <= 100:
        msg = "percentile must be in (0, 100]"
        raise ValueError(msg)
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile_value / 100 * len(ordered)))
    return ordered[rank - 1]
