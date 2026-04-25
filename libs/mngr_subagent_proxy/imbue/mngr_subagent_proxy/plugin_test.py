"""Acceptance test for the mngr_subagent_proxy plugin provisioning hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy.plugin import on_after_provisioning
from imbue.mngr_subagent_proxy.testing import FakeAgent
from imbue.mngr_subagent_proxy.testing import FakeHost

# on_after_provisioning declares its third parameter as MngrContext but
# immediately ``del``-s it. Tests pass through an untyped wrapper so the
# None sentinel doesn't leak argument-type noise to every call site.
_provision: Any = on_after_provisioning


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

    _provision(agent, host, None)

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


def _seed_settings_with_stop_hooks(work_dir: Path) -> Path:
    """Write a settings.local.json containing Stop and SubagentStop hook entries.

    Mirrors the pre-existing state that mngr_claude's provisioning (plus a
    user-configured Stop hook like imbue-code-guardian's stop-hook
    orchestrator) would leave on disk before on_after_provisioning runs.
    """
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.local.json"
    seed = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
            "SubagentStop": [{"hooks": [{"type": "command", "command": "echo subagent-stop"}]}],
        }
    }
    settings_path.write_text(json.dumps(seed, indent=2) + "\n")
    return settings_path


@pytest.mark.acceptance
def test_plugin_strips_stop_hooks_for_subagent_proxy_child(tmp_path: Path) -> None:
    """A proxy-child agent (name contains --subagent-) has Stop/SubagentStop stripped after provisioning."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    host = FakeHost(host_dir)
    agent_id = AgentId.generate()
    agent = FakeAgent(
        agent_id,
        work_dir,
        ClaudeAgentConfig(),
        name=AgentName("reviewer--subagent-code-review-abcd1234"),
    )
    settings_path = _seed_settings_with_stop_hooks(work_dir)

    _provision(agent, host, None)

    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "Stop" not in hooks
    assert "SubagentStop" not in hooks
    # The usual proxy hooks were still merged in.
    assert any(entry.get("matcher") == "Agent" for entry in hooks["PreToolUse"])


@pytest.mark.acceptance
def test_plugin_preserves_stop_hooks_for_top_level_agent(tmp_path: Path) -> None:
    """A plain top-level agent (no --subagent- infix) keeps its Stop/SubagentStop hooks."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    host = FakeHost(host_dir)
    agent_id = AgentId.generate()
    agent = FakeAgent(agent_id, work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))
    settings_path = _seed_settings_with_stop_hooks(work_dir)

    _provision(agent, host, None)

    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "Stop" in hooks
    assert "SubagentStop" in hooks


@pytest.mark.acceptance
def test_plugin_strip_hooks_is_safe_when_settings_missing(tmp_path: Path) -> None:
    """A subagent-proxy-child agent with no pre-existing settings.local.json provisions without error."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    host = FakeHost(host_dir)
    agent_id = AgentId.generate()
    agent = FakeAgent(
        agent_id,
        work_dir,
        ClaudeAgentConfig(),
        name=AgentName("parent--subagent-slug-deadbeef"),
    )

    _provision(agent, host, None)

    # Provisioning still wrote the merged settings (PreToolUse/PostToolUse/SessionStart),
    # and the to-be-stripped keys were never present to begin with.
    settings_path = work_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "Stop" not in hooks
    assert "SubagentStop" not in hooks
    assert "PreToolUse" in hooks
