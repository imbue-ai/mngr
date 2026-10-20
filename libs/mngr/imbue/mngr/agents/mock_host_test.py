"""Shared host mocks for agent unit tests (imported explicitly, defines no tests)."""

from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path

import pydantic

from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import CommandResult


def is_send_preflight_command(command: str) -> bool:
    """Whether a command is send preflight (resolving the agent's pane, leaving copy-mode) rather than the send itself.

    Preflight runs before every send, so a stub that scripts results for the send serves preflight
    outside its scripted queue. A mode-clear chained onto a write (`\\;`) is the write, so it is not
    preflight.
    """
    return command.startswith("tmux show-options") or (command.startswith("tmux if-shell") and "\\;" not in command)


class ScriptedHost(pydantic.BaseModel):
    """In-memory host stub: records commands and replays scripted results (then succeeds).

    ``host_dir`` exists so ``BaseAgent._get_agent_dir`` (and anything built on
    it, e.g. ``record_message_delivery_event``) resolves to a stable fake path.
    """

    host_dir: Path = pydantic.Field(default=Path("/tmp/fake-mngr-host"))
    captured: list[str] = pydantic.Field(default_factory=list)
    # The timeout_seconds each captured command was run with, in the same order.
    captured_timeout_seconds: list[float | None] = pydantic.Field(default_factory=list)
    scripted_results: list[CommandResult] = pydantic.Field(default_factory=list)
    # The agent pane's recorded ID, as `tmux show-options` would answer. None models a session
    # created before mngr recorded one, where tmux answers `invalid option:` and the send falls
    # back to the window target.
    pane_id: str | None = None

    def execute_stateful_command(
        self, command: str, timeout_seconds: float | None = None, **_: object
    ) -> CommandResult:
        self.captured.append(command)
        self.captured_timeout_seconds.append(timeout_seconds)
        if command.startswith("tmux show-options"):
            if self.pane_id is None:
                return CommandResult(stdout="", stderr="invalid option: @mngr_agent_pane", success=False)
            return CommandResult(stdout=f"{self.pane_id}\n", stderr="", success=True)
        if is_send_preflight_command(command):
            return CommandResult(stdout="", stderr="", success=True)
        if self.scripted_results:
            return self.scripted_results.pop(0)
        return CommandResult(stdout="", stderr="", success=True)

    @property
    def sent_commands(self) -> list[str]:
        """Captured commands with send preflight filtered out."""
        return [c for c in self.captured if not is_send_preflight_command(c)]


class HangingTmuxHost(Host):
    """A real local host on which every tmux command hangs, the way a wedged tmux server makes it.

    A tmux command runs a long sleep under the caller's timeout instead, so the caller sees what the
    local backend reports for a command that outlives its timeout. One run without a timeout would
    hang forever, so that fails loudly.
    """

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> CommandResult:
        if command.startswith("tmux "):
            assert timeout_seconds is not None, f"unbounded tmux command: {command}"
            return super().execute_stateful_command("sleep 48213", timeout_seconds=timeout_seconds)
        return super().execute_stateful_command(command, user, cwd, env, timeout_seconds, on_output)


class CommandCountingHost(Host):
    """A real local host that records every command it runs, each of which is one process spawn."""

    executed_commands: list[str] = pydantic.Field(default_factory=list)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        self.executed_commands.append(command)
        return super().execute_idempotent_command(command, user, cwd, env, timeout_seconds, raise_on_timeout)
