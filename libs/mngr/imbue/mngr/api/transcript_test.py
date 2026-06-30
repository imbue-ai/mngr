import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from imbue.mngr.api.preservation import get_preserved_agent_dir
from imbue.mngr.api.transcript import apply_head_or_tail
from imbue.mngr.api.transcript import format_event_human
from imbue.mngr.api.transcript import get_event_role
from imbue.mngr.api.transcript import parse_transcript_events
from imbue.mngr.api.transcript import render_preserved_agent_transcript
from imbue.mngr.api.transcript import render_transcript_to_string
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.testing import capture_loguru


def _write_preserved_transcript(
    host_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    events: Sequence[dict[str, Any]],
    source: str = "claude",
) -> None:
    """Lay down a preserved common-transcript JSONL file for an agent under host_dir."""
    transcript_dir = get_preserved_agent_dir(host_dir, agent_name, agent_id) / "events" / source / "common_transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))


# =============================================================================
# get_event_role
# =============================================================================


def test_get_event_role_from_explicit_role_field() -> None:
    assert get_event_role({"role": "user"}) == "user"


def test_get_event_role_from_user_message_type() -> None:
    assert get_event_role({"type": "user_message"}) == "user"


def test_get_event_role_from_assistant_message_type() -> None:
    assert get_event_role({"type": "assistant_message"}) == "assistant"


def test_get_event_role_from_tool_result_type() -> None:
    assert get_event_role({"type": "tool_result"}) == "tool"


def test_get_event_role_returns_none_for_unknown_type() -> None:
    assert get_event_role({"type": "something_else"}) is None


def test_get_event_role_returns_none_for_empty_event() -> None:
    assert get_event_role({}) is None


# =============================================================================
# parse_transcript_events
# =============================================================================


def test_parse_transcript_events_parses_jsonl_lines() -> None:
    content = (
        json.dumps({"type": "user_message", "content": "hello"})
        + "\n"
        + json.dumps({"type": "assistant_message", "text": "hi"})
        + "\n"
    )
    events = parse_transcript_events(content, roles=(), source_description="test transcript")
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
    events = parse_transcript_events(content, roles=("user",), source_description="test transcript")
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
    events = parse_transcript_events(content, roles=("user", "tool"), source_description="test transcript")
    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "tool_result"


def test_parse_transcript_events_skips_blank_lines() -> None:
    content = "\n\n" + json.dumps({"type": "user_message", "content": "hello"}) + "\n\n"
    events = parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1


def test_parse_transcript_events_skips_malformed_json() -> None:
    content = "not json\n" + json.dumps({"type": "user_message", "content": "hello"}) + "\n"
    with capture_loguru(level="WARNING"):
        events = parse_transcript_events(content, roles=(), source_description="test transcript")
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
        events = parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 2
    assert "Skipped corrupt JSONL line" in log_output.getvalue()


def test_parse_transcript_events_silent_on_partial_last_line() -> None:
    content = json.dumps({"type": "user_message", "content": "hello"}) + "\nincomplete{"
    with capture_loguru(level="WARNING") as log_output:
        events = parse_transcript_events(content, roles=(), source_description="test transcript")
    assert len(events) == 1
    assert log_output.getvalue() == ""


# =============================================================================
# format_event_human
# =============================================================================


def test_format_event_human_user_message() -> None:
    event = {"type": "user_message", "timestamp": "2026-01-01T00:00:00.123Z", "content": "Hello world"}
    result = format_event_human(event)
    assert "[2026-01-01T00:00:00Z] user:" in result
    assert "Hello world" in result


def test_format_event_human_assistant_message_with_text() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:01.456Z",
        "text": "Here is my response",
        "tool_calls": [],
        "parts": [{"type": "text", "content": "Here is my response"}],
    }
    result = format_event_human(event)
    assert "[2026-01-01T00:00:01Z] assistant:" in result
    assert "Here is my response" in result


def test_format_event_human_assistant_message_with_tool_calls() -> None:
    event = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:02Z",
        "text": "",
        "tool_calls": [{"tool_call_id": "c1", "tool_name": "Read", "input_preview": '{"file":"test.py"}'}],
        "parts": [
            {"type": "tool_call", "tool_call_id": "c1", "tool_name": "Read", "input_preview": '{"file":"test.py"}'},
        ],
    }
    result = format_event_human(event)
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
    result = format_event_human(event)
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
    assert "[ERROR]" in format_event_human(event)


def test_format_event_human_tool_result_truncates_long_output() -> None:
    event = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:03Z",
        "tool_name": "Read",
        "output": "x" * 1000,
        "is_error": False,
    }
    result = format_event_human(event)
    assert "..." in result
    output_line = result.split("\n", 1)[1]
    assert len(output_line) <= 504


