from __future__ import annotations

import shlex
from abc import ABC
from abc import abstractmethod
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterator
from typing import Mapping
from typing import Sequence

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ParseSpecError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import ActivityConfig
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TmuxHeight
from imbue.mngr.primitives import TmuxWidth
from imbue.mngr.primitives import TmuxWindowSize
from imbue.mngr.primitives import TransferMode


class HostInterface(MutableModel, ABC):
    """Interface for host implementations."""

    id: HostId = Field(frozen=True, description="Unique identifier for this host")
    pre_baked_agent_id: AgentId | None = Field(
        default=None,
        frozen=True,
        description=(
            "Agent id of an agent that already exists on this host at host-creation "
            "time and that ``create_agent_state`` is expected to adopt in place "
            "(rather than treat as a duplicate-name collision). Set by providers "
            "whose ``create_host`` returns a host with a baked-in agent -- "
            "``ImbueCloudHost`` is the only example today (the lease surfaces a "
            "pre-baked ``system-services`` agent). ``None`` for every other "
            "provider, in which case the standard duplicate-name check applies."
        ),
    )

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
    def discover_agents(self) -> list[DiscoveredAgent]:
        """Return lightweight data for all agents on this host."""
        ...

    @abstractmethod
    def rename_agent(
        self,
        agent_ref: DiscoveredAgent,
        new_name: AgentName,
        labels_to_merge: Mapping[str, str] | None = None,
    ) -> DiscoveredAgent:
        """Rename an agent (and optionally merge labels in the same write) and return its updated ref.

        Works on both online and offline hosts. Online hosts additionally
        rename the agent's tmux session and update its env file; offline
        hosts edit only the provider's persisted agent data (data.json is
        the source of truth for the agent name).

        When ``labels_to_merge`` is non-empty, those keys/values are merged
        into the agent's existing labels as part of the same read-modify-
        write of ``data.json``, so an external observer (e.g. ``mngr
        observe``) never sees an in-between state where the new name is set
        but the new labels are not. Existing label keys are overwritten by
        ``labels_to_merge``.
        """
        ...

    # =========================================================================
    # Agent-Derived Information
    # =========================================================================

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

    def disconnect(self) -> None:
        """Disconnect from this host, releasing any held connections.

        Online host implementations should override this to close SSH or other
        network connections. The default is a no-op for offline hosts.
        """


class HostFileReadInterface(MutableModel, ABC):
    """Read-only access to a host's persistent files.

    The subset of file operations that work even when a host is not online for
    command execution, as long as its persistent storage (volume) is reachable.
    All paths are absolute paths as seen under the host's ``host_dir``.

    Implemented by:
    - :class:`OuterHostInterface` (and thus every online host), reading the
      live filesystem over SSH / locally.
    - :class:`~imbue.mngr.hosts.offline_host.OfflineHostWithVolume`, reading the
      host's persisted volume when the host itself is stopped.

    Splitting these reads out of :class:`OuterHostInterface` lets callers that
    only need to *read* files (log / transcript / session readers, session
    preservation, ``mngr file get``/``list``) accept a stopped-but-volume-backed
    host without it having to pretend it can execute commands or write files.
    """

    @abstractmethod
    def read_file(self, path: Path) -> bytes:
        """Read a file and return its contents as bytes."""
        ...

    @abstractmethod
    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        """Read a file and return its contents as a string."""
        ...

    @abstractmethod
    def path_exists(self, path: Path) -> bool:
        """Whether ``path`` exists on this host."""
        ...

    @abstractmethod
    def get_file_mtime(self, path: Path) -> datetime | None:
        """Return the modification time of a file, or None if the file doesn't exist."""
        ...

    @abstractmethod
    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        """List the entries under directory ``path``.

        Returns one :class:`~imbue.mngr.interfaces.data_types.VolumeFile` per
        entry, each with an absolute ``path`` under the host's ``host_dir``.
        When ``recursive`` is True, descends into subdirectories and returns
        every nested entry. Returns an empty list if ``path`` does not exist or
        is not a directory.
        """
        ...


