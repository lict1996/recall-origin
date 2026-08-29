from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import (
    IDEMPOTENCY_KEY_REUSED,
    NOT_FOUND,
    REVISION_CONFLICT,
)
from recall_origin.contracts.v1 import (
    ContextQuery,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _cli(database: Path, *arguments: str) -> tuple[int, dict[str, Any], str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "recall_origin.interfaces.cli",
            *arguments,
            "--db",
            str(database),
            "--json",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.stdout.strip(), (
        f"CLI produced no JSON stdout (exit={process.returncode}): {process.stderr}"
    )
    decoder = json.JSONDecoder()
    envelope, end = decoder.raw_decode(process.stdout.lstrip())
    assert not process.stdout.lstrip()[end:].strip()
    assert isinstance(envelope, dict)
    return process.returncode, envelope, process.stderr


def test_core_error_codes_fit_the_cli_v1_contract() -> None:
    schema = json.loads(
        (REPOSITORY_ROOT / "contracts" / "cli-envelope.schema.json").read_text(encoding="utf-8")
    )
    cli_codes = set(schema["$defs"]["Error"]["properties"]["code"]["enum"])

    assert {
        NOT_FOUND.code,
        REVISION_CONFLICT.code,
        IDEMPOTENCY_KEY_REUSED.code,
    } <= cli_codes


def test_sdk_and_cli_share_remember_search_and_context_semantics(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("parity")
    sdk = MemoryEngine.local(tmp_path / "sdk.sqlite3", token_counter=len).initialize()
    sdk_receipt = sdk.remember(
        RememberRequest(
            content="release after CI passes",
            scope=scope,
            external_event_id="parity-event",
            origin=OriginContext(producer_id="parity"),
        )
    )
    sdk_hits = sdk.search(SearchRequest(query="release CI", scope=scope))
    sdk_pack = sdk.context(
        ContextQuery(
            query="release CI",
            scope=scope,
            token_budget=800,
        )
    )

    cli_database = tmp_path / "cli.sqlite3"
    init_code, init_envelope, init_stderr = _cli(cli_database, "init")
    assert init_code == 0, init_stderr
    assert init_envelope["ok"] is True
    remember_code, remembered, remember_stderr = _cli(
        cli_database,
        "remember",
        "release after CI passes",
        "--scope",
        scope.serialize(),
        "--external-event-id",
        "parity-event",
    )
    assert remember_code == 0, remember_stderr
    search_code, searched, search_stderr = _cli(
        cli_database,
        "search",
        "release CI",
        "--scope",
        scope.serialize(),
    )
    assert search_code == 0, search_stderr
    context_code, context, context_stderr = _cli(
        cli_database,
        "context",
        "release CI",
        "--scope",
        scope.serialize(),
        "--token-budget",
        "800",
    )
    assert context_code == 0, context_stderr

    assert remembered["data"]["status"] == sdk_receipt.status.value
    assert remembered["data"]["confirmation"] == sdk_receipt.confirmation.value
    assert remembered["data"]["source_count"] == sdk_receipt.source_count
    assert remembered["data"]["index_state"] == sdk_receipt.index_state.value
    assert [item["content"] for item in searched["data"]["hits"]] == [
        item.content for item in sdk_hits
    ]
    assert [item["content"] for item in context["data"]["items"]] == [
        item.content for item in sdk_pack.items
    ]
    assert context["data"]["token_count"] <= context["data"]["token_budget"]
