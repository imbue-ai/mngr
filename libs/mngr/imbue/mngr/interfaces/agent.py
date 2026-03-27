from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Generic
from typing import Mapping
from typing import Sequence
from typing import TYPE_CHECKING
from typing import TypeVar

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import Permission

# this is the only place where it is acceptable to use the TYPE_CHECKING flag
if TYPE_CHECKING:
    from imbue.mngr.interfaces.host import CreateAgentOptions
    from imbue.mngr.interfaces.host import OnlineHostInterface

AgentConfigT = TypeVar("AgentConfigT", bound=AgentTypeConfig)


class AgentInterface(MutableModel, ABC, Generic[AgentConfigT]):
    """Interface for agent implementations.

    Generic over AgentConfigT so that each agent subclass can declare the
    specific config type it requires, and ``self.agent_config`` will have
    the correct narrowed type for the type checker.
    """

    id: AgentId = Field(frozen=True, description="Unique identifier for this agent")
    name: AgentName = Field(description="Human-readable agent name")
    agent_type: AgentTypeName = Field(frozen=True, description="Type of agent (claude, codex, etc.)")
    work_dir: Path = Field(frozen=True, description="Working directory for this agent")
    create_time: datetime = Field(frozen=True, description="When the agent was created")
    host_id: HostId = Field(description="ID of the host this agent runs on")
    mngr_ctx: MngrContext = Field(frozen=True, repr=False, description="Mngr context")
    agent_config: AgentConfigT = Field(frozen=True, repr=False, description="Agent type config")

    @abstractmethod
    def get_host(self) -> OnlineHostInterface:
        """Return the host this agent runs on (must be online)."""
        ...

    @abstractmethod
    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Assemble the full command to execute for this agent.

        May raise NoCommandDefinedError if no command is defined.
        """
        ...

    # =========================================================================
    # Certified Field Getters/Setters
    # =========================================================================

    @abstractmethod
    def get_command(self) -> CommandString:
        """Return the command used to start this agent."""
        ...

    @abstractmethod
    def get_expected_process_name(self) -> str:
        """Get the expected process name for lifecycle state detection.

        Subclasses can override this to return a hardcoded process name
        when the command is complex (e.g., shell wrappers with exports).
        """
        ...

    @abstractmethod
    def get_permissions(self) -> list[Permission]:
        """Return the list of permissions assigned to this agent."""
        ...

    @abstractmethod
    def set_permissions(self, value: Sequence[Permission]) -> None:
        """Set the list of permissions for this agent."""
        ...

    @abstractmethod
    def get_labels(self) -> dict[str, str]:
        """Return the labels attached to this agent."""
        ...

    @abstractmethod
    def set_labels(self, labels: Mapping[str, str]) -> None:
        """Replace all labels on this agent with the given mapping."""
        ...

    @abstractmethod
    def get_created_branch_name(self) -> str | None:
        """Return the git branch name that was created for this agent, or None if not applicable."""
        ...

    @abstractmethod
    def get_is_start_on_boot(self) -> bool:
        """Return whether this agent should start automatically on host boot."""
        ...

    @abstractmethod
    def set_is_start_on_boot(self, value: bool) -> None:
        """Set whether this agent should start automatically on host boot."""
        ...

    # =========================================================================
    # Interaction
    # =========================================================================

    @abstractmethod
    def is_running(self) -> bool:
        """Return whether the agent process is currently running."""
        ...

    @abstractmethod
    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Return the lifecycle state of this agent (stopped, running, waiting, replaced, or done)."""
        ...

    @abstractmethod
    def get_initial_message(self) -> str | None:
        """Return the initial message to send to the agent on creation, or None if not set."""
        ...

    @abstractmethod
    def get_resume_message(self) -> str | None:
        """Return the resume message to send when the agent is started (resumed), or None if not set."""
        ...

    @abstractmethod
    def get_ready_timeout_seconds(self) -> float:
        """Return the timeout in seconds to wait for agent readiness."""
        ...

    @abstractmethod
    def send_message(self, message: str) -> None:
        """Send a message to the running agent via its stdin."""
        ...

    @abstractmethod
    def capture_pane_content(self, include_scrollback: bool = False) -> str | None:
        """Capture the current tmux pane content for this agent.

        When include_scrollback is True, captures the full scrollback buffer
        instead of just the visible pane.

        Returns the pane content as a string, or None if capture fails
        (e.g., the session doesn't exist or the host is unreachable).
        """
        ...

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Wait for the agent to become ready, executing start_action while listening.

        Can be overridden by agent implementations that support signal-based readiness
        detection (e.g., polling for a marker file). Default just runs start_action
        without waiting for readiness confirmation.

        Implementations that override this should raise AgentStartError if the agent
        doesn't signal readiness within the timeout.
        """
        start_action()

    # =========================================================================
    # Status (Reported)
    # =========================================================================

    @abstractmethod
    def get_reported_url(self) -> str | None:
        """Return the agent's self-reported URL, or None if not set."""
        ...

    @abstractmethod
    def get_reported_start_time(self) -> datetime | None:
        """Return the agent's self-reported start time, or None if not set."""
        ...

    # =========================================================================
    # Activity
    # =========================================================================

    @abstractmethod
    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        """Return the last activity time for a given activity source, or None if not recorded."""
        ...

    @abstractmethod
    def record_activity(self, activity_type: ActivitySource) -> None:
        """Record activity of a given type for this agent at the current time."""
        ...

    @abstractmethod
    def get_reported_activity_record(self, activity_type: ActivitySource) -> str | None:
        """Return the raw activity record for a given type, or None if not found."""
        ...

    # =========================================================================
    # Plugin Data (Certified)
    # =========================================================================

    @abstractmethod
    def get_plugin_data(self, plugin_name: str) -> dict[str, Any]:
        """Return certified plugin data for a given plugin, or empty dict if not found."""
        ...

    @abstractmethod
    def set_plugin_data(self, plugin_name: str, data: dict[str, Any]) -> None:
        """Set certified plugin data for a given plugin."""
        ...

    # =========================================================================
    # Plugin Data (Reported)
    # =========================================================================

    @abstractmethod
    def get_reported_plugin_file(self, plugin_name: str, filename: str) -> str:
        """Read and return the contents of a reported plugin file."""
        ...

    @abstractmethod
    def set_reported_plugin_file(self, plugin_name: str, filename: str, data: str) -> None:
        """Write data to a reported plugin file."""
        ...

    @abstractmethod
    def list_reported_plugin_files(self, plugin_name: str) -> list[str]:
        """Return a list of all reported file names for a given plugin."""
        ...

    # =========================================================================
    # Environment
    # =========================================================================

    @abstractmethod
    def get_env_vars(self) -> dict[str, str]:
        """Return all environment variables for this agent."""
        ...

    @abstractmethod
    def set_env_vars(self, env: Mapping[str, str]) -> None:
        """Set all environment variables for this agent, replacing any existing ones."""
        ...

    @abstractmethod
    def get_env_var(self, key: str) -> str | None:
        """Return a single environment variable by key, or None if not found."""
        ...

    @abstractmethod
    def set_env_var(self, key: str, value: str) -> None:
        """Set a single environment variable for this agent."""
        ...

    # =========================================================================
    # Computed Properties
    # =========================================================================

    @property
    @abstractmethod
    def runtime_seconds(self) -> float | None:
        """Return how many seconds the agent has been running, or None if not started."""
        ...

    # =========================================================================
    # Provisioning Lifecycle
    # =========================================================================

    @abstractmethod
    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Called before any provisioning steps run, for validation.

        This method runs before any file transfers or package installations.
        Subclasses should use this to validate preconditions:
        - Check that required environment variables are set (e.g., ANTHROPIC_API_KEY)
        - Verify that required local files exist (e.g., SSH keys, config templates)
        - Validate any agent-type-specific configuration

        If validation fails, raise a PluginMngrError with a clear message
        explaining what is missing and how to fix it.

        IMPORTANT: This method should only perform read-only validation checks.
        Do not make any changes to the host in this method.
        """
        ...

    @abstractmethod
    def get_provision_file_transfers(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> Sequence[FileTransferSpec]:
        """Return file transfer specifications for provisioning.

        Subclasses can declare files that need to be transferred from the local
        machine to the remote host during provisioning.

        Returns a sequence of FileTransferSpec objects, each specifying:
        - local_path: Path to the file on the local machine
        - agent_path: Destination path on the remote host (relative to work_dir)
        - is_required: If True, provisioning fails if the local file doesn't exist

        Return an empty sequence if no files need to be transferred.

        All collected file transfers are executed before package installation
        and other provisioning steps.
        """
        ...

    def modify_env_vars(
        self,
        host: OnlineHostInterface,
        env_vars: dict[str, str],
    ) -> None:
        """Mutate the agent's environment variables before they are written.

        Called during provisioning after the base env vars (MNGR_HOST_DIR,
        MNGR_AGENT_STATE_DIR, etc.) and user-provided env vars have been
        collected, but before the env file is written to disk. Subclasses
        can add, update, or remove entries in env_vars.

        The default implementation is a no-op.
        """
        ...

    @abstractmethod
    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Called during agent provisioning, after file transfers but before CLI options.

        This method is called after on_before_provisioning validation and
        after get_provision_file_transfers files have been copied, but before any
        of the CLI-defined provisioning options (create_directories, upload_files,
        append_to_files, prepend_to_files, extra_provision_commands) are
        processed.

        Use this method to perform agent-type-specific provisioning that should happen
        before user-defined provisioning steps. Subclasses can install packages,
        create config files, or perform other setup tasks.
        """
        ...

    @abstractmethod
    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Called after all provisioning steps have completed.

        This method is called after all provisioning has finished, including:
        - Agent file transfers
        - Agent provisioning (provision method)
        - CLI-defined provisioning options (directories, uploads, commands, etc.)

        Use this method to perform finalization or verification steps, such as:
        - Verify that provisioning completed successfully
        - Perform final configuration that depends on other provisioning
        - Log or report provisioning status
        """
        ...

    # =========================================================================
    # Destruction Lifecycle
    # =========================================================================

    @abstractmethod
    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Called when the agent is being destroyed, before cleanup.

        This method is called at the beginning of destroy_agent(), before
        the agent's state directory and work directory are removed.

        Use this method to perform agent-type-specific cleanup, such as
        removing external configuration entries or releasing resources.
        """
        ...


class NoPermissionsAgentMixin:
    """Marker mixin for agents that are granted no permissions.

    These agents have no tool access and cannot perform destructive actions
    (e.g. configured with --tools ""). Because no permissions are granted,
    trust validation and permission dialogs are unnecessary during provisioning.
    """


class HeadlessAgentMixin(ABC):
    """Mixin for agent types that run headlessly (no TUI, no interactive input).

    Headless agents produce their output non-interactively and expose it
    via output(). This mixin serves as a marker interface so callers can
    check for headless capability without depending on a specific agent
    implementation.
    """

    @abstractmethod
    def output(self) -> str:
        """Wait for the agent to finish and return its complete output."""
        ...


class StreamingHeadlessAgentMixin(HeadlessAgentMixin):
    """Headless agent that can also stream output incrementally."""

    @abstractmethod
    def stream_output(self) -> Iterator[str]:
        """Yield output chunks as they become available."""
        ...
