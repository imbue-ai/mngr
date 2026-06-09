"""Unit tests for OpenCodeAgentConfig and OpenCodeAgent."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.opencode_config import get_shared_opencode_auth_path
from imbue.mngr_opencode.plugin import OpenCodeAgent
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig
from imbue.mngr_opencode.plugin import register_agent_type


def test_opencode_agent_config_has_correct_defaults() -> None:
    config = OpenCodeAgentConfig()

    assert str(config.command) == "opencode"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.config_overrides == {}
    assert config.sync_global_config is True
    assert config.symlink_auth is True
    assert config.auto_allow_permissions is False
    assert config.emit_common_transcript is True


def test_opencode_agent_config_merge_with_replaces_cli_args_and_overrides() -> None:
    """Override fields win under the base assign-by-default merge semantics."""
    base = OpenCodeAgentConfig()
    override = OpenCodeAgentConfig(cli_args=("--verbose",), config_overrides={"model": "anthropic/claude-sonnet-4-5"})

    merged = base.merge_with(override)

    assert isinstance(merged, OpenCodeAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert merged.config_overrides == {"model": "anthropic/claude-sonnet-4-5"}
    assert str(merged.command) == "opencode"


def test_opencode_agent_config_merge_with_rejects_other_type() -> None:
    class _OtherConfig(OpenCodeAgentConfig):
        pass

    with pytest.raises(ConfigParseError):
        OpenCodeAgentConfig().merge_with(_OtherConfig())


def test_opencode_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(OpenCodeAgent, InteractiveTuiAgent)


def test_opencode_agent_advertises_tui_ready_indicator() -> None:
    """Ready indicator is a footer-hint substring shown only once the input row is drawn.

    Deliberately not the ASCII-art splash banner, which renders before the
    input prompt exists. Verified against the live opencode 1.16.2 TUI.
    """
    assert OpenCodeAgent.TUI_READY_INDICATOR == "ctrl+p commands"


def test_opencode_agent_reports_opencode_process_name() -> None:
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_expected_process_name() == "opencode"


def test_opencode_agent_implements_send_enter_and_validate() -> None:
    assert "_send_enter_and_validate" not in OpenCodeAgent.__abstractmethods__


def test_register_agent_type_returns_opencode_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "opencode"
    assert agent_class is OpenCodeAgent
    assert config_class is OpenCodeAgentConfig


def test_is_common_transcript_enabled_reflects_config() -> None:
    enabled = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    disabled = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig(emit_common_transcript=False))
    assert enabled.is_common_transcript_enabled is True
    assert disabled.is_common_transcript_enabled is False


def test_raw_transcript_has_no_commands_scripts_but_common_does() -> None:
    """Raw capture is in-process (the .ts plugin), so no commands/ raw script; common is a converter."""
    agent = OpenCodeAgent.model_construct(agent_config=OpenCodeAgentConfig())
    assert agent.get_raw_transcript_scripts() == {}
    common = agent.get_common_transcript_scripts()
    assert "opencode_common_transcript.sh" in common
    assert common["opencode_common_transcript.sh"].strip() != ""


def _make_opencode_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    agent_config: OpenCodeAgentConfig,
) -> OpenCodeAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return OpenCodeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-opencode"),
        agent_type=AgentTypeName("opencode"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=agent_config,
        host=host,
    )


@pytest.fixture
def opencode_agent(local_provider: LocalProviderInstance, tmp_path: Path) -> OpenCodeAgent:
    return _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())


@pytest.fixture
def opencode_agent_no_common(local_provider: LocalProviderInstance, tmp_path: Path) -> OpenCodeAgent:
    return _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(emit_common_transcript=False))


def test_assemble_command_injects_per_agent_config_and_data_env(opencode_agent: OpenCodeAgent) -> None:
    """OPENCODE_CONFIG_DIR + XDG_DATA_HOME are injected as an env prefix on the opencode process."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    config_dir = str(opencode_agent._get_opencode_config_dir())
    data_home = str(opencode_agent._get_opencode_data_home())
    assert f"env OPENCODE_CONFIG_DIR={config_dir} XDG_DATA_HOME={data_home}" in command
    # The env prefix sits immediately before the opencode command, not on the whole chain.
    assert command.index("XDG_DATA_HOME") < command.index(" opencode")


