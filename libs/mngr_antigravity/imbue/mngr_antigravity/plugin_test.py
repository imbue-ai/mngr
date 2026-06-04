"""Unit tests for AntigravityAgentConfig and AntigravityAgent."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_antigravity.antigravity_config import CAPTURE_CONVERSATION_ID_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import get_antigravity_user_settings_path
from imbue.mngr_antigravity.plugin import AntigravityAgent
from imbue.mngr_antigravity.plugin import AntigravityAgentConfig
from imbue.mngr_antigravity.plugin import register_agent_type


def test_antigravity_agent_config_has_correct_defaults() -> None:
    config = AntigravityAgentConfig()

    assert str(config.command) == "agy"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.auto_allow_permissions is False
    # Default-off, matching mngr_claude's auto_dismiss_dialogs posture: writing
    # to the user's shared ~/.gemini/antigravity-cli/settings.json should be an
    # explicit choice (--yes or auto_dismiss_dialogs=True), not a default.
    assert config.auto_dismiss_dialogs is False


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
    """Agent with `auto_dismiss_dialogs=True` so provision() pre-trusts silently."""
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

    Used to exercise the worktree-of-trusted-source branches without actually
    creating a git repo on disk. The fake source path is just ``work_dir.parent``
    so the test can assert on a stable, predictable value.
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
    """Agent whose ``mngr_ctx.is_auto_approve=True`` so provision() pre-trusts silently."""
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


_BACKGROUND_TASKS_LAUNCH_PREFIX = "( bash $MNGR_AGENT_STATE_DIR/commands/antigravity_background_tasks.sh"


def test_assemble_command_invokes_agy_with_log_file(antigravity_agent: AntigravityAgent) -> None:
    """The foreground command runs `agy ... --log-file <agent-state>/logs/agy_cli.log`."""
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert " agy " in command
    assert "--log-file" in command
    assert "logs/agy_cli.log" in command


def test_assemble_command_appends_user_agent_args(antigravity_agent: AntigravityAgent) -> None:
    """User agent_args land between the agy command and the appended log-file/auto-allow flags."""
    command = str(
        antigravity_agent.assemble_command(antigravity_agent.host, ("--add-dir", "/tmp"), command_override=None)
    )
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
    confirmation dialog (verified live against agy 1.0.3).
    """
    agent = antigravity_agent_auto_allow
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert "--dangerously-skip-permissions" in command


def test_assemble_command_adds_hooks_via_nondotted_tmp_symlink(antigravity_agent: AntigravityAgent) -> None:
    """agy gets --add-dir pointing at a non-dotted /tmp symlink to the hooks dir.

    Regression for the hidden-path bug: agy rejects --add-dir paths with a
    dot-prefixed segment, so pointing it straight at the state-dir hooks path
    (under ~/.mngr/) silently loads no hooks. A /tmp symlink resolving to the
    durable hooks dir bypasses the rejection.
    """
    agent = antigravity_agent
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    hooks_symlink = agent._get_agy_hooks_symlink_path()
    hooks_dir = str(agent._get_agy_hooks_dir())
    # --add-dir uses the symlink, never the dotted state-dir path.
    assert f"--add-dir {hooks_symlink}" in command
    assert f"--add-dir {hooks_dir}" not in command
    # The symlink path must be non-dotted (no dot-prefixed segment) so agy accepts it.
    assert hooks_symlink.startswith("/tmp/")
    assert not any(segment.startswith(".") for segment in hooks_symlink.split("/") if segment)
    # And it is created (ln -sfn <hooks_dir> <symlink>) before agy launches.
    assert f"ln -sfn {hooks_dir} {hooks_symlink}" in command
    assert command.index(f"ln -sfn {hooks_dir} {hooks_symlink}") < command.index(" agy ")


