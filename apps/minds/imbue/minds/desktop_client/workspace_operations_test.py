import threading
import time
from datetime import datetime
from datetime import timezone

from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import MAX_OPERATION_LOG_LINES
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.mngr.primitives import AgentId


def _now() -> datetime:
    return datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_start_registers_a_running_record_with_a_readable_log() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()

    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.RUNNING
    assert record.kind == WorkspaceOperationKind.RESTART
    assert record.error is None
    chunk = registry.read_log_chunk(agent_id, 0, timeout_seconds=0.01)
    assert chunk is not None
    assert chunk.lines == ()
    assert chunk.is_terminal is False


def test_get_returns_none_for_an_unknown_operation() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    assert registry.get(AgentId()) is None
    assert registry.read_log_chunk(AgentId(), 0, timeout_seconds=0.01) is None


def test_complete_marks_done_and_ends_the_log_stream() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())

    registry.append_log(agent_id, "stopping services")
    registry.complete(agent_id)

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.DONE
    assert record.warning is None
    chunk = registry.read_log_chunk(agent_id, 0, timeout_seconds=0.01)
    assert chunk is not None
    assert chunk.lines == ("stopping services",)
    assert chunk.is_terminal is True


def test_fail_marks_failed_with_error_and_ends_the_log_stream() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.RESTART, now=_now())

    registry.fail(agent_id, "mngr start exited 1")

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.FAILED
    assert record.error == "mngr start exited 1"
    chunk = registry.read_log_chunk(agent_id, 0, timeout_seconds=0.01)
    assert chunk is not None
    assert chunk.is_terminal is True


def test_cancel_marks_cancelled_without_an_error() -> None:
    # A user cancel honored before mutation is a neutral terminal state, not a
    # failure: no error text, and the UI renders it without error styling.
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())

    registry.cancel(agent_id)

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.CANCELLED
    assert record.error is None
    chunk = registry.read_log_chunk(agent_id, 0, timeout_seconds=0.01)
    assert chunk is not None
    assert chunk.is_terminal is True


def test_complete_with_warning_carries_the_caveat_on_a_done_record() -> None:
    # A restore that succeeded but whose chained update failed ends DONE with
    # a warning -- failing the whole operation would misrepresent the restore.
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())

    registry.complete_with_warning(agent_id, "The backup service update failed afterwards.")

    record = registry.get(agent_id)
    assert record is not None
    assert record.status == WorkspaceOperationStatus.DONE
    assert record.error is None
    assert record.warning == "The backup service update failed afterwards."


def test_read_log_chunk_replays_history_to_a_late_reader() -> None:
    # A page attaching mid-operation must see everything logged so far, and
    # two concurrent readers must both see the full stream (the old
    # consume-once queue split lines between readers).
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())
    registry.append_log(agent_id, "line 1")
    registry.append_log(agent_id, "line 2")

    first_reader = registry.read_log_chunk(agent_id, 0, timeout_seconds=0.01)
    second_reader = registry.read_log_chunk(agent_id, 0, timeout_seconds=0.01)

    assert first_reader is not None and second_reader is not None
    assert first_reader.lines == ("line 1", "line 2")
    assert second_reader.lines == ("line 1", "line 2")
    # An incremental read from the returned index yields only newer lines.
    registry.append_log(agent_id, "line 3")
    tail = registry.read_log_chunk(agent_id, first_reader.next_index, timeout_seconds=0.01)
    assert tail is not None
    assert tail.lines == ("line 3",)


def test_read_log_chunk_wakes_promptly_when_a_line_arrives() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, now=_now())
    results: list[tuple[str, ...]] = []
    is_waiting = threading.Event()

    def _read_and_record() -> None:
        is_waiting.set()
        chunk = registry.read_log_chunk(agent_id, 0, timeout_seconds=30.0)
        assert chunk is not None
        results.append(chunk.lines)

    reader = threading.Thread(target=_read_and_record)
    reader.start()
    assert is_waiting.wait(timeout=10.0)

    started_at = time.monotonic()
    registry.append_log(agent_id, "woke up")
    reader.join(timeout=10.0)

    assert not reader.is_alive()
    assert results == [("woke up",)]
    assert time.monotonic() - started_at < 10.0


def test_log_cap_drops_oldest_lines_but_keeps_indices_logical() -> None:
    registry = InMemoryWorkspaceOperationRegistry()
    agent_id = AgentId()
    registry.start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now=_now())
    total = MAX_OPERATION_LOG_LINES + 10
    for i in range(total):
        registry.append_log(agent_id, f"line {i}")

    chunk = registry.read_log_chunk(agent_id, 0, timeout_seconds=0.01)

    assert chunk is not None
    assert len(chunk.lines) == MAX_OPERATION_LOG_LINES
    # The oldest lines were dropped; the newest survive and the next-index
    # keeps counting logically past the cap.
    assert chunk.lines[0] == "line 10"
    assert chunk.lines[-1] == f"line {total - 1}"
    assert chunk.next_index == total


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
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, _now(), None) is True
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, _now(), None) is False
    record = registry.get(agent_id)
    assert record is not None
    assert record.kind == WorkspaceOperationKind.BACKUP_UPDATE

    # A finished (DONE, FAILED, or CANCELLED) record no longer blocks a fresh claim.
    registry.complete(agent_id)
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, _now(), None) is True
    registry.fail(agent_id, "boom")
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_UPDATE, _now(), None) is True
    registry.cancel(agent_id)
    assert registry.start_if_idle(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, _now(), None) is True


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
    registry.cancel(agent_id)

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
