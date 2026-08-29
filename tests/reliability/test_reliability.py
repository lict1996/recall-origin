from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from recall_origin import MemoryEngine
from recall_origin.contracts.errors import REVISION_CONFLICT, RecallOriginError
from recall_origin.contracts.v1 import (
    CaptureRequest,
    ForgetRequest,
    ForgetTarget,
    FormationCandidate,
    GovernRequest,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)
from recall_origin.domain.enums import (
    ForgetTargetType,
    FormationOperation,
    GovernAction,
    MemoryKind,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _remember_request(scope: PartitionRef) -> RememberRequest:
    return RememberRequest(
        content="one durable visible fact",
        scope=scope,
        memory_key="reliability.fact",
        external_event_id="replayed-request",
        idempotency_key="replayed-idempotency-key",
        origin=OriginContext(producer_id="reliability-gate"),
    )


def test_repeated_request_replay_has_one_visible_fact_and_one_event(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    engine = MemoryEngine.local(tmp_path / "replay.sqlite3").initialize()
    request = _remember_request(scope)

    receipts = [engine.remember(request) for _ in range(250)]

    assert len({receipt.event_id for receipt in receipts}) == 1
    assert len({receipt.claim_id for receipt in receipts}) == 1
    assert all(receipt.replayed is (index > 0) for index, receipt in enumerate(receipts))
    assert len(engine.search(SearchRequest(query="durable visible fact", scope=scope))) == 1
    with engine.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM memory_claims").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM claim_heads").fetchone()[0] == 1


def test_multithreaded_revision_cas_allows_exactly_one_governance_commit(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "cas.sqlite3"
    owner = MemoryEngine.local(database).initialize()
    remembered = owner.remember(_remember_request(scope))
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        engine = MemoryEngine.local(database).initialize()
        barrier.wait(timeout=10)
        try:
            engine.govern(
                GovernRequest(
                    claim_id=remembered.claim_id,
                    expected_revision_id=remembered.revision_id,
                    action=GovernAction.QUARANTINE,
                    reason=f"concurrent CAS worker {index}",
                )
            )
        except RecallOriginError as exc:
            outcome = exc.spec.code
        else:
            outcome = "success"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count("success") == 1
    assert outcomes.count(REVISION_CONFLICT.code) == 7
    with owner.store.connection() as connection:
        assert (
            connection.execute(
                """
            SELECT count(*) FROM memory_revisions
            WHERE claim_id = ? AND previous_revision_id = ?
            """,
                (remembered.claim_id, remembered.revision_id),
            ).fetchone()[0]
            == 1
        )


class _OneCandidateProvider:
    fingerprint = "reliability-provider:v1"

    def __init__(self, callback: object | None = None) -> None:
        self.callback = callback

    def extract(self, event: object) -> tuple[FormationCandidate, ...]:
        del event
        if callable(self.callback):
            self.callback()
        return (
            FormationCandidate(
                operation=FormationOperation.ADD,
                kind=MemoryKind.SEMANTIC,
                content="leased formation fact",
                reason="Reliability lease test.",
            ),
        )


def test_expired_lease_takeover_fences_old_worker_and_delete_wins(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    now = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]
    database = tmp_path / "lease.sqlite3"
    first = MemoryEngine.local(database, clock=lambda: now[0]).initialize()
    second = MemoryEngine.local(database, clock=lambda: now[0]).initialize()
    captured = first.capture(
        CaptureRequest(
            scope=scope,
            external_event_id="leased-event",
            event_type="host_observation",
            payload={"memory_candidates": []},
            origin=OriginContext(producer_id="lease-gate"),
        )
    )
    takeover = None

    def take_over_then_delete() -> None:
        nonlocal takeover
        now[0] += timedelta(seconds=2)
        takeover = second.process_formation_job(
            _OneCandidateProvider(),
            job_id=captured.job_id,
            worker_id="new-worker",
            lease_seconds=1,
        )
        second.forget(
            ForgetRequest(
                target=ForgetTarget(
                    target_type=ForgetTargetType.EVENT,
                    target_id=captured.event_id or "",
                ),
                idempotency_key="delete-after-takeover",
                cascade_policy="purge",
            )
        )

    stale = first.process_formation_job(
        _OneCandidateProvider(take_over_then_delete),
        job_id=captured.job_id,
        worker_id="old-worker",
        lease_seconds=1,
    )

    assert takeover is not None and takeover.status == "done"
    assert stale is not None and stale.status == "cancelled"
    with first.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM formation_runs").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT status FROM outbox_messages WHERE outbox_id = ?",
                (captured.job_id,),
            ).fetchone()[0]
            == "done"
        )
    assert (
        first.search(
            SearchRequest(query="leased formation fact", scope=scope, include_candidates=True)
        )
        == ()
    )


def test_abrupt_uncommitted_connection_close_recovers_idempotently(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "interrupted.sqlite3"
    engine = MemoryEngine.local(database).initialize()
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        INSERT INTO ledger_transactions(tx_id, operation, committed_at)
        VALUES ('tx_interrupted', 'interrupted-test', 1)
        """
    )
    connection.close()  # Simulates a killed owner: SQLite must roll back the transaction.

    request = _remember_request(scope)
    first = engine.remember(request)
    replay = engine.remember(request)

    assert replay.replayed is True
    assert replay.event_id == first.event_id
    with engine.store.connection() as recovered:
        assert (
            recovered.execute(
                "SELECT count(*) FROM ledger_transactions WHERE tx_id = 'tx_interrupted'"
            ).fetchone()[0]
            == 0
        )
        assert recovered.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission classification gate")
def test_read_only_database_is_classified_as_retryable_temporary_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "readonly" / "memory.sqlite3"
    database.parent.mkdir()
    engine = MemoryEngine.local(database).initialize()
    engine.close()
    protected = [path for path in database.parent.iterdir() if path.is_file()]
    original_modes = {path: path.stat().st_mode & 0o777 for path in protected}
    directory_mode = database.parent.stat().st_mode & 0o777
    try:
        for path in protected:
            path.chmod(0o444)
        database.parent.chmod(0o555)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "recall_origin.interfaces.cli",
                "remember",
                "read only write",
                "--scope",
                "workspace:alpha",
                "--external-event-id",
                "readonly-event",
                "--db",
                str(database),
                "--json",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        database.parent.chmod(directory_mode)
        for path, mode in original_modes.items():
            path.chmod(mode)

    envelope = json.loads(process.stdout)
    assert process.returncode != 0
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "TEMPORARY_FAILURE"
    assert envelope["error"]["retryable"] is True


def test_backup_restore_merges_current_purge_registry_and_cannot_resurrect(
    tmp_path: Path,
    scope: PartitionRef,
) -> None:
    database = tmp_path / "restore.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    engine = MemoryEngine.local(database).initialize()
    remembered = engine.remember(_remember_request(scope))
    with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
        source.backup(target)
    deletion = engine.forget(
        ForgetRequest(
            target=ForgetTarget(
                target_type=ForgetTargetType.EVENT,
                target_id=remembered.event_id,
            ),
            idempotency_key="delete-before-restore",
            cascade_policy="purge",
        )
    )
    engine.purge(deletion.deletion_id)

    with sqlite3.connect(snapshot) as source, sqlite3.connect(database) as target:
        source.backup(target)
    restored = MemoryEngine.local(database).initialize()

    assert restored.search(SearchRequest(query="durable visible fact", scope=scope)) == ()
    with pytest.raises(RecallOriginError):
        restored.get(remembered.claim_id)
