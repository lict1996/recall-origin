from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from recall_origin.contracts.errors import IDEMPOTENCY_KEY_REUSED

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLI_SCHEMA = json.loads(
    (REPOSITORY_ROOT / "contracts" / "cli-envelope.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(CLI_SCHEMA)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _run_cli(
    database: Path,
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    environment["NO_COLOR"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "recall_origin.interfaces.cli",
            *arguments,
            "--db",
            str(database),
            "--json",
        ],
        cwd=cwd or REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_raw_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    environment["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "recall_origin.interfaces.cli", *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _decode_single_json(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert process.stdout.strip(), (
        f"CLI produced no JSON stdout (exit={process.returncode}): {process.stderr}"
    )
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(process.stdout.lstrip())
    assert not process.stdout.lstrip()[end:].strip(), "stdout contained more than one JSON value"
    assert isinstance(value, dict)
    assert not ANSI_ESCAPE.search(process.stdout)
    assert "Traceback (most recent call last)" not in process.stdout
    assert "Traceback (most recent call last)" not in process.stderr
    errors = sorted(VALIDATOR.iter_errors(value), key=lambda error: list(error.path))
    assert errors == [], "\n".join(error.message for error in errors)
    return value


def _initialize(database: Path, *, cwd: Path | None = None) -> dict[str, Any]:
    process = _run_cli(database, "init", cwd=cwd)
    envelope = _decode_single_json(process)
    assert process.returncode == 0
    assert envelope["ok"] is True
    assert envelope["command"] == "init"
    return envelope


def test_cli_json_success_uses_stdout_only_for_one_versioned_envelope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory with spaces.sqlite3"
    _initialize(database)

    process = _run_cli(
        database,
        "remember",
        "用户偏好中文回答",
        "--scope",
        "workspace:alpha",
        "--external-event-id",
        "cli-event-1",
    )
    envelope = _decode_single_json(process)

    assert process.returncode == 0
    assert envelope["contract_version"] == "1"
    assert envelope["ok"] is True
    assert envelope["command"] == "remember"
    assert envelope["meta"]["scope"] == "workspace:alpha"
    assert envelope["data"]["source_count"] == 1
    assert envelope["data"]["replayed"] is False


def test_cli_json_error_preserves_the_application_error_code_and_exit_code(
    tmp_path: Path,
) -> None:
    database = tmp_path / "idempotency.sqlite3"
    _initialize(database)
    first = _run_cli(
        database,
        "remember",
        "first payload",
        "--scope",
        "workspace:alpha",
        "--external-event-id",
        "same-event",
    )
    assert first.returncode == 0, first.stderr

    mismatch = _run_cli(
        database,
        "remember",
        "changed payload",
        "--scope",
        "workspace:alpha",
        "--external-event-id",
        "same-event",
    )
    envelope = _decode_single_json(mismatch)

    assert mismatch.returncode == IDEMPOTENCY_KEY_REUSED.exit_code
    assert envelope["ok"] is False
    assert envelope["command"] == "remember"
    assert envelope["error"]["code"] == IDEMPOTENCY_KEY_REUSED.code
    assert envelope["error"]["retryable"] is False


@pytest.mark.parametrize(
    ("arguments", "command"),
    [
        (("unknown", "--json"), "unknown"),
        (("remember", "value", "--scope", "workspace:a", "--bogus", "--json"), "remember"),
        (("init", "--json", "--db"), "init"),
        (("remember", "value", "--scope", "--json"), "remember"),
        (("init", "--help", "--json"), "init"),
    ],
)
def test_json_mode_wraps_parser_level_errors_in_one_stdout_envelope(
    arguments: tuple[str, ...],
    command: str,
) -> None:
    process = _run_raw_cli(*arguments)
    envelope = _decode_single_json(process)

    assert process.returncode == 2
    assert process.stderr == ""
    assert envelope["ok"] is False
    assert envelope["command"] == command
    assert envelope["error"]["code"] == "VALIDATION_ERROR"


def test_non_json_parser_errors_keep_human_stderr_behavior() -> None:
    process = _run_raw_cli("unknown")

    assert process.returncode == 2
    assert process.stdout == ""
    assert "No such command 'unknown'" in process.stderr


def test_explicit_database_path_is_independent_of_the_calling_directory(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.sqlite3"
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    _initialize(database, cwd=first_cwd)
    remembered = _run_cli(
        database,
        "remember",
        "cwd independent needle",
        "--scope",
        "workspace:alpha",
        "--external-event-id",
        "cwd-event",
        cwd=first_cwd,
    )
    assert remembered.returncode == 0, remembered.stderr

    searched = _run_cli(
        database,
        "search",
        "independent needle",
        "--scope",
        "workspace:alpha",
        cwd=second_cwd,
    )
    envelope = _decode_single_json(searched)

    assert searched.returncode == 0
    assert [item["content"] for item in envelope["data"]["hits"]] == ["cwd independent needle"]


def test_cli_evidence_context_exports_an_explicit_unmanaged_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "evidence.sqlite3"
    export = tmp_path / "exported-pack"
    _initialize(database)
    remembered = _run_cli(
        database,
        "remember",
        "Evidence mode exports a verifiable Inspector.",
        "--scope",
        "workspace:alpha",
        "--external-event-id",
        "evidence-cli-event",
    )
    assert remembered.returncode == 0, remembered.stderr

    process = _run_cli(
        database,
        "context",
        "verifiable Inspector",
        "--scope",
        "workspace:alpha",
        "--mode",
        "evidence",
        "--ttl-seconds",
        "60",
        "--out",
        str(export),
    )
    envelope = _decode_single_json(process)

    assert process.returncode == 0, process.stderr
    assert envelope["data"]["mode"] == "evidence"
    assert envelope["data"]["exported_to"] == str(export)
    assert envelope["data"]["export_managed"] is False
    assert envelope["data"]["integrity_sha256"]
    assert any(uri.endswith("/inspector.html") for uri in envelope["data"]["resource_uris"])
    assert (export / "manifest.json").is_file()
    assert (export / "inspector.html").is_file()
    assert "unmanaged external copy" in " ".join(envelope["meta"]["warnings"])


def test_cli_fast_context_rejects_export_destination(tmp_path: Path) -> None:
    database = tmp_path / "fast-out.sqlite3"
    _initialize(database)

    process = _run_cli(
        database,
        "context",
        "anything",
        "--scope",
        "workspace:alpha",
        "--out",
        str(tmp_path / "must-not-exist"),
    )
    envelope = _decode_single_json(process)

    assert process.returncode != 0
    assert envelope["error"]["code"] == "VALIDATION_ERROR"
    assert not (tmp_path / "must-not-exist").exists()
