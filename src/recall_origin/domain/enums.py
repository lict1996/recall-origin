"""Closed domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class NamespaceKind(StrEnum):
    WORKSPACE = "workspace"
    USER = "user"
    AGENT_PRIVATE = "agent_private"
    SESSION_PRIVATE = "session_private"


class PrincipalType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


class Capability(StrEnum):
    READ = "read"
    WRITE = "write"
    GOVERN = "govern"
    DELETE = "delete"
    EXPORT = "export"
    STATS = "stats"
    ADMIN = "admin"


class MemoryKind(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class MemorySubtype(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    OUTCOME = "outcome"
    DECISION = "decision"
    WORKFLOW = "workflow"
    GOTCHA = "gotcha"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"
    UNSUPPORTED = "unsupported"
    WITHHELD = "withheld"
    REBUILDING = "rebuilding"
    REJECTED = "rejected"
    TOMBSTONED = "tombstoned"


class Confirmation(StrEnum):
    UNVERIFIED = "unverified"
    USER_CONFIRMED = "user_confirmed"
    SOURCE_VERIFIED = "source_verified"
    TEST_VERIFIED = "test_verified"


class FormationMode(StrEnum):
    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"


class FormationOperation(StrEnum):
    ADD = "add"
    REINFORCE = "reinforce"
    SUPERSEDE = "supersede"
    CONFLICT = "conflict"
    IGNORE = "ignore"
    QUARANTINE = "quarantine"


class OriginType(StrEnum):
    EXPLICIT_USER = "explicit_user"
    AGENT_CLAIM = "agent_claim"
    TOOL_RESULT = "tool_result"
    MODEL_DERIVED = "model_derived"


class GovernAction(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    QUARANTINE = "quarantine"
    ACTIVATE = "activate"


class ForgetTargetType(StrEnum):
    EVENT = "event"
    CLAIM = "claim"
    SUBJECT = "subject"
    PARTITION = "partition"
    MANAGED_PACK = "managed_pack"


class DeletionState(StrEnum):
    ACCEPTED = "accepted"
    LOGICALLY_HIDDEN = "logically_hidden"
    COMPLETED = "completed"
    FAILED = "failed"


class IndexState(StrEnum):
    READY = "ready"
    PENDING = "pending"
    DEGRADED = "degraded"


class FeedbackType(StrEnum):
    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"
    INCORRECT = "incorrect"
    STALE = "stale"
    PROPOSAL = "proposal"
