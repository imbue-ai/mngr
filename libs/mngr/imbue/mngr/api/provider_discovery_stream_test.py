import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Final

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
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import _provider_config_registry
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import BoundedProviderDiscoveryResult
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import HostDiscoveryReadRegistry
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
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
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.providers.registry import _backend_registry
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


_CONTROLLABLE_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("controllable")


def _make_controllable_provider(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
    is_released: bool,
    provider_name: ProviderInstanceName = _CONTROLLABLE_PROVIDER_NAME,
) -> _ControllableProvider:
    """Build a controllable provider and register it as ``provider_name``'s built instance.

    Registering it in the instance cache is how the poller gets hold of it: the poller
    resolves its provider by name through ``get_provider_instance`` on every poll, which
    returns a cached instance without consulting any backend. The ``temp_mngr_ctx``
    fixture clears the cache on teardown.
    """
    provider = _ControllableProvider(
        name=provider_name,
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )
    if is_released:
        provider.release()
    _instance_cache[(provider_name, id(temp_mngr_ctx))] = provider
    return provider


def _submit_discovery(
    executor: _MngrExecutor,
    provider: BaseProviderInstance,
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


_UNAVAILABLE_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("unavailable-at-construction")
_EMPTY_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("empty-at-construction")
_UNAUTHORIZED_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("unauthorized-at-construction")


class _UnavailableAtConstructionBackend(ProviderBackendInterface):
    """Backend that cannot be reached, the way a paused Docker Desktop reports itself."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return _UNAVAILABLE_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Test backend that is unreachable at construction time"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        del config, mngr_ctx
        raise ProviderUnavailableError(provider_name=name, reason="simulated paused backend from test")


class _EmptyAtConstructionBackend(ProviderBackendInterface):
    """Backend that is reachable but holds nothing yet, the way docker reports no state container."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return _EMPTY_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Test backend that reports itself empty at construction time"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        del config, mngr_ctx
        raise ProviderEmptyError(provider_name=name, reason="simulated empty backend from test")


class _UnauthorizedAtConstructionBackend(ProviderBackendInterface):
    """Backend with no usable credentials, which no amount of retrying can fix."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return _UNAUTHORIZED_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Test backend that reports missing credentials at construction time"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        del config, mngr_ctx
        raise ProviderNotAuthorizedError(provider_name=name, reason="simulated missing credentials from test")


@contextmanager
def _registered_backend(backend: type[ProviderBackendInterface]) -> Iterator[ProviderInstanceName]:
    """Register a test backend and yield the provider-instance name that resolves to it.

    The name matches the backend name so it resolves as an implicit-default instance,
    with no ``[providers.<name>]`` block needed.
    """
    backend_name = backend.get_name()
    _backend_registry[backend_name] = backend
    _provider_config_registry[backend_name] = ProviderInstanceConfig
    try:
        yield ProviderInstanceName(str(backend_name))
    finally:
        del _backend_registry[backend_name]
        del _provider_config_registry[backend_name]


def _discard_line(line: str) -> None:
    """Sink for the stream's tail, so its JSONL does not land on the test's stdout."""
    del line


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


@pytest.mark.allow_warnings(match="could not be built")
def test_a_provider_that_cannot_be_built_keeps_its_poller_and_recovers(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A provider whose *construction* fails must be retried, not written off for the process.

    This is the paused-Docker-Desktop case: the backend reports itself unreachable when
    the poller tries to build it, and the condition ends the moment the user unpauses.
    The stream used to build every instance once at startup, so a provider that failed
    there got a single snapshot and no poller at all -- while a provider that constructed
    and then failed every poll kept its poller and recovered on its own. Each failed poll
    now emits the same error snapshot the startup path used to emit once, and a later poll
    that can build the provider produces a normal snapshot from the same poller.
    """
    with _registered_backend(_UnavailableAtConstructionBackend) as provider_name:
        poller = _ProviderDiscoveryPoller(
            provider_name=provider_name, mngr_ctx=temp_mngr_ctx, config=_generous_config()
        )
        with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
            poller.poll_and_emit(lambda provider: _submit_discovery(executor, provider, temp_mngr_ctx, poller))
            poller.poll_and_emit(lambda provider: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

            # Both polls reported the provider as errored, rather than one of them going quiet.
            errored = _read_snapshots(temp_mngr_ctx)
            assert len(errored) == 2
            for snapshot in errored:
                assert snapshot.provider_name == provider_name
                assert snapshot.error is not None
                assert snapshot.error.type_name == "ProviderUnavailableError"
                assert "simulated paused backend from test" in snapshot.error.message
                assert snapshot.agents == ()
                assert snapshot.hosts == ()
                # The config rides along so the minds providers panel can render the
                # provider at all while it is failing to construct.
                assert snapshot.provider is not None
                assert snapshot.provider.provider_name == provider_name

            # The provider comes back (the user unpauses Docker).
            host = DiscoveredHost(
                host_id=HostId.generate(),
                host_name=HostName("recovered-host"),
                provider_name=provider_name,
                host_state=HostState.RUNNING,
            )
            recovered = _make_controllable_provider(
                temp_host_dir, temp_mngr_ctx, is_released=True, provider_name=provider_name
            )
            recovered.result_agents_by_host = {host: []}

            poller.poll_and_emit(lambda provider: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

    snapshots = _read_snapshots(temp_mngr_ctx)
    assert len(snapshots) == 3
    assert snapshots[-1].error is None
    assert {h.host_id for h in snapshots[-1].hosts} == {host.host_id}


@pytest.mark.allow_warnings(match="could not be built")
def test_a_provider_whose_backend_is_missing_still_reports_every_poll(temp_mngr_ctx: MngrContext) -> None:
    """A construction failure the poller does not specifically know about must still be reported.

    ``list_provider_names_to_load`` hands back every name in ``[providers.*]`` without
    checking that its backend is registered, so a config naming a backend this install
    does not have produces a poller whose ``get_provider_instance`` raises
    ``UnknownBackendError``. That is neither empty, unavailable, nor unauthorized, so it
    would otherwise fall through to the poll loop's warn-and-continue handler, which
    writes no snapshot -- leaving exactly the invisible provider this whole mechanism
    exists to prevent, with the process still alive and exit-0.
    """
    provider_name = ProviderInstanceName("a-backend-nobody-installed")
    config = ProviderInstanceConfig(
        backend=ProviderBackendName(str(provider_name)),
        discovery_poll_interval_seconds=PositiveFloat(60.0),
    )
    poller = _ProviderDiscoveryPoller(provider_name=provider_name, mngr_ctx=temp_mngr_ctx, config=config)

    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda provider: _submit_discovery(executor, provider, temp_mngr_ctx, poller))
        poller.poll_and_emit(lambda provider: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

    snapshots = _read_snapshots(temp_mngr_ctx)
    assert len(snapshots) == 2
    for snapshot in snapshots:
        assert snapshot.provider_name == provider_name
        assert snapshot.error is not None
        assert snapshot.error.type_name == "UnknownBackendError"


def test_an_empty_provider_emits_clean_snapshots_until_it_has_something(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A known-empty provider is retried every poll, and its snapshots stay clean.

    ``ProviderEmptyError`` is a sibling of ``ProviderUnavailableError``, not a subclass,
    so it needs retrying in its own right: docker raises it when its state container does
    not exist yet, and the user creating a first workspace is exactly what ends that. It
    is a healthy state, so its snapshot carries no error.

    Retried at the ordinary cadence with no extra backoff. A failing build costs less per
    poll than a successful one (which pays a full re-read of the provider's hosts and
    agents), so slowing the retry down would save less than a healthy provider already
    spends, and would pay for it in how long the user waits for discovery to notice.
    """
    with _registered_backend(_EmptyAtConstructionBackend) as provider_name:
        poller = _ProviderDiscoveryPoller(
            provider_name=provider_name, mngr_ctx=temp_mngr_ctx, config=_generous_config()
        )

        with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:

            def _poll() -> None:
                poller.poll_and_emit(lambda provider: _submit_discovery(executor, provider, temp_mngr_ctx, poller))

            _poll()
            _poll()
            _poll()

            empty_snapshots = _read_snapshots(temp_mngr_ctx)
            assert len(empty_snapshots) == 3
            assert [snapshot.error for snapshot in empty_snapshots] == [None, None, None]
            assert all(snapshot.agents == () for snapshot in empty_snapshots)
            assert all(snapshot.hosts == () for snapshot in empty_snapshots)

            # The user creates their first workspace, so the provider now builds. It has
            # to bring a host with it: a successful discovery of a provider holding
            # nothing writes the same clean zero-host snapshot the empty-construction
            # skip does, so a bare snapshot count cannot tell recovery from another
            # failed build.
            host = DiscoveredHost(
                host_id=HostId.generate(),
                host_name=HostName("first-workspace-host"),
                provider_name=provider_name,
                host_state=HostState.RUNNING,
            )
            recovered = _make_controllable_provider(
                temp_host_dir, temp_mngr_ctx, is_released=True, provider_name=provider_name
            )
            recovered.result_agents_by_host = {host: []}
            _poll()

    snapshots = _read_snapshots(temp_mngr_ctx)
    assert len(snapshots) == 4
    assert snapshots[-1].error is None
    assert {h.host_id for h in snapshots[-1].hosts} == {host.host_id}


@pytest.mark.allow_warnings(match="not authorized")
def test_an_unauthorized_provider_ends_only_its_own_poller(temp_mngr_ctx: MngrContext) -> None:
    """Missing credentials end one poller cleanly -- they must not read as a crashed poller.

    Credentials change through a user action that restarts this process, so retrying
    would just re-report the same thing every poll forever. But the poller has to end by
    *returning* rather than by raising: one provider's missing API key must not take
    down the thread, and so the discovery, of every other provider.
    """
    stop_event = threading.Event()
    with _registered_backend(_UnauthorizedAtConstructionBackend) as provider_name:
        poller = _ProviderDiscoveryPoller(
            provider_name=provider_name, mngr_ctx=temp_mngr_ctx, config=_generous_config()
        )

        # Returns on its own: the poll interval is 60s, so anything that waits hangs here.
        poller.run(stop_event)

    assert not stop_event.is_set()

    snapshots = _read_snapshots(temp_mngr_ctx)
    assert len(snapshots) == 1
    assert snapshots[0].provider_name == provider_name
    assert snapshots[0].error is not None
    assert snapshots[0].error.type_name == "ProviderNotAuthorizedError"
    assert snapshots[0].provider is not None


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

    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))

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

    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))

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

    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))

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

    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_generous_config())
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))

    snapshots = _read_snapshots(temp_mngr_ctx)
    assert len(snapshots) == 1
    error = snapshots[0].error
    assert error is not None
    assert error.provider_name == provider.name
    assert snapshots[0].agents == ()
    # The snapshot is the only durable record of a failed poll -- nothing logs a
    # traceback around it -- so it must carry one naming the code that raised.
    assert error.traceback_text is not None
    assert error.traceback_text.startswith("Traceback (most recent call last):")
    assert "discover_hosts_and_agents_within_timeouts" in error.traceback_text
    assert "provider exploded during discovery" in error.traceback_text


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
    poller = _ProviderDiscoveryPoller(
        provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_tiny_timeout_config()
    )

    try:
        with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
            # First poll times out (discovery is blocked) -> error snapshot, orphan kept.
            poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))
            timeout_snapshots = _read_snapshots(temp_mngr_ctx)
            assert len(timeout_snapshots) == 1
            assert timeout_snapshots[0].error is not None
            # Wait for the orphaned discovery thread to actually begin (it then blocks on the gate).
            poll_until(lambda: provider.discovery_call_count == 1, timeout=5.0)

            # While the orphan is still in flight, another poll must NOT start a second
            # discovery -- but must still emit, so a provider that stays wedged keeps
            # reading as alive-and-erroring rather than fading into silence.
            poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))
            assert provider.discovery_call_count == 1
            wedged_snapshots = _read_snapshots(temp_mngr_ctx)
            assert len(wedged_snapshots) == 2
            assert wedged_snapshots[-1].error is not None
            assert wedged_snapshots[-1].discovery_finished_at > wedged_snapshots[0].discovery_finished_at

            # Release the orphaned discovery; once it finishes, a poll harvests its late
            # result. Polled until a *non-errored* snapshot lands, since the wedged
            # re-emits above are themselves errored snapshots.
            provider.release()
            poll_until(
                lambda: poller.poll_and_emit(
                    lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller)
                )
                or _read_snapshots(temp_mngr_ctx)[-1].error is None,
                timeout=5.0,
            )
            snapshots = _read_snapshots(temp_mngr_ctx)
            assert snapshots[-1].error is None
            assert {h.host_id for h in snapshots[-1].hosts} == {host.host_id}
    finally:
        provider.release()
