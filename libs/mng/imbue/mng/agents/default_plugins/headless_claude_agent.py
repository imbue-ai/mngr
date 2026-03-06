from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng import hookimpl
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.errors import NoCommandDefinedError
from imbue.mng.errors import SendMessageError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.agent import HeadlessAgentMixin
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import CommandString
from imbue.mng.utils.polling import poll_until

_TAIL_POLL_INTERVAL: float = 0.05
_TAIL_POLL_TIMEOUT: float = 300.0


class _FileMtimeTracker(MutableModel):
    """Tracks a file's mtime and size to detect changes without polling content."""

    path: Path
    last_mtime: float = 0
    last_size: int = 0

    def has_changed(self) -> bool:
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        if st.st_mtime != self.last_mtime or st.st_size != self.last_size:
            self.last_mtime = st.st_mtime
            self.last_size = st.st_size
            return True
        return False


@pure
def extract_text_delta(line: str) -> str | None:
    """Extract text from a stream-json content_block_delta event.

    Returns the delta text if the line is a content_block_delta with a text_delta,
    or None otherwise.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if parsed.get("type") != "stream_event":
        return None

    event = parsed.get("event")
    if not isinstance(event, dict):
        return None

    if event.get("type") != "content_block_delta":
        return None

    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None

    if delta.get("type") != "text_delta":
        return None

    text = delta.get("text")
    if isinstance(text, str):
        return text

    return None


def _yield_text_deltas_from_lines(lines: list[str]) -> Iterator[str]:
    """Yield text deltas parsed from stream-json lines, skipping blanks and non-delta events."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        text = extract_text_delta(stripped)
        if text is not None:
            yield text


class HeadlessClaudeAgentConfig(ClaudeAgentConfig):
    """Config for the headless_claude agent type.

    Inherits all ClaudeAgentConfig fields (sync settings, credentials, etc.).
    Command defaults to 'claude'.
    """

    command: CommandString = Field(
        default=CommandString("claude"),
        description="Command to run headless claude agent",
    )


class HeadlessClaude(ClaudeAgent, HeadlessAgentMixin):
    """Agent type for non-interactive (headless) Claude usage.

    Runs `claude --print` with stdout redirected to a file so callers can
    read output programmatically via stream_output(). Does not support
    interactive messages, paste detection, or TUI readiness checking.
    """

    def _preflight_send_message(self, tmux_target: str) -> None:
        """Headless agents do not accept interactive messages."""
        raise SendMessageError(
            str(self.name),
            "Headless claude agents do not accept interactive messages.",
        )

    def uses_paste_detection_send(self) -> bool:
        return False

    def get_tui_ready_indicator(self) -> str | None:
        return None

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        raise NotImplementedError(
            "HeadlessClaude agents do not support wait_for_ready_signal. "
            "The prompt is passed as a CLI arg, not via send_message."
        )

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Build a simplified command for headless operation.

        Always includes --print, no session resumption, no background activity
        tracking. Redirects stdout to $MNG_AGENT_STATE_DIR/stdout.jsonl.
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            raise NoCommandDefinedError(f"No command defined for agent type '{self.agent_type}'")

        parts = [base, "--print"]

        all_extra_args = self.agent_config.cli_args + agent_args
        if all_extra_args:
            parts.extend(all_extra_args)

        cmd_str = " ".join(parts)
        return CommandString(f'{cmd_str} > "$MNG_AGENT_STATE_DIR/stdout.jsonl"')

    def _get_stdout_path(self) -> Path:
        """Return the path to the stdout.jsonl file for this agent."""
        return self._get_agent_dir() / "stdout.jsonl"

    def _is_agent_finished(self) -> bool:
        state = self.get_lifecycle_state()
        return state in (AgentLifecycleState.STOPPED, AgentLifecycleState.DONE)

    def _wait_for_stdout_file(self, stdout_path: Path) -> bool:
        """Wait for the stdout file to be created or the agent to exit.

        Returns True if the file exists, False if the agent exited without creating it.
        """
        poll_until(
            lambda: stdout_path.exists() or self._is_agent_finished(),
            timeout=_TAIL_POLL_TIMEOUT,
            poll_interval=_TAIL_POLL_INTERVAL,
        )
        return stdout_path.exists()

    def stream_output(self) -> Iterator[str]:
        """Stream text output from the headless agent.

        Tails $MNG_AGENT_STATE_DIR/stdout.jsonl using a file handle kept open
        at the current read position. Uses mtime/size checks (via poll_until)
        to detect new data instead of busy-polling with time.sleep.

        Yields text delta chunks parsed from stream-json events. Completes when
        the agent process exits and the file is fully consumed.
        """
        stdout_path = self._get_stdout_path()

        if not self._wait_for_stdout_file(stdout_path):
            return

        tracker = _FileMtimeTracker(path=stdout_path)
        line_buffer = ""

        with open(stdout_path) as fh:
            while not self._is_agent_finished():
                poll_until(tracker.has_changed, timeout=_TAIL_POLL_TIMEOUT, poll_interval=_TAIL_POLL_INTERVAL)
                data = fh.read()
                if data:
                    data = line_buffer + data
                    line_buffer = ""

                    lines = data.split("\n")
                    if not data.endswith("\n"):
                        line_buffer = lines.pop()

                    yield from _yield_text_deltas_from_lines(lines)

            # Final drain after agent exits
            final_data = line_buffer + fh.read()
            if final_data:
                yield from _yield_text_deltas_from_lines(final_data.split("\n"))


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_claude agent type."""
    return ("headless_claude", HeadlessClaude, HeadlessClaudeAgentConfig)
