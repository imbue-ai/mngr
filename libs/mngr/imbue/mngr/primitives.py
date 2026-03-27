import re
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import Mapping
from typing import Self

from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.primitives import NonEmptyStr

# === Enums ===


class AgentNameStyle(UpperCaseStrEnum):
    """Style for auto-generated agent names."""

    COOLNAME = auto()
    ENGLISH = auto()
    FANTASY = auto()
    SCIFI = auto()
    PAINTERS = auto()
    AUTHORS = auto()
    ARTISTS = auto()
    MUSICIANS = auto()
    ANIMALS = auto()
    SCIENTISTS = auto()
    DEMONS = auto()


class HostNameStyle(UpperCaseStrEnum):
    """Style for auto-generated host names."""

    COOLNAME = auto()
    ASTRONOMY = auto()
    PLACES = auto()
    CITIES = auto()
    FANTASY = auto()
    SCIFI = auto()
    PAINTERS = auto()
    AUTHORS = auto()
    ARTISTS = auto()
    MUSICIANS = auto()
    SCIENTISTS = auto()


class LogLevel(UpperCaseStrEnum):
    """Log verbosity level."""

    TRACE = auto()
    DEBUG = auto()
    BUILD = auto()
    INFO = auto()
    WARN = auto()
    ERROR = auto()
    NONE = auto()


class IdleMode(UpperCaseStrEnum):
    """Mode for determining when host is considered idle."""

    IO = auto()
    USER = auto()
    AGENT = auto()
    SSH = auto()
    CREATE = auto()
    BOOT = auto()
    START = auto()
    RUN = auto()
    CUSTOM = auto()
    DISABLED = auto()


class ActivitySource(UpperCaseStrEnum):
    """Sources of activity for idle detection."""

    CREATE = auto()
    BOOT = auto()
    START = auto()
    SSH = auto()
    PROCESS = auto()
    AGENT = auto()
    USER = auto()


class BootstrapMode(UpperCaseStrEnum):
    """Bootstrap behavior for missing tools."""

    SILENT = auto()
    WARN = auto()
    FAIL = auto()


class LifecycleHook(UpperCaseStrEnum):
    """Available lifecycle hooks."""

    INITIALIZE = auto()
    ON_CREATE = auto()
    UPDATE_CONTENT = auto()
    POST_CREATE = auto()
    POST_START = auto()
    POST_ATTACH = auto()


class OutputFormat(UpperCaseStrEnum):
    """Output format mode."""

    HUMAN = auto()
    JSON = auto()
    JSONL = auto()


class ErrorBehavior(UpperCaseStrEnum):
    """Behavior when encountering errors during operations."""

    ABORT = auto()
    CONTINUE = auto()


class CleanupAction(UpperCaseStrEnum):
    """Action to perform on selected agents during cleanup."""

    DESTROY = auto()
    STOP = auto()


class TransferMode(UpperCaseStrEnum):
    """How to transfer the project into the agent's work directory.

    NONE: Run in-place, no transfer.
    RSYNC: Transfer files via rsync (non-git projects only).
    GIT_MIRROR: Transfer via git push --mirror (git projects, works locally and remotely).
    GIT_WORKTREE: Create a git worktree (git projects, local agents only).
    """

    NONE = auto()
    RSYNC = auto()
    GIT_MIRROR = auto()
    GIT_WORKTREE = auto()


class UncommittedChangesMode(UpperCaseStrEnum):
    """Mode for handling uncommitted changes in the destination during sync operations."""

    STASH = auto()
    CLOBBER = auto()
    MERGE = auto()
    FAIL = auto()


class SyncMode(UpperCaseStrEnum):
    """Direction of sync operation.

    PUSH: local -> agent
    PULL: agent -> local
    """

    PUSH = auto()
    PULL = auto()


class SyncDirection(UpperCaseStrEnum):
    """Direction for file synchronization in pair mode."""

    FORWARD = auto()
    REVERSE = auto()
    BOTH = auto()


class ConflictMode(UpperCaseStrEnum):
    """Conflict resolution mode for pair mode sync."""

    NEWER = auto()
    SOURCE = auto()
    TARGET = auto()
    ASK = auto()


# === ID Types ===


class HostState(UpperCaseStrEnum):
    """The lifecycle state of a host."""

    BUILDING = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    PAUSED = auto()
    CRASHED = auto()
    FAILED = auto()
    DESTROYED = auto()
    UNAUTHENTICATED = auto()


class AgentLifecycleState(UpperCaseStrEnum):
    """The lifecycle state of an agent."""

    STOPPED = auto()
    RUNNING = auto()
    WAITING = auto()
    REPLACED = auto()
    DONE = auto()


class AgentId(RandomId):
    """Unique identifier for an agent."""

    PREFIX = "agent"


class HostId(RandomId):
    """Unique identifier for a host."""

    PREFIX = "host"


class SnapshotId(NonEmptyStr):
    """Unique identifier for a snapshot."""


class VolumeId(RandomId):
    """Unique identifier for a volume."""

    PREFIX = "vol"


class InvalidName(ValueError):
    pass


_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


