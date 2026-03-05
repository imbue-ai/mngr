import json
import re
import shlex
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final
from typing import Mapping
from typing import NoReturn
from typing import Sequence

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import HostConnectionError
from imbue.mng.errors import SendMessageError
from imbue.mng.hosts.common import determine_lifecycle_state
from imbue.mng.hosts.tmux import LONG_MESSAGE_THRESHOLD
from imbue.mng.hosts.tmux import capture_tmux_pane_content
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import FileTransferSpec
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import DEFAULT_AGENT_READY_TIMEOUT_SECONDS
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import Permission
from imbue.mng.utils.env_utils import parse_env_file
from imbue.mng.utils.polling import poll_until

# Constants for send_message paste-detection synchronization
_SEND_MESSAGE_TIMEOUT_SECONDS: Final[float] = 10.0
_TUI_READY_TIMEOUT_SECONDS: Final[float] = 10.0
_CAPTURE_PANE_TIMEOUT_SECONDS: Final[float] = 5.0

# Default timeout for signal-based synchronization
# Note that this does need to be fairly long, since it can take a little while for the machine to respond if you're unlucky
_DEFAULT_ENTER_SUBMISSION_WAIT_FOR_TIMEOUT_SECONDS: Final[float] = 10.0

# Compiled once for _normalize_for_match performance
_NON_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]")


def _normalize_for_match(text: str) -> str:
    """Strip non-alphanumeric characters and lowercase for fuzzy matching."""
    return _NON_ALNUM_RE.sub("", text.lower())


def _check_paste_content(pane_content: str, message: str) -> bool:
    """Check whether pasted message content is visible in pane text.

    Returns True if the tmux paste indicator is present OR if a
    normalized tail of the message matches the normalized pane content.
    """
    if "[Pasted text " in pane_content:
        return True

    normalized_pane = _normalize_for_match(pane_content)
    normalized_msg = _normalize_for_match(message)

    probe_length = min(60, len(normalized_msg))
    if probe_length == 0:
        return True
    probe = normalized_msg[-probe_length:]
    return probe in normalized_pane


