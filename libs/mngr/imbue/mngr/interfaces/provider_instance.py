from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence

from loguru import logger
from pydantic import Field
from pyinfra.api.host import Host as PyinfraHost

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundOnHostError
from imbue.mngr.errors import HostAuthenticationError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.volume import HostVolume
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.utils.name_generator import generate_host_name


def _compute_idle_seconds(
    user_activity: datetime | None,
    agent_activity: datetime | None,
    ssh_activity: datetime | None,
) -> float | None:
    """Compute idle seconds from the most recent activity time."""
    latest_activity: datetime | None = None
    for activity_time in (user_activity, agent_activity, ssh_activity):
        if activity_time is not None:
            if latest_activity is None or activity_time > latest_activity:
                latest_activity = activity_time
    if latest_activity is None:
        return None
    return (datetime.now(timezone.utc) - latest_activity).total_seconds()


def _build_host_details_from_host(
    host: HostInterface,
    host_ref: DiscoveredHost,
    is_authentication_failure: bool,
) -> tuple[HostDetails, datetime | None]:
    """Build HostDetails from a host object (online or offline).

    Returns the HostDetails and the SSH activity time (needed for agent idle calculation).
    """
    # Build SSH info if this is a remote host (only available for online hosts)
    ssh_info: SSHInfo | None = None

    is_locked: bool | None = None
    locked_time: datetime | None = None
    if isinstance(host, OnlineHostInterface):
        ssh_connection = host.get_ssh_connection_info()
        if ssh_connection is not None:
            user, hostname, port, key_path = ssh_connection
            ssh_info = SSHInfo(
                user=user,
                host=hostname,
                port=port,
                key_path=key_path,
                command=f"ssh -i {key_path} -p {port} {user}@{hostname}",
            )
        boot_time = host.get_boot_time()
        uptime_seconds = host.get_uptime_seconds()
        resource = host.get_provider_resources()
        is_locked = host.is_lock_held()
        # Only fetch locked_time when the lock is held to avoid a redundant
        # SSH stat command on remote hosts (is_lock_held already checked existence).
        locked_time = host.get_reported_lock_time() if is_locked else None
    else:
        boot_time = None
        uptime_seconds = None
        resource = None

    certified_data = host.get_certified_data()
    host_plugin_data = certified_data.plugin
    # Always use the certified host_name for consistency between online and offline hosts.
    # Online hosts would otherwise return the SSH hostname (e.g., "r438.modal.host") via
    # get_name(), while offline hosts return the friendly name from certified data.
    host_name = certified_data.host_name
    # SSH activity is tracked at the host level (host_dir/activity/ssh)
    ssh_activity = (
        host.get_reported_activity_time(ActivitySource.SSH) if isinstance(host, OnlineHostInterface) else None
    )
    host_details = HostDetails(
        id=host.id,
        name=host_name,
        provider_name=host_ref.provider_name,
        state=host.get_state() if not is_authentication_failure else HostState.UNAUTHENTICATED,
        image=certified_data.image,
        tags={**certified_data.user_tags},
        boot_time=boot_time,
        uptime_seconds=uptime_seconds,
        resource=resource,
        ssh=ssh_info,
        snapshots=host.get_snapshots(),
        is_locked=is_locked,
        locked_time=locked_time,
        plugin=host_plugin_data,
        ssh_activity_time=ssh_activity,
        failure_reason=certified_data.failure_reason,
    )
    return host_details, ssh_activity


