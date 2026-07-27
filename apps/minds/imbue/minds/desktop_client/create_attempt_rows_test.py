from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptInfo
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.agent_creator import CreateAttemptErrorKind
from imbue.minds.desktop_client.create_attempt_rows import CreateAttemptRowKind
from imbue.minds.desktop_client.create_attempt_rows import derive_create_attempt_rows
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId


def _live_info(
    status: AgentCreateAttemptStatus,
    *,
    create_attempt_id: CreateAttemptId | None = None,
    host_name: str = "my-workspace",
    agent_id: AgentId | None = None,
    error: str | None = None,
) -> AgentCreateAttemptInfo:
    return AgentCreateAttemptInfo(
        create_attempt_id=create_attempt_id if create_attempt_id is not None else CreateAttemptId(),
        agent_id=agent_id,
        status=status,
        launch_mode=LaunchMode.LIMA,
        host_name=host_name,
        provider_instance_name="lima",
        error=error,
        error_kind=CreateAttemptErrorKind.GITHUB_AUTH_REQUIRED if error is not None else None,
    )


def _record(
    create_attempt_id: str,
    state: PendingCreateAttemptState,
    *,
    host_name: str = "my-workspace",
    display_name: str = "My Workspace",
    agent_id: str | None = None,
    error: str | None = None,
    created_at: datetime | None = None,
) -> PendingCreateAttemptRecord:
    effective_created_at = created_at if created_at is not None else datetime.now(timezone.utc)
    return PendingCreateAttemptRecord(
        create_attempt_id=create_attempt_id,
        state=state,
        provider_instance_name="lima",
        created_at=effective_created_at,
        updated_at=effective_created_at,
        agent_id=agent_id,
        error=error,
        request=PendingCreateAttemptRequest(
            repo_source="https://example.com/repo.git",
            host_name=host_name,
            display_name=display_name,
            launch_mode=LaunchMode.LIMA,
            account_email="owner@example.com",
            color="#a1b2c3",
        ),
    )


def test_live_non_terminal_create_attempt_derives_a_creating_row() -> None:
    info = _live_info(AgentCreateAttemptStatus.CREATING_WORKSPACE)
    rows = derive_create_attempt_rows([info], [], frozenset())
    assert [row.kind for row in rows] == [CreateAttemptRowKind.CREATING]
    assert rows[0].create_attempt_id == str(info.create_attempt_id)
    assert rows[0].display_name == "my-workspace"
    assert rows[0].provider_instance_name == "lima"


def test_live_create_attempt_prefers_the_record_display_name_and_color() -> None:
    info = _live_info(AgentCreateAttemptStatus.CLONING_REPO)
    record = _record(str(info.create_attempt_id), PendingCreateAttemptState.IN_FLIGHT)
    rows = derive_create_attempt_rows([info], [record], frozenset())
    assert rows[0].display_name == "My Workspace"
    assert rows[0].color == "#a1b2c3"
    assert rows[0].account_email == "owner@example.com"


def test_live_failed_create_attempt_derives_a_failed_row_with_its_error() -> None:
    info = _live_info(AgentCreateAttemptStatus.FAILED, error="clone exploded")
    rows = derive_create_attempt_rows([info], [], frozenset())
    assert rows[0].kind is CreateAttemptRowKind.FAILED
    assert rows[0].error == "clone exploded"
    assert rows[0].error_kind == "GITHUB_AUTH_REQUIRED"


def test_live_done_create_attempt_keeps_a_creating_row_until_discovery_confirms() -> None:
    agent_id = AgentId()
    info = _live_info(AgentCreateAttemptStatus.DONE, agent_id=agent_id)
    record = _record(str(info.create_attempt_id), PendingCreateAttemptState.DONE, agent_id=str(agent_id))

    # Discovery has not confirmed the workspace yet: the row stays (no flicker).
    rows_before = derive_create_attempt_rows([info], [record], frozenset())
    assert [row.kind for row in rows_before] == [CreateAttemptRowKind.CREATING]

    # Once the agent id appears in a snapshot, the real workspace row takes over.
    rows_after = derive_create_attempt_rows([info], [record], frozenset({str(agent_id)}))
    assert rows_after == []


def test_live_done_create_attempt_without_a_record_produces_no_row() -> None:
    info = _live_info(AgentCreateAttemptStatus.DONE, agent_id=AgentId())
    assert derive_create_attempt_rows([info], [], frozenset()) == []


def test_record_without_live_create_attempt_derives_interrupted_or_failed() -> None:
    interrupted = _record("create-attempt-" + "a" * 32, PendingCreateAttemptState.IN_FLIGHT)
    failed = _record("create-attempt-" + "b" * 32, PendingCreateAttemptState.FAILED, error="boom")
    rows = derive_create_attempt_rows([], [interrupted, failed], frozenset())
    kind_by_id = {row.create_attempt_id: row.kind for row in rows}
    assert kind_by_id[interrupted.create_attempt_id] is CreateAttemptRowKind.INTERRUPTED
    assert kind_by_id[failed.create_attempt_id] is CreateAttemptRowKind.FAILED
    failed_row = next(row for row in rows if row.create_attempt_id == failed.create_attempt_id)
    assert failed_row.error == "boom"


def test_done_record_without_live_create_attempt_produces_no_row() -> None:
    record = _record("create-attempt-" + "c" * 32, PendingCreateAttemptState.DONE, agent_id=str(AgentId()))
    assert derive_create_attempt_rows([], [record], frozenset()) == []


def test_record_whose_agent_is_discovered_produces_no_row() -> None:
    agent_id = AgentId()
    record = _record("create-attempt-" + "d" * 32, PendingCreateAttemptState.IN_FLIGHT, agent_id=str(agent_id))
    assert derive_create_attempt_rows([], [record], frozenset({str(agent_id)})) == []


def test_live_create_attempt_wins_over_its_own_record() -> None:
    info = _live_info(AgentCreateAttemptStatus.WAITING_FOR_READY)
    # The record still says IN_FLIGHT; the live view must not duplicate the row.
    record = _record(str(info.create_attempt_id), PendingCreateAttemptState.IN_FLIGHT)
    rows = derive_create_attempt_rows([info], [record], frozenset())
    assert len(rows) == 1
    assert rows[0].kind is CreateAttemptRowKind.CREATING


def test_record_only_rows_sort_oldest_first() -> None:
    now = datetime.now(timezone.utc)
    newer = _record("create-attempt-" + "e" * 32, PendingCreateAttemptState.IN_FLIGHT, created_at=now)
    older = _record(
        "create-attempt-" + "f" * 32, PendingCreateAttemptState.FAILED, created_at=now - timedelta(hours=2)
    )
    rows = derive_create_attempt_rows([], [newer, older], frozenset())
    assert [row.create_attempt_id for row in rows] == [older.create_attempt_id, newer.create_attempt_id]
