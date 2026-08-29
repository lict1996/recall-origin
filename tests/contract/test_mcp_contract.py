from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

from recall_origin import __version__
from recall_origin.contracts.errors import (
    FEATURE_NOT_ENABLED,
    NOT_FOUND,
    SCOPE_DENIED,
    VALIDATION_ERROR,
    RecallOriginError,
)
from recall_origin.contracts.v1 import ForgetTarget, SubjectRef
from recall_origin.domain.enums import (
    Confirmation,
    ForgetTargetType,
    GovernAction,
    MemoryStatus,
    PrincipalType,
)
from recall_origin.interfaces import mcp as mcp_interface
from recall_origin.interfaces.mcp import (
    TOOL_SPECS,
    MCPServerConfig,
    MemoryMCPAdapter,
    create_mcp_server,
    enabled_tool_specs,
)
from recall_origin.storage.sqlite.errors import SQLiteFeatureError, SQLiteStorageError

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "contracts" / "mcp-tools.json").read_text(encoding="utf-8"))
DEFAULT_NAMES = {
    "memory_put",
    "memory_search",
    "memory_context",
    "memory_get",
    "memory_feedback",
}
PRIVILEGED_NAMES = {"memory_govern", "memory_forget"}


def _config(tmp_path: Path, **overrides: object) -> MCPServerConfig:
    values: dict[str, object] = {
        "db_path": tmp_path / "memory.sqlite3",
        "tenant_id": "tenant-test",
        "principal_id": "agent-test",
        "allowed_partitions": ["workspace:alpha"],
    }
    values.update(overrides)
    return MCPServerConfig.from_strings(**values)  # type: ignore[arg-type]


def test_registration_metadata_matches_canonical_contract() -> None:
    canonical = {
        item["name"]: item
        for section in ("defaultTools", "privilegedTools")
        for item in CONTRACT[section]
    }
    actual = {spec.name: spec for spec in TOOL_SPECS}

    assert set(actual) == set(canonical)
    for name, spec in actual.items():
        expected = canonical[name]
        assert spec.required_capability == expected["requiredCapability"]
        annotations = spec.annotations.to_protocol_dict()
        assert annotations["readOnlyHint"] == expected["annotations"]["readOnlyHint"]
        assert annotations["destructiveHint"] == expected["annotations"]["destructiveHint"]
        assert annotations["openWorldHint"] == expected["annotations"]["openWorldHint"]
    assert actual["memory_feedback"].annotations.idempotent is False
    assert actual["memory_context"].annotations.idempotent is False
    assert actual["memory_context"].annotations.read_only is False


def test_default_and_privileged_tool_surfaces_are_default_deny(tmp_path: Path) -> None:
    default = _config(tmp_path)
    assert {spec.name for spec in enabled_tool_specs(default)} == DEFAULT_NAMES
    assert not ({spec.name for spec in enabled_tool_specs(default)} & PRIVILEGED_NAMES)

    privileged = _config(tmp_path, privileged=True)
    assert {spec.name for spec in enabled_tool_specs(privileged)} == (
        DEFAULT_NAMES | PRIVILEGED_NAMES
    )

    explicit_govern = _config(tmp_path, capabilities=["read", "govern"])
    assert {spec.name for spec in enabled_tool_specs(explicit_govern)} == {
        "memory_search",
        "memory_context",
        "memory_get",
        "memory_govern",
    }


def test_startup_argv_has_safe_defaults_and_honors_explicit_trust_controls(
    tmp_path: Path,
) -> None:
    default = mcp_interface.config_from_argv(
        [
            "--db",
            str(tmp_path / "default.sqlite3"),
            "--tenant-id",
            "tenant-default",
            "--principal-id",
            "agent-default",
            "--partition",
            "workspace:alpha",
        ]
    )

    assert default.capabilities == frozenset({"read", "write", "feedback"})
    assert default.principal_type is PrincipalType.AGENT
    assert default.producer_id == "mcp-stdio"
    assert default.durable is True
    assert default.purge_registry_path is None

    purge_registry = tmp_path / "registry" / "purge.json"
    explicit = mcp_interface.config_from_argv(
        [
            "--db",
            str(tmp_path / "explicit.sqlite3"),
            "--tenant-id",
            "tenant-explicit",
            "--principal-id",
            "human-explicit",
            "--principal-type",
            "human",
            "--partition",
            "workspace:alpha",
            "--partition",
            "workspace:alpha",
            "--partition",
            "user:owner",
            "--capability",
            "read",
            "--capability",
            "govern",
            "--privileged",
            "--producer-id",
            "trusted-host",
            "--purge-registry",
            str(purge_registry),
            "--non-durable",
        ]
    )

    assert explicit.db_path == (tmp_path / "explicit.sqlite3").resolve()
    assert explicit.tenant_id == "tenant-explicit"
    assert explicit.principal_id == "human-explicit"
    assert explicit.principal_type is PrincipalType.HUMAN
    assert [partition.serialize() for partition in explicit.allowed_partitions] == [
        "workspace:alpha",
        "user:owner",
    ]
    assert explicit.capabilities == frozenset({"read", "govern", "delete"})
    assert explicit.producer_id == "trusted-host"
    assert explicit.durable is False
    assert explicit.purge_registry_path == purge_registry.resolve()


