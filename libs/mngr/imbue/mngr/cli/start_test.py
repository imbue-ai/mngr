"""Unit tests for the start CLI command."""

import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.start import StartCliOptions
from imbue.mngr.cli.start import _output_result
from imbue.mngr.cli.start import start
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat


def test_start_cli_options_fields() -> None:
    """Test StartCliOptions has required fields."""
    opts = StartCliOptions(
        agents=("agent1", "agent2"),
        agent_list=("agent3",),
        connect=False,
        connect_command=None,
        host=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.agents == ("agent1", "agent2")
    assert opts.agent_list == ("agent3",)
    assert opts.connect is False


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
