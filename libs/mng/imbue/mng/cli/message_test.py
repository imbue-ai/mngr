import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.message import MessageResult
from imbue.mng.cli.message import MessageCliOptions
from imbue.mng.cli.message import _emit_human_output
from imbue.mng.cli.message import _emit_json_output
from imbue.mng.cli.message import _emit_jsonl_error
from imbue.mng.cli.message import _emit_jsonl_success
from imbue.mng.cli.message import _emit_output
from imbue.mng.cli.message import _get_message_content
from imbue.mng.cli.message import message
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


_DEFAULT_OPTS = MessageCliOptions(
    agents=(),
    agent_list=(),
    all_agents=False,
    include=(),
    exclude=(),
    stdin=False,
    message_content=None,
    on_error="continue",
    start=False,
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
        all_agents=False,
        include=("name == 'test'",),
        exclude=(),
        stdin=False,
        message_content="Hello",
        on_error="continue",
        start=False,
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
    assert opts.all_agents is False
    assert opts.message_content == "Hello"


def test_get_message_content_returns_option_when_provided() -> None:
    """Test that _get_message_content returns the option value when provided."""
    result = _get_message_content("Hello World", click.Context(click.Command("test")))
    assert result == "Hello World"


def test_emit_human_output_logs_successful_agents(capsys: pytest.CaptureFixture) -> None:
    """Test that _emit_human_output logs successful agents."""
    result = MessageResult()
    result.successful_agents = ["agent1", "agent2"]

    _emit_human_output(result)

    # The output is logged via loguru, not printed directly
    # We can't easily capture it here, but we can verify no exception is raised


def test_emit_human_output_logs_failed_agents(capsys: pytest.CaptureFixture) -> None:
    """Test that _emit_human_output logs failed agents."""
    result = MessageResult()
    result.failed_agents = [("agent1", "error1"), ("agent2", "error2")]

    _emit_human_output(result)

    # The output is logged via loguru


def test_emit_human_output_handles_no_agents() -> None:
    """Test that _emit_human_output handles no agents case."""
    result = MessageResult()

    # Should not raise
    _emit_human_output(result)


def test_emit_json_output_formats_successful_agents(capsys: pytest.CaptureFixture) -> None:
    """Test that _emit_json_output includes successful agents."""
    result = MessageResult()
    result.successful_agents = ["agent1", "agent2"]

    _emit_json_output(result)

    captured = capsys.readouterr()
    assert '"successful_agents": ["agent1", "agent2"]' in captured.out


def test_emit_json_output_formats_failed_agents(capsys: pytest.CaptureFixture) -> None:
    """Test that _emit_json_output includes failed agents."""
    result = MessageResult()
    result.failed_agents = [("agent1", "error message")]

    _emit_json_output(result)

    captured = capsys.readouterr()
    assert '"failed_agents"' in captured.out
    assert '"agent": "agent1"' in captured.out
    assert '"error": "error message"' in captured.out


def test_emit_json_output_includes_counts(capsys: pytest.CaptureFixture) -> None:
    """Test that _emit_json_output includes counts."""
    result = MessageResult()
    result.successful_agents = ["agent1", "agent2", "agent3"]
    result.failed_agents = [("agent4", "error")]

    _emit_json_output(result)

    captured = capsys.readouterr()
    assert '"total_sent": 3' in captured.out
    assert '"total_failed": 1' in captured.out


def test_message_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that message requires at least one agent, --all, or --include."""
    result = cli_runner.invoke(
        message,
        ["-m", "hello"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


def test_message_cannot_combine_agents_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --all cannot be combined with agent names."""
    result = cli_runner.invoke(
        message,
        ["my-agent", "--all", "-m", "hello"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify both agent names and --all" in result.output


def test_message_sends_nothing_with_no_matching_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that message --all with no agents reports no agents found."""
    result = cli_runner.invoke(
        message,
        ["--all", "-m", "hello"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No agents found to send message to" in result.output


def test_message_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that message --help works and exits 0."""
    result = cli_runner.invoke(
        message,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "message" in result.output.lower()


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


def test_message_all_json_format_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test message --all --format json with no agents."""
    result = cli_runner.invoke(
        message,
        ["--all", "-m", "hello", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


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


def test_emit_output_human_dispatches() -> None:
    """Test _emit_output dispatches to human output handler."""
    result = MessageResult()
    result.successful_agents = ["agent-1"]
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with _capture_stdout() as buf:
        _emit_output(result, output_opts)
    assert "Message sent to: agent-1" in buf.getvalue()


def test_emit_output_json_dispatches() -> None:
    """Test _emit_output dispatches to JSON output handler."""
    result = MessageResult()
    result.successful_agents = ["agent-1"]
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    with _capture_stdout() as buf:
        _emit_output(result, output_opts)
    data = json.loads(buf.getvalue().strip())
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


def test_emit_human_output_successful_agents_with_count() -> None:
    """Test _emit_human_output shows success count."""
    result = MessageResult()
    result.successful_agents = ["agent-1", "agent-2", "agent-3"]
    with _capture_stdout() as buf:
        _emit_human_output(result)
    output = buf.getvalue()
    assert "Message sent to: agent-1" in output
    assert "Message sent to: agent-2" in output
    assert "Message sent to: agent-3" in output
    assert "Successfully sent message to 3 agent(s)" in output


def test_emit_human_output_only_failed_agents() -> None:
    """Test _emit_human_output handles case with only failures."""
    result = MessageResult()
    result.failed_agents = [("agent-1", "error1"), ("agent-2", "error2")]
    with _capture_stdout() as buf:
        _emit_human_output(result)
    output = buf.getvalue()
    assert "Failed to send message to 2 agent(s)" in output