def test_startup_argv_rejects_unknown_capabilities_before_server_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        mcp_interface.config_from_argv(
            [
                "--db",
                str(tmp_path / "memory.sqlite3"),
                "--tenant-id",
                "tenant-test",
                "--principal-id",
                "agent-test",
                "--partition",
                "workspace:alpha",
                "--capability",
                "administrator",
            ]
        )

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--capability" in captured.err
    assert "administrator" in captured.err


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"tenant_id": ""}, "tenant_id must contain 1-128 characters"),
        ({"principal_id": ""}, "principal_id must contain 1-256 characters"),
        ({"producer_id": ""}, "producer_id must contain 1-256 characters"),
        ({"allowed_partitions": []}, "at least one exact partition is required"),
        (
            {"capabilities": ["superuser", "root"]},
            "unknown MCP capabilities: root, superuser",
        ),
    ],
    ids=["tenant", "principal", "producer", "partition", "capability"],
)
def test_trusted_startup_config_rejects_invalid_identity_and_authority(
    tmp_path: Path,
    overrides: dict[str, object],
    expected_message: str,
) -> None:
    with pytest.raises(ValueError) as error:
        _config(tmp_path, **overrides)

    assert str(error.value) == expected_message


def test_explicit_empty_capabilities_expose_nothing_and_deny_every_operation(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mcp")
    config = _config(tmp_path, capabilities=[])
    adapter = MemoryMCPAdapter.create(config)
    target = ForgetTarget(target_type=ForgetTargetType.CLAIM, target_id="claim-not-authorized")
    operations = (
        (
            "write",
            lambda: adapter.memory_put(
                mode="remember",
                content="This must be denied before ingestion.",
                scope="workspace:alpha",
                external_event_id="event-denied-001",
                idempotency_key="idempotency-denied-001",
            ),
        ),
        (
            "read",
            lambda: adapter.memory_search(query="anything", scope="workspace:alpha"),
        ),
        (
            "feedback",
            lambda: adapter.memory_feedback(
                claim_id="claim-not-authorized",
                revision_id="revision-not-authorized",
                feedback_type="helpful",
            ),
        ),
        (
            "govern",
            lambda: adapter.memory_govern(
                claim_id="claim-not-authorized",
                expected_revision_id="revision-not-authorized",
                action=GovernAction.CONFIRM,
                reason="This must be denied.",
            ),
        ),
        (
            "delete",
            lambda: adapter.memory_forget(
                target=target,
                idempotency_key="idempotency-denied-delete",
            ),
        ),
    )

    try:
        assert enabled_tool_specs(config) == ()
        server = create_mcp_server(config, adapter=adapter)
        assert asyncio.run(server.list_tools()) == []
        assert asyncio.run(server.list_resources()) == []
        assert asyncio.run(server.list_resource_templates()) == []

        for capability, operation in operations:
            with pytest.raises(RecallOriginError) as denied:
                operation()
            assert denied.value.spec is SCOPE_DENIED
            assert denied.value.details == {"capability": capability}
    finally:
        adapter.close()


def test_tool_payloads_cannot_override_server_identity() -> None:
    forbidden = {"tenant", "tenant_id", "principal", "principal_id", "capabilities"}
    for spec in TOOL_SPECS:
        parameters = set(inspect.signature(getattr(MemoryMCPAdapter, spec.method_name)).parameters)
        parameters.discard("self")
        assert not parameters & forbidden, spec.name


def test_agent_put_is_unverified_and_results_are_marked_untrusted(tmp_path: Path) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))

    receipt = adapter.memory_put(
        mode="remember",
        content="Use deterministic IDs in retry tests.",
        scope="workspace:alpha",
        external_event_id="event-contract-001",
        idempotency_key="idempotency-contract-001",
    )

    assert receipt.status == MemoryStatus.CANDIDATE.value
    assert receipt.confirmation == Confirmation.UNVERIFIED.value
    found = adapter.memory_search(
        query="deterministic IDs",
        scope="workspace:alpha",
        include_candidates=True,
    )
    assert found.untrusted_data is True
    assert [item.claim_id for item in found.items] == [receipt.claim_id]

    expanded = adapter.memory_get(receipt.claim_id)
    assert expanded.untrusted_data is True
    assert expanded.claim_id == receipt.claim_id


