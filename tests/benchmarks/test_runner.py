from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from recall_origin.benchmarks import runner
from recall_origin.benchmarks.runner import (
    SCHEMA_VERSION,
    run_benchmark,
    run_performance_and_retrieval,
    to_inspector_benchmarks,
)


def test_smoke_artifact_contains_provenance_raw_results_and_real_metrics(
    tmp_path: Path,
) -> None:
    artifact = run_benchmark(
        tmp_path,
        scale=12,
        seed=17,
        query_count=6,
        k=5,
    )

    assert artifact["schema_version"] == SCHEMA_VERSION
    assert artifact["generated_at_utc"].endswith("+00:00")
    assert "git_commit" in artifact["provenance"]
    assert artifact["provenance"]["python_version"]
    assert artifact["provenance"]["platform"]
    assert artifact["provenance"]["machine"]
    assert artifact["provenance"]["logical_cpu_count"]
    assert artifact["provenance"]["sqlite_version"]
    source_snapshot = artifact["provenance"]["source_snapshot"]
    assert len(source_snapshot["sha256"]) == 64
    assert source_snapshot["file_count"] > 0
    assert source_snapshot["stable_during_run"] is True
    assert artifact["configuration"]["engine"]["durable"] is True
    assert artifact["configuration"]["engine"]["end_to_end_ingest"] is True
    assert artifact["configuration"]["engine"]["retrievers"]["vector"] is None
    observed = artifact["configuration"]["engine"]["observed_retrieval"]
    assert observed["ranking_policy_version"] >= 1
    assert observed["query_shape"]["retrievers"]["exact"] == "builtin:v1"
    assert observed["query_shape"]["retrievers"]["vector"] is None
    assert observed["query_shape"]["rrf"]["k"] > 0
    assert artifact["corpus"]["document_count"] == 12
    assert len(artifact["corpus"]["sha256"]) == 64
    assert len(artifact["retrieval"]["queries"]) == 6
    assert artifact["retrieval"]["recall_at_k"] == 1.0
    assert artifact["retrieval"]["mrr"] == 1.0
    assert artifact["retrieval"]["latency_ms"]["p95"] is not None
    assert set(artifact["retrieval"]["by_query_type"]) == {"exact", "lexical"}
    assert (
        sum(metrics["query_count"] for metrics in artifact["retrieval"]["by_query_type"].values())
        == 6
    )
    assert all(
        metrics["recall_at_k"] == 1.0 for metrics in artifact["retrieval"]["by_query_type"].values()
    )
    assert artifact["performance"]["ingest"]["writes"] == 12
    assert artifact["performance"]["ingest"]["writes_per_second"] > 0
    assert artifact["performance"]["db_size_bytes"] > 0
    assert artifact["comparisons"]["vector_baseline"] is None
    assert artifact["quality"]["passed"] is True


def test_inspector_payload_maps_measured_values_and_does_not_invent_candidate() -> None:
    artifact = {
        "configuration": {
            "engine": {
                "search_limit_k": 10,
                "vector_baseline": {"reason": "No vector measurement."},
            }
        },
        "retrieval": {
            "query_count": 7,
            "recall_at_k": 0.75,
            "mrr": 0.625,
            "latency_ms": {"p95": 4.25},
        },
        "performance": {
            "ingest": {
                "writes": 12,
                "writes_per_second": 321.5,
            }
        },
    }

    payload = to_inspector_benchmarks(artifact)

    assert [metric["baseline"] for metric in payload["metrics"]] == [
        0.75,
        0.625,
        4.25,
        321.5,
    ]
    assert all(metric["candidate"] is None for metric in payload["metrics"])
    assert payload["candidate_label"] == "Not measured"
    assert "No vector measurement." in payload["notes"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"scale": 0, "query_count": 1, "k": 1}, "scale must be positive"),
        ({"scale": 1, "query_count": 0, "k": 1}, "query_count must be positive"),
        ({"scale": 1, "query_count": 1, "k": 0}, r"k must be in \[1, 100]"),
        ({"scale": 1, "query_count": 1, "k": 101}, r"k must be in \[1, 100]"),
    ],
)
def test_runner_rejects_invalid_measurement_bounds(
    tmp_path: Path,
    arguments: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        run_performance_and_retrieval(
            tmp_path,
            seed=17,
            durable=True,
            **arguments,
        )


@pytest.mark.parametrize(("quality_passed", "expected_status"), [(True, 0), (False, 1)])
def test_cli_writes_canonical_and_inspector_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    quality_passed: bool,
    expected_status: int,
) -> None:
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "configuration": {"scale": 100},
        "quality": {"passed": quality_passed},
        "retrieval": {"recall_at_k": 0.75, "mrr": 0.5},
        "inspector_payload": {"generated_at": "2026-08-30T00:00:00+00:00"},
    }
    monkeypatch.setattr(runner, "run_benchmark", lambda *_args, **_kwargs: artifact)
    output = tmp_path / "nested" / "artifact.json"
    inspector = tmp_path / "inspector" / "payload.json"

    status = runner.main(
        [
            "--scale",
            "100",
            "--query-count",
            "1",
            "--relaxed-durability",
            "--output",
            str(output),
            "--inspector-output",
            str(inspector),
        ]
    )

    assert status == expected_status
    assert json.loads(output.read_text(encoding="utf-8")) == artifact
    assert json.loads(inspector.read_text(encoding="utf-8")) == artifact["inspector_payload"]
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "artifact": str(output.resolve()),
        "mrr": 0.5,
        "quality_passed": quality_passed,
        "recall_at_k": 0.75,
        "scale": 100,
        "schema_version": SCHEMA_VERSION,
    }
