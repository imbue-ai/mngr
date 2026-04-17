"""Tests for agent_discovery module."""

from pathlib import Path

from imbue.minds_workspace_server.agent_discovery import read_claude_config_dir_from_env_file
from imbue.minds_workspace_server.agent_discovery import resolve_root_agent_type
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.primitives import AgentTypeName


def test_reads_claude_config_dir_from_env_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text('CLAUDE_CONFIG_DIR="/custom/config/dir"\n')

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/custom/config/dir")


def test_falls_back_to_conventional_path_when_env_file_missing(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_conventional_path_when_env_has_no_config_dir(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text("OTHER_VAR=something\n")
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_home_claude_when_nothing_else_exists(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path.home() / ".claude"


def test_resolve_root_agent_type_walks_parent_chain() -> None:
    """Types with parent_type resolve to the root, matching the watcher-selection contract."""
    config = MngrConfig(
        agent_types={
            AgentTypeName("hermes_main"): AgentTypeConfig(parent_type=AgentTypeName("hermes")),
        },
    )

    assert resolve_root_agent_type("hermes_main", config) == "hermes"


def test_resolve_root_agent_type_returns_unknown_types_unchanged() -> None:
    """Types not present in the config resolve to themselves."""
    config = MngrConfig()

    assert resolve_root_agent_type("claude", config) == "claude"


def test_resolve_root_agent_type_returns_empty_string_when_type_is_empty() -> None:
    """agent_manager stores '' as a fallback when a discovery event has no type.

    Without defensive handling, AgentTypeName('') raises InvalidName and every
    _find_agent-backed endpoint returns 500 for that agent.
    """
    config = MngrConfig()

    assert resolve_root_agent_type("", config) == ""


def test_resolve_root_agent_type_returns_invalid_name_unchanged() -> None:
    """Strings that can't be cast to AgentTypeName (e.g. containing spaces) resolve to themselves."""
    config = MngrConfig()

    assert resolve_root_agent_type("not a valid name", config) == "not a valid name"
