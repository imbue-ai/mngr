"""Tests for ProviderInstanceInterface default method implementations."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import _discover_agents_on_host
from imbue.mngr.interfaces.provider_instance import build_agent_details_from_offline_ref
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.mock_provider_test import MockProviderInstance


def _make_certified_data(host_id: HostId) -> CertifiedHostData:
    now = datetime.now(timezone.utc)
    return CertifiedHostData(
        host_id=str(host_id),
        host_name="test-host",
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.SSH,),
        image="test-image:latest",
        created_at=now,
        updated_at=now,
    )


def _make_agent_ref(host_id: HostId, agent_id: AgentId, provider_name: ProviderInstanceName) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("test-agent"),
        provider_name=provider_name,
        certified_data={
            "command": "sleep 999",
            "work_dir": "/tmp/test",
            "type": "generic",
        },
    )


def _make_offline_host(host_id: HostId, provider: MockProviderInstance, mngr_ctx: MngrContext) -> OfflineHost:
    return OfflineHost(
        id=host_id,
        certified_host_data=_make_certified_data(host_id),
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
    )


def _make_mock_online_host(host_id: HostId) -> MagicMock:
    """Create a MagicMock that passes isinstance(host, OnlineHostInterface) checks.

    Sets up the minimum return values needed by _build_host_details_from_host.
    """
    host = MagicMock(spec=OnlineHostInterface)
    host.id = host_id
    host.get_name.return_value = "test-host"
    host.get_state.return_value = HostState.RUNNING
    host.get_ssh_connection_info.return_value = None
    host.get_boot_time.return_value = None
    host.get_uptime_seconds.return_value = 0.0
    host.get_provider_resources.return_value = None
    host.is_lock_held.return_value = False
    host.get_certified_data.return_value = _make_certified_data(host_id)
    host.get_snapshots.return_value = []
    host.get_reported_activity_time.return_value = None
    return host


@pytest.fixture
def host_id() -> HostId:
    return HostId.generate()


@pytest.fixture
def provider(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> MockProviderInstance:
    return MockProviderInstance(
        name=ProviderInstanceName("test"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )


def test_get_host_and_agent_details_disconnects_host(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """get_host_and_agent_details disconnects the host after collecting details."""
    online_host = _make_mock_online_host(host_id)
    online_host.get_agents.return_value = []

    provider.mock_hosts = [online_host]

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_id = AgentId.generate()
    agent_ref = _make_agent_ref(host_id, agent_id, provider.name)

    provider.get_host_and_agent_details(host_ref, [agent_ref])

    online_host.disconnect.assert_called_once()


def test_get_host_and_agent_details_disconnects_on_connection_error(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """get_host_and_agent_details disconnects the host even when HostConnectionError occurs."""
    online_host = _make_mock_online_host(host_id)
    online_host.get_agents.side_effect = HostConnectionError("SSH error")

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_id = AgentId.generate()
    agent_ref = _make_agent_ref(host_id, agent_id, provider.name)

    provider.get_host_and_agent_details(host_ref, [agent_ref])

    online_host.disconnect.assert_called_once()


def test_connection_error_during_get_agents_falls_back_to_offline(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """HostConnectionError during host.get_agents() should fall back to offline data."""
    online_host = _make_mock_online_host(host_id)
    online_host.get_agents.side_effect = HostConnectionError("SSH error (Error reading SSH protocol banner)")

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_id = AgentId.generate()
    agent_ref = _make_agent_ref(host_id, agent_id, provider.name)

    # This should NOT raise -- it should fall back to offline data
    host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    assert len(agent_details_list) == 1
    assert agent_details_list[0].name == "test-agent"
    assert agent_details_list[0].state == AgentLifecycleState.STOPPED


def test_connection_error_during_agent_detail_building_falls_back_to_offline(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """HostConnectionError during _build_agent_details_from_online_agent should fall back to offline data."""
    agent_id = AgentId.generate()

    # Create a mock agent that raises HostConnectionError when get_reported_url is called.
    # Earlier methods (get_reported_activity_time, get_command, etc.) must succeed
    # so the error occurs mid-way through _build_agent_details_from_online_agent.
    mock_agent = MagicMock()
    mock_agent.id = agent_id
    mock_agent.name = AgentName("test-agent")
    mock_agent.get_reported_activity_time.return_value = None
    mock_agent.get_reported_url.side_effect = HostConnectionError("SSH connection dropped")

    online_host = _make_mock_online_host(host_id)
    online_host.get_agents.return_value = [mock_agent]
    online_host.get_activity_config.return_value = MagicMock(
        idle_mode=MagicMock(value="ssh"),
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.SSH,),
    )

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, agent_id, provider.name)

    # This should NOT raise -- it should fall back to offline data
    host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    assert len(agent_details_list) == 1
    assert agent_details_list[0].name == "test-agent"
    assert agent_details_list[0].state == AgentLifecycleState.STOPPED


def test_offline_field_generators_populate_plugin_data_via_get_host_and_agent_details(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """When a host falls back to offline data, offline_field_generators populate plugin fields."""
    online_host = _make_mock_online_host(host_id)
    online_host.get_agents.side_effect = HostConnectionError("SSH error")

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider.name)
    offline_field_generators = {"demo": {"kind": lambda ref, host: ref.certified_data.get("type")}}

    _host_details, agent_details_list = provider.get_host_and_agent_details(
        host_ref, [agent_ref], offline_field_generators=offline_field_generators
    )

    assert len(agent_details_list) == 1
    assert agent_details_list[0].plugin == {"demo": {"kind": "generic"}}


# =============================================================================
# discover_hosts_and_agents disconnect tests
# =============================================================================


def test_discover_agents_on_host_disconnects(host_id: HostId, provider: MockProviderInstance) -> None:
    """_discover_agents_on_host calls discover_agents then disconnect."""
    mock_host = MagicMock(spec=HostInterface)
    mock_host.id = host_id
    mock_host.get_name.return_value = HostName("test-host")
    mock_host.discover_agents.return_value = []

    provider.mock_hosts = [mock_host]

    result = _discover_agents_on_host(provider, host_id)

    assert result == []
    mock_host.disconnect.assert_called_once()


def test_discover_hosts_and_agents_disconnects_hosts(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """discover_hosts_and_agents disconnects each host after fetching agents."""
    mock_host = MagicMock(spec=HostInterface)
    mock_host.id = host_id
    mock_host.get_name.return_value = HostName("test-host")
    mock_host.get_state.return_value = HostState.RUNNING
    mock_host.discover_agents.return_value = []

    provider.mock_hosts = [mock_host]

    provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)

    mock_host.disconnect.assert_called_once()


# =============================================================================
# build_agent_details_from_offline_ref offline field generator tests
# =============================================================================


def _make_offline_host_details(host_id: HostId, provider_name: ProviderInstanceName) -> HostDetails:
    return HostDetails(
        id=host_id,
        name="test-host",
        provider_name=provider_name,
        state=HostState.CRASHED,
    )


def test_build_agent_details_from_offline_ref_without_generators_has_empty_plugin(host_id: HostId) -> None:
    """With no offline field generators, plugin data is empty (the prior behavior)."""
    provider_name = ProviderInstanceName("test")
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider_name)
    host_details = _make_offline_host_details(host_id, provider_name)

    agent_details = build_agent_details_from_offline_ref(agent_ref, host_details)

    assert agent_details.plugin == {}


def test_build_agent_details_from_offline_ref_populates_plugin_data(host_id: HostId) -> None:
    """Offline field generators receive (agent_ref, host_details) and populate plugin data."""
    provider_name = ProviderInstanceName("test")
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("test-agent"),
        provider_name=provider_name,
        certified_data={"plugin": {"demo_plugin": {"flag": True}}},
    )
    host_details = _make_offline_host_details(host_id, provider_name)
    offline_field_generators = {
        "demo_plugin": {
            "flag": lambda ref, host: ref.certified_data.get("plugin", {}).get("demo_plugin", {}).get("flag", False),
        }
    }

    agent_details = build_agent_details_from_offline_ref(agent_ref, host_details, offline_field_generators)

    assert agent_details.plugin == {"demo_plugin": {"flag": True}}


def test_build_agent_details_from_offline_ref_omits_none_field_values(host_id: HostId) -> None:
    """Fields whose generator returns None are omitted, and empty plugins are dropped."""
    provider_name = ProviderInstanceName("test")
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider_name)
    host_details = _make_offline_host_details(host_id, provider_name)
    offline_field_generators = {
        "plugin_a": {
            "present": lambda ref, host: "yes",
            "absent": lambda ref, host: None,
        },
        "plugin_b": {
            "also_absent": lambda ref, host: None,
        },
    }

    agent_details = build_agent_details_from_offline_ref(agent_ref, host_details, offline_field_generators)

    assert agent_details.plugin == {"plugin_a": {"present": "yes"}}


def test_discover_hosts_and_agents_tolerates_per_host_connection_error(
    provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """One host's HostConnectionError must not poison the provider's whole enumeration.

    A wedged container (sshd hang, banner reset, auth failure) used to abort
    the provider's entire discovery, which downstream blanked the discovery
    snapshot and broke mngr_forward's resolver for every workspace. The
    unreachable host now falls back to its offline view (here yielding no
    persisted agents) while the rest of the provider's hosts come through
    normally.
    """
    healthy_host_id = HostId.generate()
    broken_host_id = HostId.generate()
    healthy_agent_id = AgentId.generate()
    healthy_agent_ref = _make_agent_ref(healthy_host_id, healthy_agent_id, provider.name)

    healthy_host = MagicMock(spec=HostInterface)
    healthy_host.id = healthy_host_id
    healthy_host.get_name.return_value = HostName("healthy-host")
    healthy_host.get_state.return_value = HostState.RUNNING
    healthy_host.discover_agents.return_value = [healthy_agent_ref]

    broken_host = MagicMock(spec=HostInterface)
    broken_host.id = broken_host_id
    broken_host.get_name.return_value = HostName("broken-host")
    broken_host.get_state.return_value = HostState.RUNNING
    broken_host.discover_agents.side_effect = HostConnectionError("SSH error (Error reading SSH protocol banner)")

    provider.mock_hosts = [healthy_host, broken_host]
    # The broken host has an offline view (mock_agent_data is empty, so it
    # yields no agents). Without one, to_offline_host would raise and -- since
    # the offline fallback no longer swallows that -- re-poison the provider.
    provider.mock_offline_hosts = {str(broken_host_id): _make_offline_host(broken_host_id, provider, temp_mngr_ctx)}

    results = provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)

    healthy_ref = next(ref for ref in results if ref.host_id == healthy_host_id)
    assert results[healthy_ref] == [healthy_agent_ref]

    broken_ref = next(ref for ref in results if ref.host_id == broken_host_id)
    assert results[broken_ref] == []

    # The connected_host context manager's finally must still run on the
    # failure path, so both hosts had disconnect() called.
    healthy_host.disconnect.assert_called_once()
    broken_host.disconnect.assert_called_once()

    # The cache-invalidation hook must fire for the broken host so providers
    # that cache per-host state (docker/modal/lima/vps_docker) drop the
    # wedged entry instead of replaying it on the next discovery cycle.
    assert provider.connection_errors_cleared == [broken_host_id]


def test_discover_hosts_and_agents_falls_back_to_offline_on_connection_error(
    provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """When the online path raises HostConnectionError but the provider has
    an offline view (e.g. a docker container that is still RUNNING but
    whose sshd has died), agents from the offline view should populate the
    discovery result. This mirrors the behavior of a fully-stopped
    container, whose agents remain visible via the offline path.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()

    broken_host = MagicMock(spec=HostInterface)
    broken_host.id = host_id
    broken_host.get_name.return_value = HostName("ssh-dead-host")
    broken_host.get_state.return_value = HostState.RUNNING
    broken_host.discover_agents.side_effect = HostConnectionError("Error reading SSH protocol banner")

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [broken_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}
    provider.mock_agent_data = [
        {
            "id": str(agent_id),
            "name": "ssh-dead-agent",
            "labels": {"workspace": "ws-42", "is_primary": "true"},
        }
    ]

    results = provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)

    host_ref = next(iter(results))
    assert host_ref.host_id == host_id
    offline_agents = results[host_ref]
    assert len(offline_agents) == 1
    assert offline_agents[0].agent_id == agent_id
    # Labels must be preserved through the offline fallback so downstream
    # consumers (e.g. minds, which filters on workspace/is_primary labels)
    # do not silently drop the agents.
    assert offline_agents[0].labels == {"workspace": "ws-42", "is_primary": "true"}

    # The connected_host cleanup and on_connection_error hook still run.
    broken_host.disconnect.assert_called_once()
    assert provider.connection_errors_cleared == [host_id]
