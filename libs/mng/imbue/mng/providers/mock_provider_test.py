from typing import Any
from typing import Mapping
from typing import Sequence

from pydantic import Field
from pyinfra.api import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.config.data_types import MngContext
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.primitives import HostId
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId
from imbue.mng.providers.base_provider import BaseProviderInstance


class MockProviderInstance(BaseProviderInstance):
    """In-memory provider instance for OfflineHost unit tests.

    Provides configurable return values for provider methods that OfflineHost
    delegates to, without using mocks.
    """

    mock_supports_snapshots: bool = Field(default=True)
    mock_supports_shutdown_hosts: bool = Field(default=True)
    mock_snapshots: list[SnapshotInfo] = Field(default_factory=list)
    mock_tags: dict[str, str] = Field(default_factory=dict)
    mock_agent_data: list[dict[str, Any]] = Field(default_factory=list)
    mock_hosts: list[HostInterface] = Field(default_factory=list)
    deleted_hosts: list[HostId] = Field(default_factory=list)

    @property
    def supports_snapshots(self) -> bool:
        return self.mock_supports_snapshots

    @property
    def supports_shutdown_hosts(self) -> bool:
        return self.mock_supports_shutdown_hosts

    @property
    def supports_volumes(self) -> bool:
        return False

    @property
    def supports_mutable_tags(self) -> bool:
        return True

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        return self.mock_snapshots

    def get_host_tags(self, host: HostInterface | HostId) -> dict[str, str]:
        return self.mock_tags

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        return self.mock_agent_data

    def stop_host(
        self, host: HostInterface | HostId, create_snapshot: bool = True, timeout_seconds: float = 60.0
    ) -> None:
        raise NotImplementedError()

    def list_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[HostInterface]:
        return list(self.mock_hosts)

    def destroy_host(self, host: HostInterface | HostId) -> None:
        raise NotImplementedError()

    def delete_host(self, host: HostInterface) -> None:
        self.deleted_hosts.append(host.id)

    def on_connection_error(self, host_id: HostId) -> None:
        pass

    def get_host_resources(self, host: HostInterface) -> HostResources:
        raise NotImplementedError()

    def create_snapshot(self, host: HostInterface | HostId, name: SnapshotName | None = None) -> SnapshotId:
        raise NotImplementedError()

    def delete_snapshot(self, host: HostInterface | HostId, snapshot_id: SnapshotId) -> None:
        raise NotImplementedError()

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        raise NotImplementedError()

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
    mng_ctx: MngContext,
) -> OfflineHost:
    host_id = HostId(certified_data.host_id)
    return OfflineHost(
        id=host_id,
        certified_host_data=certified_data,
        provider_instance=provider,
        mng_ctx=mng_ctx,
    )
