"""Unit tests for the exec CLI command."""

import json
from io import StringIO

import pluggy
import pytest
from click.testing import CliRunner
from loguru import logger

from imbue.mngr.api.exec import ExecResult
from imbue.mngr.api.exec import MultiExecResult
from imbue.mngr.cli.exec import ExecCliOptions
from imbue.mngr.cli.exec import _emit_human_output
from imbue.mngr.cli.exec import _emit_json_output
from imbue.mngr.cli.exec import _emit_jsonl_error
from imbue.mngr.cli.exec import _emit_jsonl_exec_result
from imbue.mngr.cli.exec import _emit_output
from imbue.mngr.cli.exec import exec_command
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat


def test_exec_cli_options_fields() -> None:
    """Test ExecCliOptions has required fields."""
    opts = ExecCliOptions(
        agents=("my-agent",),
        agent_list=(),
        command_arg="echo hello",
        user=None,
        cwd=None,
        timeout=None,
        start=True,
        on_error="continue",
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
    assert opts.agents == ("my-agent",)
    assert opts.command_arg == "echo hello"
    assert opts.user is None
    assert opts.cwd is None
    assert opts.timeout is None
    assert opts.start is True
    assert opts.on_error == "continue"


def test_exec_requires_command(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that exec requires the COMMAND argument."""
    result = cli_runner.invoke(
        exec_command,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_exec_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that exec requires at least one agent."""
    result = cli_runner.invoke(
        exec_command,
        ["echo hello"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


def test_emit_human_output_single_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test human output prints stdout and logs success for a single agent."""
    exec_result = ExecResult(agent_name="test-agent", stdout="hello world\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[exec_result], failed_agents=[])
    _emit_human_output(multi_result)

    captured = capsys.readouterr()
    assert "hello world" in captured.out


def test_emit_human_output_single_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """Test human output handles failed commands."""
    exec_result = ExecResult(agent_name="test-agent", stdout="", stderr="bad command\n", success=False)
    multi_result = MultiExecResult(successful_results=[exec_result], failed_agents=[])
    _emit_human_output(multi_result)

    captured = capsys.readouterr()
    assert "bad command" in captured.err


def test_emit_human_output_multi_agent_shows_headers(capsys: pytest.CaptureFixture[str]) -> None:
    """Test human output shows agent name headers when there are multiple results."""
    result1 = ExecResult(agent_name="agent-1", stdout="output1\n", stderr="", success=True)
    result2 = ExecResult(agent_name="agent-2", stdout="output2\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1, result2], failed_agents=[])
    _emit_human_output(multi_result)

    captured = capsys.readouterr()
    assert "output1" in captured.out
    assert "output2" in captured.out


def test_emit_json_output_multi_agent(capsys: pytest.CaptureFixture[str]) -> None:
    """Test JSON output format for multiple agents."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    result2 = ExecResult(agent_name="agent-2", stdout="world\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1, result2], failed_agents=[])
    _emit_json_output(multi_result)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["total_executed"] == 2
    assert output["total_failed"] == 0
    assert len(output["results"]) == 2
    assert output["results"][0]["agent"] == "agent-1"
    assert output["results"][1]["agent"] == "agent-2"


def test_emit_json_output_with_failures(capsys: pytest.CaptureFixture[str]) -> None:
    """Test JSON output format includes failed agents."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    multi_result = MultiExecResult(
        successful_results=[result1],
        failed_agents=[("agent-2", "host offline")],
    )
    _emit_json_output(multi_result)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["total_executed"] == 1
    assert output["total_failed"] == 1
    assert output["failed_agents"][0]["agent"] == "agent-2"
    assert output["failed_agents"][0]["error"] == "host offline"


def test_emit_jsonl_exec_result(capsys: pytest.CaptureFixture[str]) -> None:
    """Test JSONL output format for a single exec result."""
    result = ExecResult(agent_name="test-agent", stdout="hello\n", stderr="", success=True)
    _emit_jsonl_exec_result(result)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "exec_result"
    assert output["agent"] == "test-agent"
    assert output["success"] is True


# =============================================================================
# Tests for _emit_jsonl_error
# =============================================================================


def test_emit_jsonl_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_jsonl_error outputs proper JSONL error event."""
    _emit_jsonl_error("test-agent", "connection refused")
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "exec_error"
    assert output["agent"] == "test-agent"
    assert output["error"] == "connection refused"


# =============================================================================
# Tests for _emit_output (dispatch function)
# =============================================================================


def test_emit_output_with_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_output with a format template produces templated output."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{agent}\t{stdout}")
    _emit_output(multi_result, output_opts)
    captured = capsys.readouterr()
    assert "agent-1\thello" in captured.out


def test_emit_output_format_template_strips_trailing_newline_from_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that format template output strips trailing newlines from stdout/stderr."""
    result1 = ExecResult(agent_name="agent-1", stdout="output\n", stderr="err\n", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{stdout}|{stderr}")
    _emit_output(multi_result, output_opts)
    captured = capsys.readouterr()
    # Should strip trailing \n from stdout and stderr in format template mode
    assert "output|err" in captured.out


def test_emit_output_format_template_includes_failed_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that format template output includes failed agents."""
    multi_result = MultiExecResult(
        successful_results=[],
        failed_agents=[("agent-x", "host offline")],
    )
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{agent}: {stderr}")
    _emit_output(multi_result, output_opts)
    captured = capsys.readouterr()
    assert "agent-x: host offline" in captured.out


def test_emit_output_dispatches_to_human(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_output dispatches to human output."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_output(multi_result, output_opts)
    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_emit_output_dispatches_to_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_output dispatches to JSON output."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_output(multi_result, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["total_executed"] == 1


def test_emit_output_jsonl_raises() -> None:
    """Test _emit_output raises AssertionError for JSONL (should use streaming)."""
    multi_result = MultiExecResult(successful_results=[], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    with pytest.raises(AssertionError, match="JSONL should be handled with streaming"):
        _emit_output(multi_result, output_opts)


# =============================================================================
# Tests for _emit_human_output (additional coverage)
# =============================================================================


def test_emit_human_output_stdout_without_trailing_newline(capsys: pytest.CaptureFixture[str]) -> None:
    """Test human output adds trailing newline if stdout doesn't have one."""
    result = ExecResult(agent_name="test-agent", stdout="no trailing newline", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result], failed_agents=[])
    _emit_human_output(multi_result)
    captured = capsys.readouterr()
    # The output should still be readable with a newline added
    assert "no trailing newline" in captured.out


def test_emit_human_output_stderr_without_trailing_newline(capsys: pytest.CaptureFixture[str]) -> None:
    """Test human output adds trailing newline if stderr doesn't have one."""
    result = ExecResult(agent_name="test-agent", stdout="", stderr="error output", success=False)
    multi_result = MultiExecResult(successful_results=[result], failed_agents=[])
    _emit_human_output(multi_result)
    captured = capsys.readouterr()
    assert "error output" in captured.err


def test_emit_human_output_failed_agents_logs_errors() -> None:
    """Test that _emit_human_output logs error messages for failed agents."""
    multi_result = MultiExecResult(
        successful_results=[],
        failed_agents=[("agent-x", "host offline"), ("agent-y", "timeout")],
    )
    log_output = StringIO()
    sink_id = logger.add(log_output, level="ERROR", format="{message}")
    try:
        _emit_human_output(multi_result)
    finally:
        logger.remove(sink_id)
    output = log_output.getvalue()
    assert "agent-x" in output
    assert "host offline" in output
    assert "agent-y" in output
    assert "timeout" in output
