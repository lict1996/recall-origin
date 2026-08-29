"""Zero-network formation provider for explicitly structured event payloads."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError

from recall_origin.contracts.v1 import FormationCandidate
from recall_origin.providers.base import CapturedEvent

_CANDIDATES = TypeAdapter(tuple[FormationCandidate, ...])


class StructuredEventProvider:
    """Read ``memory_candidates`` from a trusted host's structured event.

    This provider is useful for integration tests and hosts that already have
    their own extractor.  It performs strict schema validation but does not
    make candidates trusted; the engine still applies its policy gate.
    """

    fingerprint = "structured-event-provider:v1"

    def extract(self, event: CapturedEvent) -> tuple[FormationCandidate, ...]:
        if not isinstance(event.payload, dict):
            return ()
        candidates: Any = event.payload.get("memory_candidates", ())
        try:
            return _CANDIDATES.validate_python(candidates)
        except ValidationError as exc:
            raise ValueError("memory_candidates failed strict schema validation") from exc