class HostFileWriteInterface(MutableModel, ABC):
    """Write access to a host's files.

    The companion to :class:`HostFileReadInterface`. Implemented by:
    - :class:`OuterHostInterface` (and thus every online host), writing the live
      filesystem over SSH / locally.
    - :class:`~imbue.mngr.hosts.offline_host.OfflineHostWithVolume`, writing the
      host's persisted volume when the host itself is stopped (so files can be
      staged for the next time it starts). File modes are not settable on a
      volume write, so ``mode`` is ignored there.

    All paths are absolute paths as seen under the host's ``host_dir``.
    """

    @abstractmethod
    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        """Write bytes content to a file."""
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


class OuterHostInterface(HostFileReadInterface, HostFileWriteInterface, ABC):
    """Minimal interface for the "outer" machine that hosts a container/sandbox.

    Outer hosts have a strictly smaller surface than mngr-managed hosts: just the
    safe primitives (file I/O, command execution, SSH info, name, disconnect).
    They have no agents, no host_dir, no lifecycle/state tracking, no snapshots,
    no tags. A regular Host (which IS an OuterHostInterface) can be returned as
    an outer when the outer is itself an mngr-managed machine.
    """

    id: HostId = Field(frozen=True, description="Unique identifier for this host")
    connector: PyinfraConnector = Field(frozen=True, description="Pyinfra connector for host operations")

    @property
    @abstractmethod
    def is_local(self) -> bool:
        """Return True if this host is the local machine, False for remote hosts."""
        ...

    @abstractmethod
    def get_name(self) -> str:
        """Return the connector's display name (e.g. SSH hostname or IP).

        Returns ``str`` -- not ``HostName`` -- because an outer host's name
        is the literal connection target, which is commonly a dotted IPv4
        address (``192.0.2.10``) or DNS name (``vps-x.vps.ovh.us``);
        ``HostName`` forbids dots since it doubles as a CLI address token.
        The ``Host`` subclass overrides this and returns a ``HostName``,
        which is a ``str`` subclass and so satisfies the wider type here.
        """
        ...

    @abstractmethod
    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute an idempotent shell command on this host and return the result."""
        ...

    @abstractmethod
    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """
        Execute a shell command on this host *that cannot be retried* and return the result.

        Prefer to use execute_idempotent_command whenever possible, as it is a much simpler abstraction and more robust.
        This is really here if you *must* do something which cannot be made idempotent.
        It automatically handles making the command idempotent, but it's much slower and more complex.
        """
        ...

    @abstractmethod
    def execute_streaming_command(
        self,
        command: str,
        on_line: Callable[[str], None],
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a command, calling ``on_line`` for each output line as it arrives.

        Useful for long-running commands where progress visibility matters
        (e.g. ``docker build``). The callback is invoked once per line of
        stdout or stderr in the order the bytes arrive on each stream. The
        trailing newline is stripped before calling ``on_line``.

        Returns a ``CommandResult`` with the merged stdout / stderr / exit
        code once the command finishes.

        The command is treated as **idempotent**: implementations may retry
        it on transient SSH errors. When a retry happens, ``on_line`` will
        be re-called with the new attempt's output -- callers should expect
        and tolerate duplicate lines on retry. Use this for commands like
        ``docker build`` where re-running is safe.
        """
        ...

    @abstractmethod
    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        """Get SSH connection info for this host if it's remote.

        Returns (user, hostname, port, private_key_path) if remote, None if local.
        """
        ...

    def get_outer_ssh_port(self) -> int | None:
        """Port of the host's outer/management sshd, when distinct from the agent connection.

        Returns ``None`` by default. A provider whose host reaches a separate
        outer sshd on a non-obvious port (e.g. a slice's VM-root sshd via a
        box-forwarded port) surfaces it here so ``mngr create --format json``
        can report it. The default is sufficient for hosts whose only SSH
        endpoint is the one ``get_ssh_connection_info`` returns.
        """
        return None

    # returns (outer_host_public_key, container_host_public_key)
    def get_ssh_host_public_keys(self) -> tuple[str | None, str | None]:
        """The host's outer (VPS/VM-root) and container sshd host public keys, when known.

        Returns ``(None, None)`` by default. A provider that generates the host's
        sshd host keys at bake time surfaces them here so ``mngr create --format
        json`` can report them for strict host-key pinning.
        """
        return (None, None)

    def disconnect(self) -> None:
        """Disconnect from this host, releasing any held connections.

        Implementations that hold network connections should override this to
        close them. The default is a no-op.
        """

    def path_exists(self, path: Path) -> bool:
        """Whether ``path`` exists on this host.

        Uses the local filesystem for local hosts and ``test -e`` over SSH for
        remote hosts. Implemented on the interface so callers (including plugins)
        don't have to branch on ``is_local`` themselves.
        """
        if self.is_local:
            return path.exists()
        return self.execute_idempotent_command(f"test -e {shlex.quote(str(path))}", timeout_seconds=5.0).success


