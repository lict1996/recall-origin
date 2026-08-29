#!/usr/bin/env python3
"""Generate RecallOrigin's complete, fail-closed 10k release evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

RUN_SCHEMA_VERSION = "recall-origin-benchmark-run/v1"
ARTIFACT_SCHEMA_VERSION = "recall-origin-benchmark/v1"
SCALE = 10_000
SEED = 20_260_830
QUERY_COUNT = 250
K = 10
BASE_NAME = "scale-10000-durable"
EVIDENCE_FILENAMES = (
    f"{BASE_NAME}.json",
    f"{BASE_NAME}-inspector.json",
    f"{BASE_NAME}-run.json",
    f"{BASE_NAME}-summary.json",
    f"{BASE_NAME}-metrics.csv",
    f"{BASE_NAME}-summary.svg",
    "SHA256SUMS",
)


class ReleaseEvidenceError(RuntimeError):
    """Raised when release evidence is incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class GitState:
    commit: str
    dirty: bool


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseEvidenceError(message)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseEvidenceError(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseEvidenceError(f"JSON evidence {path} must contain an object")
    return value


def _run_text(command: list[str], *, repository: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise ReleaseEvidenceError(f"{' '.join(command)} failed: {detail}")
    return completed.stdout.strip()


def git_state(repository: Path) -> GitState:
    """Read the exact commit and worktree state used for a release measurement."""
    commit = _run_text(["git", "rev-parse", "--verify", "HEAD"], repository=repository)
    dirty_output = _run_text(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        repository=repository,
    )
    _require(bool(commit), "release benchmark requires an available Git commit")
    return GitState(commit=commit, dirty=bool(dirty_output))


def validate_measured_bundle(
    artifact_path: Path,
    inspector_path: Path,
    *,
    expected_commit: str,
) -> dict[str, Any]:
    """Validate raw evidence before any release-facing derived asset is produced."""
    artifact = _load_json(artifact_path)
    inspector = _load_json(inspector_path)
    _require(
        artifact.get("schema_version") == ARTIFACT_SCHEMA_VERSION,
        f"artifact must use {ARTIFACT_SCHEMA_VERSION}",
    )
    configuration = artifact.get("configuration")
    _require(isinstance(configuration, dict), "artifact configuration is missing")
    configuration = cast(dict[str, Any], configuration)
    engine = configuration.get("engine")
    _require(isinstance(engine, dict), "artifact engine configuration is missing")
    engine = cast(dict[str, Any], engine)
    _require(configuration.get("scale") == SCALE, f"release scale must be {SCALE}")
    _require(configuration.get("seed") == SEED, f"release seed must be {SEED}")
    _require(
        configuration.get("query_count") == QUERY_COUNT,
        f"release query count must be {QUERY_COUNT}",
    )
    _require(engine.get("search_limit_k") == K, f"release search limit must be {K}")
    _require(engine.get("durable") is True, "release benchmark must use durable SQLite")
    quality = artifact.get("quality")
    _require(
        isinstance(quality, dict) and quality.get("passed") is True,
        "release benchmark quality cases did not pass",
    )
    provenance = artifact.get("provenance")
    _require(isinstance(provenance, dict), "artifact provenance is missing")
    provenance = cast(dict[str, Any], provenance)
    _require(
        provenance.get("git_commit_available") is True,
        "release benchmark requires an available Git commit",
    )
    _require(
        provenance.get("git_dirty") is False,
        "release benchmark must be measured from a clean checkout",
    )
    _require(
        provenance.get("git_commit") == expected_commit,
        "artifact Git commit does not match the measured checkout",
    )
    source_snapshot = provenance.get("source_snapshot")
    _require(
        isinstance(source_snapshot, dict) and source_snapshot.get("stable_during_run") is True,
        "source snapshot changed during the release benchmark",
    )
    _require(
        inspector == artifact.get("inspector_payload"),
        "Inspector projection does not exactly match the canonical artifact",
    )
    return artifact


def _portable_output_path(repository: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repository.resolve()).as_posix()
    except ValueError:
        return path.name


def build_run_metadata(
    *,
    artifact_path: Path,
    repository: Path,
    commit: str,
    started_at: datetime,
    ended_at: datetime,
    real_seconds: float,
    user_cpu_seconds: float,
    system_cpu_seconds: float,
    uv_version: str,
    lock_sha256: str,
    python_executable: str,
    published_output_dir: Path | None = None,
) -> dict[str, Any]:
    """Build portable metadata for the exact measured child process."""
    output_dir = published_output_dir or artifact_path.parent
    artifact_output = output_dir / f"{BASE_NAME}.json"
    inspector_output = output_dir / f"{BASE_NAME}-inspector.json"
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "artifact": artifact_path.name,
        "artifact_sha256": _sha256(artifact_path),
        "git_commit": commit,
        "command": [
            "uv",
            "run",
            "python",
            "scripts/benchmark_release.py",
        ],
        "benchmark_command": [
            "python",
            "-m",
            "recall_origin.benchmarks.runner",
            "--scale",
            str(SCALE),
            "--seed",
            str(SEED),
            "--query-count",
            str(QUERY_COUNT),
            "--k",
            str(K),
            "--output",
            _portable_output_path(repository, artifact_output),
            "--inspector-output",
            _portable_output_path(repository, inspector_output),
        ],
        "started_at_utc": started_at.astimezone(UTC).isoformat(),
        "ended_at_utc": ended_at.astimezone(UTC).isoformat(),
        "started_at_local": started_at.astimezone().isoformat(),
        "ended_at_local": ended_at.astimezone().isoformat(),
        "timing": {
            "real_seconds": round(real_seconds, 6),
            "system_cpu_seconds": round(system_cpu_seconds, 6),
            "user_cpu_seconds": round(user_cpu_seconds, 6),
        },
        "timing_scope": "benchmark_command",
        "timing_tool": "time.perf_counter + resource.getrusage(RUSAGE_CHILDREN)",
        "environment": {
            "python_executable": Path(python_executable).name,
            "python_version": platform.python_version(),
            "uv_version": uv_version,
            "uv_lock_sha256": lock_sha256,
        },
    }


def validate_run_metadata(
    metadata: dict[str, Any],
    artifact_path: Path,
    artifact: dict[str, Any],
) -> None:
    """Reject metadata that cannot describe the supplied canonical artifact."""
    _require(
        metadata.get("schema_version") == RUN_SCHEMA_VERSION,
        f"run metadata must use {RUN_SCHEMA_VERSION}",
    )
    _require(metadata.get("artifact") == artifact_path.name, "run metadata artifact name differs")
    _require(
        metadata.get("artifact_sha256") == _sha256(artifact_path),
        "run metadata artifact SHA-256 differs from the canonical artifact",
    )
    provenance = artifact.get("provenance")
    _require(isinstance(provenance, dict), "artifact provenance is missing")
    provenance = cast(dict[str, Any], provenance)
    _require(
        metadata.get("git_commit") == provenance.get("git_commit"),
        "run metadata Git commit differs from the canonical artifact",
    )
    try:
        started_at = datetime.fromisoformat(str(metadata["started_at_utc"]))
        ended_at = datetime.fromisoformat(str(metadata["ended_at_utc"]))
        started_at_local = datetime.fromisoformat(str(metadata["started_at_local"]))
        ended_at_local = datetime.fromisoformat(str(metadata["ended_at_local"]))
        generated_at = datetime.fromisoformat(str(artifact["generated_at_utc"]))
    except (KeyError, ValueError) as error:
        raise ReleaseEvidenceError(f"invalid release run timestamp: {error}") from error
    _require(
        all(
            value.utcoffset() is not None
            for value in (started_at, ended_at, started_at_local, ended_at_local, generated_at)
        ),
        "release run timestamps must include UTC offsets",
    )
    _require(
        started_at <= generated_at <= ended_at,
        "release run timestamp order is impossible",
    )
    _require(
        started_at_local.astimezone(UTC) == started_at.astimezone(UTC)
        and ended_at_local.astimezone(UTC) == ended_at.astimezone(UTC),
        "release run local and UTC timestamps differ",
    )
    timing = metadata.get("timing")
    _require(isinstance(timing, dict), "release run timing is missing")
    timing = cast(dict[str, Any], timing)
    real_seconds = timing.get("real_seconds")
    _require(
        isinstance(real_seconds, int | float) and real_seconds >= 0,
        "release run real_seconds must be non-negative",
    )
    real_seconds_value = cast(int | float, real_seconds)
    for field in ("user_cpu_seconds", "system_cpu_seconds"):
        cpu_seconds = timing.get(field)
        _require(
            isinstance(cpu_seconds, int | float) and cpu_seconds >= 0,
            f"release run {field} must be non-negative",
        )
    elapsed_seconds = (ended_at - started_at).total_seconds()
    _require(
        abs(float(real_seconds_value) - elapsed_seconds) <= max(1.0, elapsed_seconds * 0.01),
        "release run wall time differs from its timestamps",
    )
    performance = artifact.get("performance")
    if isinstance(performance, dict):
        ingest = performance.get("ingest")
        if isinstance(ingest, dict):
            ingest_seconds = ingest.get("duration_seconds")
            _require(
                isinstance(ingest_seconds, int | float),
                "canonical artifact ingest duration is missing",
            )
            _require(
                float(real_seconds_value) >= float(cast(int | float, ingest_seconds)),
                "release run wall time is shorter than measured ingest",
            )
    environment = metadata.get("environment")
    _require(isinstance(environment, dict), "release run environment is missing")
    environment = cast(dict[str, Any], environment)
    _require(bool(environment.get("uv_version")), "release run uv version is missing")
    lock_sha256 = environment.get("uv_lock_sha256")
    _require(
        isinstance(lock_sha256, str) and len(lock_sha256) == 64,
        "release run uv.lock SHA-256 is invalid",
    )


def validate_checksum_manifest(directory: Path) -> None:
    """Require the exact release evidence set and verify every declared digest."""
    checksum_path = directory / "SHA256SUMS"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ReleaseEvidenceError(f"cannot read {checksum_path}: {error}") from error
    declarations: dict[str, str] = {}
    for line in lines:
        try:
            digest, filename = line.split("  ", maxsplit=1)
        except ValueError as error:
            raise ReleaseEvidenceError(f"malformed checksum declaration: {line!r}") from error
        _require(Path(filename).name == filename, "checksum filenames must be local basenames")
        _require(filename not in declarations, f"duplicate checksum declaration for {filename}")
        declarations[filename] = digest
    expected = set(EVIDENCE_FILENAMES) - {"SHA256SUMS"}
    _require(
        set(declarations) == expected, "checksum manifest does not cover the exact evidence set"
    )
    for filename, digest in declarations.items():
        _require(
            _sha256(directory / filename) == digest,
            f"checksum mismatch for {filename}",
        )


def _publish_staged_bundle(staging: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered_names = [name for name in EVIDENCE_FILENAMES if name != "SHA256SUMS"]
    ordered_names.append("SHA256SUMS")
    for filename in ordered_names:
        source = staging / filename
        _require(source.is_file(), f"staged evidence is missing {filename}")
        temporary_target = output_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(source, temporary_target)
            os.replace(temporary_target, output_dir / filename)
        finally:
            temporary_target.unlink(missing_ok=True)


def run_release_benchmark(repository: Path, output_dir: Path) -> dict[str, Any]:
    """Measure, validate, render, and atomically publish one official evidence set."""
    repository = repository.resolve()
    output_dir = output_dir.resolve()
    before = git_state(repository)
    _require(not before.dirty, "release benchmark must start from a clean checkout")
    lock_path = repository / "uv.lock"
    _require(lock_path.is_file(), "release benchmark requires uv.lock")
    uv_binary = shutil.which("uv")
    _require(uv_binary is not None, "release benchmark requires uv")
    assert uv_binary is not None
    uv_version = _run_text([uv_binary, "--version"], repository=repository)
    lock_sha256 = _sha256(lock_path)

    with tempfile.TemporaryDirectory(prefix="recall-origin-release-benchmark-") as temporary:
        staging = Path(temporary)
        artifact_path = staging / f"{BASE_NAME}.json"
        inspector_path = staging / f"{BASE_NAME}-inspector.json"
        runner_command = [
            sys.executable,
            "-m",
            "recall_origin.benchmarks.runner",
            "--scale",
            str(SCALE),
            "--seed",
            str(SEED),
            "--query-count",
            str(QUERY_COUNT),
            "--k",
            str(K),
            "--output",
            str(artifact_path),
            "--inspector-output",
            str(inspector_path),
        ]
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started_at = datetime.now(UTC)
        started_counter = time.perf_counter()
        completed = subprocess.run(runner_command, cwd=repository, check=False)
        real_seconds = time.perf_counter() - started_counter
        ended_at = datetime.now(UTC)
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        if completed.returncode != 0:
            raise ReleaseEvidenceError(
                f"benchmark runner failed with exit code {completed.returncode}; "
                "existing release evidence was not replaced"
            )

        after = git_state(repository)
        _require(after == before, "Git HEAD or checkout state changed during benchmark")
        _require(_sha256(lock_path) == lock_sha256, "uv.lock changed during benchmark")
        artifact = validate_measured_bundle(
            artifact_path,
            inspector_path,
            expected_commit=before.commit,
        )
        metadata = build_run_metadata(
            artifact_path=artifact_path,
            repository=repository,
            commit=before.commit,
            started_at=started_at,
            ended_at=ended_at,
            real_seconds=real_seconds,
            user_cpu_seconds=usage_after.ru_utime - usage_before.ru_utime,
            system_cpu_seconds=usage_after.ru_stime - usage_before.ru_stime,
            uv_version=uv_version,
            lock_sha256=lock_sha256,
            python_executable=sys.executable,
            published_output_dir=output_dir,
        )
        validate_run_metadata(metadata, artifact_path, artifact)
        (staging / f"{BASE_NAME}-run.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        renderer_command = [
            sys.executable,
            str(repository / "scripts" / "render_benchmark_summary.py"),
            str(artifact_path),
        ]
        renderer = subprocess.run(renderer_command, cwd=repository, check=False)
        if renderer.returncode != 0:
            raise ReleaseEvidenceError(
                f"benchmark renderer failed with exit code {renderer.returncode}; "
                "existing release evidence was not replaced"
            )
        validate_checksum_manifest(staging)
        _publish_staged_bundle(staging, output_dir)
        return metadata


def _parser() -> argparse.ArgumentParser:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Run the official 10k durable RecallOrigin benchmark from a clean commit and "
            "publish one internally consistent evidence bundle."
        )
    )
    parser.add_argument("--repository", type=Path, default=repository)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repository / "docs" / "benchmarks" / "results",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        metadata = run_release_benchmark(args.repository, args.output_dir)
    except ReleaseEvidenceError as error:
        print(f"release benchmark refused: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "artifact": metadata["artifact"],
                "artifact_sha256": metadata["artifact_sha256"],
                "git_commit": metadata["git_commit"],
                "run_metadata_schema": metadata["schema_version"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
