import json
from collections.abc import Iterator

import pluggy
import pytest
from click.testing import CliRunner

import imbue.mngr.cli.ask as ask_module
from imbue.mngr.agents.agent_registry import reset_agent_registry
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.cli.ask import ClaudeBackendInterface
from imbue.mngr.cli.ask import _accumulate_chunks
from imbue.mngr.cli.ask import _build_ask_context
from imbue.mngr.cli.ask import _check_headless_claude_available
from imbue.mngr.cli.ask import _execute_response
from imbue.mngr.cli.ask import _show_command_summary
from imbue.mngr.cli.ask import ask
from imbue.mngr.config.agent_class_registry import set_default_agent_class
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import OutputFormat


class FakeClaude(ClaudeBackendInterface):
    """Test double that records queries and returns canned responses."""

    responses: list[str] = []
    queries: list[str] = []
    system_prompts: list[str] = []

    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        self.queries.append(prompt)
        self.system_prompts.append(system_prompt)
        yield self.responses.pop(0)


class FakeClaudeError(ClaudeBackendInterface):
    """Test double that raises MngrError on query."""

    error_message: str

    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        raise MngrError(self.error_message)


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """Provide a FakeClaude backend and monkeypatch it into the ask module."""
    backend = FakeClaude()
    monkeypatch.setattr(ask_module, "HeadlessClaudeBackend", lambda **kwargs: backend)
    return backend


def test_build_ask_context_contains_mngr_docs() -> None:
    """The generated context should contain mngr command documentation and the mega tutorial."""
    context = _build_ask_context()
    assert len(context) > 100
    assert "mngr" in context
    assert "create" in context.lower()
    assert "Tutorial and Examples" in context
    assert "CREATING AGENTS" in context


def test_no_query_shows_command_summary(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """When no query is provided, shows a summary of available commands."""
    result = cli_runner.invoke(ask, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "Available mngr commands" in result.output
    assert "mngr ask" in result.output


def test_ask_passes_query_to_claude(
    fake_claude: FakeClaude,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """The full query (with prefix) should be passed to the claude backend."""
    fake_claude.responses.append("mngr create my-agent")

    result = cli_runner.invoke(
        ask, ["how", "do", "I", "create", "an", "agent?"], obj=plugin_manager, catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "mngr create my-agent" in result.output
    assert len(fake_claude.queries) == 1
    assert "how do I create an agent?" in fake_claude.queries[0]


def test_ask_json_output(
    fake_claude: FakeClaude,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    fake_claude.responses.append("mngr list")

    result = cli_runner.invoke(ask, ["--format", "json", "list", "agents"], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0
    assert '"response": "mngr list"' in result.output


def test_ask_jsonl_output(
    fake_claude: FakeClaude,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    fake_claude.responses.append("mngr list")

    result = cli_runner.invoke(
        ask, ["--format", "jsonl", "list", "agents"], obj=plugin_manager, catch_exceptions=False
    )

    assert result.exit_code == 0
    assert '"event": "response"' in result.output
    assert '"response": "mngr list"' in result.output


@pytest.mark.parametrize(
    "error_message, expected_substring",
    [
        ("claude --print failed (exit code 1): authentication failed", "authentication failed"),
        (
            "claude is not installed or not found in PATH. Install Claude Code: https://docs.anthropic.com/en/docs/claude-code/overview",
            "claude is not installed",
        ),
    ],
)
def test_ask_claude_error_shows_message(
    error_message: str,
    expected_substring: str,
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """When the claude backend raises an error, it should be displayed to the user."""
    backend = FakeClaudeError(error_message=error_message)
    monkeypatch.setattr(ask_module, "HeadlessClaudeBackend", lambda **kwargs: backend)

    result = cli_runner.invoke(ask, ["test"], obj=plugin_manager, catch_exceptions=True)

    assert result.exit_code != 0
    assert expected_substring in result.output


def test_ask_human_streams_output(
    fake_claude: FakeClaude,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """HUMAN format should output the streamed response text."""
    fake_claude.responses.append("Use mngr create")

    result = cli_runner.invoke(ask, ["how", "to", "create?"], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0
    assert "Use mngr create" in result.output


def test_execute_response_raises_on_empty_response() -> None:
    with pytest.raises(MngrError, match="empty response"):
        _execute_response(response="   \n  ", output_format=OutputFormat.HUMAN)


def test_execute_response_rejects_non_mngr_command() -> None:
    """Commands that don't start with 'mngr' should be rejected."""
    with pytest.raises(MngrError, match="not a valid mngr command"):
        _execute_response(response="rm -rf /", output_format=OutputFormat.HUMAN)


def test_execute_response_rejects_markdown_response() -> None:
    """Markdown-wrapped responses should be rejected."""
    with pytest.raises(MngrError, match="not a valid mngr command"):
        _execute_response(response="```\nmngr list\n```", output_format=OutputFormat.HUMAN)


def test_execute_response_raises_on_unmatched_quotes() -> None:
    """shlex.split raises ValueError on unmatched quotes; should become MngrError."""
    with pytest.raises(MngrError, match="could not be parsed"):
        _execute_response(response="mngr create 'unmatched", output_format=OutputFormat.HUMAN)


def test_no_query_json_output(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """No-query with JSON format should emit commands dict."""
    result = cli_runner.invoke(ask, ["--format", "json"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert '"commands"' in result.output


# =============================================================================
# Tests for _accumulate_chunks
# =============================================================================


def test_accumulate_chunks_joins_all_chunks() -> None:
    """_accumulate_chunks should join all chunks into a single string."""
    chunks = iter(["Hello ", "world", "!"])
    result = _accumulate_chunks(chunks)
    assert result == "Hello world!"


def test_accumulate_chunks_empty_iterator() -> None:
    """_accumulate_chunks should return empty string for empty iterator."""
    chunks = iter([])
    result = _accumulate_chunks(chunks)
    assert result == ""


def test_accumulate_chunks_single_chunk() -> None:
    """_accumulate_chunks should work with a single chunk."""
    chunks = iter(["Hello"])
    result = _accumulate_chunks(chunks)
    assert result == "Hello"


# =============================================================================
# Tests for _show_command_summary
# =============================================================================


def test_show_command_summary_human(capsys: pytest.CaptureFixture[str]) -> None:
    """_show_command_summary should output command list in HUMAN format."""
    _show_command_summary(OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "Available mngr commands" in captured.out
    assert "mngr ask" in captured.out


def test_show_command_summary_json(capsys: pytest.CaptureFixture[str]) -> None:
    """_show_command_summary should output JSON in JSON format."""
    _show_command_summary(OutputFormat.JSON)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert "commands" in data


def test_show_command_summary_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """_show_command_summary should output JSONL in JSONL format."""
    _show_command_summary(OutputFormat.JSONL)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "commands"


def test_check_headless_claude_available_raises_when_plugin_missing() -> None:
    """When the headless_claude plugin is not registered, raises with an actionable error."""
    reset_agent_registry()
    set_default_agent_class(BaseAgent)
    with pytest.raises(MngrError, match="headless_claude.*not available"):
        _check_headless_claude_available()
