from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from recall_origin.benchmarks.runner import SCHEMA_VERSION, _source_snapshot

_REPOSITORY = Path(__file__).resolve().parents[2]
_RESULTS = _REPOSITORY / "docs" / "benchmarks" / "results"
_ARTIFACT = _RESULTS / "scale-10000-durable.json"
_INSPECTOR = _RESULTS / "scale-10000-durable-inspector.json"
_RUN_METADATA = _RESULTS / "scale-10000-durable-run.json"
_RUN_SCHEMA_VERSION = "recall-origin-benchmark-run/v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_release_evidence_checksums_are_complete_and_valid() -> None:
    declarations = {}
    for line in (_RESULTS / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split("  ", maxsplit=1)
        declarations[filename] = digest

    assert set(declarations) == {
        "scale-10000-durable-inspector.json",
        "scale-10000-durable-metrics.csv",
        "scale-10000-durable-run.json",
        "scale-10000-durable-summary.json",
        "scale-10000-durable-summary.svg",
        "scale-10000-durable.json",
    }
    for filename, expected in declarations.items():
        assert _sha256(_RESULTS / filename) == expected


def test_release_artifact_is_bounded_measured_and_tied_to_current_source() -> None:
    artifact = _load_json(_ARTIFACT)

    assert _ARTIFACT.stat().st_size < 1_000_000
    assert artifact["schema_version"] == SCHEMA_VERSION
    assert artifact["configuration"]["scale"] == 10_000
    assert artifact["configuration"]["seed"] == 20_260_830
    assert artifact["configuration"]["query_count"] == 250
    assert artifact["configuration"]["engine"]["durable"] is True
    assert artifact["configuration"]["engine"]["retrievers"]["vector"] is None
    assert artifact["comparisons"]["vector_baseline"] is None
    assert artifact["quality"]["passed"] is True
    assert artifact["quality"]["passed_count"] == artifact["quality"]["case_count"] == 4
    assert artifact["retrieval"]["query_count"] == 250
    assert len(artifact["retrieval"]["queries"]) == 250
    assert artifact["retrieval"]["recall_at_k"] == 1.0
    assert artifact["retrieval"]["mrr"] == 1.0
    assert all(
        hit["vector_score"] is None
        for query in artifact["retrieval"]["queries"]
        for hit in query["returned"]
    )
    source_snapshot = artifact["provenance"]["source_snapshot"]
    assert source_snapshot["stable_during_run"] is True
    assert _source_snapshot(_REPOSITORY)["sha256"] == source_snapshot["sha256"]
    provenance = artifact["provenance"]
    assert provenance["git_commit_available"] is True
    assert provenance["git_dirty"] is False
    commit = provenance["git_commit"]
    assert isinstance(commit, str)
    assert len(commit) == 40
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=_REPOSITORY,
        check=False,
        capture_output=True,
    )
    if ancestry.returncode != 0:
        # actions/checkout uses a one-commit shallow clone in normal CI. The raw
        # HEAD commit object still proves that the measured commit is its direct
        # parent, which is the documented two-commit evidence workflow.
        head_object = subprocess.run(
            ["git", "cat-file", "-p", "HEAD"],
            cwd=_REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        direct_parents = {
            line.removeprefix("parent ")
            for line in head_object.splitlines()
            if line.startswith("parent ")
        }
        assert commit in direct_parents


def test_release_inspector_and_run_metadata_match_canonical_artifact() -> None:
    artifact = _load_json(_ARTIFACT)
    inspector = _load_json(_INSPECTOR)
    run_metadata = _load_json(_RUN_METADATA)

    assert inspector == artifact["inspector_payload"]
    assert run_metadata["schema_version"] == _RUN_SCHEMA_VERSION
    assert run_metadata["artifact"] == _ARTIFACT.name
    assert run_metadata["artifact_sha256"] == _sha256(_ARTIFACT)
    assert run_metadata["git_commit"] == artifact["provenance"]["git_commit"]
    assert run_metadata["command"] == [
        "uv",
        "run",
        "python",
        "scripts/benchmark_release.py",
    ]
    assert run_metadata["benchmark_command"] == [
        "python",
        "-m",
        "recall_origin.benchmarks.runner",
        "--scale",
        "10000",
        "--seed",
        "20260830",
        "--query-count",
        "250",
        "--k",
        "10",
        "--output",
        "docs/benchmarks/results/scale-10000-durable.json",
        "--inspector-output",
        "docs/benchmarks/results/scale-10000-durable-inspector.json",
    ]

    started_at = datetime.fromisoformat(run_metadata["started_at_utc"])
    ended_at = datetime.fromisoformat(run_metadata["ended_at_utc"])
    generated_at = datetime.fromisoformat(artifact["generated_at_utc"])
    assert started_at <= generated_at <= ended_at
    elapsed_seconds = (ended_at - started_at).total_seconds()
    real_seconds = run_metadata["timing"]["real_seconds"]
    assert abs(real_seconds - elapsed_seconds) <= max(1.0, elapsed_seconds * 0.01)
    assert real_seconds >= artifact["performance"]["ingest"]["duration_seconds"]
    assert run_metadata["timing_scope"] == "benchmark_command"
    assert run_metadata["environment"]["uv_version"]
    assert run_metadata["environment"]["uv_lock_sha256"] == _sha256(_REPOSITORY / "uv.lock")


def test_release_summary_csv_and_svg_derive_from_canonical_artifact() -> None:
    artifact = _load_json(_ARTIFACT)
    summary = _load_json(_RESULTS / "scale-10000-durable-summary.json")
    with (_RESULTS / "scale-10000-durable-metrics.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    svg = (_RESULTS / "scale-10000-durable-summary.svg").read_text(encoding="utf-8")

    assert summary["source_artifact_sha256"] == _sha256(_ARTIFACT)
    assert summary["retrieval"]["recall_at_k"] == artifact["retrieval"]["recall_at_k"]
    assert summary["retrieval"]["mrr"] == artifact["retrieval"]["mrr"]
    assert summary["retrieval"]["latency_ms"] == artifact["retrieval"]["latency_ms"]
    assert (
        float(next(row["value"] for row in rows if row["metric"] == "throughput"))
        == (artifact["performance"]["ingest"]["writes_per_second"])
    )
    assert summary["source_artifact_sha256"][:12] in svg
    assert "<script" not in svg
    assert "foreignObject" not in svg
