import json
from pathlib import Path

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.message import MessageResult
from imbue.mngr.cli.message import MessageCliOptions
from imbue.mngr.cli.message import _emit_human_output
from imbue.mngr.cli.message import _emit_json_output
from imbue.mngr.cli.message import _emit_jsonl_error
from imbue.mngr.cli.message import _emit_jsonl_success
from imbue.mngr.cli.message import _emit_output
from imbue.mngr.cli.message import _get_message_content
from imbue.mngr.cli.message import message
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat

_DEFAULT_OPTS = MessageCliOptions(
    agents=(),
    agent_list=(),
    message_content=None,
    message_file=None,
    on_error="continue",
    start=False,
    provider=(),
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


def test_message_cli_options_has_expected_fields() -> None:
    """Test that MessageCliOptions has all expected fields."""
    opts = MessageCliOptions(
        agents=("agent1", "agent2"),
        agent_list=("agent3",),
        message_content="Hello",
        message_file=None,
        on_error="continue",
        start=False,
        provider=(),
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
    assert opts.message_content == "Hello"
    assert opts.message_file is None


def test_get_message_content_returns_option_when_provided() -> None:
    """Test that _get_message_content returns the option value when provided."""
    result = _get_message_content("Hello World", click.Context(click.Command("test")), is_interactive=False)
    assert result == "Hello World"


def test_emit_human_output_handles_no_agents() -> None:
    """Test that _emit_human_output handles no agents case."""
    result = MessageResult()

    # Should not raise
    _emit_human_output(result)


def test_emit_json_output_formats_successful_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that _emit_json_output includes successful agents."""
    result = MessageResult()
    result.successful_agents = ["agent1", "agent2"]

    _emit_json_output(result)

    captured = capsys.readouterr()
    assert '"successful_agents": ["agent1", "agent2"]' in captured.out


def test_emit_json_output_formats_failed_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that _emit_json_output includes failed agents."""
    result = MessageResult()
    result.failed_agents = [("agent1", "error message")]

    _emit_json_output(result)

    captured = capsys.readouterr()
    assert '"failed_agents"' in captured.out
    assert '"agent": "agent1"' in captured.out
    assert '"error": "error message"' in captured.out


def test_emit_json_output_includes_counts(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that _emit_json_output includes counts."""
    result = MessageResult()
    result.successful_agents = ["agent1", "agent2", "agent3"]
    result.failed_agents = [("agent4", "error")]

    _emit_json_output(result)

    captured = capsys.readouterr()
    assert '"total_sent": 3' in captured.out
    assert '"total_failed": 1' in captured.out


def test_message_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that message requires at least one agent."""
    result = cli_runner.invoke(
        message,
        ["-m", "hello"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


def test_message_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test message to a non-existent agent reports no agents found."""
    result = cli_runner.invoke(
        message,
        ["nonexistent-agent-55231", "-m", "hello"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # The message command reports "no agents found" rather than failing
    assert result.exit_code == 0
    assert "No agents found" in result.output


# =============================================================================
# Tests for _emit_jsonl_success
# =============================================================================


def test_emit_jsonl_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_jsonl_success outputs proper JSONL event."""
    _emit_jsonl_success("my-agent")
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "message_sent"
    assert output["agent"] == "my-agent"
    assert output["message"] == "Message sent successfully"


# =============================================================================
# Tests for _emit_jsonl_error
# =============================================================================


def test_emit_jsonl_error_message(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_jsonl_error outputs proper JSONL error event."""
    _emit_jsonl_error("failing-agent", "connection refused")
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "message_error"
    assert output["agent"] == "failing-agent"
    assert output["error"] == "connection refused"


# =============================================================================
# Tests for _emit_output (dispatch function)
# =============================================================================


def test_emit_output_human_dispatches(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_output dispatches to human output handler."""
    result = MessageResult()
    result.successful_agents = ["agent-1"]
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_output(result, output_opts)
    captured = capsys.readouterr()
    assert "Message sent to: agent-1" in captured.out


def test_emit_output_json_dispatches(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_output dispatches to JSON output handler."""
    result = MessageResult()
    result.successful_agents = ["agent-1"]
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_output(result, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["total_sent"] == 1
    assert data["successful_agents"] == ["agent-1"]


def test_emit_output_jsonl_raises() -> None:
    """Test _emit_output raises AssertionError for JSONL (should use streaming)."""
    result = MessageResult()
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    with pytest.raises(AssertionError, match="JSONL should be handled with streaming"):
        _emit_output(result, output_opts)


# =============================================================================
# Tests for _emit_human_output (additional coverage)
# =============================================================================


def test_emit_human_output_successful_agents_with_count(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_human_output shows success count."""
    result = MessageResult()
    result.successful_agents = ["agent-1", "agent-2", "agent-3"]
    _emit_human_output(result)
    captured = capsys.readouterr()
    output = captured.out
    assert "Message sent to: agent-1" in output
    assert "Message sent to: agent-2" in output
    assert "Message sent to: agent-3" in output
    assert "Successfully sent message to 3 agent(s)" in output


def test_emit_human_output_only_failed_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_human_output handles case with only failures."""
    result = MessageResult()
    result.failed_agents = [("agent-1", "error1"), ("agent-2", "error2")]
    _emit_human_output(result)
    captured = capsys.readouterr()
    output = captured.out
    assert "Failed to send message to 2 agent(s)" in output


# =============================================================================
# Tests for stdin '-' placeholder
# =============================================================================


def test_message_dash_reads_agent_names_from_input(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test message '-' reads agent names from stdin and reports them not found."""
    result = cli_runner.invoke(
        message,
        ["-", "-m", "hello"],
        input="nonexistent-stdin-agent-1\nnonexistent-stdin-agent-2\n",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No agents found" in result.output


# =============================================================================
# Tests for --message-file
# =============================================================================


def test_message_file_reads_content_from_file(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that --message-file reads message content from a file and sends it."""
    message_file = tmp_path / "message.txt"
    message_file.write_text("Hello from file")

    result = cli_runner.invoke(
        message,
        ["nonexistent-test-agent", "--message-file", str(message_file)],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No agents found" in result.output


def test_message_and_message_file_both_provided_raises_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that providing both --message and --message-file raises an error."""
    message_file = tmp_path / "message.txt"
    message_file.write_text("Hello from file")

    result = cli_runner.invoke(
        message,
        ["nonexistent-test-agent", "-m", "Hello from flag", "--message-file", str(message_file)],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot provide both --message and --message-file" in result.output


def test_message_file_nonexistent_file_raises_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that --message-file with a nonexistent file raises an error."""
    nonexistent_file = tmp_path / "does_not_exist.txt"

    result = cli_runner.invoke(
        message,
        ["nonexistent-test-agent", "--message-file", str(nonexistent_file)],
        obj=plugin_manager,
    )

    # click.Path(exists=True) validates the file exists before the command runs
    assert result.exit_code != 0
