"""Unit tests for CodexAgentConfig and CodexAgent."""

from __future__ import annotations

import tomllib
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.ratchet_testing.ratchets import assert_posix_compatible
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_codex.codex_config import CLEAR_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import SET_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import get_codex_personality_migration_path
from imbue.mngr_codex.codex_config import is_project_trusted
from imbue.mngr_codex.plugin import CodexAgent
from imbue.mngr_codex.plugin import CodexAgentConfig
from imbue.mngr_codex.plugin import register_agent_type

# =============================================================================
# Config
# =============================================================================


def test_codex_agent_config_has_correct_defaults() -> None:
    config = CodexAgentConfig()
    assert str(config.command) == "codex"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.model is None
    assert config.model_reasoning_effort is None
    assert config.sandbox_mode == "workspace-write"
    assert config.auto_allow_permissions is False
    assert config.auto_dismiss_dialogs is False
    assert config.config_overrides == {}
    assert config.emit_common_transcript is True


def test_codex_agent_config_merge_with_replaces_cli_args() -> None:
    base = CodexAgentConfig()
    override = CodexAgentConfig(cli_args=("--foo",))
    merged = base.merge_with(override)
    assert isinstance(merged, CodexAgentConfig)
    assert merged.cli_args == ("--foo",)
    assert str(merged.command) == "codex"


def test_codex_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(CodexAgent, InteractiveTuiAgent)


def test_codex_agent_advertises_tui_ready_indicator() -> None:
    """The ready indicator is a fixed header string that renders with the input composer.

    codex has no pre-input readiness hook (SessionStart fires lazily on the first
    prompt), so this banner poll is the readiness signal.
    """
    assert CodexAgent.TUI_READY_INDICATOR == "/model to change"


def test_codex_agent_implements_send_enter_and_validate() -> None:
    assert "_send_enter_and_validate" not in CodexAgent.__abstractmethods__


def test_register_agent_type_returns_codex_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "codex"
    assert agent_class is CodexAgent
    assert config_class is CodexAgentConfig


# =============================================================================
# Construction helpers
# =============================================================================


def _make_codex_agent(
    agent_cls: type[CodexAgent],
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: CodexAgentConfig,
    *,
    is_interactive: bool = False,
    is_auto_approve: bool = False,
) -> CodexAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    ctx = local_provider.mngr_ctx.model_copy_update(
        to_update(local_provider.mngr_ctx.field_ref().is_interactive, is_interactive),
        to_update(local_provider.mngr_ctx.field_ref().is_auto_approve, is_auto_approve),
    )
    return agent_cls.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=ctx,
        agent_config=agent_config,
        host=host,
    )


class _ConfirmingCodexAgent(CodexAgent):
    """Test subclass whose trust prompt auto-accepts without invoking click.confirm."""

    def _prompt_user_to_trust_workspace(self, source_path: Path, config_path: Path) -> bool:
        return True


class _DecliningCodexAgent(CodexAgent):
    """Test subclass whose trust prompt auto-declines without invoking click.confirm."""

    def _prompt_user_to_trust_workspace(self, source_path: Path, config_path: Path) -> bool:
        return False


@pytest.fixture
def codex_agent(local_provider: LocalProviderInstance, tmp_path: Path) -> CodexAgent:
    return _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)


# =============================================================================
# Simple accessors
# =============================================================================


def test_get_expected_process_name(codex_agent: CodexAgent) -> None:
    assert codex_agent.get_expected_process_name() == "codex"


def test_is_common_transcript_enabled_follows_config(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    on = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig())
    off = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(emit_common_transcript=False))
    assert on.is_common_transcript_enabled is True
    assert off.is_common_transcript_enabled is False


def test_transcript_scripts_are_loadable(codex_agent: CodexAgent) -> None:
    """Both transcript-script accessors return non-empty shell content from resources/."""
    raw = codex_agent.get_raw_transcript_scripts()
    common = codex_agent.get_common_transcript_scripts()
    assert "stream_transcript.sh" in raw
    assert raw["stream_transcript.sh"].strip() != ""
    assert "common_transcript.sh" in common
    assert common["common_transcript.sh"].strip() != ""


def test_codex_home_and_root_session_paths(codex_agent: CodexAgent) -> None:
    state_dir = codex_agent._get_agent_dir()
    assert codex_agent._get_codex_home() == get_codex_home(state_dir)
    assert codex_agent._get_root_session_file_path() == state_dir / "codex_root_session"


# =============================================================================
# Host-shell resolution helpers
# =============================================================================


