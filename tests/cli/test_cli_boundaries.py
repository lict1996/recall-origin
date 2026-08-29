from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from recall_origin.contracts.errors import (
    NOT_FOUND,
    REVISION_CONFLICT,
    TEMPORARY_FAILURE,
    VALIDATION_ERROR,
    ErrorSpec,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLI_SCHEMA = json.loads(
    (REPOSITORY_ROOT / "contracts" / "cli-envelope.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(CLI_SCHEMA)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DATABASE_ENV = "RECALLCTL_DB"
PURGE_KEY_ENV = "RECALL_ORIGIN_PURGE_KEY"


def _environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    environment["NO_COLOR"] = "1"
    environment.pop(DATABASE_ENV, None)
    environment.pop(PURGE_KEY_ENV, None)
    if overrides:
        environment.update(overrides)
    return environment


def _run_cli(
    *arguments: str,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "recall_origin.interfaces.cli", *arguments],
        cwd=cwd or REPOSITORY_ROOT,
        env=_environment(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_for_database(
    database: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_cli(
        *arguments,
        "--db",
        str(database),
        "--json",
        environment=environment,
    )


def _envelope(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert process.stdout.strip(), (
        f"CLI produced no JSON stdout (exit={process.returncode}): {process.stderr}"
    )
    value, end = json.JSONDecoder().raw_decode(process.stdout.lstrip())
    assert not process.stdout.lstrip()[end:].strip(), "stdout must contain exactly one JSON value"
    assert isinstance(value, dict)
    assert not ANSI_ESCAPE.search(process.stdout)
    assert "Traceback (most recent call last)" not in process.stdout
    assert "Traceback (most recent call last)" not in process.stderr
    errors = sorted(VALIDATOR.iter_errors(value), key=lambda error: list(error.path))
    assert errors == [], "\n".join(error.message for error in errors)
    return value


def _assert_success(
    process: subprocess.CompletedProcess[str],
    command: str,
) -> dict[str, Any]:
    envelope = _envelope(process)
    assert process.returncode == 0, process.stderr
    assert envelope["ok"] is True
    assert envelope["command"] == command
    return envelope


def _assert_error(
    process: subprocess.CompletedProcess[str],
    command: str,
    spec: ErrorSpec,
) -> dict[str, Any]:
    envelope = _envelope(process)
    assert process.returncode == spec.exit_code
    assert envelope["ok"] is False
    assert envelope["command"] == command
    assert envelope["error"]["code"] == spec.code
    assert envelope["error"]["retryable"] is spec.retryable
    return envelope


def _initialize(database: Path) -> dict[str, Any]:
    return _assert_success(_run_for_database(database, "init"), "init")


def _remember(database: Path, content: str = "CLI boundary memory") -> dict[str, Any]:
    envelope = _assert_success(
        _run_for_database(
            database,
            "remember",
            content,
            "--scope",
            "workspace:boundary",
            "--external-event-id",
            "cli-boundary-event",
        ),
        "remember",
    )
    return envelope["data"]


def test_config_show_without_option_or_environment_marks_default_source() -> None:
    process = _run_cli("config", "show", "--json")
    envelope = _assert_success(process, "config.show")

    assert envelope["data"]["database"]["source"] == "default"
    assert Path(envelope["data"]["database"]["path"]).is_absolute()
    assert envelope["data"]["environment"][DATABASE_ENV] is None
    assert envelope["data"]["environment"][PURGE_KEY_ENV] is None


def test_config_show_uses_environment_database_and_never_emits_purge_key(
    tmp_path: Path,
) -> None:
    database = tmp_path / "from-environment.sqlite3"
    secret = "boundary-secret-that-must-not-leak"
    process = _run_cli(
        "config",
        "show",
        "--json",
        environment={
            DATABASE_ENV: str(database),
            PURGE_KEY_ENV: secret,
        },
    )
    envelope = _assert_success(process, "config.show")

    assert envelope["data"]["database"] == {
        "path": str(database.resolve()),
        "source": "environment",
    }
    assert envelope["data"]["purge_registry"]["key_source"] == "environment"
    assert envelope["data"]["purge_registry"]["key"] == "<redacted>"
    assert envelope["data"]["environment"][PURGE_KEY_ENV] == "<redacted>"
    assert secret not in process.stdout
    assert secret not in process.stderr


def test_command_database_option_overrides_environment_and_reports_file_key(
    tmp_path: Path,
) -> None:
    environment_database = tmp_path / "environment.sqlite3"
    explicit_database = tmp_path / "explicit.sqlite3"
    _initialize(explicit_database)

    process = _run_for_database(
        explicit_database,
        "config",
        "show",
        environment={DATABASE_ENV: str(environment_database)},
    )
    envelope = _assert_success(process, "config.show")

    assert envelope["data"]["database"] == {
        "path": str(explicit_database.resolve()),
        "source": "option",
    }
    assert envelope["data"]["purge_registry"]["key_source"] == "file"
    assert envelope["data"]["purge_registry"]["key"] == "<redacted>"


def test_config_show_rejects_relative_database_before_creating_it(tmp_path: Path) -> None:
    process = _run_cli(
        "config",
        "show",
        "--db",
        "relative.sqlite3",
        "--json",
        cwd=tmp_path,
    )
    envelope = _assert_error(process, "config.show", VALIDATION_ERROR)

    assert "absolute path" in envelope["error"]["message"]
    assert not (tmp_path / "relative.sqlite3").exists()


def test_parser_error_does_not_treat_global_database_value_as_the_command(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unused.sqlite3"
    process = _run_cli(
        "--db",
        str(database),
        "unknown_command",
        "--json",
    )
    _assert_error(process, "unknown-command", VALIDATION_ERROR)

    assert not database.exists()


def test_remember_requires_scope_before_opening_storage(tmp_path: Path) -> None:
    database = tmp_path / "must-not-be-created.sqlite3"
    process = _run_for_database(
        database,
        "remember",
        "Memory without an authorization scope.",
        "--external-event-id",
        "missing-scope",
    )
    envelope = _assert_error(process, "remember", VALIDATION_ERROR)

    assert envelope["error"]["message"] == "--scope is required"
    assert not database.exists()


def test_doctor_reports_a_healthy_store_without_degraded_metadata(tmp_path: Path) -> None:
    database = tmp_path / "healthy.sqlite3"
    _initialize(database)

    envelope = _assert_success(_run_for_database(database, "doctor"), "doctor")

    assert envelope["data"]["ok"] is True
    assert envelope["data"]["index_drift"] == 0
    assert "degraded" not in envelope["meta"]
    assert "warnings" not in envelope["meta"]


@pytest.mark.parametrize(
    ("arguments", "command"),
    [
        (("doctor",), "doctor"),
        (("reindex", "--dry-run"), "reindex"),
    ],
)
def test_storage_commands_return_safe_error_when_database_is_a_directory(
    tmp_path: Path,
    arguments: tuple[str, ...],
    command: str,
) -> None:
    database_directory = tmp_path / "not-a-database"
    database_directory.mkdir()

    process = _run_for_database(database_directory, *arguments)
    _assert_error(process, command, TEMPORARY_FAILURE)

    assert "Traceback" not in process.stdout
    assert "Traceback" not in process.stderr


def test_govern_compare_and_swap_conflict_preserves_current_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "govern.sqlite3"
    _initialize(database)
    remembered = _remember(database)

    governed = _assert_success(
        _run_for_database(
            database,
            "govern",
            remembered["claim_id"],
            "--expected-revision-id",
            remembered["revision_id"],
            "--action",
            "quarantine",
            "--reason",
            "Review this memory before reuse.",
        ),
        "govern",
    )
    assert governed["data"]["status"] == "quarantined"
    assert governed["data"]["previous_revision_id"] == remembered["revision_id"]

    stale = _run_for_database(
        database,
        "govern",
        remembered["claim_id"],
        "--expected-revision-id",
        remembered["revision_id"],
        "--action",
        "activate",
        "--reason",
        "This stale write must not win.",
    )
    envelope = _assert_error(stale, "govern", REVISION_CONFLICT)

    assert envelope["error"]["details"]["expected_revision_id"] == remembered["revision_id"]
    assert envelope["error"]["details"]["actual_revision_id"] == governed["data"]["revision_id"]


def test_govern_rejects_unknown_action_before_opening_storage(tmp_path: Path) -> None:
    database = tmp_path / "must-not-be-created.sqlite3"
    process = _run_for_database(
        database,
        "govern",
        "mem_unknown",
        "--expected-revision-id",
        "rev_unknown",
        "--action",
        "delete",
        "--reason",
        "Unsupported governance action.",
    )
    envelope = _assert_error(process, "govern", VALIDATION_ERROR)

    assert "GovernAction" in envelope["error"]["message"]
    assert not database.exists()


def test_forget_rejects_unknown_cascade_policy_before_opening_storage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "must-not-be-created.sqlite3"
    process = _run_for_database(
        database,
        "forget",
        "claim:mem_unknown",
        "--scope",
        "workspace:boundary",
        "--idempotency-key",
        "invalid-policy",
        "--cascade-policy",
        "everything",
    )
    envelope = _assert_error(process, "forget", VALIDATION_ERROR)

    assert "safe" in envelope["error"]["message"]
    assert "purge" in envelope["error"]["message"]
    assert not database.exists()


def test_forget_subject_requires_an_explicit_scope(tmp_path: Path) -> None:
    database = tmp_path / "subject-scope.sqlite3"
    _initialize(database)

    process = _run_for_database(
        database,
        "forget",
        "subject:user-1",
        "--idempotency-key",
        "subject-without-scope",
    )
    envelope = _assert_error(process, "forget", NOT_FOUND)

    assert "scope is required for subject deletion" in envelope["error"]["message"]
    assert "scope" not in envelope["meta"]


@pytest.mark.parametrize("command", ["forget-status", "purge"])
def test_deletion_commands_require_a_receipt_identifier(
    tmp_path: Path,
    command: str,
) -> None:
    database = tmp_path / f"{command}.sqlite3"
    _initialize(database)

    process = _run_for_database(database, command)
    envelope = _assert_error(process, command, VALIDATION_ERROR)

    assert envelope["error"]["message"] == "deletion_id is required"


@pytest.mark.parametrize("command", ["forget-status", "purge"])
def test_deletion_commands_report_unknown_receipts_as_not_found(
    tmp_path: Path,
    command: str,
) -> None:
    database = tmp_path / f"{command}.sqlite3"
    _initialize(database)

    process = _run_for_database(database, command, "del_unknown")
    envelope = _assert_error(process, command, NOT_FOUND)

    assert envelope["error"]["message"] == "Deletion was not found."


def test_claim_forget_is_immediate_and_purge_reports_every_managed_layer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "deletion-lifecycle.sqlite3"
    _initialize(database)
    remembered = _remember(database, "sensitive boundary marker")

    forgotten = _assert_success(
        _run_for_database(
            database,
            "forget",
            f"claim:{remembered['claim_id']}",
            "--scope",
            "workspace:boundary",
            "--idempotency-key",
            "forget-boundary-claim",
            "--expected-revision-id",
            remembered["revision_id"],
            "--cascade-policy",
            "purge",
        ),
        "forget",
    )
    deletion_id = forgotten["data"]["deletion_id"]
    assert forgotten["data"]["state"] == "logically_hidden"
    assert forgotten["meta"]["scope"] == "workspace:boundary"
    assert forgotten["data"]["external_copies"]
    assert "outside engine control" in " ".join(forgotten["data"]["external_copies"])

    searched = _assert_success(
        _run_for_database(
            database,
            "search",
            "sensitive boundary marker",
            "--scope",
            "workspace:boundary",
        ),
        "search",
    )
    assert searched["data"]["hits"] == []

    status = _assert_success(
        _run_for_database(database, "forget-status", deletion_id),
        "forget-status",
    )
    assert status["data"]["state"] == "logically_hidden"

    purged = _assert_success(
        _run_for_database(database, "purge", deletion_id),
        "purge",
    )
    assert purged["data"]["state"] == "completed"
    assert {layer["state"] for layer in purged["data"]["layers"]} == {"completed"}

    repeated = _assert_success(
        _run_for_database(database, "purge", deletion_id),
        "purge",
    )
    assert repeated["data"]["state"] == "completed"


def test_reindex_dry_run_and_apply_report_their_distinct_effects(tmp_path: Path) -> None:
    database = tmp_path / "reindex.sqlite3"
    _initialize(database)
    _remember(database)

    dry_run = _assert_success(
        _run_for_database(database, "reindex", "--dry-run"),
        "reindex",
    )
    assert dry_run["data"] == {
        "currently_indexed": 1,
        "dry_run": True,
        "would_index": 1,
    }

    applied = _assert_success(_run_for_database(database, "reindex"), "reindex")
    assert applied["data"] == {
        "considered": 1,
        "dry_run": False,
        "indexed": 1,
    }
