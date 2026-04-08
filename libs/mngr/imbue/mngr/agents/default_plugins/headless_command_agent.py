from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Callable

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_headless_agent import BaseHeadlessAgent
from imbue.mngr.agents.base_headless_agent import TAIL_POLL_INTERVAL
from imbue.mngr.agents.base_headless_agent import TAIL_POLL_TIMEOUT
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until


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
                timeout=TAIL_POLL_TIMEOUT,
                poll_interval=TAIL_POLL_INTERVAL,
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


class HeadlessCommand(BaseHeadlessAgent[HeadlessCommandConfig]):
    """Agent type that runs an arbitrary command headlessly and captures its output.

    Redirects stdout/stderr to files so callers can read output programmatically
    via stream_output(). Does not support interactive messages, paste detection,
    or TUI readiness checking.
    """

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Build the command with stdout/stderr redirected to files."""
        base_command = super().assemble_command(host, agent_args, command_override)
        return CommandString(
            f'{base_command} > "$MNGR_AGENT_STATE_DIR/stdout.log" 2> "$MNGR_AGENT_STATE_DIR/stderr.log"'
        )

    def _get_stdout_path(self) -> Path:
        return self._get_agent_dir() / "stdout.log"

    def _get_stderr_path(self) -> Path:
        return self._get_agent_dir() / "stderr.log"

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
