"""Optional loopback-only HTTP adapter for the embedded memory engine.

The module itself is import-safe without the ``server`` extra.  Calling
``create_app`` loads FastAPI and raises an actionable error when the optional
dependency is unavailable.

This adapter deliberately has no request-level authentication.  A trusted
launcher fixes one principal and its exact allowed partitions for the whole
application, and middleware rejects non-loopback network clients.  Deploying
the ASGI app behind a remote proxy would cross that security boundary and is
not supported by this factory.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import sqlite3
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias, TypedDict, cast
from urllib.parse import quote
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from recall_origin import __version__
from recall_origin.application.engine import MemoryEngine
from recall_origin.contracts.errors import (
    FEATURE_NOT_ENABLED,
    IDEMPOTENCY_KEY_REUSED,
    NOT_FOUND,
    PURGE_REGISTRY_REQUIRED,
    REVISION_CONFLICT,
    SCOPE_DENIED,
    TEMPORARY_FAILURE,
    VALIDATION_ERROR,
    ErrorSpec,
    RecallOriginError,
)
from recall_origin.contracts.v1 import (
    ContextQuery,
    DeletionReceipt,
    EvidenceContextPack,
    FastContextPack,
    ForgetRequest,
    ForgetTarget,
    GovernReceipt,
    GovernRequest,
    MemoryRecord,
    OriginContext,
    PartitionRef,
    PrincipalContext,
    RememberReceipt,
    RememberRequest,
    SearchHit,
    SearchRequest,
    SubjectRef,
)
from recall_origin.domain.enums import (
    Capability,
    GovernAction,
    MemoryKind,
    MemorySubtype,
    NamespaceKind,
)
from recall_origin.storage.sqlite.errors import SQLiteFeatureError

if TYPE_CHECKING:
    from fastapi import FastAPI as FastAPIType

CONTRACT_VERSION: Literal["1"] = "1"
MAX_IDEMPOTENCY_ENTRIES = 2_048
INSPECTOR_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data:; "
    "connect-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)
_REQUEST_ID: ContextVar[str | None] = ContextVar("recall_origin_http_request_id", default=None)


class _HttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OriginContextInput(_HttpModel):
    """Untrusted origin hints; producer and request identity are server-stamped."""

    session_id: str | None = Field(default=None, max_length=256)
    host_agent_id: str | None = Field(default=None, max_length=256)


class RememberHttpRequest(_HttpModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    scope: PartitionRef
    kind: MemoryKind = MemoryKind.SEMANTIC
    subtype: MemorySubtype = MemorySubtype.FACT
    memory_key: str | None = Field(default=None, max_length=512)
    subject: SubjectRef | None = None
    external_event_id: str = Field(min_length=1, max_length=512)
    origin: OriginContextInput = Field(default_factory=OriginContextInput)
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class GovernHttpRequest(_HttpModel):
    expected_revision_id: str = Field(min_length=1, max_length=128)
    action: GovernAction
    reason: str = Field(min_length=1, max_length=4_000)


class DeletionHttpRequest(_HttpModel):
    target: ForgetTarget
    scope: PartitionRef | None = None
    expected_revision_id: str | None = Field(default=None, max_length=128)
    cascade_policy: Literal["safe", "purge"] = "safe"


class RememberHttpResponse(RememberReceipt):
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str


class SearchHttpResponse(_HttpModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str
    retrieval_id: str | None
    items: tuple[SearchHit, ...]
    candidate_count: int = Field(ge=0)
    ranking_policy_version: int = Field(ge=1)
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()
    untrusted_data: Literal[True] = True


class FastContextHttpRequest(ContextQuery):
    mode: Literal["fast"]


class EvidenceContextHttpRequest(ContextQuery):
    mode: Literal["evidence"]
    ttl_seconds: int = Field(default=86_400, ge=60, le=604_800)


ContextHttpRequest: TypeAlias = Annotated[
    FastContextHttpRequest | EvidenceContextHttpRequest,
    Field(discriminator="mode"),
]


class FastContextHttpResponse(FastContextPack):
    mode: Literal["fast"]
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str
    untrusted_data: Literal[True] = True


class EvidenceContextHttpResponse(EvidenceContextPack):
    mode: Literal["evidence"]
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str
    untrusted_data: Literal[True] = True


ContextHttpResponse: TypeAlias = Annotated[
    FastContextHttpResponse | EvidenceContextHttpResponse,
    Field(discriminator="mode"),
]


class MemoryGetHttpResponse(_HttpModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str
    memory: MemoryRecord
    untrusted_data: Literal[True] = True


class GovernHttpResponse(GovernReceipt):
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str
    replayed: bool = False


class DeletionHttpResponse(DeletionReceipt):
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str


class HealthHttpResponse(_HttpModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    contract_version: Literal["1"] = CONTRACT_VERSION


class StatsHttpResponse(_HttpModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str
    authorized_partition_count: int = Field(ge=0)
    current_claim_count: int = Field(ge=0)
    pending_job_count: int = Field(ge=0)
    deletion_backlog_count: int = Field(ge=0)


class ErrorBody(_HttpModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None


class ErrorResponse(_HttpModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    request_id: str
    error: ErrorBody


class _StatsCounts(TypedDict):
    authorized_partition_count: int
    current_claim_count: int
    pending_job_count: int
    deletion_backlog_count: int


class _IdempotencyCache:
    """Bounded same-process replay protection for HTTP-only write semantics.

    Remember and deletion also retain their durable engine-level idempotency.
    Governance has no v1 storage-level idempotency record, so this cache makes
    retries deterministic within one local server process without pretending
    to provide a cross-process guarantee.
    """

    def __init__(self, max_entries: int = MAX_IDEMPOTENCY_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], tuple[str, dict[str, Any]]] = OrderedDict()
        self._lock = RLock()

    def execute(
        self,
        *,
        operation: str,
        key: str,
        request_data: Mapping[str, Any],
        action: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        digest = _sha256_json(request_data)
        cache_key = (operation, key)
        with self._lock:
            existing = self._entries.get(cache_key)
            if existing is not None:
                previous_digest, previous_response = existing
                if previous_digest != digest:
                    raise RecallOriginError(
                        IDEMPOTENCY_KEY_REUSED,
                        "The Idempotency-Key was already used with a different payload.",
                    )
                self._entries.move_to_end(cache_key)
                replay = copy.deepcopy(previous_response)
                replay["replayed"] = True
                return replay

            response = dict(action())
            self._entries[cache_key] = (digest, copy.deepcopy(response))
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return response


def _load_fastapi() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    try:
        import fastapi
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse, Response
        from starlette.exceptions import HTTPException as StarletteHttpException
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError(
            "The HTTP adapter requires optional dependencies. "
            "Install them with `pip install 'recall-origin[server]'`."
        ) from exc
    return (
        fastapi.FastAPI,
        fastapi.Header,
        fastapi.Query,
        RequestValidationError,
        JSONResponse,
        Response,
        StarletteHttpException,
    )


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_id() -> str:
    current = _REQUEST_ID.get()
    return current if current is not None else f"req_{uuid4().hex}"


def _trusted_producer_id(principal: PrincipalContext) -> str:
    material = f"{principal.tenant_id}\0{principal.principal_id}".encode()
    return f"http:{hashlib.sha256(material).hexdigest()[:32]}"


def _context_query(body: FastContextHttpRequest | EvidenceContextHttpRequest) -> ContextQuery:
    return ContextQuery.model_validate(
        body.model_dump(exclude={"mode", "ttl_seconds"}),
    )


def _evidence_resource_http_uri(pack_id: str, resource_uri: str) -> str:
    prefix = f"memory://packs/{pack_id}/"
    if not resource_uri.startswith(prefix):
        raise ValueError("The engine returned an invalid Evidence Pack resource URI.")
    relative_path = resource_uri.removeprefix(prefix)
    parts = relative_path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("The engine returned an invalid Evidence Pack resource URI.")
    encoded_pack_id = quote(pack_id, safe="")
    encoded_path = "/".join(quote(part, safe="") for part in parts)
    return f"/v1/evidence-packs/{encoded_pack_id}/resources/{encoded_path}"


def _evidence_resource_media_type(resource_path: str) -> str:
    lowered = resource_path.lower()
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith(".html"):
        return "text/html"
    return "application/octet-stream"


def _stable_origin_request_id(operation: str, idempotency_key: str) -> str:
    material = f"{operation}\0{idempotency_key}".encode()
    return f"http_{hashlib.sha256(material).hexdigest()[:40]}"


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        # ASGI Unix-socket transports may not provide an IP client tuple.
        return True
    if host == "testclient":
        return True
    try:
        address = ipaddress.ip_address(host.split("%", maxsplit=1)[0])
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _model_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _model_data(item) for key, item in value.items()}
    if isinstance(value, tuple | list | set | frozenset):
        return [_model_data(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _validation_details(error: Any) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue.get("loc", ())) or "request"
        issues.append(
            {
                "field": location[:512],
                "message": str(issue.get("msg", "Invalid value."))[:1_024],
                "type": str(issue.get("type", "value_error"))[:128],
            }
        )
    return {"errors": issues}


def _error_status(spec: ErrorSpec) -> int:
    return {
        VALIDATION_ERROR.code: 422,
        SCOPE_DENIED.code: 403,
        NOT_FOUND.code: 404,
        REVISION_CONFLICT.code: 409,
        IDEMPOTENCY_KEY_REUSED.code: 409,
        TEMPORARY_FAILURE.code: 503,
        FEATURE_NOT_ENABLED.code: 501,
        PURGE_REGISTRY_REQUIRED.code: 503,
    }.get(spec.code, 500)


def _error_content(
    spec: ErrorSpec,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": spec.code,
        "message": (message.strip() or "The request failed.")[:4_096],
        "retryable": spec.retryable,
    }
    if details:
        body["details"] = _model_data(details)
    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id or _request_id(),
        "error": body,
    }


def _error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        status_code: {
            "model": ErrorResponse,
            "description": "Machine-readable RecallOrigin error.",
        }
        for status_code in status_codes
    }


def _authorized_stats(
    engine: MemoryEngine,
    configured_partitions: tuple[PartitionRef, ...],
) -> _StatsCounts:
    if Capability.STATS not in engine.principal.capabilities:
        raise RecallOriginError(
            SCOPE_DENIED,
            "The fixed principal is not authorized to read statistics.",
        )

    with engine._connection() as connection:
        row_ids: dict[str, str] = {}
        if engine.principal.auto_grant_local_scopes:
            rows = connection.execute(
                """
                SELECT partition_id, namespace_kind, namespace_id
                FROM partitions
                WHERE tenant_id = ?
                ORDER BY namespace_kind, namespace_id
                """,
                (engine.principal.tenant_id,),
            ).fetchall()
            candidates = tuple(
                PartitionRef(
                    namespace_kind=NamespaceKind(str(row["namespace_kind"])),
                    namespace_id=str(row["namespace_id"]),
                )
                for row in rows
            )
            row_ids = {
                PartitionRef(
                    namespace_kind=NamespaceKind(str(row["namespace_kind"])),
                    namespace_id=str(row["namespace_id"]),
                ).serialize(): str(row["partition_id"])
                for row in rows
            }
        else:
            candidates = tuple(
                {partition.serialize(): partition for partition in configured_partitions}.values()
            )
            for partition in candidates:
                row = connection.execute(
                    """
                    SELECT partition_id
                    FROM partitions
                    WHERE tenant_id = ? AND namespace_kind = ? AND namespace_id = ?
                    """,
                    (
                        engine.principal.tenant_id,
                        partition.namespace_kind.value,
                        partition.namespace_id,
                    ),
                ).fetchone()
                if row is not None:
                    row_ids[partition.serialize()] = str(row["partition_id"])

        authorized = tuple(
            partition
            for partition in candidates
            if engine.authorization.allows(Capability.STATS, partition)
        )
        partition_ids = [
            row_ids[partition.serialize()]
            for partition in authorized
            if partition.serialize() in row_ids
        ]
        if not partition_ids:
            return {
                "authorized_partition_count": len(authorized),
                "current_claim_count": 0,
                "pending_job_count": 0,
                "deletion_backlog_count": 0,
            }

        placeholders = ", ".join("?" for _ in partition_ids)
        current_claim_count = int(
            connection.execute(
                f"""
                SELECT COUNT(DISTINCT c.claim_id)
                FROM memory_claims AS c
                JOIN claim_heads AS h
                  ON h.partition_id = c.partition_id AND h.claim_id = c.claim_id
                JOIN memory_revisions AS r
                  ON r.partition_id = h.partition_id
                 AND r.claim_id = h.claim_id
                 AND r.revision_id = h.current_revision_id
                JOIN claim_contents AS cc ON cc.claim_id = c.claim_id
                WHERE c.partition_id IN ({placeholders})
                  AND r.status IN ('active', 'candidate', 'conflicted')
                  AND NOT EXISTS (
                    SELECT 1 FROM tombstone_fences AS tf
                    WHERE tf.scope_key = c.partition_id
                      AND (
                        (tf.target_type = 'claim' AND tf.target_id = c.claim_id)
                        OR tf.target_type = 'partition'
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM claim_subjects AS cs
                    JOIN subjects AS s
                      ON s.partition_id = cs.partition_id
                     AND s.subject_row_id = cs.subject_row_id
                    JOIN tombstone_fences AS sf
                      ON sf.scope_key = s.partition_id
                     AND sf.target_type = 'subject'
                     AND sf.target_id = s.subject_id
                    WHERE cs.partition_id = c.partition_id
                      AND cs.claim_id = c.claim_id
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM revision_evidence AS re
                    JOIN evidence_artifacts AS ea
                      ON ea.partition_id = re.partition_id
                     AND ea.evidence_id = re.evidence_id
                    JOIN evidence_bodies AS eb ON eb.evidence_id = ea.evidence_id
                    WHERE re.partition_id = r.partition_id
                      AND re.revision_id = r.revision_id
                      AND ea.availability = 'available'
                      AND NOT EXISTS (
                        SELECT 1 FROM tombstone_fences AS ef
                        WHERE ef.scope_key = ea.partition_id
                          AND ef.target_type = 'event'
                          AND ef.target_id = ea.event_id
                      )
                  )
                """,
                partition_ids,
            ).fetchone()[0]
        )
        pending_job_count = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM outbox_messages
                WHERE partition_id IN ({placeholders})
                  AND status IN ('pending', 'leased', 'retry_wait')
                """,
                partition_ids,
            ).fetchone()[0]
        )
        deletion_backlog_count = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM deletion_requests
                WHERE partition_id IN ({placeholders})
                  AND state <> 'completed'
                """,
                partition_ids,
            ).fetchone()[0]
        )
        return {
            "authorized_partition_count": len(authorized),
            "current_claim_count": current_claim_count,
            "pending_job_count": pending_job_count,
            "deletion_backlog_count": deletion_backlog_count,
        }


def create_app(
    *,
    db: str | Path,
    principal: PrincipalContext,
    allowed_partitions: Iterable[PartitionRef],
    durable: bool = True,
    purge_registry_path: str | Path | None = None,
    purge_registry_key: bytes | None = None,
    managed_pack_root: str | Path | None = None,
) -> Any:
    """Create a fixed-principal, loopback-only FastAPI application.

    The returned app must be served on ``127.0.0.1``, ``::1``, or a local Unix
    socket.  The middleware is defense in depth; it is not an authentication
    replacement and forwarded client headers are intentionally ignored.
    """

    (
        FastAPI,
        Header,
        Query,
        RequestValidationError,
        JSONResponse,
        Response,
        StarletteHttpException,
    ) = _load_fastapi()
    partitions = tuple(allowed_partitions)
    engine = MemoryEngine(
        db,
        principal=principal,
        allowed_partitions=partitions,
        durable=durable,
        purge_registry_path=purge_registry_path,
        purge_registry_key=purge_registry_key,
        managed_pack_root=managed_pack_root,
    )
    idempotency = _IdempotencyCache()
    app = cast(
        "FastAPIType",
        FastAPI(
            title="RecallOrigin API",
            version=__version__,
            description=(
                "Loopback-only local HTTP adapter backed by a fixed principal and exact "
                "partitions. There is no HTTP authentication in this adapter; it is not safe "
                "for remote binding or deployment behind a network proxy."
            ),
            servers=[
                {
                    "url": "http://127.0.0.1:8765",
                    "description": "Loopback-only local server",
                }
            ],
            openapi_tags=[
                {"name": "Memories"},
                {"name": "Retrieval"},
                {"name": "Governance"},
                {"name": "Deletion"},
                {"name": "Operations"},
            ],
        ),
    )
    app.state.engine = engine
    app.state.security_mode = "loopback-only-fixed-principal"
    app.state.allowed_partitions = partitions

    @app.middleware("http")
    async def enforce_local_transport(request: Any, call_next: Any) -> Any:
        request_id = f"req_{uuid4().hex}"
        token = _REQUEST_ID.set(request_id)
        try:
            client = getattr(request, "client", None)
            client_host = None if client is None else getattr(client, "host", None)
            if not _is_loopback_host(client_host):
                response = JSONResponse(
                    status_code=403,
                    content=_error_content(
                        SCOPE_DENIED,
                        "This unauthenticated adapter accepts loopback clients only.",
                        request_id=request_id,
                    ),
                )
            else:
                response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        finally:
            _REQUEST_ID.reset(token)

    @app.exception_handler(RecallOriginError)
    async def handle_public_error(_: Any, error: RecallOriginError) -> Any:
        return JSONResponse(
            status_code=_error_status(error.spec),
            content=_error_content(
                error.spec,
                error.message,
                details=error.details,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Any, error: Any) -> Any:
        return JSONResponse(
            status_code=422,
            content=_error_content(
                VALIDATION_ERROR,
                "The request did not match the v1 HTTP contract.",
                details=_validation_details(error),
            ),
        )

    @app.exception_handler(ValidationError)
    async def handle_runtime_validation(_: Any, error: ValidationError) -> Any:
        return JSONResponse(
            status_code=422,
            content=_error_content(
                VALIDATION_ERROR,
                "The request did not match the v1 runtime contract.",
                details=_validation_details(error),
            ),
        )

    @app.exception_handler(StarletteHttpException)
    async def handle_http_error(_: Any, error: Any) -> Any:
        if int(error.status_code) == 404:
            spec = NOT_FOUND
            message = "The requested HTTP resource was not found."
        else:
            spec = VALIDATION_ERROR
            message = "The HTTP method or request target is not valid."
        return JSONResponse(
            status_code=int(error.status_code),
            content=_error_content(spec, message),
        )

    @app.exception_handler(SQLiteFeatureError)
    async def handle_sqlite_feature_error(_: Any, error: SQLiteFeatureError) -> Any:
        return JSONResponse(
            status_code=501,
            content=_error_content(FEATURE_NOT_ENABLED, str(error)),
        )

    @app.exception_handler(ValueError)
    @app.exception_handler(TypeError)
    async def handle_value_error(_: Any, error: ValueError | TypeError) -> Any:
        return JSONResponse(
            status_code=422,
            content=_error_content(VALIDATION_ERROR, str(error)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Any, error: Exception) -> Any:
        if isinstance(error, (OSError, sqlite3.Error)):
            message = "The local memory store is temporarily unavailable."
        else:
            message = "The request failed because of an unexpected internal error."
        return JSONResponse(
            status_code=503 if isinstance(error, (OSError, sqlite3.Error)) else 500,
            content=_error_content(TEMPORARY_FAILURE, message),
        )

    @app.post(
        "/v1/memories",
        operation_id="rememberMemory",
        tags=["Memories"],
        status_code=201,
        response_model=RememberHttpResponse,
        responses=_error_responses(403, 409, 422, 503),
        summary="Explicitly remember a claim",
    )
    def remember_memory(
        body: RememberHttpRequest,
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=8,
            max_length=512,
        ),
    ) -> dict[str, Any]:
        request_data = body.model_dump(mode="json")

        def remember() -> Mapping[str, Any]:
            request = RememberRequest(
                content=body.content,
                scope=body.scope,
                kind=body.kind,
                subtype=body.subtype,
                memory_key=body.memory_key,
                subject=body.subject,
                external_event_id=body.external_event_id,
                idempotency_key=idempotency_key,
                origin=OriginContext(
                    session_id=body.origin.session_id,
                    host_agent_id=body.origin.host_agent_id,
                    producer_id=_trusted_producer_id(principal),
                    request_id=_stable_origin_request_id("remember", idempotency_key),
                ),
                valid_from=body.valid_from,
                valid_to=body.valid_to,
            )
            try:
                return engine.remember(request).model_dump(mode="json")
            except sqlite3.IntegrityError as error:
                if "events.partition_id, events.producer_id, events.idempotency_key" in str(error):
                    raise RecallOriginError(
                        IDEMPOTENCY_KEY_REUSED,
                        "The Idempotency-Key was already used with a different payload.",
                    ) from error
                raise

        receipt = idempotency.execute(
            operation="remember",
            key=idempotency_key,
            request_data=request_data,
            action=remember,
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "request_id": _request_id(),
            **receipt,
        }

    @app.post(
        "/v1/search",
        operation_id="searchMemories",
        tags=["Retrieval"],
        response_model=SearchHttpResponse,
        responses=_error_responses(403, 422, 503),
        summary="Search authorized current memories",
    )
    def search_memories(body: SearchRequest) -> SearchHttpResponse:
        result = engine.search_result(body)
        return SearchHttpResponse(
            request_id=_request_id(),
            retrieval_id=result.retrieval_id,
            items=result.items,
            candidate_count=result.candidate_count,
            ranking_policy_version=result.ranking_policy_version,
            degraded=result.degraded,
            degradation_reasons=result.degradation_reasons,
        )

    @app.post(
        "/v1/context",
        operation_id="buildContext",
        tags=["Retrieval"],
        response_model=ContextHttpResponse,
        responses=_error_responses(403, 404, 409, 422, 503),
        summary="Build a bounded fast or Evidence Context Pack",
    )
    def build_context(body: ContextHttpRequest) -> ContextHttpResponse:
        query = _context_query(body)
        if isinstance(body, EvidenceContextHttpRequest):
            evidence_pack = engine.evidence_context(query, ttl_seconds=body.ttl_seconds)
            return EvidenceContextHttpResponse(
                **evidence_pack.model_dump(exclude={"resource_uris"}),
                resource_uris=tuple(
                    _evidence_resource_http_uri(evidence_pack.pack_id, uri)
                    for uri in evidence_pack.resource_uris
                ),
                request_id=_request_id(),
            )
        fast_pack = engine.context(query)
        return FastContextHttpResponse(
            **fast_pack.model_dump(),
            request_id=_request_id(),
        )

    @app.get(
        "/v1/evidence-packs/{pack_id}/resources/{resource_path:path}",
        operation_id="getEvidencePackResource",
        tags=["Retrieval"],
        response_class=Response,
        responses={
            200: {
                "description": "Authorized, unexpired Evidence Pack resource.",
                "content": {
                    "application/json": {"schema": {}},
                    "text/markdown": {"schema": {"type": "string"}},
                    "text/html": {"schema": {"type": "string"}},
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"},
                    },
                },
            },
            **_error_responses(403, 404, 422, 503),
        },
        summary="Read an authorized Evidence Pack resource",
    )
    def get_evidence_pack_resource(pack_id: str, resource_path: str) -> Any:
        content = engine.read_evidence_pack_resource(pack_id, resource_path)
        media_type = _evidence_resource_media_type(resource_path)
        headers = (
            {"Content-Security-Policy": INSPECTOR_CONTENT_SECURITY_POLICY}
            if media_type == "text/html"
            else None
        )
        return Response(
            content=content,
            media_type=media_type,
            headers=headers,
        )

    @app.get(
        "/v1/memories/{claim_id}",
        operation_id="getMemory",
        tags=["Memories"],
        response_model=MemoryGetHttpResponse,
        responses=_error_responses(404, 422, 503),
        summary="Get an authorized memory with provenance",
    )
    def get_memory(
        claim_id: str,
        scope: str | None = Query(
            default=None,
            description="Optional exact scope constraint as '<kind>:<id>'.",
        ),
    ) -> MemoryGetHttpResponse:
        partition = None if scope is None else PartitionRef.parse(scope)
        return MemoryGetHttpResponse(
            request_id=_request_id(),
            memory=engine.get(claim_id, scope=partition),
        )

    @app.post(
        "/v1/memories/{claim_id}/govern",
        operation_id="governMemory",
        tags=["Governance"],
        response_model=GovernHttpResponse,
        responses=_error_responses(403, 404, 409, 422, 503),
        summary="Change governance state using revision compare-and-swap",
    )
    def govern_memory(
        claim_id: str,
        body: GovernHttpRequest,
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=8,
            max_length=512,
        ),
    ) -> dict[str, Any]:
        request_data = {
            "claim_id": claim_id,
            **body.model_dump(mode="json"),
        }

        def govern() -> Mapping[str, Any]:
            receipt = engine.govern(
                GovernRequest(
                    claim_id=claim_id,
                    expected_revision_id=body.expected_revision_id,
                    action=body.action,
                    reason=body.reason,
                )
            )
            return {
                **receipt.model_dump(mode="json"),
                "replayed": False,
            }

        receipt = idempotency.execute(
            operation=f"govern:{claim_id}",
            key=idempotency_key,
            request_data=request_data,
            action=govern,
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "request_id": _request_id(),
            **receipt,
        }

    @app.post(
        "/v1/deletions",
        operation_id="createDeletion",
        tags=["Deletion"],
        status_code=202,
        response_model=DeletionHttpResponse,
        responses=_error_responses(403, 404, 409, 422, 501, 503),
        summary="Submit a typed deletion request",
    )
    def create_deletion(
        body: DeletionHttpRequest,
        idempotency_key: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=8,
            max_length=512,
        ),
    ) -> dict[str, Any]:
        request_data = body.model_dump(mode="json")

        def forget() -> Mapping[str, Any]:
            receipt = engine.forget(
                ForgetRequest(
                    target=body.target,
                    scope=body.scope,
                    idempotency_key=idempotency_key,
                    expected_revision_id=body.expected_revision_id,
                    cascade_policy=body.cascade_policy,
                )
            )
            return receipt.model_dump(mode="json")

        receipt = idempotency.execute(
            operation="deletion",
            key=idempotency_key,
            request_data=request_data,
            action=forget,
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "request_id": _request_id(),
            **receipt,
        }

    @app.get(
        "/v1/deletions/{deletion_id}",
        operation_id="getDeletion",
        tags=["Deletion"],
        response_model=DeletionHttpResponse,
        responses=_error_responses(404, 503),
        summary="Get per-layer deletion status",
    )
    def get_deletion(deletion_id: str) -> DeletionHttpResponse:
        receipt = engine.deletion_status(deletion_id)
        return DeletionHttpResponse(
            **receipt.model_dump(),
            request_id=_request_id(),
        )

    @app.get(
        "/v1/health",
        operation_id="getHealth",
        tags=["Operations"],
        response_model=HealthHttpResponse,
        responses=_error_responses(503),
        summary="Read non-sensitive service health",
    )
    def get_health() -> HealthHttpResponse:
        diagnostics = engine.doctor()
        healthy = bool(diagnostics.get("ok")) and int(diagnostics.get("index_drift", 0)) == 0
        return HealthHttpResponse(status="healthy" if healthy else "degraded")

    @app.get(
        "/v1/stats",
        operation_id="getStats",
        tags=["Operations"],
        response_model=StatsHttpResponse,
        responses=_error_responses(403, 503),
        summary="Read exact-partition-authorized statistics",
    )
    def get_stats() -> StatsHttpResponse:
        return StatsHttpResponse(
            request_id=_request_id(),
            **_authorized_stats(engine, partitions),
        )

    return app


__all__ = [
    "CONTRACT_VERSION",
    "ContextHttpRequest",
    "ContextHttpResponse",
    "DeletionHttpRequest",
    "DeletionHttpResponse",
    "ErrorResponse",
    "EvidenceContextHttpRequest",
    "EvidenceContextHttpResponse",
    "FastContextHttpRequest",
    "FastContextHttpResponse",
    "GovernHttpRequest",
    "GovernHttpResponse",
    "HealthHttpResponse",
    "MemoryGetHttpResponse",
    "OriginContextInput",
    "RememberHttpRequest",
    "RememberHttpResponse",
    "SearchHttpResponse",
    "StatsHttpResponse",
    "create_app",
]
