from pathlib import Path
from typing import cast

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.api.find import resolve_to_started_host_and_running_agent
from imbue.mngr.cli.agent_utils import find_agent_by_address_or_interactively
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance

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
def test_resolve_to_started_host_and_agent_succeeds_for_stopped_agent(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """resolve_to_started_host_and_agent does not check the agent's lifecycle."""
    agent_name = AgentName("stopped-resolve-test-agent")
    local_host, host_ref, agent_ref = _create_stopped_agent_with_ref(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47293")
    )

    found_agent, found_host = resolve_to_started_host_and_agent(
        host_ref=host_ref,
        agent_ref=agent_ref,
        allow_auto_start=False,
        mngr_ctx=temp_mngr_ctx,
    )
    assert found_agent.id == agent_ref.agent_id
    assert found_host.id == local_host.id


@pytest.mark.tmux
def test_resolve_to_started_host_and_running_agent_raises_for_stopped_agent_without_auto_start(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """resolve_to_started_host_and_running_agent raises when the agent is stopped and auto-start is disabled."""
    agent_name = AgentName("stopped-ensure-test-agent")
    _local_host, host_ref, agent_ref = _create_stopped_agent_with_ref(
        local_provider, temp_work_dir, agent_name, CommandString("sleep 47294")
    )

    with pytest.raises(UserInputError, match="stopped and automatic starting is disabled"):
        resolve_to_started_host_and_running_agent(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=False,
            mngr_ctx=temp_mngr_ctx,
        )
