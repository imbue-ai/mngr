from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.api.agent_state import CombinedState
from imbue.mngr.api.agent_state import ResolvedTarget
from imbue.mngr.api.agent_state import get_agent_details
from imbue.mngr.api.agent_state import get_host_details
from imbue.mngr.api.agent_state import poll_combined_state
from imbue.mngr.api.agent_state import resolve_target
from imbue.mngr.api.testing import create_agent_data_json
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import MockProviderInstance

# === resolve_target ===


def test_resolve_target_finds_agent_by_name(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    agent_id = create_agent_data_json(local_provider.host_dir, "my-agent")
    result = resolve_target(AgentAddress(agent=AgentName("my-agent")), temp_mngr_ctx)
    assert result.is_agent_target is True
    assert result.agent_id == agent_id


def test_resolve_target_finds_host_by_id(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # An agent must exist so the host gets discovered.
    create_agent_data_json(local_provider.host_dir, "irrelevant-agent")
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    result = resolve_target(HostAddress(host=host.id), temp_mngr_ctx)
    assert result.is_agent_target is False
    assert result.host_id == host.id
    assert result.agent_id is None


def test_resolve_target_raises_when_agent_not_found(
    temp_mngr_ctx: MngrContext,
) -> None:
    with pytest.raises(UserInputError, match="Could not find agent"):
        resolve_target(AgentAddress(agent=AgentName("nonexistent-agent-92814")), temp_mngr_ctx)


def test_resolve_target_raises_when_host_not_found(
    temp_mngr_ctx: MngrContext,
) -> None:
    nonexistent_host_id = HostId.generate()
    with pytest.raises(UserInputError, match="Could not find host"):
        resolve_target(HostAddress(host=nonexistent_host_id), temp_mngr_ctx)


# === poll_combined_state ===


class _UnreachableHostProvider(MockProviderInstance):
    """Provider whose get_host always fails with a HostConnectionError.

    Exercises poll_combined_state's real fallback to to_offline_host without
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


def test_poll_combined_state_returns_running_for_reachable_local_host(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # The local host is always reachable and reports RUNNING.
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    resolved = ResolvedTarget(
        identifier=str(host.id),
        provider=local_provider,
        host_id=host.id,
        agent_id=None,
    )

    result = poll_combined_state(resolved)

    assert result.host_state == HostState.RUNNING
    assert result.agent_state is None


def test_poll_combined_state_falls_back_to_offline_stopped_on_connection_error(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    # When get_host raises HostConnectionError, poll_combined_state must fall back
    # to the offline host's derived state (STOPPED here).
    host_id = HostId.generate()
    provider = _make_unreachable_provider(temp_host_dir, temp_mngr_ctx, host_id, stop_reason="STOPPED")
    resolved = ResolvedTarget(
        identifier=str(host_id),
        provider=provider,
        host_id=host_id,
        agent_id=None,
    )

    result = poll_combined_state(resolved)

    assert result.host_state == HostState.STOPPED
    # No agent on this target, so agent_state stays None even on the fallback path.
    assert result.agent_state is None


def test_poll_combined_state_fallback_reports_agent_stopped_when_agent_target(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    # On the connection-error fallback, an agent target's agent_state is forced
    # to STOPPED (the agent cannot be running on an unreachable host).
    host_id = HostId.generate()
    provider = _make_unreachable_provider(temp_host_dir, temp_mngr_ctx, host_id, stop_reason=None)
    resolved = ResolvedTarget(
        identifier=str(host_id),
        provider=provider,
        host_id=host_id,
        agent_id=AgentId.generate(),
    )

    result = poll_combined_state(resolved)

    # stop_reason=None with shutdown support derives to CRASHED.
    assert result.host_state == HostState.CRASHED
    assert result.agent_state == AgentLifecycleState.STOPPED


def test_combined_state_defaults_to_unknown() -> None:
    state = CombinedState()
    assert state.host_state is None
    assert state.agent_state is None


# === get_agent_details ===


@pytest.mark.tmux
def test_get_agent_details_returns_full_details_for_agent(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("details-agent"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847400"),
        ),
    )
    local_host.start_agents([agent.id])
    try:
        details = get_agent_details(AgentAddress(agent=AgentName("details-agent")), temp_mngr_ctx)
    finally:
        local_host.destroy_agent(agent)

    assert details.name == AgentName("details-agent")
    assert details.command == "sleep 847400"
    # The embedded host details come from the same path list uses.
    assert details.host.name == LOCAL_HOST_NAME


def test_get_agent_details_raises_when_agent_not_found(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    with pytest.raises(UserInputError, match="Could not find agent"):
        get_agent_details(AgentAddress(agent=AgentName("nonexistent-agent-55501")), temp_mngr_ctx)


# === get_host_details ===


def test_get_host_details_returns_host_and_its_agent_refs(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    # Stub agents are enough: get_host_details returns lightweight discovery refs,
    # not full per-agent details, so no agent process (or tmux) is required.
    create_agent_data_json(local_provider.host_dir, "host-agent-a")
    create_agent_data_json(local_provider.host_dir, "host-agent-b")
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))

    host_details, agent_refs = get_host_details(HostAddress(host=host.id), temp_mngr_ctx)

    assert host_details.id == host.id
    assert host_details.state == HostState.RUNNING
    returned_names = {ref.agent_name for ref in agent_refs}
    assert {AgentName("host-agent-a"), AgentName("host-agent-b")} <= returned_names
