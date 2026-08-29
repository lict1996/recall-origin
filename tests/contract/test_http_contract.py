from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

pytest.importorskip("fastapi", reason="HTTP contract tests require the server extra")

from fastapi.openapi.models import OpenAPI
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from recall_origin import MemoryEngine, __version__
from recall_origin.contracts.v1 import (
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberRequest,
)
from recall_origin.domain.enums import Capability, PrincipalType
from recall_origin.interfaces.http import create_app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_CONTRACT = REPOSITORY_ROOT / "contracts" / "openapi.yaml"


def _principal(name: str = "http-admin") -> PrincipalContext:
    return PrincipalContext(
        tenant_id="local",
        principal_id=name,
        principal_type=PrincipalType.HUMAN,
        capabilities=frozenset(Capability),
    )


def _scope_payload(scope: PartitionRef) -> dict[str, str]:
    return {
        "namespace_kind": scope.namespace_kind.value,
        "namespace_id": scope.namespace_id,
    }


def _headers(values: Mapping[str, str] | None = None) -> list[tuple[bytes, bytes]]:
    result = [(b"host", b"127.0.0.1:8765"), (b"accept", b"application/json")]
    for name, value in (values or {}).items():
        result.append((name.lower().encode("ascii"), value.encode("utf-8")))
    return result


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    query: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 43123),
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = _headers(headers)
    if payload is not None:
        request_headers.append((b"content-type", b"application/json"))
    messages: list[dict[str, Any]] = []
    sent_request = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def invoke() -> None:
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.5"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": urlencode(query or {}).encode("ascii"),
                "root_path": "",
                "headers": request_headers,
                "client": client,
                "server": ("127.0.0.1", 8765),
            },
            receive,
            send,
        )

    asyncio.run(invoke())
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1"): value.decode("latin-1") for name, value in start.get("headers", [])
    }
    return int(start["status"]), json.loads(response_body), response_headers


def _create_http_app(
    database: Path,
    allowed_partitions: Iterable[PartitionRef],
    *,
    principal_name: str = "http-admin",
) -> Any:
    return create_app(
        db=database,
        principal=_principal(principal_name),
        allowed_partitions=allowed_partitions,
    )


def _without_documentation_noise(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_documentation_noise(item)
            for key, item in value.items()
            if key not in {"title", "description"}
        }
    if isinstance(value, list):
        return [_without_documentation_noise(item) for item in value]
    return value


def _operation_contract(operation: Mapping[str, Any]) -> dict[str, Any]:
    request_body = operation.get("requestBody")
    request_schema = (
        None if request_body is None else request_body["content"]["application/json"]["schema"]
    )
    responses = {
        status: response.get("content", {}).get("application/json", {}).get("schema")
        for status, response in operation["responses"].items()
    }
    return {
        "operationId": operation["operationId"],
        "parameters": _without_documentation_noise(operation.get("parameters", [])),
        "request_required": None if request_body is None else request_body.get("required", False),
        "request_schema": request_schema,
        "responses": responses,
    }


def _schema_refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        refs = {value["$ref"]} if "$ref" in value else set()
        for item in value.values():
            refs.update(_schema_refs(item))
        return refs
    if isinstance(value, list):
        list_refs: set[str] = set()
        for item in value:
            list_refs.update(_schema_refs(item))
        return list_refs
    return set()


