"""Unit tests for the pi-coding plugin."""

import inspect
import json
import os
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pluggy
import pytest
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.overlay_merge import merge_models_via_overlay
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig
from imbue.mngr_pi_coding.plugin import _INBOX_FILE_NAME
from imbue.mngr_pi_coding.plugin import _LIFECYCLE_EXTENSION_NAME
from imbue.mngr_pi_coding.plugin import _PI_NPM_PACKAGE
from imbue.mngr_pi_coding.plugin import _SESSION_FILE_NAME
from imbue.mngr_pi_coding.plugin import _SESSION_STARTED_SENTINEL_NAME
from imbue.mngr_pi_coding.plugin import _inbox_append_command
from imbue.mngr_pi_coding.plugin import _load_resource
from imbue.mngr_pi_coding.plugin import _read_pi_trust
from imbue.mngr_pi_coding.plugin import _serialize_pi_trust
from imbue.mngr_pi_coding.plugin import _waiting_reason
from imbue.mngr_pi_coding.plugin import agent_field_generators
from imbue.mngr_pi_coding.plugin import register_agent_aliases
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


def _make_options(
    *,
    adopt_session: tuple[str, ...] = (),
    source_agent_state_location: HostLocation | None = None,
) -> CreateAgentOptions:
    return CreateAgentOptions(
        name=AgentName("test"),
        agent_type=AgentTypeName("pi-coding"),
        environment=AgentEnvironmentOptions(),
        adopt_session=adopt_session,
        source_agent_state_location=source_agent_state_location,
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
    assert config.resume_session is True
    assert config.emit_common_transcript is True
    assert config.emit_raw_transcript is True


def test_pi_coding_agent_config_merge_with_override() -> None:
    base = PiCodingAgentConfig()
    override = PiCodingAgentConfig(cli_args=("--verbose",))

    merged, _ = merge_models_via_overlay(base, override)

    assert isinstance(merged, PiCodingAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "pi"


# =============================================================================
# PiCodingAgent method tests
# =============================================================================


def test_pi_coding_agent_is_concrete_and_instantiable() -> None:
    # PiCodingAgent subclasses BaseAgent directly (not InteractiveTuiAgent) and must
    # implement every abstract method it inherits (e.g. assemble_command,
    # get_expected_process_name, send_message) or it could not be instantiated to
    # create agents. inspect.isabstract is the observable property that guards this;
    # the actual behavior of those methods is exercised by the other unit tests here
    # and by the release e2e.
    assert inspect.isabstract(PiCodingAgent) is False


def test_get_expected_process_name_returns_pi(pi_agent: PiCodingAgent) -> None:
    assert pi_agent.get_expected_process_name() == "pi"


def test_register_agent_type_returns_correct_tuple() -> None:
    name, agent_class, config_class = register_agent_type()

    assert name == "pi-coding"
    assert agent_class is PiCodingAgent
    assert config_class is PiCodingAgentConfig


def test_register_agent_aliases_maps_pi_to_pi_coding() -> None:
    assert register_agent_aliases() == {"pi": "pi-coding"}


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
    log_warnings: list[str],
) -> None:
    """on_before_provisioning warns when no API credentials are found anywhere."""
    # _has_api_credentials_available reads Path.home()/.pi/agent/auth.json; the autouse
    # setup_test_mngr_env fixture points HOME at tmp_path, which has no auth.json.
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    mngr_ctx = _make_test_mngr_ctx(tmp_path)

    pi_agent.on_before_provisioning(host, options, mngr_ctx)

    assert any("No API credentials detected" in message for message in log_warnings)


def test_on_before_provisioning_does_not_warn_when_auth_file_present(
    pi_agent: PiCodingAgent,
    tmp_path: Path,
    log_warnings: list[str],
) -> None:
    """on_before_provisioning stays silent when ~/.pi/agent/auth.json holds credentials."""
    # HOME is redirected to tmp_path by the autouse setup_test_mngr_env fixture.
    pi_auth_dir = tmp_path / ".pi" / "agent"
    pi_auth_dir.mkdir(parents=True)
    (pi_auth_dir / "auth.json").write_text('{"anthropic": {"type": "api_key"}}')
    host = _stub_host(tmp_path, is_local=False)
    options = _make_options()
    mngr_ctx = _make_test_mngr_ctx(tmp_path)

    pi_agent.on_before_provisioning(host, options, mngr_ctx)

    assert not any("No API credentials detected" in message for message in log_warnings)


# =============================================================================
# Provisioning tests
# =============================================================================


def test_setup_local_config_dir_symlinks_auth(tmp_path: Path, pi_agent: PiCodingAgent, local_host: Host) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "auth.json").write_text('{"anthropic": {}}')

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = local_host
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


