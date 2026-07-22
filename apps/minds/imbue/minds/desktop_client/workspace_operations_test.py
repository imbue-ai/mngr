import threading
import time
from datetime import datetime
from datetime import timezone

from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import OPERATION_LOG_SENTINEL
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.mngr.primitives import AgentId


def _now() -> datetime:
    return datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_start_registers_a_running_record_with_a_log_queue() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()

    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.RUNNING
    assert record.kind == WorkspaceOperationKind.RESTART
    assert record.error is None
    assert registry.get_log_queue(agent_id) is not None


def test_get_returns_none_for_an_unknown_operation() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    assert registry.get(AgentId()) is None
    assert registry.get_log_queue(AgentId()) is None


def test_complete_marks_done_and_closes_the_log_stream() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())

    registry.append_log(agent_id, "stopping services")
    registry.complete(agent_id)

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.DONE
    log_queue = registry.get_log_queue(agent_id)
    assert log_queue is not None
    assert log_queue.get_nowait() == "stopping services"
    assert log_queue.get_nowait() == OPERATION_LOG_SENTINEL


def test_fail_marks_failed_with_error_and_closes_the_log_stream() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())

    registry.fail(agent_id, "mngr start exited 1")

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.error == "mngr start exited 1"
    log_queue = registry.get_log_queue(agent_id)
    assert log_queue is not None
    assert log_queue.get_nowait() == OPERATION_LOG_SENTINEL


def test_start_again_replaces_the_prior_record() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())
    registry.fail(agent_id, "first attempt failed")

    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.RUNNING
    assert record.error is None


def test_start_if_idle_claims_only_while_no_operation_is_running() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()

    # First claim wins; a second claim while RUNNING is refused and does not
    # replace the record.
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now()) is True
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, now=_now()) is False
    record = registry.get(agent_id)
    assert record is not None
    assert record.kind == WorkspaceOperationKind.BACKUP_UPDATE

    # A finished (DONE or FAILED) record no longer blocks a fresh claim.
    registry.complete(agent_id)
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, now=_now()) is True
    registry.fail(agent_id, "boom")
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now()) is True


def test_request_cancel_flags_a_running_operation() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())
    assert registry.is_cancel_requested(agent_id) is False

    assert registry.request_cancel(agent_id) is True

    assert registry.is_cancel_requested(agent_id) is True
    # wait_for_cancel returns immediately once a cancel is pending.
    assert registry.wait_for_cancel(agent_id, timeout_seconds=5.0) is True


def test_request_cancel_is_refused_without_a_running_operation() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()

    # No operation at all.
    assert registry.request_cancel(agent_id) is False
    assert registry.is_cancel_requested(agent_id) is False

    # A finished operation cannot be cancelled either.
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())
    registry.complete(agent_id)
    assert registry.request_cancel(agent_id) is False
    assert registry.is_cancel_requested(agent_id) is False


def test_wait_for_cancel_times_out_without_a_cancel_request() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()

    # No operation: nothing to wait on.
    assert registry.wait_for_cancel(agent_id, timeout_seconds=0.01) is False

    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())
    assert registry.wait_for_cancel(agent_id, timeout_seconds=0.01) is False


def test_start_clears_a_prior_cancel_request() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())
    assert registry.request_cancel(agent_id) is True
    registry.fail(agent_id, "cancelled")

    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())

    assert registry.is_cancel_requested(agent_id) is False


def test_begin_mutation_closes_the_cancel_window() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())

    assert registry.begin_mutation(agent_id) is True

    record = registry.get(agent_id)
    assert record is not None
    assert record.is_mutating is True
    # A late cancel is refused and leaves no pending cancel flag behind (the
    # operation must run to completion, not absorb a cancel it will ignore).
    assert registry.request_cancel(agent_id) is False
    assert registry.is_cancel_requested(agent_id) is False


def test_begin_mutation_loses_to_an_earlier_cancel() -> None:
    # A cancel that arrives while the operation is still waiting wins the
    # race: the worker's begin_mutation claim is refused, so it ends the
    # operation as cancelled instead of mutating.
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())
    assert registry.request_cancel(agent_id) is True

    assert registry.begin_mutation(agent_id) is False

    record = registry.get(agent_id)
    assert record is not None
    assert record.is_mutating is False


def test_begin_mutation_requires_a_running_operation() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()

    # No operation at all.
    assert registry.begin_mutation(agent_id) is False

    # A finished operation cannot enter its mutating phase either.
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())
    registry.complete(agent_id)
    assert registry.begin_mutation(agent_id) is False


def test_a_fresh_start_resets_the_mutating_state() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())
    assert registry.begin_mutation(agent_id) is True
    registry.fail(agent_id, "boom")

    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())

    record = registry.get(agent_id)
    assert record is not None
    assert record.is_mutating is False
    assert registry.request_cancel(agent_id) is True


def test_request_cancel_wakes_a_blocked_wait_for_cancel_promptly() -> None:
    # Pins the lock discipline: wait_for_cancel must not hold the registry
    # lock while blocking. If it did, this request_cancel would stall for the
    # waiter's whole 30s timeout (as would every other registry call), and
    # the join bound below would fail.
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())
    results: list[bool] = []
    is_waiting = threading.Event()

    def _wait_and_record() -> None:
        is_waiting.set()
        results.append(registry.wait_for_cancel(agent_id, timeout_seconds=30.0))

    waiter = threading.Thread(target=_wait_and_record)
    waiter.start()
    # Barrier, not a sleep: the cancel is only sent once the waiter is at (or
    # inside) its blocking wait. If the cancel still slips in first, the wait
    # simply returns True immediately -- either interleaving passes, and only
    # the held-lock bug fails the time bound.
    assert is_waiting.wait(timeout=10.0)

    started_at = time.monotonic()
    assert registry.request_cancel(agent_id) is True
    waiter.join(timeout=10.0)

    assert not waiter.is_alive()
    assert results == [True]
    assert time.monotonic() - started_at < 10.0
