from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.v1 import PartitionRef


@pytest.fixture
def scope() -> PartitionRef:
    return PartitionRef.workspace("alpha")


@pytest.fixture
def id_factory() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def make(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]:08d}"

    return make


@pytest.fixture
def engine(
    tmp_path: Path,
    id_factory: Callable[[str], str],
) -> MemoryEngine:
    runtime = MemoryEngine.local(
        tmp_path / "memory.sqlite3",
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        id_factory=id_factory,
    )
    runtime.initialize()
    return runtime
