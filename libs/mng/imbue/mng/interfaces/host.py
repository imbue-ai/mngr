from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final
from typing import Iterator
from typing import Mapping
from typing import Sequence

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.config.data_types import EnvVar
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import ParseSpecError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import ActivityConfig
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import CommandResult
from imbue.mng.interfaces.data_types import HostLifecycleOptions
from imbue.mng.interfaces.data_types import HostResources
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostNameStyle
from imbue.mng.primitives import HostState
from imbue.mng.primitives import Permission
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import WorkDirCopyMode

# Default timeout for waiting for agent readiness before sending messages.
# With hook-based polling, we return early when the agent signals readiness,
# so this is a max wait time, not an unconditional delay.
DEFAULT_AGENT_READY_TIMEOUT_SECONDS: Final[float] = 10.0


class HostInterface(MutableModel, ABC):
    """Interface for host implementations."""

    id: HostId = Field(frozen=True, description="Unique identifier for this host")

    @property
    @abstractmethod
    def is_local(self) -> bool:
        """Return True if this host is the local machine, False for remote hosts."""
        ...

    @property
    @abstractmethod
    def host_dir(self) -> Path:
        """Get the host state directory path."""
        ...

    @abstractmethod
    def get_name(self) -> HostName:
        """Return the human-readable name of this host."""
        ...

    # =========================================================================
    # Activity Configuration
    # =========================================================================

    @abstractmethod
    def get_activity_config(self) -> ActivityConfig:
        """Return the activity configuration for idle detection on this host."""
        ...

    @abstractmethod
    def set_activity_config(self, config: ActivityConfig) -> None:
        """Update the activity configuration for idle detection on this host."""
        ...

    # =========================================================================
    # Certified Data
    # =========================================================================

    @abstractmethod
    def get_certified_data(self) -> CertifiedHostData:
        """Return all certified (trustworthy) host data stored in data.json."""
        ...

    @abstractmethod
    def set_certified_data(self, data: CertifiedHostData) -> None:
        """Save certified data to data.json and notify the provider."""
        ...

    @abstractmethod
    def get_plugin_data(self, plugin_name: str) -> dict[str, Any]:
        """Return the certified plugin data for the given plugin name."""
        ...

    # =========================================================================
    # Provider-Derived Information
    # =========================================================================

    @abstractmethod
    def get_seconds_since_stopped(self) -> float | None:
        """Return the number of seconds since this host was stopped (or None if it is running)."""
        ...

    @abstractmethod
    def get_stop_time(self) -> datetime | None:
        """Return the host last stop time as a datetime, or None if unknown."""
        ...

    @abstractmethod
    def get_snapshots(self) -> list[SnapshotInfo]:
        """Return a list of all snapshots available for this host."""
        ...

    @abstractmethod
    def get_image(self) -> str | None:
        """Return the base image used for this host, or None if not applicable."""
        ...

    @abstractmethod
    def get_tags(self) -> dict[str, str]:
        """Return all metadata tags associated with this host."""
        ...

    # =========================================================================
    # Agent Information
    # =========================================================================

    @abstractmethod
    def get_agent_references(self) -> list[AgentReference]:
        """Return a list of all agent references for this host."""
        ...

    # =========================================================================
    # Agent-Derived Information
    # =========================================================================

    @abstractmethod
    def get_permissions(self) -> list[str]:
        """Return the union of all permissions granted to agents on this host."""
        ...

    @abstractmethod
    def get_state(self) -> HostState:
        """Return the current lifecycle state of this host."""
        ...

    @abstractmethod
    def get_failure_reason(self) -> str | None:
        """Return the failure reason if this host failed during creation, or None."""
        ...

    @abstractmethod
    def get_build_log(self) -> str | None:
        """Return the build log if this host failed during creation, or None."""
        ...


