"""Tests for ProviderInstanceInterface default method implementations."""

import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.test_utils import poll_until
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import HostDiscoveryReadRegistry
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
from imbue.mngr.utils.testing import capture_loguru


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


def test_connection_error_fallback_applies_provider_state_override(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """A provider that confirms the host is up out-of-band overrides the offline state.

    Mirrors a docker container that is still running but whose inner sshd has
    died: the connection-error fallback must report the provider's override
    (UNAUTHENTICATED) on both the host and every agent's nested host, not the
    default offline-derived CRASHED that would make minds skip a host restart's
    stop step.
    """
    online_host = _make_mock_online_host(host_id)
    online_host.get_agents.side_effect = HostConnectionError("Error reading SSH protocol banner")

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}
    provider.mock_connection_error_fallback_state = HostState.UNAUTHENTICATED

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider.name)

    host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    assert host_details.state == HostState.UNAUTHENTICATED
    assert len(agent_details_list) == 1
    assert agent_details_list[0].host.state == HostState.UNAUTHENTICATED


def test_connection_error_fallback_without_override_uses_offline_state(
    host_id: HostId, provider: MockProviderInstance, temp_mngr_ctx: MngrContext
) -> None:
    """Without a provider override, the fallback keeps the default offline-derived state.

    A shutdown-capable provider with no recorded stop reason derives CRASHED.
    This pins that the override hook is purely additive: a provider that does
    not implement it is unaffected.
    """
    online_host = _make_mock_online_host(host_id)
    online_host.get_agents.side_effect = HostConnectionError("Error reading SSH protocol banner")

    offline_host = _make_offline_host(host_id, provider, temp_mngr_ctx)
    provider.mock_hosts = [online_host, offline_host]
    provider.mock_offline_hosts = {str(host_id): offline_host}
    # mock_connection_error_fallback_state left at its default (None).

    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=provider.name,
    )
    agent_ref = _make_agent_ref(host_id, AgentId.generate(), provider.name)

    host_details, agent_details_list = provider.get_host_and_agent_details(host_ref, [agent_ref])

    assert host_details.state == HostState.CRASHED
    assert agent_details_list[0].host.state == HostState.CRASHED


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


# =============================================================================
# discover_hosts_and_agents_within_timeouts per-host timeout tests
# =============================================================================


class _PerHostGatedProvider(MockProviderInstance):
    """Provider whose per-host agent reads can be individually gated.

    Exercises the per-host timeout in ``discover_hosts_and_agents_within_timeouts``
    without a live host connection: ``read_host_agents_for_bounded_discovery`` blocks
    for ``gated_host_id`` until ``release()`` is called, and returns the configured
    agents for every other host immediately.
    """

    gated_host_id: HostId | None = Field(default=None)
    agents_by_host_id: dict[str, list[DiscoveredAgent]] = Field(default_factory=dict)

    _gate: threading.Event = PrivateAttr(default_factory=threading.Event)
    # Records the ``timeout_seconds`` seen on each per-host read, keyed by host id, so tests
    # can assert the per-host bound is threaded down (and how many reads were started).
    _read_timeouts_by_host_id: dict[str, list[float]] = PrivateAttr(default_factory=dict)
    _record_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        return list(self.mock_discovered_hosts)

    def read_host_agents_for_bounded_discovery(
        self,
        host_ref: DiscoveredHost,
        timeout_seconds: float,
    ) -> list[DiscoveredAgent]:
        with self._record_lock:
            self._read_timeouts_by_host_id.setdefault(str(host_ref.host_id), []).append(timeout_seconds)
        if self.gated_host_id is not None and host_ref.host_id == self.gated_host_id:
            self._gate.wait()
        return list(self.agents_by_host_id.get(str(host_ref.host_id), []))

    def read_count_for_host(self, host_id: HostId) -> int:
        with self._record_lock:
            return len(self._read_timeouts_by_host_id.get(str(host_id), []))

    def read_timeouts_for_host(self, host_id: HostId) -> list[float]:
        with self._record_lock:
            return list(self._read_timeouts_by_host_id.get(str(host_id), []))

    def release(self) -> None:
        self._gate.set()