def test_resolve_user_codex_home_defaults_to_home_dot_codex(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """With no CODEX_HOME override, resolves to $HOME/.codex (HOME is test-redirected)."""
    host = codex_agent.host
    resolved = codex_agent._resolve_user_codex_home(host)
    assert resolved.name == ".codex"
    assert resolved.parent == Path.home()


def test_resolve_canonical_path_resolves_symlinks(codex_agent: CodexAgent, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    resolved = codex_agent._resolve_canonical_path(codex_agent.host, link)
    assert Path(resolved) == real.resolve()


# =============================================================================
# assemble_command
# =============================================================================


def test_assemble_command_structure(codex_agent: CodexAgent) -> None:
    command = str(codex_agent.assemble_command(codex_agent.host, (), None))
    codex_home = str(codex_agent._get_codex_home())
    # Backgrounded supervisor, scoped to `&` so codex is the foreground process.
    assert "codex_background_tasks.sh" in command
    assert command.split("&", 1)[0].strip().startswith("( bash")
    # cwd is the work dir (codex accepts the dotted path; no symlink workaround).
    assert f"cd {codex_agent.work_dir}" in command
    # CODEX_HOME injected only on the codex process; hook trust bypassed.
    assert f"env CODEX_HOME={codex_home}" in command
    assert "--dangerously-bypass-hook-trust" in command
    # Resume prelude reads the recorded root session id and selects `resume <id>`.
    assert "codex_root_session" in command
    assert "set -- resume" in command


def test_assemble_command_appends_cli_and_agent_args(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(cli_args=("--oss",)))
    command = str(agent.assemble_command(agent.host, ("hello world",), None))
    assert "--oss" in command
    # agent_args are shell-quoted (raw argv).
    assert "'hello world'" in command


def test_assemble_command_honors_command_override(codex_agent: CodexAgent) -> None:
    command = str(codex_agent.assemble_command(codex_agent.host, (), CommandString("/opt/codex")))
    assert "env CODEX_HOME=" in command
    assert "/opt/codex --dangerously-bypass-hook-trust" in command


def test_assemble_command_is_posix_compatible(codex_agent: CodexAgent) -> None:
    """The assembled command runs in the user's interactive shell (possibly zsh), so it
    must avoid bash-only constructs (the resume prelude uses POSIX `set --` / "$@")."""
    command = str(codex_agent.assemble_command(codex_agent.host, ("a b", "--flag"), None))
    assert_posix_compatible(command)


# =============================================================================
# provision
# =============================================================================


def _provision(agent: CodexAgent) -> None:
    agent.provision(
        agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("codex")),
        mngr_ctx=agent.mngr_ctx,
    )


def test_provision_builds_the_codex_home_tree(codex_agent: CodexAgent, isolated_codex_home: Path) -> None:
    """provision writes config.toml (pin + trust), hooks.json, the auth symlink, and the NUX marker."""
    _provision(codex_agent)
    codex_home = codex_agent._get_codex_home()

    config_path = get_codex_config_path(codex_home)
    assert config_path.exists()
    config = tomllib.loads(config_path.read_text())
    assert config["cli_auth_credentials_store"] == "file"
    assert config["sandbox_mode"] == "workspace-write"
    # The (canonicalized) work dir is seeded as a trusted project.
    canonical_work = str(codex_agent.work_dir.resolve())
    assert is_project_trusted(config, canonical_work)

    hooks_path = get_codex_hooks_path(codex_home)
    assert hooks_path.exists()
    hooks_text = hooks_path.read_text()
    assert SET_ACTIVE_MARKER_SCRIPT_NAME in hooks_text
    assert CLEAR_ACTIVE_MARKER_SCRIPT_NAME in hooks_text

    # auth.json is a symlink to the shared user auth.json. With the shared auth
    # seeded (isolated_codex_home), the symlink resolves to that real file rather
    # than dangling.
    auth_path = get_codex_auth_path(codex_home)
    assert auth_path.is_symlink()
    shared_auth = get_codex_auth_path(isolated_codex_home / ".codex")
    assert auth_path.resolve() == shared_auth.resolve()
    assert auth_path.read_text() == shared_auth.read_text()

    # The personality-migration NUX-skip marker exists.
    assert get_codex_personality_migration_path(codex_home).exists()


def test_provision_sets_approval_never_when_auto_allow(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(
        CodexAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(auto_allow_permissions=True),
        is_auto_approve=True,
    )
    _provision(agent)
    config = tomllib.loads(get_codex_config_path(agent._get_codex_home()).read_text())
    assert config["approval_policy"] == "never"


# =============================================================================
# Trust / hook-bypass consent (mirrors mngr_claude / antigravity)
# =============================================================================


def _read_user_codex_config(agent: CodexAgent) -> dict[str, object]:
    user_config = get_codex_config_path(agent._resolve_user_codex_home(agent.host))
    if not user_config.exists():
        return {}
    return tomllib.loads(user_config.read_text())


def test_auto_approve_silently_persists_durable_trust(codex_agent: CodexAgent) -> None:
    """`--yes` (is_auto_approve) records the source repo trust in the user's global config."""
    _provision(codex_agent)
    user_config = _read_user_codex_config(codex_agent)
    canonical_source = str(codex_agent.work_dir.resolve())
    assert is_project_trusted(user_config, canonical_source)


def test_already_trusted_source_is_a_noop(codex_agent: CodexAgent) -> None:
    """A second provision does not re-prompt or error (idempotent durable trust)."""
    _provision(codex_agent)
    # Second provision: source is already trusted -> the consent path is skipped.
    _provision(codex_agent)
    user_config = _read_user_codex_config(codex_agent)
    canonical_source = str(codex_agent.work_dir.resolve())
    assert is_project_trusted(user_config, canonical_source)


def test_interactive_confirm_persists_trust(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(_ConfirmingCodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True)
    _provision(agent)
    assert is_project_trusted(_read_user_codex_config(agent), str(agent.work_dir.resolve()))


def test_interactive_decline_aborts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(_DecliningCodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True)
    with pytest.raises(SystemExit):
        _provision(agent)


def test_non_interactive_without_optin_aborts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """Default ctx (not interactive, not auto-approve) must refuse to run on untrusted code."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig())
    with pytest.raises(SystemExit):
        _provision(agent)


def test_auto_dismiss_dialogs_persists_trust_without_optin_ctx(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """auto_dismiss_dialogs trusts silently even when the ctx is non-interactive."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(auto_dismiss_dialogs=True))
    _provision(agent)
    assert is_project_trusted(_read_user_codex_config(agent), str(agent.work_dir.resolve()))
