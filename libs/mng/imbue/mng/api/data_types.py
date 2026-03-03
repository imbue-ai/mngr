from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic import computed_field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import BuildCacheInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.interfaces.data_types import LogFileInfo
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.data_types import WorkDirInfo
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName


class CreateAgentResult(FrozenModel):
    """Result of creating an agent."""

    agent: AgentInterface = Field(description="The created agent")
    host: OnlineHostInterface = Field(description="The host running the agent")


class SourceLocation(FrozenModel):
    """Specifies where to get source data from.

    Can be a local path, an agent on a host, or a combination. At minimum,
    either path or agent_name must be specified.
    """

    path: Path | None = Field(
        default=None,
        description="Local or remote path to the source directory",
    )
    agent_id: AgentId | None = Field(
        default=None,
        description="Source agent ID (for cloning from an existing agent)",
    )
    agent_name: AgentName | None = Field(
        default=None,
        description="Source agent name (alternative to ID)",
    )
    host_id: HostId | None = Field(
        default=None,
        description="Host where the source agent/path resides",
    )
    host_name: HostName | None = Field(
        default=None,
        description="Host name (alternative to ID)",
    )

    @computed_field
    @property
    def is_from_agent(self) -> bool:
        """Returns True if this source is from an existing agent."""
        return self.agent_id is not None or self.agent_name is not None


class ConnectionOptions(FrozenModel):
    """Options for connecting to an agent after creation."""

    is_reconnect: bool = Field(
        default=True,
        description="Automatically reconnect if connection is dropped",
    )
    is_interactive: bool | None = Field(
        default=None,
        description="Enable interactive mode (None means auto-detect TTY)",
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
    attach_command: str | None = Field(
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


class GcResult(MutableModel):
    """Aggregated results of garbage collection across all resource types."""

    work_dirs_destroyed: list[WorkDirInfo] = Field(
        default_factory=list,
        description="Work directories that were destroyed",
    )
    machines_deleted: list[HostInfo] = Field(
        default_factory=list,
        description="Machines that were deleted (removing records of old destroyed hosts)",
    )
    machines_destroyed: list[HostInfo] = Field(
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
    errors: list[str] = Field(
        default_factory=list,
        description="Errors encountered during garbage collection",
    )


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
    errors: list[str] = Field(
        default_factory=list,
        description="Errors encountered during cleanup",
    )
