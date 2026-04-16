"""Unit tests for the Hermes agent plugin."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_hermes.plugin import HermesAgent
from imbue.mngr_hermes.plugin import HermesAgentConfig
from imbue.mngr_hermes.plugin import _HERMES_HOME_DIR_NAME
from imbue.mngr_hermes.plugin import _HERMES_HOME_SEED_DIRS
from imbue.mngr_hermes.plugin import _HERMES_HOME_SEED_FILES
from imbue.mngr_hermes.plugin import _get_user_hermes_dir
from imbue.mngr_hermes.plugin import register_agent_type


def _make_hermes_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    mngr_ctx: MngrContext,
    agent_config: HermesAgentConfig | None = None,
) -> tuple[HermesAgent, Host]:
    """Create a HermesAgent with a real local host for testing."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    if agent_config is None:
        agent_config = HermesAgentConfig()

    agent = HermesAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-hermes"),
        agent_type=AgentTypeName("hermes"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=agent_config,
        host=host,
    )
    return agent, host


def _create_fake_hermes_dir(home_dir: Path) -> Path:
    """Create a fake .hermes directory under the given home with all seed files and dirs."""
    hermes_dir = home_dir / ".hermes"
    hermes_dir.mkdir()

    # Seed files (reference constants to avoid hardcoding filenames)
    (hermes_dir / _HERMES_HOME_SEED_FILES[0]).write_text("model: anthropic/claude-sonnet-4")
    (hermes_dir / _HERMES_HOME_SEED_FILES[1]).write_text("ANTHROPIC_API_KEY=sk-test")
    (hermes_dir / _HERMES_HOME_SEED_FILES[2]).write_text('{"token": "test"}')
    (hermes_dir / _HERMES_HOME_SEED_FILES[3]).write_text("I am a helpful agent.")

    # Seed directories with content
    (hermes_dir / "memories").mkdir()
    (hermes_dir / "memories" / "MEMORY.md").write_text("# Memories")
    (hermes_dir / "skills").mkdir()
    (hermes_dir / "skills" / "test_skill.md").write_text("# Skill")
    (hermes_dir / "home").mkdir()
    (hermes_dir / "home" / ".gitconfig").write_text("[user]\nname = Test")

    # Runtime state files that should NOT be copied
    (hermes_dir / "state.db").write_text("runtime state")
    (hermes_dir / "sessions").mkdir()
    (hermes_dir / "sessions" / "session1.json").write_text("{}")
    (hermes_dir / "logs").mkdir()
    (hermes_dir / ".hermes_history").write_text("history")

    return hermes_dir


# =============================================================================
# Config Tests
# =============================================================================


def test_hermes_agent_config_has_correct_defaults() -> None:
    """HermesAgentConfig should default to 'hermes chat' command."""
    config = HermesAgentConfig()

    assert str(config.command) == "hermes chat"
    assert config.cli_args == ()
    assert config.permissions == []
    assert config.parent_type is None


def test_hermes_agent_config_merge_with_override_cli_args() -> None:
    """merge_with should concatenate cli_args from base and override."""
    base = HermesAgentConfig()
    override = HermesAgentConfig(cli_args=("-m", "anthropic/claude-sonnet-4"))

    merged = base.merge_with(override)

    assert isinstance(merged, HermesAgentConfig)
    assert merged.cli_args == ("-m", "anthropic/claude-sonnet-4")
    assert str(merged.command) == "hermes chat"


def test_hermes_agent_config_merge_with_override_command() -> None:
    """merge_with should prefer override command when set."""
    base = HermesAgentConfig()
    override = HermesAgentConfig(command=CommandString("hermes --debug chat"))

    merged = base.merge_with(override)

    assert isinstance(merged, HermesAgentConfig)
    assert str(merged.command) == "hermes --debug chat"


# =============================================================================
# Hook Registration Tests
# =============================================================================


def test_register_agent_type_returns_correct_tuple() -> None:
    """register_agent_type should return the hermes agent type, class, and config."""
    result = register_agent_type()

    assert result[0] == "hermes"
    assert result[1] is HermesAgent
    assert result[2] is HermesAgentConfig


# =============================================================================
# modify_env_vars Tests
# =============================================================================


