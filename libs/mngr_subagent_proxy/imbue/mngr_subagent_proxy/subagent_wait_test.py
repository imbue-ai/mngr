from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Iterator

import pytest
from loguru import logger

from imbue.mngr.primitives import AgentId
from imbue.mngr_subagent_proxy import subagent_wait
from imbue.mngr_subagent_proxy.subagent_wait import AgentLocation
from imbue.mngr_subagent_proxy.subagent_wait import TailState
from imbue.mngr_subagent_proxy.subagent_wait import _WaitRuntime
from imbue.mngr_subagent_proxy.subagent_wait import _check_permissions_newly_waiting
from imbue.mngr_subagent_proxy.subagent_wait import _delete_watermark_file
from imbue.mngr_subagent_proxy.subagent_wait import _read_watermark_file
from imbue.mngr_subagent_proxy.subagent_wait import _write_watermark_file
from imbue.mngr_subagent_proxy.subagent_wait import extract_assistant_text
from imbue.mngr_subagent_proxy.subagent_wait import is_end_turn_event
from imbue.mngr_subagent_proxy.subagent_wait import is_real_user_event
from imbue.mngr_subagent_proxy.subagent_wait import read_new_jsonl_lines
from imbue.mngr_subagent_proxy.subagent_wait import resolve_destroyed_result
from imbue.mngr_subagent_proxy.subagent_wait import truncate_result_text


@contextmanager
def _capture_loguru_messages() -> Iterator[list[str]]:
    """Install a loguru sink that appends formatted messages to a list."""
    captured: list[str] = []

    def sink(message: Any) -> None:
        captured.append(message.record["message"])

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(handler_id)


# is_end_turn_event must accept pure-text terminal events (end_turn,
# stop_sequence, max_tokens) and reject tool-call / malformed events.
# stop_sequence-as-terminal was discovered live: a verify-and-fix
# subagent finished with stop_reason=stop_sequence and our wait
# blocked indefinitely waiting for end_turn. max_tokens means the model
# truncated; surface what we have rather than hang.
_END_TURN_DETECTION_CASES: tuple[tuple[str, dict[str, Any], bool], ...] = (
    (
        "pure_text_end_turn",
        {
            "type": "assistant",
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "hello"}]},
        },
        True,
    ),
    (
        "tool_use_block_present",
        {
            "type": "assistant",
            "message": {
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "calling a tool"},
                    {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
                ],
            },
        },
        False,
    ),
    (
        "stop_reason_tool_use",
        {
            "type": "assistant",
            "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "thinking"}]},
        },
        False,
    ),
    ("missing_message", {"type": "assistant"}, False),
    (
        "non_assistant_role",
        {"type": "user", "message": {"stop_reason": "end_turn", "content": []}},
        False,
    ),
    (
        "stop_sequence_terminal",
        {
            "type": "assistant",
            "message": {"stop_reason": "stop_sequence", "content": [{"type": "text", "text": "verified"}]},
        },
        True,
    ),
    (
        "max_tokens_terminal",
        {
            "type": "assistant",
            "message": {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "long output ..."}]},
        },
        True,
    ),
)


@pytest.mark.parametrize(
    ("event", "expected"),
    [(case[1], case[2]) for case in _END_TURN_DETECTION_CASES],
    ids=[case[0] for case in _END_TURN_DETECTION_CASES],
)
def test_is_end_turn_event(event: dict[str, Any], expected: bool) -> None:
    assert is_end_turn_event(event) is expected


def test_extract_assistant_text_concatenates_text_blocks_only() -> None:
    """extract_assistant_text joins text blocks and ignores non-text blocks and non-dict entries."""
    multi_text_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "thinking", "thinking": "internal state"},
                {"type": "text", "text": "world"},
                "not-a-dict",
            ],
        },
    }
    assert extract_assistant_text(multi_text_event) == "hello world"


# is_real_user_event must accept only plain-text human prompts and reject
# tool_result blocks plus the synthetic hook-feedback messages Claude
# Code emits as type=user. Hook-feedback prefixes are matched after a
# left-strip, so leading whitespace must not bypass the filter.
_REAL_USER_EVENT_CASES: tuple[tuple[str, dict[str, Any], bool], ...] = (
    ("non_user_role", {"type": "assistant", "message": {"content": "hello"}}, False),
    ("missing_message", {"type": "user"}, False),
    ("non_dict_message", {"type": "user", "message": "not-a-dict"}, False),
    (
        "tool_result_list_content",
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "abc", "content": "done"}]},
        },
        False,
    ),
    (
        "stop_hook_feedback",
        {"type": "user", "message": {"content": "Stop hook feedback: please continue"}},
        False,
    ),
    (
        "pretooluse_hook_feedback_with_leading_whitespace",
        {"type": "user", "message": {"content": "   PreToolUse hook feedback: blocked"}},
        False,
    ),
    ("null_content", {"type": "user", "message": {"content": None}}, False),
    (
        "real_human_prompt",
        {"type": "user", "message": {"content": "please refactor foo.py"}},
        True,
    ),
)


