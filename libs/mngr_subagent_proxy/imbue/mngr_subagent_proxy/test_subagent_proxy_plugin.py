"""Release tests for the mngr_subagent_proxy plugin provisioning hooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy.plugin import build_subagent_proxy_hooks_config
from imbue.mngr_subagent_proxy.plugin import on_after_provisioning
from imbue.mngr_subagent_proxy.testing import FakeAgent
from imbue.mngr_subagent_proxy.testing import FakeHost


@pytest.mark.release
def test_build_hooks_config_shape() -> None:
    """build_subagent_proxy_hooks_config returns a well-formed hooks dict."""
    config = build_subagent_proxy_hooks_config()

    assert "hooks" in config
    hooks = config["hooks"]

    assert set(hooks.keys()) == {"PreToolUse", "PostToolUse", "SessionStart"}

    guard_prefix = '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0'
    commands_prefix = "$MNGR_AGENT_STATE_DIR/commands/"

    pre = hooks["PreToolUse"]
    assert len(pre) >= 1
    assert pre[0]["matcher"] == "Agent"
    pre_hook = pre[0]["hooks"][0]
    assert commands_prefix in pre_hook["command"]
    assert guard_prefix in pre_hook["command"]
    assert pre_hook["timeout"] == 15

    post = hooks["PostToolUse"]
    assert len(post) >= 1
    assert post[0]["matcher"] == "Agent"
    post_hook = post[0]["hooks"][0]
    assert commands_prefix in post_hook["command"]
    assert guard_prefix in post_hook["command"]
    assert post_hook["timeout"] == 15

    session = hooks["SessionStart"]
    assert len(session) >= 1
    session_hook = session[0]["hooks"][0]
    assert commands_prefix in session_hook["command"]
    assert guard_prefix in session_hook["command"]


@pytest.mark.release
def test_on_after_provisioning_writes_hooks_and_scripts(tmp_path: Path) -> None:
    """Provisioning writes hooks, scripts, and the proxy agent definition."""
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
    assert len(hooks["SessionStart"]) >= 1

    proxy_md = work_dir / ".claude" / "agents" / "mngr-proxy.md"
    assert proxy_md.exists()
    proxy_content = proxy_md.read_text()
    assert "model: haiku" in proxy_content
    assert "tools: Bash" in proxy_content

    commands_dir = host_dir / "agents" / str(agent_id) / "commands"
    for script_name in ("spawn_proxy_subagent.sh", "rewrite_subagent_result.sh", "reap_orphan_subagents.sh"):
        script_path = commands_dir / script_name
        assert script_path.exists(), f"missing {script_name}"
        assert script_path.read_text().startswith("#!/usr/bin/env bash")


@pytest.mark.release
def test_on_after_provisioning_skips_non_claude_agents(tmp_path: Path) -> None:
    """Provisioning is a no-op for agents whose config is not ClaudeAgentConfig."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    host = FakeHost(host_dir)
    # Use a plain sentinel that is not a ClaudeAgentConfig instance.
    agent = FakeAgent(AgentId.generate(), work_dir, object())

    on_after_provisioning(agent, host, None)  # type: ignore[arg-type]

    assert len(host.written_files) == 0
    assert host.executed_commands == []
    assert not (work_dir / ".claude").exists()
