from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import NoPermissionsAgentMixin
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude import hookimpl
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig

_TAIL_POLL_INTERVAL: float = 0.05
_TAIL_POLL_TIMEOUT: float = 300.0


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


@pure
def _is_result_event(line: str) -> bool:
    """Check if a stream-json line is a result event (signals completion)."""
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    return parsed.get("type") == "result"


def _yield_text_deltas_from_lines(lines: list[str]) -> Iterator[str]:
    """Yield text deltas parsed from stream-json lines, skipping blanks and non-delta events."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        text = extract_text_delta(stripped)
        if text is not None:
            yield text


class _StreamTailState(MutableModel):
    """Encapsulates mutable state for tailing a file via the host interface.

    Separated from HeadlessClaude to avoid lambda-in-loop closure issues (B023)
    when polling for file changes.
    """

    stdout_path: Path
    host: OnlineHostInterface
    is_finished: Callable[[], bool]
    last_mtime: datetime | None = None
    chars_consumed: int = 0
    line_buffer: str = ""

    def _has_new_data_or_finished(self) -> bool:
        current_mtime = self.host.get_file_mtime(self.stdout_path)
        if current_mtime is not None and current_mtime != self.last_mtime:
            return True
        return self.is_finished()

    def tail_until_done(self) -> Iterator[str]:
        got_result = False
        while not got_result and not self.is_finished():
            poll_until(
                self._has_new_data_or_finished,
                timeout=_TAIL_POLL_TIMEOUT,
                poll_interval=_TAIL_POLL_INTERVAL,
            )
            self.last_mtime = self.host.get_file_mtime(self.stdout_path)

            try:
                content = self.host.read_text_file(self.stdout_path)
            except FileNotFoundError:
                continue

            raw = content[self.chars_consumed :]
            self.chars_consumed = len(content)

            if raw:
                combined = self.line_buffer + raw
                self.line_buffer = ""

                lines = combined.split("\n")
                if not combined.endswith("\n"):
                    self.line_buffer = lines.pop()

                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if _is_result_event(stripped):
                        got_result = True
                        break
                    text = extract_text_delta(stripped)
                    if text is not None:
                        yield text

        if not got_result:
            # Final drain after agent exits
            try:
                content = self.host.read_text_file(self.stdout_path)
            except FileNotFoundError:
                return
            remaining = self.line_buffer + content[self.chars_consumed :]
            if remaining:
                yield from _yield_text_deltas_from_lines(remaining.split("\n"))


class NoPermissionsClaudeAgent(ClaudeAgent, NoPermissionsAgentMixin):
    """ClaudeAgent with no permissions granted (no tools, no trust needed).

    Skips trust validation and dialog dismissal during provisioning since
    the agent cannot perform any actions that require permissions. All other
    provisioning (config dir setup, installation, hooks) runs normally.
    """

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """No-op: skip trust/dialog validation for no-permissions agents."""

    def interactively_dismiss_claude_dialogs(self, source_path: Path | None, mngr_ctx: MngrContext) -> None:
        """No-op: no permissions means no dialogs to check."""


class HeadlessClaudeAgentConfig(ClaudeAgentConfig):
    """Config for the headless_claude agent type.

    Disables sync_home_settings because headless agents are ephemeral and
    should not inherit user-level hooks (e.g. Stop hooks) from
    ~/.claude/settings.json.
    """

    sync_home_settings: bool = Field(
        default=False,
        description="Headless agents do not sync user settings from ~/.claude/ "
        "to avoid inheriting hooks (e.g. Stop hooks) that interfere with ephemeral operation.",
    )


class HeadlessClaude(NoPermissionsClaudeAgent, StreamingHeadlessAgentMixin):
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
        tracking. Redirects stdout to $MNGR_AGENT_STATE_DIR/stdout.jsonl.
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
        return CommandString(f'{cmd_str} > "$MNGR_AGENT_STATE_DIR/stdout.jsonl"')

    def _get_stdout_path(self) -> Path:
        """Return the path to the stdout.jsonl file for this agent."""
        return self._get_agent_dir() / "stdout.jsonl"

    def _is_agent_finished(self) -> bool:
        """Check if the agent process has exited (tmux lifecycle) or is no longer running."""
        state = self.get_lifecycle_state()
        return state in (AgentLifecycleState.STOPPED, AgentLifecycleState.DONE)

    def _file_exists_on_host(self, path: Path) -> bool:
        """Check if a file exists on the agent's host (works for both local and remote)."""
        return self.host.get_file_mtime(path) is not None

    def _wait_for_stdout_file(self, stdout_path: Path) -> bool:
        """Wait for the stdout file to be created or the agent to exit.

        Returns True if the file exists, False if the agent exited without creating it.
        """
        poll_until(
            lambda: self._file_exists_on_host(stdout_path) or self._is_agent_finished(),
            timeout=_TAIL_POLL_TIMEOUT,
            poll_interval=_TAIL_POLL_INTERVAL,
        )
        return self._file_exists_on_host(stdout_path)

    def output(self) -> str:
        """Wait for the agent to finish and return its complete output."""
        return "".join(self.stream_output())

    def stream_output(self) -> Iterator[str]:
        """Stream text output as it becomes available.

        Tails $MNGR_AGENT_STATE_DIR/stdout.jsonl via the host interface so it
        works for both local and remote hosts. Uses mtime checks via
        host.get_file_mtime() (through poll_until) to detect new data instead
        of busy-polling with time.sleep.

        Yields text delta chunks parsed from stream-json events. Completes when
        the agent process exits and the file is fully consumed.
        """
        stdout_path = self._get_stdout_path()

        if not self._wait_for_stdout_file(stdout_path):
            return

        state = _StreamTailState(
            stdout_path=stdout_path,
            host=self.host,
            is_finished=self._is_agent_finished,
        )
        yield from state.tail_until_done()


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_claude agent type."""
    return ("headless_claude", HeadlessClaude, HeadlessClaudeAgentConfig)
