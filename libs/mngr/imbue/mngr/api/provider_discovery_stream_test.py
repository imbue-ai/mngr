import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import PositiveFloat
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.provider_discovery_stream import CREDENTIAL_SOURCE_RETRY_INTERVAL_SECONDS
from imbue.mngr.api.provider_discovery_stream import _ProviderDiscoveryPoller
from imbue.mngr.api.provider_discovery_stream import _compute_host_read_delta
from imbue.mngr.api.provider_discovery_stream import _discover_one_provider
from imbue.mngr.api.provider_discovery_stream import _host_reads_from_result
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import _provider_config_registry
from imbue.mngr.errors import ProviderCredentialSourceMissingError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import BoundedHostRead
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
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.providers.registry import _backend_registry
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.thread_cleanup import _MngrExecutor
from imbue.mngr.utils.thread_cleanup import mngr_executor


class _ControllableProvider(MockProviderInstance):
    """Mock provider whose discovery can succeed, raise, or block until released, and whose host re-reads are canned."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    discovery_call_count: int = 0
    should_raise: bool = False
    result_agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] | None = None
    result_host_ssh_infos: list[tuple[HostId, SSHInfo]] | None = None
    is_reread_supported: bool = True
    reread_by_host_id: dict[HostId, BoundedHostRead] = Field(default_factory=dict)
    reread_error_by_host_id: dict[HostId, Exception] = Field(default_factory=dict)
    reread_count_by_host_id: dict[HostId, int] = Field(default_factory=dict)

    _release_gate: threading.Event = PrivateAttr(default_factory=threading.Event)
    _reread_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def supports_incremental_host_discovery(self) -> bool:
        return self.is_reread_supported

    def read_host_for_incremental_discovery(self, host_ref: DiscoveredHost, timeout_seconds: float) -> BoundedHostRead:
        with self._reread_lock:
            self.reread_count_by_host_id[host_ref.host_id] = self.reread_count_by_host_id.get(host_ref.host_id, 0) + 1
        error = self.reread_error_by_host_id.get(host_ref.host_id)
        if error is not None:
            raise error
        return self.reread_by_host_id[host_ref.host_id]

    def reread_count(self, host_id: HostId) -> int:
        with self._reread_lock:
            return self.reread_count_by_host_id.get(host_id, 0)

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


_CREDENTIAL_SOURCE_MISSING_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName(
    "credential-source-missing-at-construction"
)


class _CredentialSourceMissingAtConstructionBackend(ProviderBackendInterface):
    """Backend whose block names a login this machine lacks; the login may appear later."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return _CREDENTIAL_SOURCE_MISSING_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Test backend that reports a missing credential source at construction time"

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
        raise ProviderCredentialSourceMissingError(
            provider_name=name,
            reason="simulated profile missing from test",
            short_remediation="run `aws configure --profile simulated`",
        )


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


def _read_events(temp_mngr_ctx: MngrContext) -> list[DiscoveryEvent]:
    events_path = get_discovery_events_path(temp_mngr_ctx.config)
    if not events_path.exists():
        return []
    events: list[DiscoveryEvent] = []
    for line in events_path.read_text().splitlines():
        parsed = parse_discovery_event_line(line)
        if parsed is not None:
            events.append(parsed)
    return events


def _read_snapshots(temp_mngr_ctx: MngrContext) -> list[ProviderDiscoverySnapshotEvent]:
    return [event for event in _read_events(temp_mngr_ctx) if isinstance(event, ProviderDiscoverySnapshotEvent)]


def _read_host_ssh_info_events(temp_mngr_ctx: MngrContext) -> list[HostSSHInfoEvent]:
    return [event for event in _read_events(temp_mngr_ctx) if isinstance(event, HostSSHInfoEvent)]


def _make_host(provider_name: ProviderInstanceName, host_state: HostState) -> DiscoveredHost:
    return DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("h-" + get_short_random_string()),
        provider_name=provider_name,
        host_state=host_state,
    )


