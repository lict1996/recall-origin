"""Provider boundary for automatic memory formation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from recall_origin.contracts.v1 import FormationCandidate, PartitionRef, SubjectRef


@dataclass(frozen=True, slots=True)
class CapturedEvent:
    event_id: str
    partition: PartitionRef
    event_type: str
    payload_schema_version: int
    payload: str | dict[str, Any] | list[Any]
    subjects: tuple[SubjectRef, ...]


class FormationProvider(Protocol):
    """A deterministic or model-backed candidate extractor.

    Provider output is always treated as an untrusted proposal.  Confirmation
    and activation are assigned only by the engine's policy gate.
    """

    @property
    def fingerprint(self) -> str: ...

    def extract(self, event: CapturedEvent) -> tuple[FormationCandidate, ...]: ...
