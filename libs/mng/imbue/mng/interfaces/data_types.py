from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from functools import cached_property
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from typing import Final
from typing import Literal

from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic import computed_field
from pydantic import model_validator
from pydantic_core import core_schema
from pyinfra.api import Host as PyinfraHost

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.mng.errors import InvalidRelativePathError
from imbue.mng.errors import ParseSpecError
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostState
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId

# Canonical mapping from IdleMode to the activity sources it enables.
# hosts/common.py delegates to this mapping via get_activity_sources_for_idle_mode().
ACTIVITY_SOURCES_BY_IDLE_MODE: Final[dict[IdleMode, tuple[ActivitySource, ...]]] = {
    IdleMode.IO: (
        ActivitySource.USER,
        ActivitySource.AGENT,
        ActivitySource.SSH,
        ActivitySource.CREATE,
        ActivitySource.START,
        ActivitySource.BOOT,
    ),
    IdleMode.USER: (
        ActivitySource.USER,
        ActivitySource.SSH,
        ActivitySource.CREATE,
        ActivitySource.START,
        ActivitySource.BOOT,
    ),
    IdleMode.AGENT: (
        ActivitySource.AGENT,
        ActivitySource.SSH,
        ActivitySource.CREATE,
        ActivitySource.START,
        ActivitySource.BOOT,
    ),
    IdleMode.SSH: (
        ActivitySource.SSH,
        ActivitySource.CREATE,
        ActivitySource.START,
        ActivitySource.BOOT,
    ),
    IdleMode.CREATE: (ActivitySource.CREATE,),
    IdleMode.BOOT: (ActivitySource.BOOT,),
    IdleMode.START: (ActivitySource.START, ActivitySource.BOOT),
    IdleMode.RUN: (
        ActivitySource.CREATE,
        ActivitySource.START,
        ActivitySource.BOOT,
        ActivitySource.PROCESS,
    ),
    IdleMode.DISABLED: (),
}

# Reverse mapping: frozenset of activity sources -> IdleMode
IDLE_MODE_BY_ACTIVITY_SOURCES: Final[dict[frozenset[ActivitySource], IdleMode]] = {
    frozenset(sources): mode for mode, sources in ACTIVITY_SOURCES_BY_IDLE_MODE.items()
}


def get_idle_mode_for_activity_sources(activity_sources: tuple[ActivitySource, ...]) -> IdleMode:
    """Derive the IdleMode from a set of activity sources.

    Returns CUSTOM if the activity sources don't match any known IdleMode preset.
    """
    return IDLE_MODE_BY_ACTIVITY_SOURCES.get(frozenset(activity_sources), IdleMode.CUSTOM)


