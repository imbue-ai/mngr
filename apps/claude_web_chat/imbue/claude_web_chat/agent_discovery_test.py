"""Tests for agent_discovery module."""

from pathlib import Path

from imbue.claude_web_chat.agent_discovery import _read_claude_config_dir_from_env_file


def test_reads_claude_config_dir_from_env_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text('CLAUDE_CONFIG_DIR="/custom/config/dir"\n')

    result = _read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/custom/config/dir")


def test_falls_back_to_conventional_path_when_env_file_missing(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = _read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_conventional_path_when_env_has_no_config_dir(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text("OTHER_VAR=something\n")
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = _read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_home_claude_when_nothing_else_exists(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    result = _read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path.home() / ".claude"
