"""Unit tests for CodexAgentConfig and CodexAgent."""

from __future__ import annotations

import json
import shlex
import shutil
import tomllib
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.ratchet_testing.ratchets import assert_posix_compatible
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr_codex.codex_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_codex.codex_config import CLEAR_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_codex.codex_config import ROOT_SESSION_FILENAME
from imbue.mngr_codex.codex_config import SET_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import get_codex_personality_migration_path
from imbue.mngr_codex.codex_config import is_project_trusted
from imbue.mngr_codex.plugin import CodexAgent
from imbue.mngr_codex.plugin import CodexAgentConfig
from imbue.mngr_codex.plugin import CodexUpdatePolicy
from imbue.mngr_codex.plugin import _resolve_lifecycle_state_for_permission
from imbue.mngr_codex.plugin import _waiting_reason
from imbue.mngr_codex.plugin import agent_field_generators
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
    assert config.update_policy is CodexUpdatePolicy.ASK
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
# Capability-mixin contract methods (install / unattended / permission / version)
# =============================================================================


def test_get_install_binary_name_is_codex() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig())
    assert agent.get_install_binary_name() == "codex"


def test_get_install_command_installs_codex() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig())
    assert agent.get_install_command() == "npm i -g @openai/codex"


def test_is_unattended_enabled_reflects_auto_allow_permissions() -> None:
    unattended = CodexAgent.model_construct(agent_config=CodexAgentConfig(auto_allow_permissions=True))
    attended = CodexAgent.model_construct(agent_config=CodexAgentConfig())
    assert unattended.is_unattended_enabled() is True
    assert attended.is_unattended_enabled() is False


def test_get_permission_policy_carries_sandbox_mode() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig(sandbox_mode="read-only"))
    policy = agent.get_permission_policy()
    assert policy["sandbox_mode"] == "read-only"


def test_get_permission_policy_includes_approval_policy_override() -> None:
    agent = CodexAgent.model_construct(agent_config=CodexAgentConfig(config_overrides={"approval_policy": "never"}))
    policy = agent.get_permission_policy()
    assert policy["sandbox_mode"] == "workspace-write"
    assert policy["approval_policy"] == "never"


def test_reconcile_installed_version_delegates_to_update_check() -> None:
    # codex's version reconciliation IS its update check: reconcile resolves the codex
    # home and runs _maybe_check_for_codex_update against it. (The update decision logic
    # itself is covered by the _read_codex_versions-override tests below.)
    recorded: dict[str, object] = {}

    class _RecordingAgent(CodexAgent):
        def _resolve_user_codex_home(self, host: object) -> Path:
            return Path("/sentinel/codex-home")

        def _maybe_check_for_codex_update(self, host: object, user_codex_home: Path, mngr_ctx: object) -> None:
            recorded["home"] = user_codex_home

    agent = _RecordingAgent.model_construct(agent_config=CodexAgentConfig())
    agent.reconcile_installed_version(cast(OnlineHostInterface, object()), cast(MngrContext, object()))
    assert recorded["home"] == Path("/sentinel/codex-home")


class _StubHost(FakeHost):
    """FakeHost that returns scripted results for commands matched by substring.

    Records every command and returns the first ``command_results`` entry whose
    pattern is a substring of the command; otherwise falls through to the local
    FakeHost. Lets the host-shell helpers (version probe, codex update,
    CODEX_HOME resolution) be exercised without a real codex binary.
    """

    command_results: dict[str, CommandResult] = {}
    executed_commands: list[str] = []

    def _execute_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.executed_commands.append(command)
        for pattern, result in self.command_results.items():
            if pattern in command:
                return result
        return super()._execute_command(command, user, cwd, env, timeout_seconds)


def _stub_host(
    host_dir: Path,
    *,
    is_local: bool = True,
    command_results: dict[str, CommandResult] | None = None,
) -> Any:
    """Create a _StubHost typed as Any to satisfy OnlineHostInterface parameters in tests."""
    return _StubHost(host_dir=host_dir, is_local=is_local, command_results=command_results or {})


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
    # These setup tests run against a real local host where codex is not installed; the
    # install check is irrelevant to provision setup (files/trust/config) and is covered
    # separately, so skip it unless a caller opted in explicitly.
    if "check_installation" not in agent_config.model_fields_set:
        agent_config = agent_config.model_copy_update(to_update(agent_config.field_ref().check_installation, False))
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


