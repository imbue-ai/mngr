"""Tests for ProviderInstanceInterface.get_host_and_agent_details."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import OnlineHostInterface
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


def _make_agent_ref(
    host_id: HostId, agent_id: AgentId, provider_name: ProviderInstanceName
) -> DiscoveredAgent:
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


def _make_offline_host(
    host_id: HostId, provider: MockProviderInstance, mngr_ctx: MngrContext
) -> OfflineHost:
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
