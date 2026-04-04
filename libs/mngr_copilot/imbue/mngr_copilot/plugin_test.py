"""Unit tests for the copilot plugin."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_copilot.plugin import CopilotAgent
from imbue.mngr_copilot.plugin import CopilotAgentConfig
from imbue.mngr_copilot.plugin import _has_token_available
from imbue.mngr_copilot.plugin import register_agent_type

# =============================================================================
# Test helpers
# =============================================================================


class _StubHost(FakeHost):
    """FakeHost that stubs specific commands and exposes env_vars."""

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
    return FakeHost(host_dir=host_dir, is_local=is_local)


def _stub_host(
    host_dir: Path,
    *,
    is_local: bool = True,
    command_results: dict[str, CommandResult] | None = None,
    env_vars: dict[str, str] | None = None,
) -> Any:
    return _StubHost(
        host_dir=host_dir,
        is_local=is_local,
        command_results=command_results or {},
        env_vars=env_vars or {},
    )


def _make_options(
    env_vars: dict[str, str] | None = None,
) -> CreateAgentOptions:
    evars = tuple(EnvVar(key=k, value=v) for k, v in (env_vars or {}).items())
    return CreateAgentOptions(
        name=AgentName("test"),
        agent_type=AgentTypeName("copilot"),
        environment=AgentEnvironmentOptions(env_vars=evars),
    )


def _make_mngr_ctx(
    tmp_path: Path,
    *,
    is_auto_approve: bool = False,
    is_remote_agent_installation_allowed: bool = True,
) -> MngrContext:
    return make_mngr_ctx(
        config=MngrConfig(
            is_remote_agent_installation_allowed=is_remote_agent_installation_allowed
        ),
        pm=pluggy.PluginManager("mngr"),
        profile_dir=tmp_path / "profile",
        is_interactive=False,
        is_auto_approve=is_auto_approve,
        concurrency_group=ConcurrencyGroup(name="test"),
    )


def _make_copilot_agent(
    tmp_path: Path,
    *,
    config: CopilotAgentConfig | None = None,
    host: Any | None = None,
    is_local: bool = True,
) -> CopilotAgent:
    """Create a CopilotAgent via __new__ to bypass pydantic host-type validation."""
    resolved_host = host if host is not None else _fake_host(tmp_path, is_local=is_local)
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    mngr_ctx = _make_mngr_ctx(tmp_path)

    agent = CopilotAgent.__new__(CopilotAgent)
    object.__setattr__(agent, "agent_config", config or CopilotAgentConfig())
    object.__setattr__(agent, "host", resolved_host)
    object.__setattr__(agent, "id", AgentId.generate())
    object.__setattr__(agent, "name", AgentName("test-copilot"))
    object.__setattr__(agent, "agent_type", AgentTypeName("copilot"))
    object.__setattr__(agent, "work_dir", work_dir)
    object.__setattr__(agent, "mngr_ctx", mngr_ctx)
    return agent


@pytest.fixture()
def copilot_agent(tmp_path: Path) -> CopilotAgent:
    """Create a minimally-configured CopilotAgent for testing."""
    return _make_copilot_agent(tmp_path)


# =============================================================================
# CopilotAgentConfig tests
# =============================================================================


def test_copilot_agent_config_defaults() -> None:
    config = CopilotAgentConfig()

    assert str(config.command) == "copilot"
    assert config.sync_copilot_credentials is True
    assert config.check_installation is True
    assert config.allow_all_tools is True
    assert config.cli_args == ()
    assert config.permissions == []
    assert config.parent_type is None


def test_copilot_agent_config_merge_with_override() -> None:
    base = CopilotAgentConfig()
    override = CopilotAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, CopilotAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "copilot"


# =============================================================================
# CopilotAgent basic method tests
# =============================================================================


def test_get_expected_process_name(copilot_agent: CopilotAgent) -> None:
    assert copilot_agent.get_expected_process_name() == "copilot"


def test_get_tui_ready_indicator(copilot_agent: CopilotAgent) -> None:
    assert copilot_agent.get_tui_ready_indicator() == "\u276f"


def test_register_agent_type_returns_correct_tuple() -> None:
    name, agent_class, config_class = register_agent_type()

    assert name == "copilot"
    assert agent_class is CopilotAgent
    assert config_class is CopilotAgentConfig


def test_get_provision_file_transfers_returns_empty(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    host = _fake_host(tmp_path)
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path)
    assert copilot_agent.get_provision_file_transfers(host, options, mngr_ctx) == []


# =============================================================================
# assemble_command tests
# =============================================================================


def test_assemble_command_includes_allow_all_tools_by_default(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    cmd = str(copilot_agent.assemble_command(_fake_host(tmp_path), (), None))
    assert "--allow-all-tools" in cmd
    assert cmd.startswith("copilot")


def test_assemble_command_omits_allow_all_tools_when_disabled(tmp_path: Path) -> None:
    agent = _make_copilot_agent(tmp_path, config=CopilotAgentConfig(allow_all_tools=False))
    cmd = str(agent.assemble_command(_fake_host(tmp_path), (), None))
    assert "--allow-all-tools" not in cmd


def test_assemble_command_includes_cli_args(tmp_path: Path) -> None:
    agent = _make_copilot_agent(tmp_path, config=CopilotAgentConfig(cli_args=("--verbose",)))
    cmd = str(agent.assemble_command(_fake_host(tmp_path), (), None))
    assert "--verbose" in cmd


def test_assemble_command_includes_agent_args(copilot_agent: CopilotAgent, tmp_path: Path) -> None:
    cmd = str(copilot_agent.assemble_command(_fake_host(tmp_path), ("--model", "sonnet"), None))
    assert "--model" in cmd
    assert "sonnet" in cmd


def test_assemble_command_respects_command_override(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    from imbue.mngr.primitives import CommandString

    cmd = str(copilot_agent.assemble_command(_fake_host(tmp_path), (), CommandString("my-copilot")))
    assert cmd.startswith("my-copilot")


# =============================================================================
# modify_env_vars tests
# =============================================================================


def test_modify_env_vars_sets_copilot_home(copilot_agent: CopilotAgent, tmp_path: Path) -> None:
    env_vars: dict[str, str] = {}
    copilot_agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    assert "COPILOT_HOME" in env_vars
    assert ".copilot" in env_vars["COPILOT_HOME"]


def test_modify_env_vars_injects_token_from_keychain(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    env_vars: dict[str, str] = {}
    with (
        patch("imbue.mngr_copilot.plugin.is_macos", return_value=True),
        patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain", return_value="ghu_test_token"),
    ):
        copilot_agent.modify_env_vars(_fake_host(tmp_path), env_vars)

    assert env_vars.get("COPILOT_GITHUB_TOKEN") == "ghu_test_token"


def test_modify_env_vars_skips_keychain_when_token_already_set(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    env_vars: dict[str, str] = {"GITHUB_TOKEN": "existing_token"}
    with patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain") as mock_keychain:
        copilot_agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    mock_keychain.assert_not_called()
    assert "COPILOT_GITHUB_TOKEN" not in env_vars


def test_modify_env_vars_skips_keychain_on_non_macos(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    env_vars: dict[str, str] = {}
    with (
        patch("imbue.mngr_copilot.plugin.is_macos", return_value=False),
        patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain") as mock_keychain,
    ):
        copilot_agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    mock_keychain.assert_not_called()
    assert "COPILOT_GITHUB_TOKEN" not in env_vars


def test_modify_env_vars_skips_when_sync_disabled(
    tmp_path: Path,
) -> None:
    agent = _make_copilot_agent(tmp_path, config=CopilotAgentConfig(sync_copilot_credentials=False))
    env_vars: dict[str, str] = {}
    with patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain") as mock_keychain:
        agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    mock_keychain.assert_not_called()


# =============================================================================
# _has_token_available tests
# =============================================================================


def test_has_token_available_via_host_env(tmp_path: Path) -> None:
    host = _stub_host(tmp_path, is_local=False, env_vars={"GH_TOKEN": "ghs_test"})
    options = _make_options()
    assert _has_token_available(host, options) is True


def test_has_token_available_returns_false_when_no_token(tmp_path: Path) -> None:
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    with patch("imbue.mngr_copilot.plugin.is_macos", return_value=False):
        assert _has_token_available(host, options) is False


def test_has_token_available_via_keychain_on_macos(tmp_path: Path) -> None:
    host = _stub_host(tmp_path, is_local=True)
    options = _make_options()
    with (
        patch("imbue.mngr_copilot.plugin.is_macos", return_value=True),
        patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain", return_value="ghu_token"),
    ):
        assert _has_token_available(host, options, sync_copilot_credentials=True) is True


def test_has_token_available_via_keychain_for_remote_host(tmp_path: Path) -> None:
    """Keychain check runs even for remote hosts (credentials injected from local machine)."""
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    with (
        patch("imbue.mngr_copilot.plugin.is_macos", return_value=True),
        patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain", return_value="ghu_token"),
    ):
        assert _has_token_available(host, options, sync_copilot_credentials=True) is True


def test_has_token_available_skips_keychain_when_sync_disabled(tmp_path: Path) -> None:
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    with (
        patch("imbue.mngr_copilot.plugin.is_macos", return_value=True),
        patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain", return_value="ghu_token"),
    ):
        assert _has_token_available(host, options, sync_copilot_credentials=False) is False


# =============================================================================
# on_before_provisioning tests
# =============================================================================


def test_on_before_provisioning_completes_without_credentials(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    """Verify on_before_provisioning completes (with warning) when no credentials found."""
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path)

    with patch("imbue.mngr_copilot.plugin.is_macos", return_value=False):
        copilot_agent.on_before_provisioning(host, options, mngr_ctx)


# =============================================================================
# provision tests
# =============================================================================


def test_provision_writes_trust_config(copilot_agent: CopilotAgent, tmp_path: Path) -> None:
    host = _stub_host(
        tmp_path,
        command_results={"command -v copilot": CommandResult(stdout="/usr/bin/copilot", stderr="", success=True)},
    )
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path)

    copilot_agent.provision(host, options, mngr_ctx)

    config_path = copilot_agent._get_copilot_home_dir() / "config.json"
    import json

    data = json.loads(config_path.read_text())
    assert "trusted_folders" in data
    assert str(copilot_agent.work_dir) in data["trusted_folders"]


def test_provision_raises_when_not_installed_locally(
    copilot_agent: CopilotAgent, tmp_path: Path
) -> None:
    host = _stub_host(
        tmp_path,
        is_local=True,
        command_results={"command -v copilot": CommandResult(stdout="", stderr="", success=False)},
    )
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path, is_auto_approve=False)

    with pytest.raises(PluginMngrError, match="Copilot CLI is not installed"):
        copilot_agent.provision(host, options, mngr_ctx)


def test_provision_auto_installs_on_remote(tmp_path: Path) -> None:
    host = _stub_host(
        tmp_path,
        is_local=False,
        command_results={
            "command -v copilot": CommandResult(stdout="", stderr="", success=False),
            "command -v curl": CommandResult(stdout="/usr/bin/curl", stderr="", success=True),
            "curl -fsSL https://gh.io/copilot-install": CommandResult(stdout="installed", stderr="", success=True),
        },
    )
    agent = _make_copilot_agent(
        tmp_path,
        config=CopilotAgentConfig(check_installation=True),
        host=host,
        is_local=False,
    )
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path)

    agent.provision(host, options, mngr_ctx)


def test_provision_raises_when_remote_install_disabled(tmp_path: Path) -> None:
    host = _stub_host(
        tmp_path,
        is_local=False,
        command_results={"command -v copilot": CommandResult(stdout="", stderr="", success=False)},
    )
    agent = _make_copilot_agent(
        tmp_path,
        config=CopilotAgentConfig(check_installation=True),
        host=host,
        is_local=False,
    )
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path, is_remote_agent_installation_allowed=False)

    with pytest.raises(PluginMngrError, match="automatic remote installation is disabled"):
        agent.provision(host, options, mngr_ctx)


def test_provision_skips_install_check_when_disabled(tmp_path: Path) -> None:
    """When check_installation=False, provision should not run 'command -v copilot'."""
    host = _stub_host(
        tmp_path,
        command_results={
            # Return failure for any command-v check -- should never be called
            "command -v copilot": CommandResult(stdout="", stderr="", success=False),
        },
    )
    agent = _make_copilot_agent(tmp_path, config=CopilotAgentConfig(check_installation=False), host=host)
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path)

    # Should not raise even though copilot is "not installed"
    agent.provision(host, options, mngr_ctx)


# =============================================================================
# send_message tests
# =============================================================================


def test_send_message_raises_on_enter_failure(tmp_path: Path) -> None:
    host = _stub_host(
        tmp_path,
        command_results={
            "tmux send-keys": CommandResult(stdout="", stderr="session not found", success=False)
        },
    )
    agent = _make_copilot_agent(tmp_path, host=host)

    with pytest.raises(SendMessageError):
        agent.send_message("hello")


def test_read_token_from_macos_keychain_returns_none_when_binary_missing() -> None:
    from imbue.mngr_copilot.plugin import _read_token_from_macos_keychain

    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = _read_token_from_macos_keychain()
    assert result is None


def test_read_token_from_macos_keychain_returns_none_when_not_found() -> None:
    from imbue.mngr_copilot.plugin import _read_token_from_macos_keychain
    import subprocess

    mock_result = subprocess.CompletedProcess(args=[], returncode=44, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        result = _read_token_from_macos_keychain()
    assert result is None


def test_has_token_available_via_local_env(tmp_path: Path) -> None:
    host = _stub_host(tmp_path, is_local=True)
    options = _make_options()
    with (
        patch("imbue.mngr_copilot.plugin.is_macos", return_value=False),
        patch.dict("os.environ", {"GH_TOKEN": "ghs_local"}),
    ):
        assert _has_token_available(host, options) is True


def test_has_token_available_via_options_env_vars(tmp_path: Path) -> None:
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options(env_vars={"COPILOT_GITHUB_TOKEN": "ghu_passed"})
    assert _has_token_available(host, options) is True


def test_install_copilot_raises_on_failure(tmp_path: Path) -> None:
    from imbue.mngr_copilot.plugin import _install_copilot

    host = _stub_host(
        tmp_path,
        command_results={
            "command -v curl": CommandResult(stdout="", stderr="", success=False),
            "command -v brew": CommandResult(stdout="", stderr="", success=False),
            "command -v npm": CommandResult(stdout="", stderr="", success=False),
        },
    )
    with pytest.raises(PluginMngrError, match="none of curl, brew, or npm"):
        _install_copilot(host)


def test_install_copilot_uses_curl_script_first(tmp_path: Path) -> None:
    from imbue.mngr_copilot.plugin import _install_copilot

    host = _stub_host(
        tmp_path,
        command_results={
            "command -v curl": CommandResult(stdout="/usr/bin/curl", stderr="", success=True),
            "curl -fsSL https://gh.io/copilot-install": CommandResult(stdout="", stderr="", success=True),
        },
    )
    _install_copilot(host)  # Should not raise


def test_install_copilot_falls_back_to_brew(tmp_path: Path) -> None:
    from imbue.mngr_copilot.plugin import _install_copilot

    host = _stub_host(
        tmp_path,
        command_results={
            "command -v curl": CommandResult(stdout="/usr/bin/curl", stderr="", success=True),
            "curl -fsSL https://gh.io/copilot-install": CommandResult(stdout="", stderr="network error", success=False),
            "command -v brew": CommandResult(stdout="/usr/local/bin/brew", stderr="", success=True),
            "brew install": CommandResult(stdout="", stderr="", success=True),
        },
    )
    _install_copilot(host)  # Should not raise


def test_install_copilot_falls_back_to_npm(tmp_path: Path) -> None:
    from imbue.mngr_copilot.plugin import _install_copilot

    host = _stub_host(
        tmp_path,
        command_results={
            "command -v curl": CommandResult(stdout="", stderr="", success=False),
            "command -v brew": CommandResult(stdout="", stderr="", success=False),
            "command -v npm": CommandResult(stdout="/usr/bin/npm", stderr="", success=True),
            "npm install -g": CommandResult(stdout="installed", stderr="", success=True),
        },
    )
    _install_copilot(host)  # Should not raise


def test_install_copilot_raises_when_npm_fails(tmp_path: Path) -> None:
    from imbue.mngr_copilot.plugin import _install_copilot

    host = _stub_host(
        tmp_path,
        command_results={
            "command -v curl": CommandResult(stdout="", stderr="", success=False),
            "command -v brew": CommandResult(stdout="", stderr="", success=False),
            "command -v npm": CommandResult(stdout="/usr/bin/npm", stderr="", success=True),
            "npm install -g": CommandResult(stdout="", stderr="permission denied", success=False),
        },
    )
    with pytest.raises(PluginMngrError, match="Failed to install Copilot CLI via npm"):
        _install_copilot(host)


def test_modify_env_vars_warns_when_no_keychain_token(copilot_agent: CopilotAgent, tmp_path: Path) -> None:
    env_vars: dict[str, str] = {}
    with (
        patch("imbue.mngr_copilot.plugin.is_macos", return_value=True),
        patch("imbue.mngr_copilot.plugin._read_token_from_macos_keychain", return_value=None),
    ):
        copilot_agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    assert "COPILOT_GITHUB_TOKEN" not in env_vars


def test_on_after_provisioning_is_noop(copilot_agent: CopilotAgent, tmp_path: Path) -> None:
    host = _fake_host(tmp_path)
    options = _make_options()
    mngr_ctx = _make_mngr_ctx(tmp_path)
    copilot_agent.on_after_provisioning(host, options, mngr_ctx)


def test_on_destroy_is_noop(copilot_agent: CopilotAgent, tmp_path: Path) -> None:
    host = _fake_host(tmp_path)
    copilot_agent.on_destroy(host)
