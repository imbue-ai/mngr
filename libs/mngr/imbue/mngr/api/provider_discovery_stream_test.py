import threading
from concurrent.futures import Future
from pathlib import Path

import pytest
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import PositiveFloat
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.provider_discovery_stream import _ProviderDiscoveryPoller
from imbue.mngr.api.provider_discovery_stream import _discover_one_provider
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.interfaces.data_types import BoundedProviderDiscoveryResult
from imbue.mngr.interfaces.provider_instance import HostDiscoveryReadRegistry
from imbue.mngr.interfaces.provider_instance import bounded_result_from_agents_by_host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.thread_cleanup import _MngrExecutor
from imbue.mngr.utils.thread_cleanup import mngr_executor


class _ControllableProvider(MockProviderInstance):
    """Mock provider whose discovery can succeed, raise, or block until released."""

    discovery_call_count: int = 0
    should_raise: bool = False
    result_agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] | None = None
    result_host_ssh_infos: list[tuple[HostId, SSHInfo]] | None = None

    _release_gate: threading.Event = PrivateAttr(default_factory=threading.Event)

    def discover_hosts_and_agents_within_timeouts(
        self,
        cg: ConcurrencyGroup,
        host_discovery_timeout_seconds: float,
        agent_discovery_timeout_seconds: float,
        include_destroyed: bool = False,
        registry: HostDiscoveryReadRegistry | None = None,
    ) -> BoundedProviderDiscoveryResult:
        self.discovery_call_count = self.discovery_call_count + 1
        self._release_gate.wait()
        if self.should_raise:
            raise RuntimeError("provider exploded during discovery")
        return bounded_result_from_agents_by_host(
            dict(self.result_agents_by_host or {}),
            host_ssh_infos=self.result_host_ssh_infos or (),
        )

    def release(self) -> None:
        self._release_gate.set()