def test_modify_env_vars_injects_hermes_home(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """modify_env_vars should set HERMES_HOME to the per-agent hermes home directory."""
    agent, host = _make_hermes_agent(local_provider, tmp_path, temp_mngr_ctx)

    env_vars: dict[str, str] = {}
    agent.modify_env_vars(host, env_vars)

    assert "HERMES_HOME" in env_vars
    expected_suffix = f"agents/{agent.id}/{_HERMES_HOME_DIR_NAME}"
    assert env_vars["HERMES_HOME"].endswith(expected_suffix)


# =============================================================================
# Provision Tests
# =============================================================================


def test_provision_skips_when_hermes_dir_missing(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision should silently skip when ~/.hermes does not exist."""
    # The setup_test_mngr_env fixture already sets HOME to tmp_path,
    # so ~/.hermes does not exist by default.
    agent, host = _make_hermes_agent(local_provider, tmp_path, temp_mngr_ctx)
    options = CreateAgentOptions(agent_type=AgentTypeName("hermes"))

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # The hermes_home directory should NOT have been created
    hermes_home = agent._get_hermes_home_dir()
    assert not hermes_home.exists()


def test_provision_seeds_files_from_hermes_dir(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision should copy seed files and directories from ~/.hermes to the agent's HERMES_HOME."""
    agent, host = _make_hermes_agent(local_provider, tmp_path, temp_mngr_ctx)
    options = CreateAgentOptions(agent_type=AgentTypeName("hermes"))

    # HOME is already set to tmp_path by the autouse fixture.
    # Create .hermes under the fake HOME so _get_user_hermes_dir() finds it.
    hermes_dir = _create_fake_hermes_dir(tmp_path)

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    hermes_home = agent._get_hermes_home_dir()

    # Verify seed files were copied
    first_seed_file = _HERMES_HOME_SEED_FILES[0]
    assert (hermes_home / first_seed_file).read_text() == "model: anthropic/claude-sonnet-4"
    assert (hermes_home / ".env").read_text() == "ANTHROPIC_API_KEY=sk-test"
    assert (hermes_home / "auth.json").read_text() == '{"token": "test"}'
    assert (hermes_home / "SOUL.md").read_text() == "I am a helpful agent."

    # Verify seed directories were copied
    assert (hermes_home / "memories" / "MEMORY.md").read_text() == "# Memories"
    assert (hermes_home / "skills" / "test_skill.md").read_text() == "# Skill"
    assert (hermes_home / "home" / ".gitconfig").read_text() == "[user]\nname = Test"

    # Verify runtime state was NOT copied
    assert not (hermes_home / "state.db").exists()
    assert not (hermes_home / "sessions").exists()
    assert not (hermes_home / "logs").exists()
    assert not (hermes_home / ".hermes_history").exists()


def test_provision_handles_partial_hermes_dir(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision should handle a ~/.hermes that only has some of the expected files."""
    agent, host = _make_hermes_agent(local_provider, tmp_path, temp_mngr_ctx)
    options = CreateAgentOptions(agent_type=AgentTypeName("hermes"))

    # Create .hermes with only the first seed file
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    first_seed_file = _HERMES_HOME_SEED_FILES[0]
    (hermes_dir / first_seed_file).write_text("model: test")

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    hermes_home = agent._get_hermes_home_dir()

    # Only the seed file should be present
    assert (hermes_home / first_seed_file).read_text() == "model: test"
    assert not (hermes_home / ".env").exists()
    assert not (hermes_home / "memories").exists()


def test_provision_skips_seeding_when_hermes_dir_empty(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision should create hermes_home dir but skip rsync when ~/.hermes has no seed files."""
    agent, host = _make_hermes_agent(local_provider, tmp_path, temp_mngr_ctx)
    options = CreateAgentOptions(agent_type=AgentTypeName("hermes"))

    # Create an empty .hermes (exists but has no seed files)
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()

    agent.provision(host=host, options=options, mngr_ctx=temp_mngr_ctx)

    # hermes_home dir should exist (created by mkdir -p) but be empty
    hermes_home = agent._get_hermes_home_dir()
    assert hermes_home.exists()
    assert list(hermes_home.iterdir()) == []


# =============================================================================
# Constant Validation
# =============================================================================


def test_seed_lists_contain_expected_entries() -> None:
    """Verify the seed file and directory lists match the spec."""
    assert len(_HERMES_HOME_SEED_FILES) == 4
    assert ".env" in _HERMES_HOME_SEED_FILES
    assert "auth.json" in _HERMES_HOME_SEED_FILES
    assert "SOUL.md" in _HERMES_HOME_SEED_FILES
    assert set(_HERMES_HOME_SEED_DIRS) == {"memories", "skills", "home"}


def test_get_user_hermes_dir_returns_dot_hermes() -> None:
    """_get_user_hermes_dir should return ~/.hermes."""
    result = _get_user_hermes_dir()
    assert result == Path.home() / ".hermes"
