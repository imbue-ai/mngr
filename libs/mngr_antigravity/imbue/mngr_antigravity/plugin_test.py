"""Unit tests for AntigravityAgentConfig and AntigravityAgent."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_antigravity.plugin import AntigravityAgent
from imbue.mngr_antigravity.plugin import AntigravityAgentConfig
from imbue.mngr_antigravity.plugin import register_agent_type


def test_antigravity_agent_config_has_correct_defaults() -> None:
    config = AntigravityAgentConfig()

    assert str(config.command) == "agy"
    assert config.cli_args == ()
    assert config.parent_type is None
    assert config.auto_allow_permissions is False


def test_antigravity_agent_config_merge_with_concatenates_user_args() -> None:
    """User-supplied cli_args concatenate onto the (empty) default."""
    base = AntigravityAgentConfig()
    override = AntigravityAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, AntigravityAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "agy"


def test_antigravity_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(AntigravityAgent, InteractiveTuiAgent)


def test_antigravity_agent_advertises_tui_ready_indicator() -> None:
    """Ready indicator is the stable splash-banner substring captured from `agy` 1.0.0."""
    assert AntigravityAgent.TUI_READY_INDICATOR == "Antigravity CLI"


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


def test_assemble_command_uses_bare_agy_command_with_no_default_cli_args(
    antigravity_agent: AntigravityAgent,
) -> None:
    command = antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None)
    assert str(command) == "agy"


def test_assemble_command_appends_user_agent_args(antigravity_agent: AntigravityAgent) -> None:
    command = antigravity_agent.assemble_command(antigravity_agent.host, ("--add-dir", "/tmp"), command_override=None)
    assert str(command) == "agy --add-dir /tmp"


def test_assemble_command_omits_dangerously_skip_permissions_when_auto_allow_disabled(
    antigravity_agent: AntigravityAgent,
) -> None:
    command = str(antigravity_agent.assemble_command(antigravity_agent.host, (), command_override=None))
    assert "--dangerously-skip-permissions" not in command


def test_assemble_command_appends_dangerously_skip_permissions_when_auto_allow_enabled(
    antigravity_agent_auto_allow: AntigravityAgent,
) -> None:
    """`auto_allow_permissions=True` wires Antigravity's documented auto-approve flag."""
    agent = antigravity_agent_auto_allow
    command = str(agent.assemble_command(agent.host, (), command_override=None))
    assert command == "agy --dangerously-skip-permissions"


def test_assemble_command_preserves_user_args_when_auto_allow_enabled(
    antigravity_agent_auto_allow: AntigravityAgent,
) -> None:
    """User-supplied agent_args still land before the auto-allow flag."""
    agent = antigravity_agent_auto_allow
    command = str(agent.assemble_command(agent.host, ("--add-dir", "/tmp"), command_override=None))
    assert command == "agy --add-dir /tmp --dangerously-skip-permissions"


def test_get_expected_process_name_returns_agy(antigravity_agent: AntigravityAgent) -> None:
    """`agy` is the single-file Go binary name visible to ps/tmux."""
    assert antigravity_agent.get_expected_process_name() == "agy"


def test_modify_env_vars_is_a_noop(antigravity_agent: AntigravityAgent) -> None:
    """Antigravity has no equivalent of Gemini's GEMINI_CLI_SYSTEM_SETTINGS_PATH/GEMINI_CLI_TRUST_WORKSPACE env vars.

    ``modify_env_vars`` therefore inherits the no-op default; this test pins
    that contract so a future re-introduction of env-var injection has to be
    explicit.
    """
    env_vars = {"PRE_EXISTING": "kept"}
    antigravity_agent.modify_env_vars(antigravity_agent.host, env_vars)
    assert env_vars == {"PRE_EXISTING": "kept"}


def test_provision_does_not_create_workspace_subdirs(antigravity_agent: AntigravityAgent) -> None:
    """v0 plugin writes nothing to the user's work_dir.

    Antigravity reads workspace-tier files from `<work_dir>/.agents/` and
    `<work_dir>/.antigravityignore`; v0 must not populate either. Re-evaluate
    when transcript / readiness-hook support lands.
    """
    antigravity_agent.provision(
        host=antigravity_agent.host,
        options=CreateAgentOptions(agent_type=AgentTypeName("antigravity")),
        mngr_ctx=antigravity_agent.mngr_ctx,
    )
    assert not (antigravity_agent.work_dir / ".agents").exists()
    assert not (antigravity_agent.work_dir / ".antigravityignore").exists()
    assert not (antigravity_agent.work_dir / ".gemini").exists()