def test_assemble_command_resume_prelude_guards_continue_on_root_session_file(
    opencode_agent: OpenCodeAgent,
) -> None:
    """`--continue` is appended only when the plugin-written root-session file exists."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    root_file = str(opencode_agent._get_root_session_file_path())
    assert f"if [ -s {root_file} ]; then set -- --continue; fi" in command


def test_assemble_command_launches_background_supervisor_when_common_enabled(
    opencode_agent: OpenCodeAgent,
) -> None:
    command = str(opencode_agent.assemble_command(opencode_agent.host, (), command_override=None))
    assert "( bash $MNGR_AGENT_STATE_DIR/commands/opencode_background_tasks.sh" in command
    assert command.strip().startswith("( bash ")


def test_assemble_command_omits_supervisor_when_common_disabled(
    opencode_agent_no_common: OpenCodeAgent,
) -> None:
    command = str(opencode_agent_no_common.assemble_command(opencode_agent_no_common.host, (), command_override=None))
    assert "opencode_background_tasks.sh" not in command
    assert "env OPENCODE_CONFIG_DIR=" in command


def test_assemble_command_appends_user_agent_args(opencode_agent: OpenCodeAgent) -> None:
    command = str(opencode_agent.assemble_command(opencode_agent.host, ("run", "hello"), command_override=None))
    assert " opencode run hello " in command


def test_assemble_command_shell_quotes_agent_args_with_spaces_and_parens(opencode_agent: OpenCodeAgent) -> None:
    """A model name with spaces/parens is shell-quoted, not spliced in raw (bash would mis-parse `(`)."""
    command = str(opencode_agent.assemble_command(opencode_agent.host, ("--model", "A B (C)"), command_override=None))
    assert "'A B (C)'" in command


def _provision(agent: OpenCodeAgent) -> None:
    agent.provision(
        host=agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("opencode")),
        mngr_ctx=agent.mngr_ctx,
    )


def test_provision_writes_per_agent_config_with_schema(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    config_path = get_opencode_config_file_path(opencode_agent._get_opencode_config_dir())
    assert config_path.exists()
    parsed = json.loads(config_path.read_text())
    assert parsed["$schema"] == "https://opencode.ai/config.json"


def test_provision_inherits_user_global_config_and_applies_overrides(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    """sync_global_config seeds from the user's ~/.config/opencode/opencode.json; overrides win."""
    user_config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    user_config_path.write_text(json.dumps({"theme": "user-theme", "model": "old/model"}))
    agent = _make_opencode_agent(
        local_provider, tmp_path, OpenCodeAgentConfig(config_overrides={"model": "anthropic/claude-sonnet-4-5"})
    )
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert parsed["theme"] == "user-theme"
    assert parsed["model"] == "anthropic/claude-sonnet-4-5"


def test_provision_injects_wildcard_allow_when_auto_allow(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(auto_allow_permissions=True))
    _provision(agent)
    parsed = json.loads(get_opencode_config_file_path(agent._get_opencode_config_dir()).read_text())
    assert parsed["permission"] == {"*": "allow"}


def test_provision_installs_lifecycle_plugin(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    plugin_path = get_opencode_plugin_path(opencode_agent._get_opencode_config_dir())
    assert plugin_path.exists()
    assert "MngrLifecyclePlugin" in plugin_path.read_text()


def test_provision_symlinks_auth_to_shared_path_by_default(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    auth_path = get_opencode_auth_path_for_data_home(opencode_agent._get_opencode_data_home())
    assert auth_path.is_symlink()
    assert auth_path.readlink() == get_shared_opencode_auth_path(Path.home())


def test_provision_copies_auth_when_symlink_disabled(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    shared = get_shared_opencode_auth_path(Path.home())
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text('{"anthropic":{"type":"api","key":"seed"}}')
    agent = _make_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig(symlink_auth=False))
    _provision(agent)
    auth_path = get_opencode_auth_path_for_data_home(agent._get_opencode_data_home())
    assert auth_path.exists()
    assert not auth_path.is_symlink()
    assert json.loads(auth_path.read_text())["anthropic"]["type"] == "api"


def test_provision_installs_transcript_scripts(opencode_agent: OpenCodeAgent) -> None:
    _provision(opencode_agent)
    commands_dir = opencode_agent._get_agent_dir() / "commands"
    assert (commands_dir / "opencode_common_transcript.sh").exists()
    assert (commands_dir / "opencode_background_tasks.sh").exists()


def test_provision_omits_converter_when_common_disabled(opencode_agent_no_common: OpenCodeAgent) -> None:
    _provision(opencode_agent_no_common)
    commands_dir = opencode_agent_no_common._get_agent_dir() / "commands"
    assert not (commands_dir / "opencode_common_transcript.sh").exists()
    # The supervisor is still installed; it simply finds nothing to supervise.
    assert (commands_dir / "opencode_background_tasks.sh").exists()


def test_provision_does_not_write_into_work_dir(opencode_agent: OpenCodeAgent) -> None:
    """The plugin isolates everything under the agent state dir; the user's work_dir is untouched."""
    _provision(opencode_agent)
    assert not (opencode_agent.work_dir / "opencode.json").exists()
    assert not (opencode_agent.work_dir / ".opencode").exists()
