"""Tests for ProviderInstanceInterface default method implementations."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.mock_agent_test import MockAgent
from imbue.mngr.interfaces.mock_host_test import MockOnlineHost
from imbue.mngr.interfaces.provider_instance import _discover_agents_on_host
from imbue.mngr.interfaces.provider_instance import build_agent_details_from_offline_ref
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
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


def _make_mock_online_host(host_id: HostId, *, host_name: str = "test-host", **kwargs: Any) -> MockOnlineHost:
    """Build a concrete online host whose behavior is set via fields.

    Pass ``agents=`` / ``discovered_agents=`` for the happy path or
    ``raise_connection_error_on_get_agents=True`` /
    ``raise_connection_error_on_discover_agents=True`` to simulate a host whose
    sshd dies after discovery.
    """
    return MockOnlineHost(
        id=host_id,
        host_name=HostName(host_name),
        certified_data=_make_certified_data(host_id),
        **kwargs,
    )


def _make_mock_agent(agent_id: AgentId, host_id: HostId, mngr_ctx: MngrContext, **kwargs: Any) -> MockAgent:
    return MockAgent(
        id=agent_id,
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("generic"),
        work_dir=Path("/tmp/test"),
        create_time=datetime.now(timezone.utc),
        host_id=host_id,
        mngr_ctx=mngr_ctx,
        agent_config=AgentTypeConfig(),
        **kwargs,
    )


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
    online_host = _make_mock_online_host(host_id, agents=[])
    provider.mock_hosts = [online_host]

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider.name)

    host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    assert online_host.disconnect_count == 1
    # The collected details still come back (the disconnect happens in a finally).
    assert host_details.name == "test-host"
    assert len(agent_details_list) == 1


def test_get_host_and_agent_details_disconnects_on_connection_error(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """get_host_and_agent_details disconnects the host even when HostConnectionError occurs."""
    online_host = _make_mock_online_host(host_id, raise_connection_error_on_get_agents=True)

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider.name)

    _host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    # The connection is released even on the error path...
    assert online_host.disconnect_count == 1
    # ...and the cleanup is tied to actual recovery: the agent comes back via the
    # offline fallback and the per-host connection cache is cleared.
    assert len(agent_details_list) == 1
    assert agent_details_list[0].state == AgentLifecycleState.STOPPED
    assert provider.connection_errors_cleared == [host_id]


def test_connection_error_during_get_agents_falls_back_to_offline(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """HostConnectionError during host.get_agents() should fall back to offline data."""
    online_host = _make_mock_online_host(host_id, raise_connection_error_on_get_agents=True)

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider.name)

    # This should NOT raise -- it should fall back to offline data.
    host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    assert len(agent_details_list) == 1
    assert agent_details_list[0].name == "test-agent"
    assert agent_details_list[0].state == AgentLifecycleState.STOPPED
    # The offline branch leaves idle_mode unset (distinct from a live agent).
    assert agent_details_list[0].idle_mode is None


def test_connection_error_during_agent_detail_building_falls_back_to_offline(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """HostConnectionError raised mid-way through _build_agent_details_from_online_agent falls back to offline.

    The live agent is enumerated successfully (get_agents returns it) and its
    early getters (get_command, get_lifecycle_state, ...) all succeed; only
    get_reported_url raises. So if the production code stopped calling
    get_reported_url, the build would complete online and the agent would be
    RUNNING -- this test would then fail on the STOPPED assertion, which is
    exactly the regression protection a bare MagicMock could not provide.
    """
    agent_id = AgentId.generate()
    mock_agent = _make_mock_agent(agent_id, host_id, temp_mngr_ctx, raise_connection_error_on_reported_url=True)

    online_host = _make_mock_online_host(host_id, agents=[mock_agent])

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, agent_id, provider.name)

    # This should NOT raise -- it should fall back to offline data.
    _host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    assert len(agent_details_list) == 1
    assert agent_details_list[0].name == "test-agent"
    # Offline-derived values prove we abandoned the half-built online details
    # rather than surfacing a partially-populated (or live) agent.
    assert agent_details_list[0].state == AgentLifecycleState.STOPPED
    assert agent_details_list[0].idle_mode is None
    assert agent_details_list[0].url is None
    assert online_host.disconnect_count == 1


def test_offline_field_generators_populate_plugin_data_via_get_host_and_agent_details(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """When a host falls back to offline data, offline_field_generators populate plugin fields."""
    online_host = _make_mock_online_host(host_id, raise_connection_error_on_get_agents=True)

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
    mock_host = _make_mock_online_host(host_id, discovered_agents=[])
    provider.mock_hosts = [mock_host]

    result = _discover_agents_on_host(provider, host_id)

    assert result == []
    assert mock_host.disconnect_count == 1


def test_discover_hosts_and_agents_disconnects_hosts(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """discover_hosts_and_agents disconnects each host after fetching agents."""
    mock_host = _make_mock_online_host(host_id, discovered_agents=[])
    provider.mock_hosts = [mock_host]

    provider.discover_hosts_and_agents(cg=temp_mngr_ctx.concurrency_group)

    assert mock_host.disconnect_count == 1


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

    healthy_host = _make_mock_online_host(
        healthy_host_id, host_name="healthy-host", discovered_agents=[healthy_agent_ref]
    )
    broken_host = _make_mock_online_host(
        broken_host_id, host_name="broken-host", raise_connection_error_on_discover_agents=True
    )

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
    assert healthy_host.disconnect_count == 1
    assert broken_host.disconnect_count == 1

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

    broken_host = _make_mock_online_host(
        host_id, host_name="ssh-dead-host", raise_connection_error_on_discover_agents=True
    )

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
    assert broken_host.disconnect_count == 1
    assert provider.connection_errors_cleared == [host_id]
