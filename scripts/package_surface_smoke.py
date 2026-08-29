"""Exercise every advertised package surface from a clean wheel installation."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import recall_origin
from recall_origin import (
    MemoryEngine,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)
from recall_origin.contracts.v1 import PrincipalContext
from recall_origin.domain.enums import Capability, PrincipalType
from recall_origin.interfaces.http import create_app
from recall_origin.interfaces.mcp import MCPServerConfig, create_mcp_server


def _run_cli(database: Path, *arguments: str) -> dict[str, Any]:
    executable = Path(sys.executable).with_name("recallctl")
    if not executable.is_file():
        raise RuntimeError("The recallctl console script is not installed.")
    process = subprocess.run(
        [str(executable), *arguments, "--db", str(database), "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if process.returncode != 0 or process.stderr:
        raise RuntimeError(
            f"recallctl failed with exit {process.returncode}: "
            f"stdout={process.stdout!r} stderr={process.stderr!r}"
        )
    value: object = json.loads(process.stdout)
    command = arguments[0]
    if (
        not isinstance(value, dict)
        or value.get("contract_version") != "1"
        or value.get("command") != command
        or value.get("ok") is not True
        or not isinstance(value.get("data"), dict)
    ):
        raise RuntimeError(f"recallctl returned an invalid envelope: {value!r}")
    return value


def _require_nonempty_pack(directory: Path, *, label: str) -> None:
    required_files = ("manifest.json", "MANIFEST.md", "retrieval.json", "inspector.html")
    if not directory.is_dir():
        raise RuntimeError(f"The installed CLI did not create the {label} directory: {directory}")
    invalid_files = [
        name
        for name in required_files
        if not (directory / name).is_file() or (directory / name).stat().st_size == 0
    ]
    if invalid_files:
        raise RuntimeError(
            f"The installed CLI created an incomplete {label}: {sorted(invalid_files)!r}"
        )


def _smoke_python(directory: Path) -> None:
    scope = PartitionRef.workspace("python-smoke")
    with MemoryEngine.local(directory / "python.sqlite3") as engine:
        receipt = engine.remember(
            RememberRequest(
                content="The package wheel exposes the Python API.",
                scope=scope,
                external_event_id="package-python-smoke",
                idempotency_key="package-python-smoke",
                origin=OriginContext(producer_id="package-smoke"),
            )
        )
        hits = engine.search(SearchRequest(query="Python API", scope=scope))
    if [hit.claim_id for hit in hits] != [receipt.claim_id]:
        raise RuntimeError("The installed Python API failed its remember/search smoke.")


def _smoke_cli(directory: Path) -> None:
    database = directory / "cli.sqlite3"
    export = directory / "cli-evidence-pack"
    content = "The wheel installs recallctl."
    scope = "workspace:cli-smoke"
    _run_cli(database, "init")
    remembered = _run_cli(
        database,
        "remember",
        content,
        "--scope",
        scope,
        "--external-event-id",
        "package-cli-smoke",
        "--idempotency-key",
        "package-cli-smoke",
    )
    data = remembered.get("data")
    claim_id = data.get("claim_id") if isinstance(data, dict) else None
    if not isinstance(claim_id, str) or not claim_id:
        raise RuntimeError("The installed CLI did not return a memory claim.")

    searched = _run_cli(
        database,
        "search",
        "wheel installs recallctl",
        "--scope",
        scope,
    )
    search_data = searched.get("data")
    hits = search_data.get("hits") if isinstance(search_data, dict) else None
    if not isinstance(hits, list) or not any(
        isinstance(hit, dict) and hit.get("claim_id") == claim_id and hit.get("content") == content
        for hit in hits
    ):
        raise RuntimeError(
            "The installed CLI search did not return the claim and content it just remembered."
        )

    context = _run_cli(
        database,
        "context",
        "wheel installs recallctl",
        "--scope",
        scope,
        "--mode",
        "evidence",
        "--ttl-seconds",
        "600",
        "--out",
        str(export),
    )
    context_data = context.get("data")
    if not isinstance(context_data, dict):
        raise RuntimeError(f"The installed CLI returned an invalid evidence envelope: {context!r}")
    pack_id = context_data.get("pack_id")
    exported_to = context_data.get("exported_to")
    resource_uris = context_data.get("resource_uris")
    required_resources = ("manifest.json", "MANIFEST.md", "retrieval.json", "inspector.html")
    if (
        not isinstance(pack_id, str)
        or not pack_id
        or not isinstance(exported_to, str)
        or Path(exported_to).resolve() != export.resolve()
        or context_data.get("mode") != "evidence"
        or context_data.get("managed") is not True
        or context_data.get("export_managed") is not False
        or not context_data.get("integrity_sha256")
        or not isinstance(resource_uris, list)
        or not all(
            any(isinstance(uri, str) and uri.endswith(f"/{name}") for uri in resource_uris)
            for name in required_resources
        )
    ):
        raise RuntimeError(f"The installed CLI returned an invalid evidence envelope: {context!r}")

    managed_pack = Path(f"{database}.packs") / pack_id
    _require_nonempty_pack(managed_pack, label="managed Evidence Pack")
    _require_nonempty_pack(export, label="exported Evidence Pack")


async def _smoke_mcp_async(directory: Path) -> None:
    config = MCPServerConfig.from_strings(
        db_path=directory / "mcp.sqlite3",
        tenant_id="package-smoke",
        principal_id="package-smoke",
        allowed_partitions=["workspace:mcp-smoke"],
    )
    server = create_mcp_server(config)
    try:
        tools = await server.list_tools()
        names = {tool.name for tool in tools}
        if "memory_put" not in names or "memory_context" not in names:
            raise RuntimeError(f"The installed MCP tool surface is incomplete: {sorted(names)}")
        _, structured = await server.call_tool(
            "memory_put",
            {
                "mode": "remember",
                "content": "The wheel exposes the MCP surface.",
                "scope": "workspace:mcp-smoke",
                "external_event_id": "package-mcp-smoke",
                "idempotency_key": "package-mcp-smoke",
            },
        )
        if structured["status"] != "candidate":
            raise RuntimeError(f"The installed MCP call returned an invalid result: {structured!r}")
        resources = list(await server.read_resource("memory://schema"))
        if len(resources) != 1 or not resources[0].content:
            raise RuntimeError("The installed MCP resource surface returned no schema.")
    finally:
        server.recall_origin_adapter.close()


def _smoke_mcp(directory: Path) -> None:
    asyncio.run(_smoke_mcp_async(directory))


def _smoke_http(directory: Path, expected_version: str) -> None:
    scope = PartitionRef.workspace("http-smoke")
    principal = PrincipalContext(
        tenant_id="package-smoke",
        principal_id="package-smoke",
        principal_type=PrincipalType.HUMAN,
        capabilities=frozenset(Capability),
    )
    app = create_app(
        db=directory / "http.sqlite3",
        principal=principal,
        allowed_partitions=[scope],
    )
    schema = app.openapi()
    if schema["info"]["version"] != expected_version or "/v1/health" not in schema["paths"]:
        raise RuntimeError("The installed HTTP application returned an invalid OpenAPI surface.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    arguments = parser.parse_args()
    if recall_origin.__version__ != arguments.expected_version:
        raise RuntimeError(
            f"Installed version {recall_origin.__version__!r} does not match "
            f"{arguments.expected_version!r}."
        )
    with tempfile.TemporaryDirectory(prefix="recall-origin-package-smoke-") as temporary:
        directory = Path(temporary)
        _smoke_python(directory)
        _smoke_cli(directory)
        _smoke_mcp(directory)
        _smoke_http(directory, arguments.expected_version)
    print(
        f"Installed package surfaces passed: Python, CLI, MCP, HTTP ({arguments.expected_version})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