class OnlineHostInterface(HostInterface, OuterHostInterface, ABC):
    """Interface for hosts that are currently online and accessible for operations.

    Extends both HostInterface (mngr-managed metadata, agents, lifecycle) and
    OuterHostInterface (the minimal safe API for command/file primitives). A
    Host that implements OnlineHostInterface is automatically also an
    OuterHostInterface, so it can be returned as the outer host of another host
    when the outer is itself mngr-managed.
    """

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
    def lock_cooperatively(self, timeout_seconds: float | None = 300.0) -> Iterator[None]:
        """Hold the host's exclusive, cross-actor lock for the duration of the block.

        Holds a real flock(2) (directly on local hosts, over SSH on remote hosts)
        that coordinates local (in-host) and remote (over-SSH) holders and
        suppresses idle shutdown while held. ``timeout_seconds=None`` blocks
        indefinitely; a finite value raises LockNotHeldError if it cannot be
        acquired in time.
        """
        ...

    @abstractmethod
    def get_reported_lock_time(self) -> datetime | None:
        """Return the last modification time of the host lock file, or None if absent."""
        ...

    @abstractmethod
    def is_lock_held(self) -> bool:
        """Check whether the host lock is currently held (via a non-blocking flock probe)."""
        ...

    # =========================================================================
    # Certified Data
    # =========================================================================

    @abstractmethod
    def set_plugin_data(self, plugin_name: str, data: dict[str, Any]) -> None:
        """Update the certified plugin data for the given plugin name."""
        ...

    @abstractmethod
    def to_offline_host(self) -> HostInterface:
        """Return an offline representation of this host for use when the host is unreachable."""
        ...

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
    def get_host_env_path(self) -> Path:
        """Get the path to the host env file."""
        ...

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

    @abstractmethod
    def build_source_env_prefix(self, agent: AgentInterface) -> str:
        """Build a shell prefix that sources host and agent env files if they exist."""
        ...

    # =========================================================================
    # Provider-Derived Information
    # =========================================================================

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
        mngr_ctx: MngrContext,
    ) -> None:
        """Install packages, create config files, and set up an agent."""
        ...

    @abstractmethod
    def destroy_agent(self, agent: AgentInterface) -> None:
        """Remove an agent and all its associated state from this host.

        Best-effort and aggregate-and-continue: attempts every teardown step and collects
        every real failure. Returns normally on full success or benign "already gone"
        outcomes; raises ``CleanupFailedGroup`` if any real resources were left behind.
        See specs/cleanup-error-aggregation.md.
        """
        ...

    @abstractmethod
    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        """Start the specified agents by creating their tmux sessions and processes."""
        ...

    @abstractmethod
    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        """Stop the specified agents gracefully within the given timeout.

        Best-effort and aggregate-and-continue: attempts every step for every agent and
        collects every real failure. Returns normally on full success or benign "already
        gone" outcomes; raises ``CleanupFailedGroup`` if any real resources were left
        behind. See specs/cleanup-error-aggregation.md.
        """
        ...

    @abstractmethod
    def copy_directory(
        self,
        source_host: OnlineHostInterface,
        source_path: Path,
        target_path: Path,
        extra_args: str | None = None,
        exclude_git: bool = False,
    ) -> None:
        """Copy a directory from source_host:source_path to self:target_path using rsync.

        Handles all combinations of local/remote source and target:
        - Local to local
        - Local to remote (push via SSH)
        - Remote to local (pull via SSH)
        - Remote to remote (via local temp directory as intermediary)
        """
        ...

    @abstractmethod
    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        """Copy a directory from the local machine (where mngr runs) to self:target_path.

        Like ``copy_directory`` with a local source, but takes no source-host object --
        the source is always the local filesystem. This lets the host layer push staged
        files without resolving a local host (which would require ``mngr.api.providers``
        and hit an import cycle). Uses rsync (additive, no ``--delete``); ``extra_args``
        is appended to the rsync invocation (e.g. ``--include``/``--exclude`` filters).
        """
        ...

    @abstractmethod
    def save_agent_data(self, agent_id: AgentId, agent_data: Mapping[str, object]) -> None:
        """Persist agent data to external storage.

        Called when an agent's data.json is updated. Providers that support
        persistent agent state (like Modal) will sync this to their storage.
        """
        ...

    # =========================================================================
    # Outer Host Access
    # =========================================================================

    @contextmanager
    def outer_host(self) -> Iterator[OuterHostInterface | None]:
        """Open the outer host (the underlying machine that hosts this container/sandbox).

        Yields an ``OuterHostInterface`` for running commands and moving files
        on the outer machine, or ``None`` when no outer host is accessible
        (e.g. for the local provider, the ssh provider, modal sandboxes, or
        docker-over-tcp daemons).

        Each ``with`` entry produces a fresh outer-host instance and a fresh
        SSH connection (when applicable); the connection is closed on exit.
        Outer-host construction is delegated to
        ``self.provider_instance.outer_host_for(self.id)``.

        The default implementation here yields ``None``. Concrete ``Host``
        subclasses override this to delegate to the provider.
        """
        yield None