def test_setup_local_config_dir_symlinks_settings(tmp_path: Path, pi_agent: PiCodingAgent, local_host: Host) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "settings.json").write_text('{"defaultModel": "sonnet"}')

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = local_host
    config = PiCodingAgentConfig(sync_home_settings=True)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    settings_link = config_dir / "settings.json"
    assert settings_link.is_symlink()
    assert Path(os.readlink(settings_link)) == home / ".pi" / "agent" / "settings.json"


def test_setup_local_config_dir_skips_settings_when_disabled(
    tmp_path: Path, pi_agent: PiCodingAgent, local_host: Host
) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "settings.json").write_text('{"defaultModel": "sonnet"}')

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = local_host
    config = PiCodingAgentConfig(sync_home_settings=False)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    assert not (config_dir / "settings.json").exists()


def test_setup_local_config_dir_symlinks_resource_dirs(
    tmp_path: Path, pi_agent: PiCodingAgent, local_host: Host
) -> None:
    home = _setup_home_pi(tmp_path)
    (home / ".pi" / "agent" / "skills").mkdir()
    (home / ".pi" / "agent" / "prompts").mkdir()
    # `agents` holds subagent definitions read by subagent extensions.
    (home / ".pi" / "agent" / "agents").mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    host = local_host
    config = PiCodingAgentConfig(sync_home_settings=True)

    pi_agent._setup_local_config_dir(host, config, config_dir, home)

    skills_link = config_dir / "skills"
    prompts_link = config_dir / "prompts"
    agents_link = config_dir / "agents"
    assert skills_link.is_symlink()
    assert prompts_link.is_symlink()
    assert agents_link.is_symlink()
    assert Path(os.readlink(skills_link)) == home / ".pi" / "agent" / "skills"
    assert Path(os.readlink(prompts_link)) == home / ".pi" / "agent" / "prompts"
    assert Path(os.readlink(agents_link)) == home / ".pi" / "agent" / "agents"


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

    with pytest.raises(AgentInstallationError, match="pi is not installed"):
        pi_agent.provision(host, options, mngr_ctx)


def test_provision_auto_installs_on_remote(tmp_path: Path, make_pi_agent: Callable[..., PiCodingAgent]) -> None:
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
    agent = make_pi_agent(agent_config=config, host=host)
    options = _make_options()
    # is_auto_approve so the workspace-trust gate proceeds silently (the autouse
    # HOME redirect keeps the global trust write inside the test's temp home).
    mngr_ctx = _make_test_mngr_ctx(tmp_path, is_auto_approve=True)

    # The trust gate resolves the git source via the concurrency group, which
    # must be active (it is during real provisioning).
    with mngr_ctx.concurrency_group:
        agent.provision(host, options, mngr_ctx)

    # The install branch must actually run: provision would otherwise complete
    # silently (the only other command, mkdir -p, also succeeds) without installing pi.
    assert any(f"npm install -g {_PI_NPM_PACKAGE}" in command for command in host.executed_commands)


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

    with pytest.raises(AgentInstallationError, match="automatic remote installation is disabled"):
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


