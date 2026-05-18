"""Unit tests for the start CLI command."""

import json
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.start import StartCliOptions
from imbue.mngr.cli.start import _output_result
from imbue.mngr.cli.start import _try_acquire_restart_lock
from imbue.mngr.cli.start import start
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import OutputFormat


def test_start_cli_options_fields() -> None:
    """Test StartCliOptions has required fields."""
    opts = StartCliOptions(
        agents=("agent1", "agent2"),
        agent_list=(AgentAddress(agent=AgentName("agent3")),),
        connect=False,
        connect_command=None,
        restart=False,
        host=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.agents == ("agent1", "agent2")
    assert opts.agent_list == (AgentAddress(agent=AgentName("agent3")),)
    assert opts.connect is False
    assert opts.restart is False


def test_start_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that start requires at least one agent."""
    result = cli_runner.invoke(
        start,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


def test_start_connect_with_multiple_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --connect with multiple agents fails."""
    result = cli_runner.invoke(
        start,
        ["agent1", "agent2", "--connect"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--connect can only be used with a single agent" in result.output


# =============================================================================
# Output helper tests
# =============================================================================


def test_output_result_human_with_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with started agents."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result(["agent-1", "agent-2"], output_opts)
    captured = capsys.readouterr()
    assert "Successfully started 2 agent(s)" in captured.out


def test_output_result_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _output_result(["agent-x"], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["started_agents"] == ["agent-x"]
    assert data["count"] == 1


def test_output_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _output_result(["agent-a", "agent-b"], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "start_result"
    assert data["count"] == 2


def test_output_result_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result with format template."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    _output_result(["my-agent"], output_opts)
    captured = capsys.readouterr()
    assert "my-agent" in captured.out


# =============================================================================
# Restart lock tests
# =============================================================================


def test_try_acquire_restart_lock_succeeds(tmp_path: Path) -> None:
    """Acquiring the restart lock on a fresh directory returns an open file handle."""
    agent_id = AgentId()
    lock_handle = _try_acquire_restart_lock(tmp_path, agent_id)
    assert lock_handle is not None
    assert not lock_handle.closed
    lock_handle.close()


def test_try_acquire_restart_lock_contention_returns_none(tmp_path: Path) -> None:
    """A second non-blocking acquire while the first is held returns None."""
    agent_id = AgentId()
    first_handle = _try_acquire_restart_lock(tmp_path, agent_id)
    assert first_handle is not None

    second_handle = _try_acquire_restart_lock(tmp_path, agent_id)
    assert second_handle is None

    first_handle.close()


def test_try_acquire_restart_lock_reacquire_after_release(tmp_path: Path) -> None:
    """After the first lock is released, a new acquire succeeds."""
    agent_id = AgentId()
    first_handle = _try_acquire_restart_lock(tmp_path, agent_id)
    assert first_handle is not None
    first_handle.close()

    second_handle = _try_acquire_restart_lock(tmp_path, agent_id)
    assert second_handle is not None
    second_handle.close()


def test_try_acquire_restart_lock_creates_parent_directories(tmp_path: Path) -> None:
    """The lock function creates missing parent directories for the lock file."""
    agent_id = AgentId()
    expected_dir = tmp_path / "agents" / str(agent_id)
    assert not expected_dir.exists()

    lock_handle = _try_acquire_restart_lock(tmp_path, agent_id)
    assert lock_handle is not None
    assert expected_dir.exists()
    lock_handle.close()
