"""Unit tests for transcript_watcher.py."""

import json
from pathlib import Path
from typing import Any
from typing import cast

from imbue.mng_claude_zygote.conftest import write_changelings_settings_toml
from imbue.mng_claude_zygote.resources.transcript_watcher import _convert_new_events
from imbue.mng_claude_zygote.resources.transcript_watcher import _extract_text_content
from imbue.mng_claude_zygote.resources.transcript_watcher import _has_tool_results_only
from imbue.mng_claude_zygote.resources.transcript_watcher import _load_poll_interval
from imbue.mng_claude_zygote.resources.transcript_watcher import _make_event_id

# -- _load_poll_interval tests --


def test_load_poll_interval_defaults_when_no_file(tmp_path: Path) -> None:
    assert _load_poll_interval(tmp_path) == 5


def test_load_poll_interval_reads_from_file(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "[watchers]\ntranscript_poll_interval_seconds = 20\n")
    assert _load_poll_interval(tmp_path) == 20


def test_load_poll_interval_handles_corrupt_file(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "this is not valid toml {{{")
    assert _load_poll_interval(tmp_path) == 5


# -- _extract_text_content tests --


def test_extract_text_content_with_string() -> None:
    assert _extract_text_content("Hello world") == "Hello world"


def test_extract_text_content_with_text_blocks() -> None:
    content = [
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": "world"},
    ]
    assert _extract_text_content(content) == "Hello\nworld"


def test_extract_text_content_with_empty_list() -> None:
    assert _extract_text_content([]) == ""


def test_extract_text_content_with_non_text_blocks() -> None:
    content = [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"},
    ]
    assert _extract_text_content(content) == ""


def test_extract_text_content_with_mixed_blocks() -> None:
    content = [
        {"type": "text", "text": "Here is text"},
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"},
        {"type": "text", "text": "More text"},
    ]
    assert _extract_text_content(content) == "Here is text\nMore text"


def test_extract_text_content_with_non_dict_blocks() -> None:
    content: list[dict[str, Any]] = ["raw string", {"type": "text", "text": "ok"}]  # type: ignore[list-item]
    assert _extract_text_content(content) == "ok"


def test_extract_text_content_with_non_list_non_string() -> None:
    assert _extract_text_content(cast(Any, 42)) == ""


# -- _has_tool_results_only tests --


def test_has_tool_results_only_with_string() -> None:
    assert _has_tool_results_only("Hello") is False


def test_has_tool_results_only_with_only_tool_results() -> None:
    content = [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "result2"},
    ]
    assert _has_tool_results_only(content) is True


def test_has_tool_results_only_with_text_blocks() -> None:
    content = [
        {"type": "text", "text": "Hello"},
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"},
    ]
    assert _has_tool_results_only(content) is False


def test_has_tool_results_only_with_empty_list() -> None:
    assert _has_tool_results_only([]) is True


def test_has_tool_results_only_with_string_items() -> None:
    content: list[dict[str, Any]] = ["raw string"]  # type: ignore[list-item]
    assert _has_tool_results_only(content) is False


def test_has_tool_results_only_with_non_list_non_string() -> None:
    assert _has_tool_results_only(cast(Any, 42)) is True


# -- _make_event_id tests --


def test_make_event_id_format() -> None:
    assert _make_event_id("uuid-123", "user") == "uuid-123-user"


def test_make_event_id_with_different_suffixes() -> None:
    assert _make_event_id("uuid-1", "assistant") == "uuid-1-assistant"
    assert _make_event_id("uuid-1", "tool_result-toolu_1") == "uuid-1-tool_result-toolu_1"


# -- _convert_new_events tests --


def _make_assistant_event(
    uuid: str,
    timestamp: str,
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "claude-opus-4-6",
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> str:
    content_blocks: list[dict[str, object]] = []
    if text:
        content_blocks.append({"type": "text", "text": text})
    if tool_calls:
        for tc in tool_calls:
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc.get("input", {}),
                }
            )
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "model": model,
                "content": content_blocks,
                "stop_reason": stop_reason,
                "usage": usage or {"input_tokens": 100, "output_tokens": 50},
            },
        }
    )


