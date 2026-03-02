"""Unit tests for the exec CLI command."""

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.exec import ExecResult
from imbue.mng.api.exec import MultiExecResult
from imbue.mng.cli.exec import ExecCliOptions
from imbue.mng.cli.exec import _emit_human_output
from imbue.mng.cli.exec import _emit_json_output
from imbue.mng.cli.exec import _emit_jsonl_error
from imbue.mng.cli.exec import _emit_jsonl_exec_result
from imbue.mng.cli.exec import _emit_output
from imbue.mng.cli.exec import exec_command
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat


@contextmanager
def _capture_stdout() -> Iterator[StringIO]:
    """Temporarily redirect sys.stdout to a StringIO buffer."""
    buf = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old_stdout


def test_exec_cli_options_fields() -> None:
    """Test ExecCliOptions has required fields."""
    opts = ExecCliOptions(
        agents=("my-agent",),
        agent_list=(),
        exec_all=False,
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


def test_exec_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test executing on a non-existent agent."""
    result = cli_runner.invoke(
        exec_command,
        ["nonexistent-agent-99999", "echo hello"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


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


def test_exec_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that exec --help works and exits 0."""
    result = cli_runner.invoke(
        exec_command,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "exec" in result.output.lower()


def test_exec_all_with_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test exec --all with no running agents exits 0."""
    result = cli_runner.invoke(
        exec_command,
        ["--all", "echo hello"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_exec_cannot_combine_agents_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --all cannot be combined with agent names."""
    result = cli_runner.invoke(
        exec_command,
        ["my-agent", "--all", "echo hello"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


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


def test_emit_output_with_format_template() -> None:
    """Test _emit_output with a format template produces templated output."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{agent}\t{stdout}")
    with _capture_stdout() as buf:
        _emit_output(multi_result, output_opts)
    assert "agent-1\thello" in buf.getvalue()


def test_emit_output_format_template_strips_trailing_newline_from_stdout() -> None:
    """Test that format template output strips trailing newlines from stdout/stderr."""
    result1 = ExecResult(agent_name="agent-1", stdout="output\n", stderr="err\n", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{stdout}|{stderr}")
    with _capture_stdout() as buf:
        _emit_output(multi_result, output_opts)
    # Should strip trailing \n from stdout and stderr in format template mode
    assert "output|err" in buf.getvalue()


def test_emit_output_format_template_includes_failed_agents() -> None:
    """Test that format template output includes failed agents."""
    multi_result = MultiExecResult(
        successful_results=[],
        failed_agents=[("agent-x", "host offline")],
    )
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{agent}: {stderr}")
    with _capture_stdout() as buf:
        _emit_output(multi_result, output_opts)
    assert "agent-x: host offline" in buf.getvalue()


def test_emit_output_dispatches_to_human() -> None:
    """Test _emit_output dispatches to human output."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with _capture_stdout() as buf:
        _emit_output(multi_result, output_opts)
    assert "hello" in buf.getvalue()


def test_emit_output_dispatches_to_json() -> None:
    """Test _emit_output dispatches to JSON output."""
    result1 = ExecResult(agent_name="agent-1", stdout="hello\n", stderr="", success=True)
    multi_result = MultiExecResult(successful_results=[result1], failed_agents=[])
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    with _capture_stdout() as buf:
        _emit_output(multi_result, output_opts)
    data = json.loads(buf.getvalue().strip())
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


def test_emit_human_output_failed_agents_in_multi(capsys: pytest.CaptureFixture[str]) -> None:
    """Test human output shows failed agents."""
    multi_result = MultiExecResult(
        successful_results=[],
        failed_agents=[("agent-x", "host offline"), ("agent-y", "timeout")],
    )
    _emit_human_output(multi_result)
    # Error messages go to logger (stderr) not stdout