def test_resolve_user_codex_home_aborts_when_resolution_fails(codex_agent: CodexAgent, tmp_path: Path) -> None:
    """A failed CODEX_HOME-resolution probe is fatal: the shared auth.json can't be located."""
    host = _stub_host(
        tmp_path,
        command_results={"${CODEX_HOME:-$HOME/.codex}": CommandResult(stdout="", stderr="boom", success=False)},
    )
    with pytest.raises(SystemExit):
        codex_agent._resolve_user_codex_home(host)


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
    # The permission-waiting marker hooks (PermissionRequest/PostToolUse) are wired too.
    assert PERMISSIONS_WAITING_FILENAME in hooks_text

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


class _OutdatedRealUpdateAgent(CodexAgent):
    """Reports a fixed outdated pair but runs the *real* ``_run_codex_update``.

    Unlike ``_OutdatedCodexAgent`` it does not stub ``_run_codex_update``, so the
    AUTO path exercises the actual ``codex update`` host call (stubbed on the host).
    """

    def _read_codex_versions(self, host: object, user_codex_home: Path) -> tuple[str | None, str | None]:
        return ("0.140.0", "0.141.0")


def _check_update(agent: CodexAgent) -> None:
    # user_codex_home is irrelevant in the decision tests (the probe is overridden).
    agent._maybe_check_for_codex_update(agent.host, agent.work_dir, agent.mngr_ctx)


def test_auto_policy_runs_codex_update_without_prompting(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """The AUTO policy applies the update directly and never consults the interactive prompt."""
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO),
        is_interactive=True,
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


def test_unattended_remote_host_only_notifies(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """A remote (non-local) host is unattended even from an interactive tty: notify, never prompt.

    Mirrors the claude plugin's ``is_unattended = not host.is_local``: provisioning a
    remote codex agent from a local interactive terminal must not prompt to upgrade the
    remote's global install (the prompt override raises if consulted), nor run the update.
    """
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent, local_provider, tmp_path, CodexAgentConfig(), is_interactive=True
    )
    remote_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    agent._maybe_check_for_codex_update(remote_host, agent.work_dir, agent.mngr_ctx)