def test_put_capture_returns_pending_job_without_claiming_formation(tmp_path: Path) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))

    captured = adapter.memory_put(
        mode="capture",
        content='{"observation":"The project uses uv."}',
        scope="workspace:alpha",
        event_type="host_observation",
        external_event_id="event-capture-001",
        idempotency_key="idempotency-capture-001",
    )
    replayed = adapter.memory_put(
        mode="capture",
        content='{"observation":"The project uses uv."}',
        scope="workspace:alpha",
        event_type="host_observation",
        external_event_id="event-capture-001",
        idempotency_key="idempotency-capture-001",
    )

    assert captured.formation_mode == "automatic"
    assert captured.status == "accepted_pending"
    assert captured.job_id is not None
    assert captured.claim_id is None
    assert replayed.replayed is True
    assert replayed.job_id == captured.job_id


def test_memory_put_rejects_mode_specific_invalid_requests_without_writing(
    tmp_path: Path,
) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))
    subjects = (
        SubjectRef(subject_type="user", subject_id="one"),
        SubjectRef(subject_type="user", subject_id="two"),
    )

    try:
        with pytest.raises(RecallOriginError) as missing_event_type:
            adapter.memory_put(
                mode="capture",
                content='{"observation":"missing event type"}',
                scope="workspace:alpha",
                external_event_id="event-invalid-capture-001",
                idempotency_key="idempotency-invalid-capture-001",
            )
        assert missing_event_type.value.spec is VALIDATION_ERROR
        assert missing_event_type.value.message == (
            "event_type is required when memory_put mode is 'capture'."
        )

        with pytest.raises(RecallOriginError) as too_many_subjects:
            adapter.memory_put(
                mode="remember",
                content="Explicit memory currently has one subject.",
                scope="workspace:alpha",
                external_event_id="event-invalid-remember-001",
                idempotency_key="idempotency-invalid-remember-001",
                subjects=subjects,
            )
        assert too_many_subjects.value.spec is VALIDATION_ERROR
        assert too_many_subjects.value.details == {"subject_count": 2}

        found = adapter.memory_search(
            query="missing event type explicit memory",
            scope="workspace:alpha",
            include_candidates=True,
        )
        assert found.items == ()
    finally:
        adapter.close()


def test_fast_context_is_the_bounded_untrusted_default(tmp_path: Path) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))

    try:
        pack = adapter.memory_context(
            query="nothing has been stored",
            scope="workspace:alpha",
            token_budget=32,
        )

        assert pack.mode == "fast"
        assert pack.untrusted_data is True
        assert pack.scope.serialize() == "workspace:alpha"
        assert pack.token_budget == 32
        assert pack.token_count == 0
        assert pack.items == ()
    finally:
        adapter.close()


def test_agent_govern_capability_cannot_issue_human_confirmation(tmp_path: Path) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path, capabilities=["read", "write", "govern"]))

    try:
        memory = adapter.memory_put(
            mode="remember",
            content="Agent governance cannot establish user confirmation.",
            scope="workspace:alpha",
            external_event_id="event-agent-govern-001",
            idempotency_key="idempotency-agent-govern-001",
        )

        with pytest.raises(RecallOriginError) as denied:
            adapter.memory_govern(
                claim_id=memory.claim_id or "",
                expected_revision_id=memory.revision_id or "",
                action=GovernAction.CONFIRM,
                reason="The agent must not impersonate a human reviewer.",
            )

        assert denied.value.spec is SCOPE_DENIED
        assert denied.value.message == ("Only an authenticated human can issue user confirmation.")
        unchanged = adapter.memory_get(memory.claim_id or "")
        assert unchanged.revision_id == memory.revision_id
        assert unchanged.status is MemoryStatus.CANDIDATE
        assert unchanged.confirmation is Confirmation.UNVERIFIED
    finally:
        adapter.close()


