from __future__ import annotations

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import BuildCacheInfo
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import LogFileInfo
from imbue.mngr.interfaces.data_types import ProviderResourceInfo
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.data_types import WorkDirInfo
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredHost


class CreateAgentResult(FrozenModel):
    """Result of creating an agent."""

    agent: AgentInterface = Field(description="The created agent")
    host: OnlineHostInterface = Field(description="The host running the agent")


class ConnectionOptions(FrozenModel):
    """Options for connecting to an agent after creation."""

    is_reconnect: bool = Field(
        default=True,
        description="Automatically reconnect if connection is dropped",
    )
    message: str | None = Field(
        default=None,
        description="Message to send after connecting to agent",
    )
    retry_count: int = Field(
        default=3,
        description="Number of connection retries",
    )
    retry_delay: str = Field(
        default="5s",
        description="Delay between retries (e.g., 5s, 1m)",
    )
    session_command: str | None = Field(
        default=None,
        description="Command to run instead of attaching to main session",
    )
    is_unknown_host_allowed: bool = Field(
        default=False,
        description="Whether to allow connecting to hosts with unknown SSH keys",
    )


class GcResourceTypes(FrozenModel):
    """Specifies which resource types to garbage collect."""

    is_machines: bool = Field(default=False, description="Clean idle machines with no agents")
    is_snapshots: bool = Field(default=False, description="Clean orphaned snapshots")
    is_volumes: bool = Field(default=False, description="Clean orphaned volumes")
    is_work_dirs: bool = Field(default=False, description="Clean orphaned work directories")
    is_logs: bool = Field(default=False, description="Clean old log files")
    is_build_cache: bool = Field(default=False, description="Clean build cache entries")
    is_provider_resources: bool = Field(
        default=False, description="Clean orphaned provider-level cloud resources (e.g. Azure NICs/public IPs)"
    )


class GcResult(MutableModel):
    """Aggregated results of garbage collection across all resource types."""

    work_dirs_destroyed: list[WorkDirInfo] = Field(
        default_factory=list,
        description="Work directories that were destroyed",
    )
    source_dirs_destroyed: list[WorkDirInfo] = Field(
        default_factory=list,
        description="Source repositories (e.g. mngr-managed git clones) that were destroyed",
    )
    source_dirs_kept_due_to_unpushed_branches: list[WorkDirInfo] = Field(
        default_factory=list,
        description=(
            "Source repositories that were left in place because they still have local branches "
            "not present on any remote. Delete the branches (or push them) to allow future GC."
        ),
    )
    machines_deleted: list[DiscoveredHost] = Field(
        default_factory=list,
        description="Machines that were deleted (removing records of old destroyed hosts)",
    )
    machines_destroyed: list[DiscoveredHost] = Field(
        default_factory=list,
        description="Machines that were destroyed",
    )
    snapshots_destroyed: list[SnapshotInfo] = Field(
        default_factory=list,
        description="Snapshots that were destroyed",
    )
    volumes_destroyed: list[VolumeInfo] = Field(
        default_factory=list,
        description="Volumes that were destroyed",
    )
    logs_destroyed: list[LogFileInfo] = Field(
        default_factory=list,
        description="Log files that were destroyed",
    )
    build_cache_destroyed: list[BuildCacheInfo] = Field(
        default_factory=list,
        description="Build cache entries that were destroyed",
    )
    provider_resources_destroyed: list[ProviderResourceInfo] = Field(
        default_factory=list,
        description="Orphaned provider-level cloud resources (e.g. Azure NICs/public IPs) that were reclaimed",
    )
    failures: list[CleanupFailure] = Field(
        default_factory=list,
        description="Real failures (resources left behind / not collected) encountered during garbage collection",
    )

    @property
    def errors(self) -> list[str]:
        """Formatted failure messages, for human output and legacy string consumers."""
        return [f"[{failure.category}] {failure.message}" for failure in self.failures]


class CleanupResult(MutableModel):
    """Result of a cleanup operation."""

    destroyed_agents: list[AgentName] = Field(
        default_factory=list,
        description="Names of agents that were destroyed",
    )
    stopped_agents: list[AgentName] = Field(
        default_factory=list,
        description="Names of agents that were stopped",
    )
    failures: list[CleanupFailure] = Field(
        default_factory=list,
        description="Real failures (resources left behind) encountered during cleanup",
    )

    @property
    def errors(self) -> list[str]:
        """Formatted failure messages, for human output and legacy string consumers."""
        return [f"[{failure.category}] {failure.message}" for failure in self.failures]
