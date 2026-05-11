import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.testing import create_agent_with_events_dir
from imbue.mngr.cli.testing import create_agent_with_sample_transcript
from imbue.mngr.cli.transcript import TranscriptCliOptions
from imbue.mngr.cli.transcript import _format_event_human
from imbue.mngr.cli.transcript import _get_event_role
from imbue.mngr.cli.transcript import _parse_transcript_events
from imbue.mngr.cli.transcript import _resolve_target_identifier
from imbue.mngr.cli.transcript import _resolve_turn_index
from imbue.mngr.cli.transcript import _user_message_indices
from imbue.mngr.cli.transcript import transcript
from imbue.mngr.errors import UserInputError
from imbue.mngr.utils.testing import capture_loguru


def _make_transcript_opts(
    target: str | None = "my-agent",
    role: tuple[str, ...] = (),
    tail: int | None = None,
    head: int | None = None,
    turn: int | None = None,
    last_completed_turn: bool = False,
    count_turns: bool = False,
    list_turns: bool = False,
) -> TranscriptCliOptions:
    return TranscriptCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
        target=target,
        role=role,
        tail=tail,
        head=head,
        turn=turn,
        last_completed_turn=last_completed_turn,
        count_turns=count_turns,
        list_turns=list_turns,
    )


def _make_numbered_user_assistant_events(num_turns: int) -> list[dict[str, str | list | bool]]:
    """Build a transcript with N turns; each turn is a user_message + assistant_message."""
    events: list[dict[str, str | list | bool]] = []
    for i in range(num_turns):
        events.append(
            {
                "timestamp": f"2026-01-01T00:00:{i * 2:02d}Z",
                "type": "user_message",
                "event_id": f"u{i}",
                "source": "claude/common_transcript",
                "role": "user",
                "content": f"prompt-{i}",
            }
        )
        events.append(
            {
                "timestamp": f"2026-01-01T00:00:{i * 2 + 1:02d}Z",
                "type": "assistant_message",
                "event_id": f"a{i}",
                "source": "claude/common_transcript",
                "role": "assistant",
                "text": f"reply-{i}",
                "tool_calls": [],
                "model": "test-model",
            }
        )
    return events


# =============================================================================
# TranscriptCliOptions tests
# =============================================================================


def test_transcript_cli_options_can_be_constructed() -> None:
    opts = _make_transcript_opts()
    assert opts.target == "my-agent"
    assert opts.role == ()
    assert opts.tail is None
    assert opts.head is None


def test_transcript_cli_options_with_roles() -> None:
    opts = _make_transcript_opts(role=("user", "assistant"))
    assert opts.role == ("user", "assistant")


def test_transcript_cli_options_with_tail() -> None:
    opts = _make_transcript_opts(tail=10)
    assert opts.tail == 10


def test_transcript_cli_options_with_head() -> None:
    opts = _make_transcript_opts(head=5)
    assert opts.head == 5


# =============================================================================
# _get_event_role tests
# =============================================================================


def test_get_event_role_from_explicit_role_field() -> None:
    assert _get_event_role({"role": "user"}) == "user"


def test_get_event_role_from_user_message_type() -> None:
    assert _get_event_role({"type": "user_message"}) == "user"


def test_get_event_role_from_assistant_message_type() -> None:
    assert _get_event_role({"type": "assistant_message"}) == "assistant"


def test_get_event_role_from_tool_result_type() -> None:
    assert _get_event_role({"type": "tool_result"}) == "tool"


def test_get_event_role_returns_none_for_unknown_type() -> None:
    assert _get_event_role({"type": "something_else"}) is None


def test_get_event_role_returns_none_for_empty_event() -> None:
    assert _get_event_role({}) is None


# =============================================================================
# _parse_transcript_events tests
# =============================================================================


def test_parse_transcript_events_parses_jsonl_lines() -> None:
    content = (
        json.dumps({"type": "user_message", "content": "hello"})
        + "\n"
        + json.dumps({"type": "assistant_message", "text": "hi"})
        + "\n"
    )
    events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "assistant_message"


