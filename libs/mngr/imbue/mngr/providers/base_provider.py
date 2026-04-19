from typing import Mapping
from typing import Sequence

from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName


class BaseProviderInstance(ProviderInstanceInterface):
    """
    Abstract base class for provider instances.

    Useful because it communicates that the concrete Host class (not HostInterface) is returned from these methods.
    """

    _host_by_id_cache: dict[HostId, HostInterface] = PrivateAttr(default_factory=dict)

    def _evict_cached_host(self, host_id: HostId, replacement: HostInterface | None = None) -> None:
        """Remove a Host from the cache, disconnecting it if it holds an SSH connection.

        If replacement is provided and is a different instance than the cached one, the old
        Host is disconnected before being evicted. If replacement is None, the entry is simply
        removed (with disconnect if applicable).
        """
        old_host = self._host_by_id_cache.pop(host_id, None)
        if old_host is not None and old_host is not replacement and isinstance(old_host, Host):
            old_host.disconnect()
        if replacement is not None:
            self._host_by_id_cache[host_id] = replacement

    def reset_caches(self) -> None:
        pass

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
        raise NotImplementedError("Offline hosts not supported for this provider")

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        raise NotImplementedError()

    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> HostInterface:
        raise NotImplementedError()

    def get_max_destroyed_host_persisted_seconds(self) -> float:
        # Check for a provider-level override first
        provider_config = self.mngr_ctx.config.providers.get(self.name)
        if provider_config is not None and provider_config.destroyed_host_persisted_seconds is not None:
            return provider_config.destroyed_host_persisted_seconds
        # Fall back to the global default
        return self.mngr_ctx.config.default_destroyed_host_persisted_seconds

    def get_min_online_host_age_seconds(self) -> float:
        # Check for a provider-level override first
        provider_config = self.mngr_ctx.config.providers.get(self.name)
        if provider_config is not None and provider_config.min_online_host_age_seconds is not None:
            return provider_config.min_online_host_age_seconds
        # Fall back to the global default
        return self.mngr_ctx.config.default_min_online_host_age_seconds