class OnlineHostInterface(HostInterface, ABC):
    """Interface for hosts that are currently online and accessible for operations."""

    connector: PyinfraConnector = Field(frozen=True, description="Pyinfra connector for host operations")

    # =========================================================================
    # Core Primitives
    # =========================================================================

    @abstractmethod
    def execute_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a shell command on this host and return the result."""
        ...

    @abstractmethod
    def read_file(
        self,
        path: Path,
    ) -> bytes:
        """Read a file and return its contents as bytes."""
        ...

    @abstractmethod
    def write_file(
        self,
        path: Path,
        content: bytes,
        mode: str | None = None,
    ) -> None:
        """Write bytes content to a file."""
        ...

    @abstractmethod
    def read_text_file(
        self,
        path: Path,
        encoding: str = "utf-8",
    ) -> str:
        """Read a file and return its contents as a string."""
        ...

    @abstractmethod
    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        """Write string content to a file."""
        ...

    @abstractmethod
    def get_file_mtime(self, path: Path) -> datetime | None:
        """Return the modification time of a file, or None if the file doesn't exist."""
        ...

    # =========================================================================
    # Activity Times (aggregated across all agents on this host)
    # =========================================================================

    @abstractmethod
    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        """
        Return the last reported activity time for the given activity type, or None if unknown.

        For offline hosts, we can look at the time at which the host data file was written
        """
        ...

    @abstractmethod
    def record_activity(self, activity_type: ActivitySource) -> None:
        """Record activity of the given type; only BOOT and CREATE are valid here."""
        ...

    @abstractmethod
    def get_reported_activity_content(self, activity_type: ActivitySource) -> str | None:
        """Return the content associated with the last activity of the given type, or None."""
        ...

    # =========================================================================
    # Cooperative Locking
    # =========================================================================

    @abstractmethod
    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float = 30.0) -> Iterator[None]:
        """Context manager for acquiring and releasing the host lock."""
        ...

    @abstractmethod
    def get_reported_lock_time(self) -> datetime | None:
        """Return the last modification time of the host lock file, or None if not locked."""
        ...

    @abstractmethod
    def is_lock_held(self) -> bool:
        """Check whether the host lock is currently held."""
        ...

    # =========================================================================
    # Certified Data
    # =========================================================================

    @abstractmethod
    def set_plugin_data(self, plugin_name: str, data: dict[str, Any]) -> None:
        """Update the certified plugin data for the given plugin name."""
        ...

    @abstractmethod
    def to_offline_host(self) -> HostInterface: ...

    # =========================================================================
    # Agent-Derived Information
    # =========================================================================

    @abstractmethod
    def get_idle_seconds(self) -> float:
        """Return the number of seconds since the host was last considered active."""
        ...

    # =========================================================================
    # Reported Plugin Data
    # =========================================================================

    @abstractmethod
    def get_reported_plugin_state_file_data(self, plugin_name: str, filename: str) -> str:
        """Return the content of a reported plugin state file."""
        ...

    @abstractmethod
    def set_reported_plugin_state_file_data(
        self,
        plugin_name: str,
        filename: str,
        data: str,
    ) -> None:
        """Write content to a reported plugin state file."""
        ...

    @abstractmethod
    def get_reported_plugin_state_files(self, plugin_name: str) -> list[str]:
        """Return a list of all reported state file names for the given plugin."""
        ...

    # =========================================================================
    # Environment
    # =========================================================================

    @abstractmethod
    def get_env_vars(self) -> dict[str, str]:
        """Return all environment variables configured for this host."""
        ...

    @abstractmethod
    def set_env_vars(self, env: Mapping[str, str]) -> None:
        """Replace all environment variables with the given mapping."""
        ...

    @abstractmethod
    def get_env_var(self, key: str) -> str | None:
        """Return the value of an environment variable, or None if not set."""
        ...

    @abstractmethod
    def set_env_var(self, key: str, value: str) -> None:
        """Set a single environment variable to the given value."""
        ...

    # =========================================================================
    # Provider-Derived Information
    # =========================================================================

    @abstractmethod
    def _get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        """Get SSH connection info for this host if it's remote.

        Returns (user, hostname, port, private_key_path) if remote, None if local.
        """
        ...

    @abstractmethod
    def get_boot_time(self) -> datetime | None:
        """Get the host boot time as a datetime.

        Returns the actual boot time from the OS, not computed from uptime,
        to avoid timing inconsistencies.
        """
        ...

    @abstractmethod
    def get_uptime_seconds(self) -> float:
        """Return the number of seconds since this host was last started."""
        ...

    @abstractmethod
    def get_provider_resources(self) -> HostResources:
        """Return the resource allocation (CPU, memory, disk) for this host."""
        ...

    @abstractmethod
    def set_tags(self, tags: Mapping[str, str]) -> None:
        """Replace all metadata tags with the given mapping."""
        ...

    @abstractmethod
    def add_tags(self, tags: Mapping[str, str]) -> None:
        """Add or update metadata tags from the given mapping."""
        ...

    @abstractmethod
    def remove_tags(self, keys: Sequence[str]) -> None:
        """Remove tags by key."""
        ...

    # =========================================================================
    # Agent Information
    # =========================================================================

    @abstractmethod
    def get_agent_env_path(self, agent: AgentInterface) -> Path:
        """Get the path to the agent's environment file."""
        ...

    @abstractmethod
    def get_agents(self) -> list[AgentInterface]:
        """Return a list of all agents running on this host."""
        ...

    @abstractmethod
    def create_agent_work_dir(
        self,
        host: OnlineHostInterface,
        path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        """Create and populate the work directory for a new agent."""
        ...

    @abstractmethod
    def create_agent_state(
        self,
        work_dir_path: Path,
        options: CreateAgentOptions,
        created_branch_name: str | None = None,
    ) -> AgentInterface:
        """Create the state directory and metadata for a new agent."""
        ...

    @abstractmethod
    def provision_agent(
        self,
        agent: AgentInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Install packages, create config files, and set up an agent."""
        ...

    @abstractmethod
    def rename_agent(self, agent: AgentInterface, new_name: AgentName) -> AgentInterface:
        """Rename an agent and return the updated agent object."""
        ...

    @abstractmethod
    def destroy_agent(self, agent: AgentInterface) -> None:
        """Remove an agent and all its associated state from this host."""
        ...

    @abstractmethod
    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        """Start the specified agents by creating their tmux sessions and processes."""
        ...

    @abstractmethod
    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        """Stop the specified agents gracefully within the given timeout."""
        ...

    @abstractmethod
    def save_agent_data(self, agent_id: AgentId, agent_data: Mapping[str, object]) -> None:
        """Persist agent data to external storage.

        Called when an agent's data.json is updated. Providers that support
        persistent agent state (like Modal) will sync this to their storage.
        """
        ...


class CreateWorkDirResult(FrozenModel):
    """Result of creating an agent work directory."""

    path: Path = Field(description="Path to the created work directory")
    created_branch_name: str | None = Field(
        default=None,
        description="Name of the git branch created for this work directory, if any",
    )


class AgentGitOptions(FrozenModel):
    """Git-related options for the agent work_dir."""

    is_git_synced: bool = Field(
        default=True,
        description="Whether to sync git data from the source repository",
    )
    copy_mode: WorkDirCopyMode = Field(
        default=WorkDirCopyMode.COPY,
        description="How to set up the work_dir: copy, clone, or worktree",
    )
    base_branch: str | None = Field(
        default=None,
        description="Starting branch for the agent (default: current branch)",
    )
    is_new_branch: bool = Field(
        default=False,
        description="Whether to create a new branch",
    )
    new_branch_name: str | None = Field(
        default=None,
        description="Name for the new branch (implies is_new_branch)",
    )
    new_branch_prefix: str = Field(
        default="mng/",
        description="Prefix for auto-generated branch names",
    )
    depth: int | None = Field(
        default=None,
        description="Shallow clone depth (None for full clone)",
    )
    shallow_since: str | None = Field(
        default=None,
        description="Shallow clone since date",
    )
    is_include_unclean: bool = Field(
        # the default is true because we should not assume that git is even being used
        default=True,
        description="Whether to include uncommitted files",
    )
    is_include_gitignored: bool = Field(
        default=False,
        description="Whether to include files matching .gitignore",
    )


class AgentEnvironmentOptions(FrozenModel):
    """Environment variable configuration for the agent."""

    env_vars: tuple[EnvVar, ...] = Field(
        default=(),
        description="Environment variables to set (KEY=VALUE)",
    )
    env_files: tuple[Path, ...] = Field(
        default=(),
        description="Files to load environment variables from",
    )


class AgentLifecycleOptions(FrozenModel):
    """Lifecycle options for the agent.

    Note: Host-level idle detection options (idle_timeout_seconds, idle_mode,
    activity_sources) are configured via HostLifecycleOptions in interfaces/data_types.py,
    not here. This class only contains agent-level lifecycle options.
    """

    is_start_on_boot: bool | None = Field(
        default=None,
        description="Whether to restart agent on host boot",
    )


class AgentLabelOptions(FrozenModel):
    """Label options for the agent."""

    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Key-value labels to attach to the agent",
    )


class AgentPermissionsOptions(FrozenModel):
    """Permissions options for the agent."""

    granted_permissions: tuple[Permission, ...] = Field(
        default=(),
        description="Permissions to grant to the agent",
    )


class UploadFileSpec(FrozenModel):
    """Specification for uploading a file: LOCAL:REMOTE."""

    local_path: Path = Field(description="Local path to the file to upload")
    remote_path: Path = Field(description="Remote path where the file should be placed")

    @classmethod
    def from_string(cls, s: str) -> "UploadFileSpec":
        """Parse a LOCAL:REMOTE string into an UploadFileSpec."""
        if ":" not in s:
            raise ParseSpecError(f"Upload file must be in LOCAL:REMOTE format, got: {s}")
        local, remote = s.split(":", 1)
        return cls(local_path=Path(local.strip()), remote_path=Path(remote.strip()))


class FileModificationSpec(FrozenModel):
    """Specification for modifying a file: REMOTE:TEXT."""

    remote_path: Path = Field(description="Remote path to the file")
    text: str = Field(description="Text to append/prepend")

    @classmethod
    def from_string(cls, s: str) -> "FileModificationSpec":
        """Parse a REMOTE:TEXT string into a FileModificationSpec."""
        if ":" not in s:
            raise ParseSpecError(f"File modification must be in REMOTE:TEXT format, got: {s}")
        remote, text = s.split(":", 1)
        return cls(remote_path=Path(remote.strip()), text=text)


class AgentProvisioningOptions(FrozenModel):
    """Simple provisioning options for the agent."""

    user_commands: tuple[str, ...] = Field(
        default=(),
        description="Custom shell commands to run during provisioning",
    )
    sudo_commands: tuple[str, ...] = Field(
        default=(),
        description="Custom shell commands to run as root during provisioning",
    )
    upload_files: tuple[UploadFileSpec, ...] = Field(
        default=(),
        description="Files to upload (LOCAL:REMOTE pairs)",
    )
    append_to_files: tuple[FileModificationSpec, ...] = Field(
        default=(),
        description="Text to append to files (REMOTE:TEXT pairs)",
    )
    prepend_to_files: tuple[FileModificationSpec, ...] = Field(
        default=(),
        description="Text to prepend to files (REMOTE:TEXT pairs)",
    )
    create_directories: tuple[Path, ...] = Field(
        default=(),
        description="Directories to create on the remote",
    )


class NamedCommand(FrozenModel):
    """A command with an optional window name for tmux."""

    command: CommandString = Field(description="The command to run")
    window_name: str | None = Field(
        default=None,
        description="Optional name for the tmux window (auto-generated if not provided)",
    )

    @classmethod
    def from_string(cls, s: str) -> "NamedCommand":
        """Parse a command string, optionally with a window name prefix.

        Accepts two formats:
        - "command string" -> NamedCommand(command="command string", window_name=None)
        - 'name="command string"' -> NamedCommand(command="command string", window_name="name")
        - 'name=command string' -> NamedCommand(command="command string", window_name="name")

        Window names are distinguished from environment variables by case:
        - Lowercase or mixed-case names (e.g., server, my_window) are treated as window names
        - ALL_UPPERCASE names (e.g., FOO, MY_VAR) are treated as env var assignments
        """
        # Check if the string starts with a name= prefix
        if "=" in s:
            # Find the first = to split name from command
            eq_idx = s.index("=")
            potential_name = s[:eq_idx]
            # Validate that the potential name looks like a valid window name
            # (no spaces, quotes, or special characters that would indicate it's part of the command)
            if potential_name and " " not in potential_name and '"' not in potential_name:
                rest = s[eq_idx + 1 :]
                # Check if the rest is quoted - if so, strip the quotes
                if rest.startswith('"') and rest.endswith('"') and len(rest) > 1:
                    command = rest[1:-1]
                    return cls(command=CommandString(command), window_name=potential_name)
                elif rest.startswith("'") and rest.endswith("'") and len(rest) > 1:
                    command = rest[1:-1]
                    return cls(command=CommandString(command), window_name=potential_name)
                else:
                    # Unquoted - use heuristic to distinguish window names from env vars
                    # Environment variables are typically ALL_UPPERCASE
                    # Window names are typically lowercase or mixed-case
                    is_likely_env_var = potential_name.isupper() and potential_name.replace("_", "").isalnum()
                    if is_likely_env_var:
                        # Treat as plain command (env var assignment like FOO=bar cmd)
                        return cls(command=CommandString(s), window_name=None)
                    else:
                        # Treat as named command
                        return cls(command=CommandString(rest), window_name=potential_name)

        # No name prefix or equals sign, just a plain command
        return cls(command=CommandString(s), window_name=None)


class AgentDataOptions(FrozenModel):
    """Options for what data to include from the source."""

    is_rsync_enabled: bool = Field(
        default=True,
        description="Whether to use rsync for file transfer",
    )
    rsync_args: str = Field(
        default="",
        description="Additional arguments to pass to rsync",
    )


class CreateAgentOptions(FrozenModel):
    """Complete options for creating a new agent.

    Combines identity, environment, git, and lifecycle options.
    """

    agent_type: AgentTypeName | None = Field(
        default=None,
        description="Type of agent to run (claude, codex, etc.)",
    )
    name: AgentName | None = Field(
        default=None,
        description="Agent name (auto-generated if not specified)",
    )
    command: CommandString | None = Field(
        default=None,
        description="Override the agent command",
    )
    additional_commands: tuple[NamedCommand, ...] = Field(
        default=(),
        description="Extra commands to run in additional tmux windows",
    )
    agent_args: tuple[str, ...] = Field(
        default=(),
        description="Additional arguments passed to the agent",
    )
    user: str | None = Field(
        default=None,
        description="User to run the agent as",
    )
    target_path: Path | None = Field(
        default=None,
        description="Target path for the agent work_dir",
    )
    is_copy_immediate: bool = Field(
        default=False,
        description="Whether to copy the source data immediately (before building the host) or after",
    )
    initial_message: str | None = Field(
        default=None,
        description="Initial message to pipe to the agent on startup",
    )
    resume_message: str | None = Field(
        default=None,
        description="Message to send when the agent is started (resumed) after being stopped",
    )
    ready_timeout_seconds: float = Field(
        default=DEFAULT_AGENT_READY_TIMEOUT_SECONDS,
        description="Timeout in seconds to wait for agent readiness before sending initial message",
    )
    git: AgentGitOptions | None = Field(
        default=None,
        description="Git configuration for the work_dir (None if no git repo)",
    )
    data_options: AgentDataOptions = Field(
        default_factory=AgentDataOptions,
        description="Options for what data to include from the source",
    )
    environment: AgentEnvironmentOptions = Field(
        default_factory=AgentEnvironmentOptions,
        description="Environment variable configuration",
    )
    lifecycle: AgentLifecycleOptions = Field(
        default_factory=AgentLifecycleOptions,
        description="Lifecycle and idle detection options",
    )
    permissions: AgentPermissionsOptions = Field(
        default_factory=AgentPermissionsOptions,
        description="Permissions options",
    )
    label_options: AgentLabelOptions = Field(
        default_factory=AgentLabelOptions,
        description="Label options",
    )
    provisioning: AgentProvisioningOptions = Field(
        default_factory=AgentProvisioningOptions,
        description="Simple provisioning options",
    )


# =========================================================================
# Host Option Types (parallel to Agent option types above)
# =========================================================================


class NewHostBuildOptions(FrozenModel):
    """Options for building a new host image."""

    snapshot: SnapshotName | None = Field(
        default=None,
        description="Use existing snapshot instead of building",
    )
    context_path: Path | None = Field(
        default=None,
        description="Build context directory [default: local .git root]",
    )
    build_args: tuple[str, ...] = Field(
        default=(),
        description="Arguments for the build command",
    )
    start_args: tuple[str, ...] = Field(
        default=(),
        description="Arguments for the start command",
    )


class HostEnvironmentOptions(FrozenModel):
    """Environment variable configuration for a host."""

    env_vars: tuple[EnvVar, ...] = Field(
        default=(),
        description="Environment variables to set (KEY=VALUE)",
    )
    env_files: tuple[Path, ...] = Field(
        default=(),
        description="Files to load environment variables from",
    )
    known_hosts: tuple[str, ...] = Field(
        default=(),
        description="SSH known_hosts entries to add to the host (for outbound SSH connections)",
    )
    authorized_keys: tuple[str, ...] = Field(
        default=(),
        description="SSH authorized_keys entries to add to the host (for inbound SSH connections)",
    )


class NewHostOptions(FrozenModel):
    """Options for creating a new host."""

    provider: ProviderInstanceName = Field(
        description="Provider to use for creating the host (docker, modal, local, ...)",
    )
    name: HostName | None = Field(
        default=None,
        description="Name for the new host (None means use provider default or auto-generate)",
    )
    name_style: HostNameStyle = Field(
        default=HostNameStyle.ASTRONOMY,
        description="Style for auto-generated host name (used when name is None and provider has no default)",
    )
    tags: dict[str, str] = Field(
        default_factory=dict,
        description="Metadata tags for the host",
    )
    build: NewHostBuildOptions = Field(
        default_factory=NewHostBuildOptions,
        description="Build options for the host image",
    )
    environment: HostEnvironmentOptions = Field(
        default_factory=HostEnvironmentOptions,
        description="Environment variable configuration",
    )
    lifecycle: HostLifecycleOptions = Field(
        default_factory=HostLifecycleOptions,
        description="Lifecycle and idle detection options",
    )
