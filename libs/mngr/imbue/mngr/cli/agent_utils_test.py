import pytest

from imbue.mngr.api.addresses import HostAddress
from imbue.mngr.cli.agent_utils import filter_agents_by_host
from imbue.mngr.cli.agent_utils import select_agent_interactively_with_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName


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
# select_agent_interactively_with_host tests
# =============================================================================


def test_select_agent_interactively_raises_when_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With a fresh context (no hosts or agents), raises UserInputError."""
    with pytest.raises(UserInputError, match="No agents found"):
        select_agent_interactively_with_host(temp_mngr_ctx)