def test_unattended_remote_host_still_auto_updates(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """The AUTO policy is an explicit opt-in, so it upgrades even on an unattended remote host."""
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO),
        is_interactive=True,
    )
    remote_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    with pytest.raises(_CodexUpdateRan):
        agent._maybe_check_for_codex_update(remote_host, agent.work_dir, agent.mngr_ctx)


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
    agent = _make_codex_agent(
        _UpToDateCodexAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    _check_update(agent)


def test_unknown_version_skips_update(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """An undeterminable version (codex absent / no cache) skips the check entirely."""
    agent = _make_codex_agent(
        _UnknownVersionCodexAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    _check_update(agent)


def test_never_policy_only_notifies(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """The NEVER policy runs the (cheap) check but only notifies -- never prompts, never updates.

    Even on an attended local interactive run, the prompt override raises if consulted and
    the update sentinel fires if run, so reaching neither confirms NEVER just logs the notice.
    """
    agent = _make_codex_agent(
        _OutdatedPromptRaisesAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(update_policy=CodexUpdatePolicy.NEVER),
        is_interactive=True,
    )
    _check_update(agent)


def test_auto_policy_runs_real_codex_update_on_success(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """AUTO with an outdated codex shells out ``codex update`` over the host; a clean exit just logs."""
    agent = _make_codex_agent(
        _OutdatedRealUpdateAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    host = _stub_host(tmp_path, command_results={"codex update": CommandResult(stdout="ok", stderr="", success=True)})
    agent._maybe_check_for_codex_update(host, agent.work_dir, agent.mngr_ctx)
    assert any("codex update" in command for command in host.executed_commands)


def test_auto_policy_real_codex_update_failure_is_not_fatal(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A non-zero ``codex update`` is warned about but never aborts provisioning."""
    agent = _make_codex_agent(
        _OutdatedRealUpdateAgent, local_provider, tmp_path, CodexAgentConfig(update_policy=CodexUpdatePolicy.AUTO)
    )
    host = _stub_host(
        tmp_path,
        command_results={"codex update": CommandResult(stdout="", stderr="updater missing", success=False)},
    )
    agent._maybe_check_for_codex_update(host, agent.work_dir, agent.mngr_ctx)
    assert any("codex update" in command for command in host.executed_commands)


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


def test_read_codex_versions_returns_none_when_probe_fails(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """A non-zero version probe (codex absent / shell error) yields (None, None) without raising."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig())
    host = _stub_host(
        tmp_path,
        command_results={"--version": CommandResult(stdout="", stderr="no such file", success=False)},
    )
    installed, latest = agent._read_codex_versions(host, tmp_path / ".codex")
    assert installed is None
    assert latest is None


@pytest.mark.parametrize("cache_body", ["{not valid json", "[]", '"0.139.0"', "42"])
def test_read_codex_versions_latest_is_none_for_unusable_cache(
    local_provider: LocalProviderInstance, tmp_path: Path, cache_body: str
) -> None:
    """A corrupt or non-object version.json yields latest=None without raising.

    `_parse_latest_codex_version` warning-logs a JSON decode error and otherwise
    returns None for any cache that is not a JSON object with a clean `latest_version`
    -- the update check then just skips rather than aborting provisioning. `installed`
    still parses, proving the unusable cache does not poison the whole probe.
    """
    agent = _make_codex_agent(
        CodexAgent, local_provider, tmp_path, CodexAgentConfig(command=CommandString("sh -c 'echo codex-cli 1.2.3'"))
    )
    user_home = agent._resolve_user_codex_home(agent.host)
    user_home.mkdir(parents=True, exist_ok=True)
    (user_home / "version.json").write_text(cache_body)
    installed, latest = agent._read_codex_versions(agent.host, user_home)
    assert installed == "1.2.3"
    assert latest is None


# =============================================================================
# Preservation on destroy
# =============================================================================


def test_codex_config_preserves_on_destroy_by_default() -> None:
    assert CodexAgentConfig().preserve_on_destroy is True


def _populate_codex_transcripts(agent: CodexAgent) -> None:
    """Write the raw/common transcripts and the root session-id history into the state dir."""
    agent_dir = agent._get_agent_dir()
    (agent_dir / "logs" / "codex_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "logs" / "codex_transcript" / "events.jsonl").write_text('{"type":"raw"}\n')
    (agent_dir / "events" / "codex" / "common_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "events" / "codex" / "common_transcript" / "events.jsonl").write_text('{"type":"common"}\n')
    (agent_dir / ROOT_SESSION_FILENAME).write_text("sess-codex\n")


@pytest.mark.rsync
def test_on_destroy_preserves_transcripts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy copies transcripts and session-id history to the mirrored preserved layout."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(preserve_on_destroy=True))
    _populate_codex_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert (dest_dir / "logs" / "codex_transcript" / "events.jsonl").read_text() == '{"type":"raw"}\n'
    assert (dest_dir / "events" / "codex" / "common_transcript" / "events.jsonl").read_text() == '{"type":"common"}\n'
    assert (dest_dir / ROOT_SESSION_FILENAME).read_text() == "sess-codex\n"


def test_on_destroy_skips_preservation_when_disabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy preserves nothing when preserve_on_destroy is False."""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(preserve_on_destroy=False))
    _populate_codex_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert not dest_dir.exists()


# =============================================================================
# Lifecycle promotion + waiting_reason field generator
# =============================================================================


@pytest.mark.parametrize(
    "base_state, is_blocked, expected",
    [
        # Only a RUNNING base is promoted, and only while blocked on a dialog.
        (AgentLifecycleState.RUNNING, True, AgentLifecycleState.WAITING),
        (AgentLifecycleState.RUNNING, False, AgentLifecycleState.RUNNING),
        # Every non-RUNNING base passes through unchanged, blocked or not.
        (AgentLifecycleState.WAITING, True, AgentLifecycleState.WAITING),
        (AgentLifecycleState.STOPPED, True, AgentLifecycleState.STOPPED),
        (AgentLifecycleState.REPLACED, True, AgentLifecycleState.REPLACED),
        (AgentLifecycleState.DONE, True, AgentLifecycleState.DONE),
        (
            AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
            True,
            AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
        ),
    ],
)
def test_resolve_lifecycle_state_for_permission(
    base_state: AgentLifecycleState, is_blocked: bool, expected: AgentLifecycleState
) -> None:
    assert _resolve_lifecycle_state_for_permission(base_state, is_blocked) == expected


@pytest.mark.tmux
def test_get_lifecycle_state_promotes_running_to_waiting_when_blocked_on_permission(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """End-to-end override against a live pane: the base state is RUNNING, and a
    permissions_waiting marker promotes it to WAITING; removing the marker restores
    RUNNING. (The promotion rule itself is unit-tested above without tmux.)"""
    agent = _make_codex_agent(CodexAgent, local_provider, tmp_path, CodexAgentConfig(), is_auto_approve=True)
    # A long-lived process that ps reports as "codex" (the expected process name) so
    # the base lifecycle reads RUNNING -- the renamed-sleep trick from base_agent_test.
    sleep_bin = shutil.which("sleep")
    assert sleep_bin is not None
    fake_codex = tmp_path / "codex"
    shutil.copy(sleep_bin, fake_codex)
    fake_codex.chmod(0o755)
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session_name = agent.session_name
    agent.host.execute_idempotent_command(
        f"tmux new-session -d -s {shlex.quote(session_name)} {shlex.quote(str(fake_codex))} 600",
        timeout_seconds=5.0,
    )
    try:
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.RUNNING,
            error_message="expected codex agent to read RUNNING with a live pane",
        )
        (agent_dir / PERMISSIONS_WAITING_FILENAME).touch()
        assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING
        (agent_dir / PERMISSIONS_WAITING_FILENAME).unlink()
        assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING
    finally:
        cleanup_tmux_session(session_name)


def test_agent_field_generators_exposes_codex_waiting_reason() -> None:
    result = agent_field_generators()
    assert result is not None
    plugin_name, generators = result
    assert plugin_name == "codex"
    assert "waiting_reason" in generators
    assert callable(generators["waiting_reason"])


def test_waiting_reason_returns_permissions_when_active_and_blocked(codex_agent: CodexAgent) -> None:
    """A real open dialog: the active marker (set at turn start) is present *and*
    permissions_waiting is present, so the agent is blocked on an approval dialog."""
    agent_dir = codex_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ACTIVE_MARKER_FILENAME).touch()
    (agent_dir / PERMISSIONS_WAITING_FILENAME).touch()
    assert _waiting_reason(codex_agent, codex_agent.host) == WaitingReason.PERMISSIONS


def test_waiting_reason_ignores_stranded_permissions_marker_after_turn(codex_agent: CodexAgent) -> None:
    """A stranded permissions_waiting marker (active absent -> turn over) reports
    END_OF_TURN, not PERMISSIONS. The PERMISSIONS verdict is gated on the active
    marker, so correctness does not depend on the Stop/UserPromptSubmit safety nets
    having deleted the file."""
    agent_dir = codex_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / PERMISSIONS_WAITING_FILENAME).touch()
    assert _waiting_reason(codex_agent, codex_agent.host) == WaitingReason.END_OF_TURN


def test_waiting_reason_returns_end_of_turn_when_idle(codex_agent: CodexAgent) -> None:
    agent_dir = codex_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    assert _waiting_reason(codex_agent, codex_agent.host) == WaitingReason.END_OF_TURN


def test_waiting_reason_returns_none_when_active(codex_agent: CodexAgent) -> None:
    agent_dir = codex_agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ACTIVE_MARKER_FILENAME).touch()
    assert _waiting_reason(codex_agent, codex_agent.host) is None
