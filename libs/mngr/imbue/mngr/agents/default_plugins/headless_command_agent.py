from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Callable
from typing import Never

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until

_TAIL_POLL_INTERVAL: float = 0.05
_TAIL_POLL_TIMEOUT: float = 300.0


class _RawStreamTailState(MutableModel):
    """Tails a raw text file via the host interface, yielding new content as it appears."""

    stdout_path: Path
    host: OnlineHostInterface
    is_finished: Callable[[], bool]
    last_mtime: datetime | None = None
    chars_consumed: int = 0

    def _has_new_data_or_finished(self) -> bool:
        current_mtime = self.host.get_file_mtime(self.stdout_path)
        if current_mtime is not None and current_mtime != self.last_mtime:
            return True
        return self.is_finished()

    def _read_new_content(self) -> str | None:
        """Read any new content from the file since the last read."""
        self.last_mtime = self.host.get_file_mtime(self.stdout_path)

        try:
            content = self.host.read_text_file(self.stdout_path)
        except FileNotFoundError:
            return None

        raw = content[self.chars_consumed :]
        self.chars_consumed = len(content)
        return raw if raw else None

    def tail_until_done(self) -> Iterator[str]:
        """Poll for file changes and yield raw text chunks until the agent finishes."""
        while not self.is_finished():
            poll_until(
                self._has_new_data_or_finished,
                timeout=_TAIL_POLL_TIMEOUT,
                poll_interval=_TAIL_POLL_INTERVAL,
            )

            new_content = self._read_new_content()
            if new_content:
                yield new_content

        # Final drain after agent exits
        new_content = self._read_new_content()
        if new_content:
            yield new_content


class HeadlessCommandConfig(AgentTypeConfig):
    """Config for the headless_command agent type."""


class HeadlessCommand(BaseAgent[HeadlessCommandConfig], StreamingHeadlessAgentMixin):
    """Agent type that runs an arbitrary command headlessly and captures its output.

    Redirects stdout/stderr to files so callers can read output programmatically
    via stream_output(). Does not support interactive messages, paste detection,
    or TUI readiness checking.
    """

    def _preflight_send_message(self, tmux_target: str) -> None:
        """Headless command agents do not accept interactive messages."""
        raise SendMessageError(
            str(self.name),
            "Headless command agents do not accept interactive messages.",
        )

    def uses_paste_detection_send(self) -> bool:
        return False

    def get_tui_ready_indicator(self) -> str | None:
        return None

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        raise NotImplementedError(
            "HeadlessCommand agents do not support wait_for_ready_signal. "
            "The command receives its input via CLI args, not via send_message."
        )

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Build the command with stdout/stderr redirected to files."""
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            # Fall back to using the agent type as a command (documented "Direct command" behavior)
            base = str(self.agent_type)

        parts = [base]

        all_extra_args = self.agent_config.cli_args + agent_args
        if all_extra_args:
            parts.extend(all_extra_args)

        cmd_str = " ".join(parts)
        return CommandString(f'{cmd_str} > "$MNGR_AGENT_STATE_DIR/stdout.log" 2> "$MNGR_AGENT_STATE_DIR/stderr.log"')

    def _get_stdout_path(self) -> Path:
        return self._get_agent_dir() / "stdout.log"

    def _get_stderr_path(self) -> Path:
        return self._get_agent_dir() / "stderr.log"

    def _is_agent_finished(self) -> bool:
        state = self.get_lifecycle_state()
        return state in (AgentLifecycleState.STOPPED, AgentLifecycleState.DONE)

    def _file_exists_on_host(self, path: Path) -> bool:
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

    def _raise_no_output_error(self) -> Never:
        """Raise MngrError collecting all available error detail.

        Checks stderr, then falls back to tmux pane capture if neither
        redirect file exists (the shell never ran).
        """
        parts: list[str] = []

        # Check stderr for error content
        stderr_error = self._get_stderr_error_message()
        if stderr_error:
            parts.append(stderr_error)

        # Fall back to pane capture if neither file exists
        if not parts:
            is_stderr_exists = self._file_exists_on_host(self._get_stderr_path())
            is_stdout_exists = self._file_exists_on_host(self._get_stdout_path())
            if not is_stderr_exists and not is_stdout_exists:
                pane_error = self._get_pane_error_message()
                if pane_error:
                    parts.append(pane_error)

        if parts:
            detail = "\n".join(parts)
            raise MngrError(f"Command exited without producing output:\n{detail}")
        raise MngrError("Command exited without producing output (no details available)")

    def _get_stderr_error_message(self) -> str | None:
        """Read stderr.log for error output from the command."""
        stderr_path = self._get_stderr_path()
        try:
            content = self.host.read_text_file(stderr_path)
        except FileNotFoundError:
            return None
        stripped = content.strip()
        return stripped if stripped else None

    def _get_pane_error_message(self) -> str | None:
        """Capture the tmux pane content as a last-resort error source."""
        content = self.capture_pane_content()
        if content is None:
            return None
        stripped = content.strip()
        return stripped if stripped else None

    def stream_output(self) -> Iterator[str]:
        """Stream raw text output as it becomes available.

        Tails $MNGR_AGENT_STATE_DIR/stdout.log via the host interface so it
        works for both local and remote hosts. Yields raw text chunks.

        Raises MngrError if the agent exits without producing any output.
        """
        stdout_path = self._get_stdout_path()

        if not self._wait_for_stdout_file(stdout_path):
            self._raise_no_output_error()

        state = _RawStreamTailState(
            stdout_path=stdout_path,
            host=self.host,
            is_finished=self._is_agent_finished,
        )
        is_yielded_any = False
        for chunk in state.tail_until_done():
            is_yielded_any = True
            yield chunk

        if not is_yielded_any:
            self._raise_no_output_error()


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_command agent type."""
    return ("headless_command", HeadlessCommand, HeadlessCommandConfig)