def test_parse_transcript_events_filters_by_role() -> None:
    content = (
        json.dumps({"type": "user_message", "role": "user", "content": "hello"})
        + "\n"
        + json.dumps({"type": "assistant_message", "role": "assistant", "text": "hi"})
        + "\n"
        + json.dumps({"type": "tool_result", "tool_name": "Bash", "output": "ok"})
        + "\n"
    )
    events = _parse_transcript_events(content, roles=("user",), source_description="test transcript")
    assert len(events) == 1
    assert events[0]["type"] == "user_message"


def test_parse_transcript_events_filters_multiple_roles() -> None:
    content = (
        json.dumps({"type": "user_message", "role": "user", "content": "hello"})
        + "\n"
        + json.dumps({"type": "assistant_message", "role": "assistant", "text": "hi"})
        + "\n"
        + json.dumps({"type": "tool_result", "tool_name": "Bash", "output": "ok"})
        + "\n"
    )
    events = _parse_transcript_events(content, roles=("user", "tool"), source_description="test transcript")
    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "tool_result"


def test_parse_transcript_events_skips_blank_lines() -> None:
    content = "\n\n" + json.dumps({"type": "user_message", "content": "hello"}) + "\n\n"
    events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1


def test_parse_transcript_events_skips_malformed_json() -> None:
    content = "not json\n" + json.dumps({"type": "user_message", "content": "hello"}) + "\n"
    # Mid-file malformed lines now emit a logger.warning; absorb it so it doesn't
    # leak to uncaptured output. The dedicated mid-file warning test asserts on it.
    with capture_loguru(level="WARNING"):
        events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1


def test_parse_transcript_events_warns_on_mid_file_corruption() -> None:
    content = (
        json.dumps({"type": "user_message", "content": "hello"})
        + "\n"
        + "this is not json {{{\n"
        + json.dumps({"type": "assistant_message", "text": "hi"})
        + "\n"
    )
    with capture_loguru(level="WARNING") as log_output:
        events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 2
    assert "Skipped corrupt JSONL line" in log_output.getvalue()


def test_parse_transcript_events_silent_on_partial_last_line() -> None:
    content = json.dumps({"type": "user_message", "content": "hello"}) + "\nincomplete{"
    with capture_loguru(level="WARNING") as log_output:
        events = _parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1
    assert log_output.getvalue() == ""


# =============================================================================
# _format_event_human tests
# =============================================================================


def test_format_event_human_user_message() -> None:
    event = {
        "type": "user_message",
        "timestamp": "2026-01-01T00:00:00.123Z",
        "content": "Hello world",
    }
    result = _format_event_human(event)
    assert "[2026-01-01T00:00:00Z] user:" in result
    assert "Hello world" in result


def test_format_event_human_assistant_message_with_text() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:01.456Z",
        "text": "Here is my response",
        "tool_calls": [],
    }
    result = _format_event_human(event)
    assert "[2026-01-01T00:00:01Z] assistant:" in result
    assert "Here is my response" in result


def test_format_event_human_assistant_message_with_tool_calls() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:02Z",
        "text": "",
        "tool_calls": [
            {"tool_name": "Read", "input_preview": '{"file":"test.py"}'},
        ],
    }
    result = _format_event_human(event)
    assert "assistant:" in result
    assert "-> Read(" in result


def test_format_event_human_tool_result() -> None:
    event = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:03Z",
        "tool_name": "Bash",
        "output": "command output here",
        "is_error": False,
    }
    result = _format_event_human(event)
    assert "tool (Bash):" in result
    assert "command output here" in result
    assert "[ERROR]" not in result


def test_format_event_human_tool_result_error() -> None:
    event = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:03Z",
        "tool_name": "Bash",
        "output": "failed",
        "is_error": True,
    }
    result = _format_event_human(event)
    assert "[ERROR]" in result


def test_format_event_human_tool_result_truncates_long_output() -> None:
    event = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:03Z",
        "tool_name": "Read",
        "output": "x" * 1000,
        "is_error": False,
    }
    result = _format_event_human(event)
    assert "..." in result
    # Output should be truncated (500 chars + "...")
    output_line = result.split("\n", 1)[1]
    assert len(output_line) <= 504