def _build_agent_details_from_online_agent(
    agent: AgentInterface,
    host_details: HostDetails,
    host: OnlineHostInterface,
    ssh_activity: datetime | None,
    field_generators: Mapping[str, Mapping[str, Callable[[AgentInterface, OnlineHostInterface], Any]]],
) -> AgentDetails:
    """Build AgentDetails from a live agent on an online host."""
    # Get activity config from host
    activity_config = host.get_activity_config()

    # Activity times from file mtimes (per-agent)
    user_activity = agent.get_reported_activity_time(ActivitySource.USER)
    agent_activity = agent.get_reported_activity_time(ActivitySource.AGENT)

    # start_time from activity/start file mtime (not the status/start_time file)
    start_time = agent.get_reported_activity_time(ActivitySource.START)

    # runtime_seconds computed from start_time
    now = datetime.now(timezone.utc)
    runtime_seconds = (now - start_time).total_seconds() if start_time else None

    # idle_seconds: include host-level ssh_activity; 0.0 if no activity yet
    idle_seconds = _compute_idle_seconds(user_activity, agent_activity, ssh_activity) or 0.0

    # Compute plugin-specific fields from field generators
    plugin_data: dict[str, Any] = {}
    for plugin_name, generators in field_generators.items():
        plugin_fields: dict[str, Any] = {}
        for field_name, generator in generators.items():
            value = generator(agent, host)
            if value is not None:
                plugin_fields[field_name] = value
        if plugin_fields:
            plugin_data[plugin_name] = plugin_fields

    return AgentDetails(
        id=agent.id,
        name=agent.name,
        type=str(agent.agent_type),
        command=agent.get_command(),
        work_dir=agent.work_dir,
        initial_branch=agent.get_created_branch_name(),
        create_time=agent.create_time,
        start_on_boot=agent.get_is_start_on_boot(),
        state=agent.get_lifecycle_state(),
        url=agent.get_reported_url(),
        start_time=start_time,
        runtime_seconds=runtime_seconds,
        user_activity_time=user_activity,
        agent_activity_time=agent_activity,
        idle_seconds=idle_seconds,
        idle_mode=activity_config.idle_mode.value,
        idle_timeout_seconds=activity_config.idle_timeout_seconds,
        activity_sources=tuple(s.value for s in activity_config.activity_sources),
        labels=agent.get_labels(),
        host=host_details,
        plugin=plugin_data,
    )


def _build_agent_details_from_offline_ref(
    agent_ref: DiscoveredAgent,
    host_details: HostDetails,
) -> AgentDetails:
    """Build AgentDetails from a discovered agent reference when the host is offline."""
    create_time = agent_ref.create_time or datetime(1970, 1, 1, tzinfo=timezone.utc)
    return AgentDetails(
        id=agent_ref.agent_id,
        name=agent_ref.agent_name,
        type=str(agent_ref.agent_type) if agent_ref.agent_type else "unknown",
        command=agent_ref.command or CommandString(""),
        work_dir=agent_ref.work_dir or Path("/"),
        initial_branch=agent_ref.created_branch_name,
        create_time=create_time,
        start_on_boot=agent_ref.start_on_boot,
        state=AgentLifecycleState.STOPPED,
        url=None,
        start_time=None,
        runtime_seconds=None,
        user_activity_time=None,
        agent_activity_time=None,
        idle_seconds=None,
        idle_mode=None,
        labels=agent_ref.labels,
        host=host_details,
        plugin={},
    )


