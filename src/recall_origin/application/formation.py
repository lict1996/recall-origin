"""Policy gate for untrusted automatic-formation candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from recall_origin.contracts.v1 import FormationCandidate
from recall_origin.domain.enums import FormationOperation, MemoryKind, MemoryStatus

_INSTRUCTION_LIKE = re.compile(
    r"(?i)(ignore (all|any|the) previous|system prompt|developer message|"
    r"disable (safety|security)|bypass (approval|permission)|sudo\s+rm|"
    r"curl\s+[^|]+\|\s*(sh|bash))"
)


@dataclass(frozen=True, slots=True)
class FormationDecision:
    candidate: FormationCandidate
    status: MemoryStatus
    operation: FormationOperation
    policy_reason: str


class FormationPolicy:
    """Small, inspectable v1 gate; it deliberately never declares truth."""

    version = "formation-policy:v1"

    @property
    def policy_hash(self) -> str:
        material = json.dumps(
            {
                "version": self.version,
                "procedural_auto_active": False,
                "instruction_pattern": _INSTRUCTION_LIKE.pattern,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def evaluate(self, candidate: FormationCandidate) -> FormationDecision:
        if candidate.operation is FormationOperation.IGNORE:
            return FormationDecision(
                candidate=candidate,
                status=MemoryStatus.REJECTED,
                operation=FormationOperation.IGNORE,
                policy_reason="Provider explicitly proposed ignore.",
            )
        if candidate.operation is FormationOperation.QUARANTINE or _INSTRUCTION_LIKE.search(
            candidate.content
        ):
            return FormationDecision(
                candidate=candidate,
                status=MemoryStatus.QUARANTINED,
                operation=FormationOperation.QUARANTINE,
                policy_reason="Instruction-like content requires governance review.",
            )
        if candidate.kind is MemoryKind.PROCEDURAL:
            return FormationDecision(
                candidate=candidate,
                status=MemoryStatus.CANDIDATE,
                operation=candidate.operation,
                policy_reason="Procedural memories never auto-activate.",
            )
        return FormationDecision(
            candidate=candidate,
            status=MemoryStatus.CANDIDATE,
            operation=candidate.operation,
            policy_reason="Automatic formation remains unverified pending governance.",
        )