class PyinfraConnector:
    """Pydantic-serializable wrapper for pyinfra Host objects.

    Stores the actual pyinfra Host instance while providing serialization
    based on the host name and connector class name. Access the underlying
    pyinfra Host via the `host` property for all operations.
    """

    __slots__ = ("_host",)

    def __init__(self, host: "PyinfraHost") -> None:
        self._host = host

    @property
    def host(self) -> "PyinfraHost":
        """The underlying pyinfra Host instance."""
        return self._host

    @property
    def name(self) -> str:
        """The pyinfra host name."""
        return self._host.name

    @property
    def connector_cls_name(self) -> str:
        """The name of the connector class (e.g., 'LocalConnector', 'SSHConnector')."""
        return self._host.connector_cls.__name__

    def __repr__(self) -> str:
        return f"PyinfraConnector(name={self.name!r}, connector={self.connector_cls_name})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        """Define how Pydantic should serialize/validate this type."""
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                cls._serialize,
                info_arg=False,
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: core_schema.CoreSchema,
        handler: Any,
    ) -> dict[str, Any]:
        """Define the JSON schema for this type."""
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The pyinfra host name"},
                "connector_cls": {
                    "type": "string",
                    "description": "The connector class name",
                },
            },
            "required": ["name", "connector_cls"],
        }

    @classmethod
    def _validate(cls, value: Any) -> "PyinfraConnector":
        if isinstance(value, cls):
            return value
        # Allow constructing from a pyinfra Host directly
        if isinstance(value, PyinfraHost):
            return cls(value)
        raise ParseSpecError(f"Expected PyinfraConnector or pyinfra Host, got {type(value)}")

    def _serialize(self) -> dict[str, str]:
        return {
            "name": self.name,
            "connector_cls": self.connector_cls_name,
        }


class CommandResult(FrozenModel):
    """Result of executing a command on a host."""

    stdout: str = Field(description="Standard output from the command")
    stderr: str = Field(description="Standard error from the command")
    success: bool = Field(description="True if the command succeeded (had an expected exit code)")


class CpuResources(FrozenModel):
    """CPU resource information for a host."""

    count: int = Field(description="Number of CPUs allocated to the host")
    frequency_ghz: float | None = Field(
        default=None,
        description="CPU frequency in GHz (None if not reported by provider)",
    )


class GpuResources(FrozenModel):
    """GPU resource information for a host."""

    count: int = Field(default=0, description="Number of GPUs allocated to the host")
    model: str | None = Field(
        default=None,
        description="GPU model name (e.g., 'NVIDIA A100')",
    )
    memory_gb: float | None = Field(
        default=None,
        description="GPU memory in GB per GPU",
    )


class HostResources(FrozenModel):
    """Resource allocation for a host.

    These values are reported by the provider and represent what has been
    allocated to the host, not necessarily what is currently in use.
    """

    cpu: CpuResources = Field(description="CPU resources")
    memory_gb: float = Field(description="Allocated memory in GB")
    disk_gb: float | None = Field(
        default=None,
        description="Allocated disk space in GB (None if not reported)",
    )
    gpu: GpuResources | None = Field(
        default=None,
        description="GPU resources (None if no GPU allocated)",
    )


class ActivityConfig(FrozenModel):
    """Configuration for host activity detection and idle timeout."""

    idle_timeout_seconds: int = Field(description="Maximum idle time before stopping")
    activity_sources: tuple[ActivitySource, ...] = Field(
        default=ACTIVITY_SOURCES_BY_IDLE_MODE[IdleMode.IO],
        description="Activity sources that count toward keeping host active",
    )

    @computed_field
    @cached_property
    def idle_mode(self) -> IdleMode:
        """Derived from activity_sources."""
        return get_idle_mode_for_activity_sources(self.activity_sources)


class HostConfig(FrozenModel):
    pass


class SnapshotRecord(FrozenModel):
    """Snapshot metadata so that a host can be resumed"""

    id: str = Field(description="Image ID (in whatever format the provider uses)")
    name: str = Field(description="Human-readable name")
    created_at: str = Field(description="ISO format timestamp")


class CertifiedHostData(FrozenModel):
    """Certified data stored in the host's data.json file."""

    idle_timeout_seconds: int = Field(
        default=3600,
        description="Maximum idle time before stopping",
    )
    activity_sources: tuple[ActivitySource, ...] = Field(
        default=ACTIVITY_SOURCES_BY_IDLE_MODE[IdleMode.IO],
        description="Activity sources that count toward keeping host active",
    )

    created_at: datetime = Field(description="When this host data was first created (always UTC)")
    updated_at: datetime = Field(description="When this host data was last updated (always UTC)")

    @model_validator(mode="before")
    @classmethod
    def _handle_backwards_compatibility(cls, data: Any) -> Any:
        """Handle backward compatibility with old data.json files.

        Strips deprecated idle_mode field and provides defaults for
        created_at/updated_at when missing from old data.
        """
        if isinstance(data, dict):
            data.pop("idle_mode", None)
            now = datetime.now(timezone.utc)
            if "created_at" not in data:
                data["created_at"] = now - timedelta(weeks=1)
            if "updated_at" not in data:
                data["updated_at"] = now - timedelta(days=1)
        return data

    @computed_field
    @cached_property
    def idle_mode(self) -> IdleMode:
        """Derived from activity_sources."""
        return get_idle_mode_for_activity_sources(self.activity_sources)

    max_host_age: int | None = Field(
        default=None,
        description="Maximum host age in seconds from boot before shutdown (used by providers with hard timeouts)",
    )
    plugin: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Plugin-specific certified data indexed by plugin name",
    )
    image: str | None = Field(
        default=None,
        description="Image reference used to create the host",
    )
    generated_work_dirs: tuple[str, ...] = Field(
        default_factory=tuple,
        description="List of work directories that were generated by mng for agents on this host",
    )
    host_id: str = Field(description="Unique identifier for the host")
    host_name: str = Field(description="Human-readable name")
    user_tags: dict[str, str] = Field(default_factory=dict, description="User-defined tags")
    snapshots: list[SnapshotRecord] = Field(default_factory=list, description="List of snapshots")
    tmux_session_prefix: str | None = Field(
        default=None,
        description="Prefix for tmux session names on this host (e.g., 'mng-'). Used by the activity watcher to detect when no agents are running.",
    )
    stop_reason: str | None = Field(
        default=None,
        description="Reason for last shutdown: 'PAUSED' (idle), 'STOPPED' (user requested or all agents exited), or None (crashed)",
    )
    failure_reason: str | None = Field(
        default=None,
        description="Reason for failure if the host failed during creation",
    )
    build_log: str | None = Field(
        default=None,
        description="Build log output if the host failed during creation",
    )


