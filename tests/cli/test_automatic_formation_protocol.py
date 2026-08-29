from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from jsonschema import Draft202012Validator

from recall_origin import MemoryEngine
from recall_origin.contracts.v1 import (
    CaptureRequest,
    OriginContext,
    PartitionRef,
    RememberRequest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLI_SCHEMA = json.loads(
    (REPOSITORY_ROOT / "contracts" / "cli-envelope.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(CLI_SCHEMA)


def _run(
    database: Path,
    *arguments: str,
    db_position: Literal["global", "command"] = "command",
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    environment["NO_COLOR"] = "1"
    command = [sys.executable, "-m", "recall_origin.interfaces.cli"]
    if db_position == "global":
        command.extend(("--db", str(database)))
    command.extend(arguments)
    if db_position == "command":
        command.extend(("--db", str(database)))
    command.append("--json")
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _envelope(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert process.stdout.strip(), (
        f"CLI produced no JSON stdout (exit={process.returncode}): {process.stderr}"
    )
    value, end = json.JSONDecoder().raw_decode(process.stdout.lstrip())
    assert not process.stdout.lstrip()[end:].strip(), "stdout must contain exactly one JSON value"
    assert isinstance(value, dict)
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


def _initialize(database: Path) -> None:
    _assert_success(_run(database, "init"), "init")


def _structured_payload(content: str) -> str:
    return json.dumps(
        {
            "memory_candidates": [
                {
                    "operation": "add",
                    "kind": "semantic",
                    "subtype": "fact",
                    "memory_key": "cli.automatic",
                    "content": content,
                    "reason": "CLI automatic formation contract.",
                }
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize("db_position", ["global", "command"])
def test_capture_string_payload_accepts_global_and_command_database_options(
    tmp_path: Path,
    db_position: Literal["global", "command"],
) -> None:
    database = tmp_path / f"{db_position}.sqlite3"
    _initialize(database)

    envelope = _assert_success(
        _run(
            database,
            "capture",
            "plain host observation",
            "--scope",
            "workspace:alpha",
            "--external-event-id",
            f"string-{db_position}",
            "--event-type",
            "host_observation",
            db_position=db_position,
        ),
        "capture",
    )

    assert envelope["meta"]["scope"] == "workspace:alpha"
    assert envelope["data"]["status"] == "accepted_pending"
    assert envelope["data"]["event_id"]
    assert envelope["data"]["job_id"]
    assert envelope["data"]["formation_version"]
    assert envelope["data"]["replayed"] is False


def test_capture_json_subjects_and_idempotent_replay(tmp_path: Path) -> None:
    database = tmp_path / "capture-json.sqlite3"
    _initialize(database)
    arguments = (
        "capture",
        _structured_payload("CLI remembers structured JSON."),
        "--payload-json",
        "--scope",
        "workspace:alpha",
        "--external-event-id",
        "json-replay",
        "--subject",
        "user:42",
        "--subject",
        "repository:recall-origin",
    )

    first = _assert_success(_run(database, *arguments), "capture")
    replay = _assert_success(_run(database, *arguments), "capture")

    assert replay["data"]["replayed"] is True
    assert replay["data"]["event_id"] == first["data"]["event_id"]
    assert replay["data"]["job_id"] == first["data"]["job_id"]
    with MemoryEngine.local(database).initialize().store.connection() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM outbox_messages WHERE outbox_id = ?",
                (first["data"]["job_id"],),
            ).fetchone()[0]
        )
    assert payload["subjects"] == [
        {"subject_type": "user", "subject_id": "42"},
        {"subject_type": "repository", "subject_id": "recall-origin"},
    ]


def test_capture_no_persist_returns_no_store_without_durable_rows(tmp_path: Path) -> None:
    database = tmp_path / "no-store.sqlite3"
    _initialize(database)

    envelope = _assert_success(
        _run(
            database,
            "capture",
            "transient observation",
            "--scope",
            "workspace:alpha",
            "--external-event-id",
            "no-store",
            "--no-persist",
        ),
        "capture",
    )

    assert envelope["data"]["status"] == "no_store"
    assert envelope["data"]["event_id"] is None
    assert envelope["data"]["job_id"] is None
    with MemoryEngine.local(database).initialize().store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == 0


def test_capture_invalid_json_has_stable_validation_envelope(tmp_path: Path) -> None:
    database = tmp_path / "invalid-json.sqlite3"
    _initialize(database)

    process = _run(
        database,
        "capture",
        "{not-json",
        "--payload-json",
        "--scope",
        "workspace:alpha",
        "--external-event-id",
        "invalid-json",
    )
    envelope = _envelope(process)

    assert process.returncode != 0
    assert envelope["ok"] is False
    assert envelope["command"] == "capture"
    assert envelope["error"]["code"] == "VALIDATION_ERROR"
    assert envelope["error"]["retryable"] is False


def test_formation_processes_a_specific_job_with_the_structured_provider(
    tmp_path: Path,
) -> None:
    database = tmp_path / "process.sqlite3"
    _initialize(database)
    captured = _assert_success(
        _run(
            database,
            "capture",
            _structured_payload("process this exact job"),
            "--payload-json",
            "--scope",
            "workspace:alpha",
            "--external-event-id",
            "process-job",
        ),
        "capture",
    )

    processed = _assert_success(
        _run(
            database,
            "formation-process",
            "--job-id",
            captured["data"]["job_id"],
        ),
        "formation-process",
    )

    assert processed["data"]["processed"] is True
    assert processed["data"]["job"]["job_id"] == captured["data"]["job_id"]
    assert processed["data"]["job"]["status"] == "done"
    assert len(processed["data"]["job"]["committed_claim_ids"]) == 1


def test_formation_process_without_available_work_is_a_successful_noop(
    tmp_path: Path,
) -> None:
    database = tmp_path / "no-work.sqlite3"
    _initialize(database)

    envelope = _assert_success(
        _run(database, "formation-process"),
        "formation-process",
    )

    assert envelope["data"] == {"processed": False, "job": None}


def test_formation_status_reports_pending_then_done(tmp_path: Path) -> None:
    database = tmp_path / "status.sqlite3"
    engine = MemoryEngine.local(database).initialize()
    captured = engine.capture(
        CaptureRequest(
            scope=PartitionRef.workspace("alpha"),
            external_event_id="status-job",
            event_type="host_observation",
            payload=json.loads(_structured_payload("status candidate")),
            origin=OriginContext(producer_id="status-test"),
        )
    )

    pending = _assert_success(
        _run(database, "formation-status", captured.job_id or ""),
        "formation-status",
    )
    assert pending["data"]["job_id"] == captured.job_id
    assert pending["data"]["event_id"] == captured.event_id
    assert pending["data"]["status"] == "pending"
    assert pending["data"]["attempt"] == 0

    _assert_success(
        _run(database, "formation-process", "--job-id", captured.job_id or ""),
        "formation-process",
    )
    done = _assert_success(
        _run(database, "formation-status", captured.job_id or ""),
        "formation-status",
    )
    assert done["data"]["status"] == "done"
    assert done["data"]["attempt"] == 1


def test_unknown_formation_status_has_stable_not_found_error(tmp_path: Path) -> None:
    database = tmp_path / "missing-status.sqlite3"
    _initialize(database)

    process = _run(database, "formation-status", "job_missing")
    envelope = _envelope(process)

    assert process.returncode != 0
    assert envelope["ok"] is False
    assert envelope["command"] == "formation-status"
    assert envelope["error"]["code"] == "NOT_FOUND"
    assert envelope["error"]["retryable"] is False


def test_feedback_appends_without_changing_the_claim_revision(tmp_path: Path) -> None:
    database = tmp_path / "feedback.sqlite3"
    engine = MemoryEngine.local(database).initialize()
    remembered = engine.remember(
        RememberRequest(
            content="Feedback must not govern a memory.",
            scope=PartitionRef.workspace("alpha"),
            external_event_id="feedback-memory",
            origin=OriginContext(producer_id="feedback-test"),
        )
    )

    envelope = _assert_success(
        _run(
            database,
            "feedback",
            remembered.claim_id,
            "--revision-id",
            remembered.revision_id,
            "--type",
            "helpful",
            "--reason",
            "Useful during CLI integration.",
        ),
        "feedback",
    )

    assert envelope["data"]["feedback_id"]
    assert envelope["data"]["claim_id"] == remembered.claim_id
    assert envelope["data"]["revision_id"] == remembered.revision_id
    assert envelope["data"]["appended"] is True
    assert MemoryEngine.local(database).initialize().get(remembered.claim_id).revision_id == (
        remembered.revision_id
    )