def test_get_install_command_installs_latest_by_default(pi_agent: PiCodingAgent) -> None:
    assert pi_agent.get_install_command() == "npm install -g @earendil-works/pi-coding-agent"


def test_get_install_command_pins_version(make_pi_agent: Callable[..., PiCodingAgent]) -> None:
    agent = make_pi_agent(agent_config=PiCodingAgentConfig(version="1.2.3"))
    assert agent.get_install_command() == "npm install -g @earendil-works/pi-coding-agent@1.2.3"


def test_modify_env_vars_skips_version_check_when_policy_never(
    make_pi_agent: Callable[..., PiCodingAgent], tmp_path: Path
) -> None:
    agent = make_pi_agent(agent_config=PiCodingAgentConfig(update_policy=AgentUpdatePolicy.NEVER))
    env_vars: dict[str, str] = {}
    agent.modify_env_vars(_fake_host(tmp_path), env_vars)
    assert env_vars["PI_SKIP_VERSION_CHECK"] == "1"


def test_modify_env_vars_skips_version_check_by_default_on_attended_local(
    pi_agent: PiCodingAgent, tmp_path: Path
) -> None:
    """The default policy disables pi's startup version check, even on an attended local host."""
    env_vars: dict[str, str] = {}
    pi_agent.modify_env_vars(_fake_host(tmp_path, is_local=True), env_vars)
    assert env_vars["PI_SKIP_VERSION_CHECK"] == "1"


def test_modify_env_vars_leaves_version_check_when_policy_auto(
    make_pi_agent: Callable[..., PiCodingAgent], tmp_path: Path
) -> None:
    """Explicit AUTO opts back into pi's startup version check (no PI_SKIP_VERSION_CHECK)."""
    agent = make_pi_agent(agent_config=PiCodingAgentConfig(update_policy=AgentUpdatePolicy.AUTO))
    env_vars: dict[str, str] = {}
    agent.modify_env_vars(_fake_host(tmp_path, is_local=True), env_vars)
    assert "PI_SKIP_VERSION_CHECK" not in env_vars


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