def test_assemble_command_premakes_hooks_agents_dir(antigravity_agent: AntigravityAgent) -> None:
    """The foreground mkdir creates the hooks .agents dir so --add-dir never points at a missing path.

    provision() writes hooks.json there; the mkdir guarantees the directory
    exists at launch even when the hooks file has not been provisioned (e.g. a
    restart that runs before re-provision).
    """
    agent = antigravity_agent
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    hooks_agents_dir = str(agent._get_agy_hooks_file_path().parent)
    assert hooks_agents_dir in command.split(" agy ")[0]
    assert command.index("mkdir -p") < command.index(" agy "), command


def test_assemble_command_preserves_user_args_when_auto_allow_enabled(
    antigravity_agent_auto_allow: AntigravityAgent,
) -> None:
    """User-supplied agent_args land right after `agy`, with the auto-allow flag still appended after."""
    agent = antigravity_agent_auto_allow
    command = str(agent.assemble_command(agent.host, ("--add-dir", "/tmp"), command_override=None))
    assert "agy --add-dir /tmp --log-file" in command
    # The user args do not displace the appended auto-allow flag.
    assert "--dangerously-skip-permissions" in command
    assert command.index("agy --add-dir /tmp") < command.index("--dangerously-skip-permissions")


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
    The same `mkdir -p` also creates the workspace-symlink parent
    (``/tmp/mngr_antigravity_workspaces``); the test allows either path
    to appear inside the mkdir argument list.
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
    The symlink is recreated via ``ln -sfn`` so it's safe to re-run.
    """
    agent = antigravity_agent
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    expected_symlink = f"/tmp/mngr_antigravity_workspaces/{agent.id}"
    assert f"ln -sfn {agent.work_dir} {expected_symlink}" in command
    assert f"cd {expected_symlink} &&" in command
    # Ordering: mkdir -> ln -> cd -> agy
    mkdir_idx = command.index("mkdir -p")
    ln_idx = command.index("ln -sfn")
    cd_idx = command.index(f"cd {expected_symlink}")
    agy_idx = command.index(" agy ")
    assert mkdir_idx < ln_idx < cd_idx < agy_idx, command


def test_get_expected_process_name_returns_agy(antigravity_agent: AntigravityAgent) -> None:
    """`agy` is the single-file Go binary name visible to ps/tmux."""
    assert antigravity_agent.get_expected_process_name() == "agy"


def test_assemble_command_resumes_last_conversation_via_set_dash_dash(antigravity_agent: AntigravityAgent) -> None:
    """The launch command resumes the last-recorded conversation, evaluated in the shell.

    The stored command is replayed verbatim on every `mngr start`, so the
    resume decision is shell-evaluated at launch: read the last line of the
    per-agent conversation-ids file and, when present, pass `--conversation
    "$id"` via `set --` / "$@" (which avoids unquoted-substitution word
    splitting so it works in bash and zsh). We do not stat agy's store to
    pre-check existence -- agy warns and starts fresh on its own for a pruned
    conversation -- so the command stays decoupled from agy's on-disk layout.
    """
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    ids_file = str(antigravity_agent._get_conversation_ids_file_path())
    # Reads the last recorded id from the per-agent ids file.
    assert f"__mngr_cid=$(tail -n 1 {ids_file} 2>/dev/null || true)" in command
    # Passes the flag positionally whenever an id is recorded (no store stat).
    assert 'if [ -n "$__mngr_cid" ]; then set -- --conversation "$__mngr_cid"; fi' in command
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


def test_provision_does_not_create_workspace_subdirs(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """The plugin writes nothing to the user's work_dir.

    Antigravity reads workspace-tier files from `<work_dir>/.agents/` and
    `<work_dir>/.antigravityignore`; mngr leaves both alone so the user's
    project tree is untouched by ``mngr create``.

    Runs under ``auto_approve_ctx`` so ``provision`` takes the silent-trust
    branch; otherwise the default non-interactive non-auto-approve config
    would correctly raise rather than touch the user's work_dir.
    """
    agent = auto_approve_ctx
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
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


def _read_user_settings(monkeypatched_home: Path) -> dict[str, Any]:
    """Read the user-tier settings.json that the agent should have populated."""
    settings_path = monkeypatched_home / ".gemini" / "antigravity-cli" / "settings.json"
    if not settings_path.exists():
        return {}
    parsed: Any = json.loads(settings_path.read_text())
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a tmpdir so trust-file writes do not touch the user's real config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_provision_pre_trusts_workspace_under_auto_approve(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """``mngr create --yes`` (mngr_ctx.is_auto_approve) silently trusts the agy workspace symlink path."""
    agent = auto_approve_ctx
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
    settings = _read_user_settings(isolated_home)
    assert agent._get_agy_workspace_symlink_path() in settings["trustedWorkspaces"]


def test_provision_pre_trusts_workspace_under_auto_dismiss_dialogs(
    antigravity_agent_auto_dismiss: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """`auto_dismiss_dialogs=True` (per-agent-type opt-in) silently trusts the agy workspace symlink path."""
    agent = antigravity_agent_auto_dismiss
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
    settings = _read_user_settings(isolated_home)
    assert agent._get_agy_workspace_symlink_path() in settings["trustedWorkspaces"]


def test_provision_prompts_user_then_trusts_when_interactive_and_user_accepts(
    interactive_ctx_with_confirmation: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Mirror of mngr_claude's `_prompt_user_for_trust`: prompt, then write on yes."""
    agent = interactive_ctx_with_confirmation
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
    settings = _read_user_settings(isolated_home)
    assert agent._get_agy_workspace_symlink_path() in settings["trustedWorkspaces"]


