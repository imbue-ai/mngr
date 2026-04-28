from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Iterator

from loguru import logger

from imbue.mngr.primitives import AgentId
from imbue.mngr_subagent_proxy import subagent_wait
from imbue.mngr_subagent_proxy.subagent_wait import AgentLocation
from imbue.mngr_subagent_proxy.subagent_wait import TailState
from imbue.mngr_subagent_proxy.subagent_wait import _WaitRuntime
from imbue.mngr_subagent_proxy.subagent_wait import _check_permissions_newly_waiting
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


def test_end_turn_detection_with_pure_text() -> None:
    """is_end_turn_event accepts pure-text end_turn and rejects tool_use / malformed events."""
    pure_text_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    assert is_end_turn_event(pure_text_event) is True

    tool_use_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "calling a tool"},
                {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
            ],
        },
    }
    assert is_end_turn_event(tool_use_event) is False

    tool_use_stop_reason_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "thinking"}],
        },
    }
    assert is_end_turn_event(tool_use_stop_reason_event) is False

    missing_message_event = {"type": "assistant"}
    assert is_end_turn_event(missing_message_event) is False

    non_assistant_event = {"type": "user", "message": {"stop_reason": "end_turn", "content": []}}
    assert is_end_turn_event(non_assistant_event) is False

    # stop_sequence is a real end-of-turn too; Claude Code emits it for
    # certain skill/agent integrations. Discovered live: a verify-and-fix
    # subagent finished with stop_reason=stop_sequence and our wait
    # blocked indefinitely waiting for end_turn.
    stop_sequence_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "verified"}],
        },
    }
    assert is_end_turn_event(stop_sequence_event) is True

    # max_tokens: model truncated. Surface what we have rather than hang.
    max_tokens_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "max_tokens",
            "content": [{"type": "text", "text": "long output ..."}],
        },
    }
    assert is_end_turn_event(max_tokens_event) is True

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


def test_is_real_user_event_discriminates_human_from_machine_events() -> None:
    """is_real_user_event accepts only plain-text human prompts, rejecting tool_result and hook-injected events."""
    # Not a user event.
    assistant_event = {"type": "assistant", "message": {"content": "hello"}}
    assert is_real_user_event(assistant_event) is False

    # Missing or malformed message payload.
    assert is_real_user_event({"type": "user"}) is False
    assert is_real_user_event({"type": "user", "message": "not-a-dict"}) is False

    # tool_result blocks come in as list content; must be rejected.
    tool_result_event = {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "abc", "content": "done"}],
        },
    }
    assert is_real_user_event(tool_result_event) is False

    # Hook-injected synthetic user events must be rejected, including ones with
    # leading whitespace before the prefix.
    stop_hook_event = {"type": "user", "message": {"content": "Stop hook feedback: please continue"}}
    assert is_real_user_event(stop_hook_event) is False

    pretooluse_hook_event = {"type": "user", "message": {"content": "   PreToolUse hook feedback: blocked"}}
    assert is_real_user_event(pretooluse_hook_event) is False

    # Non-str, non-list content (e.g. None) is not a real user prompt.
    null_content_event = {"type": "user", "message": {"content": None}}
    assert is_real_user_event(null_content_event) is False

    # Actual human prompt is accepted.
    human_event = {"type": "user", "message": {"content": "please refactor foo.py"}}
    assert is_real_user_event(human_event) is True


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
