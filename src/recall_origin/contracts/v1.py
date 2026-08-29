"""Public v1 request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from recall_origin.domain.enums import (
    Capability,
    Confirmation,
    DeletionState,
    FeedbackType,
    ForgetTargetType,
    FormationOperation,
    GovernAction,
    IndexState,
    MemoryKind,
    MemoryStatus,
    MemorySubtype,
    NamespaceKind,
    PrincipalType,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _reject_control_characters(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("identity fields must not contain control characters")
    return value


class PartitionRef(StrictModel):
    namespace_kind: NamespaceKind
    namespace_id: str = Field(min_length=1, max_length=256)

    _validate_namespace_id = field_validator("namespace_id")(_reject_control_characters)

    @classmethod
    def workspace(cls, namespace_id: str) -> PartitionRef:
        return cls(namespace_kind=NamespaceKind.WORKSPACE, namespace_id=namespace_id)

    @classmethod
    def parse(cls, value: str) -> PartitionRef:
        kind, separator, namespace_id = value.partition(":")
        if not separator or not namespace_id:
            raise ValueError("scope must use '<kind>:<id>'")
        return cls(namespace_kind=NamespaceKind(kind), namespace_id=namespace_id)

    def serialize(self) -> str:
        return f"{self.namespace_kind.value}:{self.namespace_id}"


class SubjectRef(StrictModel):
    subject_type: str = Field(min_length=1, max_length=64)
    subject_id: str = Field(min_length=1, max_length=256)

    _validate_subject_fields = field_validator("subject_type", "subject_id")(
        _reject_control_characters
    )


class OriginContext(StrictModel):
    session_id: str | None = Field(default=None, max_length=256)
    host_agent_id: str | None = Field(default=None, max_length=256)
    producer_id: str = Field(default="python-sdk", min_length=1, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)

    @field_validator("session_id", "host_agent_id", "producer_id", "request_id")
    @classmethod
    def validate_origin_identity(cls, value: str | None) -> str | None:
        return None if value is None else _reject_control_characters(value)


class PrincipalContext(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    principal_id: str = Field(min_length=1, max_length=256)
    principal_type: PrincipalType
    capabilities: frozenset[Capability] = frozenset()
    auto_grant_local_scopes: bool = False

    _validate_principal_fields = field_validator("tenant_id", "principal_id")(
        _reject_control_characters
    )

    @classmethod
    def local_human(cls) -> PrincipalContext:
        return cls(
            tenant_id="local",
            principal_id="local-human",
            principal_type=PrincipalType.HUMAN,
            capabilities=frozenset(Capability),
            auto_grant_local_scopes=True,
        )


class CaptureRequest(StrictModel):
    scope: PartitionRef
    external_event_id: str = Field(min_length=1, max_length=512)
    event_type: str = Field(min_length=1, max_length=64)
    payload_schema_version: int = Field(default=1, ge=1)
    payload: str | dict[str, Any] | list[Any]
    origin: OriginContext = OriginContext()
    subjects: tuple[SubjectRef, ...] = Field(default=(), max_length=32)
    sensitivity: Literal["normal", "sensitive", "restricted"] = "normal"
    persist: bool = True
    event_time: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=512)


class CaptureReceipt(StrictModel):
    event_id: str | None
    job_id: str | None
    partition: PartitionRef
    status: Literal["accepted_pending", "no_store"]
    formation_version: str
    replayed: bool = False


class FormationCandidate(StrictModel):
    operation: FormationOperation
    kind: MemoryKind
    subtype: MemorySubtype = MemorySubtype.FACT
    content: str = Field(min_length=1, max_length=1_000_000)
    memory_key: str | None = Field(default=None, max_length=512)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    reason: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def validate_interval(self) -> FormationCandidate:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class FormationJobReceipt(StrictModel):
    job_id: str
    event_id: str
    status: Literal["done", "retry_wait", "dead_letter", "cancelled"]
    formation_id: str | None = None
    committed_claim_ids: tuple[str, ...] = ()
    attempt: int = Field(ge=0)
    error_code: str | None = None


class FormationJobStatus(StrictModel):
    job_id: str
    event_id: str
    partition: PartitionRef
    formation_version: str
    status: Literal[
        "pending",
        "leased",
        "retry_wait",
        "done",
        "dead_letter",
        "cancelled",
    ]
    attempt: int = Field(ge=0)
    formation_id: str | None = None
    committed_claim_ids: tuple[str, ...] = ()
    available_at: datetime
    leased_until: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None


class RememberRequest(StrictModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    scope: PartitionRef
    kind: MemoryKind = MemoryKind.SEMANTIC
    subtype: MemorySubtype = MemorySubtype.FACT
    memory_key: str | None = Field(default=None, max_length=512)
    subject: SubjectRef | None = None
    external_event_id: str = Field(min_length=1, max_length=512)
    idempotency_key: str | None = Field(default=None, max_length=512)
    origin: OriginContext = OriginContext()
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> RememberRequest:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class RememberReceipt(StrictModel):
    event_id: str
    claim_id: str
    revision_id: str
    partition: PartitionRef
    status: MemoryStatus
    confirmation: Confirmation
    source_count: int = Field(ge=0)
    index_state: IndexState
    replayed: bool = False


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=100_000)
    scope: PartitionRef
    limit: int = Field(default=8, ge=1, le=100)
    valid_at: datetime | None = None
    known_at_seq: int | None = Field(default=None, ge=1)
    include_candidates: bool = False


class SearchHit(StrictModel):
    claim_id: str
    revision_id: str
    partition: PartitionRef
    kind: MemoryKind
    subtype: MemorySubtype
    content: str
    status: MemoryStatus
    confirmation: Confirmation
    source_count: int = Field(ge=0)
    rank: int = Field(ge=1)
    rank_score: float = Field(ge=0)
    exact_score: float | None = None
    lexical_score: float | None = None
    vector_score: float | None = None
    rrf_score: float = Field(default=0.0, ge=0)


class SearchResult(StrictModel):
    retrieval_id: str | None
    items: tuple[SearchHit, ...]
    candidate_count: int = Field(ge=0)
    ranking_policy_version: int = Field(ge=1)
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()


class RetrievalCandidateTrace(StrictModel):
    claim_id: str
    exact_rank: int | None = Field(default=None, ge=1)
    exact_score: float | None = None
    lexical_rank: int | None = Field(default=None, ge=1)
    lexical_score: float | None = None
    vector_rank: int | None = Field(default=None, ge=1)
    vector_score: float | None = None
    rrf_score: float = Field(ge=0)
    final_score: float = Field(ge=0)
    selected: bool
    omission_reason: str | None = None


class RetrievalTrace(StrictModel):
    retrieval_id: str
    partition: PartitionRef
    keyed_query_hash: str = Field(min_length=64, max_length=64)
    ranking_policy_version: int = Field(ge=1)
    query_shape: dict[str, Any]
    candidates: tuple[RetrievalCandidateTrace, ...]
    selected_claim_ids: tuple[str, ...]
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    degraded: bool
    degradation_reasons: tuple[str, ...]
    duration_micros: int = Field(ge=0)
    created_at: datetime
    expires_at: datetime | None


class RevisionRecord(StrictModel):
    revision_id: str
    previous_revision_id: str | None
    status: MemoryStatus
    confirmation: Confirmation
    source_count: int = Field(ge=0)
    reason: str | None
    tx_from_seq: int = Field(ge=1)
    revision_time: datetime


class EvidenceRecord(StrictModel):
    evidence_id: str
    event_id: str | None
    excerpt: str | None
    available: bool


class MemoryRecord(StrictModel):
    claim_id: str
    revision_id: str
    partition: PartitionRef
    memory_key: str | None
    kind: MemoryKind
    subtype: MemorySubtype
    content: str
    status: MemoryStatus
    confirmation: Confirmation
    source_count: int = Field(ge=0)
    valid_from: datetime | None
    valid_to: datetime | None
    subjects: tuple[SubjectRef, ...] = ()
    revisions: tuple[RevisionRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()


class ContextQuery(StrictModel):
    query: str = Field(min_length=1, max_length=100_000)
    scope: PartitionRef
    token_budget: int = Field(default=800, ge=32, le=100_000)
    limit: int = Field(default=8, ge=1, le=100)
    valid_at: datetime | None = None
    known_at_seq: int | None = Field(default=None, ge=1)


class ContextItem(StrictModel):
    claim_id: str
    revision_id: str
    content: str
    kind: MemoryKind
    confirmation: Confirmation
    source_count: int
    why: dict[str, Any]


class OmittedContextItem(StrictModel):
    claim_id: str
    reason: Literal["token_budget", "not_active", "duplicate"]


class FastContextPack(StrictModel):
    mode: Literal["fast"] = "fast"
    scope: PartitionRef
    query: str
    token_budget: int
    token_count: int
    items: tuple[ContextItem, ...]
    omitted: tuple[OmittedContextItem, ...]
    retrieval_id: str | None = None
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()


class EvidenceContextPack(StrictModel):
    mode: Literal["evidence"] = "evidence"
    pack_id: str
    retrieval_id: str
    scope: PartitionRef
    query: str
    token_budget: int
    token_count: int
    selected_count: int = Field(ge=0)
    manifest: str
    resource_uris: tuple[str, ...]
    integrity_sha256: str = Field(min_length=64, max_length=64)
    created_at: datetime
    expires_at: datetime
    managed: Literal[True] = True
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()


class GovernRequest(StrictModel):
    claim_id: str
    expected_revision_id: str
    action: GovernAction
    reason: str = Field(min_length=1, max_length=4_000)


class GovernReceipt(StrictModel):
    claim_id: str
    previous_revision_id: str
    revision_id: str
    status: MemoryStatus
    confirmation: Confirmation


class FeedbackRequest(StrictModel):
    claim_id: str
    revision_id: str
    feedback_type: FeedbackType
    reason: str | None = Field(default=None, max_length=4_000)


class FeedbackReceipt(StrictModel):
    feedback_id: str
    claim_id: str
    revision_id: str
    appended: Literal[True] = True


class ForgetTarget(StrictModel):
    target_type: ForgetTargetType
    target_id: str = Field(min_length=1, max_length=512)

    @classmethod
    def parse(cls, value: str) -> ForgetTarget:
        target_type, separator, target_id = value.partition(":")
        if not separator or not target_id:
            raise ValueError("target must use '<type>:<id>'")
        return cls(target_type=ForgetTargetType(target_type), target_id=target_id)


class ForgetRequest(StrictModel):
    target: ForgetTarget
    scope: PartitionRef | None = None
    idempotency_key: str = Field(min_length=1, max_length=512)
    expected_revision_id: str | None = None
    cascade_policy: Literal["safe", "purge"] = "safe"

    @model_validator(mode="after")
    def validate_expected_revision_target(self) -> ForgetRequest:
        if (
            self.expected_revision_id is not None
            and self.target.target_type is not ForgetTargetType.CLAIM
        ):
            raise ValueError("expected_revision_id is only valid for claim deletion")
        return self


class DeletionLayerStatus(StrictModel):
    layer: str
    state: DeletionState
    attempt: int = Field(ge=0)
    last_verified_at: datetime | None = None
    error_code: str | None = None


class DeletionReceipt(StrictModel):
    deletion_id: str
    target: ForgetTarget
    state: DeletionState
    logically_hidden_at: datetime | None
    completed_at: datetime | None
    layers: tuple[DeletionLayerStatus, ...]
    external_copies: tuple[str, ...] = ()
    replayed: bool = False