def test_explicit_delete_capability_fences_a_claim_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path, capabilities=["read", "write", "delete"]))

    try:
        memory = adapter.memory_put(
            mode="remember",
            content="This claim will be deleted through the MCP boundary.",
            scope="workspace:alpha",
            external_event_id="event-mcp-delete-001",
            idempotency_key="idempotency-mcp-delete-001",
        )
        target = ForgetTarget(
            target_type=ForgetTargetType.CLAIM,
            target_id=memory.claim_id or "",
        )
        deletion = adapter.memory_forget(
            target=target,
            idempotency_key="idempotency-mcp-forget-001",
            expected_revision_id=memory.revision_id,
        )
        replayed = adapter.memory_forget(
            target=target,
            idempotency_key="idempotency-mcp-forget-001",
            expected_revision_id=memory.revision_id,
        )

        assert deletion.target == target
        assert replayed.deletion_id == deletion.deletion_id
        assert replayed.replayed is True
        with pytest.raises(RecallOriginError) as hidden:
            adapter.memory_get(memory.claim_id or "")
        assert hidden.value.spec is NOT_FOUND
    finally:
        adapter.close()


def test_scope_arguments_can_only_narrow_fixed_partitions(tmp_path: Path) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))

    with pytest.raises(RecallOriginError) as error:
        adapter.memory_search(query="anything", scope="workspace:other")

    assert error.value.spec is SCOPE_DENIED
    assert error.value.details == {"scope": "workspace:other"}


def test_feedback_is_append_only_and_does_not_govern(tmp_path: Path) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))
    memory = adapter.memory_put(
        mode="remember",
        content="Feedback must remain separate from governance.",
        scope="workspace:alpha",
        external_event_id="event-feedback-001",
        idempotency_key="idempotency-feedback-001",
    )

    feedback = adapter.memory_feedback(
        claim_id=memory.claim_id,
        revision_id=memory.revision_id,
        feedback_type="helpful",
        reason="The retrieval was useful.",
    )

    assert feedback.appended is True
    assert feedback.claim_id == memory.claim_id
    current = adapter.memory_get(memory.claim_id)
    assert current.revision_id == memory.revision_id
    assert current.status is MemoryStatus.CANDIDATE


def test_resources_are_read_only_authorized_views(tmp_path: Path) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))
    receipt = adapter.memory_put(
        mode="remember",
        content="Resources hydrate canonical memory.",
        scope="workspace:alpha",
        external_event_id="event-resource-001",
        idempotency_key="idempotency-resource-001",
    )

    schema = json.loads(adapter.schema_resource())
    assert {item["name"] for item in schema["tools"]} == DEFAULT_NAMES
    assert schema["security"]["identity_source"] == "trusted_server_configuration"

    stats = json.loads(adapter.stats_resource())
    assert stats["authorized_partitions"] == ["workspace:alpha"]
    assert stats["memory_counts"]["available"] is False
    serialized_stats = json.dumps(stats)
    assert "tenant-test" not in serialized_stats
    assert "agent-test" not in serialized_stats
    assert str(tmp_path) not in serialized_stats

    entry = json.loads(adapter.entry_resource(receipt.claim_id))
    assert entry["claim_id"] == receipt.claim_id
    assert entry["untrusted_data"] is True


def test_evidence_context_returns_only_mcp_readable_opaque_resources(
    tmp_path: Path,
) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))
    adapter.memory_put(
        mode="remember",
        content="Evidence resources use opaque MCP keys.",
        scope="workspace:alpha",
        external_event_id="event-evidence-resource-001",
        idempotency_key="idempotency-evidence-resource-001",
    )

    pack = adapter.memory_context(
        query="resource contract",
        scope="workspace:alpha",
        mode="evidence",
        ttl_seconds=60,
    )

    assert pack.mode == "evidence"
    assert pack.untrusted_data is True
    assert pack.resource_uris
    assert all(str(tmp_path) not in uri for uri in pack.resource_uris)
    for uri in pack.resource_uris:
        prefix = f"memory://packs/{pack.pack_id}/"
        assert uri.startswith(prefix)
        resource_key = uri.removeprefix(prefix)
        assert "/" not in resource_key
        assert adapter.pack_resource(pack.pack_id, resource_key)


