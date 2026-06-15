import json

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.testing import create_agent_with_events_dir
from imbue.mngr.cli.testing import create_agent_with_sample_transcript
from imbue.mngr.cli.transcript import _format_event_human
from imbue.mngr.cli.transcript import _get_event_role
from imbue.mngr.cli.transcript import _parse_transcript_events
from imbue.mngr.cli.transcript import transcript
from imbue.mngr.utils.testing import capture_loguru

# NOTE: The flag->field mappings (--role -> role, --tail -> tail, --head -> head) are
# verified through real CLI behavior in test_transcript_cli_filters_by_role,
# test_transcript_cli_applies_tail, and test_transcript_cli_applies_head below, so a
# tautological construct-and-echo test for TranscriptCliOptions is intentionally omitted.


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
    # Output is truncated to exactly the first 500 chars plus a "..." suffix (503 chars total).
    output_line = result.split("\n", 1)[1]
    assert output_line == "x" * 500 + "..."


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
    # Assert each role label is paired with the correct content (a role-label swap must fail).
    # _format_event_human renders "<role>:\n<content>" (see transcript.py:147,160,170).
    assert "user:\nHello" in result.output
    assert "assistant:\nWorld" in result.output
    # The tool_result event (tool_name "Bash", output "ok") is rendered too.
    assert "tool (Bash):\nok" in result.output


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


def test_transcript_cli_rejects_agent_type_without_mixin(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """Agent types whose class does not implement HasCommonTranscriptMixin should be rejected up front.

    The default 'generic' agent_type maps to the BaseAgent default class, which
    does not implement the mixin -- the CLI must fail with a clear error
    naming the agent and its type, rather than a misleading 'no transcript yet' message.
    """
    create_agent_with_events_dir(local_provider.host_dir, agent_name="no-transcript-agent")

    result = cli_runner.invoke(
        transcript,
        ["no-transcript-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "no-transcript-agent" in result.output
    assert "generic" in result.output
    assert "does not produce a common transcript" in result.output


def test_transcript_cli_missing_events_file_for_supporting_type_gives_clear_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """A supporting agent type with no transcript events yet still gets the 'no source' error.

    The mixin precheck passes (the type implements it), but the on-disk file is
    missing, so the original 'No common transcript found' error path runs.
    """
    create_agent_with_events_dir(local_provider.host_dir, agent_name="claude-pending-agent", agent_type="claude")

    result = cli_runner.invoke(
        transcript,
        ["claude-pending-agent"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "No common transcript found" in result.output
