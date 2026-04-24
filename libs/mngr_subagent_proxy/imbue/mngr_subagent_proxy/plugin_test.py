"""Acceptance test for the mngr_subagent_proxy plugin provisioning hooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy.plugin import on_after_provisioning
from imbue.mngr_subagent_proxy.testing import FakeAgent
from imbue.mngr_subagent_proxy.testing import FakeHost


@pytest.mark.acceptance
def test_plugin_hooks_register_on_claude_agent(tmp_path: Path) -> None:
    """The plugin's provisioning hook wires up hooks and the proxy agent.

    This is the golden-path CI check: verify that invoking on_after_provisioning
    for a Claude agent writes the mngr-proxy agent definition and merges the
    python-module hooks into .claude/settings.local.json.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    host = FakeHost(host_dir)
    agent_id = AgentId.generate()
    agent = FakeAgent(agent_id, work_dir, ClaudeAgentConfig())

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

    python_prefix = "uv run python -m imbue.mngr_subagent_proxy.hooks."
    pre_cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    post_cmd = hooks["PostToolUse"][0]["hooks"][0]["command"]
    session_cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert python_prefix + "spawn" in pre_cmd
    assert python_prefix + "rewrite" in post_cmd
    assert python_prefix + "reap" in session_cmd