class SnapshotInfo(FrozenModel):
    """Information about a snapshot."""

    id: SnapshotId = Field(description="Unique identifier for the snapshot")
    name: SnapshotName = Field(description="Human-readable name")
    created_at: datetime = Field(description="When the snapshot was created")
    size_bytes: int | None = Field(
        default=None,
        description="Size in bytes (None if provider doesn't report size)",
    )
    recency_idx: int = Field(
        default=0,
        description="Snapshot recency within host (0 = most recent, incrementing for older snapshots)",
    )


class VolumeFileType(UpperCaseStrEnum):
    """Type of entry in a volume listing."""

    FILE = auto()
    DIRECTORY = auto()


class VolumeFile(FrozenModel):
    """An entry listed from a volume directory."""

    path: str = Field(description="Path of the entry within the volume")
    file_type: VolumeFileType = Field(description="Whether this entry is a file or directory")
    mtime: int = Field(description="Last modification time as Unix timestamp")
    size: int = Field(description="Size in bytes")


class VolumeInfo(FrozenModel):
    """Information about a volume."""

    volume_id: VolumeId = Field(description="Unique identifier")
    name: str = Field(description="Human-readable name")
    size_bytes: int = Field(description="Size in bytes")
    created_at: datetime | None = Field(
        default=None, description="Creation timestamp (None if provider doesn't report it)"
    )
    host_id: HostId | None = Field(default=None, description="Associated host, if any")
    tags: dict[str, str] = Field(default_factory=dict, description="Provider tags")


class SizeBytes(NonNegativeInt):
    """Size in bytes. Must be >= 0."""


class WorkDirInfo(FrozenModel):
    """Information about a work directory to be cleaned."""

    path: Path = Field(description="Path to the work directory")
    size_bytes: SizeBytes = Field(default=SizeBytes(0), description="Size in bytes")
    host_id: HostId = Field(description="Host ID this work dir belongs to")
    provider_name: ProviderInstanceName = Field(description="Provider that owns the host")
    is_local: bool = Field(description="Whether this resource is on the local host")
    created_at: datetime = Field(description="When the work directory was created")


class LogFileInfo(FrozenModel):
    """Information about a log file to be cleaned."""

    path: Path = Field(description="Path to the log file")
    size_bytes: SizeBytes = Field(default=SizeBytes(0), description="Size in bytes")
    created_at: datetime = Field(description="When the log file was created")


class BuildCacheInfo(FrozenModel):
    """Information about a build cache entry to be cleaned."""

    path: Path = Field(description="Path to the build cache directory")
    size_bytes: SizeBytes = Field(default=SizeBytes(0), description="Size in bytes")
    created_at: datetime = Field(description="When the cache entry was created")


class SSHInfo(FrozenModel):
    """SSH connection information for a remote host."""

    user: str = Field(description="SSH username")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to SSH private key")
    command: str = Field(description="Full SSH command to connect")


class HostInfo(FrozenModel):
    """Information about a host/machine."""

    id: HostId = Field(description="Host ID")
    name: str = Field(description="Host name")
    provider_name: ProviderInstanceName = Field(description="Provider that owns the host")

    # Extended fields (all optional)
    state: HostState | None = Field(default=None, description="Current host state (RUNNING, STOPPED, etc.)")
    image: str | None = Field(default=None, description="Host image (Docker image name, Modal image ID, etc.)")
    tags: dict[str, str] = Field(default_factory=dict, description="Metadata tags for the host")
    boot_time: datetime | None = Field(default=None, description="When the host was last started")
    uptime_seconds: float | None = Field(default=None, description="How long the host has been running")
    resource: HostResources | None = Field(default=None, description="Resource limits for the host")
    ssh: SSHInfo | None = Field(default=None, description="SSH access details (remote hosts only)")
    snapshots: list[SnapshotInfo] = Field(default_factory=list, description="List of available snapshots")
    is_locked: bool | None = Field(
        default=None,
        description="Whether the host is currently locked for an operation",
    )
    locked_time: datetime | None = Field(default=None, description="When the host was locked")
    plugin: dict[str, Any] = Field(default_factory=dict, description="Plugin-defined fields")
    ssh_activity_time: datetime | None = Field(
        default=None,
        description="Last SSH activity time (from host-level activity/ssh file mtime)",
    )
    failure_reason: str | None = Field(
        default=None,
        description="Reason for failure if the host failed during creation",
    )


