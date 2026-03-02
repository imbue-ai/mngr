"""Unit tests for the stop CLI command."""

import json

import pluggy
from click.testing import CliRunner

from imbue.mng.cli.conftest import capture_stdout
from imbue.mng.cli.stop import StopCliOptions
from imbue.mng.cli.stop import _output_result
from imbue.mng.cli.stop import stop
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat


def test_stop_cli_options_fields() -> None:
    """Test StopCliOptions has required fields."""
    opts = StopCliOptions(
        agents=("agent1", "agent2"),
        agent_list=("agent3",),
        stop_all=False,
        dry_run=True,
        sessions=(),
        include=(),
        exclude=(),
        stdin=False,
        snapshot_mode=None,
        graceful=True,
        graceful_timeout=None,
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
    assert opts.stop_all is False
    assert opts.dry_run is True
    assert opts.sessions == ()


def test_stop_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that stop requires at least one agent or --all."""
    result = cli_runner.invoke(
        stop,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent or use --all" in result.output


def test_stop_cannot_combine_agents_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --all cannot be combined with agent names."""
    result = cli_runner.invoke(
        stop,
        ["my-agent", "--all"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify both agent names and --all" in result.output


def test_stop_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test stopping a non-existent agent."""
    result = cli_runner.invoke(
        stop,
        ["nonexistent-agent-45721"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0


def test_stop_all_with_no_running_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test stopping all agents when none are running."""
    result = cli_runner.invoke(
        stop,
        ["--all"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    # Should succeed but report no agents to stop
    assert result.exit_code == 0
    assert "No running agents found to stop" in result.output


def test_stop_session_cannot_combine_with_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with agent names."""
    result = cli_runner.invoke(
        stop,
        ["my-agent", "--session", "mng-some-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify --session with agent names or --all" in result.output


def test_stop_session_cannot_combine_with_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with --all."""
    result = cli_runner.invoke(
        stop,
        ["--session", "mng-some-agent", "--all"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify --session with agent names or --all" in result.output


def test_stop_session_fails_with_invalid_prefix(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session fails when session doesn't match expected prefix format."""
    result = cli_runner.invoke(
        stop,
        ["--session", "other-session-name"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "does not match the expected format" in result.output


# =============================================================================
# Dry-run and format tests
# =============================================================================


def test_stop_dry_run_all_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--dry-run --all with no running agents returns 0."""
    result = cli_runner.invoke(
        stop,
        ["--all", "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No running agents found to stop" in result.output


def test_stop_format_json_all_no_running_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--format json --all with no running agents outputs nothing (early return).

    In JSON mode, the "No running agents" message is not emitted because _output()
    only writes for HUMAN format. The command returns early before _output_result().
    """
    result = cli_runner.invoke(
        stop,
        ["--all", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert result.output.strip() == ""


# =============================================================================
# StopCliOptions additional field tests
# =============================================================================


def test_stop_cli_options_accepts_all_optional_fields() -> None:
    """Test StopCliOptions can be instantiated with all optional fields set."""
    opts = StopCliOptions(
        agents=("a1", "a2", "a3"),
        agent_list=("a4",),
        stop_all=True,
        dry_run=False,
        sessions=("mng-session-1", "mng-session-2"),
        include=("state == 'RUNNING'",),
        exclude=("name == 'keep-me'",),
        stdin=True,
        snapshot_mode="auto",
        graceful=False,
        graceful_timeout="30s",
        output_format="json",
        quiet=True,
        verbose=2,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=("my-plugin",),
        disable_plugin=("other-plugin",),
    )
    assert opts.agents == ("a1", "a2", "a3")
    assert opts.stop_all is True
    assert opts.sessions == ("mng-session-1", "mng-session-2")
    assert opts.include == ("state == 'RUNNING'",)
    assert opts.exclude == ("name == 'keep-me'",)
    assert opts.stdin is True
    assert opts.snapshot_mode == "auto"
    assert opts.graceful is False
    assert opts.graceful_timeout == "30s"
    assert opts.quiet is True
    assert opts.verbose == 2
    assert opts.plugin == ("my-plugin",)
    assert opts.disable_plugin == ("other-plugin",)


def test_stop_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that stop --help works and exits 0."""
    result = cli_runner.invoke(
        stop,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "stop" in result.output.lower()


# =============================================================================
# Output helper function tests
# =============================================================================


def test_stop_output_result_human_with_agents() -> None:
    """Test _output_result in HUMAN format with stopped agents."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with capture_stdout() as buf:
        _output_result(["agent-1", "agent-2"], output_opts)
    assert "Successfully stopped 2 agent(s)" in buf.getvalue()


def test_stop_output_result_human_empty() -> None:
    """Test _output_result in HUMAN format with no agents outputs nothing."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with capture_stdout() as buf:
        _output_result([], output_opts)
    # With no agents, the HUMAN output does not write a success message
    assert "Successfully stopped" not in buf.getvalue()


def test_stop_output_result_json() -> None:
    """Test _output_result in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    with capture_stdout() as buf:
        _output_result(["agent-x"], output_opts)
    data = json.loads(buf.getvalue().strip())
    assert data["stopped_agents"] == ["agent-x"]
    assert data["count"] == 1


def test_stop_output_result_jsonl() -> None:
    """Test _output_result in JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    with capture_stdout() as buf:
        _output_result(["agent-a"], output_opts)
    data = json.loads(buf.getvalue().strip())
    assert data["event"] == "stop_result"
    assert data["count"] == 1


def test_stop_output_result_format_template() -> None:
    """Test _output_result with a format template."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    with capture_stdout() as buf:
        _output_result(["template-agent"], output_opts)
    assert "template-agent" in buf.getvalue()