def test_openapi_exposes_only_implemented_v1_operations_and_runtime_dtos(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("alpha")
    app = _create_http_app(tmp_path / "memory.sqlite3", [scope])
    runtime_schema = app.openapi()
    canonical_schema = json.loads(OPENAPI_CONTRACT.read_text(encoding="utf-8"))
    OpenAPI.model_validate(canonical_schema)
    for component_schema in canonical_schema["components"]["schemas"].values():
        Draft202012Validator.check_schema(component_schema)

    expected_operations = {
        ("/v1/memories", "post"): "rememberMemory",
        ("/v1/search", "post"): "searchMemories",
        ("/v1/context", "post"): "buildContext",
        (
            "/v1/evidence-packs/{pack_id}/resources/{resource_path}",
            "get",
        ): "getEvidencePackResource",
        ("/v1/memories/{claim_id}", "get"): "getMemory",
        ("/v1/memories/{claim_id}/govern", "post"): "governMemory",
        ("/v1/deletions", "post"): "createDeletion",
        ("/v1/deletions/{deletion_id}", "get"): "getDeletion",
        ("/v1/health", "get"): "getHealth",
        ("/v1/stats", "get"): "getStats",
    }
    for (path, method), operation_id in expected_operations.items():
        assert runtime_schema["paths"][path][method]["operationId"] == operation_id

    assert canonical_schema["openapi"] == "3.1.0"
    assert runtime_schema["info"]["version"] == __version__
    assert canonical_schema["info"]["version"] == __version__
    assert canonical_schema["x-recallorigin-contract-version"] == "1"
    assert canonical_schema["x-recallorigin-security-boundary"] == ("loopback-only-fixed-principal")
    assert canonical_schema["paths"].keys() == runtime_schema["paths"].keys()
    for path in canonical_schema["paths"]:
        assert canonical_schema["paths"][path].keys() == runtime_schema["paths"][path].keys()
        for method, operation in canonical_schema["paths"][path].items():
            assert _operation_contract(operation) == _operation_contract(
                runtime_schema["paths"][path][method]
            )

    canonical_components = canonical_schema["components"]["schemas"]
    runtime_components = runtime_schema["components"]["schemas"]
    assert canonical_components.keys() == runtime_components.keys()
    assert _without_documentation_noise(canonical_components) == (
        _without_documentation_noise(runtime_components)
    )
    for reference in _schema_refs(canonical_schema):
        prefix = "#/components/schemas/"
        assert reference.startswith(prefix)
        assert reference.removeprefix(prefix) in canonical_components

    assert "/v1/events" not in canonical_schema["paths"]
    assert "/v1/memories/{claim_id}/revisions" not in canonical_schema["paths"]
    assert "/v1/memories/{claim_id}/feedback" not in canonical_schema["paths"]
    assert "security" not in canonical_schema
    assert "securitySchemes" not in canonical_schema.get("components", {})
    partition_schema = canonical_components["PartitionRef"]
    assert set(partition_schema["properties"]) == {"namespace_kind", "namespace_id"}
    assert canonical_components["IndexState"]["enum"] == [
        "ready",
        "pending",
        "degraded",
    ]
    context_operation = runtime_schema["paths"]["/v1/context"]["post"]
    context_request_schema = context_operation["requestBody"]["content"]["application/json"][
        "schema"
    ]
    context_response_schema = context_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    for schema in (context_request_schema, context_response_schema):
        assert len(schema["oneOf"]) == 2
        assert schema["discriminator"]["propertyName"] == "mode"
        assert set(schema["discriminator"]["mapping"]) == {"fast", "evidence"}
    assert set(context_operation["responses"]) == {"200", "403", "404", "409", "422", "503"}
    resource_operation = runtime_schema["paths"][
        "/v1/evidence-packs/{pack_id}/resources/{resource_path}"
    ]["get"]
    assert resource_operation["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert "loopback" in canonical_schema["info"]["description"].lower()
    assert "no http authentication" in canonical_schema["info"]["description"].lower()


def test_http_search_context_get_and_stats_preserve_exact_partition_authorization(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    owner = MemoryEngine.local(database).initialize()
    alpha_memory = owner.remember(
        RememberRequest(
            content="alpha release checklist",
            scope=alpha,
            external_event_id="alpha-event",
            origin=OriginContext(producer_id="owner"),
        )
    )
    beta_memory = owner.remember(
        RememberRequest(
            content="ultraviolet-badger-4821",
            scope=beta,
            external_event_id="beta-event",
            origin=OriginContext(producer_id="owner"),
        )
    )
    app = _create_http_app(database, [alpha])

    search_status, searched, _ = _request(
        app,
        "POST",
        "/v1/search",
        payload={"query": "release", "scope": _scope_payload(alpha)},
    )
    assert search_status == 200
    assert [item["claim_id"] for item in searched["items"]] == [alpha_memory.claim_id]
    assert searched["untrusted_data"] is True

    context_status, context, _ = _request(
        app,
        "POST",
        "/v1/context",
        payload={
            "mode": "fast",
            "query": "release",
            "scope": _scope_payload(alpha),
            "token_budget": 800,
        },
    )
    assert context_status == 200
    assert context["mode"] == "fast"
    assert [item["claim_id"] for item in context["items"]] == [alpha_memory.claim_id]
    assert context["untrusted_data"] is True

    get_status, fetched, _ = _request(
        app,
        "GET",
        f"/v1/memories/{alpha_memory.claim_id}",
    )
    assert get_status == 200
    assert fetched["memory"]["content"] == "alpha release checklist"

    denied_search_status, denied_search, _ = _request(
        app,
        "POST",
        "/v1/search",
        payload={"query": "ultraviolet", "scope": _scope_payload(beta)},
    )
    assert denied_search_status == 403
    assert denied_search["error"]["code"] == "SCOPE_DENIED"

    hidden_status, hidden, _ = _request(
        app,
        "GET",
        f"/v1/memories/{beta_memory.claim_id}",
    )
    assert hidden_status == 404
    assert hidden["error"]["code"] == "NOT_FOUND"
    assert "partition" not in json.dumps(hidden).lower()

    stats_status, stats, _ = _request(app, "GET", "/v1/stats")
    assert stats_status == 200
    assert stats["authorized_partition_count"] == 1
    assert stats["current_claim_count"] == 1

    health_status, health, _ = _request(app, "GET", "/v1/health")
    assert health_status == 200
    assert health == {"contract_version": "1", "status": "healthy"}


def test_write_idempotency_governance_and_deletion_have_stable_http_semantics(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    scope = PartitionRef.workspace("alpha")
    app = _create_http_app(database, [scope])
    remember_body = {
        "content": "deploy only after verification",
        "scope": _scope_payload(scope),
        "external_event_id": "http-event-1",
        "origin": {"session_id": "session-1", "host_agent_id": "agent-1"},
    }

    first_status, first, first_headers = _request(
        app,
        "POST",
        "/v1/memories",
        payload=remember_body,
        headers={"idempotency-key": "remember-key-0001"},
    )
    replay_status, replay, _ = _request(
        app,
        "POST",
        "/v1/memories",
        payload=remember_body,
        headers={"idempotency-key": "remember-key-0001"},
    )
    assert first_status == replay_status == 201
    assert first["claim_id"] == replay["claim_id"]
    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert first_headers["cache-control"] == "no-store"
    assert first_headers["x-request-id"] == first["request_id"]

    restarted_app = _create_http_app(database, [scope])
    restarted_replay_status, restarted_replay, _ = _request(
        restarted_app,
        "POST",
        "/v1/memories",
        payload=remember_body,
        headers={"idempotency-key": "remember-key-0001"},
    )
    assert restarted_replay_status == 201
    assert restarted_replay["claim_id"] == first["claim_id"]
    assert restarted_replay["replayed"] is True

    mismatch_status, mismatch, _ = _request(
        restarted_app,
        "POST",
        "/v1/memories",
        payload={**remember_body, "content": "different payload"},
        headers={"idempotency-key": "remember-key-0001"},
    )
    assert mismatch_status == 409
    assert mismatch["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    govern_body = {
        "expected_revision_id": first["revision_id"],
        "action": "activate",
        "reason": "Validated by the local operator.",
    }
    governed_status, governed, _ = _request(
        app,
        "POST",
        f"/v1/memories/{first['claim_id']}/govern",
        payload=govern_body,
        headers={"idempotency-key": "govern-key-0001"},
    )
    governed_replay_status, governed_replay, _ = _request(
        app,
        "POST",
        f"/v1/memories/{first['claim_id']}/govern",
        payload=govern_body,
        headers={"idempotency-key": "govern-key-0001"},
    )
    assert governed_status == governed_replay_status == 200
    assert governed["revision_id"] == governed_replay["revision_id"]
    assert governed_replay["replayed"] is True

    deletion_body = {
        "target": {"target_type": "claim", "target_id": first["claim_id"]},
        "scope": _scope_payload(scope),
        "expected_revision_id": governed["revision_id"],
        "cascade_policy": "safe",
    }
    deletion_status, deletion, _ = _request(
        app,
        "POST",
        "/v1/deletions",
        payload=deletion_body,
        headers={"idempotency-key": "delete-key-0001"},
    )
    assert deletion_status == 202
    assert deletion["state"] == "logically_hidden"

    status_code, deletion_state, _ = _request(
        app,
        "GET",
        f"/v1/deletions/{deletion['deletion_id']}",
    )
    assert status_code == 200
    assert deletion_state["deletion_id"] == deletion["deletion_id"]

    other_scope_app = _create_http_app(
        database,
        [PartitionRef.workspace("beta")],
        principal_name="other-http-admin",
    )
    hidden_deletion_status, hidden_deletion, _ = _request(
        other_scope_app,
        "GET",
        f"/v1/deletions/{deletion['deletion_id']}",
    )
    assert hidden_deletion_status == 404
    assert hidden_deletion["error"]["code"] == "NOT_FOUND"

    missing_status, missing, _ = _request(
        app,
        "GET",
        f"/v1/memories/{first['claim_id']}",
    )
    assert missing_status == 404
    assert missing["error"]["code"] == "NOT_FOUND"


def test_validation_and_non_loopback_requests_use_the_public_error_envelope(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("alpha")
    app = _create_http_app(tmp_path / "memory.sqlite3", [scope])

    validation_status, validation, _ = _request(
        app,
        "POST",
        "/v1/memories",
        payload={
            "content": "missing idempotency header",
            "scope": _scope_payload(scope),
            "external_event_id": "event-1",
        },
    )
    assert validation_status == 422
    assert validation["contract_version"] == "1"
    assert validation["error"]["code"] == "VALIDATION_ERROR"
    assert validation["error"]["retryable"] is False
    assert "request_id" in validation

    malformed_scope_status, malformed_scope, _ = _request(
        app,
        "GET",
        "/v1/memories/not-a-claim",
        query={"scope": "not-a-scope"},
    )
    assert malformed_scope_status == 422
    assert malformed_scope["error"]["code"] == "VALIDATION_ERROR"

    remote_status, remote, _ = _request(
        app,
        "GET",
        "/v1/health",
        client=("203.0.113.9", 43123),
    )
    assert remote_status == 403
    assert remote["error"]["code"] == "SCOPE_DENIED"
    assert "loopback" in remote["error"]["message"].lower()