@pytest.mark.parametrize(
    ("event", "expected"),
    [(case[1], case[2]) for case in _REAL_USER_EVENT_CASES],
    ids=[case[0] for case in _REAL_USER_EVENT_CASES],
)
def test_is_real_user_event(event: dict[str, Any], expected: bool) -> None:
    assert is_real_user_event(event) is expected


def test_jsonl_tail_handles_partial_lines(tmp_path: Path) -> None:
    """read_new_jsonl_lines parses complete lines, buffers partials, logs on malformed, resets on truncation."""
    transcript = tmp_path / "transcript.jsonl"
    state = TailState(path=transcript, offset=0)

    first_complete = json.dumps({"type": "assistant", "n": 1}) + "\n"
    partial = json.dumps({"type": "assistant", "n": 2})
    transcript.write_bytes((first_complete + partial).encode("utf-8"))

    parsed = read_new_jsonl_lines(state)
    assert len(parsed) == 1
    assert parsed[0] == {"type": "assistant", "n": 1}
    assert state.pending_buffer == partial

    remainder = "\n" + json.dumps({"type": "assistant", "n": 3}) + "\n"
    with transcript.open("ab") as handle:
        handle.write(remainder.encode("utf-8"))

    parsed = read_new_jsonl_lines(state)
    assert len(parsed) == 2
    assert parsed[0] == {"type": "assistant", "n": 2}
    assert parsed[1] == {"type": "assistant", "n": 3}
    assert state.pending_buffer == ""

    with _capture_loguru_messages() as captured:
        with transcript.open("ab") as handle:
            handle.write(b"this is not json\n")
            handle.write((json.dumps({"type": "assistant", "n": 4}) + "\n").encode("utf-8"))
        parsed = read_new_jsonl_lines(state)

    assert len(parsed) == 1
    assert parsed[0] == {"type": "assistant", "n": 4}
    assert any("Malformed JSONL line" in msg for msg in captured)

    short_content = json.dumps({"type": "assistant", "n": 99}) + "\n"
    transcript.write_bytes(short_content.encode("utf-8"))
    parsed = read_new_jsonl_lines(state)
    assert state.offset == len(short_content)
    assert parsed == [{"type": "assistant", "n": 99}]


def test_result_truncation() -> None:
    """truncate_result_text preserves short text, truncates long text, and clips budget safely."""
    short_text = "a" * 100
    assert truncate_result_text(short_text, max_chars=200) == short_text

    long_text = "a" * 500
    result = truncate_result_text(long_text, max_chars=200)
    assert len(result) == 200
    assert result.endswith("\n\n[truncated]")
    assert result.startswith("a")

    # When max_chars is smaller than the truncation suffix, the function clips
    # budget to 0 and returns just the suffix. Accepted behavior: we document
    # that the result may exceed max_chars rather than crashing.
    tiny_result = truncate_result_text(long_text, max_chars=10)
    assert tiny_result == "\n\n[truncated]"
    assert len(tiny_result) == 13


def test_destroyed_fallback_from_preserved_sessions(tmp_path: Path) -> None:
    """resolve_destroyed_result returns the last assistant_message text from preserved events."""
    host_dir = tmp_path / "fake_host_dir"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent_id = AgentId.generate()
    target_name = "reviewer"
    location = AgentLocation(host_dir=host_dir, agent_id=agent_id, work_dir=work_dir)

    events_dir = (
        host_dir / "plugin" / "mngr_claude" / "preserved_sessions" / f"{target_name}--{agent_id}" / "common_transcript"
    )
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    lines = [
        json.dumps({"type": "assistant_message", "text": "first"}),
        json.dumps({"type": "user_message", "text": "ignored"}),
        json.dumps({"type": "assistant_message", "text": "last answer"}),
        "this is not valid json",
    ]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert (
        resolve_destroyed_result(target_name, location)
        == "[ERROR] mngr subagent destroyed before completion: last answer"
    )

    # Missing preserved-events file returns the prefix with an empty last_text.
    missing_agent_id = AgentId.generate()
    missing_location = AgentLocation(host_dir=host_dir, agent_id=missing_agent_id, work_dir=work_dir)
    assert (
        resolve_destroyed_result(target_name, missing_location)
        == "[ERROR] mngr subagent destroyed before completion: "
    )