class SafeName(NonEmptyStr):
    """Base type for human-readable names used in filesystem paths and shell commands.

    Must be alphanumeric with dashes and underscores allowed in the middle, must not start
    or end with a dash. This is enforced because these names appear in
    filesystem paths, tmux session names, and other contexts where special
    characters like ``/`` would break things.
    """

    def __new__(cls, value: str) -> Self:
        value = value.strip()
        if not _SAFE_NAME_RE.match(value):
            raise InvalidName(
                f"{cls.__name__} must be alphanumeric (with dashes and underscores allowed in the middle): '{value}'"
            )
        return super().__new__(cls, value)


class ProviderInstanceName(SafeName):
    """Name of a provider instance."""


LOCAL_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("local")

DEFAULT_BRANCH_PREFIX: Final[str] = "mngr/"


def default_branch_name(agent_name: "AgentName", prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    """Build the default branch name for an agent."""
    return f"{prefix}{agent_name}"


class ProviderBackendName(SafeName):
    """Name of a provider backend."""


class AgentName(SafeName):
    """Human-readable name for an agent."""


class HostName(NonEmptyStr):
    """Human-readable name for a host.

    Supports the format ``host_name.provider_name``. Not validated with
    SafeName because host names can be IP addresses or other formats.
    """

    @property
    def provider_name(self) -> ProviderInstanceName | None:
        """Extract the provider name if specified as 'host_name.provider_name'."""
        parts = self.split(".")
        if len(parts) == 2:
            return ProviderInstanceName(parts[1])
        return None

    @property
    def short_name(self) -> str:
        """Get the short host name without the provider suffix."""
        parts = self.split(".")
        return parts[0]


class AgentTypeName(SafeName):
    """Type name for an agent (e.g., claude, codex)."""


class UserId(NonEmptyStr):
    """Unique user identifier for namespacing provider resources."""


class PluginName(NonEmptyStr):
    """Name of a plugin."""


class Permission(NonEmptyStr):
    """Permission identifier for agent access control."""


class ImageReference(NonEmptyStr):
    """Reference to a container or VM image."""


class CommandString(NonEmptyStr):
    """Command string to be executed."""


class SnapshotName(str):
    """Human-readable name for a snapshot."""

    def __new__(cls, value: str) -> Self:
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )


class CertifiedDataError(Exception):
    """Raised when certified_data contains an unexpected type for a field."""


class SSHInfo(FrozenModel):
    """SSH connection information for a remote host."""

    user: str = Field(description="SSH username")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to SSH private key")
    command: str = Field(description="Full SSH command to connect")


class DiscoveredHost(FrozenModel):
    """Lightweight host data collected during discovery (without connecting to the host)."""

    host_id: HostId = Field(description="Unique identifier for the host")
    host_name: HostName = Field(description="Human-readable name of the host")
    provider_name: ProviderInstanceName = Field(description="Name of the provider instance that owns the host")


class DiscoveredAgent(FrozenModel):
    """Lightweight agent data collected during discovery (without connecting to the host).

    This class provides access to agent data that can be retrieved without requiring
    the host to be online. The certified_data field contains the raw data.json contents,
    and property methods provide convenient typed access to common fields.
    """

    host_id: HostId
    agent_id: AgentId
    agent_name: AgentName
    provider_name: ProviderInstanceName
    certified_data: Mapping[str, Any] = Field(default_factory=dict)

    @property
    def agent_type(self) -> "AgentTypeName | None":
        """Return the agent type, or None if not available."""
        type_value = self.certified_data.get("type")
        if type_value is not None:
            return AgentTypeName(type_value)
        return None

    @property
    def work_dir(self) -> Path | None:
        """Return the agent's working directory, or None if not available."""
        work_dir_value = self.certified_data.get("work_dir")
        if work_dir_value is not None:
            return Path(work_dir_value)
        return None

    @property
    def command(self) -> "CommandString | None":
        """Return the command used to start this agent, or None if not available."""
        command_value = self.certified_data.get("command")
        if command_value is not None:
            return CommandString(command_value)
        return None

    @property
    def create_time(self) -> datetime | None:
        """Return the agent creation time, or None if not available."""
        create_time_value = self.certified_data.get("create_time")
        if create_time_value is not None:
            if isinstance(create_time_value, datetime):
                return create_time_value
            # Handle ISO format string
            return datetime.fromisoformat(create_time_value)
        return None

    @property
    def start_on_boot(self) -> bool:
        """Return whether this agent should start automatically on host boot."""
        return bool(self.certified_data.get("start_on_boot", False))

    @property
    def permissions(self) -> tuple["Permission", ...]:
        """Return the list of permissions assigned to this agent."""
        permissions_value = self.certified_data.get("permissions", [])
        return tuple(Permission(p) for p in permissions_value)

    @property
    def created_branch_name(self) -> str | None:
        """Return the git branch name that was created for this agent, or None if not set."""
        match self.certified_data.get("created_branch_name"):
            case str(value):
                return value
            case None:
                return None
            case unexpected:
                raise CertifiedDataError(f"Expected str or None for created_branch_name, got {type(unexpected)}")

    @property
    def labels(self) -> dict[str, str]:
        """Return the labels attached to this agent."""
        return dict(self.certified_data.get("labels", {}))