@pytest.mark.parametrize(
    "resource_key",
    ["bad$key", "A", "YR"],
    ids=["invalid-alphabet", "invalid-base64", "noncanonical-base64"],
)
def test_pack_resources_reject_malformed_or_noncanonical_opaque_keys(
    tmp_path: Path,
    resource_key: str,
) -> None:
    adapter = MemoryMCPAdapter.create(_config(tmp_path))

    try:
        with pytest.raises(RecallOriginError) as error:
            adapter.pack_resource("pack-does-not-matter", resource_key)
        assert error.value.spec is VALIDATION_ERROR
        assert error.value.message == "Evidence Pack resource key is invalid."
    finally:
        adapter.close()


def test_optional_sdk_is_real_or_fails_with_feature_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)

    if importlib.util.find_spec("mcp") is None:
        with pytest.raises(RecallOriginError) as error:
            create_mcp_server(config)
        assert error.value.spec is FEATURE_NOT_ENABLED
        captured = capsys.readouterr()
        assert captured.out == ""
        return

    server = create_mcp_server(config)
    captured = capsys.readouterr()
    assert captured.out == ""

    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == DEFAULT_NAMES
    assert all(tool.outputSchema is not None for tool in tools)
    by_name = {tool.name: tool for tool in tools}
    assert by_name["memory_search"].annotations.readOnlyHint is False
    assert by_name["memory_search"].annotations.idempotentHint is False
    assert by_name["memory_context"].annotations.readOnlyHint is False
    assert by_name["memory_put"].annotations.destructiveHint is False

    _, structured = asyncio.run(
        server.call_tool(
            "memory_put",
            {
                "content": "The MCP result has structured content.",
                "mode": "remember",
                "scope": "workspace:alpha",
                "external_event_id": "event-sdk-001",
                "idempotency_key": "idempotency-sdk-001",
            },
        )
    )
    assert structured["status"] == "candidate"
    assert structured["confirmation"] == "unverified"

    _, evidence = asyncio.run(
        server.call_tool(
            "memory_context",
            {
                "query": "structured content",
                "scope": "workspace:alpha",
                "mode": "evidence",
                "ttl_seconds": 60,
            },
        )
    )
    evidence_result = evidence["result"]
    assert evidence_result["mode"] == "evidence"
    assert evidence_result["resource_uris"]
    for uri in evidence_result["resource_uris"]:
        contents = list(asyncio.run(server.read_resource(uri)))
        assert len(contents) == 1
        assert isinstance(contents[0].content, bytes)
        assert contents[0].content
        assert str(tmp_path).encode() not in contents[0].content

    resources = asyncio.run(server.list_resources())
    templates = asyncio.run(server.list_resource_templates())
    assert {str(resource.uri) for resource in resources} == {
        "memory://schema",
        "memory://stats",
    }
    assert {template.uriTemplate for template in templates} == {
        "memory://entries/{claim_id}",
        "memory://packs/{pack_id}/{resource_key}",
    }


