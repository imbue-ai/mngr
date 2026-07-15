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

    def execute_stateful_command(self, command: str, **_: object) -> CommandResult:
        self.captured.append(command)
        if self.scripted_results:
            return self.scripted_results.pop(0)
        return CommandResult(stdout="", stderr="", success=True)