def test_format_event_human_assistant_no_content() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:00Z",
        "text": "",
        "tool_calls": [],
    }
    result = _format_event_human(event)
    assert "(no content)" in result


# =============================================================================
# CLI validation tests
# =============================================================================


def test_transcript_cli_rejects_head_and_tail_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    result = cli_runner.invoke(
        transcript,
        ["my-agent", "--head", "5", "--tail", "10"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot specify both --head and --tail" in result.output


# =============================================================================
# Integration tests with real agent data
# =============================================================================


def test_transcript_cli_reads_and_displays_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-human-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-human-test"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "Hello" in result.output
    assert "World" in result.output
    assert "user:" in result.output
    assert "assistant:" in result.output


def test_transcript_cli_reads_jsonl_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-jsonl-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-jsonl-test", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 3
    parsed = json.loads(lines[0])
    assert parsed["type"] == "user_message"
    assert parsed["content"] == "Hello"


def test_transcript_cli_reads_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-json-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-json-test", "--format", "json"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert len(parsed) == 3


def test_transcript_cli_filters_by_role(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-role-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-role-test", "--role", "user", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "user_message"


def test_transcript_cli_filters_by_multiple_roles(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="transcript-multirole-test")

    result = cli_runner.invoke(
        transcript,
        ["transcript-multirole-test", "--role", "user", "--role", "assistant", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2


def test_transcript_cli_applies_tail(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    numbered_events = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "user_message",
            "event_id": f"e{i}",
            "source": "claude/common_transcript",
            "role": "user",
            "content": f"msg-{i}",
        }
        for i in range(5)
    ]
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-tail-test", events=numbered_events
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-tail-test", "--tail", "2", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "msg-3"
    assert json.loads(lines[1])["content"] == "msg-4"


def test_transcript_cli_applies_head(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    numbered_events = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "user_message",
            "event_id": f"e{i}",
            "source": "claude/common_transcript",
            "role": "user",
            "content": f"msg-{i}",
        }
        for i in range(5)
    ]
    create_agent_with_sample_transcript(
        local_provider.host_dir, agent_name="transcript-head-test", events=numbered_events
    )

    result = cli_runner.invoke(
        transcript,
        ["transcript-head-test", "--head", "2", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "msg-0"
    assert json.loads(lines[1])["content"] == "msg-1"


def test_transcript_cli_no_transcript_gives_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """An agent with no common_transcript source should give a clear error."""
    create_agent_with_events_dir(local_provider.host_dir, agent_name="no-transcript-agent")

    result = cli_runner.invoke(
        transcript,
        ["no-transcript-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "No common transcript found" in result.output


# =============================================================================
# Turn-helper unit tests
# =============================================================================


def test_user_message_indices_picks_up_user_messages_only() -> None:
    events = _make_numbered_user_assistant_events(3)
    assert _user_message_indices(events) == [0, 2, 4]


def test_user_message_indices_ignores_meta_tool_results() -> None:
    """Stop-hook injections are reclassified to tool_result(tool_name='meta') upstream.

    They must not affect turn counts.
    """
    events: list[dict] = [
        {"type": "user_message", "content": "real prompt", "event_id": "u1"},
        {"type": "assistant_message", "text": "ok", "event_id": "a1"},
        {"type": "tool_result", "tool_name": "meta", "output": "Stop hook feedback...", "event_id": "m1"},
        {"type": "user_message", "content": "second prompt", "event_id": "u2"},
        {"type": "assistant_message", "text": "ok", "event_id": "a2"},
    ]
    assert _user_message_indices(events) == [0, 3]


def test_resolve_turn_index_positive_one_indexed() -> None:
    assert _resolve_turn_index(1, 4) == 0
    assert _resolve_turn_index(4, 4) == 3


def test_resolve_turn_index_negative_python_style() -> None:
    assert _resolve_turn_index(-1, 4) == 3
    assert _resolve_turn_index(-4, 4) == 0


def test_resolve_turn_index_rejects_zero() -> None:
    with pytest.raises(UserInputError, match="1-indexed"):
        _resolve_turn_index(0, 4)


def test_resolve_turn_index_rejects_out_of_range_positive() -> None:
    with pytest.raises(UserInputError, match="out of range"):
        _resolve_turn_index(5, 4)


def test_resolve_turn_index_rejects_out_of_range_negative() -> None:
    with pytest.raises(UserInputError, match="out of range"):
        _resolve_turn_index(-5, 4)


def test_resolve_turn_index_empty_transcript() -> None:
    with pytest.raises(UserInputError, match="no turns"):
        _resolve_turn_index(1, 0)


# =============================================================================
# Auto-discovery (MNGR_AGENT_ID) unit tests
# =============================================================================


def test_resolve_target_identifier_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "from-env")
    assert _resolve_target_identifier("explicit-target") == "explicit-target"


def test_resolve_target_identifier_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "from-env")
    assert _resolve_target_identifier(None) == "from-env"


def test_resolve_target_identifier_treats_empty_string_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_ID", "from-env")
    assert _resolve_target_identifier("") == "from-env"


def test_resolve_target_identifier_errors_when_neither_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    with pytest.raises(UserInputError, match="MNGR_AGENT_ID"):
        _resolve_target_identifier(None)


# =============================================================================
# CLI: mutual exclusivity
# =============================================================================


@pytest.mark.parametrize(
    "flags",
    [
        ["--turn", "1", "--last-completed-turn"],
        ["--turn", "1", "--count-turns"],
        ["--turn", "1", "--list-turns"],
        ["--last-completed-turn", "--count-turns"],
        ["--last-completed-turn", "--list-turns"],
        ["--count-turns", "--list-turns"],
    ],
)
def test_transcript_cli_rejects_two_turn_flags_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    flags: list[str],
) -> None:
    result = cli_runner.invoke(transcript, ["my-agent", *flags], obj=plugin_manager)
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


@pytest.mark.parametrize(
    "flags",
    [
        ["--turn", "1", "--head", "5"],
        ["--turn", "1", "--tail", "5"],
        ["--last-completed-turn", "--head", "5"],
        ["--count-turns", "--tail", "5"],
        ["--list-turns", "--head", "5"],
    ],
)
def test_transcript_cli_rejects_turn_flag_combined_with_head_or_tail(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    flags: list[str],
) -> None:
    result = cli_runner.invoke(transcript, ["my-agent", *flags], obj=plugin_manager)
    assert result.exit_code != 0
    assert "Cannot combine" in result.output


# =============================================================================
# CLI: --count-turns / --list-turns / --turn integration
# =============================================================================


def test_transcript_cli_count_turns_returns_integer(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(3)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="count-turns-test", events=events)

    result = cli_runner.invoke(transcript, ["count-turns-test", "--count-turns"], obj=plugin_manager)
    assert result.exit_code == 0
    assert result.output.strip() == "3"


def test_transcript_cli_count_turns_ignores_meta_tool_results(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """Meta-reclassified stop-hook events must not be counted as turns."""
    events: list[dict] = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "user_message",
            "event_id": "u1",
            "source": "claude/common_transcript",
            "role": "user",
            "content": "first prompt",
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "tool_result",
            "event_id": "meta1",
            "source": "claude/common_transcript",
            "tool_name": "meta",
            "tool_call_id": "meta-xyz",
            "output": "Stop hook feedback...",
            "is_error": False,
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "user_message",
            "event_id": "u2",
            "source": "claude/common_transcript",
            "role": "user",
            "content": "second prompt",
        },
    ]
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="count-meta-test", events=events)

    result = cli_runner.invoke(transcript, ["count-meta-test", "--count-turns"], obj=plugin_manager)
    assert result.exit_code == 0
    assert result.output.strip() == "2"


def test_transcript_cli_count_turns_empty_transcript_is_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="count-empty-test", events=[])
    result = cli_runner.invoke(transcript, ["count-empty-test", "--count-turns"], obj=plugin_manager)
    assert result.exit_code == 0
    assert result.output.strip() == "0"


def test_transcript_cli_list_turns_jsonl(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(2)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="list-turns-jsonl-test", events=events)

    result = cli_runner.invoke(
        transcript, ["list-turns-jsonl-test", "--list-turns", "--format", "jsonl"], obj=plugin_manager
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "turn": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "u0",
        "content_preview": "prompt-0",
    }
    second = json.loads(lines[1])
    assert second["turn"] == 2
    assert second["event_id"] == "u1"


def test_transcript_cli_list_turns_human_includes_header(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(2)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="list-turns-human-test", events=events)

    result = cli_runner.invoke(transcript, ["list-turns-human-test", "--list-turns"], obj=plugin_manager)
    assert result.exit_code == 0
    assert "preview" in result.output
    assert "prompt-0" in result.output
    assert "prompt-1" in result.output


def test_transcript_cli_list_turns_truncates_long_preview(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    long_content = "a" * 200
    events = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "user_message",
            "event_id": "u0",
            "source": "claude/common_transcript",
            "role": "user",
            "content": long_content,
        }
    ]
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="list-turns-trunc-test", events=events)

    result = cli_runner.invoke(
        transcript, ["list-turns-trunc-test", "--list-turns", "--format", "jsonl"], obj=plugin_manager
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output.strip())
    assert parsed["content_preview"].endswith("...")
    assert len(parsed["content_preview"]) == 80 + len("...")


