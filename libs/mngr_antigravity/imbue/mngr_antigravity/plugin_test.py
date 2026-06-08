"""Unit tests for AntigravityAgentConfig and AntigravityAgent."""

import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import is_macos
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_antigravity.antigravity_config import CAPTURE_CONVERSATION_ID_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import CLEAR_ACTIVE_MARKER_WHEN_IDLE_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import SET_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import build_onboarding_seed
from imbue.mngr_antigravity.antigravity_config import get_antigravity_hooks_config_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_onboarding_cache_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.mngr_antigravity.plugin import AntigravityAgent
from imbue.mngr_antigravity.plugin import AntigravityAgentConfig
from imbue.mngr_antigravity.plugin import register_agent_type


def test_antigravity_agent_config_has_correct_defaults() -> None:
    config = AntigravityAgentConfig()

    assert str(config.command) == "agy"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.auto_allow_permissions is False
    # Default-off, matching mngr_claude's auto_dismiss_dialogs posture: trusting
    # the source repo (writing to the user's shared global settings) should be an
    # explicit choice (--yes or auto_dismiss_dialogs=True), not a default.
    assert config.auto_dismiss_dialogs is False
    # Per-agent settings default to a copy of the user's real settings (claude-parity).
    assert config.sync_home_settings is True
    # No structured permission schema -- a free-form blob mirroring mngr_claude.
    assert config.settings_overrides == {}
    # Token is symlinked by default so refreshes propagate.
    assert config.symlink_oauth_token is True


def test_antigravity_agent_config_merge_with_replaces_cli_args() -> None:
    """User-supplied cli_args replace the default under assign-by-default merge semantics."""
    base = AntigravityAgentConfig()
    override = AntigravityAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, AntigravityAgentConfig)
    # Override's cli_args replaces (rather than concatenates onto) the base.
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "agy"


def test_antigravity_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(AntigravityAgent, InteractiveTuiAgent)


def test_antigravity_agent_advertises_tui_ready_indicator() -> None:
    """Ready indicator is a footer-hint substring that only appears once the input prompt is drawn.

    Pinned because the obvious-but-wrong choice ("Antigravity CLI" from the
    splash banner) matches earlier than the input row is actually ready --
    agy emits a "Welcome to the Antigravity CLI. You are currently not
    signed in." line while still authing, which is too early to paste
    into. See plugin.py for the rationale.
    """
    assert AntigravityAgent.TUI_READY_INDICATOR == "? for shortcuts"


def test_antigravity_agent_implements_send_enter_and_validate() -> None:
    """AntigravityAgent fills in the abstract method by picking a strategy."""
    assert "_send_enter_and_validate" not in AntigravityAgent.__abstractmethods__


def test_register_agent_type_returns_antigravity_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "antigravity"
    assert agent_class is AntigravityAgent
    assert config_class is AntigravityAgentConfig


def _make_antigravity_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: AntigravityAgentConfig,
) -> AntigravityAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return AntigravityAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-antigravity"),
        agent_type=AgentTypeName("antigravity"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


@pytest.fixture
def antigravity_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    return _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig())


@pytest.fixture
def antigravity_agent_auto_allow(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    return _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig(auto_allow_permissions=True))


@pytest.fixture
def antigravity_agent_auto_dismiss(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    """Agent with `auto_dismiss_dialogs=True` so provision() trusts silently."""
    return _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig(auto_dismiss_dialogs=True))


class _ConfirmingAntigravityAgent(AntigravityAgent):
    """Test subclass whose trust prompt auto-accepts without invoking click.confirm."""

    def _prompt_user_to_trust_workspace(self, source_path: Path, settings_path: Path) -> bool:
        return True


class _DecliningAntigravityAgent(AntigravityAgent):
    """Test subclass whose trust prompt auto-declines without invoking click.confirm."""

    def _prompt_user_to_trust_workspace(self, source_path: Path, settings_path: Path) -> bool:
        return False


class _ConfirmingAgentWithFakeSourceRoot(AntigravityAgent):
    """Test subclass that auto-accepts the trust prompt AND fakes a source repo root.

    Used to exercise the source-vs-workspace split without actually creating a
    git repo on disk. The fake source path is just ``work_dir.parent`` so the
    test can assert on a stable, predictable value.
    """

    def _prompt_user_to_trust_workspace(self, source_path: Path, settings_path: Path) -> bool:
        return True

    def _find_git_source_path(self, concurrency_group: ConcurrencyGroup) -> Path | None:
        return self.work_dir.parent


class _DecliningAgentWithFakeSourceRoot(AntigravityAgent):
    """Auto-declines and fakes a source repo root different from work_dir.

    Used to verify the source-already-trusted short-circuit doesn't prompt
    (the prompt is wired to decline, so reaching it would raise SystemExit).
    """

    def _prompt_user_to_trust_workspace(self, source_path: Path, settings_path: Path) -> bool:
        return False

    def _find_git_source_path(self, concurrency_group: ConcurrencyGroup) -> Path | None:
        return self.work_dir.parent


