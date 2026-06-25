from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from imbue.mngr.api.discover import _reconcile_running_hosts_with_live_agents
from imbue.mngr.api.discover import _reconciled
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.mock_provider_test import MockProviderInstance

_PROVIDER = ProviderInstanceName("modal-test")


def _agent(host_id: HostId, name: str) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName(name),
        provider_name=_PROVIDER,
        certified_data={},
    )


def _host(host_id: HostId, name: str, state: HostState) -> DiscoveredHost:
    return DiscoveredHost(host_id=host_id, host_name=HostName(name), provider_name=_PROVIDER, host_state=state)


def _provider(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
    hosts: list[MagicMock],
    live_discovery: bool = False,
) -> MockProviderInstance:
    # live_discovery=False models Modal (cached/volume discovery -> eligible for the
    # live re-read); True models every other provider (already-live discovery -> skipped).
    return MockProviderInstance(
        name=_PROVIDER,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_hosts=cast("list[HostInterface]", hosts),
        mock_discovers_agents_from_live_host=live_discovery,
    )


def _live_host(host_id: HostId, name: str, agents: list[DiscoveredAgent]) -> MagicMock:
    host = MagicMock(spec=OnlineHostInterface)
    host.id = host_id
    host.get_name.return_value = HostName(name)
    host.discover_agents.return_value = agents
    return host


def test_reconcile_finds_live_agent_missing_from_discovery(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """A live agent absent from a RUNNING host's (stale) discovery results is recovered via a live read."""
    host_id = HostId.generate()
    discovered_host = _host(host_id, "h1", HostState.RUNNING)
    live_agent = _agent(host_id, "system-services")
    # Discovery (e.g. Modal's state volume) returned nothing for this still-running host.
    agents_by_host = {discovered_host: []}
    live_host = _live_host(host_id, "h1", [live_agent])
    provider = _provider(temp_host_dir, temp_mngr_ctx, [live_host])

    _reconcile_running_hosts_with_live_agents(agents_by_host, [provider], [str(live_agent.agent_id)])

    assert agents_by_host[discovered_host] == [live_agent]
    live_host.disconnect.assert_called_once()


def test_reconcile_is_noop_when_all_ids_already_found(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """When the requested id is already in discovery, no live read happens (no extra SSH)."""
    host_id = HostId.generate()
    agent = _agent(host_id, "a1")
    discovered_host = _host(host_id, "h1", HostState.RUNNING)
    agents_by_host = {discovered_host: [agent]}
    live_host = _live_host(host_id, "h1", [agent])
    provider = _provider(temp_host_dir, temp_mngr_ctx, [live_host])

    _reconcile_running_hosts_with_live_agents(agents_by_host, [provider], [str(agent.agent_id)])

    live_host.discover_agents.assert_not_called()


def test_reconciled_is_noop_when_identifiers_is_none(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """The unfiltered (no-identifier) callers never trigger a live read."""
    host_id = HostId.generate()
    discovered_host = _host(host_id, "h1", HostState.RUNNING)
    live_host = _live_host(host_id, "h1", [_agent(host_id, "a1")])
    provider = _provider(temp_host_dir, temp_mngr_ctx, [live_host])
    agents_by_host = {discovered_host: []}

    _reconciled(agents_by_host, [provider], None)

    live_host.discover_agents.assert_not_called()


def test_reconcile_skips_non_running_hosts(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """Only RUNNING hosts are read live; a stopped host is left as discovery reported it."""
    host_id = HostId.generate()
    discovered_host = _host(host_id, "h1", HostState.STOPPED)
    live_host = _live_host(host_id, "h1", [_agent(host_id, "a1")])
    provider = _provider(temp_host_dir, temp_mngr_ctx, [live_host])
    agents_by_host = {discovered_host: []}

    _reconcile_running_hosts_with_live_agents(agents_by_host, [provider], ["agent-not-present"])

    live_host.discover_agents.assert_not_called()
    assert agents_by_host[discovered_host] == []


def test_reconcile_skips_live_discovery_providers(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """Non-Modal providers (discovery already live) are never re-read -- reconciliation is Modal-scoped."""
    host_id = HostId.generate()
    discovered_host = _host(host_id, "h1", HostState.RUNNING)
    live_host = _live_host(host_id, "h1", [_agent(host_id, "a1")])
    provider = _provider(temp_host_dir, temp_mngr_ctx, [live_host], live_discovery=True)
    agents_by_host = {discovered_host: []}

    _reconcile_running_hosts_with_live_agents(agents_by_host, [provider], ["agent-not-present"])

    live_host.discover_agents.assert_not_called()
    assert agents_by_host[discovered_host] == []


def test_reconcile_degrades_when_host_unreachable(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """An unreachable running host keeps its existing refs and does not fail the whole resolve."""
    host_id = HostId.generate()
    discovered_host = _host(host_id, "h1", HostState.RUNNING)
    agents_by_host = {discovered_host: []}
    # No mock_hosts -> MockProviderInstance.get_host raises HostNotFoundError, which is swallowed.
    provider = _provider(temp_host_dir, temp_mngr_ctx, [])

    _reconcile_running_hosts_with_live_agents(agents_by_host, [provider], ["agent-not-present"])

    assert agents_by_host[discovered_host] == []
