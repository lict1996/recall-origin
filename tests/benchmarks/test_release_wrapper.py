from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPOSITORY = Path(__file__).resolve().parents[2]


def _load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


benchmark_release = _load_script(
    "recall_origin_benchmark_release",
    _REPOSITORY / "scripts" / "benchmark_release.py",
)
render_summary = _load_script(
    "recall_origin_render_benchmark_summary",
    _REPOSITORY / "scripts" / "render_benchmark_summary.py",
)


def _artifact(*, commit: str, dirty: bool = False) -> dict[str, Any]:
    generated_at = datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC).isoformat()
    return {
        "schema_version": "recall-origin-benchmark/v1",
        "generated_at_utc": generated_at,
        "provenance": {
            "git_commit": commit,
            "git_commit_available": True,
            "git_dirty": dirty,
            "source_snapshot": {"stable_during_run": True},
        },
        "configuration": {
            "scale": 10_000,
            "seed": 20_260_830,
            "query_count": 250,
            "engine": {
                "durable": True,
                "search_limit_k": 10,
            },
        },
        "quality": {"passed": True},
        "inspector_payload": {
            "generated_at": generated_at,
            "benchmarks": {"metrics": []},
        },
    }


def _write_bundle(
    root: Path,
    *,
    artifact: dict[str, Any],
    inspector: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    artifact_path = root / "scale-10000-durable.json"
    inspector_path = root / "scale-10000-durable-inspector.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    inspector_path.write_text(
        json.dumps(inspector if inspector is not None else artifact["inspector_payload"]),
        encoding="utf-8",
    )
    return artifact_path, inspector_path


def test_validate_measured_bundle_requires_clean_matching_git_and_exact_inspector(
    tmp_path: Path,
) -> None:
    commit = "a" * 40
    artifact_path, inspector_path = _write_bundle(
        tmp_path,
        artifact=_artifact(commit=commit),
    )

    loaded = benchmark_release.validate_measured_bundle(
        artifact_path,
        inspector_path,
        expected_commit=commit,
    )

    assert loaded["provenance"]["git_dirty"] is False

    dirty_path, dirty_inspector = _write_bundle(
        tmp_path / "dirty",
        artifact=_artifact(commit=commit, dirty=True),
    )
    with pytest.raises(benchmark_release.ReleaseEvidenceError, match="clean checkout"):
        benchmark_release.validate_measured_bundle(
            dirty_path,
            dirty_inspector,
            expected_commit=commit,
        )

    stale_path, stale_inspector = _write_bundle(
        tmp_path / "stale",
        artifact=_artifact(commit=commit),
        inspector={"generated_at": "stale", "benchmarks": {"metrics": []}},
    )
    with pytest.raises(benchmark_release.ReleaseEvidenceError, match="Inspector"):
        benchmark_release.validate_measured_bundle(
            stale_path,
            stale_inspector,
            expected_commit=commit,
        )


