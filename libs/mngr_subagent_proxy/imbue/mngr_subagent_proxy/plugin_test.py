"""Unit tests for the mngr_subagent_proxy plugin provisioning hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import PluginName
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy.data_types import SubagentProxyMode
from imbue.mngr_subagent_proxy.data_types import SubagentProxyPluginConfig
from imbue.mngr_subagent_proxy.plugin import SUBAGENT_PROXY_PLUGIN_NAME
from imbue.mngr_subagent_proxy.plugin import SubagentProxyChildConfig
from imbue.mngr_subagent_proxy.plugin import UnguardedProjectStopHookError
from imbue.mngr_subagent_proxy.plugin import UnsupportedSubagentHookError
from imbue.mngr_subagent_proxy.plugin import cascade_destroy_recorded_children
from imbue.mngr_subagent_proxy.plugin import on_after_provisioning
from imbue.mngr_subagent_proxy.plugin import on_before_agent_destroy
from imbue.mngr_subagent_proxy.testing import FakeAgent
from imbue.mngr_subagent_proxy.testing import FakeHost

# on_after_provisioning declares its third parameter as MngrContext, but
# ``_resolve_plugin_mode`` treats a None mngr_ctx as "use defaults" (PROXY
# mode), so PROXY-mode tests can pass None and skip building a full
# MngrContext. Tests that exercise DENY mode pass a real ctx instead.
# The untyped wrapper keeps the None sentinel from leaking argument-type
# noise to every call site.
_provision: Any = on_after_provisioning
_destroy: Any = on_before_agent_destroy


@pytest.fixture
def host_dir(tmp_path: Path) -> Path:
    """Standard ``host`` subdir under tmp_path; pre-created."""
    path = tmp_path / "host"
    path.mkdir()
    return path


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """Standard ``work`` subdir under tmp_path; pre-created."""
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def fake_host(host_dir: Path) -> FakeHost:
    """FakeHost rooted at ``host_dir``."""
    return FakeHost(host_dir)


def test_plugin_hooks_register_on_claude_agent(work_dir: Path, fake_host: FakeHost) -> None:
    """The plugin's provisioning hook wires up hooks and the proxy agent.

    This is the golden-path CI check: verify that invoking on_after_provisioning
    for a Claude agent writes the mngr-proxy agent definition and merges the
    python-module hooks into .claude/settings.local.json.
    """
    agent_id = AgentId.generate()
    agent = FakeAgent(agent_id, work_dir, ClaudeAgentConfig())

    _provision(agent, fake_host, None)

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
    assert python_prefix + "cleanup" in post_cmd
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


def test_plugin_raises_on_user_stop_hooks_for_subagent_proxy_child(work_dir: Path, fake_host: FakeHost) -> None:
    """A proxy-child agent with user-configured Stop/SubagentStop hooks raises UnsupportedSubagentHookError.

    The plugin doesn't know whether a user's Stop hook is meant to fire on
    every subagent turn or only at the outer end_turn, so it refuses to
    proceed rather than silently guess.
    """
    agent_id = AgentId.generate()
    agent = FakeAgent(
        agent_id,
        work_dir,
        SubagentProxyChildConfig(),
        name=AgentName("reviewer--subagent-code-review-abcd1234"),
    )
    _seed_settings_with_stop_hooks(work_dir)

    with pytest.raises(UnsupportedSubagentHookError):
        _provision(agent, fake_host, None)


def test_plugin_allows_mngr_baseline_stop_hook_for_subagent_proxy_child(work_dir: Path, fake_host: FakeHost) -> None:
    """A proxy-child agent inheriting only mngr-managed Stop hooks provisions cleanly.

    mngr_claude's readiness Stop hook (which runs wait_for_stop_hook.sh)
    is recognized as baseline and passed through without triggering the
    UnsupportedSubagentHookError.
    """
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '
                                    'bash "$MNGR_AGENT_STATE_DIR/commands/wait_for_stop_hook.sh"',
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n"
    )
    agent = FakeAgent(
        AgentId.generate(),
        work_dir,
        SubagentProxyChildConfig(),
        name=AgentName("reviewer--subagent-code-review-abcd1234"),
    )

    _provision(agent, fake_host, None)

    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "Stop" in hooks
    assert "PreToolUse" in hooks


def test_plugin_preserves_stop_hooks_for_top_level_agent(work_dir: Path, fake_host: FakeHost) -> None:
    """A plain top-level agent (no --subagent- infix) keeps its Stop/SubagentStop hooks."""
    agent_id = AgentId.generate()
    agent = FakeAgent(agent_id, work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))
    settings_path = _seed_settings_with_stop_hooks(work_dir)

    _provision(agent, fake_host, None)

    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "Stop" in hooks
    assert "SubagentStop" in hooks


def _seed_project_settings_with_unguarded_stop(work_dir: Path) -> Path:
    """Write .claude/settings.json with one user Stop hook that is not env-guarded."""
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo project-stop"}]}]}},
            indent=2,
        )
        + "\n"
    )
    return settings_path


def test_plugin_raises_on_unguarded_project_stop_hook(
    work_dir: Path, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An un-guarded Stop hook in .claude/settings.json blocks provisioning."""
    monkeypatch.delenv("MNGR_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS", raising=False)
    _seed_project_settings_with_unguarded_stop(work_dir)
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    with pytest.raises(UnguardedProjectStopHookError):
        _provision(agent, fake_host, None)


