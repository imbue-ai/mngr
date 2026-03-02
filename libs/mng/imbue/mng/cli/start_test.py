"""Unit tests for the start CLI command."""

import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.start import StartCliOptions
from imbue.mng.cli.start import _output_result
from imbue.mng.cli.start import start
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat


def test_start_cli_options_fields() -> None:
    """Test StartCliOptions has required fields."""
    opts = StartCliOptions(
        agents=("agent1", "agent2"),
        agent_list=("agent3",),
        start_all=False,
        dry_run=True,
        connect=False,
        connect_command=None,
        host=(),
        include=(),
        exclude=(),
        stdin=False,
        snapshot=None,
        latest=True,
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
    assert opts.start_all is False
    assert opts.dry_run is True
    assert opts.connect is False


def test_start_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that start requires at least one agent or --all."""
    result = cli_runner.invoke(
        start,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent or use --all" in result.output


def test_start_cannot_combine_agents_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --all cannot be combined with agent names."""
    result = cli_runner.invoke(
        start,
        ["my-agent", "--all"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify both agent names and --all" in result.output


def test_start_connect_requires_single_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --connect requires a single agent."""
    result = cli_runner.invoke(
        start,
        ["--all", "--connect"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "--connect can only be used with a single agent" in result.output


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


def test_start_all_with_no_stopped_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test starting all agents when none are stopped."""
    result = cli_runner.invoke(
        start,
        ["--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    # Should succeed but report no agents to start
    assert result.exit_code == 0
    assert "No stopped agents found to start" in result.output


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


def test_start_dry_run_all_no_stopped_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--dry-run --all with no stopped agents should report none found."""
    result = cli_runner.invoke(
        start,
        ["--all", "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No stopped agents found to start" in result.output


def test_start_all_json_format_no_stopped_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--all --format json with no stopped agents exits 0 with empty output.

    In JSON mode, the "No stopped agents" message is not emitted because _output()
    only writes for HUMAN format. The command returns early before _output_result().
    """
    result = cli_runner.invoke(
        start,
        ["--all", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert result.output.strip() == ""
