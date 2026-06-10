"""Unit tests for the start CLI command."""

import json
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.start import StartCliOptions
from imbue.mngr.cli.start import _output_result
from imbue.mngr.cli.start import start
from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentAddress
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
        no_resume=False,
        dry_run=False,
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


def test_output_result_human_with_restarted_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with restarted agents uses 'restarted' verb."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result(["agent-1"], output_opts, is_restart=True)
    captured = capsys.readouterr()
    assert "Successfully restarted 1 agent(s)" in captured.out


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
# Dry-run tests
# =============================================================================


def test_start_dry_run_reports_plan_without_touching_host(
    local_host: Host,
    temp_work_dir: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--dry-run prints which agents would be started and returns without starting them.

    The agent exists only as persisted state (no live tmux session), so if the
    dry-run actually tried to start it the call would fail. Reaching the
    "Would be started" output proves the dry-run short-circuits before any host op.
    """
    create_test_agent_state(local_host, temp_work_dir, "dry-run-agent")

    result = cli_runner.invoke(
        start,
        ["dry-run-agent", "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Would be started" in result.output
    assert "dry-run-agent" in result.output
