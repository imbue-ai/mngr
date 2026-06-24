from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Generic
from typing import Sequence
from typing import TYPE_CHECKING
from typing import TypeVar

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.live_output import LiveOutputReader
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId

# this is the only place where it is acceptable to use the TYPE_CHECKING flag
if TYPE_CHECKING:
    from imbue.mngr.interfaces.host import CreateAgentOptions
    from imbue.mngr.interfaces.host import OnlineHostInterface

AgentConfigT = TypeVar("AgentConfigT", bound=AgentTypeConfig, covariant=True)


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

    @property
    def session_name(self) -> str:
        """The agent's tmux session name (``prefix + name``), via the config's single definition."""
        return self.mngr_ctx.config.agent_session_name(self.name)

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
        initial_message: str | None = None,
    ) -> CommandString:
        """Assemble the full command to execute for this agent.

        ``initial_message`` is the ``CreateAgentOptions.initial_message`` value
        (the content of ``--message`` / ``--message-file``) threaded through
        so agent types that bake the prompt into the command line (e.g.
        streaming headless agents that ``cat`` a staged prompt file) can make
        that decision without reading ``data.json`` -- at assembly time,
        inside ``Host.create_agent_state``, ``data.json`` has not been
        written yet.

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
    def set_command(self, command: CommandString) -> None:
        """Replace the command used to start this agent (applied on the next start/restart)."""
        ...

    @abstractmethod
    def get_expected_process_name(self) -> str:
        """Get the expected process name for lifecycle state detection.

        Subclasses can override this to return a hardcoded process name
        when the command is complex (e.g., shell wrappers with exports).
        """
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
        """Return the lifecycle state of this agent."""
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
    def capture_pane_content(self, include_scrollback: bool = False, window: int | str | None = None) -> str | None:
        """Capture the current tmux pane content for this agent.

        When include_scrollback is True, captures the full scrollback buffer
        instead of just the visible pane.

        When window is None, captures the agent's primary window. Otherwise,
        captures the given tmux window (by index or name) in the agent's session.

        Returns the pane content as a string, or None if capture fails
        (e.g., the session/window doesn't exist or the host is unreachable).
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
    # Preflight Checks (before host creation)
    # =========================================================================

    @classmethod
    def preflight_check(
        cls,
        source_host: OnlineHostInterface,
        source_path: Path,
        agent_options: CreateAgentOptions,
        agent_config: AgentTypeConfig,
        mngr_ctx: MngrContext,
    ) -> None:
        """Called before host creation to validate early prerequisites.

        This classmethod runs at the very start of create(), after
        on_before_create hooks but before the target host is resolved.
        Agent types can override this to perform cheap validation that
        would otherwise only surface much later (e.g., during provisioning).

        Because no agent instance exists yet, this is a classmethod that
        receives the source location, options, and config directly.

        If validation fails, raise a PluginMngrError with a clear message.

        IMPORTANT: This method should only perform read-only checks on
        the source. Do not make any changes to the source host.
        """
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
        extra_provision_commands) are processed.

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


class HasTranscriptMixin(ABC):
    """Mixin for agent types that capture a raw, agent-native transcript.

    Subclasses promise to copy their agent's native session JSONL files
    (whatever schema the agent uses internally) verbatim into
    ``$MNGR_AGENT_STATE_DIR/logs/<agent_type>_transcript/events.jsonl``.
    This raw stream is the source of truth: it preserves every field the
    agent emits, and it lives inside the agent state dir so it is durable
    against cleanup of the agent's own working directories.

    Raw transcript scripts are **always provisioned** when an agent type
    implements this mixin -- there is no user-facing opt-out, because the
    raw bytes are the only thing that lets ``mngr`` reconstruct the
    session after the agent's native files have been rotated or removed.

    The agent is responsible for launching the script(s) (typically as a
    backgrounded child of the tmux session in ``assemble_command``, or via
    a supervisor it provisions separately).

    Agents that also want the friendlier ``mngr transcript`` output should
    additionally implement :class:`HasCommonTranscriptMixin`, which adds
    a (gated) converter layer that maps the raw bytes into the
    agent-agnostic common schema.
    """

    @abstractmethod
    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return ``{script_name: contents}`` for raw-transcript capture scripts.

        Scripts are written to ``$MNGR_AGENT_STATE_DIR/commands/`` at mode
        ``0755`` during provisioning by
        :func:`imbue.mngr.agents.common_transcript.provision_raw_transcript_scripts`.
        """
        ...


class HasCommonTranscriptMixin(HasTranscriptMixin):
    """Mixin for agent types that emit a common, agent-agnostic transcript.

    Subclasses promise to produce a JSONL transcript at
    ``$MNGR_AGENT_STATE_DIR/events/<agent_type>/common_transcript/events.jsonl``
    using the shared event envelope (``timestamp``, ``type``, ``event_id``,
    ``source``) and one of three message types: ``user_message``,
    ``assistant_message``, ``tool_result``. ``mngr transcript`` discovers
    any such file regardless of agent type, so any agent that satisfies
    this contract gets ``mngr transcript`` support for free.

    Because the common schema is lossy (truncated previews, dropped
    metadata), the converter always runs on top of the raw transcript
    captured by :class:`HasTranscriptMixin`. Subclasses therefore inherit
    from that mixin and must also implement ``get_raw_transcript_scripts``.

    Subclasses implement ``get_common_transcript_scripts`` to return the
    per-agent converter scripts that read the raw transcript and write to
    the common path, and ``is_common_transcript_enabled`` to report whether
    the user has opted in for this particular instance. They are
    responsible for launching those scripts as part of ``assemble_command``
    (typically as a backgrounded child of the tmux session).
    """

    @property
    @abstractmethod
    def is_common_transcript_enabled(self) -> bool:
        """Whether this agent instance should emit a common transcript.

        Typically derived from a config field such as
        ``self.agent_config.emit_common_transcript``. Read by the shared
        ``maybe_provision_common_transcript_scripts`` helper to gate
        provisioning, and (by convention) by the agent's ``assemble_command``
        to gate launching.
        """
        ...

    @abstractmethod
    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return ``{script_name: contents}`` for the gated transcript converter scripts.

        Only scripts that should be **omitted entirely** when
        ``is_common_transcript_enabled`` is False belong here. Raw-transcript
        scripts (always provisioned) belong on
        :meth:`HasTranscriptMixin.get_raw_transcript_scripts`.

        Scripts are written to ``$MNGR_AGENT_STATE_DIR/commands/`` at mode
        ``0755`` during provisioning by
        :func:`imbue.mngr.agents.common_transcript.maybe_provision_common_transcript_scripts`.
        """
        ...


