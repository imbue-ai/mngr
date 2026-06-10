"""Unit tests for CodexAgentConfig and CodexAgent."""

from __future__ import annotations

import json
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
    assert config.check_for_updates is True
    assert config.auto_update is False
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


def test_assemble_command_resets_stale_marker_state(codex_agent: CodexAgent) -> None:
    """Each launch clears stale lifecycle-marker state a SIGKILL-mid-turn stop can leave.

    Otherwise a killed subagent's `codex_subagents/<id>` file (whose SubagentStop
    will never arrive) or a leftover `codex_root_active`/`active` could strand the
    resumed agent as RUNNING. The resume id (`codex_root_session`) is NOT reset.
    """
    command = str(codex_agent.assemble_command(codex_agent.host, (), None))
    assert 'rm -rf "$MNGR_AGENT_STATE_DIR/active"' in command
    assert "$MNGR_AGENT_STATE_DIR/codex_root_active" in command
    assert "$MNGR_AGENT_STATE_DIR/codex_subagents" in command
    assert "$MNGR_AGENT_STATE_DIR/codex_marker.lock" in command
    # The reset must run before codex launches but must not clobber the resume id.
    assert command.index("rm -rf") < command.index("env CODEX_HOME=")
    assert "$MNGR_AGENT_STATE_DIR/codex_root_session" not in command.split("env CODEX_HOME=")[0].split("rm -rf")[1]


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
# Trust / hook-bypass consent
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


# =============================================================================
# Update check / auto-update
# =============================================================================


class _CodexUpdateRan(Exception):
    """Raised by a test override to signal that `codex update` would have run."""


class _CodexUpdatePrompted(Exception):
    """Raised by a test override to signal that the interactive update prompt was shown."""


class _OutdatedCodexAgent(CodexAgent):
    """Reports a fixed outdated (installed, latest) and records an update via a sentinel.

    Overriding `_read_codex_versions` keeps the decision tests off any real codex
    binary or version.json; `_run_codex_update` raises so a test can assert whether
    the update path was taken.
    """

    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return ("0.138.0", "0.139.0")

    def _run_codex_update(self, host: object, installed: str, latest: str) -> None:
        raise _CodexUpdateRan()


class _OutdatedPromptYesAgent(_OutdatedCodexAgent):
    def _prompt_user_to_update_codex(self, installed: str, latest: str) -> bool:
        return True


class _OutdatedPromptNoAgent(_OutdatedCodexAgent):
    def _prompt_user_to_update_codex(self, installed: str, latest: str) -> bool:
        return False


class _OutdatedPromptRaisesAgent(_OutdatedCodexAgent):
    """Prompt raises, so a test can assert the prompt is NOT consulted."""

    def _prompt_user_to_update_codex(self, installed: str, latest: str) -> bool:
        raise _CodexUpdatePrompted()


class _UpToDateCodexAgent(_OutdatedCodexAgent):
    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return ("0.139.0", "0.139.0")


class _UnknownVersionCodexAgent(_OutdatedCodexAgent):
    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return (None, None)


class _ProbeRaisesCodexAgent(CodexAgent):
    """`_read_codex_versions` raises, so a test can assert it is never called."""

    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        raise AssertionError("the version probe should not run when the check is disabled")


def _check_update(agent: CodexAgent) -> None:
    # user_codex_home is irrelevant in the decision tests (the probe is overridden).
    agent._maybe_check_for_codex_update(agent.host, agent.work_dir, agent.mngr_ctx)


def test_auto_update_runs_codex_update_without_prompting(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """auto_update applies the update directly and never consults the interactive prompt."""
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent, local_provider, tmp_path, CodexAgentConfig(auto_update=True), is_interactive=True
    )
    with pytest.raises(_CodexUpdateRan):
        _check_update(agent)


def test_interactive_prompt_yes_runs_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(
        _OutdatedPromptYesAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True
    )
    with pytest.raises(_CodexUpdateRan):
        _check_update(agent)


def test_interactive_prompt_no_does_not_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """A declined prompt falls through to the non-blocking notice -- no update, no abort."""
    agent = _make_codex_agent(
        _OutdatedPromptNoAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True
    )
    _check_update(agent)


def test_non_interactive_only_notifies(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """Non-interactive: never prompt and never mutate the global install -- just notify."""
    agent = _make_codex_agent(_OutdatedPromptRaisesAgent, local_provider, tmp_path, CodexAgentConfig())
    _check_update(agent)


def test_auto_approve_does_not_trigger_global_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """`--yes` clears blocking prerequisites but does NOT opt into a heavy global upgrade."""
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(),
        is_interactive=True,
        is_auto_approve=True,
    )
    _check_update(agent)


def test_up_to_date_skips_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    agent = _make_codex_agent(_UpToDateCodexAgent, local_provider, tmp_path, CodexAgentConfig(auto_update=True))
    _check_update(agent)


def test_unknown_version_skips_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """An undeterminable version (codex absent / no cache) skips the check entirely."""
    agent = _make_codex_agent(_UnknownVersionCodexAgent, local_provider, tmp_path, CodexAgentConfig(auto_update=True))
    _check_update(agent)


def test_disabled_check_skips_the_probe(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """With both check_for_updates and auto_update off, the version probe never runs."""
    agent = _make_codex_agent(
        _ProbeRaisesCodexAgent, local_provider, tmp_path, CodexAgentConfig(check_for_updates=False)
    )
    _check_update(agent)


def test_read_codex_versions_parses_installed_and_cached(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The real probe parses `codex --version` and the version.json cache over the host.

    Uses a fake `command` (`sh -c 'echo ...'` ignores the appended --version) so the
    test needs no real codex binary; the cache is a real file under the user's home.
    """
    agent = _make_codex_agent(
        CodexAgent, local_provider, tmp_path, CodexAgentConfig(command=CommandString("sh -c 'echo codex-cli 1.2.3'"))
    )
    user_home = agent._resolve_user_codex_home(agent.host)
    user_home.mkdir(parents=True, exist_ok=True)
    (user_home / "version.json").write_text(
        json.dumps({"latest_version": "1.3.0", "last_checked_at": "2026-06-09T00:00:00Z", "dismissed_version": None})
    )
    installed, latest = agent._read_codex_versions(agent.host, user_home)
    assert installed == "1.2.3"
    assert latest == "1.3.0"


def test_read_codex_versions_latest_is_none_when_cache_absent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_codex_agent(
        CodexAgent, local_provider, tmp_path, CodexAgentConfig(command=CommandString("sh -c 'echo codex-cli 1.2.3'"))
    )
    installed, latest = agent._read_codex_versions(agent.host, agent._resolve_user_codex_home(agent.host))
    assert installed == "1.2.3"
    assert latest is None
