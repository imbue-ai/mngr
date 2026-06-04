from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import HostInterface
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
from imbue.mngr.providers.local.instance import LocalProviderInstance
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
from imbue.mngr_wait.testing import create_agent_data_json

# === resolve_wait_target ===


def test_resolve_wait_target_finds_agent_by_name(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    agent_id = create_agent_data_json(local_provider.host_dir, "my-agent")
    result = resolve_wait_target(AgentAddress(agent=AgentName("my-agent")), temp_mngr_ctx)
    assert result.target.target_type == WaitTargetType.AGENT
    assert result.agent_id == agent_id


def test_resolve_wait_target_finds_host_by_id(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # An agent must exist so the host gets discovered.
    create_agent_data_json(local_provider.host_dir, "irrelevant-agent")
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


def test_wait_for_state_records_running_to_destroyed_transition_from_state_sequence() -> None:
    """wait_for_state records a RUNNING -> DESTROYED change given that state sequence.

    This exercises wait_for_state's change-recording and matching against a
    hand-fed sequence; it does NOT exercise poll_target_state's real
    HostConnectionError fallback (that is covered by the poll_target_state
    tests below).
    """
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


# === poll_target_state ===


class _UnreachableHostProvider(MockProviderInstance):
    """Provider whose get_host always fails with a HostConnectionError.

    Exercises poll_target_state's real fallback to to_offline_host without
    requiring network access. to_offline_host returns the configured offline
    host so the offline state derivation runs for real.
    """

    offline_host_to_return: OfflineHost

    def get_host(self, host: HostId | HostName) -> HostInterface:
        raise HostConnectionError(f"cannot reach host {host}")

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        return self.offline_host_to_return


def _make_offline_host(
    provider: MockProviderInstance,
    mngr_ctx: MngrContext,
    host_id: HostId,
    stop_reason: str | None,
) -> OfflineHost:
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="offline-host",
        stop_reason=stop_reason,
        created_at=now,
        updated_at=now,
    )
    return OfflineHost(
        id=host_id,
        certified_host_data=certified_data,
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
    )


def _make_unreachable_provider(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
    host_id: HostId,
    stop_reason: str | None,
) -> _UnreachableHostProvider:
    # MockProviderInstance defaults mock_supports_shutdown_hosts=True, so the
    # offline state is derived directly from stop_reason (STOPPED -> STOPPED,
    # None -> CRASHED).
    provider = _UnreachableHostProvider(
        name=ProviderInstanceName("unreachable-" + host_id),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        offline_host_to_return=_make_offline_host(
            # The offline host's provider must support to_offline_host's
            # snapshot/shutdown lookups; reuse a plain MockProviderInstance.
            MockProviderInstance(
                name=ProviderInstanceName("offline-backing-" + host_id),
                host_dir=temp_host_dir,
                mngr_ctx=temp_mngr_ctx,
            ),
            temp_mngr_ctx,
            host_id,
            stop_reason,
        ),
    )
    return provider


def test_poll_target_state_returns_running_for_reachable_local_host(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # The local host is always reachable and reports RUNNING.
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    resolved = ResolvedTarget(
        target=WaitTarget(identifier=str(host.id), target_type=WaitTargetType.HOST),
        provider=local_provider,
        host_id=host.id,
        agent_id=None,
    )

    result = poll_target_state(resolved)

    assert result.host_state == HostState.RUNNING
    assert result.agent_state is None


def test_poll_target_state_falls_back_to_offline_stopped_on_connection_error(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    # When get_host raises HostConnectionError, poll_target_state must fall back
    # to the offline host's derived state (STOPPED here).
    host_id = HostId.generate()
    provider = _make_unreachable_provider(temp_host_dir, temp_mngr_ctx, host_id, stop_reason="STOPPED")
    resolved = ResolvedTarget(
        target=WaitTarget(identifier=str(host_id), target_type=WaitTargetType.HOST),
        provider=provider,
        host_id=host_id,
        agent_id=None,
    )

    result = poll_target_state(resolved)

    assert result.host_state == HostState.STOPPED
    # No agent on this target, so agent_state stays None even on the fallback path.
    assert result.agent_state is None


def test_poll_target_state_fallback_reports_agent_stopped_when_agent_target(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    # On the connection-error fallback, an agent target's agent_state is forced
    # to STOPPED (the agent cannot be running on an unreachable host).
    host_id = HostId.generate()
    provider = _make_unreachable_provider(temp_host_dir, temp_mngr_ctx, host_id, stop_reason=None)
    resolved = ResolvedTarget(
        target=WaitTarget(identifier=str(host_id), target_type=WaitTargetType.AGENT),
        provider=provider,
        host_id=host_id,
        agent_id=AgentId.generate(),
    )

    result = poll_target_state(resolved)

    # stop_reason=None with shutdown support derives to CRASHED.
    assert result.host_state == HostState.CRASHED
    assert result.agent_state == AgentLifecycleState.STOPPED