def test_discover_within_timeouts_marks_slow_host_unknown(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """A host whose agent read exceeds the per-host timeout is reported UNKNOWN, and the
    method returns without waiting for that host -- other hosts' agents still come through."""
    provider = _PerHostGatedProvider(
        name=ProviderInstanceName("gated"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    fast_host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("fast"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    slow_host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("slow"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    fast_agent = _make_agent_ref(fast_host.host_id, AgentId.generate(), provider.name)
    provider.mock_discovered_hosts = [fast_host, slow_host]
    provider.agents_by_host_id = {str(fast_host.host_id): [fast_agent]}
    provider.gated_host_id = slow_host.host_id

    try:
        result = provider.discover_hosts_and_agents_within_timeouts(
            cg=temp_mngr_ctx.concurrency_group,
            host_discovery_timeout_seconds=1.0,
            agent_discovery_timeout_seconds=1.0,
        )

        assert slow_host.host_id in result.unknown_host_ids
        assert fast_host.host_id not in result.unknown_host_ids
        assert {h.host_id for h in result.hosts} == {fast_host.host_id}
        assert {a.agent_id for a in result.agents} == {fast_agent.agent_id}
    finally:
        # Release the orphaned read so its daemon thread can exit cleanly.
        provider.release()


def test_discover_within_timeouts_returns_all_when_fast(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """With no slow host, every host's agents are returned and nothing is marked unknown."""
    provider = _PerHostGatedProvider(
        name=ProviderInstanceName("gated"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("h"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    agent = _make_agent_ref(host.host_id, AgentId.generate(), provider.name)
    provider.mock_discovered_hosts = [host]
    provider.agents_by_host_id = {str(host.host_id): [agent]}

    result = provider.discover_hosts_and_agents_within_timeouts(
        cg=temp_mngr_ctx.concurrency_group,
        host_discovery_timeout_seconds=5.0,
        agent_discovery_timeout_seconds=5.0,
    )

    assert result.unknown_host_ids == ()
    assert {h.host_id for h in result.hosts} == {host.host_id}
    assert {a.agent_id for a in result.agents} == {agent.agent_id}


def test_discover_within_timeouts_threads_host_timeout_into_read(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """The per-host timeout is threaded into each host's read (bounded), not left as None.

    Change 1: without a hard per-command timeout, an abandoned read runs forever; here we
    assert the read receives exactly the ``host_discovery_timeout_seconds`` value.
    """
    provider = _PerHostGatedProvider(
        name=ProviderInstanceName("gated"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("h"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.mock_discovered_hosts = [host]
    provider.agents_by_host_id = {
        str(host.host_id): [_make_agent_ref(host.host_id, AgentId.generate(), provider.name)]
    }

    result = provider.discover_hosts_and_agents_within_timeouts(
        cg=temp_mngr_ctx.concurrency_group,
        # Deliberately distinct from the agent timeout so a bug that passes the wrong value
        # (or None) would be caught.
        host_discovery_timeout_seconds=4.0,
        agent_discovery_timeout_seconds=9.0,
    )

    assert result.unknown_host_ids == ()
    assert provider.read_timeouts_for_host(host.host_id) == [4.0]


def test_discover_within_timeouts_reuses_in_flight_read_across_polls(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A host whose prior read is still in flight is not re-read on the next poll, and the
    skip is logged as a warning.

    Change 2: sharing one registry across two polls, a permanently-gated host's read is
    started exactly once (the second poll reuses the in-flight future), both polls report it
    UNKNOWN, and a wedged-host warning is emitted.
    """
    provider = _PerHostGatedProvider(
        name=ProviderInstanceName("gated"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    wedged_host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("wedged"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.mock_discovered_hosts = [wedged_host]
    provider.gated_host_id = wedged_host.host_id
    registry = HostDiscoveryReadRegistry()

    try:
        with capture_loguru() as log_output:
            first = provider.discover_hosts_and_agents_within_timeouts(
                cg=temp_mngr_ctx.concurrency_group,
                host_discovery_timeout_seconds=0.3,
                agent_discovery_timeout_seconds=0.3,
                registry=registry,
            )
            # The first poll must have actually started the read (and stored its future) before
            # the second poll, so the second poll sees it as in-flight.
            assert poll_until(lambda: provider.read_count_for_host(wedged_host.host_id) >= 1)
            second = provider.discover_hosts_and_agents_within_timeouts(
                cg=temp_mngr_ctx.concurrency_group,
                host_discovery_timeout_seconds=0.3,
                agent_discovery_timeout_seconds=0.3,
                registry=registry,
            )

        assert wedged_host.host_id in first.unknown_host_ids
        assert wedged_host.host_id in second.unknown_host_ids
        # The read was started exactly once across both polls (the second reused the in-flight one).
        assert provider.read_count_for_host(wedged_host.host_id) == 1
        assert "prior read still in flight" in log_output.getvalue()
    finally:
        # Release the orphaned read so its daemon thread can exit cleanly.
        provider.release()


def test_discover_within_timeouts_harvests_late_finished_read_on_next_poll(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A wedged host whose read finishes late is harvested (reported discovered) on a later poll.

    Change 2: complements the in-flight-skip path. Sharing one registry, a gated host is
    UNKNOWN on the first poll (its read is left in flight). After the gate is released and the
    read completes, the next poll harvests that finished future -- the host is now discovered
    (its agents surface) rather than UNKNOWN -- and the registry entry is cleared so a third
    poll starts a fresh read.
    """
    provider = _PerHostGatedProvider(
        name=ProviderInstanceName("gated"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("late"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    agent = _make_agent_ref(host.host_id, AgentId.generate(), provider.name)
    provider.mock_discovered_hosts = [host]
    provider.gated_host_id = host.host_id
    provider.agents_by_host_id = {str(host.host_id): [agent]}
    registry = HostDiscoveryReadRegistry()

    first = provider.discover_hosts_and_agents_within_timeouts(
        cg=temp_mngr_ctx.concurrency_group,
        host_discovery_timeout_seconds=0.3,
        agent_discovery_timeout_seconds=0.3,
        registry=registry,
    )
    # The first poll left the read in flight (host UNKNOWN, one read started).
    assert host.host_id in first.unknown_host_ids
    assert poll_until(lambda: provider.read_count_for_host(host.host_id) >= 1)

    # Let the wedged read complete, then wait until its future is actually done so the next
    # poll takes the harvest branch rather than the still-in-flight branch.
    provider.release()
    in_flight = registry.future_by_host_id[host.host_id]
    assert poll_until(in_flight.done)

    second = provider.discover_hosts_and_agents_within_timeouts(
        cg=temp_mngr_ctx.concurrency_group,
        host_discovery_timeout_seconds=0.3,
        agent_discovery_timeout_seconds=0.3,
        registry=registry,
    )
    # The finished-late read is harvested: the host is discovered (not UNKNOWN) and its agent
    # surfaces, without starting a second read.
    assert host.host_id not in second.unknown_host_ids
    assert {h.host_id for h in second.hosts} == {host.host_id}
    assert {a.agent_id for a in second.agents} == {agent.agent_id}
    assert provider.read_count_for_host(host.host_id) == 1
    # The harvest cleared the registry entry, so a third poll starts a fresh read.
    assert host.host_id not in registry.future_by_host_id
    provider.discover_hosts_and_agents_within_timeouts(
        cg=temp_mngr_ctx.concurrency_group,
        host_discovery_timeout_seconds=0.3,
        agent_discovery_timeout_seconds=0.3,
        registry=registry,
    )
    assert provider.read_count_for_host(host.host_id) == 2