class BaseAgent(AgentInterface):
    """Concrete agent implementation that stores data on the host filesystem."""

    host: OnlineHostInterface = Field(description="The host this agent runs on (must be online)")
    enter_submission_timeout_seconds: float = Field(
        default=_DEFAULT_ENTER_SUBMISSION_WAIT_FOR_TIMEOUT_SECONDS,
        description="Timeout in seconds for waiting on the enter submission signal",
    )

    def get_host(self) -> OnlineHostInterface:
        return self.host

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Default: command_override or config.command or agent_type, then append cli_args and agent_args.

        If no explicit command is defined, falls back to using the agent_type as a command.
        This allows using arbitrary commands as agent types (e.g., 'mng create my-agent echo').
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            # Fall back to using the agent type as a command (documented "Direct command" behavior)
            base = str(self.agent_type)

        parts = [base]
        if self.agent_config.cli_args:
            parts.extend(self.agent_config.cli_args)
        if agent_args:
            parts.extend(agent_args)

        command = CommandString(" ".join(parts))
        logger.trace("Assembled command: {}", command)
        return command

    def _get_agent_dir(self) -> Path:
        """Get the agent's state directory path."""
        return self.host.host_dir / "agents" / str(self.id)

    def _get_data_path(self) -> Path:
        """Get the path to the agent's data.json file."""
        return self._get_agent_dir() / "data.json"

    def _read_data(self) -> dict[str, Any]:
        """Read the agent's data.json file."""
        try:
            content = self.host.read_text_file(self._get_data_path())
            return json.loads(content)
        except FileNotFoundError:
            return {}

    def _write_data(self, data: dict[str, Any]) -> None:
        """Write the agent's data.json file and persist to external storage."""
        self.host.write_text_file(self._get_data_path(), json.dumps(data, indent=2))

        # Persist agent data to external storage (e.g., Modal volume)
        self.host.save_agent_data(self.id, data)

    # =========================================================================
    # Certified Field Getters/Setters
    # =========================================================================

    def get_command(self) -> CommandString:
        data = self._read_data()
        cmd = data.get("command")
        return CommandString(cmd) if cmd else CommandString("bash")

    def get_permissions(self) -> list[Permission]:
        data = self._read_data()
        perms = data.get("permissions", [])
        return [Permission(p) for p in perms]

    def set_permissions(self, value: Sequence[Permission]) -> None:
        data = self._read_data()
        data["permissions"] = [str(p) for p in value]
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
        is_running = state in (AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING, AgentLifecycleState.REPLACED)
        logger.trace("Determined agent {} is_running={} (lifecycle_state={})", self.name, is_running, state)
        return is_running

    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Get the lifecycle state of this agent using tmux format variables.

        Collects tmux state and ps output via SSH, then delegates to the shared
        determine_lifecycle_state pure function for the actual state logic.
        """
        try:
            session_name = f"{self.mng_ctx.config.prefix}{self.name}"

            # Get pane state and pid in one command
            result = self.host.execute_command(
                f"tmux list-panes -t '{session_name}:0' "
                f"-F '#{{pane_dead}}|#{{pane_current_command}}|#{{pane_pid}}' 2>/dev/null | head -n 1",
                timeout_seconds=5.0,
            )
            tmux_info = result.stdout.strip() if result.success else None

            # Get ps output for descendant process detection
            ps_result = self.host.execute_command(
                "ps -e -o pid=,ppid=,comm= 2>/dev/null",
                timeout_seconds=5.0,
            )
            ps_output = ps_result.stdout if ps_result.success else ""

            # Check if the active file exists
            is_active = self._check_file_exists(self._get_agent_dir() / "active")

            expected_process_name = self.get_expected_process_name()

            state = determine_lifecycle_state(
                tmux_info=tmux_info if tmux_info else None,
                is_active=is_active,
                expected_process_name=expected_process_name,
                ps_output=ps_output,
            )
            logger.trace("Determined agent {} lifecycle state: {}", self.name, state)
            return state
        except HostConnectionError:
            logger.trace("Determined agent {} lifecycle state: STOPPED (host connection error)", self.name)
            return AgentLifecycleState.STOPPED

    def _get_command_basename(self, command: CommandString) -> str:
        """Extract the basename from a command string."""
        return command.split()[0].split("/")[-1] if command else ""

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
        return data.get("ready_timeout_seconds", DEFAULT_AGENT_READY_TIMEOUT_SECONDS)

    @property
    def session_name(self) -> str:
        return f"{self.mng_ctx.config.prefix}{self.name}"

    @property
    def tmux_target(self) -> str:
        """Tmux target for the agent's primary window (window 0).

        Agents always run in window 0 of their tmux session. Using the bare
        session name as a tmux target selects the *currently active* window,
        which is wrong when additional windows exist (e.g., watchers, ttyd).
        """
        return f"{self.session_name}:0"

    def send_message(self, message: str) -> None:
        """Send a message to the running agent.

        For agents that echo input to the terminal (like Claude Code), uses a
        paste-detection approach to ensure the message is fully received before
        sending Enter. This avoids race conditions where Enter could be
        interpreted as a literal newline instead of a submit action.

        Subclasses can enable this by overriding uses_paste_detection_send().

        Before sending, runs preflight checks (e.g., dialog detection) that
        subclasses can customize by overriding _preflight_send_message().
        """
        with log_span("Sending message to agent {} (length={})", self.name, len(message)):
            self._preflight_send_message(self.tmux_target)

            if self.uses_paste_detection_send():
                self._send_message_with_paste_detection(self.tmux_target, message)
            else:
                self._send_message_simple(self.tmux_target, message)

    def uses_paste_detection_send(self) -> bool:
        """Return True to use paste-detection synchronization for send_message.

        When enabled, send_message sends text without a trailing newline, waits
        for paste detection or fuzzy content match on the pane, then sends Enter.
        This is useful for interactive agents like Claude Code where sending Enter
        immediately after the message text can cause race conditions.

        Returns False by default. Subclasses can override to enable.
        """
        return False

    def get_tui_ready_indicator(self) -> str | None:
        """Return a string that indicates the TUI is ready to accept input.

        This string will be looked for in the terminal pane content before sending
        messages. This is useful for TUIs that take time to initialize after the
        process starts.

        Returns None by default (no TUI readiness check). Subclasses can override.
        """
        return None

    def _preflight_send_message(self, tmux_target: str) -> None:
        """Run preflight checks before sending a message.

        Called at the start of send_message. Default is a no-op.
        Subclasses can override to perform checks (e.g., dialog detection)
        and raise an appropriate error to abort the send.
        """

    def _raise_send_timeout(self, tmux_target: str, timeout_reason: str) -> NoReturn:
        """Raise a SendMessageError for a send timeout."""
        raise SendMessageError(str(self.name), timeout_reason)

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

        if is_creating:
            # Wait for TUI to be ready if an indicator is configured
            tui_indicator = self.get_tui_ready_indicator()
            if tui_indicator is not None:
                self._wait_for_tui_ready(self.tmux_target, tui_indicator)

    def capture_pane_content(self) -> str | None:
        """Capture the current tmux pane content for this agent."""
        return self._capture_pane_content(self.tmux_target)

    def _send_tmux_literal_keys(self, tmux_target: str, message: str) -> None:
        """Send literal text to a tmux pane, choosing the best method by length.

        For short messages (< 1024 chars), uses ``tmux send-keys -l``.
        For long messages (>= 1024 chars), writes the text to a temp file on
        the host and uses ``tmux load-buffer`` + ``tmux paste-buffer`` to avoid
        the tmux "command too long" error.
        """
        if len(message) < LONG_MESSAGE_THRESHOLD:
            send_msg_cmd = f"tmux send-keys -t '{tmux_target}' -l {shlex.quote(message)}"
            result = self.host.execute_command(send_msg_cmd)
            if not result.success:
                raise SendMessageError(str(self.name), f"tmux send-keys failed: {result.stderr or result.stdout}")
        else:
            tmp_path = Path(f"/tmp/mng-msg-buffer-{self.session_name}.txt")
            quoted_buffer = shlex.quote(f"mng-{self.session_name}")
            quoted_path = shlex.quote(str(tmp_path))
            try:
                self.host.write_text_file(tmp_path, message)
                load_cmd = f"tmux load-buffer -b {quoted_buffer} {quoted_path}"
                result = self.host.execute_command(load_cmd)
                if not result.success:
                    raise SendMessageError(
                        str(self.name), f"tmux load-buffer failed: {result.stderr or result.stdout}"
                    )
                paste_cmd = f"tmux paste-buffer -b {quoted_buffer} -t '{tmux_target}'"
                result = self.host.execute_command(paste_cmd)
                if not result.success:
                    raise SendMessageError(
                        str(self.name), f"tmux paste-buffer failed: {result.stderr or result.stdout}"
                    )
            finally:
                self.host.execute_command(f"tmux delete-buffer -b {quoted_buffer} 2>/dev/null; rm -f {quoted_path}")

    def _send_message_simple(self, tmux_target: str, message: str) -> None:
        """Send a message directly without waiting for paste confirmation."""
        self._send_tmux_literal_keys(tmux_target, message)

        send_enter_cmd = f"tmux send-keys -t '{tmux_target}' Enter"
        result = self.host.execute_command(send_enter_cmd)
        if not result.success:
            raise SendMessageError(str(self.name), f"tmux send-keys Enter failed: {result.stderr or result.stdout}")

    def _send_message_with_paste_detection(self, tmux_target: str, message: str) -> None:
        """Send a message using paste-detection synchronization.

        Sends the message text WITHOUT a trailing newline, then waits for
        evidence that the text was received before pressing Enter. Evidence
        is either:

        1. The tmux paste indicator (``[Pasted text ``) visible on screen, or
        2. A fuzzy content match: the last chunk of the message (stripped to
           alphanumeric, lowercased) appears in the pane content (similarly
           treated).

        Once the message is confirmed on screen, sends Enter via
        ``_send_enter_and_wait`` for submission signal synchronization.
        """
        # Send keys WITHOUT a trailing newline (so it probably does not submit)
        self._send_tmux_literal_keys(tmux_target, message)

        # Wait for the pasted content to appear on screen
        self._wait_for_paste_visible(tmux_target, message)

        # Send Enter and wait for submission signal
        self._send_enter_and_wait(tmux_target)

    def _capture_pane_content(self, tmux_target: str) -> str | None:
        """Capture the current pane content, returning None on failure."""
        return capture_tmux_pane_content(
            self.host,
            tmux_target,
            timeout_seconds=_CAPTURE_PANE_TIMEOUT_SECONDS,
        )

    def _wait_for_tui_ready(self, tmux_target: str, indicator: str) -> None:
        """Wait until the TUI is ready by looking for the indicator string in the pane.

        This ensures the application's UI is fully rendered before we send input.
        Without this check, input sent too early may be lost or appear as raw text
        instead of being processed by the application's input handler.
        """
        with log_span("Waiting for TUI to be ready (looking for: {})", indicator):
            if not poll_until(
                lambda: self._check_pane_contains(tmux_target, indicator),
                timeout=_TUI_READY_TIMEOUT_SECONDS,
            ):
                pane_content = self._capture_pane_content(tmux_target)
                if pane_content is not None:
                    logger.error(
                        "TUI ready timeout -- remote pane content:\n{}",
                        pane_content,
                    )
                else:
                    logger.error("TUI ready timeout -- failed to capture remote pane content")
                raise SendMessageError(
                    str(self.name),
                    f"Timeout waiting for TUI to be ready (waited {_TUI_READY_TIMEOUT_SECONDS:.1f}s)"
                    + (f"\nPane content:\n{pane_content}" if pane_content else ""),
                )

    def _wait_for_paste_visible(self, tmux_target: str, message: str) -> None:
        """Wait until pasted content is confirmed visible in the tmux pane.

        Checks two conditions (either is sufficient):

        1. The tmux paste indicator ``[Pasted text `` appears on screen.
        2. A fuzzy content match: join all pane lines, strip to lowercase
           alphanumeric, and check that the last chunk of the message
           (similarly normalized) is present.
        """
        with log_span("Waiting for pasted content to appear"):
            if not poll_until(
                lambda: self._is_paste_visible(tmux_target, message),
                timeout=_SEND_MESSAGE_TIMEOUT_SECONDS,
            ):
                self._raise_send_timeout(
                    tmux_target,
                    f"Timeout waiting for pasted content to appear (waited {_SEND_MESSAGE_TIMEOUT_SECONDS:.1f}s)",
                )

    def _is_paste_visible(self, tmux_target: str, message: str) -> bool:
        """Check whether the pasted message is visible in the pane.

        Delegates to the pure ``_check_paste_content`` function after
        capturing the pane. Returns False if the pane cannot be captured.
        """
        content = self._capture_pane_content(tmux_target)
        if content is None:
            return False
        return _check_paste_content(content, message)

    def _check_pane_contains(self, tmux_target: str, text: str) -> bool:
        """Check if the pane content contains the given text."""
        content = self._capture_pane_content(tmux_target)
        found = content is not None and text in content
        return found

    def _send_enter_and_wait(self, tmux_target: str) -> None:
        """Send Enter to submit the message and wait for the submission signal.

        Uses tmux wait-for to detect when the UserPromptSubmit hook fires.
        Raises SendMessageError if the signal is not received within the timeout.

        The wait_channel is derived from the pure session name (without window
        suffix) because the UserPromptSubmit hook signals using ``#S`` which
        is just the session name.
        """
        wait_channel = f"mng-submit-{self.session_name}"
        if self._send_enter_and_wait_for_signal(tmux_target, wait_channel):
            logger.debug("Message submitted successfully")
            return

        pane_content = self._capture_pane_content(tmux_target)
        if pane_content is not None:
            logger.error(
                "TUI send enter and wait timeout -- remote pane content:\n{}",
                pane_content,
            )
        else:
            logger.error("TUI send enter and wait timeout -- failed to capture remote pane content")

        self._raise_send_timeout(
            tmux_target,
            f"Timeout waiting for message submission signal (waited {self.enter_submission_timeout_seconds}s)",
        )

    def _send_enter_and_wait_for_signal(self, tmux_target: str, wait_channel: str) -> bool:
        """Send Enter and wait for the tmux wait-for signal from the hook.

        This starts waiting BEFORE sending Enter to avoid a race condition where
        the hook might fire before we start listening for the signal.

        The sequence is:
        1. Start tmux wait-for (with timeout) in background
        2. Send Enter
        3. Wait for the background process to complete

        Returns True if signal received, False if timeout.
        """
        timeout_secs = self.enter_submission_timeout_seconds
        cmd = (
            f"bash -c '"
            f'timeout {timeout_secs} tmux wait-for "$0" & W=$!; '
            f'tmux send-keys -t "$1" Enter; '
            f"wait $W"
            f"' {shlex.quote(wait_channel)} {shlex.quote(tmux_target)}"
        )
        start = time.time()
        result = self.host.execute_command(cmd, timeout_seconds=timeout_secs + 1)
        elapsed_ms = (time.time() - start) * 1000
        if result.success:
            logger.trace("Received submission signal in {:.0f}ms", elapsed_ms)
            return True
        logger.debug("Timeout waiting for submission signal on channel {}", wait_channel)
        return False

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
            result = self.host.execute_command(f"ls -1 '{plugin_dir}'", timeout_seconds=5.0)
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
        mng_ctx: MngContext,
    ) -> None:
        """Default implementation: no-op.

        Subclasses can override to validate preconditions before provisioning.
        """

    def get_provision_file_transfers(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> Sequence[FileTransferSpec]:
        """Default implementation: no file transfers.

        Subclasses can override to declare files to transfer during provisioning.
        """
        return []

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Default implementation: no-op.

        Subclasses can override to perform agent-type-specific provisioning.
        """

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
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
