"""Shared in-memory host mock for agent unit tests (imported explicitly, defines no tests)."""

from pathlib import Path

import pydantic

from imbue.mngr.interfaces.data_types import CommandResult


class ScriptedHost(pydantic.BaseModel):
    """In-memory host stub: records commands and replays scripted results (then succeeds).

    ``host_dir`` exists so ``BaseAgent._get_agent_dir`` (and anything built on
    it, e.g. ``record_message_delivery_event``) resolves to a stable fake path.
    """

    host_dir: Path = pydantic.Field(default=Path("/tmp/fake-mngr-host"))
    captured: list[str] = pydantic.Field(default_factory=list)
    scripted_results: list[CommandResult] = pydantic.Field(default_factory=list)
    # The agent pane's recorded ID, as `tmux show-options` would answer. None models a session
    # created before mngr recorded one, where tmux answers `invalid option:` and the send falls
    # back to the window target.
    pane_id: str | None = None

    # Send preflight, not the command under test. Resolving the agent's pane and leaving copy-mode
    # run before every send, so serving them outside the scripted queue keeps that queue lined up
    # with the command each test actually means.
    _PREFLIGHT_PREFIXES = ("tmux show-options", "tmux copy-mode")

    def execute_stateful_command(self, command: str, **_: object) -> CommandResult:
        self.captured.append(command)
        if command.startswith("tmux show-options"):
            if self.pane_id is None:
                return CommandResult(stdout="", stderr="invalid option: @mngr_agent_pane", success=False)
            return CommandResult(stdout=f"{self.pane_id}\n", stderr="", success=True)
        if command.startswith(self._PREFLIGHT_PREFIXES):
            return CommandResult(stdout="", stderr="", success=True)
        if self.scripted_results:
            return self.scripted_results.pop(0)
        return CommandResult(stdout="", stderr="", success=True)

    @property
    def sent_commands(self) -> list[str]:
        """Captured commands with send preflight filtered out."""
        return [c for c in self.captured if not c.startswith(self._PREFLIGHT_PREFIXES)]