def _make_user_event(
    uuid: str,
    timestamp: str,
    text: str = "",
    tool_results: list[dict[str, object]] | None = None,
) -> str:
    if text and not tool_results:
        content: str | list[dict[str, object]] = text
    else:
        blocks: list[dict[str, object]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        if tool_results:
            for tr in tool_results:
                blocks.append({"type": "tool_result", **tr})
        content = blocks
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {"role": "user", "content": content},
        }
    )


def test_convert_new_events_converts_user_text_message(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    input_file.write_text(_make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="Hello") + "\n")
    count = _convert_new_events(input_file, output_file)

    assert count == 1
    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Hello"
    assert events[0]["event_id"] == "uuid-1-user"
    assert events[0]["source"] == "common_transcript"


def test_convert_new_events_converts_assistant_message(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    input_file.write_text(_make_assistant_event("uuid-2", "2026-01-01T00:00:01Z", text="Hi there!") + "\n")
    count = _convert_new_events(input_file, output_file)

    assert count == 1
    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "Hi there!"
    assert events[0]["model"] == "claude-opus-4-6"
    assert events[0]["event_id"] == "uuid-2-assistant"
    assert events[0]["stop_reason"] == "end_turn"
    assert events[0]["usage"]["input_tokens"] == 100


def test_convert_new_events_converts_tool_calls(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    input_file.write_text(
        _make_assistant_event(
            "uuid-3",
            "2026-01-01T00:00:02Z",
            tool_calls=[{"id": "toolu_1", "name": "Read", "input": {"file": "test.txt"}}],
            stop_reason="tool_use",
        )
        + "\n"
    )
    count = _convert_new_events(input_file, output_file)

    assert count == 1
    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    assert len(events[0]["tool_calls"]) == 1
    assert events[0]["tool_calls"][0]["tool_name"] == "Read"
    assert events[0]["tool_calls"][0]["tool_call_id"] == "toolu_1"


def test_convert_new_events_converts_tool_results(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    # Assistant with tool call, then user with tool result
    assistant = _make_assistant_event(
        "uuid-4",
        "2026-01-01T00:00:03Z",
        tool_calls=[{"id": "toolu_2", "name": "Bash"}],
        stop_reason="tool_use",
    )
    user = _make_user_event(
        "uuid-5",
        "2026-01-01T00:00:04Z",
        tool_results=[{"tool_use_id": "toolu_2", "content": "output text", "is_error": False}],
    )
    input_file.write_text(assistant + "\n" + user + "\n")

    count = _convert_new_events(input_file, output_file)
    assert count == 2  # assistant + tool_result

    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_call_id"] == "toolu_2"
    assert tool_results[0]["tool_name"] == "Bash"
    assert tool_results[0]["output"] == "output text"
    assert tool_results[0]["is_error"] is False


def test_convert_new_events_deduplicates_by_event_id(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    user_event = _make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="Hello")
    input_file.write_text(user_event + "\n")

    # Pre-populate output with the same event_id
    output_file.write_text(json.dumps({"event_id": "uuid-1-user", "type": "user_message", "content": "Hello"}) + "\n")

    count = _convert_new_events(input_file, output_file)
    assert count == 0


def test_convert_new_events_skips_progress_events(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    progress = json.dumps(
        {
            "type": "progress",
            "uuid": "prog-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"type": "bash_progress"},
        }
    )
    input_file.write_text(progress + "\n")

    count = _convert_new_events(input_file, output_file)
    assert count == 0


def test_convert_new_events_handles_missing_input_file(tmp_path: Path) -> None:
    input_file = tmp_path / "nonexistent.jsonl"
    output_file = tmp_path / "output.jsonl"

    count = _convert_new_events(input_file, output_file)
    assert count == 0


def test_convert_new_events_handles_malformed_json(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    valid = _make_user_event("uuid-1", "2026-01-01T00:00:00Z", text="valid")
    input_file.write_text("not json\n" + valid + "\n")

    count = _convert_new_events(input_file, output_file)
    assert count == 1


def test_convert_new_events_skips_events_without_uuid(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    no_uuid = json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "hi"}})
    input_file.write_text(no_uuid + "\n")

    count = _convert_new_events(input_file, output_file)
    assert count == 0


def test_convert_new_events_user_with_text_and_tool_results(tmp_path: Path) -> None:
    """A user message with both text and tool results should emit both."""
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    # First an assistant with a tool call
    assistant = _make_assistant_event(
        "uuid-a",
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": "toolu_3", "name": "Edit"}],
        stop_reason="tool_use",
    )
    # Then a user message with both text and tool result
    user = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-u",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Continue please"},
                    {"type": "tool_result", "tool_use_id": "toolu_3", "content": "done", "is_error": False},
                ],
            },
        }
    )
    input_file.write_text(assistant + "\n" + user + "\n")

    count = _convert_new_events(input_file, output_file)

    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    types_found = [e["type"] for e in events]
    assert "assistant_message" in types_found
    assert "user_message" in types_found
    assert "tool_result" in types_found