def test_format_event_human_assistant_no_content() -> None:
    event = {"type": "assistant_message", "timestamp": "2026-01-01T00:00:00Z", "text": "", "tool_calls": []}
    assert "(no content)" in format_event_human(event)


# =============================================================================
# render_transcript_to_string
# =============================================================================


def test_render_transcript_to_string_jsonl_round_trips() -> None:
    events = [{"type": "user_message", "content": "hi"}, {"type": "assistant_message", "parts": []}]
    rendered = render_transcript_to_string(events, OutputFormat.JSONL)
    lines = [line for line in rendered.split("\n") if line]
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "hi"


def test_render_transcript_to_string_json_is_a_list() -> None:
    events = [{"type": "user_message", "content": "hi"}]
    parsed = json.loads(render_transcript_to_string(events, OutputFormat.JSON))
    assert isinstance(parsed, list)
    assert parsed[0]["content"] == "hi"


def test_render_transcript_to_string_human_separates_events_with_blank_line() -> None:
    events = [
        {"type": "user_message", "timestamp": "2026-01-01T00:00:00Z", "content": "hi"},
        {"type": "user_message", "timestamp": "2026-01-01T00:00:01Z", "content": "bye"},
    ]
    rendered = render_transcript_to_string(events, OutputFormat.HUMAN)
    assert "hi" in rendered
    assert "bye" in rendered
    assert "\n\n" in rendered


def test_render_transcript_to_string_empty_human_is_empty() -> None:
    assert render_transcript_to_string([], OutputFormat.HUMAN) == ""


def test_render_transcript_to_string_empty_json_is_empty_list() -> None:
    assert json.loads(render_transcript_to_string([], OutputFormat.JSON)) == []


# =============================================================================
# apply_head_or_tail
# =============================================================================


def test_apply_head_or_tail_head_keeps_first_n() -> None:
    events = [{"i": i} for i in range(5)]
    assert apply_head_or_tail(events, head=2, tail=None) == [{"i": 0}, {"i": 1}]


def test_apply_head_or_tail_tail_keeps_last_n() -> None:
    events = [{"i": i} for i in range(5)]
    assert apply_head_or_tail(events, head=None, tail=2) == [{"i": 3}, {"i": 4}]


def test_apply_head_or_tail_neither_keeps_all() -> None:
    events = [{"i": i} for i in range(3)]
    assert apply_head_or_tail(events, head=None, tail=None) == events


# =============================================================================
# render_preserved_agent_transcript
# =============================================================================


def test_render_preserved_agent_transcript_returns_none_when_not_preserved(tmp_path: Path) -> None:
    assert render_preserved_agent_transcript(tmp_path, AgentId(), (), None, None, OutputFormat.HUMAN) is None


def test_render_preserved_agent_transcript_returns_none_when_no_transcript_file(tmp_path: Path) -> None:
    agent_id = AgentId()
    # Preserved dir exists but holds no common_transcript file.
    get_preserved_agent_dir(tmp_path, AgentName("old-agent"), agent_id).mkdir(parents=True)
    assert render_preserved_agent_transcript(tmp_path, agent_id, (), None, None, OutputFormat.JSONL) is None


def test_render_preserved_agent_transcript_renders_jsonl(tmp_path: Path) -> None:
    agent_id = AgentId()
    _write_preserved_transcript(
        tmp_path,
        AgentName("old-agent"),
        agent_id,
        [
            {"type": "user_message", "role": "user", "content": "set up auth"},
            {"type": "assistant_message", "role": "assistant", "parts": [{"type": "text", "content": "done"}]},
        ],
    )
    rendered = render_preserved_agent_transcript(tmp_path, agent_id, (), None, None, OutputFormat.JSONL)
    assert rendered is not None
    lines = [line for line in rendered.split("\n") if line]
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "set up auth"


def test_render_preserved_agent_transcript_filters_role_and_applies_head(tmp_path: Path) -> None:
    agent_id = AgentId()
    _write_preserved_transcript(
        tmp_path,
        AgentName("old-agent"),
        agent_id,
        [
            {"type": "user_message", "role": "user", "content": "first"},
            {"type": "assistant_message", "role": "assistant", "parts": []},
            {"type": "user_message", "role": "user", "content": "second"},
        ],
    )
    rendered = render_preserved_agent_transcript(tmp_path, agent_id, ("user",), 1, None, OutputFormat.JSONL)
    assert rendered is not None
    lines = [line for line in rendered.split("\n") if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["content"] == "first"
