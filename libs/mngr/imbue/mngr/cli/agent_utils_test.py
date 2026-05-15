from pathlib import Path
from typing import cast

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.cli.agent_utils import ensure_host_and_agent_started
from imbue.mngr.cli.agent_utils import ensure_host_started_and_resolve_agent
from imbue.mngr.cli.agent_utils import filter_agents_by_host
from imbue.mngr.cli.agent_utils import find_agent_by_address_or_interactively
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


def _make_discovered_host(
    provider: str = "local",
    host_id: HostId | None = None,
    host_name: str = "test-host",
) -> DiscoveredHost:
    """Create a DiscoveredHost for testing."""
    if host_id is None:
        host_id = HostId.generate()
    return DiscoveredHost(
        provider_name=ProviderInstanceName(provider),
        host_id=host_id,
        host_name=HostName(host_name),
    )


def _make_discovered_agent(
    agent_id: AgentId,
    agent_name: str = "test-name",
    host_id: HostId | None = None,
    provider: str = "local",
) -> DiscoveredAgent:
    """Create a DiscoveredAgent for testing."""
    if host_id is None:
        host_id = HostId.generate()
    return DiscoveredAgent(
        agent_id=agent_id,
        agent_name=AgentName(agent_name),
        host_id=host_id,
        provider_name=ProviderInstanceName(provider),
    )


# =============================================================================
# filter_agents_by_host tests
# =============================================================================


def test_filter_agents_by_host_filters_by_name() -> None:
    """Test that filter_agents_by_host keeps only matching hosts."""
    host_ref1 = _make_discovered_host(host_name="host-1")
    host_ref2 = _make_discovered_host(host_name="host-2")
    agent_ref = _make_discovered_agent(agent_id=AgentId.generate())
    agents_by_host = {host_ref1: [agent_ref], host_ref2: []}

    filtered = filter_agents_by_host(agents_by_host, HostAddress(host=HostName("host-1")))

    assert len(filtered) == 1
    assert host_ref1 in filtered


def test_filter_agents_by_host_raises_when_no_match() -> None:
    """Test that filter_agents_by_host raises UserInputError when no hosts match."""
    host_ref = _make_discovered_host(host_name="host-1")
    agents_by_host = {host_ref: []}

    with pytest.raises(UserInputError, match="No host found matching"):
        filter_agents_by_host(agents_by_host, HostAddress(host=HostName("nonexistent-host")))


# =============================================================================
# find_agent_by_address_or_interactively tests
# =============================================================================


def test_find_agent_by_address_or_interactively_raises_when_no_agents_in_interactive_mode(
    temp_mngr_ctx: MngrContext,
) -> None:
    """In interactive mode with no agents, raises UserInputError before showing the selector."""
    interactive_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().is_interactive, True))
    with pytest.raises(UserInputError, match="No agents found"):
        find_agent_by_address_or_interactively(
            mngr_ctx=interactive_ctx,
            address=None,
            host_filter=None,
        )


def test_find_agent_by_address_or_interactively_raises_when_no_address_in_non_interactive_mode(
    temp_mngr_ctx: MngrContext,
) -> None:
    """In non-interactive mode without an address, raises UserInputError rather than showing a selector."""
    with pytest.raises(UserInputError, match="not running in interactive mode"):
        find_agent_by_address_or_interactively(
            mngr_ctx=temp_mngr_ctx,
            address=None,
            host_filter=None,
        )


# =============================================================================
# ensure_*_started helpers
# =============================================================================


def _create_stopped_agent_with_ref(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    agent_name: AgentName,
    command: CommandString,
) -> tuple[OnlineHostInterface, DiscoveredHost, DiscoveredAgent]:
    """Create an agent, stop it, and return the host plus discovered refs."""
    local_host = cast(OnlineHostInterface, local_provider.get_host(HostName(LOCAL_HOST_NAME)))

    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            agent_type=AgentTypeName("generic"),
            name=agent_name,
            command=command,
        ),
    )

    # Stop the agent so it's in STOPPED state
    local_host.stop_agents([agent.id])

    host_ref = DiscoveredHost(
        provider_name=ProviderInstanceName("local"),
        host_id=local_host.id,
        host_name=local_host.get_name(),
    )
    agent_ref = DiscoveredAgent(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=local_host.id,
        provider_name=ProviderInstanceName("local"),
    )
    return local_host, host_ref, agent_ref


@pytest.mark.tmux
def test_ensure_host_started_and_resolve_agent_succeeds_for_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """ensure_host_started_and_resolve_agent does not check the agent's lifecycle."""
    agent_name = AgentName("stopped-resolve-test-agent")
    local_host, host_ref, agent_ref = _create_stopped_agent_with_ref(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47293")
    )

    found_agent, found_host = ensure_host_started_and_resolve_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=False,
        mngr_ctx=temp_mngr_ctx,
    )
    assert found_agent.id == agent_ref.agent_id
    assert found_host.id == local_host.id


@pytest.mark.tmux
def test_ensure_host_and_agent_started_raises_for_stopped_agent_without_auto_start(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """ensure_host_and_agent_started raises when the agent is stopped and auto-start is disabled."""
    agent_name = AgentName("stopped-ensure-test-agent")
    _local_host, host_ref, agent_ref = _create_stopped_agent_with_ref(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47294")
    )

    with pytest.raises(UserInputError, match="stopped and automatic starting is disabled"):
        ensure_host_and_agent_started(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=False,
            mngr_ctx=temp_mngr_ctx,
        )
