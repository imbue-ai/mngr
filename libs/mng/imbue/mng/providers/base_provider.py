from typing import Mapping
from typing import Sequence

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.hosts.host import Host
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName


class BaseProviderInstance(ProviderInstanceInterface):
    """
    Abstract base class for provider instances.

    Useful because it communicates that the concrete Host class (not HostInterface) is returned from these methods.
    """

    def create_host(
        self,
        name: HostName,
        image: ImageReference | None = None,
        tags: Mapping[str, str] | None = None,
        build_args: Sequence[str] | None = None,
        start_args: Sequence[str] | None = None,
        lifecycle: HostLifecycleOptions | None = None,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        snapshot: SnapshotName | None = None,
    ) -> Host:
        raise NotImplementedError()

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        raise NotImplementedError()

    def get_host(
        self,
        host: HostId | HostName,
    ) -> HostInterface:
        raise NotImplementedError()

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        raise Exception("Offline hosts not supported for this provider")

    def list_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[HostInterface]:
        raise NotImplementedError()

    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> HostInterface:
        raise NotImplementedError()

    def get_max_destroyed_host_persisted_seconds(self) -> float:
        # Check for a provider-level override first
        provider_config = self.mng_ctx.config.providers.get(self.name)
        if provider_config is not None and provider_config.destroyed_host_persisted_seconds is not None:
            return provider_config.destroyed_host_persisted_seconds
        # Fall back to the global default
        return self.mng_ctx.config.default_destroyed_host_persisted_seconds
