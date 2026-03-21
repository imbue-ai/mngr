import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import DiscoveredAgent
from imbue.mng.primitives import DiscoveredHost
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_wait.api import _detect_state_changes
from imbue.mng_wait.api import _is_agent_match
from imbue.mng_wait.api import _is_host_match
from imbue.mng_wait.api import _resolve_agent_target
from imbue.mng_wait.api import _resolve_by_name
from imbue.mng_wait.api import _resolve_host_target
from imbue.mng_wait.api import wait_for_state
from imbue.mng_wait.data_types import StateChange
from imbue.mng_wait.data_types import StateSnapshot
from imbue.mng_wait.data_types import WaitTarget
from imbue.mng_wait.primitives import WaitTargetType

# === _is_agent_match ===


def test_is_agent_match_by_valid_id() -> None:
    agent_id = AgentId.generate()
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=agent_id,
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    assert _is_agent_match(agent, str(agent_id), is_agent_id=True) is True


def test_is_agent_match_by_name() -> None:
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("my-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    assert _is_agent_match(agent, "my-agent", is_agent_id=False) is True


def test_is_agent_match_wrong_name() -> None:
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("other-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    assert _is_agent_match(agent, "my-agent", is_agent_id=False) is False


def test_is_agent_match_invalid_id_format() -> None:
    agent = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    # An invalid ID format should return False, not raise
    assert _is_agent_match(agent, "not-a-valid-id", is_agent_id=True) is False


# === _is_host_match ===


def test_is_host_match_by_valid_id() -> None:
    host_id = HostId.generate()
    host = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )
    assert _is_host_match(host, str(host_id), is_host_id=True) is True


def test_is_host_match_by_name() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("my-host"),
        provider_name=ProviderInstanceName("local"),
    )
    assert _is_host_match(host, "my-host", is_host_id=False) is True


def test_is_host_match_wrong_name() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("other-host"),
        provider_name=ProviderInstanceName("local"),
    )
    assert _is_host_match(host, "my-host", is_host_id=False) is False


# === _detect_state_changes ===


def test_detect_state_changes_records_host_state_change() -> None:
    previous = StateSnapshot(host_state=HostState.RUNNING)
    current = StateSnapshot(host_state=HostState.STOPPED)
    changes: list[StateChange] = []
    recorded: list[StateChange] = []

    _detect_state_changes(
        previous_snapshot=previous,
        current_snapshot=current,
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
    previous = StateSnapshot(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    current = StateSnapshot(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.WAITING,
    )
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_snapshot=previous,
        current_snapshot=current,
        elapsed=10.0,
        state_changes=changes,
        on_state_change=None,
    )

    assert len(changes) == 1
    assert changes[0].field == "agent_state"
    assert changes[0].old_value == "RUNNING"
    assert changes[0].new_value == "WAITING"


def test_detect_state_changes_no_change_records_nothing() -> None:
    snapshot = StateSnapshot(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_snapshot=snapshot,
        current_snapshot=snapshot,
        elapsed=5.0,
        state_changes=changes,
        on_state_change=None,
    )

    assert len(changes) == 0


def test_detect_state_changes_both_change() -> None:
    previous = StateSnapshot(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.RUNNING,
    )
    current = StateSnapshot(
        host_state=HostState.STOPPED,
        agent_state=AgentLifecycleState.STOPPED,
    )
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_snapshot=previous,
        current_snapshot=current,
        elapsed=15.0,
        state_changes=changes,
        on_state_change=None,
    )

    assert len(changes) == 2
    assert changes[0].field == "host_state"
    assert changes[1].field == "agent_state"


def test_detect_state_changes_skips_none_previous() -> None:
    previous = StateSnapshot()
    current = StateSnapshot(host_state=HostState.RUNNING)
    changes: list[StateChange] = []

    _detect_state_changes(
        previous_snapshot=previous,
        current_snapshot=current,
        elapsed=1.0,
        state_changes=changes,
        on_state_change=None,
    )

    # No change recorded because previous was None
    assert len(changes) == 0


# === _resolve_by_name ===


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


def test_resolve_by_name_finds_agent(temp_mng_ctx: MngContext) -> None:
    agents_by_host, host_ref, agent_ref = _make_agents_by_host(agent_name="my-agent")
    result = _resolve_by_name("my-agent", agents_by_host, temp_mng_ctx)
    assert result.target.target_type == WaitTargetType.AGENT
    assert result.agent_id == agent_ref.agent_id
    assert result.host_id == host_ref.host_id


def test_resolve_by_name_finds_host(temp_mng_ctx: MngContext) -> None:
    agents_by_host, host_ref, _agent_ref = _make_agents_by_host(
        agent_name="my-agent",
        host_name="my-host",
    )
    result = _resolve_by_name("my-host", agents_by_host, temp_mng_ctx)
    assert result.target.target_type == WaitTargetType.HOST
    assert result.agent_id is None
    assert result.host_id == host_ref.host_id


def test_resolve_by_name_raises_when_ambiguous(temp_mng_ctx: MngContext) -> None:
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

    with pytest.raises(UserInputError, match="matches both"):
        _resolve_by_name(shared_name, agents_by_host, temp_mng_ctx)