class SupportsLiveOutputMixin(ABC):
    """Mixin for agents that publish a live, in-progress view of their output before a turn completes.

    The concrete surface differs by agent kind -- a headless agent captures its
    stdout (raw text or stream-json) to a file, while a TUI agent's watcher
    maintains a streaming-snapshot buffer -- but both reduce to the same shape:
    a host file (:meth:`get_live_output_path`) plus a reader that turns
    successive reads of it into text deltas (:meth:`make_live_output_reader`).
    Capability detection treats "can stream live output" as one capability
    regardless of which surface an agent uses.

    The poll-read-extract loop over that file lives in
    :func:`imbue.mngr.agents.live_output_tail.tail_live_output`; pull consumers
    (a headless ``stream_output``) call it directly, while push consumers (the
    robinhood multi-turn drivers) instead poll the reader themselves,
    interleaved with their own transcript/lifecycle reads.
    """

    @abstractmethod
    def get_live_output_path(self) -> Path:
        """Return the host file this agent publishes its live, in-progress output to."""
        ...

    @abstractmethod
    def make_live_output_reader(self) -> LiveOutputReader:
        """Create a fresh reader that extracts text deltas from this agent's live-output file."""
        ...


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


class StreamingHeadlessAgentMixin(SupportsLiveOutputMixin, HeadlessAgentMixin):
    """Headless agent that can also stream output incrementally."""

    @abstractmethod
    def stream_output(self) -> Iterator[str]:
        """Yield output chunks as they become available."""
        ...

    def stage_initial_message(self, initial_message: str) -> None:
        """Materialise ``initial_message`` on disk before the agent process starts.

        Called by ``api_create`` after ``create_agent_state`` (so the agent's
        state dir / ``$MNGR_AGENT_STATE_DIR`` exists) but before
        ``start_agents``. Agent types override this to write prompt files
        that their command reads on startup, since headless agents cannot
        receive messages via ``send_message``. Files staged under the
        agent's state dir are removed when the agent is destroyed, so no
        explicit cleanup is required.

        The default implementation cannot deliver the message (it has no
        prompt-file protocol), so it logs a warning naming the agent class
        rather than silently dropping the user's ``--message`` content.
        Agent types that ignore the initial message should override this to
        a true no-op; agent types that expose the message some other way
        should override to stage it where their command can read it.
        """
        logger.warning(
            "Ignoring initial_message for agent type {}: this agent does not override "
            "stage_initial_message, so the --message content cannot be delivered.",
            type(self).__name__,
        )


class HasSessionPreservationMixin(ABC):
    """Mixin for agent types that preserve their session/transcript files on destroy.

    When the agent (or its host) is destroyed, its native session files, raw +
    common transcripts, and resume pointers should be copied to a durable
    preserved location before the state dir is removed, so the conversation is
    not lost. The agent's ``on_destroy`` calls this, gated by its own config.
    """

    @abstractmethod
    def preserve_session_state(self, host: OnlineHostInterface) -> None:
        """Copy this agent's session/transcript files to the preserved location before cleanup."""
        ...


class HasSessionAdoptionMixin(ABC):
    """Mixin for agent types that can adopt an existing conversation session into a new agent.

    The read-side counterpart to :class:`HasSessionPreservationMixin`: it consumes an existing
    session (a live agent's, a preserved one's, or a config dir's) so a freshly created agent
    resumes that context. Covers both an explicitly named session (the ``--adopt <id>`` create
    option) and the source agent's session when cloning (``--from <agent>``).
    The agent's ``on_after_provisioning`` calls this, gated by the relevant create options.
    """

    @abstractmethod
    def adopt_session(self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext) -> None:
        """Adopt the session(s) named in the create options into this newly provisioned agent."""
        ...