class _AntigravityAgentWithFakeSourceRoot(AntigravityAgent):
    """Plain agent with a fake source repo root for non-prompted paths (auto-approve, auto_dismiss)."""

    def _find_git_source_path(self, concurrency_group: ConcurrencyGroup) -> Path | None:
        return self.work_dir.parent


def _make_subclassed_agent_with_flags(
    cls: type[AntigravityAgent],
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: AntigravityAgentConfig,
    *,
    is_interactive: bool = False,
    is_auto_approve: bool = False,
) -> AntigravityAgent:
    """Build a subclassed agent with the requested MngrContext flags set."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    ctx = local_provider.mngr_ctx.model_copy_update(
        to_update(local_provider.mngr_ctx.field_ref().is_interactive, is_interactive),
        to_update(local_provider.mngr_ctx.field_ref().is_auto_approve, is_auto_approve),
    )
    return cls.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-antigravity"),
        agent_type=AgentTypeName("antigravity"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=ctx,
        agent_config=agent_config,
        host=host,
    )


@pytest.fixture
def auto_approve_ctx(local_provider: LocalProviderInstance, tmp_path: Path) -> AntigravityAgent:
    """Agent whose ``mngr_ctx.is_auto_approve=True`` so provision() trusts silently."""
    return _make_subclassed_agent_with_flags(
        AntigravityAgent, local_provider, tmp_path, AntigravityAgentConfig(), is_auto_approve=True
    )


@pytest.fixture
def interactive_ctx_with_confirmation(local_provider: LocalProviderInstance, tmp_path: Path) -> AntigravityAgent:
    """Subclassed agent: is_interactive=True and the prompt auto-accepts."""
    return _make_subclassed_agent_with_flags(
        _ConfirmingAntigravityAgent, local_provider, tmp_path, AntigravityAgentConfig(), is_interactive=True
    )


@pytest.fixture
def interactive_ctx_with_declination(local_provider: LocalProviderInstance, tmp_path: Path) -> AntigravityAgent:
    """Subclassed agent: is_interactive=True and the prompt auto-declines."""
    return _make_subclassed_agent_with_flags(
        _DecliningAntigravityAgent, local_provider, tmp_path, AntigravityAgentConfig(), is_interactive=True
    )


# =============================================================================
# assemble_command
# =============================================================================

_BACKGROUND_TASKS_LAUNCH_PREFIX = "( bash $MNGR_AGENT_STATE_DIR/commands/antigravity_background_tasks.sh"


def test_assemble_command_invokes_agy_with_log_file(antigravity_agent: AntigravityAgent) -> None:
    """The foreground command runs `agy ... --log-file <agent-state>/logs/agy_cli.log`."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert " agy " in command
    assert "--log-file" in command
    assert "logs/agy_cli.log" in command


def test_assemble_command_launches_agy_under_per_agent_home(antigravity_agent: AntigravityAgent) -> None:
    """agy is launched with HOME relocated to the per-agent home -- the core isolation mechanism.

    ``env HOME=<home>`` is injected only on the agy process (not the whole
    chain), so the backgrounded supervisor subshell and tmux keep the real HOME.
    """
    agent = antigravity_agent
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    home = str(agent._get_agy_home_dir())
    assert f"env HOME={home} agy " in command
    # HOME relocation comes after the cd into the workspace symlink, right before agy.
    assert command.index(f"env HOME={home}") < command.index(" agy ")


def test_assemble_command_does_not_add_hooks_via_add_dir(antigravity_agent: AntigravityAgent) -> None:
    """Under the per-agent HOME, agy executes hooks from $HOME/.gemini/config/hooks.json.

    The old --add-dir + /tmp hooks-symlink workaround is gone: there is exactly
    one hooks path now, so no --add-dir for hooks should appear.
    """
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert "--add-dir" not in command
    assert "mngr_antigravity_hooks" not in command


def test_assemble_command_appends_user_agent_args(antigravity_agent: AntigravityAgent) -> None:
    """User agent_args land between the agy command and the appended log-file flag."""
    command = str(
        antigravity_agent.assemble_command(antigravity_agent.host, ("--add-dir", "/tmp"), command_override=None)
    )
    # The user *may* pass their own --add-dir; it lands right after agy. (mngr no
    # longer injects one of its own for hooks.)
    assert "agy --add-dir /tmp --log-file" in command