def test_watermark_sidefile_roundtrip(tmp_path: Path) -> None:
    """Watermark sidefile read/write/delete helpers handle the sidefile
    that holds the dedup-watermark between subagent_wait invocations.
    Haiku never touches this file -- it's owned entirely by the python
    module so the proxy prompt can stay simple.
    """
    path = tmp_path / "nested" / "watermark"

    # Missing file reads as 0 (initial state, no prior PERMISSION_REQUIRED).
    assert _read_watermark_file(path) == 0

    # Write creates parent dirs and stores an integer round-trippable as text.
    _write_watermark_file(path, 4242)
    assert path.is_file()
    assert _read_watermark_file(path) == 4242

    # Garbage content reads as 0 (defensive: don't crash on corruption).
    path.write_text("not a number")
    assert _read_watermark_file(path) == 0

    # Delete is idempotent; second call on a missing file is a no-op.
    _write_watermark_file(path, 7)
    _delete_watermark_file(path)
    assert not path.exists()
    _delete_watermark_file(path)


def test_permission_gate_suppresses_until_transcript_advances(tmp_path: Path) -> None:
    """The transcript watermark suppresses re-firing of PERMISSION_REQUIRED
    on the SAME pending dialog. Once the target's transcript has grown
    past the watermark, a subsequent flag transition is treated as a
    genuinely new event and surfaced again.

    Directly exercises the helper to avoid spinning up the full poll
    loop in a unit test.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    state_dir = host_dir / "agents" / "agent-test"
    state_dir.mkdir(parents=True)
    location = AgentLocation(host_dir=host_dir, agent_id="agent-test", work_dir=work_dir)

    transcript_path = location.claude_projects_dir
    transcript_path.mkdir(parents=True)
    transcript_file = transcript_path / "session.jsonl"
    transcript_file.write_bytes(b"x" * 100)

    runtime = _WaitRuntime(
        target_name="target",
        location=location,
        permissions_previously_waiting=False,
        permission_gate_until_transcript_past=100,
    )
    runtime.tail_state.path = transcript_file

    # Flag goes from absent to present, but transcript size (100) is not
    # past the watermark (100) -- gate stays closed.
    location.permissions_waiting_file.touch()
    assert _check_permissions_newly_waiting(runtime) is False
    assert runtime.permissions_previously_waiting is True

    # Clear the flag, then re-set it -- still not past the watermark, so
    # still suppressed even though there's a fresh transition.
    location.permissions_waiting_file.unlink()
    runtime.permissions_previously_waiting = False
    location.permissions_waiting_file.touch()
    assert _check_permissions_newly_waiting(runtime) is False

    # Transcript advances past the watermark; clear-then-set is now a
    # genuinely new permission event.
    transcript_file.write_bytes(b"x" * 200)
    location.permissions_waiting_file.unlink()
    runtime.permissions_previously_waiting = False
    location.permissions_waiting_file.touch()
    assert _check_permissions_newly_waiting(runtime) is True


def test_target_presence_recheck_is_rate_limited() -> None:
    """The wait loop's `mngr list` calls for target-presence checks must be
    rate-limited via _TARGET_PRESENCE_RECHECK_SECONDS so the 5x/s polling
    cadence does not flood the host with concurrent `mngr list` runs.

    Found live: a nested verify-and-fix subagent stalled when many parent
    agents made `mngr list` slow. The wait loop fired its disappearance
    check on every poll iteration (every 200ms), each call timing out
    after 30s, queueing up faster than they finished and starving the
    rest of the loop.
    """
    # The rate-limit interval must be substantially larger than the poll
    # interval; otherwise the rate-limiting is effectively a no-op.
    assert subagent_wait._TARGET_PRESENCE_RECHECK_SECONDS > subagent_wait._POLL_INTERVAL_SECONDS * 5
    # The interval must also be at least a few seconds in absolute terms,
    # since a single mngr-list call commonly takes >1s on busy hosts.
    assert subagent_wait._TARGET_PRESENCE_RECHECK_SECONDS >= 2.0
