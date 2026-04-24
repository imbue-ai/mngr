"""Test utilities for mngr_subagent_proxy: thin subclasses of the shared fakes."""

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic import Field

from imbue.mngr.api.testing import FakeAgent as BaseFakeAgent
from imbue.mngr.api.testing import FakeHost as BaseFakeHost
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId


class FakeAgent(BaseFakeAgent):
    """AgentInterface stub that adds id and agent_config for subagent-proxy tests."""

    id: AgentId = Field(description="Agent identifier")
    agent_config: Any = Field(description="Agent configuration (ClaudeAgentConfig or sentinel)")

    def __init__(self, agent_id: AgentId, work_dir: Path, agent_config: Any) -> None:
        BaseModel.__init__(self, id=agent_id, work_dir=work_dir, agent_config=agent_config)


class FakeHost(BaseFakeHost):
    """OnlineHostInterface stub that records writes and executed commands."""

    written_files: dict[Path, bytes] = Field(default_factory=dict, description="Files written via write_file")
    executed_commands: list[str] = Field(
        default_factory=list, description="Commands passed to execute_idempotent_command"
    )

    def __init__(self, host_dir: Path) -> None:
        super().__init__(host_dir=host_dir)

    def write_file(self, path: Path, content: bytes, mode: str | None = None) -> None:
        self.written_files[path] = content
        super().write_file(path, content, mode)

    def write_text_file(self, path: Path, content: str, encoding: str = "utf-8", mode: str | None = None) -> None:
        self.written_files[path] = content.encode(encoding)
        super().write_text_file(path, content, encoding, mode)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.executed_commands.append(command)
        completed = subprocess.run(
            command,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            success=completed.returncode == 0,
        )
