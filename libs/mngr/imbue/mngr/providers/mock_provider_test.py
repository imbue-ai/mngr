from typing import Any
from typing import Mapping
from typing import Sequence

from pydantic import Field
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance


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
    deleted_hosts: list[HostId] = Field(default_factory=list)
    deleted_snapshots: list[tuple[HostId, SnapshotId]] = Field(default_factory=list)
    deleted_volumes: list[VolumeId] = Field(default_factory=list)

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
        raise HostNotFoundError(host)

    def stop_host(
        self, host: HostInterface | HostId, create_snapshot: bool = True, timeout_seconds: float = 60.0
    ) -> None:
        raise NotImplementedError()

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
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
        raise NotImplementedError()

    def delete_host(self, host: HostInterface) -> None:
        self.deleted_hosts.append(host.id)

    def on_connection_error(self, host_id: HostId) -> None:
        pass

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        offline = self.mock_offline_hosts.get(str(host_id))
        if offline is not None and isinstance(offline, OfflineHost):
            return offline
        raise HostNotFoundError(host_id)

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