class HasUnattendedModeMixin(ABC):
    """Mixin for agent types that can run with no human (auto-allow in-run tool prompts).

    This is what makes remote / scheduled / headless agents work: every in-run
    tool-approval prompt is auto-approved when the agent is configured for
    unattended operation. (First-launch dialogs are handled separately, by the
    universal mngr-owned-dialogs path.) How the auto-allow is applied differs per
    CLI (a permission hook, a skip flag, a config write); this contract reports
    whether unattended operation is enabled for this instance.
    """

    @abstractmethod
    def is_unattended_enabled(self) -> bool:
        """Whether this agent instance is configured to run unattended (auto-allow tool prompts)."""
        ...


class HasPermissionPolicyMixin(ABC):
    """Mixin for agent types that support a per-resource allow/deny/ask permission policy.

    A refinement on top of plain unattended auto-allow: the CLI can express a
    per-tool / per-resource policy (e.g. allow ``git *`` but deny ``rm -rf *``).
    Each CLI stores this differently (a settings block, a config-overrides key, a
    sandbox mode); this contract returns the instance's configured policy in a
    normalized form, empty when none is set.
    """

    @abstractmethod
    def get_permission_policy(self) -> Mapping[str, Any]:
        """Return this agent instance's configured per-resource permission policy (empty if none)."""
        ...


class HasVersionManagementMixin(ABC):
    """Mixin for agent types that control which version of their binary runs.

    Either by pinning a specific version or by following an update policy (the
    two faces of version control -- not pinning is itself a choice to track
    upstream). CLIs that simply assume whatever binary is on PATH do not have
    this capability. The agent calls ``reconcile_installed_version`` during
    provisioning (once the binary is present) to enforce that intent: a pinning
    agent verifies the installed version and raises on mismatch; an update-policy
    agent runs its (best-effort) update check.
    """

    @abstractmethod
    def reconcile_installed_version(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        """Enforce this agent's version intent against the already-present binary.

        Called during provisioning after the binary is known to be installed.
        Pinning agents verify the installed version matches (raising on
        mismatch); update-policy agents run their (best-effort) update check.
        """
        ...


class HasAutoInstallMixin(ABC):
    """Mixin for agent types that can install their CLI binary if it is missing.

    A base capability every real agent should have: provisioning checks whether
    the binary is present and, if not, installs it (gated by consent on local
    hosts and a config flag on remote hosts). The bare config-driven command
    shells do not have it -- they run an arbitrary command, not a known binary.
    This contract returns the per-CLI install command.
    """

    @abstractmethod
    def get_install_binary_name(self) -> str:
        """Return the name of the CLI binary to check for on PATH (e.g. 'claude')."""
        ...

    @abstractmethod
    def get_install_command(self) -> str:
        """Return the shell command that installs this agent's CLI binary."""
        ...


class InteractiveAgentMixin(ABC):
    """Mixin for agent types that accept interactive user messages at runtime.

    The contract is a single ``send_message`` method. Headless and bare-command-less
    agents do not take interactive input and so do not inherit this; the ``mngr message``
    command checks ``isinstance(agent, InteractiveAgentMixin)`` to decide whether an
    agent type can be messaged at all (rather than every agent carrying a rejecting
    stub). The send mechanism differs by agent: keystroke injection into a tmux pane
    (``SendKeysAgent`` / ``InteractiveTuiAgent``, used by claude/codex/antigravity and the
    bare ``command`` runner) or an agent-native API (opencode's server, pi's extension).
    """

    @abstractmethod
    def send_message(self, message: str) -> None:
        """Send an interactive message to the running agent."""
        ...


def require_interactive_agent(agent: AgentInterface[Any]) -> InteractiveAgentMixin:
    """Return ``agent`` narrowed to :class:`InteractiveAgentMixin`, or raise if it takes no messages.

    Used by the message-delivery paths (``mngr message`` and the initial/resume-message
    flows) to refuse a headless agent type with a clear error rather than an attribute
    error, now that ``send_message`` lives only on interactive agents.
    """
    if not isinstance(agent, InteractiveAgentMixin):
        raise SendMessageError(
            str(agent.name), f"agent type '{agent.agent_type}' does not accept interactive messages"
        )
    return agent


class CliBackedAgentMixin:
    """Marker for agents that wrap a specific external coding-model CLI (claude, codex,
    antigravity, opencode, pi), as opposed to the bare ``command`` / ``headless_command``
    runners that execute an arbitrary shell command.

    A bare marker with no contract of its own. The CLI-oriented capabilities (a native
    transcript, auto-install, version management, per-tool permission policy, usage tracking,
    session adoption) only apply to these agents; the generic command runners, which do not
    wrap a known CLI, render those rows as ``n/a``. Every real CLI-backed agent inherits this,
    so the matrix scopes those rows positively rather than by the absence of a command marker.
    """