def test_provision_aborts_when_interactive_and_user_declines(
    interactive_ctx_with_declination: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """If the user declines the prompt, exit cleanly via SystemExit.

    Using SystemExit (a ``BaseException``) rather than ``UserInputError``
    lets the abort propagate through ``provision_agent``'s
    ``ConcurrencyExceptionGroup`` wrapping unwrapped, so the operator sees
    a clean exit rather than a noisy auto-diagnostics traceback.
    """
    agent = interactive_ctx_with_declination
    with pytest.raises(SystemExit) as excinfo:
        agent.provision(
            host=agent.host,
            options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
            mngr_ctx=agent.mngr_ctx,
        )
    assert excinfo.value.code == 1
    settings_path = get_antigravity_user_settings_path()
    assert not settings_path.exists()


def test_provision_aborts_in_non_interactive_mode_without_opt_in(
    antigravity_agent: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Non-interactive without --yes or auto_dismiss_dialogs: exit cleanly rather than silently fail.

    Default mngr_ctx has is_interactive=False and is_auto_approve=False;
    the antigravity_agent fixture defaults auto_dismiss_dialogs=False, so
    no path to a trust write exists and we must abort. Mirrors Claude's
    ClaudeDirectoryNotTrustedError behavior; uses ``SystemExit`` rather
    than ``UserInputError`` to bypass provision_agent's concurrency-group
    exception wrapping.
    """
    with pytest.raises(SystemExit) as excinfo:
        antigravity_agent.provision(
            host=antigravity_agent.host,
            options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
            mngr_ctx=antigravity_agent.mngr_ctx,
        )
    assert excinfo.value.code == 1
    settings_path = get_antigravity_user_settings_path()
    assert not settings_path.exists()


def test_provision_dialog_dismissal_preserves_existing_settings(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Trust write must be additive: prior keys and entries stay verbatim."""
    agent = auto_approve_ctx
    settings_path = get_antigravity_user_settings_path()
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"trustedWorkspaces": ["/prior/workspace"], "colorScheme": "dark"}, indent=2))

    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )

    settings = _read_user_settings(isolated_home)
    assert "/prior/workspace" in settings["trustedWorkspaces"]
    assert agent._get_agy_workspace_symlink_path() in settings["trustedWorkspaces"]
    assert settings["colorScheme"] == "dark"