class ProviderInstanceInterface(MutableModel, ABC):
    """A ProviderInstance is a configured endpoint that creates and manages hosts.

    Each provider instance is created by a ProviderBackend.
    """

    name: ProviderInstanceName = Field(frozen=True, description="Name of this provider instance")
    host_dir: Path = Field(frozen=True, description="Base directory for mngr data on hosts managed by this instance")
    mngr_ctx: MngrContext = Field(frozen=True, repr=False, description="The mngr context")

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

    @abstractmethod
    def reset_caches(self) -> None:
        """Reset any internal caches held by this provider instance.

        Use this if you want to ensure that the provider fetches fresh data from the underlying infrastructure on the next operation."""
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
    def to_offline_host(self, host_id: HostId) -> HostInterface:
        """Return an offline representation of the given host for use when it is unreachable."""
        ...

    @abstractmethod
    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all hosts managed by this provider instance."""
        ...

    def discover_hosts_and_agents(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> dict[DiscoveredHost, list[DiscoveredAgent]]:
        """Load hosts from this provider and fetch agent references for each host.

        Returns a mapping from DiscoveredHost to the list of DiscoveredAgents on that host.
        Providers may override this to optimize data fetching (e.g. by reading all
        host and agent data in parallel from a shared volume instead of SSH-ing into
        each host individually).

        The default implementation calls discover_hosts() and then discover_agents()
        on each host in parallel.
        """
        logger.trace("Loading hosts from provider {}", self.name)
        host_refs = self.discover_hosts(cg=cg, include_destroyed=include_destroyed)
        logger.trace("Loaded {} host(s) from provider {}", len(host_refs), self.name)

        future_by_host_ref: dict[DiscoveredHost, Future[list[DiscoveredAgent]]] = {}
        with ConcurrencyGroupExecutor(parent_cg=cg, name=f"load_agents_{self.name}", max_workers=32) as executor:
            for host_ref in host_refs:
                future_by_host_ref[host_ref] = executor.submit(
                    self.get_host(host_ref.host_id).discover_agents,
                )

        return {host_ref: future.result() for host_ref, future in future_by_host_ref.items()}

    @abstractmethod
    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get CPU, memory, disk, and GPU resource information for a host."""
        ...

    def get_host_and_agent_details(
        self,
        host_ref: DiscoveredHost,
        agent_refs: Sequence[DiscoveredAgent],
        field_generators: Mapping[str, Mapping[str, Callable[[AgentInterface, OnlineHostInterface], Any]]]
        | None = None,
        # Called when an error occurs for a specific agent or the host itself.
        # If the callback raises, the error propagates (ABORT semantics).
        # If it returns, the errored item is skipped (CONTINUE).
        # When None, errors fall back to offline data instead.
        on_error: Callable[[DiscoveredAgent | DiscoveredHost, BaseException], None] | None = None,
    ) -> tuple[HostDetails, list[AgentDetails]]:
        """Build HostDetails and AgentDetails for a host for listing.

        The default implementation connects to the host to collect data field-by-field.
        Providers can override this to collect all needed data in a single
        operation (e.g., one SSH command) instead of making many individual calls.
        """
        is_authentication_failure = False
        try:
            host = self.get_host(host_ref.host_id)
            # this is inside the try block so that, if the host appears to be online but transitions to offline, we properly fall back to offline data
            host_details, ssh_activity = _build_host_details_from_host(host, host_ref, is_authentication_failure)
        except HostConnectionError as e:
            self.on_connection_error(host_ref.host_id)
            logger.debug("Host {} unreachable, falling back to offline data: {}", host_ref.host_id, e)
            host = self.to_offline_host(host_ref.host_id)
            is_authentication_failure = isinstance(e, HostAuthenticationError)
            host_details, ssh_activity = _build_host_details_from_host(host, host_ref, is_authentication_failure)

        # Get all agents on this host
        agents: list[AgentInterface] | None = None
        if isinstance(host, OnlineHostInterface):
            agents = host.get_agents()

        # Build AgentDetails for each agent on this host
        resolved_field_generators = field_generators or {}
        agent_details_list: list[AgentDetails] = []
        for agent_ref in agent_refs:
            try:
                agent_details: AgentDetails | None = None
                if agents is not None and isinstance(host, OnlineHostInterface):
                    # Find the agent in the list for running hosts
                    agent = next((a for a in agents if a.id == agent_ref.agent_id), None)
                    if agent is not None:
                        agent_details = _build_agent_details_from_online_agent(
                            agent, host_details, host, ssh_activity, resolved_field_generators
                        )
                    else:
                        # Agent was discovered but is no longer on the host
                        exception = AgentNotFoundOnHostError(agent_ref.agent_id, host_ref.host_id)
                        if on_error is not None:
                            on_error(agent_ref, exception)
                            continue
                        else:
                            logger.debug(
                                "Agent {} not found on host {}, using offline data",
                                agent_ref.agent_id,
                                host_ref.host_id,
                            )

                # If this host is offline, or if we failed to find the agent on the online host
                if agent_details is None:
                    agent_details = _build_agent_details_from_offline_ref(agent_ref, host_details)

                agent_details_list.append(agent_details)
            except MngrError as e:
                if on_error is not None:
                    on_error(agent_ref, e)
                    # callback didn't raise, skip this agent
                else:
                    logger.debug(
                        "Failed to build details for agent {} on host {}, using offline data: {}",
                        agent_ref.agent_id,
                        host_ref.host_id,
                        e,
                    )
                    agent_details_list.append(_build_agent_details_from_offline_ref(agent_ref, host_details))

        return host_details, agent_details_list

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

        Returns volumes with mngr- prefix in name or with mngr-managed tags.
        """
        ...

    @abstractmethod
    def delete_volume(self, volume_id: VolumeId) -> None:
        """Delete a volume.

        Raises MngrError if the volume can't be deleted. Implementations may
        silently succeed if the volume has already been deleted (idempotent).
        """
        ...

    def get_volume_for_host(self, host: HostInterface | HostId) -> HostVolume | None:
        """Get the host volume for a given host, if one exists.

        The host volume is a persistent volume that is mounted inside the
        host's sandbox and contains all data written to the host_dir. It is
        writable by untrusted code running in the sandbox.

        This is distinct from the provider's internal state volume, which is
        only accessed by mngr and contains trusted metadata.

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