def _make_controllable_provider(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
    is_released: bool,
) -> _ControllableProvider:
    provider = _ControllableProvider(
        name=ProviderInstanceName("controllable"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    if is_released:
        provider.release()
    return provider


def _submit_discovery(
    executor: _MngrExecutor,
    provider: _ControllableProvider,
    mngr_ctx: MngrContext,
    poller: _ProviderDiscoveryPoller,
) -> "Future[BoundedProviderDiscoveryResult]":
    """Submit one bounded discovery for ``provider`` to ``executor`` (the poll's submit hook)."""
    return executor.submit(
        _discover_one_provider,
        provider,
        mngr_ctx,
        poller.config.host_discovery_timeout_seconds,
        poller.config.agent_discovery_timeout_seconds,
        True,
        poller._host_read_registry,
    )


def _generous_config() -> ProviderInstanceConfig:
    """Config with a large error timeout, so a discovery that returns promptly is never
    spuriously declared timed-out even under heavy CI load (where thread scheduling is slow)."""
    return ProviderInstanceConfig(
        backend=ProviderBackendName("controllable"),
        discovery_poll_interval_seconds=PositiveFloat(60.0),
        discovery_warn_seconds=PositiveFloat(30.0),
        discovery_error_timeout_seconds=PositiveFloat(120.0),
        host_discovery_timeout_seconds=PositiveFloat(30.0),
        agent_discovery_timeout_seconds=PositiveFloat(30.0),
    )


def _tiny_timeout_config() -> ProviderInstanceConfig:
    """Config with a tiny error timeout used only by the timeout test, whose discovery is
    gated (never completes during the wait), so the timeout fires deterministically
    regardless of load -- the small value just keeps the test fast."""
    return ProviderInstanceConfig(
        backend=ProviderBackendName("controllable"),
        discovery_poll_interval_seconds=PositiveFloat(0.05),
        discovery_warn_seconds=PositiveFloat(0.05),
        discovery_error_timeout_seconds=PositiveFloat(0.1),
        host_discovery_timeout_seconds=PositiveFloat(0.05),
        agent_discovery_timeout_seconds=PositiveFloat(0.05),
    )


def _read_snapshots(temp_mngr_ctx: MngrContext) -> list[ProviderDiscoverySnapshotEvent]:
    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    if not events_path.exists():
        return []
    snapshots: list[ProviderDiscoverySnapshotEvent] = []
    for line in events_path.read_text().splitlines():
        parsed = parse_discovery_event_line(line)
        if isinstance(parsed, ProviderDiscoverySnapshotEvent):
            snapshots.append(parsed)
    return snapshots


def _read_host_ssh_info_events(temp_mngr_ctx: MngrContext) -> list[HostSSHInfoEvent]:
    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    if not events_path.exists():
        return []
    events: list[HostSSHInfoEvent] = []
    for line in events_path.read_text().splitlines():
        parsed = parse_discovery_event_line(line)
        if isinstance(parsed, HostSSHInfoEvent):
            events.append(parsed)
    return events


def test_poller_emits_host_ssh_info_events_from_discovery_result(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A successful poll re-emits each host's SSH endpoint from the result's ``host_ssh_infos``
    as a HOST_SSH_INFO event, so a tunnel consumer (the minds forward) can reach the host from
    the streaming path alone -- without waiting for an occasional full ``mngr list``."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("remote-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    agent = DiscoveredAgent(
        host_id=host.host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("a1"),
        provider_name=provider.name,
        certified_data={},
    )
    ssh_info = SSHInfo(
        user="root",
        host="203.0.113.7",
        port=22013,
        key_path=temp_host_dir / "keys" / "id_ed25519",
        command="ssh -i /keys/id_ed25519 -p 22013 root@203.0.113.7",
    )
    provider.result_agents_by_host = {host: [agent]}
    provider.result_host_ssh_infos = [(host.host_id, ssh_info)]

    poller = _ProviderDiscoveryPoller(provider=provider, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

    ssh_events = _read_host_ssh_info_events(temp_mngr_ctx)
    assert len(ssh_events) == 1
    assert ssh_events[0].host_id == host.host_id
    assert ssh_events[0].ssh == ssh_info


def test_poller_emits_no_host_ssh_info_when_result_has_none(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """A provider that surfaces no SSH info (e.g. local hosts) emits no HOST_SSH_INFO events."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("local-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.result_agents_by_host = {host: []}

    poller = _ProviderDiscoveryPoller(provider=provider, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

    assert _read_host_ssh_info_events(temp_mngr_ctx) == []


def test_poller_emits_success_snapshot(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("h1"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    agent = DiscoveredAgent(
        host_id=host.host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("a1"),
        provider_name=provider.name,
        certified_data={},
    )
    provider.result_agents_by_host = {host: [agent]}

    poller = _ProviderDiscoveryPoller(provider=provider, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

    snapshots = _read_snapshots(temp_mngr_ctx)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.provider_name == provider.name
    assert snapshot.error is None
    assert {a.agent_id for a in snapshot.agents} == {agent.agent_id}
    assert {h.host_id for h in snapshot.hosts} == {host.host_id}
    assert snapshot.discovery_finished_at >= snapshot.discovery_started_at


def test_poller_emits_error_snapshot_on_exception(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    provider.should_raise = True

    poller = _ProviderDiscoveryPoller(provider=provider, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

    snapshots = _read_snapshots(temp_mngr_ctx)
    assert len(snapshots) == 1
    assert snapshots[0].error is not None
    assert snapshots[0].error.provider_name == provider.name
    assert snapshots[0].agents == ()


@pytest.mark.allow_warnings(match=r"discovery is slow|discovery timed out")
def test_poller_timeout_emits_error_then_accepts_late_result(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """A provider that does not finish within the error timeout yields an error snapshot,
    then a later poll harvests the orphaned discovery's late result as a success snapshot."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=False)
    host = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("late-host"),
        provider_name=provider.name,
        host_state=HostState.RUNNING,
    )
    provider.result_agents_by_host = {host: []}
    poller = _ProviderDiscoveryPoller(provider=provider, mngr_ctx=temp_mngr_ctx, config=_tiny_timeout_config())

    try:
        with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
            # First poll times out (discovery is blocked) -> error snapshot, orphan kept.
            poller.poll_and_emit(lambda: _submit_discovery(executor, provider, temp_mngr_ctx, poller))
            timeout_snapshots = _read_snapshots(temp_mngr_ctx)
            assert len(timeout_snapshots) == 1
            assert timeout_snapshots[0].error is not None
            # Wait for the orphaned discovery thread to actually begin (it then blocks on the gate).
            poll_until(lambda: provider.discovery_call_count == 1, timeout=5.0)

            # While the orphan is still in flight, another poll must NOT start a second discovery.
            poller.poll_and_emit(lambda: _submit_discovery(executor, provider, temp_mngr_ctx, poller))
            assert provider.discovery_call_count == 1

            # Release the orphaned discovery; once it finishes, a poll harvests its late result.
            provider.release()
            poll_until(
                lambda: poller.poll_and_emit(lambda: _submit_discovery(executor, provider, temp_mngr_ctx, poller))
                or len(_read_snapshots(temp_mngr_ctx)) >= 2,
                timeout=5.0,
            )
            snapshots = _read_snapshots(temp_mngr_ctx)
            assert len(snapshots) >= 2
            assert snapshots[-1].error is None
            assert {h.host_id for h in snapshots[-1].hosts} == {host.host_id}
    finally:
        provider.release()