def test_provision_dialog_dismissal_is_idempotent(
    auto_approve_ctx: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """Two passes under auto-approve yield one trust entry, not duplicates."""
    agent = auto_approve_ctx
    options = CreateAgentOptions(agent_type=AgentTypeName("antigravity"))
    agent.provision(host=agent.host, options=options, mngr_ctx=agent.mngr_ctx)
    agent.provision(host=agent.host, options=options, mngr_ctx=agent.mngr_ctx)

    settings = _read_user_settings(isolated_home)
    trusted = settings["trustedWorkspaces"]
    assert trusted.count(agent._get_agy_workspace_symlink_path()) == 1


def test_provision_already_trusted_workspace_does_not_reprompt(
    interactive_ctx_with_declination: AntigravityAgent,
    isolated_home: Path,
) -> None:
    """If the workspace is already in trustedWorkspaces, no prompt fires.

    The declining-user fixture's prompt returns False; if the short-circuit
    weren't in place, this test would raise SystemExit.
    """
    agent = interactive_ctx_with_declination
    settings_path = get_antigravity_user_settings_path()
    settings_path.parent.mkdir(parents=True)
    pre_trusted = [agent._get_agy_workspace_symlink_path()]
    settings_path.write_text(json.dumps({"trustedWorkspaces": pre_trusted}))

    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )
    # The file is unchanged because the workspace was already trusted.
    assert json.loads(settings_path.read_text()) == {"trustedWorkspaces": pre_trusted}


def test_provision_silently_extends_trust_when_source_repo_already_trusted(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """A worktree of an already-trusted source repo gets trusted silently, no prompt.

    The declining-prompt subclass would raise SystemExit if the prompt fired;
    reaching the silent branch is what makes this test pass. Mirrors the UX
    goal: once you've granted trust to a source repo, spawning another worktree
    of the same repo shouldn't re-prompt.
    """
    agent = _make_subclassed_agent_with_flags(
        _DecliningAgentWithFakeSourceRoot, local_provider, tmp_path, AntigravityAgentConfig(), is_interactive=True
    )
    fake_source = str(agent.work_dir.parent)
    settings_path = get_antigravity_user_settings_path()
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"trustedWorkspaces": [fake_source]}))

    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )

    settings = _read_user_settings(isolated_home)
    assert settings["trustedWorkspaces"] == [fake_source, agent._get_agy_workspace_symlink_path()]


def test_provision_pre_trusts_both_source_and_workspace_under_auto_approve(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """`--yes` adds the source repo root AND the agy workspace symlink path, so future worktrees take the silent branch."""
    agent = _make_subclassed_agent_with_flags(
        _AntigravityAgentWithFakeSourceRoot, local_provider, tmp_path, AntigravityAgentConfig(), is_auto_approve=True
    )

    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )

    settings = _read_user_settings(isolated_home)
    fake_source = str(agent.work_dir.parent)
    assert settings["trustedWorkspaces"] == [fake_source, agent._get_agy_workspace_symlink_path()]


def test_provision_prompt_accept_trusts_both_source_and_workspace(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """When the user accepts the interactive prompt, both source and the agy workspace symlink path get trusted."""
    agent = _make_subclassed_agent_with_flags(
        _ConfirmingAgentWithFakeSourceRoot, local_provider, tmp_path, AntigravityAgentConfig(), is_interactive=True
    )

    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )

    settings = _read_user_settings(isolated_home)
    fake_source = str(agent.work_dir.parent)
    assert settings["trustedWorkspaces"] == [fake_source, agent._get_agy_workspace_symlink_path()]


