import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_wait.api import _build_agent_resolved_target
from imbue.mngr_wait.api import _build_host_resolved_target
from imbue.mngr_wait.api import _detect_state_changes
from imbue.mngr_wait.api import _resolve_by_name
from imbue.mngr_wait.api import wait_for_state
from imbue.mngr_wait.data_types import CombinedState
from imbue.mngr_wait.data_types import StateChange
from imbue.mngr_wait.data_types import WaitTarget
from imbue.mngr_wait.primitives import WaitTargetType


def _make_agents_by_host(
    agent_name: str = "test-agent",
    host_name: str = "test-host",
) -> tuple[dict[DiscoveredHost, list[DiscoveredAgent]], DiscoveredHost, DiscoveredAgent]:
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName(host_name),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(agent_name),
        provider_name=ProviderInstanceName("local"),
    )
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {host_ref: [agent_ref]}
    return agents_by_host, host_ref, agent_ref


# === _build_agent_resolved_target ===


def test_build_agent_resolved_target_finds_agent_by_id(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, host_ref, agent_ref = _make_agents_by_host(agent_name="my-agent")
    agent_id_str = str(agent_ref.agent_id)
    result = _build_agent_resolved_target(agent_id_str, agents_by_host, temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.AGENT
    assert result.agent_id == agent_ref.agent_id


def test_build_agent_resolved_target_finds_agent_by_name(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, host_ref, agent_ref = _make_agents_by_host(agent_name="named-agent")
    result = _build_agent_resolved_target("named-agent", agents_by_host, temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.AGENT
    assert result.agent_id == agent_ref.agent_id


def test_build_agent_resolved_target_raises_when_not_found(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, _host_ref, _agent_ref = _make_agents_by_host()
    nonexistent_agent_id = str(AgentId.generate())
    with pytest.raises(UserInputError):
        _build_agent_resolved_target(nonexistent_agent_id, agents_by_host, temp_mngr_ctx)


# === _build_host_resolved_target ===


def test_build_host_resolved_target_finds_host_by_id(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, host_ref, _agent_ref = _make_agents_by_host()
    all_hosts = list(agents_by_host.keys())
    host_id_str = str(host_ref.host_id)
    result = _build_host_resolved_target(host_id_str, all_hosts, temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.HOST
    assert result.host_id == host_ref.host_id


def test_build_host_resolved_target_finds_host_by_name(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, host_ref, _agent_ref = _make_agents_by_host(host_name="named-host")
    all_hosts = list(agents_by_host.keys())
    result = _build_host_resolved_target("named-host", all_hosts, temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.HOST
    assert result.host_id == host_ref.host_id


def test_build_host_resolved_target_raises_when_not_found(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, _host_ref, _agent_ref = _make_agents_by_host()
    all_hosts = list(agents_by_host.keys())
    nonexistent_host_id = str(HostId.generate())
    with pytest.raises(UserInputError):
        _build_host_resolved_target(nonexistent_host_id, all_hosts, temp_mngr_ctx)


def test_build_host_resolved_target_handles_invalid_id_format(temp_mngr_ctx: MngrContext) -> None:
    """Ensure a host-prefixed name with invalid UUID format raises UserInputError, not InvalidRandomIdError."""
    agents_by_host, _host_ref, _agent_ref = _make_agents_by_host()
    all_hosts = list(agents_by_host.keys())
    with pytest.raises(UserInputError):
        _build_host_resolved_target("host-myserver", all_hosts, temp_mngr_ctx)


# === _resolve_by_name ===


def test_resolve_by_name_finds_agent(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, host_ref, agent_ref = _make_agents_by_host(agent_name="my-agent")
    all_hosts = list(agents_by_host.keys())
    result = _resolve_by_name("my-agent", agents_by_host, all_hosts, temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.AGENT
    assert result.agent_id == agent_ref.agent_id
    assert result.host_id == host_ref.host_id


def test_resolve_by_name_finds_host(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, host_ref, _agent_ref = _make_agents_by_host(
        agent_name="my-agent",
        host_name="my-host",
    )
    all_hosts = list(agents_by_host.keys())
    result = _resolve_by_name("my-host", agents_by_host, all_hosts, temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.HOST
    assert result.agent_id is None
    assert result.host_id == host_ref.host_id


def test_resolve_by_name_raises_when_ambiguous(temp_mngr_ctx: MngrContext) -> None:
    host_id = HostId.generate()
    shared_name = "ambiguous"
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName(shared_name),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName(shared_name),
        provider_name=ProviderInstanceName("local"),
    )
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {host_ref: [agent_ref]}
    all_hosts = list(agents_by_host.keys())

    with pytest.raises(UserInputError, match="matches both"):
        _resolve_by_name(shared_name, agents_by_host, all_hosts, temp_mngr_ctx)


def test_resolve_by_name_raises_when_not_found(temp_mngr_ctx: MngrContext) -> None:
    agents_by_host, _host_ref, _agent_ref = _make_agents_by_host()
    all_hosts = list(agents_by_host.keys())

    with pytest.raises(UserInputError, match="No agent or host found"):
        _resolve_by_name("nonexistent", agents_by_host, all_hosts, temp_mngr_ctx)


def test_resolve_by_name_raises_when_multiple_agents(temp_mngr_ctx: MngrContext) -> None:
    host_ref_1 = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("host-1"),
        provider_name=ProviderInstanceName("local"),
    )
    host_ref_2 = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("host-2"),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref_1 = DiscoveredAgent(
        host_id=host_ref_1.host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("dup-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref_2 = DiscoveredAgent(
        host_id=host_ref_2.host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("dup-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {
        host_ref_1: [agent_ref_1],
        host_ref_2: [agent_ref_2],
    }
    all_hosts = list(agents_by_host.keys())

    with pytest.raises(UserInputError, match="Multiple"):
        _resolve_by_name("dup-agent", agents_by_host, all_hosts, temp_mngr_ctx)


def test_resolve_by_name_raises_when_multiple_hosts(temp_mngr_ctx: MngrContext) -> None:
    host_ref_1 = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("dup-host"),
        provider_name=ProviderInstanceName("local"),
    )
    host_ref_2 = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("dup-host"),
        provider_name=ProviderInstanceName("local"),
    )
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {
        host_ref_1: [],
        host_ref_2: [],
    }
    all_hosts = list(agents_by_host.keys())

    with pytest.raises(UserInputError, match="Multiple"):
        _resolve_by_name("dup-host", agents_by_host, all_hosts, temp_mngr_ctx)


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