def test_resolve_by_name_raises_when_not_found(temp_mng_ctx: MngContext) -> None:
    agents_by_host, _host_ref, _agent_ref = _make_agents_by_host()

    with pytest.raises(UserInputError, match="No agent or host found"):
        _resolve_by_name("nonexistent", agents_by_host, temp_mng_ctx)


def test_resolve_by_name_raises_when_multiple_agents(temp_mng_ctx: MngContext) -> None:
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

    with pytest.raises(UserInputError, match="Multiple agents"):
        _resolve_by_name("dup-agent", agents_by_host, temp_mng_ctx)


def test_resolve_by_name_raises_when_multiple_hosts(temp_mng_ctx: MngContext) -> None:
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

    with pytest.raises(UserInputError, match="Multiple hosts"):
        _resolve_by_name("dup-host", agents_by_host, temp_mng_ctx)


# === _resolve_agent_target ===


def test_resolve_agent_target_finds_agent_by_id(temp_mng_ctx: MngContext) -> None:
    agents_by_host, host_ref, agent_ref = _make_agents_by_host(agent_name="my-agent")
    agent_id_str = str(agent_ref.agent_id)
    result = _resolve_agent_target(agent_id_str, agents_by_host, temp_mng_ctx)
    assert result.target.target_type == WaitTargetType.AGENT
    assert result.agent_id == agent_ref.agent_id


def test_resolve_agent_target_raises_when_not_found(temp_mng_ctx: MngContext) -> None:
    agents_by_host, _host_ref, _agent_ref = _make_agents_by_host()
    # Use a valid AgentId format that doesn't match any existing agent
    nonexistent_agent_id = str(AgentId.generate())
    with pytest.raises(Exception, match="not found"):
        _resolve_agent_target(nonexistent_agent_id, agents_by_host, temp_mng_ctx)


# === _resolve_host_target ===


def test_resolve_host_target_finds_host_by_id(temp_mng_ctx: MngContext) -> None:
    agents_by_host, host_ref, _agent_ref = _make_agents_by_host()
    host_id_str = str(host_ref.host_id)
    result = _resolve_host_target(host_id_str, agents_by_host, temp_mng_ctx)
    assert result.target.target_type == WaitTargetType.HOST
    assert result.host_id == host_ref.host_id


def test_resolve_host_target_raises_when_not_found(temp_mng_ctx: MngContext) -> None:
    agents_by_host, _host_ref, _agent_ref = _make_agents_by_host()
    # Use a valid HostId format that doesn't match any existing host
    nonexistent_host_id = str(HostId.generate())
    with pytest.raises(Exception, match="not found"):
        _resolve_host_target(nonexistent_host_id, agents_by_host, temp_mng_ctx)


# === wait_for_state ===


def _make_wait_target(target_type: WaitTargetType = WaitTargetType.HOST) -> WaitTarget:
    return WaitTarget(identifier="test-target", target_type=target_type)


def test_wait_for_state_returns_immediately_when_already_matched() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    snapshot = StateSnapshot(host_state=HostState.STOPPED)

    result = wait_for_state(
        target=target,
        poll_fn=lambda: snapshot,
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
    snapshot = StateSnapshot(host_state=HostState.RUNNING)

    result = wait_for_state(
        target=target,
        poll_fn=lambda: snapshot,
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

    def _poll_with_transition() -> StateSnapshot:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            return StateSnapshot(host_state=HostState.STOPPED)
        return StateSnapshot(host_state=HostState.RUNNING)

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
    assert len(result.state_changes) >= 1


def test_wait_for_state_records_state_changes() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_through_states() -> StateSnapshot:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return StateSnapshot(host_state=HostState.RUNNING)
        elif call_count == 2:
            return StateSnapshot(host_state=HostState.STOPPING)
        else:
            return StateSnapshot(host_state=HostState.STOPPED)

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
    assert len(result.state_changes) >= 1
    assert len(recorded_changes) >= 1


def test_wait_for_state_handles_poll_errors_gracefully() -> None:
    target = _make_wait_target(WaitTargetType.HOST)
    call_count = 0

    def _poll_with_error() -> StateSnapshot:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("transient error")
        return StateSnapshot(host_state=HostState.STOPPED)

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
    snapshot = StateSnapshot(
        host_state=HostState.RUNNING,
        agent_state=AgentLifecycleState.WAITING,
    )

    result = wait_for_state(
        target=target,
        poll_fn=lambda: snapshot,
        target_states=frozenset({"WAITING"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "WAITING"


def test_wait_for_state_agent_target_matches_host_crashed() -> None:
    target = _make_wait_target(WaitTargetType.AGENT)
    snapshot = StateSnapshot(
        host_state=HostState.CRASHED,
        agent_state=AgentLifecycleState.RUNNING,
    )

    result = wait_for_state(
        target=target,
        poll_fn=lambda: snapshot,
        target_states=frozenset({"CRASHED"}),
        timeout_seconds=5.0,
        interval_seconds=1.0,
        on_state_change=None,
    )

    assert result.is_matched is True
    assert result.matched_state == "CRASHED"