def test_plugin_allows_guarded_project_stop_hook(work_dir: Path, fake_host: FakeHost) -> None:
    """A Stop hook in settings.json that has the env-conditional guard provisions cleanly."""
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '[ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0; echo project-stop',
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n"
    )
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, None)


def test_plugin_project_stop_hook_check_can_be_bypassed_via_env(
    work_dir: Path, fake_host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the opt-out env var bypasses the un-guarded check."""
    monkeypatch.setenv("MNGR_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS", "1")
    _seed_project_settings_with_unguarded_stop(work_dir)
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, None)


def test_plugin_skips_non_claude_agents(work_dir: Path, fake_host: FakeHost) -> None:
    """Provisioning is a no-op for agents whose config is not ClaudeAgentConfig."""
    # Use a plain sentinel that is not a ClaudeAgentConfig instance.
    agent = FakeAgent(AgentId.generate(), work_dir, object())

    _provision(agent, fake_host, None)

    assert len(fake_host.written_files) == 0
    assert fake_host.executed_commands == []
    assert not (work_dir / ".claude").exists()


def test_plugin_preserves_readiness_user_prompt_submit_for_subagent_proxy_child(
    work_dir: Path, fake_host: FakeHost
) -> None:
    """mngr_claude's UserPromptSubmit readiness entry survives the subagent-proxy strip.

    The UserPromptSubmit entry contains two inner commands: one touches
    $MNGR_AGENT_STATE_DIR and one signals tmux. Both are prefixed with
    SESSION_GUARD (which contains MAIN_CLAUDE_SESSION_ID), so the entry
    must be recognized as mngr-managed and left in place rather than
    stripped along with user-configured hooks.
    """
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    session_guard = '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": session_guard + 'touch "$MNGR_AGENT_STATE_DIR/active"',
                                },
                                {
                                    "type": "command",
                                    "command": session_guard + "tmux wait-for -S mngr-submit 2>/dev/null || true",
                                },
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n"
    )
    agent = FakeAgent(
        AgentId.generate(),
        work_dir,
        SubagentProxyChildConfig(),
        name=AgentName("parent--subagent-slug-deadbeef"),
    )

    _provision(agent, fake_host, None)

    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "UserPromptSubmit" in hooks
    user_prompt_entries = hooks["UserPromptSubmit"]
    # The readiness entry survived with both inner commands intact.
    readiness_entries = [
        entry
        for entry in user_prompt_entries
        if any("MAIN_CLAUDE_SESSION_ID" in h.get("command", "") for h in entry.get("hooks", []))
    ]
    assert len(readiness_entries) == 1
    inner_commands = readiness_entries[0]["hooks"]
    assert len(inner_commands) == 2
    assert any("tmux wait-for" in h["command"] for h in inner_commands)
    assert any('touch "$MNGR_AGENT_STATE_DIR/active"' in h["command"] for h in inner_commands)


def test_cascade_destroy_recorded_children_fires_for_every_map_entry(tmp_path: Path) -> None:
    """cascade_destroy_recorded_children fans out a detached destroy per subagent_map entry."""
    state_dir = tmp_path / "state"
    map_dir = state_dir / "subagent_map"
    map_dir.mkdir(parents=True)
    (map_dir / "toolu_aaa.json").write_text(json.dumps({"target_name": "parent--subagent-a-aaa"}))
    (map_dir / "toolu_bbb.json").write_text(json.dumps({"target_name": "parent--subagent-b-bbb"}))
    (map_dir / "toolu_bad.json").write_text("{not json")  # malformed -- skipped
    (map_dir / "ignored.txt").write_text("not a json file")

    calls: list[tuple[str, Path]] = []
    cascade_destroy_recorded_children(
        state_dir,
        AgentName("parent"),
        destroy_callable=lambda name, log: calls.append((name, log)),
    )

    target_names = sorted(name for name, _ in calls)
    assert target_names == ["parent--subagent-a-aaa", "parent--subagent-b-bbb"]
    assert all(log == state_dir / "subagent_cascade_destroy.log" for _, log in calls)


def test_on_before_agent_destroy_skips_non_claude_agents(host_dir: Path, work_dir: Path, fake_host: FakeHost) -> None:
    """The cascade hook is a no-op for non-Claude agents (even if a subagent_map exists)."""
    agent_id = AgentId.generate()
    state_dir = host_dir / "agents" / str(agent_id)
    map_dir = state_dir / "subagent_map"
    map_dir.mkdir(parents=True)
    (map_dir / "toolu_xxx.json").write_text(json.dumps({"target_name": "should-not-be-destroyed"}))

    agent = FakeAgent(agent_id, work_dir, object(), name=AgentName("non-claude"))
    # Should not raise, should not produce a cascade log file.
    _destroy(agent, fake_host)
    assert not (state_dir / "subagent_cascade_destroy.log").exists()


def test_cascade_destroy_recorded_children_no_op_without_map_dir(tmp_path: Path) -> None:
    """No subagent_map dir means no destroys fired and no log file created."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    calls: list[str] = []
    cascade_destroy_recorded_children(
        state_dir,
        AgentName("parent-no-children"),
        destroy_callable=lambda name, log: calls.append(name),
    )

    assert calls == []
    assert not (state_dir / "subagent_cascade_destroy.log").exists()


def _ctx_with_plugin_config(base_ctx: MngrContext, config: SubagentProxyPluginConfig) -> MngrContext:
    """Return a copy of ``base_ctx`` with the given subagent_proxy config injected."""
    updated_config = base_ctx.config.model_copy_update(
        to_update(base_ctx.config.field_ref().plugins, {PluginName(SUBAGENT_PROXY_PLUGIN_NAME): config}),
    )
    return base_ctx.model_copy_update(to_update(base_ctx.field_ref().config, updated_config))


def test_deny_mode_installs_only_pretooluse_deny_hook(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """In DENY mode, on_after_provisioning installs only the deny hook -- nothing else.

    Specifically, no PostToolUse hook (no spawned children to clean up),
    no SessionStart reaper, no mngr-proxy.md agent definition, no plugin
    cache hooks.json walk.
    """
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    settings_path = work_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    # Only PreToolUse:Agent is installed.
    assert "PreToolUse" in hooks
    assert any(entry.get("matcher") == "Agent" for entry in hooks["PreToolUse"])
    pre_cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "imbue.mngr_subagent_proxy.hooks.deny" in pre_cmd
    # No PostToolUse, no SessionStart -- nothing to clean up because no
    # subagent is ever spawned automatically.
    assert "PostToolUse" not in hooks
    assert "SessionStart" not in hooks


def test_deny_mode_does_not_write_proxy_agent_definition(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """In DENY mode, the mngr-proxy.md agent definition is NOT written.

    The Haiku dispatcher is part of PROXY mode only. Writing it in deny
    mode would dirty the worktree with a file the user never invokes.
    """
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    proxy_md = work_dir / ".claude" / "agents" / "mngr-proxy.md"
    assert not proxy_md.exists()


def test_deny_mode_writes_mngr_subagents_skill(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """DENY mode provisions the ``mngr-subagents`` Claude skill at .claude/skills/.

    The skill carries the verbose context (when to use, how to parse
    subagent_wait output, how to inspect a running subagent, etc.) so
    the deny hook's permissionDecisionReason can stay short.
    """
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    skill_path = work_dir / ".claude" / "skills" / "mngr-subagents" / "SKILL.md"
    assert skill_path.is_file()
    body = skill_path.read_text()
    # Frontmatter wires the skill into Claude Code's skill-discovery mechanism.
    assert body.startswith("---\n")
    assert "name: mngr-subagents" in body
    assert "description:" in body
    # Body must teach the explicit two-command spawn-and-wait protocol --
    # this is the single source of truth for how Claude delegates work
    # in DENY mode (the deny hook just points back at this skill).
    assert "uv run mngr create" in body
    assert "subagent_wait" in body
    assert "END_TURN:" in body
    # Depth-env propagation is load-bearing: without it, a chain of
    # subagents spawned through the skill protocol would bypass the
    # depth-limit guard.
    assert "MNGR_SUBAGENT_DEPTH" in body
    # Permission dialogs and backgrounding are documented secondary
    # concerns; pin them so the skill keeps that coverage.
    assert "NEED_PERMISSION" in body
    assert "run_in_background" in body


def test_proxy_mode_does_not_write_mngr_subagents_skill(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """PROXY mode does NOT write the deny-mode skill.

    The skill explains a workflow (Claude runs Bash to spawn) that
    only applies in DENY mode. In PROXY mode Claude calls Task as
    usual; surfacing the skill would be confusing.
    """
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.PROXY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    skill_path = work_dir / ".claude" / "skills" / "mngr-subagents" / "SKILL.md"
    assert not skill_path.exists()


def test_deny_mode_skips_project_stop_hook_check(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In DENY mode, un-guarded project-level Stop hooks do NOT block provisioning.

    The project-stop-hook check exists to prevent runaway loops in
    spawned proxy children. Deny mode never spawns proxy children, so
    the check is irrelevant -- and refusing to provision over it would
    be a regression vs. PROXY mode users who simply opt out of the
    proxy feature entirely.
    """
    monkeypatch.delenv("MNGR_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS", raising=False)
    _seed_project_settings_with_unguarded_stop(work_dir)
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    # Must NOT raise -- the project-stop-hook check is gated behind PROXY mode.
    _provision(agent, fake_host, ctx)


def test_deny_mode_does_not_strip_subagent_user_hooks(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """In DENY mode, even a mngr-proxy-child agent gets only the deny hook.

    A user could conceivably set deny mode and still call `mngr create
    --type mngr-proxy-child`. The existing strip / auto-allow logic for
    proxy children is gated behind PROXY mode -- in deny mode it must
    not run, otherwise we'd be doing PROXY-mode work in DENY mode.
    """
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user-stop"}]}]}},
            indent=2,
        )
        + "\n"
    )
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(
        AgentId.generate(),
        work_dir,
        SubagentProxyChildConfig(),
        name=AgentName("reviewer--subagent-foo-deadbeef"),
    )

    # PROXY mode would raise UnsupportedSubagentHookError here; DENY mode
    # leaves the user's Stop hook alone and proceeds.
    _provision(agent, fake_host, ctx)

    settings = json.loads((claude_dir / "settings.local.json").read_text())
    # The user's Stop hook is still present, untouched.
    assert any(
        h.get("command") == "echo user-stop"
        for entry in settings["hooks"].get("Stop", [])
        for h in entry.get("hooks", [])
    )


def test_proxy_mode_default_when_config_absent(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """A context with no plugin config defaults to PROXY mode (the original behavior).

    Plugin loading must not flip behavior for users who haven't opted
    in to deny mode.
    """
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    # temp_mngr_ctx has no subagent_proxy plugin config -- defaults apply.
    _provision(agent, fake_host, temp_mngr_ctx)

    settings = json.loads((work_dir / ".claude" / "settings.local.json").read_text())
    hooks = settings["hooks"]
    # PROXY mode installs all three hook events.
    assert "PreToolUse" in hooks
    assert "PostToolUse" in hooks
    assert "SessionStart" in hooks


def test_explicit_proxy_mode_installs_full_proxy_hooks(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """An explicit ``mode = PROXY`` config behaves the same as the absent-config default."""
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.PROXY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    settings = json.loads((work_dir / ".claude" / "settings.local.json").read_text())
    hooks = settings["hooks"]
    pre_cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "imbue.mngr_subagent_proxy.hooks.spawn" in pre_cmd
    assert "PostToolUse" in hooks
    assert "SessionStart" in hooks


def test_deny_mode_merges_into_existing_settings_local_json(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """Deny mode preserves pre-existing entries in settings.local.json.

    mngr_claude provisioning writes its readiness / user-prompt hooks
    before this plugin runs (we are ``trylast``). Deny mode must merge
    its single PreToolUse:Agent entry without clobbering those.
    """
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '
                                    "touch $MNGR_AGENT_STATE_DIR/active",
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n"
    )
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    settings = json.loads((claude_dir / "settings.local.json").read_text())
    hooks = settings["hooks"]
    assert "UserPromptSubmit" in hooks
    assert "PreToolUse" in hooks


def test_plugin_strip_hooks_is_safe_when_settings_missing(work_dir: Path, fake_host: FakeHost) -> None:
    """A subagent-proxy-child agent with no pre-existing settings.local.json provisions without error."""
    agent_id = AgentId.generate()
    agent = FakeAgent(
        agent_id,
        work_dir,
        SubagentProxyChildConfig(),
        name=AgentName("parent--subagent-slug-deadbeef"),
    )

    _provision(agent, fake_host, None)

    # Provisioning still wrote the merged settings (PreToolUse/PostToolUse/SessionStart),
    # and the to-be-stripped keys were never present to begin with.
    settings_path = work_dir / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "Stop" not in hooks
    assert "SubagentStop" not in hooks
    assert "PreToolUse" in hooks
