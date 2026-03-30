"""Unit tests for the stop CLI command."""

import json
from collections.abc import Callable
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.stop import StopCliOptions
from imbue.mngr.cli.stop import _output_result
from imbue.mngr.cli.stop import stop
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat


def test_stop_cli_options_fields() -> None:
    """Test StopCliOptions has required fields."""
    opts = StopCliOptions(
        agents=("agent1", "agent2"),
        agent_list=("agent3",),
        archive=False,
        sessions=(),
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
    assert opts.sessions == ()


def test_stop_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that stop requires at least one agent."""
    result = cli_runner.invoke(
        stop,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent (use '-' to read from stdin)" in result.output


def test_stop_session_cannot_combine_with_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with agent names."""
    result = cli_runner.invoke(
        stop,
        ["my-agent", "--session", "mngr-some-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify --session with agent names" in result.output


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
# StopCliOptions additional field tests
# =============================================================================


def test_stop_cli_options_accepts_all_optional_fields() -> None:
    """Test StopCliOptions can be instantiated with all optional fields set."""
    opts = StopCliOptions(
        agents=("a1", "a2", "a3"),
        agent_list=("a4",),
        archive=True,
        sessions=("mngr-session-1", "mngr-session-2"),
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
    assert opts.sessions == ("mngr-session-1", "mngr-session-2")
    assert opts.snapshot_mode == "auto"
    assert opts.graceful is False
    assert opts.graceful_timeout == "30s"
    assert opts.quiet is True
    assert opts.verbose == 2
    assert opts.plugin == ("my-plugin",)
    assert opts.disable_plugin == ("other-plugin",)


# =============================================================================
# Output helper function tests
# =============================================================================


def test_stop_output_result_human_with_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with stopped agents."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result(["agent-1", "agent-2"], output_opts)
    captured = capsys.readouterr()
    assert "Successfully stopped 2 agent(s)" in captured.out


def test_stop_output_result_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with no agents outputs nothing."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result([], output_opts)
    captured = capsys.readouterr()
    # With no agents, the HUMAN output does not write a success message
    assert "Successfully stopped" not in captured.out


def test_stop_output_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _output_result(["agent-x"], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["stopped_agents"] == ["agent-x"]
    assert data["count"] == 1


def test_stop_output_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _output_result(["agent-a"], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "stop_result"
    assert data["count"] == 1


def test_stop_output_result_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result with a format template."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    _output_result(["template-agent"], output_opts)
    captured = capsys.readouterr()
    assert "template-agent" in captured.out


# =============================================================================
# Archive integration tests (require tmux for running agents)
# =============================================================================


@pytest.mark.tmux
def test_stop_archive_sets_archived_at_label(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    temp_host_dir: Path,
) -> None:
    """stop --archive should stop the agent and set the archived_at label."""
    create_test_agent("archive-test-agent")

    result = cli_runner.invoke(
        stop,
        ["archive-test-agent", "--archive"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Stopped agent: archive-test-agent" in result.output
    assert "Updated labels for agent archive-test-agent" in result.output

    # Verify the archived_at label was set by reading the agent's data.json
    agents_dir = temp_host_dir / "agents"
    agent_dirs = list(agents_dir.iterdir())
    assert len(agent_dirs) >= 1

    for agent_dir in agent_dirs:
        data_path = agent_dir / "data.json"
        if data_path.exists():
            data = json.loads(data_path.read_text())
            if data.get("name") == "archive-test-agent":
                assert "archived_at" in data.get("labels", {}), "archived_at label should be set"
                return

    raise AssertionError("Could not find archive-test-agent data.json")
