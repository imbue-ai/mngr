"""Acceptance test for the mngr_subagent_proxy plugin provisioning hooks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy.plugin import on_after_provisioning


class _FakeHost:
    """Minimal OnlineHostInterface stub for the plugin acceptance test."""

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


class _FakeAgent:
    """Minimal AgentInterface stub carrying id, work_dir, and agent_config."""

    def __init__(self, agent_id: AgentId, work_dir: Path, agent_config: Any) -> None:
        self.id = agent_id
        self.work_dir = work_dir
        self.agent_config = agent_config


@pytest.mark.acceptance
def test_plugin_hooks_register_on_claude_agent(tmp_path: Path) -> None:
    """The plugin's provisioning hook wires up hooks, scripts, and the proxy agent.

    This is the golden-path CI check: verify that invoking on_after_provisioning
    for a Claude agent writes the subagent-proxy hook scripts, the mngr-proxy
    agent definition, and merges hooks into .claude/settings.local.json.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    host = _FakeHost(host_dir)
    agent_id = AgentId.generate()
    agent = _FakeAgent(agent_id, work_dir, ClaudeAgentConfig())

    on_after_provisioning(agent, host, None)  # type: ignore[arg-type]

    settings_path = work_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert any(entry.get("matcher") == "Agent" for entry in hooks["PreToolUse"])
    assert any(entry.get("matcher") == "Agent" for entry in hooks["PostToolUse"])
    assert "SessionStart" in hooks

    proxy_md = work_dir / ".claude" / "agents" / "mngr-proxy.md"
    assert proxy_md.exists()
    proxy_content = proxy_md.read_text()
    assert "model: haiku" in proxy_content

    commands_dir = host_dir / "agents" / str(agent_id) / "commands"
    for script_name in ("spawn_proxy_subagent.sh", "rewrite_subagent_result.sh", "reap_orphan_subagents.sh"):
        script_path = commands_dir / script_name
        assert script_path.exists(), f"missing {script_name}"