def test_assemble_command_adds_approve_when_auto_dismiss_dialogs(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    # auto_dismiss_dialogs launches pi with --approve so it auto-trusts the project
    # folder (pi's native unattended path), and the trust dialog never blocks.
    object.__setattr__(pi_agent, "agent_config", PiCodingAgentConfig(auto_dismiss_dialogs=True))
    command = str(pi_agent.assemble_command(_fake_host(tmp_path), (), None))
    assert "--approve" in command


def test_assemble_command_omits_approve_by_default(pi_agent: PiCodingAgent, tmp_path: Path) -> None:
    # Default config does not auto-dismiss, so --approve is not added: pi decides
    # trust itself (the dialog, or a previously seeded trust decision).
    command = str(pi_agent.assemble_command(_fake_host(tmp_path), (), None))
    assert "--approve" not in command


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


# =============================================================================
# Session adoption (--adopt and --from clone)
# =============================================================================


def _write_pi_session(sessions_dir: Path, subdir: str, session_id: str, cwd: str = "/old/cwd") -> Path:
    """Write a minimal pi session JSONL under ``sessions_dir/subdir`` and return its path.

    The first record is a ``session`` record carrying ``cwd`` (the field adoption
    rebinds); a second message record stands in for conversation content.
    """
    session_subdir = sessions_dir / subdir
    session_subdir.mkdir(parents=True, exist_ok=True)
    session_file = session_subdir / f"20240101_000000_{session_id}.jsonl"
    session_file.write_text(
        json.dumps({"type": "session", "cwd": cwd, "id": session_id}) + "\n" + json.dumps({"type": "message"}) + "\n"
    )
    return session_file


def _read_resume_pointer(agent: PiCodingAgent) -> str:
    return (agent._get_agent_dir() / _SESSION_FILE_NAME).read_text()


@pytest.mark.rsync
def test_adopt_session_multiple_resumes_last(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """``--adopt A B`` copies both sessions in and resumes the last (B)."""
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig())
    source_a = _write_pi_session(tmp_path / "src-a", "encoded-a", "sessA")
    source_b = _write_pi_session(tmp_path / "src-b", "encoded-b", "sessB")
    options = _make_options(adopt_session=(str(source_a), str(source_b)))

    agent.adopt_session(agent.host, options, agent.mngr_ctx)

    sessions_dir = agent.get_pi_config_dir() / "sessions"
    # Both sessions are available in the new agent's store (additive copy).
    assert (sessions_dir / "encoded-a" / source_a.name).exists()
    assert (sessions_dir / "encoded-b" / source_b.name).exists()
    # Only the last (B) is resumed.
    assert _read_resume_pointer(agent) == str(sessions_dir / "encoded-b" / source_b.name)


@pytest.mark.rsync
def test_adopt_session_rebinds_cwd(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """An adopted session's embedded cwd is rebound to the new agent's work_dir."""
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig())
    source = _write_pi_session(tmp_path / "src", "encoded", "sess1", cwd="/no/longer/exists")
    options = _make_options(adopt_session=(str(source),))

    agent.adopt_session(agent.host, options, agent.mngr_ctx)

    adopted = agent.get_pi_config_dir() / "sessions" / "encoded" / source.name
    first_record = json.loads(adopted.read_text().splitlines()[0])
    assert first_record["cwd"] == agent._get_host_canonical_work_dir(agent.host)


@pytest.mark.rsync
def test_adopt_from_clone_with_explicit_resumes_clone(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """``--adopt A --from X`` copies both, and the clone (X) is the one resumed."""
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig())
    source_a = _write_pi_session(tmp_path / "src-a", "encoded-a", "sessA")

    # The --from source's native pi session store, at the preserved relpath.
    source_state_dir = tmp_path / "source-agent-state"
    source_sessions = source_state_dir / "plugin" / "pi_coding" / "sessions"
    clone_session = _write_pi_session(source_sessions, "encoded-clone", "sessClone")
    source_location = HostLocation(host=agent.host, path=source_state_dir)
    options = _make_options(adopt_session=(str(source_a),), source_agent_state_location=source_location)

    agent.adopt_session(agent.host, options, agent.mngr_ctx)

    # The explicit session is still copied in (available, not resumed).
    assert (agent.get_pi_config_dir() / "sessions" / "encoded-a" / source_a.name).exists()
    # The clone's session is the one resumed.
    resumed = agent._get_agent_dir() / "plugin" / "pi_coding" / "sessions" / "encoded-clone" / clone_session.name
    assert _read_resume_pointer(agent) == str(resumed)


def test_adopt_from_clone_no_store_warns(
    local_provider: LocalProviderInstance, tmp_path: Path, log_warnings: list[str]
) -> None:
    """A ``--from`` clone whose source has no pi session store warns and resumes nothing."""
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig())
    # Source state dir exists but has no plugin/pi_coding/sessions store.
    source_state_dir = tmp_path / "source-agent-state"
    source_state_dir.mkdir()
    source_location = HostLocation(host=agent.host, path=source_state_dir)
    options = _make_options(source_agent_state_location=source_location)

    agent.adopt_session(agent.host, options, agent.mngr_ctx)

    assert any("no pi session store" in message for message in log_warnings)
    assert not (agent._get_agent_dir() / _SESSION_FILE_NAME).exists()