def test_assemble_command_omits_dangerously_skip_permissions_when_auto_allow_disabled(
    antigravity_agent: AntigravityAgent,
) -> None:
    """Default config does not auto-approve, so the flag is absent."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert "--dangerously-skip-permissions" not in command


def test_assemble_command_adds_dangerously_skip_permissions_when_auto_allow_enabled(
    antigravity_agent_auto_allow: AntigravityAgent,
) -> None:
    """auto_allow_permissions appends the CLI flag.

    Auto-approval goes through the flag, NOT a PreToolUse hook: agy's
    {"decision": "allow"} hook output does not gate the run_command
    confirmation dialog (verified live against agy 1.0.3). A finer-grained
    policy instead lives in the per-agent settings.json permissions block.
    """
    agent = antigravity_agent_auto_allow
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert "--dangerously-skip-permissions" in command


def test_assemble_command_does_not_symlink_playwright_cache(antigravity_agent: AntigravityAgent) -> None:
    """The playwright cache symlink is set up at provision time (durable), not in the launch command."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert "ms-playwright-go" not in command


def test_assemble_command_launches_background_tasks_supervisor(antigravity_agent: AntigravityAgent) -> None:
    """The supervisor is the single backgrounded subshell; it owns the watchers."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert command.startswith(_BACKGROUND_TASKS_LAUNCH_PREFIX), command
    # No bare watcher subshells: the supervisor is the single entry point.
    assert "( bash $MNGR_AGENT_STATE_DIR/commands/stream_transcript.sh ) &" not in command
    assert "( bash $MNGR_AGENT_STATE_DIR/commands/common_transcript.sh ) &" not in command


def test_assemble_command_pre_creates_agy_log_directory(antigravity_agent: AntigravityAgent) -> None:
    """A foreground `mkdir -p <logs_dir> ...` runs before agy so --log-file does not fail on a fresh agent.

    The supervisor runs concurrently with agy, so we cannot rely on a
    watcher's own `mkdir -p` to create the directory in time. The mkdir
    must be in the foreground chain, ordered before the agy invocation.
    """
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    log_dir = str(antigravity_agent._get_agent_dir() / "logs")
    assert "mkdir -p " in command
    assert log_dir in command.split(" agy ")[0]
    # And it must come before agy, not after.
    assert command.index("mkdir -p") < command.index(" agy "), command


def test_assemble_command_symlinks_workspace_to_a_non_hidden_path(antigravity_agent: AntigravityAgent) -> None:
    """agy refuses dotted-path workspaces, so launch via a `/tmp/.../<id>` symlink and `cd` to it.

    Verified live: agy logs ``Failed to add workspace folder ... is hidden:
    ignore uri`` for paths containing a dot-prefixed segment (e.g. anything
    under ``~/.mngr/``). Launching with cwd set to a symlink under
    ``/tmp/mngr_antigravity_workspaces/<agent_id>`` -> ``work_dir`` produces
    ``project: using project "/tmp/..."`` instead, with no hidden-path error.
    HOME relocation does not change this (agy accepts a hidden *config* dir but
    not a hidden *workspace*). The symlink is recreated via ``ln -sfn``.
    """
    agent = antigravity_agent
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    expected_symlink = f"/tmp/mngr_antigravity_workspaces/{agent.id}"
    assert f"ln -sfn {agent.work_dir} {expected_symlink}" in command
    assert f"cd {expected_symlink} &&" in command
    # Ordering: mkdir -> ln(workspace) -> cd -> agy
    mkdir_idx = command.index("mkdir -p")
    ln_idx = command.index(f"ln -sfn {agent.work_dir} {expected_symlink}")
    cd_idx = command.index(f"cd {expected_symlink}")
    agy_idx = command.index(" agy ")
    assert mkdir_idx < ln_idx < cd_idx < agy_idx, command


def test_get_expected_process_name_returns_agy(antigravity_agent: AntigravityAgent) -> None:
    """`agy` is the single-file Go binary name visible to ps/tmux."""
    assert antigravity_agent.get_expected_process_name() == "agy"


def test_modify_env_vars_exposes_app_data_dir(antigravity_agent: AntigravityAgent) -> None:
    """The streamer needs the per-agent app-data dir to find the relocated transcripts."""
    env_vars: dict[str, str] = {"PRE_EXISTING": "kept"}
    antigravity_agent.modify_env_vars(antigravity_agent.host, env_vars)
    assert env_vars["PRE_EXISTING"] == "kept"
    # The app-data dir points at the per-agent home's antigravity-cli dir so the
    # streamer (which runs on the real HOME) finds the relocated brain/ transcripts.
    expected_app_data = str(antigravity_agent._get_agy_home_dir() / ".gemini" / "antigravity-cli")
    assert env_vars["ANTIGRAVITY_APP_DATA_DIR"] == expected_app_data
    # The agy --log-file is no longer surfaced via env: conversation-id discovery
    # uses the capture-hook file, so modify_env_vars sets only the app-data dir.
    assert "ANTIGRAVITY_AGY_LOG_FILE" not in env_vars


def test_assemble_command_resumes_main_conversation_via_set_dash_dash(antigravity_agent: AntigravityAgent) -> None:
    """The launch command resumes the main (root) conversation, evaluated in the shell.

    The stored command is replayed verbatim on every `mngr start`, so the
    resume decision is shell-evaluated at launch: read the root conversation id
    from the per-agent root_conversation file and, when present, pass
    `--conversation "$id"` via `set --` / "$@" (which avoids unquoted-substitution
    word splitting so it works in bash and zsh). The id comes from
    root_conversation (the root agent's), NOT the conversation-ids file whose
    last line can be a subagent. We do not stat agy's store to pre-check
    existence -- agy warns and starts fresh on its own for a pruned conversation
    -- so the command stays decoupled from agy's on-disk layout.
    """
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    root_file = str(antigravity_agent._get_root_conversation_file_path())
    # Reads the root conversation id from the per-agent root_conversation file.
    assert f"__mngr_cid=$(cat {root_file} 2>/dev/null || true)" in command
    # Passes the flag positionally whenever an id is recorded (no store stat).
    assert 'if [ -n "$__mngr_cid" ]; then set -- --conversation "$__mngr_cid"; fi' in command
    # Resume must not read the subagent-pollutable conversation-ids file.
    assert "tail -n 1" not in command
    # No coupling to agy's conversation store path/extension.
    assert ".db" not in command
    assert "conversations/" not in command
    assert "agy " in command and '"$@"' in command


def test_assemble_command_resume_prelude_runs_after_cd_and_before_agy(antigravity_agent: AntigravityAgent) -> None:
    """The resume prelude + agy run as a `{ ...; }` group gated on the cd succeeding."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    symlink_path = antigravity_agent._get_agy_workspace_symlink_path()
    # cd -> resume-prelude -> agy "$@", all inside a brace group after the cd.
    assert f"cd {symlink_path} && {{ __mngr_cid=" in command
    assert command.index("__mngr_cid=") < command.index(" agy ")
    assert command.rstrip().endswith('"$@" ; }')


# =============================================================================
# provision: trust (global = durable source repo; per-agent = transient workspace)
# =============================================================================


def _read_global_settings(home: Path) -> dict[str, Any]:
    """Read the user-tier (global) settings.json under the redirected home."""
    settings_path = get_antigravity_settings_path(home)
    if not settings_path.exists():
        return {}
    parsed: Any = json.loads(settings_path.read_text())
    assert isinstance(parsed, dict)
    return parsed


def _read_per_agent_settings(agent: AntigravityAgent) -> dict[str, Any]:
    """Read the per-agent settings.json from the agent's relocated home."""
    settings_path = get_antigravity_settings_path(agent._get_agy_home_dir())
    parsed: Any = json.loads(settings_path.read_text())
    assert isinstance(parsed, dict)
    return parsed


def _provision(agent: AntigravityAgent) -> None:
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )


