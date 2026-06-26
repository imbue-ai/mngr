from datetime import datetime
from datetime import timezone

from imbue.minds.desktop_client.workspace_operations import OPERATION_LOG_SENTINEL
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
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