def test_adopt_from_clone_empty_store_warns(
    local_provider: LocalProviderInstance, tmp_path: Path, log_warnings: list[str]
) -> None:
    """A ``--from`` clone whose source store has no session JSONL warns and resumes nothing."""
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig())
    source_state_dir = tmp_path / "source-agent-state"
    # The store dir exists but holds no .jsonl session.
    (source_state_dir / "plugin" / "pi_coding" / "sessions").mkdir(parents=True)
    source_location = HostLocation(host=agent.host, path=source_state_dir)
    options = _make_options(source_agent_state_location=source_location)

    agent.adopt_session(agent.host, options, agent.mngr_ctx)

    assert any("no session JSONL" in message for message in log_warnings)
    assert not (agent._get_agent_dir() / _SESSION_FILE_NAME).exists()


# =============================================================================
# Message delivery via the inbox (pi.sendUserMessage injection)
# =============================================================================


def test_inbox_append_command_json_encodes_and_appends() -> None:
    cmd = _inbox_append_command(Path("/state/pi_inbox"), "hi\nthere")
    assert cmd.startswith("printf '%s\\n' ")
    assert ">> " in cmd
    assert "/state/pi_inbox" in cmd
    # The message is JSON-encoded so embedded newlines stay on one inbox line.
    assert json.dumps("hi\nthere") in cmd


def test_confirm_turn_started_returns_when_marker_present(pi_agent: PiCodingAgent) -> None:
    marker = pi_agent._get_agent_dir() / "active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1")
    # Already running -> returns immediately without waiting for a new turn.
    pi_agent._confirm_turn_started(timeout=0.5)


def test_confirm_turn_started_raises_when_no_turn(pi_agent: PiCodingAgent) -> None:
    with pytest.raises(SendMessageError, match="did not start a turn"):
        pi_agent._confirm_turn_started(timeout=0.5)


def test_send_message_appends_inbox_then_confirms(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    # Stub the inbox append so it succeeds without a shell; pre-create the marker
    # so confirmation returns immediately (no real pi turn).
    host = _stub_host(
        tmp_path,
        is_local=True,
        command_results={"printf": CommandResult(stdout="", stderr="", success=True)},
    )
    object.__setattr__(pi_agent, "host", host)
    marker = pi_agent._get_agent_dir() / "active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1")
    # Should append to the inbox and return (no exception) once the marker confirms.
    pi_agent.send_message("hello from mngr")


def test_send_message_raises_when_inbox_write_fails(tmp_path: Path, pi_agent: PiCodingAgent) -> None:
    host = _stub_host(
        tmp_path,
        is_local=True,
        command_results={"printf": CommandResult(stdout="", stderr="disk full", success=False)},
    )
    object.__setattr__(pi_agent, "host", host)
    with pytest.raises(SendMessageError, match="failed to write to pi inbox"):
        pi_agent.send_message("hello")


def test_lifecycle_extension_contract_matches_python_constants() -> None:
    """Guard against Python<->TypeScript drift in the shared file/env-var contract.

    plugin.py and mngr_pi_lifecycle.ts hardcode the same filenames and env-var names
    independently: the Python side writes/reads them under the agent state dir and sets the
    env vars; the extension reads/writes the same names. The release e2e covers the full
    round-trip but does not run in CI, so this asserts every shared name plugin.py relies on
    appears verbatim in the extension source. A rename on either side fails here instead of
    silently breaking message delivery, resume, readiness, or transcripts.
    """
    extension_source = _load_resource(_LIFECYCLE_EXTENSION_NAME)
    shared_names = (
        # filenames written by one side and read by the other, under $MNGR_AGENT_STATE_DIR
        "active",
        _SESSION_STARTED_SENTINEL_NAME,
        _SESSION_FILE_NAME,
        _INBOX_FILE_NAME,
        # env vars the Python side sets and the extension reads
        "MNGR_AGENT_STATE_DIR",
        "MNGR_PI_AGENT_TYPE",
        "MNGR_PI_EMIT_COMMON_TRANSCRIPT",
        "MNGR_PI_EMIT_RAW_TRANSCRIPT",
    )
    for name in shared_names:
        assert name in extension_source, (
            f"{name!r} is used in plugin.py but absent from {_LIFECYCLE_EXTENSION_NAME} -- "
            "the Python<->TypeScript contract has drifted"
        )


