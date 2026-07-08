"""Unit tests for the mngr_claude_subagent_proxy plugin provisioning hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import PluginName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr_claude.claude_config import get_managed_settings_path
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_claude_subagent_proxy._stop_hook_guard import PROXY_CHILD_GUARD_PREFIX
from imbue.mngr_claude_subagent_proxy.data_types import SubagentProxyMode
from imbue.mngr_claude_subagent_proxy.data_types import SubagentProxyPluginConfig
from imbue.mngr_claude_subagent_proxy.plugin import CLAUDE_SUBAGENT_PROXY_PLUGIN_NAME
from imbue.mngr_claude_subagent_proxy.plugin import SubagentProxyChildConfig
from imbue.mngr_claude_subagent_proxy.plugin import UnguardedProjectStopHookError
from imbue.mngr_claude_subagent_proxy.plugin import UnignoredProxyArtifactError
from imbue.mngr_claude_subagent_proxy.plugin import UnsupportedSubagentHookError
from imbue.mngr_claude_subagent_proxy.plugin import _check_settings_local_gitignored
from imbue.mngr_claude_subagent_proxy.plugin import _guard_user_stop_hooks_in_project_settings
from imbue.mngr_claude_subagent_proxy.plugin import cascade_destroy_recorded_children
from imbue.mngr_claude_subagent_proxy.plugin import on_after_provisioning
from imbue.mngr_claude_subagent_proxy.plugin import on_before_agent_destroy
from imbue.mngr_claude_subagent_proxy.testing import FakeAgent
from imbue.mngr_claude_subagent_proxy.testing import FakeHost

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


def _settings_json_path(host_dir: Path, agent_id: AgentId) -> Path:
    """Path of the agent's per-agent config-dir settings.json (where proxy hooks go in normal mode)."""
    return get_agent_state_dir_path(host_dir, agent_id) / "plugin" / "claude" / "anthropic" / "settings.json"


def _managed_settings_path(host_dir: Path, agent_id: AgentId) -> Path:
    """Path of the agent's mngr-managed Claude --settings file (env-config-dir mode only)."""
    return get_managed_settings_path(get_agent_state_dir_path(host_dir, agent_id))


def test_plugin_hooks_register_on_claude_agent(work_dir: Path, fake_host: FakeHost) -> None:
    """The plugin's provisioning hook wires up hooks and the proxy agent.

    This is the golden-path CI check: verify that invoking on_after_provisioning
    for a Claude agent writes the mngr-proxy agent definition and merges the
    python-module hooks into the agent's mngr-managed settings file.
    """
    agent_id = AgentId.generate()
    agent = FakeAgent(agent_id, work_dir, ClaudeAgentConfig())

    _provision(agent, fake_host, None)

    settings_path = _settings_json_path(fake_host.host_dir, agent_id)
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert any(entry.get("matcher") == "Agent" for entry in hooks["PreToolUse"])
    assert any(entry.get("matcher") == "Agent" for entry in hooks["PostToolUse"])
    assert "SessionStart" in hooks

    proxy_md = work_dir / ".claude" / "agents" / "mngr-proxy" / "proxy.md"
    assert proxy_md.exists()
    proxy_content = proxy_md.read_text()
    assert "model: haiku" in proxy_content
    # Discovery hinges on the frontmatter name, not the path; pin it.
    assert "name: mngr-proxy" in proxy_content

    python_prefix = "uv run python -m imbue.mngr_claude_subagent_proxy.hooks."
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

    # The inherited mngr baseline Stop hook is recognized as safe and left in
    # settings.local.json; the proxy's own hooks go to the config-dir settings.json.
    settings = json.loads(settings_path.read_text())
    assert "Stop" in settings["hooks"]

    managed = json.loads(_settings_json_path(fake_host.host_dir, agent.id).read_text())
    assert "PreToolUse" in managed["hooks"]


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
    monkeypatch.delenv("MNGR_CLAUDE_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS", raising=False)
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
                                    "command": '[ -n "$MNGR_CLAUDE_SUBAGENT_PROXY_CHILD" ] && exit 0; echo project-stop',
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
    monkeypatch.setenv("MNGR_CLAUDE_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS", "1")
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
        to_update(base_ctx.config.field_ref().plugins, {PluginName(CLAUDE_SUBAGENT_PROXY_PLUGIN_NAME): config}),
    )
    return base_ctx.model_copy_update(to_update(base_ctx.field_ref().config, updated_config))


