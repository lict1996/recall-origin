"""Generate the checked-in MCP contract from the official SDK surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from recall_origin.interfaces.mcp import (
    TOOL_SPECS,
    MCPServerConfig,
    create_mcp_server,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "contracts" / "mcp-tools.json"


async def _sdk_surface() -> tuple[list[Any], list[Any], list[Any]]:
    with tempfile.TemporaryDirectory(prefix="recall-origin-mcp-contract-") as temporary:
        config = MCPServerConfig.from_strings(
            db_path=Path(temporary) / "contract.sqlite3",
            tenant_id="contract-generator",
            principal_id="contract-generator",
            allowed_partitions=["workspace:contract-generator"],
            capabilities=["read", "write", "feedback", "govern", "delete"],
            privileged=True,
        )
        server = create_mcp_server(config)
        return (
            await server.list_tools(),
            await server.list_resources(),
            await server.list_resource_templates(),
        )


def build_contract() -> dict[str, Any]:
    """Return one deterministic contract derived from registered MCP objects."""

    tools, resources, templates = asyncio.run(_sdk_surface())
    tools_by_name = {tool.name: tool for tool in tools}
    default_tools: list[dict[str, Any]] = []
    privileged_tools: list[dict[str, Any]] = []
    for spec in TOOL_SPECS:
        payload = tools_by_name[spec.name].model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        payload["requiredCapability"] = spec.required_capability
        payload["privileged"] = spec.privileged
        target = privileged_tools if spec.privileged else default_tools
        target.append(payload)

    resource_payloads = [
        {
            **resource.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
            "requiredCapability": "read",
        }
        for resource in [*resources, *templates]
    ]
    resource_payloads.sort(key=lambda item: str(item.get("uri", item.get("uriTemplate", ""))))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "contract_version": "1",
        "title": "RecallOrigin MCP stdio contract",
        "status": "implemented-alpha",
        "implementation": {
            "transport": "stdio",
            "official_sdk": "mcp>=1.12,<2",
            "implemented": True,
            "generator": "scripts/generate_mcp_contract.py",
        },
        "security_model": {
            "identity_source": "trusted server startup configuration",
            "scope_rule": (
                "Tool arguments may only select one of the server-authorized exact partitions."
            ),
            "content_trust": ("Recalled memory and Evidence Pack resources are untrusted data."),
            "default_capabilities": ["read", "write", "feedback"],
            "privileged_capabilities": ["govern", "delete"],
        },
        "defaultTools": default_tools,
        "privilegedTools": privileged_tools,
        "resources": resource_payloads,
    }


def _serialized_contract() -> str:
    return (
        json.dumps(
            build_contract(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = _serialized_contract()
    output = arguments.output.resolve()
    if arguments.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != expected:
            print(f"MCP contract is stale: {output}")
            return 1
        print(f"MCP contract is current: {output}")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8")
    print(f"Wrote MCP contract: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
