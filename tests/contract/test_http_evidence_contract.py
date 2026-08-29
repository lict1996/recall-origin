from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

pytest.importorskip("fastapi", reason="HTTP contract tests require the server extra")

from recall_origin import MemoryEngine
from recall_origin.contracts.v1 import (
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberReceipt,
    RememberRequest,
)
from recall_origin.domain.enums import Capability, PrincipalType
from recall_origin.interfaces.http import _evidence_resource_http_uri, create_app


def _principal(name: str) -> PrincipalContext:
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


def _request_bytes(
    app: Any,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 43123),
) -> tuple[int, bytes, dict[str, str]]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = [(b"host", b"127.0.0.1:8765"), (b"accept", b"*/*")]
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
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
        decoded_path = unquote(path)
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.5"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": decoded_path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "root_path": "",
                "headers": headers,
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
    return int(start["status"]), response_body, response_headers


def _request_json(
    app: Any,
    method: str,
    path: str,
    *,
    payload: Mapping[str, Any] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 43123),
) -> tuple[int, dict[str, Any], dict[str, str]]:
    status, body, headers = _request_bytes(
        app,
        method,
        path,
        payload=payload,
        client=client,
    )
    return status, json.loads(body), headers


def _create_app(
    database: Path,
    pack_root: Path,
    allowed_partitions: Iterable[PartitionRef],
    *,
    principal_name: str,
) -> Any:
    return create_app(
        db=database,
        principal=_principal(principal_name),
        allowed_partitions=allowed_partitions,
        managed_pack_root=pack_root,
    )


def _seed_memory(
    database: Path,
    pack_root: Path,
    scope: PartitionRef,
    *,
    content: str = "Evidence HTTP contract release checklist.",
) -> RememberReceipt:
    engine = MemoryEngine.local(database, managed_pack_root=pack_root).initialize()
    return engine.remember(
        RememberRequest(
            content=content,
            scope=scope,
            external_event_id="http-evidence-seed",
            origin=OriginContext(producer_id="http-evidence-test"),
        )
    )