def _make_agent(host: DiscoveredHost, labels: dict[str, str] | None = None) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host.host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("a-" + get_short_random_string()),
        provider_name=host.provider_name,
        certified_data={"labels": labels or {}},
    )


def _make_ssh_info(temp_host_dir: Path, port: int) -> SSHInfo:
    return SSHInfo(
        user="root",
        host="203.0.113.7",
        port=port,
        key_path=temp_host_dir / "keys" / "id_ed25519",
        command=f"ssh -i /keys/id_ed25519 -p {port} root@203.0.113.7",
    )


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


@pytest.mark.allow_warnings(match="credential source")
def test_a_provider_awaiting_its_credential_source_keeps_polling_slowly_and_names_the_fix(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A block naming a login this machine lacks is re-checked rather than written off.

    The user installs the login outside mngr (``aws configure``, ``az login``,
    ``modal token new``) with nothing restarting this process, so the poller must
    stay alive to notice it; and the snapshot carries the command that fixes it.
    """
    stop_event = threading.Event()
    with _registered_backend(_CredentialSourceMissingAtConstructionBackend) as provider_name:
        poller = _ProviderDiscoveryPoller(
            provider_name=provider_name, mngr_ctx=temp_mngr_ctx, config=_generous_config()
        )
        runner = threading.Thread(target=poller.run, args=(stop_event,), daemon=True)
        runner.start()
        try:
            wait_for(lambda: len(_read_snapshots(temp_mngr_ctx)) >= 1, timeout=10.0)
            # Unlike missing pasted keys, the poller is still waiting for its next check.
            assert runner.is_alive()
            assert CREDENTIAL_SOURCE_RETRY_INTERVAL_SECONDS >= _generous_config().discovery_poll_interval_seconds
        finally:
            stop_event.set()
            runner.join(timeout=10.0)
        assert not runner.is_alive()

    (snapshot,) = _read_snapshots(temp_mngr_ctx)
    assert snapshot.provider_name == provider_name
    assert snapshot.error is not None
    assert snapshot.error.type_name == "ProviderCredentialSourceMissingError"
    assert snapshot.error.remediation == "run `aws configure --profile simulated`"
    assert snapshot.provider is not None


def test_poller_emits_host_ssh_info_events_from_discovery_result(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A successful poll re-emits each host's SSH endpoint from the result's ``host_ssh_infos``
    as a HOST_SSH_INFO event, so a tunnel consumer (the minds forward) can reach the host from
    the streaming path alone -- without waiting for an occasional full ``mngr list``."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    agent = _make_agent(host)
    ssh_info = _make_ssh_info(temp_host_dir, 22013)
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
    host = _make_host(provider.name, HostState.RUNNING)
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


# Between-poll re-reads of known hosts


def _reread_config(host_discovery_poll_interval_seconds: float) -> ProviderInstanceConfig:
    """A generous full-poll config whose re-read interval is the given one."""
    return _generous_config().model_copy_update(
        to_update(
            _generous_config().field_ref().host_discovery_poll_interval_seconds,
            PositiveFloat(host_discovery_poll_interval_seconds),
        )
    )


def test_compute_host_read_delta_reports_nothing_for_an_unchanged_read(temp_host_dir: Path) -> None:
    host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    agent = _make_agent(host)
    ssh_info = _make_ssh_info(temp_host_dir, 22001)
    previous = BoundedHostRead(host=host, agents=(agent,), ssh_info=ssh_info)
    current = BoundedHostRead(host=host, agents=(agent,), ssh_info=ssh_info)

    delta = _compute_host_read_delta(previous, current)

    assert delta.host is None
    assert delta.ssh_info is None
    assert delta.agents == ()
    assert delta.destroyed_agent_ids == ()


def test_compute_host_read_delta_reports_agents_that_appeared_or_changed() -> None:
    host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    unchanged = _make_agent(host)
    relabeled_before = _make_agent(host, labels={"color": "red"})
    relabeled_after = relabeled_before.model_copy_update(
        to_update(relabeled_before.field_ref().certified_data, {"labels": {"color": "blue"}})
    )
    appeared = _make_agent(host)
    previous = BoundedHostRead(host=host, agents=(unchanged, relabeled_before), ssh_info=None)
    current = BoundedHostRead(host=host, agents=(unchanged, relabeled_after, appeared), ssh_info=None)

    delta = _compute_host_read_delta(previous, current)

    assert delta.agents == (relabeled_after, appeared)
    assert delta.destroyed_agent_ids == ()
    assert delta.host is None


@pytest.mark.parametrize(
    ("current_state", "expected_destroyed_count"),
    [(HostState.RUNNING, 1), (HostState.STOPPED, 0), (HostState.UNKNOWN, 0)],
)
def test_compute_host_read_delta_reports_destroyed_agents_only_from_a_live_listing(
    current_state: HostState, expected_destroyed_count: int
) -> None:
    """Only a running host's read enumerates its agents live; any other read answers from cache, so an
    agent missing from it is unread rather than gone."""
    host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    kept = _make_agent(host)
    gone = _make_agent(host)
    previous = BoundedHostRead(host=host, agents=(kept, gone), ssh_info=None)
    current_host = host.model_copy_update(to_update(host.field_ref().host_state, current_state))
    current = BoundedHostRead(host=current_host, agents=(kept,), ssh_info=None)

    delta = _compute_host_read_delta(previous, current)

    assert len(delta.destroyed_agent_ids) == expected_destroyed_count
    if expected_destroyed_count:
        assert delta.destroyed_agent_ids == (gone.agent_id,)
    assert delta.agents == ()


def test_compute_host_read_delta_reports_a_changed_host_listing_and_ssh_endpoint(temp_host_dir: Path) -> None:
    host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    stopped_host = host.model_copy_update(to_update(host.field_ref().host_state, HostState.STOPPED))
    previous = BoundedHostRead(host=host, agents=(), ssh_info=_make_ssh_info(temp_host_dir, 22001))
    moved_ssh_info = _make_ssh_info(temp_host_dir, 22002)
    current = BoundedHostRead(host=stopped_host, agents=(), ssh_info=moved_ssh_info)

    delta = _compute_host_read_delta(previous, current)

    assert delta.host == stopped_host
    assert delta.ssh_info == moved_ssh_info


def test_host_reads_from_result_regroups_a_snapshot_by_host(temp_host_dir: Path) -> None:
    first_host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    second_host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.STOPPED)
    unlisted_host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    first_agents = (_make_agent(first_host), _make_agent(first_host))
    ssh_info = _make_ssh_info(temp_host_dir, 22001)
    result = BoundedProviderDiscoveryResult(
        hosts=(first_host, second_host),
        # The unlisted host's agent (a host that timed out this poll) belongs to no listed host.
        agents=first_agents + (_make_agent(unlisted_host),),
        host_ssh_infos=((first_host.host_id, ssh_info),),
        unknown_host_ids=(unlisted_host.host_id,),
    )

    read_by_host_id = _host_reads_from_result(result)

    assert set(read_by_host_id) == {first_host.host_id, second_host.host_id}
    assert read_by_host_id[first_host.host_id] == BoundedHostRead(
        host=first_host, agents=first_agents, ssh_info=ssh_info
    )
    assert read_by_host_id[second_host.host_id] == BoundedHostRead(host=second_host, agents=(), ssh_info=None)


def test_host_reads_from_result_is_empty_for_an_empty_snapshot() -> None:
    assert _host_reads_from_result(BoundedProviderDiscoveryResult(hosts=(), agents=())) == {}


def _full_poll(poller: _ProviderDiscoveryPoller, temp_mngr_ctx: MngrContext) -> None:
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))


def test_reread_emits_only_what_changed_since_the_host_was_last_read(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A re-read of a known running host emits an event per agent that appeared, nothing while nothing
    changes, and a destroyed event once an agent is gone -- never a provider snapshot."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    services_agent = _make_agent(host)
    provider.result_agents_by_host = {host: [services_agent]}
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.05))
    _full_poll(poller, temp_mngr_ctx)
    event_count_after_poll = len(_read_events(temp_mngr_ctx))

    # A chat agent is created inside the host between full polls.
    chat_agent = _make_agent(host)
    provider.reread_by_host_id[host.host_id] = BoundedHostRead(
        host=host, agents=(services_agent, chat_agent), ssh_info=None
    )
    poller.reread_known_hosts_and_emit()

    appeared_events = _read_events(temp_mngr_ctx)[event_count_after_poll:]
    assert len(appeared_events) == 1
    assert isinstance(appeared_events[0], AgentDiscoveryEvent)
    assert appeared_events[0].agent == chat_agent
    assert len(_read_snapshots(temp_mngr_ctx)) == 1

    # Nothing changed: the steady state is silent.
    poller.reread_known_hosts_and_emit()
    assert len(_read_events(temp_mngr_ctx)) == event_count_after_poll + 1
    assert provider.reread_count(host.host_id) == 2

    # The chat agent is destroyed inside the host.
    provider.reread_by_host_id[host.host_id] = BoundedHostRead(host=host, agents=(services_agent,), ssh_info=None)
    poller.reread_known_hosts_and_emit()

    destroyed_events = _read_events(temp_mngr_ctx)[event_count_after_poll + 1 :]
    assert len(destroyed_events) == 1
    assert isinstance(destroyed_events[0], AgentDestroyedEvent)
    assert destroyed_events[0].agent_id == chat_agent.agent_id
    assert destroyed_events[0].host_id == host.host_id


def test_reread_emits_the_host_when_its_listing_changed(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    provider.result_agents_by_host = {host: []}
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.05))
    _full_poll(poller, temp_mngr_ctx)
    event_count_after_poll = len(_read_events(temp_mngr_ctx))

    stopped_host = host.model_copy_update(to_update(host.field_ref().host_state, HostState.STOPPED))
    provider.reread_by_host_id[host.host_id] = BoundedHostRead(host=stopped_host, agents=(), ssh_info=None)
    poller.reread_known_hosts_and_emit()

    host_events = _read_events(temp_mngr_ctx)[event_count_after_poll:]
    assert len(host_events) == 1
    assert isinstance(host_events[0], HostDiscoveryEvent)
    assert host_events[0].host == stopped_host
    # A host that no longer reads as running is not re-read again until a full poll lists it running.
    poller.reread_known_hosts_and_emit()
    assert provider.reread_count(host.host_id) == 1


def test_reread_covers_only_the_hosts_the_full_poll_listed_as_running(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    running_host = _make_host(provider.name, HostState.RUNNING)
    stopped_host = _make_host(provider.name, HostState.STOPPED)
    provider.result_agents_by_host = {running_host: [], stopped_host: []}
    provider.reread_by_host_id[running_host.host_id] = BoundedHostRead(host=running_host, agents=(), ssh_info=None)
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.05))
    _full_poll(poller, temp_mngr_ctx)

    poller.reread_known_hosts_and_emit()

    assert provider.reread_count(running_host.host_id) == 1
    assert provider.reread_count(stopped_host.host_id) == 0


def test_reread_is_skipped_for_a_provider_that_reads_its_hosts_in_one_batch(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    provider.is_reread_supported = False
    host = _make_host(provider.name, HostState.RUNNING)
    provider.result_agents_by_host = {host: []}
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.05))
    _full_poll(poller, temp_mngr_ctx)

    poller.reread_known_hosts_and_emit()

    assert provider.reread_count(host.host_id) == 0


def test_reread_is_skipped_before_the_first_full_poll(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.05))

    poller.reread_known_hosts_and_emit()

    assert _read_events(temp_mngr_ctx) == []


@pytest.mark.allow_warnings(match=r"discovery is slow|discovery timed out")
def test_reread_is_skipped_while_the_full_poll_is_still_in_flight(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A wedged provider is not piled onto: the hosts its last good poll listed are left alone."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    provider.result_agents_by_host = {host: []}
    provider.reread_by_host_id[host.host_id] = BoundedHostRead(host=host, agents=(), ssh_info=None)
    poller = _ProviderDiscoveryPoller(
        provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_tiny_timeout_config()
    )
    with mngr_executor(parent_cg=temp_mngr_ctx.concurrency_group, name="test-discover", max_workers=1) as executor:
        try:
            poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))
            # The second poll wedges: its discovery blocks on the gate, so the poll times out.
            provider._release_gate.clear()
            poller.poll_and_emit(lambda resolved: _submit_discovery(executor, resolved, temp_mngr_ctx, poller))
            assert _read_snapshots(temp_mngr_ctx)[-1].error is not None

            poller.reread_known_hosts_and_emit()

            assert provider.reread_count(host.host_id) == 0
        finally:
            # Released inside the executor's scope, which waits for the wedged discovery on exit.
            provider.release()


def test_a_failed_reread_emits_nothing_and_leaves_the_host_to_the_next_full_poll(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    provider.result_agents_by_host = {host: [_make_agent(host)]}
    provider.reread_error_by_host_id[host.host_id] = ProviderUnavailableError(provider.name, "outer SSH unreachable")
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.05))
    _full_poll(poller, temp_mngr_ctx)
    event_count_after_poll = len(_read_events(temp_mngr_ctx))

    poller.reread_known_hosts_and_emit()
    poller.reread_known_hosts_and_emit()

    assert len(_read_events(temp_mngr_ctx)) == event_count_after_poll
    assert provider.reread_count(host.host_id) == 1
    # The next full poll lists the host again, which puts it back in the re-read set.
    _full_poll(poller, temp_mngr_ctx)
    poller.reread_known_hosts_and_emit()
    assert provider.reread_count(host.host_id) == 2


def test_reread_never_reads_a_host_whose_read_is_still_in_flight(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    """One read per host at a time: a slow re-read is left running, and the pass does not wait past
    its interval for it; the next pass harvests it once it finished."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    provider.result_agents_by_host = {host: []}
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.05))
    _full_poll(poller, temp_mngr_ctx)
    event_count_after_poll = len(_read_events(temp_mngr_ctx))
    appeared_agent = _make_agent(host)
    started = threading.Event()
    gate = threading.Event()

    class _GatedProvider(_ControllableProvider):
        def read_host_for_incremental_discovery(
            self, host_ref: DiscoveredHost, timeout_seconds: float
        ) -> BoundedHostRead:
            started.set()
            gate.wait()
            return super().read_host_for_incremental_discovery(host_ref, timeout_seconds)

    gated = _GatedProvider(name=provider.name, host_dir=temp_host_dir, mngr_ctx=temp_mngr_ctx)
    gated.reread_by_host_id[host.host_id] = BoundedHostRead(host=host, agents=(appeared_agent,), ssh_info=None)
    poller._provider = gated
    try:
        poller.reread_known_hosts_and_emit()
        assert started.wait(timeout=5.0)
        poller.reread_known_hosts_and_emit()
        # Both passes left the one gated read running, and neither emitted for the host.
        assert list(poller._host_read_registry.future_by_host_id) == [host.host_id]
        assert gated.reread_count(host.host_id) == 0
        assert len(_read_events(temp_mngr_ctx)) == event_count_after_poll
    finally:
        gate.set()
    in_flight = poller._host_read_registry.future_by_host_id[host.host_id]
    assert poll_until(in_flight.done)
    assert gated.reread_count(host.host_id) == 1

    poller.reread_known_hosts_and_emit()

    harvested = _read_events(temp_mngr_ctx)[event_count_after_poll:]
    assert [event.agent for event in harvested if isinstance(event, AgentDiscoveryEvent)] == [appeared_agent]


