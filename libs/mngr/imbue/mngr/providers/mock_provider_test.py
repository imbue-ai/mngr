from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Mapping
from typing import Sequence

from pydantic import Field
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.local.instance import LocalProviderInstance


class MockProviderInstance(BaseProviderInstance):
    """In-memory provider instance for OfflineHost unit tests.

    Provides configurable return values for provider methods that OfflineHost
    delegates to, without using mocks.
    """

    mock_supports_snapshots: bool = Field(default=True)
    mock_supports_shutdown_hosts: bool = Field(default=True)
    mock_supports_volumes: bool = Field(default=False)
    mock_snapshots: list[SnapshotInfo] = Field(default_factory=list)
    mock_volumes: list[VolumeInfo] = Field(default_factory=list)
    mock_tags: dict[str, str] = Field(default_factory=dict)
    mock_agent_data: list[dict[str, Any]] = Field(default_factory=list)
    mock_hosts: list[HostInterface] = Field(default_factory=list)
    mock_offline_hosts: dict[str, HostInterface] = Field(default_factory=dict)
    mock_discovered_hosts: list[DiscoveredHost] = Field(default_factory=list)
    stopped_hosts: list[HostId] = Field(default_factory=list)
    deleted_hosts: list[HostId] = Field(default_factory=list)
    destroyed_hosts: list[HostId] = Field(default_factory=list)
    deleted_snapshots: list[tuple[HostId, SnapshotId]] = Field(default_factory=list)
    deleted_volumes: list[VolumeId] = Field(default_factory=list)
    connection_errors_cleared: list[HostId] = Field(default_factory=list)

    @property
    def supports_snapshots(self) -> bool:
        return self.mock_supports_snapshots

    @property
    def supports_shutdown_hosts(self) -> bool:
        return self.mock_supports_shutdown_hosts

    @property
    def supports_volumes(self) -> bool:
        return self.mock_supports_volumes

    @property
    def supports_mutable_tags(self) -> bool:
        return True

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        return self.mock_snapshots

    def get_host_tags(self, host: HostInterface | HostId) -> dict[str, str]:
        return self.mock_tags

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        return self.mock_agent_data

    def get_host(self, host: HostId | HostName) -> HostInterface:
        for h in self.mock_hosts:
            if h.id == host or h.get_name() == host:
                return h
        raise HostNotFoundError(self.name, host)

    def stop_host(
        self, host: HostInterface | HostId, create_snapshot: bool = True, timeout_seconds: float = 60.0
    ) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        self.stopped_hosts.append(host_id)

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        if self.mock_discovered_hosts:
            return list(self.mock_discovered_hosts)
        return [
            DiscoveredHost(
                host_id=h.id,
                host_name=h.get_name(),
                provider_name=self.name,
                host_state=h.get_state(),
            )
            for h in self.mock_hosts
        ]

    def destroy_host(self, host: HostInterface | HostId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        self.destroyed_hosts.append(host_id)

    def delete_host(self, host: HostInterface) -> None:
        self.deleted_hosts.append(host.id)

    def on_connection_error(self, host_id: HostId) -> None:
        self.connection_errors_cleared.append(host_id)

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        offline = self.mock_offline_hosts.get(str(host_id))
        if offline is not None and isinstance(offline, OfflineHost):
            return offline
        raise HostNotFoundError(self.name, host_id)

    def get_host_resources(self, host: HostInterface) -> HostResources:
        raise NotImplementedError()

    def create_snapshot(self, host: HostInterface | HostId, name: SnapshotName | None = None) -> SnapshotId:
        raise NotImplementedError()

    def delete_snapshot(self, host: HostInterface | HostId, snapshot_id: SnapshotId) -> None:
        host_id = host.id if isinstance(host, HostInterface) else host
        self.deleted_snapshots.append((host_id, snapshot_id))

    def list_volumes(self) -> list[VolumeInfo]:
        return self.mock_volumes

    def delete_volume(self, volume_id: VolumeId) -> None:
        self.deleted_volumes.append(volume_id)

    def set_host_tags(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        self.mock_tags = dict(tags)

    def add_tags_to_host(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        self.mock_tags.update(tags)

    def remove_tags_from_host(self, host: HostInterface | HostId, keys: Sequence[str]) -> None:
        for k in keys:
            self.mock_tags.pop(k, None)

    def get_connector(self, host: HostInterface | HostId) -> PyinfraHost:
        raise NotImplementedError()


def make_offline_host(
    certified_data: CertifiedHostData,
    provider: MockProviderInstance,
    mngr_ctx: MngrContext,
) -> OfflineHost:
    host_id = HostId(certified_data.host_id)
    return OfflineHost(
        id=host_id,
        certified_host_data=certified_data,
        provider_instance=provider,
        mngr_ctx=mngr_ctx,
    )


class ConfigurableOnlineHost(Host):
    """A real ``Host`` whose online-discovery behavior is fully overridable for tests.

    Built through a provider's ``_create_local_pyinfra_host()`` so it is a genuine
    ``OnlineHostInterface`` running against the local connector, but every method
    the GC / cleanup paths call during discovery can be told to either return a
    fixed value or raise a chosen exception. This replaces the family of one-off
    ``Host`` subclasses (offline/auth-erroring, remote, activity-error,
    get-state-error) that each overrode a single method.

    Knobs:
    - ``mock_is_local`` controls ``is_local`` (set False to look like a remote host).
    - ``discover_agents_error`` raises from ``discover_agents`` when set; otherwise
      ``discover_agents`` returns ``[]``.
    - ``certified_data_error`` / ``mock_certified_data`` control ``get_certified_data``.
    - ``state_error`` / ``mock_state`` control ``get_state``.
    - ``activity_time_error`` raises from ``get_reported_activity_time`` when set;
      otherwise ``mock_last_activity_time`` is reported for the BOOT source (so GC
      sees it as the most recent activity) and None for all others.

    Where an ``*_error`` field is None and the corresponding value field is also
    None, the method falls back to the real ``Host`` implementation.
    """

    mock_is_local: bool = Field(default=False)
    mock_certified_data: CertifiedHostData | None = Field(default=None)
    mock_state: HostState | None = Field(default=None)
    mock_last_activity_time: datetime | None = Field(default=None)
    # Exception instances are stored as ``Any`` because MutableModel sets
    # arbitrary_types_allowed=False, so a bare ``Exception`` annotation would
    # fail pydantic schema generation. None means "do not raise from this method".
    discover_agents_error: Any = Field(default=None)
    certified_data_error: Any = Field(default=None)
    state_error: Any = Field(default=None)
    activity_time_error: Any = Field(default=None)

    @property
    def is_local(self) -> bool:
        return self.mock_is_local

    def discover_agents(self) -> list[DiscoveredAgent]:
        if self.discover_agents_error is not None:
            raise self.discover_agents_error
        return []

    def get_certified_data(self) -> CertifiedHostData:
        if self.certified_data_error is not None:
            raise self.certified_data_error
        if self.mock_certified_data is not None:
            return self.mock_certified_data
        return super().get_certified_data()

    def get_state(self) -> HostState:
        if self.state_error is not None:
            raise self.state_error
        if self.mock_state is not None:
            return self.mock_state
        return super().get_state()

    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        if self.activity_time_error is not None:
            raise self.activity_time_error
        if activity_type == ActivitySource.BOOT and self.mock_last_activity_time is not None:
            return self.mock_last_activity_time
        return None


def make_configurable_online_host(
    provider: LocalProviderInstance,
    *,
    is_local: bool = False,
    host_name: str = "configurable-test-host",
    last_activity_seconds_ago: float | None = None,
    created_seconds_ago: float = 0,
    mock_state: HostState | None = None,
    discover_agents_error: Exception | None = None,
    certified_data_error: Exception | None = None,
    state_error: Exception | None = None,
    activity_time_error: Exception | None = None,
) -> ConfigurableOnlineHost:
    """Build a ``ConfigurableOnlineHost`` against ``provider``'s local connector.

    ``last_activity_seconds_ago`` and ``created_seconds_ago`` are translated into a
    ``CertifiedHostData`` whose ``created_at`` is that many seconds in the past and
    whose BOOT activity time is reported via ``mock_last_activity_time``. Pass
    ``last_activity_seconds_ago=None`` to simulate a host with no recorded activity
    at all. When any ``*_error`` is given, the matching value field is ignored
    because the method raises instead.
    """
    pyinfra_host = provider._create_local_pyinfra_host()
    connector = PyinfraConnector(pyinfra_host)
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    last_activity_time = (
        None if last_activity_seconds_ago is None else now - timedelta(seconds=last_activity_seconds_ago)
    )
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name=host_name,
        created_at=now - timedelta(seconds=created_seconds_ago),
        updated_at=now,
    )
    return ConfigurableOnlineHost(
        id=host_id,
        host_name=HostName(host_name),
        connector=connector,
        provider_instance=provider,
        mngr_ctx=provider.mngr_ctx,
        mock_is_local=is_local,
        mock_certified_data=certified_data,
        mock_state=mock_state,
        mock_last_activity_time=last_activity_time,
        discover_agents_error=discover_agents_error,
        certified_data_error=certified_data_error,
        state_error=state_error,
        activity_time_error=activity_time_error,
    )


class RecordingDestroyProvider(LocalProviderInstance):
    """A ``LocalProviderInstance`` that records ``destroy_host`` calls instead of performing them.

    The real ``LocalProviderInstance.destroy_host`` raises
    ``LocalHostNotDestroyableError``; this records the destroyed host id into
    ``destroyed_host_ids`` and returns successfully so tests can assert which
    hosts a code path attempted to destroy.
    """

    destroyed_host_ids: list[HostId] = Field(default_factory=list)

    def destroy_host(self, host: HostInterface | HostId) -> None:
        self.destroyed_host_ids.append(host if isinstance(host, HostId) else host.id)


def make_recording_destroy_provider(provider: LocalProviderInstance) -> RecordingDestroyProvider:
    """Build a ``RecordingDestroyProvider`` copying ``provider``'s name / host_dir / ctx."""
    return RecordingDestroyProvider(
        name=provider.name,
        host_dir=provider.host_dir,
        mngr_ctx=provider.mngr_ctx,
    )


class OfflineHostProvider(LocalProviderInstance):
    """A ``LocalProviderInstance`` whose ``get_host`` returns an ``OfflineHost``.

    ``destroy_host`` is inherited from ``LocalProviderInstance`` (raises
    ``LocalHostNotDestroyableError``); subclass and override it to exercise the
    success path. ``get_host_call_count`` records how many times ``get_host`` was
    invoked so tests can assert the injected provider (rather than a real one
    resolved by a mismatched cache key) was actually consulted.

    ``get_host`` has no return annotation because it returns ``OfflineHost``, which
    satisfies ``HostInterface`` but is not a subclass of ``Host`` (the parent's
    declared return type); annotating it would produce a type error.
    """

    get_host_call_count: int = Field(default=0)

    def get_host(self, host: HostId | HostName):
        self.get_host_call_count += 1
        host_id = host if isinstance(host, HostId) else HostId.generate()
        now = datetime.now(timezone.utc)
        certified_data = CertifiedHostData(
            created_at=now,
            updated_at=now,
            host_id=str(host_id),
            host_name="test-offline-host",
        )
        return OfflineHost(
            id=host_id,
            certified_host_data=certified_data,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )


class OfflineHostDestroyableProvider(OfflineHostProvider):
    """An ``OfflineHostProvider`` whose ``destroy_host`` succeeds (no-op).

    Used to drive the success path in destroy flows for offline hosts.
    """

    def destroy_host(self, host: HostInterface | HostId) -> None:
        pass


class StopFailingHost(Host):
    """A ``Host`` whose ``stop_agents`` always raises ``MngrError``.

    Used to exercise stop-error handling without requiring a real tmux session.
    """

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        raise MngrError("Simulated stop error")


class StopFailingProvider(LocalProviderInstance):
    """A ``LocalProviderInstance`` whose ``get_host`` returns a ``StopFailingHost``.

    ``get_host_call_count`` records invocations so tests can assert the injected
    provider was actually consulted.
    """

    get_host_call_count: int = Field(default=0)

    def get_host(self, host: HostId | HostName) -> StopFailingHost:
        self.get_host_call_count += 1
        pyinfra_host = self._create_local_pyinfra_host()
        connector = PyinfraConnector(pyinfra_host)
        return StopFailingHost(
            id=self.host_id,
            host_name=HostName("test"),
            connector=connector,
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )
