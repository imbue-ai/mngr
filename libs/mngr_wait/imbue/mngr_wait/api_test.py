import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr_wait.api import ResolvedTarget
from imbue.mngr_wait.api import _detect_state_changes
from imbue.mngr_wait.api import poll_target_state
from imbue.mngr_wait.api import resolve_wait_target
from imbue.mngr_wait.api import wait_for_state
from imbue.mngr_wait.data_types import CombinedState
from imbue.mngr_wait.data_types import StateChange
from imbue.mngr_wait.data_types import WaitTarget
from imbue.mngr_wait.primitives import WaitTargetType


def _create_agent_data_json(per_host_dir: Path, agent_name: str) -> AgentId:
    """Create an agent data.json file so the agent appears in discovery."""
    agent_id = AgentId.generate()
    agent_dir = per_host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": agent_name,
        "type": "generic",
        "command": "sleep 1",
        "work_dir": "/tmp/test",
        "create_time": "2026-01-01T00:00:00+00:00",
    }
    (agent_dir / "data.json").write_text(json.dumps(data))
    return agent_id


# === resolve_wait_target ===


def test_resolve_wait_target_finds_agent_by_name(
    temp_mngr_ctx: MngrContext,
    local_provider,
) -> None:
    agent_id = _create_agent_data_json(local_provider.host_dir, "my-agent")
    result = resolve_wait_target(AgentAddress(agent=AgentName("my-agent")), temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.AGENT
    assert result.agent_id == agent_id


def test_resolve_wait_target_finds_host_by_id(
    temp_mngr_ctx: MngrContext,
    local_provider,
) -> None:
    # An agent must exist so the host gets discovered.
    _create_agent_data_json(local_provider.host_dir, "irrelevant-agent")
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    result = resolve_wait_target(HostAddress(host=host.id), temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.HOST
    assert result.host_id == host.id
    assert result.agent_id is None


def test_resolve_wait_target_raises_when_agent_not_found(
    temp_mngr_ctx: MngrContext,
) -> None:
    with pytest.raises(UserInputError, match="Could not find agent"):
        resolve_wait_target(AgentAddress(agent=AgentName("nonexistent-agent-92814")), temp_mngr_ctx)


def test_resolve_wait_target_raises_when_host_not_found(
    temp_mngr_ctx: MngrContext,
) -> None:
    nonexistent_host_id = HostId.generate()
    with pytest.raises(UserInputError, match="Could not find host"):
        resolve_wait_target(HostAddress(host=nonexistent_host_id), temp_mngr_ctx)


# === poll_target_state (offline host) ===


def _make_offline_resolved_target(
    host_id: HostId,
    stop_reason: str | None,
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> ResolvedTarget:
    """Build a ResolvedTarget whose provider returns an offline (non-online) host.

    The offline host derives its state from ``stop_reason`` (the mock provider
    supports controlled shutdown), letting us drive a deterministic host state.
    """
    now = datetime.now(timezone.utc)
    provider = MockProviderInstance(
        name=ProviderInstanceName("test"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    offline_host = OfflineHost(
        id=host_id,
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="test-host",
            idle_timeout_seconds=3600,
            activity_sources=(ActivitySource.SSH,),
            image="test-image:latest",
            created_at=now,
            updated_at=now,
            stop_reason=stop_reason,
        ),
        provider_instance=provider,
        mngr_ctx=temp_mngr_ctx,
    )
    provider.mock_hosts = [offline_host]
    return ResolvedTarget(
        target=WaitTarget(identifier=str(host_id), target_type=WaitTargetType.AGENT),
        provider=provider,
        host_id=host_id,
        agent_id=AgentId.generate(),
    )


def test_poll_target_state_offline_host_derives_stopped_agent(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A down offline host yields a STOPPED agent (process provably not running)."""
    host_id = HostId.generate()
    resolved = _make_offline_resolved_target(host_id, "STOPPED", temp_host_dir, temp_mngr_ctx)
    state = poll_target_state(resolved)
    assert state.host_state == HostState.STOPPED
    assert state.agent_state == AgentLifecycleState.STOPPED


def test_poll_target_state_offline_host_derives_unknown_agent(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """When the offline host's state is indeterminate, the agent state is UNKNOWN.

    This proves the agent state is now derived from the host state rather than
    hardcoded to STOPPED.
    """
    host_id = HostId.generate()
    resolved = _make_offline_resolved_target(host_id, "RUNNING", temp_host_dir, temp_mngr_ctx)
    state = poll_target_state(resolved)
    assert state.host_state == HostState.RUNNING
    assert state.agent_state == AgentLifecycleState.UNKNOWN


# === _detect_state_changes ===


def test_detect_state_changes_records_host_state_change() -> None:
    previous = CombinedState(host_state=HostState.RUNNING)
    current = CombinedState(host_state=HostState.STOPPED)
    changes: list[StateChange] = []
    recorded: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=5.0,
        state_changes=changes,
        on_state_change=recorded.append,
    )

    assert len(changes) == 1
    assert changes[0].field == "host_state"
    assert changes[0].old_value == "RUNNING"
    assert changes[0].new_value == "STOPPED"
    assert len(recorded) == 1


def test_detect_state_changes_records_agent_state_change() -> None:
    previous = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    current = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.WAITING,
    )
    changes: list[StateChange] = []
    recorded: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=10.0,
        state_changes=changes,
        on_state_change=recorded.append,
    )

    assert len(changes) == 1
    assert changes[0].field == "agent_state"
    assert changes[0].old_value == "RUNNING"
    assert changes[0].new_value == "WAITING"
    assert len(recorded) == 1


def test_detect_state_changes_no_change_records_nothing() -> None:
    combined_state = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_state=combined_state,
        current_state=combined_state,
        elapsed=5.0,
        state_changes=changes,
        on_state_change=None,
    )

    assert len(changes) == 0


def test_detect_state_changes_both_change() -> None:
    previous = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    current = CombinedState(
        host_state=HostState.STOPPED,
        agent_state=AgentLifecycleState.STOPPED,
    )
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=15.0,
        state_changes=changes,
        on_state_change=None,
    )

    assert len(changes) == 2
    assert changes[0].field == "host_state"
    assert changes[1].field == "agent_state"


def test_detect_state_changes_skips_none_previous() -> None:
    previous = CombinedState()
    current = CombinedState(host_state=HostState.RUNNING)
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_state=previous,
        current_state=current,
        elapsed=1.0,
        state_changes=changes,
        on_state_change=None,
    )

    # No change recorded because previous was None
    assert len(changes) == 0


# === wait_for_state ===


def _make_wait_target(target_type: WaitTargetType = WaitTargetType.HOST) -> WaitTarget:
    return WaitTarget(identifier="test-target", target_type=target_type)


def test_wait_for_state_returns_immediately_when_already_matched() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    combined_state = CombinedState(host_state=HostState.STOPPED)

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.is_timed_out is False
    assert result.matched_state == "STOPPED"
    assert result.elapsed_seconds < 1.0


def test_wait_for_state_times_out_when_state_never_matches() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    combined_state = CombinedState(host_state=HostState.RUNNING)

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=0.1,
        interval_seconds=0.05,
        on_state_change=None,
    )

    assert result.is_matched is False
    assert result.is_timed_out is True
    assert result.matched_state is None


def test_wait_for_state_detects_state_transition() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_with_transition() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            return CombinedState(host_state=HostState.STOPPED)
        return CombinedState(host_state=HostState.RUNNING)

    result = wait_for_state(
        target=target,
        poll_fn=_poll_with_transition,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "STOPPED"
    # Exactly 1 state change: RUNNING -> STOPPED
    assert len(result.state_changes) == 1


def test_wait_for_state_records_state_changes_through_multiple_transitions() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_through_states() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return CombinedState(host_state=HostState.RUNNING)
        elif call_count == 2:
            return CombinedState(host_state=HostState.STOPPING)
        else:
            return CombinedState(host_state=HostState.STOPPED)

    recorded_changes: list[StateChange] = []

    result = wait_for_state(
        target=target,
        poll_fn=_poll_through_states,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=recorded_changes.append,
    )

    assert result.is_matched is True
    # Exactly 2 changes: RUNNING -> STOPPING, STOPPING -> STOPPED
    assert len(result.state_changes) == 2
    assert len(recorded_changes) == 2


def test_wait_for_state_handles_poll_errors_gracefully() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_with_error() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("transient error")
        return CombinedState(host_state=HostState.STOPPED)

    result = wait_for_state(
        target=target,
        poll_fn=_poll_with_error,
        target_states=frozenset({"STOPPED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "STOPPED"


def test_wait_for_state_agent_target_matches_agent_state() -> None:
    target = _make_wait_target(WaitTargetType.AGENT)
    combined_state = CombinedState(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.WAITING,
    )

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"WAITING"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "WAITING"


def test_wait_for_state_agent_target_matches_host_crashed() -> None:
    target = _make_wait_target(WaitTargetType.AGENT)
    combined_state = CombinedState(
        host_state=HostState.CRASHED,
        agent_state=AgentLifecycleState.RUNNING,
    )

    result = wait_for_state(
        target=target,
        poll_fn=lambda: combined_state,
        target_states=frozenset({"CRASHED"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "CRASHED"


def test_wait_for_state_detects_destroyed_after_connection_errors() -> None:
    """Simulate what happens when a host is destroyed: polls fail, then return DESTROYED."""
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_with_destruction() -> CombinedState:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            # First two polls: host is running
            return CombinedState(host_state=HostState.RUNNING)
        else:
            # After destruction: offline host reports DESTROYED
            return CombinedState(host_state=HostState.DESTROYED)

    result = wait_for_state(
        target=target,
        poll_fn=_poll_with_destruction,
        target_states=frozenset({"DESTROYED", "STOPPED", "CRASHED"}),
        timeout_seconds=5.0,
        interval_seconds=0.01,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "DESTROYED"
    assert len(result.state_changes) == 1
    assert result.state_changes[0].old_value == "RUNNING"
    assert result.state_changes[0].new_value == "DESTROYED"
