"""Security-conscious boundary for optional vector retrieval."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VectorCandidate:
    """One opaque vector hit returned by an adapter."""

    claim_id: str
    score: float

    def __post_init__(self) -> None:
        if not self.claim_id:
            raise ValueError("vector candidate claim_id cannot be empty")
        if not math.isfinite(self.score):
            raise ValueError("vector candidate score must be finite")


class VectorRetriever(Protocol):
    """Optional vector search adapter.

    Adapters must enforce the exact partition identifier supplied by the
    engine. RecallOrigin still performs canonical partition hydration on every
    returned claim, so an adapter cannot widen authorization by returning an
    ID from another partition.
    """

    @property
    def name(self) -> str: ...

    @property
    def supports_exact_partition_filter(self) -> bool: ...

    def search(
        self,
        *,
        query: str,
        partition_id: str,
        limit: int,
    ) -> tuple[VectorCandidate, ...]: ...