def test_run_rereads_known_hosts_between_full_polls(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> None:
    """The poll loop re-reads the hosts its full poll listed, on the re-read cadence, so an agent created
    inside a known host reaches the stream long before the next full poll."""
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    provider.result_agents_by_host = {host: []}
    provider.reread_by_host_id[host.host_id] = BoundedHostRead(host=host, agents=(), ssh_info=None)
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=_reread_config(0.02))
    stop_event = threading.Event()
    thread = threading.Thread(target=poller.run, args=(stop_event,), daemon=True)
    thread.start()
    try:
        assert poll_until(lambda: provider.reread_count(host.host_id) >= 1)
        chat_agent = _make_agent(host)
        provider.reread_by_host_id[host.host_id] = BoundedHostRead(host=host, agents=(chat_agent,), ssh_info=None)

        assert poll_until(
            lambda: any(
                isinstance(event, AgentDiscoveryEvent) and event.agent == chat_agent
                for event in _read_events(temp_mngr_ctx)
            )
        )
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive()
    # The re-reads never stood in for a full poll: only the one full poll's snapshot was written.
    assert len(_read_snapshots(temp_mngr_ctx)) == 1


def test_run_does_not_reread_when_the_reread_interval_is_not_below_the_poll_interval(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    provider = _make_controllable_provider(temp_host_dir, temp_mngr_ctx, is_released=True)
    host = _make_host(provider.name, HostState.RUNNING)
    provider.result_agents_by_host = {host: []}
    provider.reread_by_host_id[host.host_id] = BoundedHostRead(host=host, agents=(), ssh_info=None)
    config = _tiny_timeout_config().model_copy_update(
        to_update(_tiny_timeout_config().field_ref().host_discovery_poll_interval_seconds, PositiveFloat(0.05))
    )
    poller = _ProviderDiscoveryPoller(provider_name=provider.name, mngr_ctx=temp_mngr_ctx, config=config)
    stop_event = threading.Event()
    thread = threading.Thread(target=poller.run, args=(stop_event,), daemon=True)
    thread.start()
    try:
        assert poll_until(lambda: len(_read_snapshots(temp_mngr_ctx)) >= 3)
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
    assert provider.reread_count(host.host_id) == 0


def test_compute_host_read_delta_reports_ssh_info_when_only_published_endpoints_moved(temp_host_dir: Path) -> None:
    """A re-picked published port re-emits the host's SSH info event, which carries the endpoints."""
    host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    ssh_info = _make_ssh_info(temp_host_dir, 22001)
    previous_endpoint = PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="127.0.0.1", host_port=1)
    moved_endpoint = PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="127.0.0.1", host_port=2)
    previous = BoundedHostRead(host=host, agents=(), ssh_info=ssh_info, published_endpoints=(previous_endpoint,))
    current = BoundedHostRead(host=host, agents=(), ssh_info=ssh_info, published_endpoints=(moved_endpoint,))

    delta = _compute_host_read_delta(previous, current)

    assert delta.ssh_info == ssh_info
    assert delta.published_endpoints == (moved_endpoint,)


def test_host_reads_from_result_attaches_published_endpoints_to_their_host(temp_host_dir: Path) -> None:
    published_host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    plain_host = _make_host(_CONTROLLABLE_PROVIDER_NAME, HostState.RUNNING)
    endpoint = PublishedEndpoint(guest_port=59000, protocol=PortProtocol.TCP, host="127.0.0.1", host_port=32768)
    result = BoundedProviderDiscoveryResult(
        hosts=(published_host, plain_host),
        agents=(),
        host_ssh_infos=((published_host.host_id, _make_ssh_info(temp_host_dir, 22001)),),
        host_published_endpoints=((published_host.host_id, (endpoint,)),),
    )

    reads = _host_reads_from_result(result)

    assert reads[published_host.host_id].published_endpoints == (endpoint,)
    assert reads[plain_host.host_id].published_endpoints == ()