def test_build_run_metadata_records_artifact_environment_and_timing(tmp_path: Path) -> None:
    commit = "b" * 40
    artifact = _artifact(commit=commit)
    artifact_path, _ = _write_bundle(tmp_path, artifact=artifact)
    started = datetime(2026, 8, 30, 1, 2, 0, tzinfo=UTC)
    ended = started + timedelta(seconds=12.5)
    lock_sha = "c" * 64

    metadata = benchmark_release.build_run_metadata(
        artifact_path=artifact_path,
        repository=_REPOSITORY,
        commit=commit,
        started_at=started,
        ended_at=ended,
        real_seconds=12.5,
        user_cpu_seconds=3.25,
        system_cpu_seconds=1.5,
        uv_version="uv 0.12.5",
        lock_sha256=lock_sha,
        python_executable="/example/python",
    )

    assert metadata["schema_version"] == "recall-origin-benchmark-run/v1"
    assert metadata["artifact"] == artifact_path.name
    assert metadata["artifact_sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    assert metadata["git_commit"] == commit
    assert metadata["environment"]["uv_version"] == "uv 0.12.5"
    assert metadata["environment"]["uv_lock_sha256"] == lock_sha
    assert metadata["environment"]["python_executable"] == "python"
    assert metadata["timing"] == {
        "real_seconds": 12.5,
        "system_cpu_seconds": 1.5,
        "user_cpu_seconds": 3.25,
    }
    assert metadata["timing_scope"] == "benchmark_command"
    assert metadata["started_at_utc"] == started.isoformat()
    assert metadata["ended_at_utc"] == ended.isoformat()
    assert metadata["command"][:4] == ["uv", "run", "python", "scripts/benchmark_release.py"]


def test_validate_run_metadata_rejects_wrong_artifact_or_impossible_timing(tmp_path: Path) -> None:
    commit = "d" * 40
    artifact = _artifact(commit=commit)
    artifact_path, _ = _write_bundle(tmp_path, artifact=artifact)
    started = datetime(2026, 8, 30, 1, 2, 0, tzinfo=UTC)
    ended = started + timedelta(seconds=5)
    metadata = benchmark_release.build_run_metadata(
        artifact_path=artifact_path,
        repository=_REPOSITORY,
        commit=commit,
        started_at=started,
        ended_at=ended,
        real_seconds=5.0,
        user_cpu_seconds=1.0,
        system_cpu_seconds=0.5,
        uv_version="uv 0.12.5",
        lock_sha256="e" * 64,
        python_executable="/example/python",
    )

    benchmark_release.validate_run_metadata(metadata, artifact_path, artifact)

    metadata["artifact_sha256"] = "0" * 64
    with pytest.raises(benchmark_release.ReleaseEvidenceError, match="SHA-256"):
        benchmark_release.validate_run_metadata(metadata, artifact_path, artifact)

    metadata["artifact_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    metadata["ended_at_utc"] = (started - timedelta(seconds=1)).isoformat()
    with pytest.raises(benchmark_release.ReleaseEvidenceError, match="timestamp"):
        benchmark_release.validate_run_metadata(metadata, artifact_path, artifact)


def test_renderer_derives_scale_k_and_durability_labels() -> None:
    summary = {
        "source_artifact_sha256": "f" * 64,
        "scope": {
            "document_count": 100,
            "query_count": 12,
            "k": 5,
            "durable": False,
        },
        "retrieval": {
            "recall_at_k": 0.75,
            "mrr": 0.5,
            "latency_ms": {"p50": 1.0, "p95": 2.0, "p99": 3.0},
            "by_query_type": {},
        },
        "ingest": {"writes_per_second": 42.0},
        "quality": {"passed_count": 4, "case_count": 4},
        "runtime": {"platform": "test-platform"},
    }

    svg = render_summary.render_svg(summary)

    assert "100-document relaxed-durability local benchmark" in svg
    assert "Recall at 5" in svg
    assert "Relaxed-durability ingest" in svg
    assert "10k durable" not in svg


def test_renderer_writes_portable_lf_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "metrics.csv"

    render_summary._write_csv(
        csv_path,
        [
            {
                "category": "retrieval",
                "metric": "MRR",
                "segment": "all",
                "value": 1.0,
                "unit": "ratio",
                "sample_size": 1,
            }
        ],
    )

    payload = csv_path.read_bytes()
    assert b"\r" not in payload
    assert payload.endswith(b"\n")


def test_checksum_manifest_requires_exact_evidence_set(tmp_path: Path) -> None:
    evidence_names = set(benchmark_release.EVIDENCE_FILENAMES) - {"SHA256SUMS"}
    declarations = []
    for filename in sorted(evidence_names):
        path = tmp_path / filename
        path.write_text(filename, encoding="utf-8")
        declarations.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {filename}")
    checksum_path = tmp_path / "SHA256SUMS"
    checksum_path.write_text("\n".join(declarations) + "\n", encoding="utf-8")

    benchmark_release.validate_checksum_manifest(tmp_path)

    checksum_path.write_text("\n".join(declarations[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(benchmark_release.ReleaseEvidenceError, match="exact evidence set"):
        benchmark_release.validate_checksum_manifest(tmp_path)