class AgentInfo(FrozenModel):
    """Complete information about an agent for listing purposes.

    This combines certified and reported data from the agent with host information.
    """

    resource_type: Literal["agent"] = "agent"
    id: AgentId = Field(description="Agent ID")
    name: AgentName = Field(description="Agent name")
    type: str = Field(description="Agent type (claude, codex, etc.)")
    command: CommandString = Field(description="Command used to start the agent")
    work_dir: Path = Field(description="Working directory")
    create_time: datetime = Field(description="Creation timestamp")
    start_on_boot: bool = Field(description="Whether agent starts on host boot")

    state: AgentLifecycleState = Field(description="Agent lifecycle state (STOPPED/RUNNING/WAITING/REPLACED/DONE)")
    url: str | None = Field(default=None, description="Agent URL (reported)")
    start_time: datetime | None = Field(default=None, description="Last start time (reported)")
    runtime_seconds: float | None = Field(default=None, description="Runtime in seconds")
    user_activity_time: datetime | None = Field(default=None, description="Last user activity (reported)")
    agent_activity_time: datetime | None = Field(default=None, description="Last agent activity (reported)")
    idle_seconds: float | None = Field(default=None, description="Idle time in seconds")
    idle_mode: str | None = Field(default=None, description="Idle detection mode")
    idle_timeout_seconds: int | None = Field(default=None, description="Idle timeout in seconds")
    activity_sources: tuple[str, ...] | None = Field(
        default=None, description="Activity sources used for idle detection"
    )

    labels: dict[str, str] = Field(default_factory=dict, description="Agent labels (key-value pairs)")

    host: HostInfo = Field(description="Host information")

    plugin: dict[str, Any] = Field(default_factory=dict, description="Plugin-specific fields")


class RelativePath(PurePosixPath):
    """A path that must be relative (not absolute).

    Inherits from PurePosixPath to provide full path manipulation capabilities.
    Uses POSIX path semantics since agent paths are always on remote Linux hosts.
    """

    def __new__(cls, *args: str | Path) -> "RelativePath":
        path_str = str(PurePosixPath(*args))
        if path_str.startswith("/"):
            raise InvalidRelativePathError(path_str)
        return super().__new__(cls, *args)

    @classmethod
    def _validate(cls, value: Any) -> "RelativePath":
        """Validate and convert input to RelativePath."""
        if isinstance(value, cls):
            return value
        if isinstance(value, (str, Path, PurePosixPath)):
            return cls(value)
        raise ParseSpecError(f"Expected str, Path, or RelativePath, got {type(value)}")

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.to_string_ser_schema(),
        )


class FileTransferSpec(FrozenModel):
    """Specification for a file transfer during agent provisioning.

    Used by plugins to declare files that should be copied from the local machine
    to the agent work_dir before other provisioning steps run.

    Note: Currently only supports individual files, not directories.
    """

    local_path: Path = Field(description="Path to the file on the local machine")
    agent_path: RelativePath = Field(
        description="Destination path on the agent host. Must be a relative path (relative to work_dir)"
    )
    is_required: bool = Field(
        description="If True, provisioning fails if local file doesn't exist. If False, skipped if missing."
    )


class HostLifecycleOptions(FrozenModel):
    """Lifecycle and idle detection options for the host.

    These options control when a host is considered idle and should be shut down.
    All fields are optional; when None, provider defaults are used.
    """

    idle_timeout_seconds: int | None = Field(
        default=None,
        description="Shutdown after idle for N seconds (None for provider default)",
    )
    idle_mode: IdleMode | None = Field(
        default=None,
        description="When to consider host idle (None for provider default)",
    )
    activity_sources: tuple[ActivitySource, ...] | None = Field(
        default=None,
        description="Activity sources for idle detection (None for provider default)",
    )

    def to_activity_config(
        self,
        default_idle_timeout_seconds: int,
        default_idle_mode: IdleMode,
        default_activity_sources: tuple[ActivitySource, ...],
    ) -> ActivityConfig:
        """Convert to ActivityConfig, using provided defaults for None values.

        When activity_sources is not explicitly provided, it is derived from the
        resolved idle_mode using ACTIVITY_SOURCES_BY_IDLE_MODE. This ensures
        that specifying --idle-mode boot results in only BOOT activity being monitored,
        without needing to also explicitly specify --activity-sources boot.
        """
        resolved_idle_mode = self.idle_mode if self.idle_mode is not None else default_idle_mode

        if self.activity_sources is not None:
            resolved_activity_sources = self.activity_sources
        else:
            resolved_activity_sources = ACTIVITY_SOURCES_BY_IDLE_MODE[resolved_idle_mode]

        return ActivityConfig(
            idle_timeout_seconds=self.idle_timeout_seconds
            if self.idle_timeout_seconds is not None
            else default_idle_timeout_seconds,
            activity_sources=resolved_activity_sources,
        )