def test_provision_does_not_duplicate_source_when_already_present(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_home: Path
) -> None:
    """The silent-extend branch must not re-append the source path that's already trusted."""
    agent = _make_subclassed_agent_with_flags(
        _DecliningAgentWithFakeSourceRoot, local_provider, tmp_path, AntigravityAgentConfig(), is_interactive=True
    )
    fake_source = str(agent.work_dir.parent)
    settings_path = get_antigravity_user_settings_path()
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"trustedWorkspaces": [fake_source, "/some/unrelated/path"]}))

    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )

    settings = _read_user_settings(isolated_home)
    assert settings["trustedWorkspaces"].count(fake_source) == 1
    assert agent._get_agy_workspace_symlink_path() in settings["trustedWorkspaces"]
    assert "/some/unrelated/path" in settings["trustedWorkspaces"]


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
    settings_path = get_antigravity_user_settings_path()
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"trustedWorkspaces": "not-a-list"}))

    with pytest.raises(UserInputError) as excinfo:
        agent.provision(
            host=agent.host,
            options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
            mngr_ctx=agent.mngr_ctx,
        )

    message = str(excinfo.value)
    assert "non-list trustedWorkspaces" in message
    assert str(settings_path) in message
    # The unexpected type's name (str) must appear so operators can grep for it.
    assert "str" in message
    # The settings file is left untouched.
    assert json.loads(settings_path.read_text()) == {"trustedWorkspaces": "not-a-list"}


def _provision(agent: AntigravityAgent) -> None:
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=agent.mngr_ctx,
    )


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


# =============================================================================
# Per-agent hooks.json provisioning
# =============================================================================


@pytest.fixture
def antigravity_agent_auto_allow_and_dismiss(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> AntigravityAgent:
    """auto_allow_permissions=True with auto_dismiss_dialogs=True so provision() completes in tests."""
    return _make_antigravity_agent(
        local_provider,
        tmp_path,
        AntigravityAgentConfig(auto_allow_permissions=True, auto_dismiss_dialogs=True),
    )


def _read_hooks_json(agent: AntigravityAgent) -> dict[str, Any]:
    """Read the per-agent hooks.json that provision() writes into the agent state dir."""
    parsed: Any = json.loads(agent._get_agy_hooks_file_path().read_text())
    assert isinstance(parsed, dict)
    return parsed


def test_provision_writes_hooks_json_into_state_dir_agents_subdir(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """hooks.json lands at <state>/agy_hooks/.agents/hooks.json -- the path agy reads via --add-dir."""
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    hooks_path = agent._get_agy_hooks_file_path()
    assert hooks_path == agent._get_agent_dir() / "agy_hooks" / ".agents" / "hooks.json"
    assert hooks_path.exists()


def test_provision_hooks_json_sets_active_marker_on_preinvocation_and_clears_on_stop(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """The active marker hooks are always present (they drive RUNNING vs WAITING)."""
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    mngr = _read_hooks_json(agent)["mngr"]
    assert mngr["PreInvocation"][0]["command"] == 'touch "$MNGR_AGENT_STATE_DIR/active"'
    assert mngr["Stop"][0]["command"] == 'rm -f "$MNGR_AGENT_STATE_DIR/active"'


def test_provision_hooks_json_never_includes_pretooluse(
    antigravity_agent_auto_allow_and_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """The provisioned hooks.json carries only lifecycle markers, never a PreToolUse hook.

    Even with auto_allow_permissions=True, auto-approval is the
    --dangerously-skip-permissions flag (see assemble_command), not a hook --
    agy's {"decision": "allow"} hook output does not gate the run_command
    dialog.
    """
    agent = antigravity_agent_auto_allow_and_dismiss
    _provision(agent)
    mngr = _read_hooks_json(agent)["mngr"]
    assert "PreToolUse" not in mngr
    assert set(mngr) == {"PreInvocation", "Stop"}


def test_provision_does_not_write_hooks_into_work_dir(
    antigravity_agent_auto_dismiss: AntigravityAgent, isolated_home: Path
) -> None:
    """Hooks live in the agent state dir, never in the user's work_dir/.agents."""
    agent = antigravity_agent_auto_dismiss
    _provision(agent)
    assert not (agent.work_dir / ".agents").exists()