def test_evidence_context_returns_only_downloadable_http_resource_uris(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "managed-packs"
    alpha = PartitionRef.workspace("alpha")
    receipt = _seed_memory(database, pack_root, alpha)
    app = _create_app(
        database,
        pack_root,
        [alpha],
        principal_name="alpha-http-reader",
    )

    status, evidence, response_headers = _request_json(
        app,
        "POST",
        "/v1/context",
        payload={
            "mode": "evidence",
            "query": "release checklist",
            "scope": _scope_payload(alpha),
            "token_budget": 800,
            "ttl_seconds": 60,
        },
    )

    assert status == 200
    assert evidence["mode"] == "evidence"
    assert evidence["contract_version"] == "1"
    assert evidence["request_id"] == response_headers["x-request-id"]
    assert evidence["untrusted_data"] is True
    assert evidence["managed"] is True
    assert evidence["selected_count"] == 1
    assert len(evidence["integrity_sha256"]) == 64
    assert response_headers["cache-control"] == "no-store"
    assert all(
        uri.startswith(
            f"/v1/evidence-packs/{evidence['pack_id']}/resources/",
        )
        for uri in evidence["resource_uris"]
    )
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert "memory://" not in serialized
    assert str(database) not in serialized
    assert str(database.resolve()) not in serialized
    assert str(pack_root) not in serialized
    assert str(pack_root.resolve()) not in serialized

    memory_uri = next(
        uri for uri in evidence["resource_uris"] if f"/memories/{receipt.claim_id}.md" in uri
    )
    memory_status, memory_body, memory_headers = _request_bytes(
        app,
        "GET",
        memory_uri,
    )
    assert memory_status == 200
    assert b"Evidence HTTP contract release checklist." in memory_body
    assert memory_headers["content-type"] == "text/markdown; charset=utf-8"
    assert memory_headers["cache-control"] == "no-store"

    manifest_uri = next(uri for uri in evidence["resource_uris"] if uri.endswith("/manifest.json"))
    manifest_status, manifest_body, manifest_headers = _request_bytes(
        app,
        "GET",
        manifest_uri,
    )
    assert manifest_status == 200
    assert manifest_headers["content-type"] == "application/json"
    assert manifest_headers["cache-control"] == "no-store"
    assert json.loads(manifest_body)["pack_id"] == evidence["pack_id"]
    assert hashlib.sha256(manifest_body).hexdigest() == evidence["integrity_sha256"]

    inspector_uri = next(
        uri for uri in evidence["resource_uris"] if uri.endswith("/inspector.html")
    )
    inspector_status, inspector_body, inspector_headers = _request_bytes(
        app,
        "GET",
        inspector_uri,
    )
    assert inspector_status == 200
    assert inspector_headers["content-type"] == "text/html; charset=utf-8"
    assert "connect-src 'none'" in inspector_headers["content-security-policy"]
    assert "frame-ancestors 'none'" in inspector_headers["content-security-policy"]
    assert inspector_body.startswith(b"<!doctype html>")
    assert b"connect-src 'none'" in inspector_body
    assert b".innerHTML" not in inspector_body


@pytest.mark.parametrize("ttl_seconds", [59, 604_801])
def test_evidence_context_rejects_ttl_outside_public_contract(
    tmp_path: Path,
    ttl_seconds: int,
) -> None:
    scope = PartitionRef.workspace("alpha")
    app = _create_app(
        tmp_path / "memory.sqlite3",
        tmp_path / "managed-packs",
        [scope],
        principal_name="ttl-http-reader",
    )

    status, body, _ = _request_json(
        app,
        "POST",
        "/v1/context",
        payload={
            "mode": "evidence",
            "query": "ttl boundary",
            "scope": _scope_payload(scope),
            "ttl_seconds": ttl_seconds,
        },
    )

    assert status == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_evidence_context_empty_partition_uses_public_not_found_envelope(
    tmp_path: Path,
) -> None:
    scope = PartitionRef.workspace("empty")
    app = _create_app(
        tmp_path / "memory.sqlite3",
        tmp_path / "managed-packs",
        [scope],
        principal_name="empty-http-reader",
    )

    status, body, headers = _request_json(
        app,
        "POST",
        "/v1/context",
        payload={
            "mode": "evidence",
            "query": "nothing is stored",
            "scope": _scope_payload(scope),
        },
    )

    assert status == 404
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["request_id"] == headers["x-request-id"]
    assert headers["cache-control"] == "no-store"


def test_evidence_resource_http_uri_encodes_each_path_segment() -> None:
    assert _evidence_resource_http_uri(
        "pack id",
        "memory://packs/pack id/nested folder/雪%2F.md",
    ) == ("/v1/evidence-packs/pack%20id/resources/nested%20folder/%E9%9B%AA%252F.md")
    assert (
        _evidence_resource_http_uri(
            "pack",
            "memory://packs/pack/%2E%2E/secret.md",
        )
        == "/v1/evidence-packs/pack/resources/%252E%252E/secret.md"
    )
    with pytest.raises(ValueError, match="invalid Evidence Pack resource URI"):
        _evidence_resource_http_uri(
            "pack",
            "memory://packs/pack/../secret.md",
        )


def test_inspector_http_resource_neutralizes_persisted_script_payload(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "managed-packs"
    scope = PartitionRef.workspace("alpha")
    attack = "</script><script>globalThis.pwned=true</script><img src=x onerror=alert(1)>"
    _seed_memory(
        database,
        pack_root,
        scope,
        content=f"persistent xss needle {attack}",
    )
    app = _create_app(
        database,
        pack_root,
        [scope],
        principal_name="xss-http-reader",
    )
    status, evidence, _ = _request_json(
        app,
        "POST",
        "/v1/context",
        payload={
            "mode": "evidence",
            "query": "persistent xss needle",
            "scope": _scope_payload(scope),
        },
    )
    assert status == 200
    inspector_uri = next(
        uri for uri in evidence["resource_uris"] if uri.endswith("/inspector.html")
    )

    inspector_status, inspector_body, inspector_headers = _request_bytes(
        app,
        "GET",
        inspector_uri,
    )

    assert inspector_status == 200
    assert inspector_headers["content-type"] == "text/html; charset=utf-8"
    assert attack.encode() not in inspector_body
    assert b"\\u003c/script\\u003e" in inspector_body
    assert b".innerHTML" not in inspector_body


def test_evidence_resource_hides_traversal_cross_partition_expiry_and_remote_access(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite3"
    pack_root = tmp_path / "managed-packs"
    alpha = PartitionRef.workspace("alpha")
    beta = PartitionRef.workspace("beta")
    _seed_memory(database, pack_root, alpha)
    alpha_app = _create_app(
        database,
        pack_root,
        [alpha],
        principal_name="alpha-http-reader",
    )
    status, evidence, _ = _request_json(
        alpha_app,
        "POST",
        "/v1/context",
        payload={
            "mode": "evidence",
            "query": "release checklist",
            "scope": _scope_payload(alpha),
            "ttl_seconds": 60,
        },
    )
    assert status == 200
    manifest_uri = next(uri for uri in evidence["resource_uris"] if uri.endswith("/manifest.json"))

    traversal_status, traversal, _ = _request_json(
        alpha_app,
        "GET",
        (f"/v1/evidence-packs/{evidence['pack_id']}/resources/memories/../manifest.json"),
    )
    assert traversal_status == 404
    assert traversal["error"]["code"] == "NOT_FOUND"

    encoded_traversal_status, encoded_traversal, _ = _request_json(
        alpha_app,
        "GET",
        (f"/v1/evidence-packs/{evidence['pack_id']}/resources/memories/%2E%2E/manifest.json"),
    )
    assert encoded_traversal_status == 404
    assert encoded_traversal["error"]["code"] == "NOT_FOUND"

    beta_app = _create_app(
        database,
        pack_root,
        [beta],
        principal_name="beta-http-reader",
    )
    cross_scope_status, cross_scope, _ = _request_json(
        beta_app,
        "GET",
        manifest_uri,
    )
    assert cross_scope_status == 404
    assert cross_scope["error"]["code"] == "NOT_FOUND"
    assert "partition" not in json.dumps(cross_scope).lower()

    remote_status, remote, _ = _request_json(
        alpha_app,
        "GET",
        manifest_uri,
        client=("203.0.113.9", 43123),
    )
    assert remote_status == 403
    assert remote["error"]["code"] == "SCOPE_DENIED"

    expires_at = datetime.fromisoformat(evidence["expires_at"])
    alpha_app.state.engine._clock = lambda: expires_at + timedelta(microseconds=1)
    expired_status, expired, _ = _request_json(
        alpha_app,
        "GET",
        manifest_uri,
    )
    assert expired_status == 404
    assert expired["error"]["code"] == "NOT_FOUND"
