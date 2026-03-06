import json
from collections.abc import Iterator

import pluggy
import pytest
from click.testing import CliRunner

import imbue.mng.cli.ask as ask_module
from imbue.mng.agents.default_plugins.headless_claude_agent import extract_text_delta as _extract_text_delta
from imbue.mng.cli.ask import ClaudeBackendInterface
from imbue.mng.cli.ask import _accumulate_chunks
from imbue.mng.cli.ask import _build_ask_context
from imbue.mng.cli.ask import _execute_response
from imbue.mng.cli.ask import _show_command_summary
from imbue.mng.cli.ask import ask
from imbue.mng.errors import MngError
from imbue.mng.primitives import OutputFormat


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
    """Test double that raises MngError on query."""

    error_message: str

    def query(self, prompt: str, system_prompt: str) -> Iterator[str]:
        raise MngError(self.error_message)


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """Provide a FakeClaude backend and monkeypatch it into the ask module."""
    backend = FakeClaude()
    monkeypatch.setattr(ask_module, "HeadlessClaudeBackend", lambda **kwargs: backend)
    return backend


def test_build_ask_context_contains_mng_docs() -> None:
    """The generated context should contain mng command documentation from the registry."""
    context = _build_ask_context()
    assert len(context) > 100
    assert "mng" in context
    assert "create" in context.lower()


def test_no_query_shows_command_summary(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """When no query is provided, shows a summary of available commands."""
    result = cli_runner.invoke(ask, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "Available mng commands" in result.output
    assert "mng ask" in result.output


def test_ask_passes_query_to_claude(
    fake_claude: FakeClaude,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """The full query (with prefix) should be passed to the claude backend."""
    fake_claude.responses.append("mng create my-agent")

    result = cli_runner.invoke(
        ask, ["how", "do", "I", "create", "an", "agent?"], obj=plugin_manager, catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "mng create my-agent" in result.output
    assert len(fake_claude.queries) == 1
    assert "how do I create an agent?" in fake_claude.queries[0]


def test_ask_json_output(
    fake_claude: FakeClaude,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    fake_claude.responses.append("mng list")

    result = cli_runner.invoke(ask, ["--format", "json", "list", "agents"], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0
    assert '"response": "mng list"' in result.output


def test_ask_jsonl_output(
    fake_claude: FakeClaude,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    fake_claude.responses.append("mng list")

    result = cli_runner.invoke(
        ask, ["--format", "jsonl", "list", "agents"], obj=plugin_manager, catch_exceptions=False
    )

    assert result.exit_code == 0
    assert '"event": "response"' in result.output
    assert '"response": "mng list"' in result.output


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
    fake_claude.responses.append("Use mng create")

    result = cli_runner.invoke(ask, ["how", "to", "create?"], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0
    assert "Use mng create" in result.output


def test_extract_text_delta_valid_event() -> None:
    """A valid content_block_delta event should return the text."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }
    )
    assert _extract_text_delta(event) == "hello"


def test_extract_text_delta_non_delta_event() -> None:
    """Non-delta events should return None."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_start", "index": 0},
        }
    )
    assert _extract_text_delta(event) is None


def test_extract_text_delta_malformed_json() -> None:
    """Malformed JSON should return None, not raise."""
    assert _extract_text_delta("not valid json {{{") is None


def test_extract_text_delta_non_stream_event() -> None:
    """Events that are not stream_event type should return None."""
    event = json.dumps({"type": "result", "subtype": "success"})
    assert _extract_text_delta(event) is None


def test_extract_text_delta_missing_delta() -> None:
    """content_block_delta without a delta field should return None."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "index": 0},
        }
    )
    assert _extract_text_delta(event) is None


def test_execute_response_raises_on_empty_response() -> None:
    with pytest.raises(MngError, match="empty response"):
        _execute_response(response="   \n  ", output_format=OutputFormat.HUMAN)


def test_execute_response_rejects_non_mng_command() -> None:
    """Commands that don't start with 'mng' should be rejected."""
    with pytest.raises(MngError, match="not a valid mng command"):
        _execute_response(response="rm -rf /", output_format=OutputFormat.HUMAN)


def test_execute_response_rejects_markdown_response() -> None:
    """Markdown-wrapped responses should be rejected."""
    with pytest.raises(MngError, match="not a valid mng command"):
        _execute_response(response="```\nmng list\n```", output_format=OutputFormat.HUMAN)


def test_execute_response_raises_on_unmatched_quotes() -> None:
    """shlex.split raises ValueError on unmatched quotes; should become MngError."""
    with pytest.raises(MngError, match="could not be parsed"):
        _execute_response(response="mng create 'unmatched", output_format=OutputFormat.HUMAN)


def test_no_query_json_output(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """No-query with JSON format should emit commands dict."""
    result = cli_runner.invoke(ask, ["--format", "json"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert '"commands"' in result.output


# =============================================================================
# Additional tests for _extract_text_delta edge cases
# =============================================================================


def test_extract_text_delta_event_not_dict() -> None:
    """_extract_text_delta should return None when event is not a dict."""
    event = json.dumps({"type": "stream_event", "event": "not_a_dict"})
    assert _extract_text_delta(event) is None


def test_extract_text_delta_non_text_delta_type() -> None:
    """_extract_text_delta should return None when delta type is not text_delta."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        }
    )
    assert _extract_text_delta(event) is None


def test_extract_text_delta_delta_not_dict() -> None:
    """_extract_text_delta should return None when delta is not a dict."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": "not_a_dict",
            },
        }
    )
    assert _extract_text_delta(event) is None


def test_extract_text_delta_text_not_string() -> None:
    """_extract_text_delta should return None when text is not a string."""
    event = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": 42},
            },
        }
    )
    assert _extract_text_delta(event) is None


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
    assert "Available mng commands" in captured.out
    assert "mng ask" in captured.out


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