class HostLocation(FrozenModel):
    """A path on a specific host."""

    host: OnlineHostInterface = Field(
        description="The actual host where the source resides",
    )
    path: Path = Field(
        description="The actual path to the source directory on the host",
    )


class CreateWorkDirResult(FrozenModel):
    """Result of creating an agent work directory."""

    path: Path = Field(description="Path to the created work directory")
    created_branch_name: str | None = Field(
        default=None,
        description="Name of the git branch created for this work directory, if any",
    )


class AgentGitOptions(FrozenModel):
    """Git-related options for the agent work_dir."""

    base_branch: str | None = Field(
        default=None,
        description="Starting branch for the agent (default: current branch)",
    )
    new_branch_name: str | None = Field(
        default=None,
        description="Fully resolved name for the new branch, or None to use base_branch directly",
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


class AgentProvisioningOptions(FrozenModel):
    """Simple provisioning options for the agent."""

    extra_provision_commands: tuple[str, ...] = Field(
        default=(),
        description="Custom shell commands to run during provisioning",
    )
    upload_files: tuple[UploadFileSpec, ...] = Field(
        default=(),
        description="Files to upload (LOCAL:REMOTE pairs)",
    )
    create_directories: tuple[Path, ...] = Field(
        default=(),
        description="Directories to create on the remote",
    )


# Mapping from raw-string config/CLI field names to AgentProvisioningOptions
# target fields and their parsers.  Covers the three fields that map to
# AgentProvisioningOptions; env/env_file are handled separately because they
# map to AgentEnvironmentOptions instead.  Used by both the CLI (create.py) and
# the agent-type merge path (host.py) so the two stay in sync.
PROVISIONING_FIELD_MAP: tuple[tuple[str, str, Any], ...] = (
    ("extra_provision_command", "extra_provision_commands", str),
    ("upload_file", "upload_files", UploadFileSpec.from_string),
    ("create_directory", "create_directories", Path),
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


class AgentTmuxOptions(FrozenModel):
    """Per-agent tmux window sizing and resize behavior.

    A ``None`` field means "use the host default" (the session builder falls back
    to its built-in defaults and leaves tmux's resize policy untouched).
    """

    width: TmuxWidth | None = Field(
        default=None,
        description="tmux window width in columns (None = host default)",
    )
    height: TmuxHeight | None = Field(
        default=None,
        description="tmux window height in rows (None = host default)",
    )
    window_size: TmuxWindowSize | None = Field(
        default=None,
        description="tmux window-size resize mode (None = host default / today's behavior)",
    )

    def to_data_dict(self) -> dict[str, Any]:
        """Serialize to the JSON-friendly shape persisted in the agent's data.json."""
        return {
            "width": int(self.width) if self.width is not None else None,
            "height": int(self.height) if self.height is not None else None,
            "window_size": self.window_size.value if self.window_size is not None else None,
        }

    @classmethod
    def from_data_dict(cls, data: Mapping[str, Any] | None) -> "AgentTmuxOptions":
        """Reconstruct from the data.json ``tmux`` block, tolerating a missing/empty block."""
        if not data:
            return cls()
        raw_width = data.get("width")
        raw_height = data.get("height")
        raw_window_size = data.get("window_size")
        return cls(
            width=TmuxWidth(raw_width) if raw_width is not None else None,
            height=TmuxHeight(raw_height) if raw_height is not None else None,
            window_size=TmuxWindowSize(raw_window_size) if raw_window_size is not None else None,
        )


class CreateAgentOptions(FrozenModel):
    """Complete options for creating a new agent.

    Combines identity, environment, git, and lifecycle options.
    """

    agent_id: AgentId | None = Field(
        default=None,
        description="Explicit agent ID (auto-generated if not specified)",
    )
    agent_type: AgentTypeName = Field(
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
    worktree_base_folder: Path | None = Field(
        default=None,
        description="Base folder for git worktrees (overrides the default <host_dir>/worktrees)",
    )
    transfer_mode: TransferMode = Field(
        default=TransferMode.NONE,
        description="How to transfer the project into the agent work_dir",
    )
    initial_message: str | None = Field(
        default=None,
        description="Initial message to pipe to the agent on startup",
    )
    resume_message: str | None = Field(
        default=None,
        description="Message to send when the agent is started (resumed) after being stopped",
    )
    ready_timeout_seconds: float | None = Field(
        default=None,
        description="Timeout in seconds to wait for agent readiness before sending initial message. "
        "When None, falls back to MngrConfig.agent_ready_timeout at consumption time.",
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
    label_options: AgentLabelOptions = Field(
        default_factory=AgentLabelOptions,
        description="Label options",
    )
    provisioning: AgentProvisioningOptions = Field(
        default_factory=AgentProvisioningOptions,
        description="Simple provisioning options",
    )
    tmux: AgentTmuxOptions = Field(
        default_factory=AgentTmuxOptions,
        description="tmux window sizing and resize behavior for the agent's session",
    )
    plugin_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque dict for plugins to pass data through the creation pipeline. "
        "Keys are namespaced by plugin.",
    )
    adopt_session: tuple[str, ...] = Field(
        default=(),
        description="Session id(s) or path(s) to adopt into the new agent so it resumes that "
        "conversation (the --adopt option). Repeatable; the last named session is the one resumed "
        "on startup. Only valid for agent types that support session adoption "
        "(HasSessionAdoptionMixin); mutually exclusive with cloning via --from.",
    )
    source_agent_state_location: HostLocation | None = Field(
        default=None,
        description="Location of the source agent's state directory "
        "(set when cloning via --from with an agent source).",
    )
    is_update: bool = Field(
        default=False,
        description="Whether this is an update of an existing agent (idempotent create). "
        "When True, existing work_dir and state are updated rather than created from scratch.",
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


class HostProvisioningOptions(FrozenModel):
    """Simple provisioning options for a new host (post-creation hooks)."""

    post_host_create_commands: tuple[CommandString, ...] = Field(
        default=(),
        description="Shell commands to run inside the newly-created host, "
        "synchronously, after the host is ready but before any agent work_dir "
        "is touched. Each command runs in order; a non-zero exit aborts the create.",
    )
    post_host_create_outer_commands: tuple[CommandString, ...] = Field(
        default=(),
        description="Shell commands to run once on the host's outer machine (the "
        "underlying VM/daemon host), synchronously, after the host is ready. Run "
        "in order; a non-zero exit aborts the create. Skipped (with a warning) "
        "when the provider exposes no outer host.",
    )


# Mapping from raw-string config/CLI field names to HostProvisioningOptions
# target fields and their parsers. Parallels PROVISIONING_FIELD_MAP for the
# agent side; used so the CLI flag and template-stacking machinery stay in
# sync.
HOST_PROVISIONING_FIELD_MAP: tuple[tuple[str, str, Any], ...] = (
    ("post_host_create_command", "post_host_create_commands", CommandString),
    ("post_host_create_outer_command", "post_host_create_outer_commands", CommandString),
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
        default=HostNameStyle.COOLNAME,
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
    provisioning: HostProvisioningOptions = Field(
        default_factory=HostProvisioningOptions,
        description="Post-create provisioning hooks",
    )
