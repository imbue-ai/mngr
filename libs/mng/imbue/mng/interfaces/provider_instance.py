from abc import ABC
from abc import abstractmethod
from concurrent.futures import Future
from pathlib import Path
from typing import Mapping
from typing import Sequence

from loguru import logger
from pydantic import Field
from pyinfra.api.host import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.host import HostInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.volume import HostVolume
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostNameStyle
from imbue.mng.primitives import HostReference
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId
from imbue.mng.utils.name_generator import generate_host_name


class ProviderInstanceInterface(MutableModel, ABC):
    """A ProviderInstance is a configured endpoint that creates and manages hosts.

    Each provider instance is created by a ProviderBackend.
    """

    name: ProviderInstanceName = Field(frozen=True, description="Name of this provider instance")
    host_dir: Path = Field(frozen=True, description="Base directory for mng data on hosts managed by this instance")
    mng_ctx: MngContext = Field(frozen=True, repr=False, description="The mng context")

    # =========================================================================
    # Capability Properties
    # =========================================================================

    def get_host_name(self, style: HostNameStyle) -> HostName:
        """Generate a name for a new host.

        The default implementation auto-generates a name using the given style.
        Providers that only support a fixed host name (e.g. "localhost" for the
        local provider) should override this to return that name.
        """
        return generate_host_name(style)

    @property
    @abstractmethod
    def supports_snapshots(self) -> bool:
        """Whether this provider supports creating and managing host snapshots."""
        ...

    @property
    @abstractmethod
    def supports_shutdown_hosts(self) -> bool:
        """Whether this provider supports directly resuming hosts (*not* from a snapshot)."""
        ...

    @property
    @abstractmethod
    def supports_volumes(self) -> bool:
        """Whether this provider supports volume management."""
        ...

    @property
    @abstractmethod
    def supports_mutable_tags(self) -> bool:
        """Whether this provider supports modifying tags after host creation.

        Some providers (like Docker) store tags as immutable labels that cannot be
        changed after container creation. Others (like local) store tags in mutable
        files that can be updated at any time.
        """
        ...

    # =========================================================================
    # Core Lifecycle Methods
    # =========================================================================

    @abstractmethod
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
    ) -> OnlineHostInterface:
        """Create and start a new host with the given name and configuration.

        If snapshot is provided, the host is created from the snapshot image
        instead of building a new one.
        """
        ...

    @abstractmethod
    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop a running host, optionally creating a snapshot before stopping."""
        ...

    @abstractmethod
    def start_host(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId | None = None,
    ) -> OnlineHostInterface:
        """Start a stopped host, optionally restoring from a specific snapshot."""
        ...

    @abstractmethod
    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Permanently destroy a host and delete its snapshots."""
        ...

    @abstractmethod
    def delete_host(self, host: HostInterface) -> None:
        """Permanently delete all records associated with a (destroyed) host."""
        ...

    @abstractmethod
    def on_connection_error(self, host_id: HostId) -> None:
        """Handle actions to take when a connection error occurs with a host."""
        ...

    @abstractmethod
    def get_max_destroyed_host_persisted_seconds(self) -> float:
        """
        Returns the number of seconds that a host is allowed to be in DESTROYED state.
        After this all associated data will be permanently deleted.
        """
        ...

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    @abstractmethod
    def get_host(
        self,
        host: HostId | HostName,
    ) -> HostInterface:
        """Retrieve a host by its ID or name, raising HostNotFoundError if not found."""
        ...

    @abstractmethod
    def to_offline_host(self, host_id: HostId) -> HostInterface: ...

    @abstractmethod
    def list_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[HostInterface]:
        """List all hosts managed by this provider instance."""
        ...

    def load_agent_refs(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[HostReference, list[AgentReference]]:
        """Load hosts from this provider and fetch agent references for each host.

        Returns a mapping from HostReference to the list of AgentReferences on that host.
        Providers may override this to optimize data fetching (e.g. by reading all
        host and agent data in parallel from a shared volume instead of SSH-ing into
        each host individually).

        The default implementation calls list_hosts() and then get_agent_references()
        on each host in parallel.
        """
        logger.trace("Loading hosts from provider {}", self.name)
        hosts = self.list_hosts(cg=cg, include_destroyed=include_destroyed)
        logger.trace("Loaded {} host(s) from provider {}", len(hosts), self.name)

        future_by_host_ref: dict[HostReference, Future[list[AgentReference]]] = {}
        with ConcurrencyGroupExecutor(parent_cg=cg, name=f"load_agents_{self.name}", max_workers=32) as executor:
            for host in hosts:
                host_ref = HostReference(
                    host_id=host.id,
                    host_name=host.get_name(),
                    provider_name=self.name,
                )
                future_by_host_ref[host_ref] = executor.submit(host.get_agent_references)

        return {host_ref: future.result() for host_ref, future in future_by_host_ref.items()}

    @abstractmethod
    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get CPU, memory, disk, and GPU resource information for a host."""
        ...

    def build_host_listing_data(
        self,
        host_ref: HostReference,
        agent_refs: Sequence[AgentReference],
    ) -> tuple[HostInfo, list[AgentInfo]] | None:
        """Build HostInfo and AgentInfo for a host in an optimized way for listing.

        Providers can override this to collect all needed data in a single
        operation (e.g., one SSH command) instead of making many individual calls.
        Returns None to fall back to the default per-field collection.
        """
        return None

    # =========================================================================
    # Snapshot Methods
    # =========================================================================

    @abstractmethod
    def create_snapshot(
        self,
        host: HostInterface | HostId,
        name: SnapshotName | None = None,
    ) -> SnapshotId:
        """Create a snapshot of the host's current state and return its ID."""
        ...

    @abstractmethod
    def list_snapshots(
        self,
        host: HostInterface | HostId,
    ) -> list[SnapshotInfo]:
        """List all snapshots associated with a host."""
        ...

    @abstractmethod
    def delete_snapshot(
        self,
        host: HostInterface | HostId,
        snapshot_id: SnapshotId,
    ) -> None:
        """Delete a snapshot by its ID."""
        ...

    # =========================================================================
    # Volume Methods
    # =========================================================================

    @abstractmethod
    def list_volumes(self) -> list[VolumeInfo]:
        """List all volumes managed by this provider.

        Returns volumes with mng- prefix in name or with mng-managed tags.
        """
        ...

    @abstractmethod
    def delete_volume(self, volume_id: VolumeId) -> None:
        """Delete a volume.

        Raises MngError if the volume can't be deleted. Implementations may
        silently succeed if the volume has already been deleted (idempotent).
        """
        ...

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Get the host volume for a given host, if one exists.

        The host volume is a persistent volume that is mounted inside the
        host's sandbox and contains all data written to the host_dir. It is
        writable by untrusted code running in the sandbox.

        This is distinct from the provider's internal state volume, which is
        only accessed by mng and contains trusted metadata.

        Returns None if the provider does not support host volumes or if
        no volume exists for the given host.
        """
        return None

    # =========================================================================
    # Host Mutation Methods
    # =========================================================================

    @abstractmethod
    def get_host_tags(
        self,
        host: HostInterface | HostId,
    ) -> dict[str, str]:
        """Get all tags associated with a host as a key-value mapping."""
        ...

    @abstractmethod
    def set_host_tags(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        """Replace all tags on a host with the provided tags."""
        ...

    @abstractmethod
    def add_tags_to_host(
        self,
        host: HostInterface | HostId,
        tags: Mapping[str, str],
    ) -> None:
        """Add or update tags on a host without removing existing tags."""
        ...

    @abstractmethod
    def remove_tags_from_host(
        self,
        host: HostInterface | HostId,
        keys: Sequence[str],
    ) -> None:
        """Remove tags from a host by their keys."""
        ...

    @abstractmethod
    def rename_host(
        self,
        host: HostInterface | HostId,
        name: HostName,
    ) -> HostInterface:
        """Rename a host and return the updated host object."""
        ...

    # =========================================================================
    # Connector Method
    # =========================================================================

    @abstractmethod
    def get_connector(
        self,
        host: HostInterface | HostId,
    ) -> "PyinfraHost":
        """Get the pyinfra connector for executing operations on a host."""
        ...

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    def close(self) -> None:
        """Clean up resources held by this provider instance.

        Providers that hold long-lived resources (like Modal app contexts) should
        override this method to release them. This method is called during shutdown
        via atexit handlers.

        The default implementation does nothing.
        """

    def list_persisted_agent_data_for_host(self, host_id: HostId) -> list[dict]:
        """List persisted agent data for a stopped host.

        Some providers (like Modal) persist agent state when hosts are stopped,
        allowing agent information to be retrieved even when the host is not running.

        Each dict in the returned list should contain at minimum an 'id' field with
        the agent ID. Returns an empty list if no persisted data exists or the
        provider doesn't support this feature.
        """
        return []

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        """Persist agent data to external storage.

        Called when an agent is created or its data.json is updated. Providers
        that support persistent agent state (like Modal) should override this
        to write the agent data to their storage backend.

        The default implementation is a no-op for providers that don't need this.
        """

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        """Remove persisted agent data from external storage.

        Called when an agent is destroyed. Providers that support persistent
        agent state (like Modal) should override this to remove the agent data
        from their storage backend.

        The default implementation is a no-op for providers that don't need this.
        """
