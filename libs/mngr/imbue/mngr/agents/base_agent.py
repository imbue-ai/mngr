import fcntl
import json
import shlex
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final
from typing import Generator
from typing import Mapping
from typing import Sequence

from loguru import logger
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_fixed

from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import CorruptedAgentDataError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import determine_lifecycle_state
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.tmux import LONG_MESSAGE_THRESHOLD
from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.hosts.tmux import capture_tmux_pane_content
from imbue.mngr.interfaces.agent import AgentConfigT
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import InteractiveAgentMixin
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.env_utils import parse_env_file

_CAPTURE_PANE_TIMEOUT_SECONDS: Final[float] = 10.0


def quote_agent_args(agent_args: tuple[str, ...]) -> tuple[str, ...]:
    """Shell-quote raw ``agent_args`` for splicing into a shell-evaluated command.

    ``agent_args`` are raw argv strings (passed after ``--`` and threaded through
    Click as ``click.UNPROCESSED``): the OS shell stripped their quote characters
    when it built argv at invocation time, so each element must be re-quoted before
    it is joined into the (shell-evaluated) launch command. Without this, a value
    containing spaces or shell metacharacters -- e.g. ``--model "Gemini 3.5 Flash
    (Medium)"`` -- word-splits and the ``(`` is parsed as a subshell.

    ``cli_args`` must NOT be passed through here: string-form ``cli_args`` configs
    are split with a quote-preserving (non-POSIX) shlex (see ``split_cli_args_string``)
    and so already arrive shell-safe.
    """
    return tuple(shlex.quote(arg) for arg in agent_args)