# =============================================================================
# Preservation on destroy
# =============================================================================


def test_pi_coding_config_preserves_on_destroy_by_default() -> None:
    assert PiCodingAgentConfig().preserve_on_destroy is True


def _make_local_pi_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, agent_config: PiCodingAgentConfig
) -> PiCodingAgent:
    """Build a PiCodingAgent on a real local host/ctx so the preservation rsync path works.

    The shared ``make_pi_agent`` fixture constructs against a FakeHost with no
    mngr_ctx, which is insufficient for the on-destroy preservation path.
    """
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    return PiCodingAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-pi"),
        agent_type=AgentTypeName("pi-coding"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


def _populate_pi_transcripts(agent: PiCodingAgent) -> None:
    """Write the raw/common transcripts and the session-file pointer into the state dir."""
    agent_dir = agent._get_agent_dir()
    (agent_dir / "logs" / "pi-coding_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "logs" / "pi-coding_transcript" / "events.jsonl").write_text('{"type":"raw"}\n')
    (agent_dir / "events" / "pi-coding" / "common_transcript").mkdir(parents=True, exist_ok=True)
    (agent_dir / "events" / "pi-coding" / "common_transcript" / "events.jsonl").write_text('{"type":"common"}\n')
    (agent_dir / _SESSION_FILE_NAME).write_text("/path/to/session.json\n")


@pytest.mark.rsync
def test_on_destroy_preserves_transcripts(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy copies transcripts and the session-file pointer to the mirrored preserved layout."""
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig(preserve_on_destroy=True))
    _populate_pi_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert (dest_dir / "logs" / "pi-coding_transcript" / "events.jsonl").read_text() == '{"type":"raw"}\n'
    assert (
        dest_dir / "events" / "pi-coding" / "common_transcript" / "events.jsonl"
    ).read_text() == '{"type":"common"}\n'
    assert (dest_dir / _SESSION_FILE_NAME).read_text() == "/path/to/session.json\n"


def test_on_destroy_skips_preservation_when_disabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    """on_destroy preserves nothing when preserve_on_destroy is False."""
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig(preserve_on_destroy=False))
    _populate_pi_transcripts(agent)

    agent.on_destroy(agent.host)

    dest_dir = get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id)
    assert not dest_dir.exists()


def test_pi_config_defaults_to_unattended() -> None:
    # pi has no tool-approval gate, so auto_allow_permissions is pinned on.
    assert PiCodingAgentConfig().auto_allow_permissions is True


def test_pi_config_rejects_disabling_auto_allow() -> None:
    # pi cannot enforce a deny, so explicitly disabling auto-allow is an error
    # (pydantic wraps the PiAutoAllowRequiredError raised by the field validator).
    with pytest.raises(ValidationError, match="cannot honor"):
        PiCodingAgentConfig(auto_allow_permissions=False)


def test_agent_field_generators_exposes_pi_waiting_reason() -> None:
    result = agent_field_generators()
    assert result is not None
    plugin_name, generators = result
    assert plugin_name == "pi-coding"
    assert "waiting_reason" in generators
    assert callable(generators["waiting_reason"])


def test_pi_waiting_reason_is_end_of_turn_when_idle(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    # No active marker -> the agent is idle, so the (single-value) reason is END_OF_TURN.
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig())
    agent._get_agent_dir().mkdir(parents=True, exist_ok=True)
    assert _waiting_reason(agent, agent.host) == WaitingReason.END_OF_TURN


def test_pi_waiting_reason_is_none_when_active(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    # Active marker present -> the agent is running, so there is no waiting reason.
    agent = _make_local_pi_agent(local_provider, tmp_path, PiCodingAgentConfig())
    marker = agent._get_agent_dir() / "active"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1")
    assert _waiting_reason(agent, agent.host) is None
