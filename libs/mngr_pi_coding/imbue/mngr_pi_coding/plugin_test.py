"""Unit tests for the pi-coding plugin."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig
from imbue.mngr_pi_coding.plugin import _LIFECYCLE_EXTENSION_NAME
from imbue.mngr_pi_coding.plugin import _load_resource
from imbue.mngr_pi_coding.plugin import _read_pi_trust
from imbue.mngr_pi_coding.plugin import _serialize_pi_trust
from imbue.mngr_pi_coding.plugin import register_agent_type

# =============================================================================
# Test helpers
# =============================================================================


class _StubHost(FakeHost):
    """FakeHost that stubs specific commands and provides get_env_var."""

    command_results: dict[str, CommandResult] = {}
    env_vars: dict[str, str] = {}

    def _execute_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        for pattern, result in self.command_results.items():
            if pattern in command:
                return result
        return super()._execute_command(command, user, cwd, env, timeout_seconds)

    def get_env_var(self, key: str) -> str | None:
        return self.env_vars.get(key)


def _fake_host(host_dir: Path, *, is_local: bool = True) -> Any:
    """Create a FakeHost typed as Any to satisfy OnlineHostInterface parameters in tests."""
    return FakeHost(host_dir=host_dir, is_local=is_local)


def _stub_host(
    host_dir: Path,
    *,
    is_local: bool = True,
    command_results: dict[str, CommandResult] | None = None,
) -> Any:
    """Create a _StubHost typed as Any to satisfy OnlineHostInterface parameters in tests."""
    return _StubHost(
        host_dir=host_dir,
        is_local=is_local,
        command_results=command_results or {},
    )


def _make_options() -> CreateAgentOptions:
    return CreateAgentOptions(
        name=AgentName("test"),
        agent_type=AgentTypeName("pi-coding"),
        environment=AgentEnvironmentOptions(),
    )


def _make_test_mngr_ctx(tmp_path: Path, *, is_auto_approve: bool = False) -> MngrContext:
    return make_mngr_ctx(
        config=MngrConfig(),
        pm=pluggy.PluginManager("mngr"),
        profile_dir=tmp_path / "profile",
        is_interactive=False,
        is_auto_approve=is_auto_approve,
        concurrency_group=ConcurrencyGroup(name="test"),
    )


def _setup_home_pi(tmp_path: Path) -> Path:
    """Create a fake ~/.pi/agent/ directory and return the home path."""
    home_pi = tmp_path / "home" / ".pi" / "agent"
    home_pi.mkdir(parents=True)
    return tmp_path / "home"


@pytest.fixture()
def pi_agent(tmp_path: Path) -> PiCodingAgent:
    """Create a minimally-configured PiCodingAgent for testing."""
    agent = PiCodingAgent.__new__(PiCodingAgent)
    object.__setattr__(agent, "agent_config", PiCodingAgentConfig())
    object.__setattr__(agent, "host", _fake_host(tmp_path))
    object.__setattr__(agent, "id", AgentId.generate())
    object.__setattr__(agent, "name", AgentName("test-pi"))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    object.__setattr__(agent, "work_dir", work_dir)
    return agent


# =============================================================================
# PiCodingAgentConfig tests
# =============================================================================


def test_pi_coding_agent_config_has_correct_defaults() -> None:
    config = PiCodingAgentConfig()

    assert str(config.command) == "pi"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.sync_home_settings is True
    assert config.sync_auth is True
    assert config.check_installation is True
    assert config.resume_session is True
    assert config.emit_common_transcript is True
    assert config.emit_raw_transcript is True


def test_pi_coding_agent_config_merge_with_override() -> None:
    base = PiCodingAgentConfig()
    override = PiCodingAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, PiCodingAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "pi"


# =============================================================================
# PiCodingAgent method tests
# =============================================================================


def test_tui_ready_indicator_is_pi_v() -> None:
    assert PiCodingAgent.TUI_READY_INDICATOR == "pi v"


def test_pi_agent_implements_send_enter_and_validate() -> None:
    """PiCodingAgent provides a concrete _send_enter_and_validate, satisfying the abstract method."""
    assert "_send_enter_and_validate" not in PiCodingAgent.__abstractmethods__


def test_get_expected_process_name_returns_pi(pi_agent: PiCodingAgent) -> None:
    assert pi_agent.get_expected_process_name() == "pi"


def test_register_agent_type_returns_correct_tuple() -> None:
    name, agent_class, config_class = register_agent_type()

    assert name == "pi-coding"
    assert agent_class is PiCodingAgent
    assert config_class is PiCodingAgentConfig


def test_modify_env_vars_sets_pi_dir(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    env_vars: dict[str, str] = {}
    pi_agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    assert "PI_CODING_AGENT_DIR" in env_vars
    assert "plugin/pi_coding" in env_vars["PI_CODING_AGENT_DIR"]


def test_get_pi_config_dir(pi_agent: PiCodingAgent) -> None:
    config_dir = pi_agent.get_pi_config_dir()
    assert str(config_dir).endswith("plugin/pi_coding")


def test_get_provision_file_transfers_returns_empty(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    host = _fake_host(tmp_path)
    options = _make_options()
    mngr_ctx = _make_test_mngr_ctx(tmp_path)
    assert pi_agent.get_provision_file_transfers(host, options, mngr_ctx) == []


# =============================================================================
# on_before_provisioning tests
# =============================================================================


def test_on_before_provisioning_completes_without_credentials(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    """Verify on_before_provisioning completes (with warning) when no API credentials are found."""
    _setup_home_pi(tmp_path)
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    mngr_ctx = _make_test_mngr_ctx(tmp_path)

    pi_agent.on_before_provisioning(host, options, mngr_ctx)


# =============================================================================
# Provisioning tests
# =============================================================================


def test_setup_local_config_dir_symlinks_auth(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "auth.json").write_text('{"anthropic": {}}')

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=True)
    config = PiCodingAgentConfig()

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    assert (config_dir / "auth.json").is_symlink()


def test_setup_remote_config_dir_copies_auth(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    auth_content = '{"anthropic": {"type": "api_key"}}'
    (home / ".pi" / "agent" / "auth.json").write_text(auth_content)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=False)
    config = PiCodingAgentConfig()

    pi_agent._setup_remote_config_dir(host, config, config_dir, home)

    assert (config_dir / "auth.json").read_text() == auth_content


def test_setup_local_config_dir_symlinks_settings(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "settings.json").write_text('{"defaultModel": "sonnet"}')

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=True)
    config = PiCodingAgentConfig(sync_home_settings=True)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    assert (config_dir / "settings.json").is_symlink()


def test_setup_local_config_dir_skips_settings_when_disabled(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "settings.json").write_text('{"defaultModel": "sonnet"}')

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=True)
    config = PiCodingAgentConfig(sync_home_settings=False)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    assert not (config_dir / "settings.json").exists()


def test_setup_local_config_dir_symlinks_resource_dirs(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "skills").mkdir()
    (home / ".pi" / "agent" / "prompts").mkdir()
    # `agents` holds subagent definitions read by subagent extensions.
    (home / ".pi" / "agent" / "agents").mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=True)
    config = PiCodingAgentConfig(sync_home_settings=True)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    assert (config_dir / "skills").is_symlink()
    assert (config_dir / "prompts").is_symlink()
    assert (config_dir / "agents").is_symlink()


def test_setup_remote_config_dir_copies_settings(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    settings_content = '{"defaultModel": "sonnet"}'
    (home / ".pi" / "agent" / "settings.json").write_text(settings_content)

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=False)
    config = PiCodingAgentConfig(sync_home_settings=True, sync_auth=False)

    pi_agent._setup_remote_config_dir(host, config, config_dir, home)

    assert (config_dir / "settings.json").read_text() == settings_content


def test_setup_remote_config_dir_copies_resource_files(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    skills_dir = home / ".pi" / "agent" / "skills"
    skills_dir.mkdir()
    (skills_dir / "test-skill.md").write_text("# Test Skill")

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=False)
    config = PiCodingAgentConfig(sync_home_settings=True, sync_auth=False)

    # Resource dirs are transferred via host.copy_local_directory (rsync). FakeHost's
    # copy_local_directory does a local shutil copytree, so the files land under config_dir.
    # NOTE: FakeHost ignores the rsync --include/--exclude args this code passes, so
    # this test only confirms resource files get transferred -- it does NOT verify the
    # include/exclude filtering (e.g. it would not catch a broken dir_name list). The
    # real rsync filter mechanism is exercised by the host.copy_directory tests in
    # libs/mngr (test_host.py).
    pi_agent._setup_remote_config_dir(host, config, config_dir, home)

    assert (config_dir / "skills" / "test-skill.md").read_text() == "# Test Skill"


def test_setup_skips_sync_when_disabled(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "auth.json").write_text('{"test": true}')
    (home / ".pi" / "agent" / "settings.json").write_text('{"test": true}')

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=True)
    config = PiCodingAgentConfig(sync_home_settings=False, sync_auth=False)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    assert not (config_dir / "auth.json").exists()
    assert not (config_dir / "settings.json").exists()


def test_provision_raises_when_pi_not_installed_locally(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    host = _stub_host(
        tmp_path,
        is_local=True,
        command_results={"command -v pi": CommandResult(stdout="", stderr="", success=False)},
    )
    options = _make_options()
    mngr_ctx = _make_test_mngr_ctx(tmp_path, is_auto_approve=False)

    with pytest.raises(PluginMngrError, match="pi is not installed"):
        pi_agent.provision(host, options, mngr_ctx)


def test_provision_auto_installs_on_remote(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    host = _stub_host(
        tmp_path,
        is_local=False,
        command_results={
            "command -v pi": CommandResult(stdout="", stderr="", success=False),
            "npm install -g": CommandResult(stdout="installed", stderr="", success=True),
            "mkdir -p": CommandResult(stdout="", stderr="", success=True),
        },
    )
    config = PiCodingAgentConfig(check_installation=True, sync_auth=False, sync_home_settings=False)
    object.__setattr__(pi_agent, "agent_config", config)
    object.__setattr__(pi_agent, "host", host)
    options = _make_options()
    # is_auto_approve so the workspace-trust gate proceeds silently (the autouse
    # HOME redirect keeps the global trust write inside the test's temp home).
    mngr_ctx = _make_test_mngr_ctx(tmp_path, is_auto_approve=True)

    # The trust gate resolves the git source via the concurrency group, which
    # must be active (it is during real provisioning).
    with mngr_ctx.concurrency_group:
        pi_agent.provision(host, options, mngr_ctx)


def test_provision_raises_when_remote_install_disabled(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    host = _stub_host(
        tmp_path,
        is_local=False,
        command_results={"command -v pi": CommandResult(stdout="", stderr="", success=False)},
    )
    options = _make_options()
    mngr_ctx = make_mngr_ctx(
        config=MngrConfig(is_remote_agent_installation_allowed=False),
        pm=pluggy.PluginManager("mngr"),
        profile_dir=tmp_path / "profile",
        is_interactive=False,
        is_auto_approve=False,
        concurrency_group=ConcurrencyGroup(name="test"),
    )

    with pytest.raises(PluginMngrError, match="automatic remote installation is disabled"):
        pi_agent.provision(host, options, mngr_ctx)


# =============================================================================
# Transcript mixin
# =============================================================================


def test_is_common_transcript_enabled_reflects_config(pi_agent: PiCodingAgent) -> None:
    assert pi_agent.is_common_transcript_enabled is True
    object.__setattr__(pi_agent, "agent_config", PiCodingAgentConfig(emit_common_transcript=False))
    assert pi_agent.is_common_transcript_enabled is False


def test_transcript_scripts_are_empty_because_extension_emits(pi_agent: PiCodingAgent) -> None:
    # pi emits both transcript layers from the lifecycle extension, so there are
    # no commands/ converter scripts to provision (unlike claude/agy).
    assert pi_agent.get_raw_transcript_scripts() == {}
    assert pi_agent.get_common_transcript_scripts() == {}


# =============================================================================
# modify_env_vars: lifecycle-extension knobs
# =============================================================================


def test_modify_env_vars_sets_extension_knobs(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    env_vars: dict[str, str] = {}
    pi_agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    assert env_vars["MNGR_PI_AGENT_TYPE"] == "pi-coding"
    assert env_vars["MNGR_PI_EMIT_COMMON_TRANSCRIPT"] == "1"
    assert env_vars["MNGR_PI_EMIT_RAW_TRANSCRIPT"] == "1"


def test_modify_env_vars_reflects_disabled_transcripts(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    object.__setattr__(
        pi_agent,
        "agent_config",
        PiCodingAgentConfig(emit_common_transcript=False, emit_raw_transcript=False),
    )
    env_vars: dict[str, str] = {}
    pi_agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    assert env_vars["MNGR_PI_EMIT_COMMON_TRANSCRIPT"] == "0"
    assert env_vars["MNGR_PI_EMIT_RAW_TRANSCRIPT"] == "0"


# =============================================================================
# assemble_command
# =============================================================================


def test_assemble_command_loads_extension_and_resumes(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    host = _fake_host(tmp_path)
    command = str(pi_agent.assemble_command(host, (), None))
    ext_path = str(pi_agent._get_lifecycle_extension_path())

    assert ext_path.endswith("commands/mngr_pi_lifecycle.ts")
    assert "-e " in command
    assert ext_path in command
    # Resume is shell-evaluated from the recorded session file path.
    assert "pi_session_file" in command
    assert "--session" in command
    assert command.rstrip().endswith('"$@"')
    # No backgrounded helper: the lifecycle-detected process is plain pi.
    assert pi_agent.get_expected_process_name() == "pi"


def test_assemble_command_omits_resume_when_disabled(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    object.__setattr__(pi_agent, "agent_config", PiCodingAgentConfig(resume_session=False))
    host = _fake_host(tmp_path)
    command = str(pi_agent.assemble_command(host, (), None))

    assert "--session" not in command
    assert "pi_session_file" not in command
    assert "-e " in command
    assert str(pi_agent._get_lifecycle_extension_path()) in command


def test_assemble_command_preserves_cli_and_agent_args(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    object.__setattr__(pi_agent, "agent_config", PiCodingAgentConfig(cli_args=("--thinking", "high")))
    host = _fake_host(tmp_path)
    command = str(pi_agent.assemble_command(host, ("--model", "claude"), None))

    assert "--thinking high" in command
    assert "--model claude" in command


# =============================================================================
# Lifecycle extension provisioning + readiness
# =============================================================================


def test_provision_lifecycle_extension_writes_resource(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    host = _fake_host(tmp_path, is_local=True)
    pi_agent._provision_lifecycle_extension(host)
    extension_path = pi_agent._get_lifecycle_extension_path()
    assert extension_path.read_text() == _load_resource(_LIFECYCLE_EXTENSION_NAME)


def test_wait_for_ready_signal_non_creating_just_runs_start_action(pi_agent: PiCodingAgent) -> None:
    calls: list[int] = []
    pi_agent.wait_for_ready_signal(is_creating=False, start_action=lambda: calls.append(1))
    assert calls == [1]


def test_wait_for_ready_signal_returns_once_sentinel_present(pi_agent: PiCodingAgent) -> None:
    # With the readiness sentinel already written, the creation path returns
    # immediately (the sentinel short-circuits before any pane capture).
    sentinel = pi_agent._get_agent_dir() / "pi_session_started"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("1")
    calls: list[int] = []
    pi_agent.wait_for_ready_signal(is_creating=True, start_action=lambda: calls.append(1))
    assert calls == [1]


# =============================================================================
# Workspace trust (pi 0.79+ "Trust project folder?" dialog)
# =============================================================================


def test_read_pi_trust_parses_bools_and_drops_null(tmp_path: Path) -> None:
    parsed = _read_pi_trust('{"/a": true, "/b": false, "/c": null}', tmp_path / "trust.json")
    assert parsed == {"/a": True, "/b": False}


def test_read_pi_trust_empty_is_empty_map() -> None:
    assert _read_pi_trust(None, Path("/x")) == {}
    assert _read_pi_trust("   ", Path("/x")) == {}


def test_read_pi_trust_rejects_malformed() -> None:
    with pytest.raises(UserInputError):
        _read_pi_trust("{not json", Path("/x"))
    with pytest.raises(UserInputError):
        _read_pi_trust("[1, 2]", Path("/x"))
    with pytest.raises(UserInputError):
        _read_pi_trust('{"/a": "yes"}', Path("/x"))


def test_serialize_pi_trust_is_sorted_with_trailing_newline() -> None:
    assert _serialize_pi_trust({"/b": True, "/a": False}) == '{\n  "/a": false,\n  "/b": true\n}\n'


class _TrustConfirmAgent(PiCodingAgent):
    """Agent whose source-path lookup and trust prompt are stubbed for tests (confirms)."""

    def _find_git_source_path(self, mngr_ctx: MngrContext) -> Path | None:
        # Returning None makes the source fall back to work_dir (no git probe needed).
        return None

    def _prompt_user_to_trust_workspace(self, source_path: Path, trust_path: Path) -> bool:
        return True


class _TrustDeclineAgent(PiCodingAgent):
    """Like _TrustConfirmAgent but declines the trust prompt."""

    def _find_git_source_path(self, mngr_ctx: MngrContext) -> Path | None:
        return None

    def _prompt_user_to_trust_workspace(self, source_path: Path, trust_path: Path) -> bool:
        return False


def _make_trust_agent(cls: type[PiCodingAgent], tmp_path: Path, **config_kwargs: Any) -> PiCodingAgent:
    agent = cls.__new__(cls)
    object.__setattr__(agent, "agent_config", PiCodingAgentConfig(**config_kwargs))
    object.__setattr__(agent, "host", _fake_host(tmp_path))
    object.__setattr__(agent, "id", AgentId.generate())
    object.__setattr__(agent, "name", AgentName("test-pi"))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    object.__setattr__(agent, "work_dir", work_dir)
    return agent


def _make_interactive_mngr_ctx(tmp_path: Path) -> MngrContext:
    return make_mngr_ctx(
        config=MngrConfig(),
        pm=pluggy.PluginManager("mngr"),
        profile_dir=tmp_path / "profile",
        is_interactive=True,
        is_auto_approve=False,
        concurrency_group=ConcurrencyGroup(name="test"),
    )


def _read_global_trust(home: Path) -> dict[str, bool]:
    trust_path = home / ".pi" / "agent" / "trust.json"
    content = trust_path.read_text() if trust_path.exists() else None
    return _read_pi_trust(content, trust_path)


def test_ensure_source_trusted_auto_dismiss_writes_global(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = _make_trust_agent(_TrustDeclineAgent, tmp_path, auto_dismiss_dialogs=True)
    agent._ensure_source_repo_trusted(_make_test_mngr_ctx(tmp_path), home_dir=home)
    assert _read_global_trust(home)[str(agent.work_dir.resolve())] is True


def test_ensure_source_trusted_auto_approve_writes_global(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = _make_trust_agent(_TrustDeclineAgent, tmp_path)
    agent._ensure_source_repo_trusted(_make_test_mngr_ctx(tmp_path, is_auto_approve=True), home_dir=home)
    assert _read_global_trust(home)[str(agent.work_dir.resolve())] is True


def test_ensure_source_trusted_noninteractive_without_optin_exits(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = _make_trust_agent(_TrustDeclineAgent, tmp_path)
    with pytest.raises(SystemExit):
        agent._ensure_source_repo_trusted(_make_test_mngr_ctx(tmp_path), home_dir=home)
    assert not (home / ".pi" / "agent" / "trust.json").exists()


def test_ensure_source_trusted_interactive_confirm_writes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = _make_trust_agent(_TrustConfirmAgent, tmp_path)
    agent._ensure_source_repo_trusted(_make_interactive_mngr_ctx(tmp_path), home_dir=home)
    assert _read_global_trust(home)[str(agent.work_dir.resolve())] is True


def test_ensure_source_trusted_interactive_decline_exits(tmp_path: Path) -> None:
    home = tmp_path / "home"
    agent = _make_trust_agent(_TrustDeclineAgent, tmp_path)
    with pytest.raises(SystemExit):
        agent._ensure_source_repo_trusted(_make_interactive_mngr_ctx(tmp_path), home_dir=home)
    assert not (home / ".pi" / "agent" / "trust.json").exists()


def test_ensure_source_trusted_already_trusted_is_noop(tmp_path: Path) -> None:
    home = tmp_path / "home"
    # Uses the decline agent: if it did not short-circuit on already-trusted, the
    # non-interactive gate (or a decline) would SystemExit instead of passing.
    agent = _make_trust_agent(_TrustDeclineAgent, tmp_path)
    trust_dir = home / ".pi" / "agent"
    trust_dir.mkdir(parents=True)
    key = str(agent.work_dir.resolve())
    (trust_dir / "trust.json").write_text(_serialize_pi_trust({key: True}))
    # Non-interactive, no opt-in: would SystemExit if it did not short-circuit on already-trusted.
    agent._ensure_source_repo_trusted(_make_test_mngr_ctx(tmp_path), home_dir=home)
    assert _read_global_trust(home)[key] is True


def test_seed_per_agent_workspace_trust_writes_per_agent_file(pi_agent: PiCodingAgent) -> None:
    pi_agent._seed_per_agent_workspace_trust(pi_agent.host)
    trust_path = pi_agent.get_pi_config_dir() / "trust.json"
    data = _read_pi_trust(trust_path.read_text(), trust_path)
    assert len(data) == 1
    assert all(value is True for value in data.values())
