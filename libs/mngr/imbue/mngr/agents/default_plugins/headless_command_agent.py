from __future__ import annotations

from pathlib import Path

from imbue.mngr import hookimpl
from imbue.mngr.agents.base_headless_agent import BaseHeadlessAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.live_output import LiveOutputReader
from imbue.mngr.interfaces.live_output import RawTextReader
from imbue.mngr.primitives import CommandString


class HeadlessCommandConfig(AgentTypeConfig):
    """Config for the headless_command agent type."""


class HeadlessCommand(BaseHeadlessAgent[HeadlessCommandConfig]):
    """Agent type that runs an arbitrary command headlessly and captures its output.

    Redirects stdout/stderr to files so callers can read output programmatically
    via stream_output(). Does not support interactive messages, paste detection,
    or TUI readiness checking. It does not wrap a known CLI (so the CLI-oriented
    capabilities are ``n/a``) and runs unattended by construction via
    ``BaseHeadlessAgent``.
    """

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Build the command with stdout/stderr redirected to files.

        ``HeadlessCommand`` has no prompt-file protocol, so ``--message``
        content cannot be delivered; the default ``stage_initial_message``
        logs a warning when it is supplied.
        """
        base_command = super().assemble_command(host, agent_args, command_override, initial_message=initial_message)
        return CommandString(
            f'{base_command} > "$MNGR_AGENT_STATE_DIR/stdout.log" 2> "$MNGR_AGENT_STATE_DIR/stderr.log"'
        )

    def _get_stdout_path(self) -> Path:
        return self._get_agent_dir() / "stdout.log"

    def _get_stderr_path(self) -> Path:
        return self._get_agent_dir() / "stderr.log"

    def make_live_output_reader(self) -> LiveOutputReader:
        """Stream the captured stdout as raw text, emitting newly-appended content."""
        return RawTextReader()


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_command agent type."""
    return ("headless_command", HeadlessCommand, HeadlessCommandConfig)