def test_official_server_initialization_reports_package_version(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    server = create_mcp_server(_config(tmp_path))

    options = server._mcp_server.create_initialization_options()

    assert options.server_name == "RecallOrigin"
    assert options.server_version == __version__


def test_official_server_rejects_an_adapter_with_a_different_principal(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mcp")
    adapter = MemoryMCPAdapter.create(_config(tmp_path))
    different_principal = _config(tmp_path, principal_id="agent-other")

    try:
        with pytest.raises(
            ValueError,
            match="adapter configuration must exactly match server configuration",
        ):
            create_mcp_server(different_principal, adapter=adapter)
    finally:
        adapter.close()


def test_official_call_tool_serializes_scope_denial_as_a_stable_public_error(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    server = create_mcp_server(_config(tmp_path))

    with pytest.raises(ToolError) as error:
        asyncio.run(
            server.call_tool(
                "memory_search",
                {"query": "anything", "scope": "workspace:other"},
            )
        )

    payload = json.loads(str(error.value).partition(": ")[2])
    assert payload == {
        "code": "SCOPE_DENIED",
        "details": {"scope": "workspace:other"},
        "message": ("The requested scope is not one of the server-authorized exact partitions."),
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        (
            {
                "content": "Invalid temporal bounds.",
                "mode": "remember",
                "scope": "workspace:alpha",
                "external_event_id": "event-sdk-error-001",
                "idempotency_key": "idempotency-sdk-error-001",
                "valid_from": "2026-01-02T00:00:00Z",
                "valid_to": "2026-01-01T00:00:00Z",
            },
            "VALIDATION_ERROR",
        ),
        (
            {
                "content": "Naive timestamps are invalid.",
                "mode": "remember",
                "scope": "workspace:alpha",
                "external_event_id": "event-sdk-error-002",
                "idempotency_key": "idempotency-sdk-error-002",
                "valid_from": "2026-01-02T00:00:00",
            },
            "VALIDATION_ERROR",
        ),
    ],
)
def test_official_call_tool_returns_stable_json_for_expected_validation_errors(
    tmp_path: Path,
    arguments: dict[str, object],
    expected_code: str,
) -> None:
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    server = create_mcp_server(_config(tmp_path))

    with pytest.raises(ToolError) as error:
        asyncio.run(server.call_tool("memory_put", arguments))

    _, separator, encoded = str(error.value).partition(": ")
    assert separator
    payload = json.loads(encoded)
    assert payload["code"] == expected_code
    assert payload["retryable"] is False
    assert isinstance(payload["details"], dict)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (SQLiteFeatureError("FTS5 is unavailable"), "FEATURE_NOT_ENABLED"),
        (SQLiteStorageError("database is busy"), "TEMPORARY_FAILURE"),
        (OSError("filesystem is busy"), "TEMPORARY_FAILURE"),
    ],
)
def test_official_call_tool_maps_expected_storage_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    adapter = MemoryMCPAdapter.create(_config(tmp_path))

    def fail(*_: object, **__: object) -> object:
        raise failure

    monkeypatch.setattr(adapter.engine, "search_result", fail)
    server = create_mcp_server(adapter.config, adapter=adapter)

    with pytest.raises(ToolError) as error:
        asyncio.run(
            server.call_tool(
                "memory_search",
                {"query": "anything", "scope": "workspace:alpha"},
            )
        )

    payload = json.loads(str(error.value).partition(": ")[2])
    assert payload["code"] == expected_code


def test_official_call_tool_does_not_relabel_programming_errors_as_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    adapter = MemoryMCPAdapter.create(_config(tmp_path))

    def fail(*_: object, **__: object) -> object:
        raise RuntimeError("programming defect sentinel")

    monkeypatch.setattr(adapter.engine, "search_result", fail)
    server = create_mcp_server(adapter.config, adapter=adapter)

    with pytest.raises(ToolError) as error:
        asyncio.run(
            server.call_tool(
                "memory_search",
                {"query": "anything", "scope": "workspace:alpha"},
            )
        )

    assert "programming defect sentinel" in str(error.value)
    assert '"code": "VALIDATION_ERROR"' not in str(error.value)


def test_main_starts_the_stdio_transport_and_returns_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created_config: MCPServerConfig | None = None
    selected_transport: str | None = None

    class StubServer:
        def run(self, *, transport: str) -> None:
            nonlocal selected_transport
            selected_transport = transport

    def create_stub(config: MCPServerConfig) -> StubServer:
        nonlocal created_config
        created_config = config
        return StubServer()

    monkeypatch.setattr(mcp_interface, "create_mcp_server", create_stub)

    exit_code = mcp_interface.main(
        [
            "--db",
            str(tmp_path / "memory.sqlite3"),
            "--tenant-id",
            "tenant-main",
            "--principal-id",
            "agent-main",
            "--partition",
            "workspace:alpha",
        ]
    )

    assert exit_code == 0
    assert created_config is not None
    assert created_config.tenant_id == "tenant-main"
    assert created_config.principal_id == "agent-main"
    assert selected_transport == "stdio"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_reports_missing_optional_sdk_with_stable_exit_and_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)

    exit_code = mcp_interface.main(
        [
            "--db",
            str(tmp_path / "memory.sqlite3"),
            "--tenant-id",
            "tenant-main",
            "--principal-id",
            "agent-main",
            "--partition",
            "workspace:alpha",
        ]
    )

    assert exit_code == FEATURE_NOT_ENABLED.exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "FEATURE_NOT_ENABLED: MCP support is not installed. "
        "Install RecallOrigin with the 'mcp' extra.\n"
    )
