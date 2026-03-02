from __future__ import annotations

import uuid
from typing import Any
from typing import Final
from typing import Mapping
from typing import Sequence

from pydantic import Field
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import SnapshotsNotSupportedError
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.data_types import CpuResources
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId
from imbue.mng.providers.base_provider import BaseProviderInstance
from imbue.mng.providers.ssh.config import SSHHostConfig

# Fixed UUID namespace for generating deterministic host IDs from names
_SSH_PROVIDER_NAMESPACE: Final[uuid.UUID] = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


class SSHProviderInstance(BaseProviderInstance):
    """Provider instance for managing SSH hosts.

    Connects to pre-configured hosts via SSH. Hosts are statically defined
    in the configuration - this provider does not create or destroy hosts,
    it simply provides access to the configured hosts.

    Tags and snapshots are not supported.
    """

    hosts: dict[str, SSHHostConfig] = Field(
        frozen=True,
        description="Map of host name to SSH configuration",
    )

    @property
    def supports_snapshots(self) -> bool:
        return False

    @property
    def supports_shutdown_hosts(self) -> bool:
        return True

    @property
    def supports_volumes(self) -> bool:
        return False

    @property
    def supports_mutable_tags(self) -> bool:
        return False

    def _host_id_for_name(self, host_name: str) -> HostId:
        """Generate a deterministic host ID from the host name.

        Uses UUID5 with a fixed namespace to ensure the same host name
        always produces the same ID.
        """
        # HostId expects format "host-" followed by 32 hex characters (UUID without dashes)
        uuid_hex = uuid.uuid5(_SSH_PROVIDER_NAMESPACE, f"{self.name}:{host_name}").hex
        return HostId(f"host-{uuid_hex}")

    def _create_pyinfra_host(self, host_config: SSHHostConfig) -> PyinfraHost:
        """Create a pyinfra host with SSH connector."""
        host_data: dict[str, Any] = {
            "ssh_user": host_config.user,
            "ssh_port": host_config.port,
        }
        if host_config.key_file is not None:
            host_data["ssh_key"] = str(host_config.key_file)

        names_data = ([(host_config.address, host_data)], {})
        inventory = Inventory(names_data)
        state = PyinfraState(inventory=inventory)

        pyinfra_host = inventory.get_host(host_config.address)
        pyinfra_host.init(state)

        return pyinfra_host

    def _create_host_object(
        self,
        host_name: str,
        host_config: SSHHostConfig,
    ) -> Host:
        """Create a Host object for the given configuration."""
        host_id = self._host_id_for_name(host_name)
        pyinfra_host = self._create_pyinfra_host(host_config)
        connector = PyinfraConnector(pyinfra_host)

        return Host(
            id=host_id,
            connector=connector,
            provider_instance=self,
            mng_ctx=self.mng_ctx,
        )

    # =========================================================================
    # Core Lifecycle Methods (not supported for SSH provider)
    # =========================================================================

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
        raise NotImplementedError("SSH provider does not support creating hosts")

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        raise NotImplementedError("SSH provider does not support stopping hosts")

    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> Host:
        raise NotImplementedError("SSH provider does not support starting hosts")

    def destroy_host(self, host: HostInterface | HostId) -> None:
        raise NotImplementedError("SSH provider does not support destroying hosts")

    def delete_host(self, host: HostInterface) -> None:
        raise NotImplementedError("SSH provider does not support destroying hosts")

    def on_connection_error(self, host_id: HostId) -> None:
        pass

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    def get_host(
        self,
        host: HostId | HostName,
    ) -> Host:
        """Get a host by ID or name."""
        if isinstance(host, HostId):
            # Search for a host with matching ID
            for host_name, host_config in self.hosts.items():
                if self._host_id_for_name(host_name) == host:
                    return self._create_host_object(host_name, host_config)
            raise HostNotFoundError(host)

        # Search by name
        name_str = str(host)
        if name_str not in self.hosts:
            raise HostNotFoundError(host)

        return self._create_host_object(name_str, self.hosts[name_str])

    def list_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[HostInterface]:
        """List all configured hosts."""
        hosts: list[HostInterface] = []
        for host_name, host_config in self.hosts.items():
            host = self._create_host_object(host_name, host_config)
            hosts.append(host)
        return hosts

    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get resource information for a host."""
        # SSH provider doesn't track resources - return defaults
        return HostResources(
            cpu=CpuResources(count=1, frequency_ghz=None),
            memory_gb=1.0,
            disk_gb=None,
            gpu=None,
        )

    # =========================================================================
    # Snapshot Methods (not supported)
    # =========================================================================

    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        raise SnapshotsNotSupportedError(self.name)

    def list_snapshots(
        self,
        host: HostInterface | HostId,
    ) -> list[SnapshotInfo]:
        return []

    def delete_snapshot(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId,
    ) -> None:
        raise SnapshotsNotSupportedError(self.name)

    # =========================================================================
    # Volume Methods (not supported)
    # =========================================================================

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        raise NotImplementedError("SSH provider does not support volumes")

    # =========================================================================
    # Tag Methods (not supported for SSH provider)
    # =========================================================================

    def get_host_tags(
        self,
        host: HostInterface | HostId,
    ) -> dict[str, str]:
        """SSH provider does not support tags - returns empty dict."""
        return {}

    def set_host_tags(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        raise NotImplementedError("SSH provider does not support mutable tags")

    def add_tags_to_host(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        raise NotImplementedError("SSH provider does not support mutable tags")

    def remove_tags_from_host(
        self,
        host: HostInterface | HostId,
        keys: Sequence[str],
    ) -> None:
        raise NotImplementedError("SSH provider does not support mutable tags")

    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> Host:
        raise NotImplementedError("SSH provider does not support renaming hosts")

    # =========================================================================
    # Connector Method
    # =========================================================================

    def get_connector(
        self,
        host: HostInterface | HostId,
    ) -> PyinfraHost:
        """Get a pyinfra connector for the host."""
        host_id = host.id if isinstance(host, HostInterface) else host

        # Search for a host with matching ID
        for host_name, host_config in self.hosts.items():
            if self._host_id_for_name(host_name) == host_id:
                return self._create_pyinfra_host(host_config)

        raise HostNotFoundError(host_id)

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    def close(self) -> None:
        """Clean up resources (no-op for SSH provider)."""
        pass
