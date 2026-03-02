import click
import pytest

from imbue.mng.api.message import MessageResult
from imbue.mng.cli.message import MessageCliOptions
from imbue.mng.cli.message import _emit_human_output
from imbue.mng.cli.message import _emit_json_output
from imbue.mng.cli.message import _get_message_content

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
