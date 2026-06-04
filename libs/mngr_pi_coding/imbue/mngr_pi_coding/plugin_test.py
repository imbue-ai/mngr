"""Unit tests for the pi-coding plugin."""

import inspect
import os
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
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig
from imbue.mngr_pi_coding.plugin import register_agent_type

# =============================================================================
# Test helpers
# =============================================================================


class _StubHost(FakeHost):
    """FakeHost that stubs specific commands, records them, and provides get_env_var.

    Kept local to this test module for now; if another plugin needs the same
    substring-command-stub + env-var behavior it should be promoted onto the
    shared FakeHost in imbue.mngr.api.testing.
    """

    command_results: dict[str, CommandResult] = {}
    env_vars: dict[str, str] = {}
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
    # This only guards against accidental edits to the documented banner substring.
    # That "pi v" is genuinely what pi prints at startup can only be confirmed by
    # the TUI acceptance/manual-verification path, not by a unit test.
    assert PiCodingAgent.TUI_READY_INDICATOR == "pi v"


def test_pi_coding_agent_is_concrete_and_instantiable() -> None:
    # PiCodingAgent must implement every abstract method of InteractiveTuiAgent
    # (notably _send_enter_and_validate) or it could not be instantiated to create
    # agents. inspect.isabstract is the observable property that guards this; the
    # actual behavior of _send_enter_and_validate (delegating to
    # send_enter_best_effort over tmux) is exercised only via manual verification.
    assert inspect.isabstract(PiCodingAgent) is False


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


def test_on_before_provisioning_warns_when_no_credentials(
    pi_agent: PiCodingAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_warnings: list[str],
) -> None:
    """on_before_provisioning warns when no API credentials are found anywhere."""
    # _has_api_credentials_available reads Path.home()/.pi/agent/auth.json, so redirect
    # HOME to the temp dir to keep the check off the real machine's credentials.
    home = _setup_home_pi(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    mngr_ctx = _make_test_mngr_ctx(tmp_path)

    pi_agent.on_before_provisioning(host, options, mngr_ctx)

    assert any("No API credentials detected" in message for message in log_warnings)


def test_on_before_provisioning_does_not_warn_when_auth_file_present(
    pi_agent: PiCodingAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_warnings: list[str],
) -> None:
    """on_before_provisioning stays silent when ~/.pi/agent/auth.json holds credentials."""
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "auth.json").write_text('{"anthropic": {"type": "api_key"}}')
    monkeypatch.setenv("HOME", str(home))
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    mngr_ctx = _make_test_mngr_ctx(tmp_path)

    pi_agent.on_before_provisioning(host, options, mngr_ctx)

    assert not any("No API credentials detected" in message for message in log_warnings)


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

    auth_link = config_dir / "auth.json"
    assert auth_link.is_symlink()
    assert Path(os.readlink(auth_link)) == home / ".pi" / "agent" / "auth.json"


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

    settings_link = config_dir / "settings.json"
    assert settings_link.is_symlink()
    assert Path(os.readlink(settings_link)) == home / ".pi" / "agent" / "settings.json"


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

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = _fake_host(tmp_path, is_local=True)
    config = PiCodingAgentConfig(sync_home_settings=True)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    skills_link = config_dir / "skills"
    prompts_link = config_dir / "prompts"
    assert skills_link.is_symlink()
    assert prompts_link.is_symlink()
    assert Path(os.readlink(skills_link)) == home / ".pi" / "agent" / "skills"
    assert Path(os.readlink(prompts_link)) == home / ".pi" / "agent" / "prompts"


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
    mngr_ctx = _make_test_mngr_ctx(tmp_path)

    pi_agent.provision(host, options, mngr_ctx)

    # The install branch must actually run: provision would otherwise complete
    # silently (the only other command, mkdir -p, also succeeds) without installing pi.
    assert any("npm install -g @mariozechner/pi-coding-agent" in command for command in host.executed_commands)


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
