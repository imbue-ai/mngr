"""Shared test utilities for mngr_subagent_proxy tests.

Provides FakeHost and FakeAgent — minimal stubs of OnlineHostInterface and
AgentInterface respectively — for use in plugin unit and integration tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId


class FakeHost:
    """Minimal OnlineHostInterface stub for plugin tests.

    Records all file writes and executes idempotent commands locally so
    that real directory creation happens under the provided host_dir.
    """

    def __init__(self, host_dir: Path) -> None:
        self._host_dir = host_dir
        self.written_files: dict[Path, bytes] = {}
        self.executed_commands: list[str] = []

    @property
    def host_dir(self) -> Path:
        return self._host_dir

    def write_file(
        self,
        path: Path,
        content: bytes,
        mode: str | None = None,
        is_atomic: bool = False,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mode is not None:
            path.chmod(int(mode, 8))
        self.written_files[path] = content

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        self.write_file(path, content.encode(encoding), mode)

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        if not path.exists():
            raise FileNotFoundError(path)
        return path.read_text(encoding=encoding)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Any = None,
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


class FakeAgent:
    """Minimal AgentInterface stub carrying id, work_dir, and agent_config."""

    def __init__(self, agent_id: AgentId, work_dir: Path, agent_config: Any) -> None:
        self.id = agent_id
        self.work_dir = work_dir
        self.agent_config = agent_config
