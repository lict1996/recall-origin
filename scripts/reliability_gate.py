#!/usr/bin/env python3
"""Opt-in, deterministic reliability gate for expensive replay volumes."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from recall_origin import MemoryEngine
from recall_origin.contracts.v1 import (
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)


def run(iterations: int, database: Path) -> dict[str, object]:
    scope = PartitionRef.workspace("reliability-gate")
    request = RememberRequest(
        content="ten thousand retries produce one visible fact",
        scope=scope,
        memory_key="gate.replay",
        external_event_id="gate-event",
        idempotency_key="gate-request",
        origin=OriginContext(producer_id="reliability-gate"),
    )
    engine = MemoryEngine.local(database).initialize()
    started = time.perf_counter()
    first = engine.remember(request)
    for _ in range(iterations - 1):
        replay = engine.remember(request)
        if not replay.replayed or replay.event_id != first.event_id:
            raise AssertionError("idempotent replay diverged")
    elapsed = time.perf_counter() - started
    hits = engine.search(SearchRequest(query="ten thousand retries", scope=scope))
    with engine.store.connection() as connection:
        counts = {
            "events": int(connection.execute("SELECT count(*) FROM events").fetchone()[0]),
            "claims": int(connection.execute("SELECT count(*) FROM memory_claims").fetchone()[0]),
            "heads": int(connection.execute("SELECT count(*) FROM claim_heads").fetchone()[0]),
        }
    if counts != {"events": 1, "claims": 1, "heads": 1} or len(hits) != 1:
        raise AssertionError(
            f"replay gate produced duplicate visible state: counts={counts}, hits={len(hits)}"
        )
    return {
        "ok": True,
        "iterations": iterations,
        "elapsed_seconds_observed": round(elapsed, 6),
        "database_counts": counts,
        "visible_hits": len(hits),
        "note": "Observed local run only; this is not a benchmark claim.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--database", type=Path)
    arguments = parser.parse_args()
    if arguments.iterations < 1:
        parser.error("--iterations must be positive")
    if arguments.database is not None:
        result = run(arguments.iterations, arguments.database.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="recall-origin-reliability-") as directory:
            result = run(arguments.iterations, Path(directory) / "memory.sqlite3")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
