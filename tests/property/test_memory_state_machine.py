from __future__ import annotations

from tempfile import TemporaryDirectory

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import (
    IDEMPOTENCY_KEY_REUSED,
    NOT_FOUND,
    RecallOriginError,
)
from recall_origin.contracts.v1 import (
    ForgetRequest,
    ForgetTarget,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)
from recall_origin.domain.enums import ForgetTargetType


@settings(max_examples=12, stateful_step_count=20, deadline=None)
class MemoryLifecycleMachine(RuleBasedStateMachine):
    """A small model for replay, deletion fencing, search, and reindex."""

    def __init__(self) -> None:
        super().__init__()
        self._directory = TemporaryDirectory(prefix="recall-origin-state-")
        self.scope = PartitionRef.workspace("property")
        self.engine = MemoryEngine.local(
            f"{self._directory.name}/memory.sqlite3",
            purge_registry_key=b"s" * 32,
        ).initialize()
        self.events: dict[int, tuple[str, str]] = {}
        self.deleted: set[int] = set()

    def teardown(self) -> None:
        self.engine.close()
        self._directory.cleanup()

    @staticmethod
    def _content(slot: int) -> str:
        return f"propertytoken{slot} durable memory"

    @rule(slot=st.integers(min_value=0, max_value=3))
    def remember_or_replay(self, slot: int) -> None:
        request = RememberRequest(
            content=self._content(slot),
            scope=self.scope,
            memory_key=f"property.key.{slot}",
            external_event_id=f"property-event-{slot}",
            origin=OriginContext(producer_id="property-machine"),
        )
        if slot in self.deleted:
            try:
                self.engine.remember(request)
            except RecallOriginError as error:
                assert error.spec is IDEMPOTENCY_KEY_REUSED
            else:
                raise AssertionError("a deleted external event was resurrected")
            return

        receipt = self.engine.remember(request)
        if slot in self.events:
            event_id, claim_id = self.events[slot]
            assert receipt.replayed is True
            assert receipt.event_id == event_id
            assert receipt.claim_id == claim_id
        else:
            assert receipt.replayed is False
            self.events[slot] = (receipt.event_id, receipt.claim_id)

    @rule(slot=st.integers(min_value=0, max_value=3))
    def delete_event(self, slot: int) -> None:
        if slot not in self.events or slot in self.deleted:
            return
        event_id, _ = self.events[slot]
        receipt = self.engine.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.EVENT,
                    target_id=event_id,
                ),
                idempotency_key=f"delete-property-event-{slot}",
            )
        )
        replay = self.engine.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.EVENT,
                    target_id=event_id,
                ),
                idempotency_key=f"delete-property-event-{slot}",
            )
        )
        assert replay.replayed is True
        assert replay.deletion_id == receipt.deletion_id
        self.deleted.add(slot)

    @rule()
    def reindex(self) -> None:
        first = self.engine.reindex()
        second = self.engine.reindex()
        assert first["indexed"] == second["indexed"]

    @invariant()
    def model_matches_visible_search_state(self) -> None:
        for slot, (_, claim_id) in self.events.items():
            hits = self.engine.search(SearchRequest(query=f"propertytoken{slot}", scope=self.scope))
            if slot in self.deleted:
                assert hits == ()
                try:
                    self.engine.get(claim_id)
                except RecallOriginError as error:
                    assert error.spec is NOT_FOUND
                else:
                    raise AssertionError("deleted claim remained visible")
            else:
                assert [hit.claim_id for hit in hits] == [claim_id]


TestMemoryLifecycle = MemoryLifecycleMachine.TestCase