class BaseAgent(AgentInterface[AgentConfigT]):
    """Concrete agent implementation that stores data on the host filesystem."""

    host: OnlineHostInterface = Field(description="The host this agent runs on (must be online)")

    def get_host(self) -> OnlineHostInterface:
        return self.host

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Assemble the agent command from an optional base plus ``cli_args`` and ``agent_args``.

        The base comes from ``command_override`` if provided, otherwise
        ``agent_config.command`` if set, otherwise nothing. After the base,
        ``cli_args`` and then ``agent_args`` are appended (joined with spaces).
        ``agent_args`` are shell-quoted (they are raw argv); ``cli_args`` and the
        base are left as-is (they arrive already shell-safe). Raises
        ``UserInputError`` if the final command would be empty -- i.e. no base,
        no ``cli_args``, and no ``agent_args``.

        ``initial_message`` is accepted for interface compatibility but is
        not used here. Subclasses that bake the prompt into the command line
        (e.g. streaming headless agents that ``cat`` a staged prompt file)
        should override to consume it; subclasses that deliver the prompt
        some other way, or ignore it entirely, can inherit this no-op.
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            base = None

        parts: list[str] = []
        if base is not None:
            parts.append(base)
        if self.agent_config.cli_args:
            parts.extend(self.agent_config.cli_args)
        # cli_args arrive already shell-safe; agent_args are raw argv and must be quoted
        # (see ``quote_agent_args``). ``mngr_claude`` overrides this method but applies the
        # identical rule via the same helper.
        parts.extend(quote_agent_args(agent_args))

        if not parts:
            raise UserInputError(
                f"Agent type '{self.agent_type}' has no command to run. "
                f"Pass a shell command after `--` "
                f"(e.g. `mngr create foo --type command -- sleep 99999`), "
                f"or set `command = '...'` on a custom `[agent_types.X]` in your config."
            )

        command = CommandString(" ".join(parts))
        logger.trace("Assembled command: {}", command)
        return command

    def _get_agent_dir(self) -> Path:
        """Get the agent's state directory path."""
        return get_agent_state_dir_path(self.host.host_dir, self.id)

    def _get_data_path(self) -> Path:
        """Get the path to the agent's data.json file."""
        return self._get_agent_dir() / "data.json"

    @retry(
        retry=retry_if_exception_type(json.JSONDecodeError),
        stop=stop_after_attempt(3),
        wait=wait_fixed(3),
        reraise=True,
    )
    def _read_data_with_retry(self) -> dict[str, Any]:
        content = self.host.read_text_file(self._get_data_path())
        return json.loads(content)

    def _read_data(self) -> dict[str, Any]:
        """Read the agent's data.json file."""
        try:
            return self._read_data_with_retry()
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as e:
            raise CorruptedAgentDataError(self.id, self._get_data_path(), e) from e

    def _write_data(self, data: dict[str, Any]) -> None:
        """Write the agent's data.json file and persist to external storage."""
        self.host.write_file(self._get_data_path(), json.dumps(data, indent=2).encode(), is_atomic=True)

        # Persist agent data to external storage (e.g., Modal volume)
        self.host.save_agent_data(self.id, data)

    # =========================================================================
    # Certified Field Getters/Setters
    # =========================================================================

    def get_command(self) -> CommandString:
        data = self._read_data()
        cmd = data.get("command")
        return CommandString(cmd) if cmd else CommandString("bash")

    def set_command(self, command: CommandString) -> None:
        data = self._read_data()
        data["command"] = str(command)
        self._write_data(data)

    def get_labels(self) -> dict[str, str]:
        data = self._read_data()
        return data.get("labels", {})

    def set_labels(self, labels: Mapping[str, str]) -> None:
        data = self._read_data()
        data["labels"] = dict(labels)
        self._write_data(data)

    def get_created_branch_name(self) -> str | None:
        data = self._read_data()
        return data.get("created_branch_name")

    def get_is_start_on_boot(self) -> bool:
        data = self._read_data()
        return data.get("start_on_boot", False)

    def set_is_start_on_boot(self, value: bool) -> None:
        data = self._read_data()
        data["start_on_boot"] = value
        self._write_data(data)

    # =========================================================================
    # Interaction
    # =========================================================================

    def is_running(self) -> bool:
        """Check if the agent is currently running by checking lifecycle state."""
        state = self.get_lifecycle_state()
        is_running = state in (
            AgentLifecycleState.RUNNING,
            AgentLifecycleState.WAITING,
            AgentLifecycleState.REPLACED,
            AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        )
        logger.trace("Determined agent {} is_running={} (lifecycle_state={})", self.name, is_running, state)
        return is_running

    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Get the lifecycle state of this agent using tmux format variables.

        Collects tmux state and ps output via SSH, then delegates to the shared
        determine_lifecycle_state pure function for the actual state logic.
        """
        try:
            # Get pane state and pid in one command.
            result = self.host.execute_idempotent_command(
                self._build_lifecycle_probe_command(),
                timeout_seconds=5.0,
            )
            tmux_info = result.stdout.strip() if result.success else None

            # Migrate pre-upgrade agents whose primary window predates named-window
            # targeting: if the by-name probe missed but the session exists, rename
            # its primary window to the configured name and probe once more.
            if not tmux_info:
                is_migrated = self._migrate_unnamed_primary_window()
                if is_migrated:
                    reprobe_result = self.host.execute_idempotent_command(
                        self._build_lifecycle_probe_command(),
                        timeout_seconds=5.0,
                    )
                    tmux_info = reprobe_result.stdout.strip() if reprobe_result.success else None

            # Get ps output for descendant process detection
            ps_result = self.host.execute_idempotent_command(
                "ps -e -o pid=,ppid=,comm= 2>/dev/null",
                timeout_seconds=5.0,
            )
            ps_output = ps_result.stdout if ps_result.success else ""

            # Check if the active file exists
            is_active = self._check_file_exists(self._get_agent_dir() / "active")

            expected_process_name = self.get_expected_process_name()
            is_type_known = check_agent_type_known(str(self.agent_type), self.mngr_ctx.config)

            state = determine_lifecycle_state(
                tmux_info=tmux_info if tmux_info else None,
                is_active=is_active,
                expected_process_name=expected_process_name,
                ps_output=ps_output,
                is_agent_type_known=is_type_known,
            )
            logger.trace("Determined agent {} lifecycle state: {}", self.name, state)
            return state
        except HostConnectionError:
            logger.trace("Determined agent {} lifecycle state: STOPPED (host connection error)", self.name)
            return AgentLifecycleState.STOPPED

    def _build_lifecycle_probe_command(self) -> str:
        """Build the command that probes the agent's primary window for lifecycle state."""
        return (
            f"tmux list-panes -t {self.tmux_target.as_shell_arg()} "
            f"-F '#{{pane_dead}}|#{{pane_current_command}}|#{{pane_pid}}' 2>/dev/null | head -n 1"
        )

    def _migrate_unnamed_primary_window(self) -> bool:
        """One-time migration for in-flight agents created before named-window targeting.

        Agents started before mngr named the primary window (``tmux.primary_window_name``)
        have an unnamed primary window (tmux auto-named it after the running command), so
        targeting it by name misses and every window-targeted op silently fails. When the
        session exists but the by-name probe missed, rename the session's primary (lowest-index)
        window to the configured name so name-based targeting resolves again. This exists ONLY
        to migrate such pre-upgrade sessions; it is not part of normal operation, and a
        correctly-named session never reaches this path (the probe would have hit).

        Selecting the lowest-index window (rather than ``:0`` or the active window) is robust to
        the user's ``base-index`` and to extra mngr windows (ttyd, watchers): the agent always
        runs in the session's first-created window. Runs through the host exec layer, so it works
        on remote hosts too. Returns whether the rename command succeeded.
        """
        session_target = TmuxSessionTarget(session_name=self.session_name)
        quoted_primary_window_name = shlex.quote(self.mngr_ctx.config.tmux.primary_window_name)
        # Guard with has-session so a non-existent session is a no-op, then rename the
        # lowest-index window (the agent's primary window) to the configured name.
        rename_command = (
            f"tmux has-session -t {session_target.as_shell_arg()} 2>/dev/null && "
            f"MNGR_PRIMARY_WINDOW_INDEX=$(tmux list-windows -t {session_target.as_shell_arg()} "
            f"-F '#I' 2>/dev/null | sort -n | head -n 1) && "
            f'tmux rename-window -t {session_target.as_shell_arg()}:"$MNGR_PRIMARY_WINDOW_INDEX" '
            f"{quoted_primary_window_name}"
        )
        result = self.host.execute_idempotent_command(rename_command, timeout_seconds=5.0)
        return result.success

    def _get_command_basename(self, command: CommandString) -> str:
        """Extract the basename from a command string.

        Strips leading shell subshell syntax (e.g. '( script.sh ... ) &')
        to find the actual command name.
        """
        stripped = str(command).lstrip("( ")
        return stripped.split()[0].split("/")[-1] if stripped else ""

    def get_expected_process_name(self) -> str:
        """Get the expected process name for lifecycle state detection.

        Subclasses can override this to return a hardcoded process name
        when the command is complex (e.g., shell wrappers with exports).
        """
        return self._get_command_basename(self.get_command())

    def _check_file_exists(self, path: Path) -> bool:
        """Check if a file exists on the host."""
        try:
            self.host.read_text_file(path)
            return True
        except FileNotFoundError:
            return False

    def get_initial_message(self) -> str | None:
        data = self._read_data()
        return data.get("initial_message")

    def get_resume_message(self) -> str | None:
        data = self._read_data()
        return data.get("resume_message")

    def get_ready_timeout_seconds(self) -> float:
        data = self._read_data()
        stored = data.get("ready_timeout_seconds")
        if stored is None:
            return self.mngr_ctx.config.agent_ready_timeout
        return stored

    @property
    def tmux_target(self) -> TmuxWindowTarget:
        """Structured tmux target for the agent's primary window.

        Pins the named primary window (``tmux.primary_window_name``, default
        ``agent``) because agents run there; using the session without a window
        component selects the *currently active* window, which is wrong when
        additional windows exist (e.g., watchers, ttyd). Targeting by name (not
        ``:0``) keeps this correct regardless of the user's tmux ``base-index``.
        """
        return TmuxWindowTarget(session_name=self.session_name, window=self.mngr_ctx.config.tmux.primary_window_name)

    @contextmanager
    def _message_lock(self) -> Generator[None, None, None]:
        """Acquire an exclusive file lock to serialize concurrent message sends.

        Multiple processes (e.g., telegram bot, bootstrap, cron scripts) may call
        ``mngr message`` for the same agent concurrently. Without serialization,
        their tmux send-keys calls can interleave, corrupting the message.

        Uses ``flock`` on a lock file in the agent's state directory. Only locks
        for local hosts (where the lock file is on the local filesystem). For
        remote hosts, concurrent sends from the same machine are serialized by
        the remote provider's SSH connection, and concurrent sends from different
        machines are rare enough to not warrant cross-host locking.
        """
        # FIXME: you CAN lock remotely, it's just a little more difficult.
        #  We should fix this both here, and for lock_cooperatively
        if not self.host.is_local:
            yield
            return

        lock_path = self._get_agent_dir() / "message.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Wait for the agent to become ready, executing start_action while listening.

        Can be overridden by agent implementations that support signal-based readiness
        detection (e.g., polling for a marker file, or waiting for a TUI banner).
        Default just runs start_action without waiting for readiness confirmation.

        Implementations that override this should raise AgentStartError if the agent
        doesn't signal readiness within the timeout.
        """
        start_action()

    def capture_pane_content(self, include_scrollback: bool = False, window: int | str | None = None) -> str | None:
        """Capture the current tmux pane content for this agent.

        When window is None, captures the agent's primary window (window 0).
        Otherwise, captures the given tmux window (by index or name) in the
        agent's session.
        """
        target = (
            self.tmux_target if window is None else TmuxWindowTarget(session_name=self.session_name, window=window)
        )
        return self._capture_pane_content(target, include_scrollback=include_scrollback)

    def _capture_pane_content(self, tmux_target: TmuxWindowTarget, include_scrollback: bool = False) -> str | None:
        """Capture the current pane content, returning None on failure."""
        return capture_tmux_pane_content(
            self.host,
            tmux_target,
            timeout_seconds=_CAPTURE_PANE_TIMEOUT_SECONDS,
            include_scrollback=include_scrollback,
        )

    def _check_pane_contains(self, tmux_target: TmuxWindowTarget, text: str) -> bool:
        """Check if the pane content contains the given text."""
        content = self._capture_pane_content(tmux_target)
        found = content is not None and text in content
        return found

    # =========================================================================
    # Status (Reported)
    # =========================================================================

    def get_reported_url(self) -> str | None:
        status_path = self._get_agent_dir() / "status" / "url"
        try:
            return self.host.read_text_file(status_path).strip()
        except FileNotFoundError:
            return None

    def get_reported_start_time(self) -> datetime | None:
        status_path = self._get_agent_dir() / "status" / "start_time"
        try:
            content = self.host.read_text_file(status_path).strip()
            return datetime.fromisoformat(content)
        except FileNotFoundError:
            return None

    # =========================================================================
    # Activity
    # =========================================================================

    def get_reported_activity_time(self, activity_type: ActivitySource) -> datetime | None:
        """Return the last activity time using file modification time.

        Activity time is determined by mtime, not by parsing the JSON content.
        This ensures consistency across all activity writers (Python, bash, lua)
        and allows simple scripts to just touch files without writing JSON.
        """
        activity_path = self._get_agent_dir() / "activity" / activity_type.value.lower()
        return self.host.get_file_mtime(activity_path)

    def record_activity(self, activity_type: ActivitySource) -> None:
        """Record activity by writing JSON with timestamp and metadata.

        The JSON contains:
        - time: milliseconds since Unix epoch (int)
        - agent_id: the agent's ID (for debugging)
        - agent_name: the agent's name (for debugging)

        Note: The authoritative activity time is the file's mtime, not the
        JSON content. The JSON is for debugging/auditing purposes.
        """
        activity_path = self._get_agent_dir() / "activity" / activity_type.value.lower()
        now = datetime.now(timezone.utc)
        data = {
            "time": int(now.timestamp() * 1000),
            "agent_id": str(self.id),
            "agent_name": str(self.name),
        }
        self.host.write_text_file(activity_path, json.dumps(data, indent=2))
        logger.trace("Recorded {} activity for agent {}", activity_type, self.name)

    def get_reported_activity_record(self, activity_type: ActivitySource) -> str | None:
        activity_path = self._get_agent_dir() / "activity" / activity_type.value.lower()
        try:
            return self.host.read_text_file(activity_path)
        except FileNotFoundError:
            return None

    # =========================================================================
    # Plugin Data (Certified)
    # =========================================================================

    def get_plugin_data(self, plugin_name: str) -> dict[str, Any]:
        data = self._read_data()
        plugin_data = data.get("plugin", {})
        return plugin_data.get(plugin_name, {})

    def set_plugin_data(self, plugin_name: str, data: dict[str, Any]) -> None:
        agent_data = self._read_data()
        if "plugin" not in agent_data:
            agent_data["plugin"] = {}
        agent_data["plugin"][plugin_name] = data
        self._write_data(agent_data)

    # =========================================================================
    # Plugin Data (Reported)
    # =========================================================================

    def get_reported_plugin_file(self, plugin_name: str, filename: str) -> str:
        plugin_path = self._get_agent_dir() / "plugin" / plugin_name / filename
        return self.host.read_text_file(plugin_path)

    def set_reported_plugin_file(self, plugin_name: str, filename: str, data: str) -> None:
        plugin_path = self._get_agent_dir() / "plugin" / plugin_name / filename
        self.host.write_text_file(plugin_path, data)

    def list_reported_plugin_files(self, plugin_name: str) -> list[str]:
        plugin_dir = self._get_agent_dir() / "plugin" / plugin_name
        try:
            result = self.host.execute_idempotent_command(f"ls -1 '{plugin_dir}'", timeout_seconds=5.0)
            if result.success:
                return [f.strip() for f in result.stdout.split("\n") if f.strip()]
            return []
        except (OSError, HostConnectionError):
            return []

    # =========================================================================
    # Environment
    # =========================================================================

    def get_env_vars(self) -> dict[str, str]:
        env_path = self._get_agent_dir() / "env"
        try:
            content = self.host.read_text_file(env_path)
            return parse_env_file(content)
        except FileNotFoundError:
            return {}

    def set_env_vars(self, env: Mapping[str, str]) -> None:
        lines = [f"{key}={value}" for key, value in env.items()]
        content = "\n".join(lines) + "\n" if lines else ""
        env_path = self._get_agent_dir() / "env"
        self.host.write_text_file(env_path, content)

    def get_env_var(self, key: str) -> str | None:
        env = self.get_env_vars()
        return env.get(key)

    def set_env_var(self, key: str, value: str) -> None:
        env = self.get_env_vars()
        env[key] = value
        self.set_env_vars(env)

    # =========================================================================
    # Computed Properties
    # =========================================================================

    @property
    def runtime_seconds(self) -> float | None:
        start_time = self.get_reported_start_time()
        if start_time is None:
            return None
        now = datetime.now(timezone.utc)
        return (now - start_time).total_seconds()

    # =========================================================================
    # Provisioning Lifecycle
    # =========================================================================

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Default implementation: no-op.

        Subclasses can override to validate preconditions before provisioning.
        """

    def get_provision_file_transfers(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> Sequence[FileTransferSpec]:
        """Default implementation: no file transfers.

        Subclasses can override to declare files to transfer during provisioning.
        """
        return []

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Default implementation: no-op.

        Subclasses can override to perform agent-type-specific provisioning.
        """

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Default implementation: no-op.

        Subclasses can override to perform finalization after provisioning.
        """

    # =========================================================================
    # Destruction Lifecycle
    # =========================================================================

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Default implementation: no-op.

        Subclasses can override to perform cleanup when the agent is destroyed.
        """


class SendKeysAgent(InteractiveAgentMixin, BaseAgent[AgentConfigT]):
    """A ``BaseAgent`` that delivers interactive messages by sending keystrokes into its tmux pane.

    Shared by the keystroke-driven agents -- the interactive TUI coding agents
    (via ``InteractiveTuiAgent``) and the bare ``command`` runner. Headless agents
    and the server/extension-driven agents (opencode, pi) do not use this:
    headless agents take no interactive input at all, and opencode/pi implement
    ``send_message`` against their own APIs. Implements the
    ``InteractiveAgentMixin`` contract with a literal-text-plus-Enter send.
    """

    def send_message(self, message: str) -> None:
        """Send a message to the running agent.

        Acquires an exclusive file lock to prevent concurrent sends from
        interleaving tmux input. Runs preflight checks (e.g., dialog detection)
        first -- errors from preflight indicate a condition that won't resolve
        by resending (e.g., a blocking dialog).

        This is the simple send (literal text + Enter). Interactive TUI agents
        subclass ``InteractiveTuiAgent`` (itself a ``SendKeysAgent``), which
        overrides this with the paste-detection / submission-signal pipeline.
        """
        with self._message_lock(), log_span("Sending message to agent {} (length={})", self.name, len(message)):
            self._preflight_send_message(self.tmux_target)
            self._send_message_simple(self.tmux_target, message)

    def _preflight_send_message(self, tmux_target: TmuxWindowTarget) -> None:
        """Run preflight checks before sending a message.

        Called at the start of send_message. Default is a no-op.
        Subclasses can override to perform checks (e.g., dialog detection)
        and raise an appropriate error to abort the send.
        """

    def _send_tmux_literal_keys(self, tmux_target: TmuxWindowTarget, message: str) -> None:
        """Send literal text to a tmux pane, choosing the best method by length.

        For short messages (< 1024 chars), uses ``tmux send-keys -l``.
        For long messages (>= 1024 chars), writes the text to a temp file on
        the host and uses ``tmux load-buffer`` + ``tmux paste-buffer`` to avoid
        the tmux "command too long" error.
        """
        target_arg = tmux_target.as_shell_arg()
        if len(message) < LONG_MESSAGE_THRESHOLD:
            send_msg_cmd = f"tmux send-keys -t {target_arg} -l -- {shlex.quote(message)}"
            result = self.host.execute_stateful_command(send_msg_cmd)
            if not result.success:
                raise SendMessageError(str(self.name), f"tmux send-keys failed: {result.stderr or result.stdout}")
        else:
            tmp_path = Path(f"/tmp/mngr-msg-buffer-{self.session_name}.txt")
            quoted_buffer = shlex.quote(f"mngr-{self.session_name}")
            quoted_path = shlex.quote(str(tmp_path))
            try:
                self.host.write_text_file(tmp_path, message)
                load_cmd = f"tmux load-buffer -b {quoted_buffer} {quoted_path}"
                result = self.host.execute_stateful_command(load_cmd)
                if not result.success:
                    raise SendMessageError(
                        str(self.name), f"tmux load-buffer failed: {result.stderr or result.stdout}"
                    )
                paste_cmd = f"tmux paste-buffer -b {quoted_buffer} -t {target_arg}"
                result = self.host.execute_stateful_command(paste_cmd)
                if not result.success:
                    raise SendMessageError(
                        str(self.name), f"tmux paste-buffer failed: {result.stderr or result.stdout}"
                    )
            finally:
                self.host.execute_idempotent_command(
                    f"tmux delete-buffer -b {quoted_buffer} 2>/dev/null; rm -f {quoted_path}"
                )

    def _send_message_simple(self, tmux_target: TmuxWindowTarget, message: str) -> None:
        """Send a message directly without waiting for paste confirmation."""
        self._send_tmux_literal_keys(tmux_target, message)

        send_enter_cmd = f"tmux send-keys -t {tmux_target.as_shell_arg()} Enter"
        result = self.host.execute_stateful_command(send_enter_cmd)
        if not result.success:
            raise SendMessageError(str(self.name), f"tmux send-keys Enter failed: {result.stderr or result.stdout}")