def test_convert_new_events_truncates_tool_input_preview(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    long_input = {"file": "x" * 500}
    input_file.write_text(
        _make_assistant_event(
            "uuid-long",
            "2026-01-01T00:00:00Z",
            tool_calls=[{"id": "toolu_long", "name": "Read", "input": long_input}],  # type: ignore[dict-item]
        )
        + "\n"
    )
    count = _convert_new_events(input_file, output_file)
    assert count == 1

    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    input_preview = events[0]["tool_calls"][0]["input_preview"]
    assert len(input_preview) <= 203  # 200 + "..."


def test_convert_new_events_truncates_long_tool_output(tmp_path: Path) -> None:
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    assistant = _make_assistant_event(
        "uuid-tr",
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": "toolu_tr", "name": "Read"}],
        stop_reason="tool_use",
    )
    long_output = "x" * 5000
    user = _make_user_event(
        "uuid-tr2",
        "2026-01-01T00:00:02Z",
        tool_results=[{"tool_use_id": "toolu_tr", "content": long_output, "is_error": False}],
    )
    input_file.write_text(assistant + "\n" + user + "\n")

    count = _convert_new_events(input_file, output_file)
    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results[0]["output"]) <= 2003  # 2000 + "..."


def test_convert_new_events_tool_result_with_list_content(tmp_path: Path) -> None:
    """Tool result content can be a list of text blocks."""
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    assistant = _make_assistant_event(
        "uuid-lc",
        "2026-01-01T00:00:01Z",
        tool_calls=[{"id": "toolu_lc", "name": "Read"}],
        stop_reason="tool_use",
    )
    user = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-lc2",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_lc",
                        "content": [{"type": "text", "text": "part 1"}, {"type": "text", "text": "part 2"}],
                        "is_error": False,
                    }
                ],
            },
        }
    )
    input_file.write_text(assistant + "\n" + user + "\n")

    count = _convert_new_events(input_file, output_file)
    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["output"] == "part 1\npart 2"


def test_convert_new_events_sorts_by_timestamp(tmp_path: Path) -> None:
    """Events should be output sorted by timestamp."""
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    # Write events out of order
    later = _make_user_event("uuid-later", "2026-01-01T00:00:02Z", text="Later")
    earlier = _make_user_event("uuid-earlier", "2026-01-01T00:00:01Z", text="Earlier")
    input_file.write_text(later + "\n" + earlier + "\n")

    count = _convert_new_events(input_file, output_file)
    assert count == 2

    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    assert events[0]["content"] == "Earlier"
    assert events[1]["content"] == "Later"


def test_convert_new_events_cache_read_and_write_tokens(tmp_path: Path) -> None:
    """Verify cache_read and cache_write tokens are captured from usage."""
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    input_file.write_text(
        _make_assistant_event(
            "uuid-cache",
            "2026-01-01T00:00:00Z",
            text="Hello",
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 20,
            },
        )
        + "\n"
    )

    count = _convert_new_events(input_file, output_file)
    assert count == 1

    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    usage = events[0]["usage"]
    assert usage["cache_read_tokens"] == 80
    assert usage["cache_write_tokens"] == 20


def test_convert_new_events_unknown_tool_name_defaults(tmp_path: Path) -> None:
    """Tool results for unknown tool_call_ids should get tool_name='unknown'."""
    input_file = tmp_path / "input.jsonl"
    output_file = tmp_path / "output.jsonl"

    # User with tool result but no preceding assistant message
    user = _make_user_event(
        "uuid-unk",
        "2026-01-01T00:00:01Z",
        tool_results=[{"tool_use_id": "toolu_unknown", "content": "result", "is_error": False}],
    )
    input_file.write_text(user + "\n")

    count = _convert_new_events(input_file, output_file)
    events = [json.loads(line) for line in output_file.read_text().strip().split("\n")]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["tool_name"] == "unknown"