def test_provision_does_not_write_into_work_dir(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """The plugin writes nothing to the user's work_dir.

    Antigravity reads workspace-tier files from `<work_dir>/.agents/` and
    `<work_dir>/.antigravityignore`; mngr leaves both alone so the user's
    project tree is untouched by ``mngr create``.
    """
    agent = auto_approve_ctx
    _provision(agent)
    assert not (agent.work_dir / ".agents").exists()
    assert not (agent.work_dir / ".antigravityignore").exists()
    assert not (agent.work_dir / ".gemini").exists()


def test_provision_installs_capture_conversation_id_script(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """provision() installs capture_conversation_id.sh into the agent's commands/ dir.

    The PreInvocation capture hook invokes this script by that path
    (build_antigravity_hooks_config), so it must be provisioned for conversation
    resume + transcript scoping to work.
    """
    agent = auto_approve_ctx
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
    script_path = agent._get_agent_dir() / "commands" / CAPTURE_CONVERSATION_ID_SCRIPT_NAME
    assert script_path.exists()
    # Sanity-check it's the capture script (extracts conversationId from stdin).
    assert "conversationId" in script_path.read_text()


def test_provision_persists_source_repo_to_global_under_auto_approve(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """`--yes` (mngr_ctx.is_auto_approve) silently records the source repo in the global settings.

    With no git repo, the source path is the work_dir itself. The *transient*
    workspace symlink path is NOT written to the global file (it goes only into
    the per-agent settings).
    """
    agent = auto_approve_ctx
    _provision(agent)
    global_settings = _read_global_settings(isolated_home)
    assert global_settings["trustedWorkspaces"] == [str(agent.work_dir)]
    assert agent._get_agy_workspace_symlink_path() not in global_settings["trustedWorkspaces"]


def test_provision_trusts_transient_workspace_in_per_agent_settings(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """The running (isolated) agy exact-matches its cwd, so the workspace symlink is trusted per-agent."""
    agent = auto_approve_ctx
    _provision(agent)
    per_agent = _read_per_agent_settings(agent)
    assert agent._get_agy_workspace_symlink_path() in per_agent["trustedWorkspaces"]


def test_provision_persists_source_repo_under_auto_dismiss_dialogs(
    antigravity_agent_auto_dismiss: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """`auto_dismiss_dialogs=True` (per-agent-type opt-in) silently trusts the source repo."""
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    global_settings = _read_global_settings(isolated_home)
    assert str(agent.work_dir) in global_settings["trustedWorkspaces"]


def test_provision_prompts_user_then_trusts_when_interactive_and_user_accepts(
    interactive_ctx_with_confirmation: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Mirror of mngr_claude's `_prompt_user_for_trust`: prompt, then write the source on yes."""
    agent = interactive_ctx_with_confirmation
    _provision(agent)
    global_settings = _read_global_settings(isolated_home)
    assert str(agent.work_dir) in global_settings["trustedWorkspaces"]
    # And the per-agent settings trust the agent's own workspace.
    per_agent = _read_per_agent_settings(agent)
    assert agent._get_agy_workspace_symlink_path() in per_agent["trustedWorkspaces"]


def test_provision_aborts_when_interactive_and_user_declines(
    interactive_ctx_with_declination: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """If the user declines the prompt, exit cleanly via SystemExit -- never run untrusted code.

    Using SystemExit (a ``BaseException``) rather than ``UserInputError``
    lets the abort propagate through ``provision_agent``'s
    ``ConcurrencyExceptionGroup`` wrapping unwrapped, so the operator sees
    a clean exit rather than a noisy auto-diagnostics traceback.
    """
    agent = interactive_ctx_with_declination
    with pytest.raises(SystemExit) as excinfo:
        _provision(agent)
    assert excinfo.value.code == 1
    # Nothing was written to the global settings, and no per-agent home was built.
    assert not get_antigravity_settings_path(isolated_home).exists()
    assert not get_antigravity_settings_path(agent._get_agy_home_dir()).exists()


def test_provision_aborts_in_non_interactive_mode_without_opt_in(
    antigravity_agent: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Non-interactive without --yes or auto_dismiss_dialogs: exit cleanly rather than run untrusted code.

    Default mngr_ctx has is_interactive=False and is_auto_approve=False;
    the antigravity_agent fixture defaults auto_dismiss_dialogs=False, so
    no path to a trust write exists and we must abort.
    """
    with pytest.raises(SystemExit) as excinfo:
        _provision(antigravity_agent)
    assert excinfo.value.code == 1
    assert not get_antigravity_settings_path(isolated_home).exists()


def test_provision_preserves_existing_global_settings(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """The global trust write must be additive: prior keys and entries stay verbatim."""
    agent = auto_approve_ctx
    settings_path = get_antigravity_settings_path(isolated_home)
    settings_path.write_text(json.dumps({"trustedWorkspaces": ["/prior/workspace"], "colorScheme": "dark"}, indent=2))

    _provision(agent)

    global_settings = _read_global_settings(isolated_home)
    assert "/prior/workspace" in global_settings["trustedWorkspaces"]
    assert str(agent.work_dir) in global_settings["trustedWorkspaces"]
    assert global_settings["colorScheme"] == "dark"


def test_provision_global_trust_is_idempotent(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Two passes under auto-approve yield one source entry, not duplicates."""
    agent = auto_approve_ctx
    _provision(agent)
    _provision(agent)

    global_settings = _read_global_settings(isolated_home)
    assert global_settings["trustedWorkspaces"].count(str(agent.work_dir)) == 1


def test_provision_already_trusted_source_does_not_reprompt(
    interactive_ctx_with_declination: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """If the source repo is already trusted, no prompt fires.

    The declining-user fixture's prompt returns False; if the short-circuit
    weren't in place, this test would raise SystemExit. The per-agent home is
    still built (and trusts the workspace).
    """
    agent = interactive_ctx_with_declination
    settings_path = get_antigravity_settings_path(isolated_home)
    settings_path.write_text(json.dumps({"trustedWorkspaces": [str(agent.work_dir)]}))

    _provision(agent)
    # Global file unchanged (source already trusted); per-agent trusts the workspace.
    assert _read_global_settings(isolated_home)["trustedWorkspaces"] == [str(agent.work_dir)]
    assert agent._get_agy_workspace_symlink_path() in _read_per_agent_settings(agent)["trustedWorkspaces"]


def test_provision_does_not_reprompt_for_worktree_of_trusted_source(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """A worktree of an already-trusted source repo is provisioned silently, no prompt.

    The declining-prompt subclass would raise SystemExit if the prompt fired;
    reaching the silent branch is what makes this test pass. Mirrors the UX
    goal: once you've trusted a source repo, spawning another worktree of the
    same repo shouldn't re-prompt.
    """
    agent = _make_subclassed_agent_with_flags(
        _DecliningAgentWithFakeSourceRoot, local_provider, tmp_path, AntigravityAgentConfig(), is_interactive=True
    )
    fake_source = str(agent.work_dir.parent)
    settings_path = get_antigravity_settings_path(isolated_home)
    settings_path.write_text(json.dumps({"trustedWorkspaces": [fake_source]}))

    _provision(agent)

    # Global unchanged (source already trusted); per-agent trusts the workspace.
    assert _read_global_settings(isolated_home)["trustedWorkspaces"] == [fake_source]
    assert agent._get_agy_workspace_symlink_path() in _read_per_agent_settings(agent)["trustedWorkspaces"]


def test_provision_persists_only_source_not_workspace_to_global(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """The global file accumulates only the durable source repo, never the transient workspace path."""
    agent = _make_subclassed_agent_with_flags(
        _AntigravityAgentWithFakeSourceRoot, local_provider, tmp_path, AntigravityAgentConfig(), is_auto_approve=True
    )

    _provision(agent)

    fake_source = str(agent.work_dir.parent)
    global_settings = _read_global_settings(isolated_home)
    assert global_settings["trustedWorkspaces"] == [fake_source]
    assert agent._get_agy_workspace_symlink_path() not in global_settings["trustedWorkspaces"]


def test_provision_does_not_duplicate_source_when_already_present(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """The already-trusted source short-circuit must not re-append the source path."""
    agent = _make_subclassed_agent_with_flags(
        _DecliningAgentWithFakeSourceRoot, local_provider, tmp_path, AntigravityAgentConfig(), is_interactive=True
    )
    fake_source = str(agent.work_dir.parent)
    settings_path = get_antigravity_settings_path(isolated_home)
    settings_path.write_text(json.dumps({"trustedWorkspaces": [fake_source, "/some/unrelated/path"]}))

    _provision(agent)

    global_settings = _read_global_settings(isolated_home)
    assert global_settings["trustedWorkspaces"].count(fake_source) == 1
    assert "/some/unrelated/path" in global_settings["trustedWorkspaces"]


def test_provision_errors_when_trustedworkspaces_has_non_list_value(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """A future agy schema that stores `trustedWorkspaces` as a non-list value must hard-error.

    Silently coercing a non-list value into a fresh array would destroy
    whatever the unknown schema put there. The plugin refuses to write
    instead, surfacing the schema break for human inspection.
    """
    agent = auto_approve_ctx
    settings_path = get_antigravity_settings_path(isolated_home)
    settings_path.write_text(json.dumps({"trustedWorkspaces": "not-a-list"}))

    with pytest.raises(UserInputError) as excinfo:
        _provision(agent)

    message = str(excinfo.value)
    assert "non-list trustedWorkspaces" in message
    assert str(settings_path) in message
    # The unexpected type's name (str) must appear so operators can grep for it.
    assert "str" in message
    # The settings file is left untouched.
    assert json.loads(settings_path.read_text()) == {"trustedWorkspaces": "not-a-list"}


# =============================================================================
# provision: per-agent $HOME tree (settings / onboarding / hooks / token)
# =============================================================================


def test_provision_writes_per_agent_settings_with_overrides_and_synced_base(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """Per-agent settings = copy of user's settings (sync_home_settings) + workspace trust + overrides.

    Overrides win on top, so a per-agent ``permissions``/``model`` policy is the
    only thing that distinguishes a locked-down agent from an open one.
    """
    # Seed the user's real settings so sync_home_settings has something to copy.
    get_antigravity_settings_path(isolated_home).write_text(
        json.dumps({"colorScheme": "dark", "model": "User Default"})
    )
    overrides = {"model": "Gemini 3.5 Flash (Medium)", "permissions": {"allow": ["command(git)"]}}
    agent = _make_antigravity_agent(
        local_provider, tmp_path, AntigravityAgentConfig(auto_dismiss_dialogs=True, settings_overrides=overrides)
    )

    _provision(agent)

    per_agent = _read_per_agent_settings(agent)
    # Inherited from the user's real settings.
    assert per_agent["colorScheme"] == "dark"
    # Override wins over the synced base.
    assert per_agent["model"] == "Gemini 3.5 Flash (Medium)"
    assert per_agent["permissions"] == {"allow": ["command(git)"]}
    # The agent's own workspace is trusted.
    assert agent._get_agy_workspace_symlink_path() in per_agent["trustedWorkspaces"]


def test_provision_per_agent_settings_ignores_user_base_when_sync_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """sync_home_settings=False starts from an empty base, not the user's real settings."""
    get_antigravity_settings_path(isolated_home).write_text(json.dumps({"colorScheme": "dark"}))
    agent = _make_antigravity_agent(
        local_provider, tmp_path, AntigravityAgentConfig(auto_dismiss_dialogs=True, sync_home_settings=False)
    )

    _provision(agent)

    per_agent = _read_per_agent_settings(agent)
    assert "colorScheme" not in per_agent
    # Workspace trust is still seeded so the isolated agy trusts its cwd.
    assert agent._get_agy_workspace_symlink_path() in per_agent["trustedWorkspaces"]


def test_provision_writes_onboarding_seed(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """Provisioning writes the NUX seed to the path agy reads, so its first-run flow doesn't intercept the first message.

    The seed's *contents* are owned by ``test_build_onboarding_seed_emits_the_three_nux_keys``;
    here we only assert provisioning persists that seed at the expected path.
    """
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    onboarding_path = get_antigravity_onboarding_cache_path(agent._get_agy_home_dir())
    assert onboarding_path.exists()
    seed = json.loads(onboarding_path.read_text())
    assert seed == build_onboarding_seed()


def test_provision_symlinks_oauth_token_into_per_agent_home(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """The shared file token is symlinked into the per-agent home so agy is authenticated."""
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    dest = get_antigravity_oauth_token_path(agent._get_agy_home_dir())
    assert dest.is_symlink()
    assert dest.resolve() == get_antigravity_oauth_token_path(isolated_home).resolve()
    assert dest.read_text() == "fake-oauth-token"


def test_provision_copies_oauth_token_when_symlink_disabled(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """symlink_oauth_token=False copies the token for full isolation."""
    agent = _make_antigravity_agent(
        local_provider, tmp_path, AntigravityAgentConfig(auto_dismiss_dialogs=True, symlink_oauth_token=False)
    )
    _provision(agent)
    dest = get_antigravity_oauth_token_path(agent._get_agy_home_dir())
    assert not dest.is_symlink()
    assert dest.read_text() == "fake-oauth-token"


def test_provision_symlinks_token_to_shared_path_even_when_shared_absent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """With no shared token yet, the per-agent token is a (dangling) symlink to the shared path.

    This is the write-through mechanism: agy writes the token in place, so the
    first agent's login writes *through* this symlink to the shared path,
    authenticating every agent that points at it. Provisioning still succeeds.

    Does NOT request ``isolated_home`` (so no shared token is seeded); ``$HOME``
    is still the autouse-isolated ``tmp_path``.
    """
    agent = _make_antigravity_agent(local_provider, tmp_path, AntigravityAgentConfig(auto_dismiss_dialogs=True))

    _provision(agent)

    dest = get_antigravity_oauth_token_path(agent._get_agy_home_dir())
    # It is a symlink pointing at the shared path, even though that target doesn't exist yet.
    assert dest.is_symlink()
    assert Path(os.readlink(dest)) == get_antigravity_oauth_token_path(tmp_path)
    # Dangling: the shared target hasn't been written yet (the first login writes it through).
    assert not dest.exists()
    # Provisioning still completed (the per-agent settings exist).
    assert get_antigravity_settings_path(agent._get_agy_home_dir()).exists()


def test_provision_copy_mode_skips_when_shared_token_absent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """In copy mode (no write-through), a missing shared token means no token is seeded at all.

    Does NOT request ``isolated_home`` (no shared token); ``$HOME`` is the
    autouse-isolated ``tmp_path``.
    """
    agent = _make_antigravity_agent(
        local_provider, tmp_path, AntigravityAgentConfig(auto_dismiss_dialogs=True, symlink_oauth_token=False)
    )

    _provision(agent)

    dest = get_antigravity_oauth_token_path(agent._get_agy_home_dir())
    assert not dest.is_symlink()
    assert not dest.exists()
    assert get_antigravity_settings_path(agent._get_agy_home_dir()).exists()


def test_provision_symlinks_playwright_cache_to_shared_host_cache(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """The per-agent home's ms-playwright-go cache is symlinked to the user's real host cache.

    A fully isolated $HOME would make each agent re-download the heavy playwright
    binaries; sharing the user's host cache avoids that. The OS-specific subpath
    comes from the host's uname (correct on remote hosts too), and it's set up at
    provision time because the per-agent home is durable.
    """
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    subpath = ("Library", "Caches", "ms-playwright-go") if is_macos() else (".cache", "ms-playwright-go")
    dest = agent._get_agy_home_dir().joinpath(*subpath)
    assert dest.is_symlink()
    assert Path(os.readlink(dest)) == isolated_home.joinpath(*subpath)


# =============================================================================
# provision: per-agent hooks.json
# =============================================================================


def _read_hooks_json(agent: AntigravityAgent) -> dict[str, Any]:
    """Read the per-agent hooks.json that provision() writes into the relocated home."""
    parsed: Any = json.loads(get_antigravity_hooks_config_path(agent._get_agy_home_dir()).read_text())
    assert isinstance(parsed, dict)
    return parsed


def test_provision_writes_hooks_json_under_per_agent_home_config(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """hooks.json lands at <home>/.gemini/config/hooks.json -- where agy executes hooks from."""
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    hooks_path = get_antigravity_hooks_config_path(agent._get_agy_home_dir())
    assert hooks_path == agent._get_agy_home_dir() / ".gemini" / "config" / "hooks.json"
    assert hooks_path.exists()


def test_provision_hooks_json_sets_active_marker_on_preinvocation_and_clears_on_stop(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """The active marker hooks are always present (they drive RUNNING vs WAITING).

    PreInvocation runs set_active_marker.sh (touch + record the turn's root);
    Stop runs the root-gated clear script, which removes the marker only on the
    root agent's fully-idle Stop.
    """
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    mngr = _read_hooks_json(agent)["mngr"]
    assert (
        mngr["PreInvocation"][0]["command"] == f'bash "$MNGR_AGENT_STATE_DIR/commands/{SET_ACTIVE_MARKER_SCRIPT_NAME}"'
    )
    assert (
        mngr["Stop"][0]["command"]
        == f'bash "$MNGR_AGENT_STATE_DIR/commands/{CLEAR_ACTIVE_MARKER_WHEN_IDLE_SCRIPT_NAME}"'
    )


def test_provision_installs_clear_active_marker_when_idle_script(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """provision() installs clear_active_marker_when_idle.sh into the commands/ dir.

    The Stop hook invokes this script by that path
    (build_antigravity_hooks_config), so it must be provisioned for the
    fully-idle-gated WAITING transition to work.
    """
    agent = auto_approve_ctx
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
    script_path = agent._get_agent_dir() / "commands" / CLEAR_ACTIVE_MARKER_WHEN_IDLE_SCRIPT_NAME
    assert script_path.exists()
    # Sanity-check it's the idle-gate script (keys on the fullyIdle field).
    assert "fullyIdle" in script_path.read_text()


def test_provision_installs_set_active_marker_script(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """provision() installs set_active_marker.sh into the commands/ dir.

    The PreInvocation hook invokes this script by that path
    (build_antigravity_hooks_config), so it must be provisioned for the marker
    to be set and the turn's root recorded.
    """
    agent = auto_approve_ctx
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
    script_path = agent._get_agent_dir() / "commands" / SET_ACTIVE_MARKER_SCRIPT_NAME
    assert script_path.exists()
    # Sanity-check it's the marker/root script (records the root conversation).
    assert "root_conversation" in script_path.read_text()


def test_provision_does_not_write_hooks_into_work_dir(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """Hooks live in the per-agent home, never in the user's work_dir/.agents."""
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    assert not (agent.work_dir / ".agents").exists()


# =============================================================================
# provision: transcript + supervisor scripts
# =============================================================================


@pytest.fixture
def antigravity_agent_without_common_transcript(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    """Agent with `auto_dismiss_dialogs=True` so provision() can complete in tests."""
    return _make_antigravity_agent(
        local_provider,
        tmp_path,
        AntigravityAgentConfig(emit_common_transcript=False, auto_dismiss_dialogs=True),
    )


def test_provision_writes_raw_transcript_streamer(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """The raw streamer is required by HasTranscriptMixin and is provisioned unconditionally."""
    _provision(antigravity_agent_auto_dismiss)
    expected = antigravity_agent_auto_dismiss._get_agent_dir() / "commands" / "stream_transcript.sh"
    assert expected.exists()
    body = expected.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "antigravity_transcript/events.jsonl" in body
    assert expected.stat().st_mode & 0o111


def test_provision_writes_raw_streamer_even_when_common_transcript_disabled(
    antigravity_agent_without_common_transcript: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Raw capture is required regardless of the common-transcript flag."""
    _provision(antigravity_agent_without_common_transcript)
    expected = antigravity_agent_without_common_transcript._get_agent_dir() / "commands" / "stream_transcript.sh"
    assert expected.exists()


def test_provision_with_common_transcript_writes_converter(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """`emit_common_transcript=True` (default) provisions common_transcript.sh."""
    _provision(antigravity_agent_auto_dismiss)
    expected = antigravity_agent_auto_dismiss._get_agent_dir() / "commands" / "common_transcript.sh"
    assert expected.exists()
    body = expected.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "events/antigravity/common_transcript/events.jsonl" in body
    assert expected.stat().st_mode & 0o111


def test_provision_without_common_transcript_omits_converter(
    antigravity_agent_without_common_transcript: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Disabling emit_common_transcript suppresses the converter script."""
    _provision(antigravity_agent_without_common_transcript)
    expected = antigravity_agent_without_common_transcript._get_agent_dir() / "commands" / "common_transcript.sh"
    assert not expected.exists()


def test_provision_writes_background_tasks_supervisor(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """The supervisor is the single backgrounded entry point launched from assemble_command."""
    _provision(antigravity_agent_auto_dismiss)
    expected = antigravity_agent_auto_dismiss._get_agent_dir() / "commands" / "antigravity_background_tasks.sh"
    assert expected.exists()
    body = expected.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "stream_transcript.sh" in body
    assert "common_transcript.sh" in body
    assert expected.stat().st_mode & 0o111


def test_provision_writes_supervisor_even_when_common_transcript_disabled(
    antigravity_agent_without_common_transcript: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """The supervisor is unconditional; the converter check inside it is the gate."""
    _provision(antigravity_agent_without_common_transcript)
    expected = (
        antigravity_agent_without_common_transcript._get_agent_dir() / "commands" / "antigravity_background_tasks.sh"
    )
    assert expected.exists()