def test_transcript_cli_turn_positive_extracts_correct_slice(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(3)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="turn-positive-test", events=events)

    result = cli_runner.invoke(
        transcript, ["turn-positive-test", "--turn", "2", "--format", "jsonl"], obj=plugin_manager
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == "u1"
    assert json.loads(lines[1])["event_id"] == "a1"


def test_transcript_cli_turn_negative_one_returns_in_progress_turn(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(3)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="turn-neg-one-test", events=events)

    result = cli_runner.invoke(
        transcript, ["turn-neg-one-test", "--turn", "-1", "--format", "jsonl"], obj=plugin_manager
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == "u2"
    assert json.loads(lines[1])["event_id"] == "a2"


def test_transcript_cli_last_completed_turn_matches_negative_two(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(3)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="last-completed-test", events=events)

    result = cli_runner.invoke(
        transcript, ["last-completed-test", "--last-completed-turn", "--format", "jsonl"], obj=plugin_manager
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == "u1"
    assert json.loads(lines[1])["event_id"] == "a1"


def test_transcript_cli_last_completed_turn_errors_with_one_user_message(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(1)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="last-completed-one-test", events=events)

    result = cli_runner.invoke(transcript, ["last-completed-one-test", "--last-completed-turn"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "No completed turn" in result.output


def test_transcript_cli_turn_out_of_range_gives_clear_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(2)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="turn-oor-test", events=events)

    result = cli_runner.invoke(transcript, ["turn-oor-test", "--turn", "5"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "out of range" in result.output
    assert "transcript has 2 turn" in result.output


def test_transcript_cli_turn_composes_with_role_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    events = _make_numbered_user_assistant_events(2)
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="turn-role-test", events=events)

    result = cli_runner.invoke(
        transcript,
        ["turn-role-test", "--turn", "1", "--role", "assistant", "--format", "jsonl"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "a0"


# =============================================================================
# CLI: auto-discovery via MNGR_AGENT_ID
# =============================================================================


def test_transcript_cli_uses_mngr_agent_id_when_target_omitted(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_agent_with_sample_transcript(local_provider.host_dir, agent_name="env-discovery-test")
    monkeypatch.setenv("MNGR_AGENT_ID", "env-discovery-test")

    result = cli_runner.invoke(transcript, ["--format", "jsonl"], obj=plugin_manager)
    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 3


def test_transcript_cli_errors_when_target_omitted_and_no_env(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)

    result = cli_runner.invoke(transcript, [], obj=plugin_manager)
    assert result.exit_code != 0
    assert "MNGR_AGENT_ID" in result.output