def test_deny_mode_installs_pretooluse_deny_and_sessionstart_reaper(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """DENY mode installs PreToolUse:Agent (skill-pointer deny) + SessionStart (reap).

    The reaper is the same ``hooks/reap.py`` module that PROXY mode
    uses -- label-driven, identical code across modes. DENY does NOT
    install the PROXY-only ``guard_stop_hooks.py`` because DENY-spawned
    children don't have the ``MNGR_CLAUDE_SUBAGENT_PROXY_CHILD`` env var.

    No PostToolUse hook (the deny hook never runs mngr create itself),
    no mngr-proxy/proxy.md agent definition.
    """
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    settings_path = _settings_json_path(fake_host.host_dir, agent.id)
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    # PreToolUse:Agent is the skill-pointer deny hook.
    assert "PreToolUse" in hooks
    assert any(entry.get("matcher") == "Agent" for entry in hooks["PreToolUse"])
    pre_cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "imbue.mngr_claude_subagent_proxy.hooks.deny" in pre_cmd
    # SessionStart installs the shared label-driven reaper -- same module
    # PROXY uses. The PROXY-only guard_stop_hooks hook is NOT installed.
    assert "SessionStart" in hooks
    session_inner = hooks["SessionStart"][0]["hooks"]
    session_cmds = [entry["command"] for entry in session_inner]
    assert any("imbue.mngr_claude_subagent_proxy.hooks.reap" in cmd for cmd in session_cmds)
    assert not any("guard_stop_hooks" in cmd for cmd in session_cmds)
    # No PostToolUse cleanup -- the deny hook never runs mngr create
    # itself, so no per-Task-call state on the parent to clean up.
    assert "PostToolUse" not in hooks


def test_deny_mode_does_not_write_proxy_agent_definition(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """In DENY mode, the mngr-proxy/proxy.md agent definition is NOT written.

    The Haiku dispatcher is part of PROXY mode only. Writing it in deny
    mode would dirty the worktree with a file the user never invokes.
    """
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    proxy_md = work_dir / ".claude" / "agents" / "mngr-proxy" / "proxy.md"
    assert not proxy_md.exists()


def test_deny_mode_writes_mngr_proxy_skill(work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext) -> None:
    """DENY mode provisions the ``mngr-proxy`` Claude skill at .claude/skills/.

    The skill carries the verbose context (when to use, how to parse
    subagent_wait output, how to inspect a running subagent, etc.) so
    the deny hook's permissionDecisionReason can stay short.
    """
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    skill_path = work_dir / ".claude" / "skills" / "mngr-proxy" / "SKILL.md"
    assert skill_path.is_file()
    body = skill_path.read_text()
    # Frontmatter wires the skill into Claude Code's skill-discovery mechanism.
    assert body.startswith("---\n")
    assert "name: mngr-proxy" in body
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
    # concerns; pin them so the skill keeps that coverage. The skill
    # describes the permission case using the literal prefix that
    # subagent_wait actually emits (PERMISSION_REQUIRED:<slug>), not
    # the PROXY-mode wait-script's NEED_PERMISSION translation.
    assert "PERMISSION_REQUIRED" in body
    assert "run_in_background" in body


def test_proxy_mode_does_not_write_mngr_proxy_skill(
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

    skill_path = work_dir / ".claude" / "skills" / "mngr-proxy" / "SKILL.md"
    assert not skill_path.exists()


def test_proxy_agent_definition_refuses_unignored_git_worktree(temp_git_repo: Path, fake_host: FakeHost) -> None:
    """PROXY provisioning aborts if the agent-definition path is not gitignored.

    Writing it would surface as an unstaged change in the tracked worktree.
    The error must point at the subdirectory to gitignore and the repo-scope
    disable command, and the artifact must not be written.
    """
    agent = FakeAgent(AgentId.generate(), temp_git_repo, ClaudeAgentConfig())

    with pytest.raises(UnignoredProxyArtifactError) as exc_info:
        _provision(agent, fake_host, None)

    message = str(exc_info.value)
    assert str(Path(".claude") / "agents" / "mngr-proxy" / "proxy.md") in message
    # The remediation points at the whole mngr-proxy/ subdir, not just the file.
    assert f"{Path('.claude') / 'agents' / 'mngr-proxy'}/" in message
    assert "mngr config set --scope project" in message
    assert f"plugins.{CLAUDE_SUBAGENT_PROXY_PLUGIN_NAME}.enabled false" in message
    assert not (temp_git_repo / ".claude" / "agents" / "mngr-proxy" / "proxy.md").exists()


def test_proxy_agent_definition_written_when_claude_dir_gitignored(temp_git_repo: Path, fake_host: FakeHost) -> None:
    """A broad .claude/ ignore covers the artifact, so provisioning writes it."""
    (temp_git_repo / ".gitignore").write_text(".claude/\n")
    agent = FakeAgent(AgentId.generate(), temp_git_repo, ClaudeAgentConfig())

    _provision(agent, fake_host, None)

    assert (temp_git_repo / ".claude" / "agents" / "mngr-proxy" / "proxy.md").is_file()


def test_proxy_agent_definition_written_when_subdir_gitignored(temp_git_repo: Path, fake_host: FakeHost) -> None:
    """Ignoring exactly the mngr-proxy/ subdir (what the error suggests) is accepted.

    Pins the file-vs-directory subtlety: the guard runs before the directory
    exists, and the suggested trailing-slash directory rule does not match the
    not-yet-existing directory path -- but it does match the file under it,
    which is what the guard checks. If this regression test ever fails, the
    error message would be telling users to add a rule that the guard rejects.
    """
    (temp_git_repo / ".gitignore").write_text(".claude/agents/mngr-proxy/\n")
    agent = FakeAgent(AgentId.generate(), temp_git_repo, ClaudeAgentConfig())

    _provision(agent, fake_host, None)

    assert (temp_git_repo / ".claude" / "agents" / "mngr-proxy" / "proxy.md").is_file()


def test_deny_skill_refuses_unignored_git_worktree(
    temp_git_repo: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """DENY provisioning aborts if the skill path is not gitignored."""
    ctx = _ctx_with_plugin_config(temp_mngr_ctx, SubagentProxyPluginConfig(mode=SubagentProxyMode.DENY))
    agent = FakeAgent(AgentId.generate(), temp_git_repo, ClaudeAgentConfig(), name=AgentName("reviewer"))

    with pytest.raises(UnignoredProxyArtifactError) as exc_info:
        _provision(agent, fake_host, ctx)

    assert str(Path(".claude") / "skills" / "mngr-proxy" / "SKILL.md") in str(exc_info.value)
    assert not (temp_git_repo / ".claude" / "skills" / "mngr-proxy" / "SKILL.md").exists()


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
    monkeypatch.delenv("MNGR_CLAUDE_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS", raising=False)
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

    settings = json.loads(_settings_json_path(fake_host.host_dir, agent.id).read_text())
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

    settings = json.loads(_settings_json_path(fake_host.host_dir, agent.id).read_text())
    hooks = settings["hooks"]
    pre_cmd = hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "imbue.mngr_claude_subagent_proxy.hooks.spawn" in pre_cmd
    assert "PostToolUse" in hooks
    assert "SessionStart" in hooks


def test_proxy_hooks_land_in_managed_file_in_env_config_dir_mode(
    work_dir: Path, fake_host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In use_env_config_dir mode the proxy hooks land in the managed --settings file.

    There is no per-agent config dir, so the managed file is the only channel
    (mirroring mngr_claude's own _configure_agent_hooks in that mode). The
    config-dir settings.json is NOT written.
    """
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared_dir))
    agent = FakeAgent(
        AgentId.generate(),
        work_dir,
        ClaudeAgentConfig(use_env_config_dir=True),
        name=AgentName("reviewer"),
    )

    _provision(agent, fake_host, None)

    settings = json.loads(_managed_settings_path(fake_host.host_dir, agent.id).read_text())
    hooks = settings["hooks"]
    assert "PreToolUse" in hooks
    assert "SessionStart" in hooks
    # The per-agent config-dir settings.json is not used in this mode.
    assert not _settings_json_path(fake_host.host_dir, agent.id).exists()


def test_deny_mode_merges_into_existing_settings_json(
    work_dir: Path, fake_host: FakeHost, temp_mngr_ctx: MngrContext
) -> None:
    """Deny mode preserves pre-existing entries in the config-dir settings.json.

    mngr_claude provisioning writes its readiness / user-prompt hooks into the
    settings.json before this plugin runs (we are ``trylast``). Deny mode must
    merge its single PreToolUse:Agent entry without clobbering those.
    """
    managed_path = _settings_json_path(fake_host.host_dir, agent_id := AgentId.generate())
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    managed_path.write_text(
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
    agent = FakeAgent(agent_id, work_dir, ClaudeAgentConfig(), name=AgentName("reviewer"))

    _provision(agent, fake_host, ctx)

    settings = json.loads(managed_path.read_text())
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

    # Provisioning still wrote the merged config-dir settings
    # (PreToolUse/PostToolUse/SessionStart), and the to-be-stripped keys were
    # never present to begin with.
    settings_path = _settings_json_path(fake_host.host_dir, agent_id)
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    hooks = settings["hooks"]
    assert "Stop" not in hooks
    assert "SubagentStop" not in hooks
    assert "PreToolUse" in hooks


def _write_user_stop_hook(work_dir: Path) -> Path:
    """Seed work_dir/.claude/settings.local.json with one user-defined Stop hook."""
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.local.json"
    settings_path.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user-stop"}]}]}}) + "\n"
    )
    return settings_path


def test_guard_user_stop_hooks_raises_when_settings_local_not_gitignored(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """Guarding user Stop hooks refuses to dirty a non-gitignored settings.local.json.

    This is the one place mngr still requires settings.local.json be gitignored:
    wrapping the user's own Stop hooks would otherwise show as an unstaged change.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # No .gitignore entry: settings.local.json is not ignored.
    init_git_repo(work_dir, initial_commit=False)
    _write_user_stop_hook(work_dir)

    with pytest.raises(PluginMngrError, match="not gitignored"):
        _guard_user_stop_hooks_in_project_settings(host, work_dir)


def test_guard_user_stop_hooks_wraps_when_gitignored(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """When settings.local.json is gitignored, user Stop hooks get the proxy-child guard."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    init_git_repo(work_dir, initial_commit=False)
    (work_dir / ".gitignore").write_text(".claude/settings.local.json\n")
    settings_path = _write_user_stop_hook(work_dir)

    _guard_user_stop_hooks_in_project_settings(host, work_dir)

    command = json.loads(settings_path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert PROXY_CHILD_GUARD_PREFIX in command


def test_guard_user_stop_hooks_skips_gitignore_check_when_nothing_to_guard(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The gitignore requirement applies only when there is a Stop hook to wrap.

    A settings.local.json with no Stop/SubagentStop hooks is never written, so a
    missing gitignore entry does not block provisioning.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # No .gitignore entry: settings.local.json is not ignored.
    init_git_repo(work_dir, initial_commit=False)
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    # Only a non-Stop user hook -- nothing for the proxy-child guard to wrap.
    (claude_dir / "settings.local.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}]}}
        )
        + "\n"
    )

    # Should not raise: no write happens, so the gitignore requirement never applies.
    _guard_user_stop_hooks_in_project_settings(host, work_dir)


def test_gitignore_check_passes_when_claude_is_symlink_and_resolved_path_gitignored(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """gitignore check should pass when .claude is a symlink and the resolved path is gitignored."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    init_git_repo(repo_dir, initial_commit=False)

    # Create .agents directory and symlink .claude -> .agents
    (repo_dir / ".agents").mkdir()
    (repo_dir / ".claude").symlink_to(".agents")

    # Gitignore the resolved path (what git actually sees)
    (repo_dir / ".gitignore").write_text(".agents/settings.local.json\n")

    # Should not raise
    _check_settings_local_gitignored(host, repo_dir)


def test_gitignore_check_raises_when_claude_is_symlink_and_resolved_path_not_gitignored(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """gitignore check should raise when .claude is a symlink and the resolved path is not gitignored."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    init_git_repo(repo_dir, initial_commit=False)

    # Create .agents directory and symlink .claude -> .agents
    (repo_dir / ".agents").mkdir()
    (repo_dir / ".claude").symlink_to(".agents")

    # Gitignore the symlink path (wrong -- git won't match this)
    (repo_dir / ".gitignore").write_text(".claude/settings.local.json\n")

    with pytest.raises(PluginMngrError, match="not gitignored") as exc_info:
        _check_settings_local_gitignored(host, repo_dir)

    # Error message should tell the user to add the resolved path, not the symlink path
    assert ".agents/settings.local.json" in str(exc_info.value)


def test_gitignore_check_skips_when_claude_symlink_points_outside_repo(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """gitignore check should silently return when .claude symlink target is outside the repo."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    init_git_repo(repo_dir, initial_commit=False)

    # Create a directory outside the repo and symlink .claude to it
    outside_dir = tmp_path / "outside_agents"
    outside_dir.mkdir()
    (repo_dir / ".claude").symlink_to(outside_dir)

    # Should not raise -- target is outside the repo, git won't track it
    _check_settings_local_gitignored(host, repo_dir)
