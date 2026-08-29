"""Run RecallOrigin's fully offline benchmark and emit a JSON evidence artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recall_origin import MemoryEngine, __version__
from recall_origin.benchmarks.corpus import corpus_sha256, generate_queries, iter_documents
from recall_origin.benchmarks.metrics import mean_reciprocal_rank, percentile, recall_at_k
from recall_origin.benchmarks.quality import deterministic_id_factory, run_quality_suite
from recall_origin.contracts.v1 import OriginContext, PartitionRef, RememberRequest, SearchRequest

SCHEMA_VERSION = "recall-origin-benchmark/v1"
SUPPORTED_SCALES = (100, 10_000, 100_000, 1_000_000)
_DOC_ID_PATTERN = re.compile(r"^\[doc:(doc-\d{9})\]")


def _git_provenance(repository: Path) -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "git_commit_available": commit.returncode == 0,
        "git_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
    }


def _source_snapshot(repository: Path) -> dict[str, Any]:
    """Hash executable package sources and available dependency metadata."""
    package_root = Path(__file__).resolve().parents[1]
    logical_files = [
        (f"recall_origin/{path.relative_to(package_root).as_posix()}", path)
        for path in sorted(package_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    for name in ("pyproject.toml", "uv.lock"):
        path = repository / name
        if path.is_file():
            logical_files.append((name, path))
    logical_files.sort(key=lambda item: item[0])

    digest = hashlib.sha256()
    for logical_name, path in logical_files:
        name_bytes = logical_name.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(name_bytes).to_bytes(8, byteorder="big"))
        digest.update(name_bytes)
        digest.update(len(content).to_bytes(8, byteorder="big"))
        digest.update(content)
    return {
        "algorithm": "sha256(length-prefixed-logical-path-and-content/v1)",
        "sha256": digest.hexdigest(),
        "file_count": len(logical_files),
        "includes": [
            "recall_origin package files (excluding __pycache__)",
            "pyproject.toml when available",
            "uv.lock when available",
        ],
    }


def _doc_id(content: str) -> str | None:
    match = _DOC_ID_PATTERN.match(content)
    return match.group(1) if match else None


def _database_sizes(database: Path) -> dict[str, Any]:
    files = {
        "sqlite": database,
        "wal": Path(f"{database}-wal"),
        "shm": Path(f"{database}-shm"),
        "purge_registry": Path(f"{database}.purge.sqlite3"),
    }
    breakdown = {name: path.stat().st_size if path.exists() else 0 for name, path in files.items()}
    return {
        "db_size_bytes": sum(breakdown.values()),
        "database_file_bytes": breakdown,
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _retrieval_metrics(
    observations: list[tuple[list[str], tuple[str, ...]]],
    latencies_ms: list[float],
    *,
    k: int,
) -> dict[str, Any]:
    return {
        "query_count": len(observations),
        "recall_at_k": _rounded(recall_at_k(observations, k=k)),
        "mrr": _rounded(mean_reciprocal_rank(observations)),
        "latency_ms": {
            "p50": _rounded(percentile(latencies_ms, 50)),
            "p95": _rounded(percentile(latencies_ms, 95)),
            "p99": _rounded(percentile(latencies_ms, 99)),
        },
    }


def _engine_configuration(*, durable: bool, k: int) -> dict[str, Any]:
    return {
        "package_version": __version__,
        "engine": "recall_origin.MemoryEngine",
        "storage": "SQLite + FTS5",
        "loader": "public MemoryEngine.remember API",
        "end_to_end_ingest": True,
        "durable": durable,
        "sqlite_profile": {
            "journal_mode": "WAL",
            "synchronous": "FULL" if durable else "NORMAL",
            "secure_delete": True,
            "foreign_keys": True,
            "busy_timeout_ms": 5_000,
        },
        "retrievers": {
            "exact": "builtin:v1",
            "lexical": "sqlite-fts5:v1",
            "vector": None,
        },
        "fusion": "RRF policy from the installed engine",
        "search_limit_k": k,
        "vector_baseline": {
            "measured": False,
            "reason": "No vector retriever was configured; no vector comparison is reported.",
        },
    }


def run_performance_and_retrieval(
    root: Path,
    *,
    scale: int,
    seed: int,
    query_count: int,
    k: int,
    durable: bool,
) -> dict[str, Any]:
    """Ingest a synthetic corpus and measure real retrieval observations."""
    if scale < 1:
        msg = "scale must be positive"
        raise ValueError(msg)
    if query_count < 1:
        msg = "query_count must be positive"
        raise ValueError(msg)
    if not 1 <= k <= 100:
        msg = "k must be in [1, 100]"
        raise ValueError(msg)

    database = root / "performance.sqlite3"
    engine = MemoryEngine.local(
        database,
        durable=durable,
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        id_factory=deterministic_id_factory(),
    ).initialize()
    scope = PartitionRef.workspace("benchmark")

    ingest_started = time.perf_counter_ns()
    for document in iter_documents(scale=scale, seed=seed):
        engine.remember(
            RememberRequest(
                content=document.content,
                scope=scope,
                memory_key=document.memory_key,
                external_event_id=f"benchmark:{document.doc_id}",
                origin=OriginContext(producer_id="offline-benchmark"),
            )
        )
    ingest_ns = time.perf_counter_ns() - ingest_started

    raw_queries: list[dict[str, Any]] = []
    metric_observations: list[tuple[list[str], tuple[str, ...]]] = []
    search_latencies_ms: list[float] = []
    observations_by_query_type: dict[str, list[tuple[list[str], tuple[str, ...]]]] = {}
    latencies_by_query_type: dict[str, list[float]] = {}
    observed_retrieval_configuration: dict[str, Any] | None = None
    for query in generate_queries(scale=scale, count=query_count, seed=seed):
        started = time.perf_counter_ns()
        result = engine.search_result(SearchRequest(query=query.text, scope=scope, limit=k))
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        if observed_retrieval_configuration is None:
            observed_retrieval_configuration = {
                "ranking_policy_version": result.ranking_policy_version,
                "trace_available": result.retrieval_id is not None,
                "query_shape": (
                    engine.retrieval_trace(result.retrieval_id, scope=scope).query_shape
                    if result.retrieval_id is not None
                    else None
                ),
            }
        search_latencies_ms.append(duration_ms)
        ranked_doc_ids = [
            parsed for hit in result.items if (parsed := _doc_id(hit.content)) is not None
        ]
        metric_observations.append((ranked_doc_ids, query.relevant_doc_ids))
        observations_by_query_type.setdefault(query.query_type, []).append(
            (ranked_doc_ids, query.relevant_doc_ids)
        )
        latencies_by_query_type.setdefault(query.query_type, []).append(duration_ms)
        raw_queries.append(
            {
                "query_id": query.query_id,
                "query_type": query.query_type,
                "query": query.text,
                "relevant_doc_ids": list(query.relevant_doc_ids),
                "returned": [
                    {
                        "doc_id": _doc_id(hit.content),
                        "claim_id": hit.claim_id,
                        "rank": hit.rank,
                        "exact_score": hit.exact_score,
                        "lexical_score": hit.lexical_score,
                        "vector_score": hit.vector_score,
                        "rrf_score": hit.rrf_score,
                    }
                    for hit in result.items
                ],
                "duration_ms": _rounded(duration_ms),
                "candidate_count": result.candidate_count,
                "ranking_policy_version": result.ranking_policy_version,
                "degraded": result.degraded,
                "degradation_reasons": list(result.degradation_reasons),
            }
        )

    elapsed_seconds = ingest_ns / 1_000_000_000
    retrieval = {
        **_retrieval_metrics(metric_observations, search_latencies_ms, k=k),
        "by_query_type": {
            query_type: _retrieval_metrics(
                observations_by_query_type[query_type],
                latencies_by_query_type[query_type],
                k=k,
            )
            for query_type in sorted(observations_by_query_type)
        },
        "queries": raw_queries,
    }
    performance = {
        "ingest": {
            "writes": scale,
            "duration_seconds": _rounded(elapsed_seconds),
            "writes_per_second": _rounded(scale / elapsed_seconds),
            "path": "public MemoryEngine.remember (end-to-end)",
        },
        **_database_sizes(database),
    }
    return {
        "observed_engine_configuration": observed_retrieval_configuration,
        "retrieval": retrieval,
        "performance": performance,
    }


def to_inspector_benchmarks(artifact: dict[str, Any]) -> dict[str, Any]:
    """Map only measured artifact values to the Inspector benchmark payload."""
    configuration = artifact["configuration"]
    retrieval = artifact["retrieval"]
    performance = artifact["performance"]
    k = configuration["engine"]["search_limit_k"]
    return {
        "baseline_label": "Current engine",
        "candidate_label": "Not measured",
        "metrics": [
            {
                "name": f"Recall@{k}",
                "unit": "ratio",
                "baseline": retrieval["recall_at_k"],
                "candidate": None,
                "sample_size": retrieval["query_count"],
                "direction": "higher",
            },
            {
                "name": "MRR",
                "unit": "ratio",
                "baseline": retrieval["mrr"],
                "candidate": None,
                "sample_size": retrieval["query_count"],
                "direction": "higher",
            },
            {
                "name": "Search p95",
                "unit": "ms",
                "baseline": retrieval["latency_ms"]["p95"],
                "candidate": None,
                "sample_size": retrieval["query_count"],
                "direction": "lower",
            },
            {
                "name": "Ingest throughput",
                "unit": "writes/s",
                "baseline": performance["ingest"]["writes_per_second"],
                "candidate": None,
                "sample_size": performance["ingest"]["writes"],
                "direction": "higher",
            },
        ],
        "notes": [
            "All displayed numbers come from this artifact's measured current-engine run.",
            configuration["engine"]["vector_baseline"]["reason"],
        ],
    }


def run_benchmark(
    work_root: Path,
    *,
    scale: int = 100,
    seed: int = 20260830,
    query_count: int = 25,
    k: int = 10,
    durable: bool = True,
) -> dict[str, Any]:
    """Run the complete quality, retrieval, and performance experiment."""
    work_root.mkdir(parents=True, exist_ok=True)
    repository = Path(__file__).resolve().parents[3]
    source_before = _source_snapshot(repository)
    engine_config = _engine_configuration(durable=durable, k=k)
    measured = run_performance_and_retrieval(
        work_root / "measured",
        scale=scale,
        seed=seed,
        query_count=query_count,
        k=k,
        durable=durable,
    )
    engine_config["observed_retrieval"] = measured.pop("observed_engine_configuration")
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "provenance": {
            **_git_provenance(repository),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
            "sqlite_version": sqlite3.sqlite_version,
            "source_snapshot": source_before,
        },
        "configuration": {
            "scale": scale,
            "seed": seed,
            "query_count": min(scale, query_count),
            "corpus_generator": "recall-origin-synthetic/v1",
            "query_generator": "recall-origin-coprime-cycle/v1",
            "engine": engine_config,
        },
        "corpus": {
            "document_count": scale,
            "sha256": corpus_sha256(scale=scale, seed=seed),
        },
        "quality": run_quality_suite(work_root / "quality"),
        **measured,
        "comparisons": {
            "vector_baseline": None,
            "reason": engine_config["vector_baseline"]["reason"],
        },
    }
    source_after = _source_snapshot(repository)
    artifact["provenance"]["source_snapshot"]["stable_during_run"] = (
        source_before["sha256"] == source_after["sha256"]
        and source_before["file_count"] == source_after["file_count"]
    )
    if not artifact["provenance"]["source_snapshot"]["stable_during_run"]:
        msg = "source package changed during benchmark run; refusing to emit mixed evidence"
        raise RuntimeError(msg)
    artifact["inspector_payload"] = {
        "generated_at": artifact["generated_at_utc"],
        "benchmarks": to_inspector_benchmarks(artifact),
    }
    return artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a fully offline RecallOrigin benchmark. The default 100-document "
            "scale is a smoke run; published performance runs should use an explicit scale."
        )
    )
    parser.add_argument("--scale", type=int, choices=SUPPORTED_SCALES, default=100)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--query-count", type=int, default=25)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--output", type=Path, default=Path("benchmark-artifact.json"))
    parser.add_argument(
        "--inspector-output",
        type=Path,
        help="Optionally write the measured Inspector-compatible payload as JSON.",
    )
    parser.add_argument(
        "--relaxed-durability",
        action="store_true",
        help=(
            "Use SQLite's non-durable benchmark mode. The artifact records this; "
            "do not compare it to durable runs as if configurations matched."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parser().parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="recall-origin-benchmark-") as temporary:
        artifact = run_benchmark(
            Path(temporary),
            scale=args.scale,
            seed=args.seed,
            query_count=args.query_count,
            k=args.k,
            durable=not args.relaxed_durability,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.inspector_output is not None:
        args.inspector_output.parent.mkdir(parents=True, exist_ok=True)
        args.inspector_output.write_text(
            json.dumps(
                artifact["inspector_payload"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    summary = {
        "artifact": str(args.output.resolve()),
        "schema_version": artifact["schema_version"],
        "quality_passed": artifact["quality"]["passed"],
        "recall_at_k": artifact["retrieval"]["recall_at_k"],
        "mrr": artifact["retrieval"]["mrr"],
        "scale": artifact["configuration"]["scale"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if artifact["quality"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
