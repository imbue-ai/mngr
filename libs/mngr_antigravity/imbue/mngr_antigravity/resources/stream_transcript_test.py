"""Tests for the antigravity stream_transcript.sh raw streamer.

Exercises the streamer's core behaviors by running it with --single-pass in
a controlled filesystem layout that mimics agy's app-data dir.

Each test stages:
  - A fake $MNGR_AGENT_STATE_DIR with stub mngr_log.sh in commands/
  - A fake $ANTIGRAVITY_APP_DATA_DIR with brain/<conv_id>/.system_generated/
    logs/transcript.jsonl files for each conversation under test
  - An antigravity_conversation_ids file (written by the capture hook) listing
    the conversation UUIDs the streamer keys off to scope its watch

The streamer's contract:
  - Read the conversation-ids file, extract conversation UUIDs (one per line).
  - For each discovered conversation, tail brain/<uuid>/.system_generated/
    logs/transcript.jsonl and append every new line to
    $MNGR_AGENT_STATE_DIR/logs/antigravity_transcript/events.jsonl after
    augmenting it with an `_mngr_conv_id` field.
  - Persist a per-conversation offset under
    $MNGR_AGENT_STATE_DIR/plugin/antigravity/.transcript_offsets/<uuid>
    so the next pass picks up only new lines.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).parent / "stream_transcript.sh"

# The streamer's UUID-shape regex is intentionally strict so that a stray
# line in the ids file can't accidentally match. Use real UUID-shaped IDs in
# tests so the regex picks them up; substituting human-friendly IDs like
# "conv-A" silently drops them at the grep step.
_CONV_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CONV_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_CONV_MINE = "11111111-1111-1111-1111-111111111111"
_CONV_OTHER = "22222222-2222-2222-2222-222222222222"
_CONV_RESUMED = "33333333-3333-3333-3333-333333333333"
_CONV_MISSING = "44444444-4444-4444-4444-444444444444"


def _make_event(step_index: int, source: str, type_: str, **extra: Any) -> str:
    body: dict[str, Any] = {
        "step_index": step_index,
        "source": source,
        "type": type_,
        "status": "DONE",
        "created_at": "2026-05-21T07:00:00Z",
    }
    body.update(extra)
    return json.dumps(body)


@pytest.fixture
def env(tmp_path: Path, stub_mngr_log_sh: str) -> dict[str, Any]:
    """Build the controlled filesystem + env vars the streamer expects.

    Returns a dict with the staged paths so individual tests can write
    transcripts / log lines / read the output.
    """
    state_dir = tmp_path / "agent"
    commands = state_dir / "commands"
    commands.mkdir(parents=True)
    (commands / "mngr_log.sh").write_text(stub_mngr_log_sh)

    app_data_dir = tmp_path / "app_data"
    (app_data_dir / "brain").mkdir(parents=True)

    return {
        "state_dir": state_dir,
        "app_data_dir": app_data_dir,
        "conversation_ids_file": state_dir / "antigravity_conversation_ids",
        "raw_output_file": state_dir / "logs" / "antigravity_transcript" / "events.jsonl",
        "offset_dir": state_dir / "plugin" / "antigravity" / ".transcript_offsets",
    }


def _write_transcript(env: dict[str, Any], conv_id: str, lines: list[str]) -> Path:
    """Stage agy's per-conversation JSONL transcript at the canonical path."""
    transcript_dir = env["app_data_dir"] / "brain" / conv_id / ".system_generated" / "logs"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / "transcript.jsonl"
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return path


def _record_conversation(env: dict[str, Any], conv_id: str) -> None:
    """Record a conversation id the way capture_conversation_id.sh would.

    Appends the uuid to the per-agent conversation-ids file (one per line).
    """
    with env["conversation_ids_file"].open("a") as f:
        f.write(f"{conv_id}\n")


def _run_streamer(env: dict[str, Any]) -> None:
    """Run stream_transcript.sh in single-pass mode against the staged tree."""
    proc_env = {
        **os.environ,
        "MNGR_AGENT_STATE_DIR": str(env["state_dir"]),
        "ANTIGRAVITY_APP_DATA_DIR": str(env["app_data_dir"]),
    }
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env=proc_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Traceback" not in result.stderr, result.stderr


def _read_raw_events(env: dict[str, Any]) -> list[dict[str, Any]]:
    if not env["raw_output_file"].exists():
        return []
    events: list[dict[str, Any]] = []
    for line in env["raw_output_file"].read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


# -- Tests --


def test_streamer_with_no_conversations_produces_empty_output(env: dict[str, Any]) -> None:
    """No recorded conversation ids -> nothing to stream."""
    _run_streamer(env)
    assert _read_raw_events(env) == []


def test_streamer_copies_transcript_lines_with_conv_id_augmentation(env: dict[str, Any]) -> None:
    """Each emitted line carries `_mngr_conv_id` so downstream pairing works."""
    _write_transcript(
        env,
        _CONV_A,
        [
            _make_event(0, "USER_EXPLICIT", "USER_INPUT", content="hi"),
            _make_event(2, "MODEL", "PLANNER_RESPONSE", content="hello"),
        ],
    )
    _record_conversation(env, _CONV_A)

    _run_streamer(env)

    events = _read_raw_events(env)
    assert len(events) == 2
    assert all(e["_mngr_conv_id"] == _CONV_A for e in events)
    assert [e["step_index"] for e in events] == [0, 2]
    # All original fields are preserved verbatim.
    assert events[0]["source"] == "USER_EXPLICIT"
    assert events[1]["content"] == "hello"


def test_streamer_filters_conversations_to_those_in_the_ids_file(env: dict[str, Any]) -> None:
    """A transcript whose UUID is not in the ids file (e.g. created by another agent) is not streamed."""
    _write_transcript(env, _CONV_MINE, [_make_event(0, "USER_EXPLICIT", "USER_INPUT", content="mine")])
    _write_transcript(env, _CONV_OTHER, [_make_event(0, "USER_EXPLICIT", "USER_INPUT", content="other")])
    _record_conversation(env, _CONV_MINE)
    # Note: conv-other is NOT in the ids file.

    _run_streamer(env)

    events = _read_raw_events(env)
    assert {e["_mngr_conv_id"] for e in events} == {_CONV_MINE}


def test_streamer_ignores_malformed_lines_in_the_ids_file(env: dict[str, Any]) -> None:
    """A stray non-uuid line in the ids file is filtered out by the shape regex."""
    _write_transcript(env, _CONV_RESUMED, [_make_event(5, "MODEL", "PLANNER_RESPONSE", content="continued")])
    # A real id plus a junk line that must not be treated as a conversation id.
    with env["conversation_ids_file"].open("a") as f:
        f.write("not-a-uuid\n")
    _record_conversation(env, _CONV_RESUMED)

    _run_streamer(env)

    events = _read_raw_events(env)
    assert len(events) == 1
    assert events[0]["_mngr_conv_id"] == _CONV_RESUMED


def test_streamer_persists_offset_so_second_pass_emits_only_new_lines(env: dict[str, Any]) -> None:
    """Per-conversation offsets are saved; new lines appearing later are picked up incrementally."""
    transcript_path = _write_transcript(env, _CONV_A, [_make_event(0, "USER_EXPLICIT", "USER_INPUT", content="first")])
    _record_conversation(env, _CONV_A)
    _run_streamer(env)

    initial = _read_raw_events(env)
    assert len(initial) == 1

    # Append a new line and re-run; only the new line should be emitted.
    with transcript_path.open("a") as f:
        f.write(_make_event(2, "MODEL", "PLANNER_RESPONSE", content="second") + "\n")
    _run_streamer(env)

    final = _read_raw_events(env)
    assert len(final) == 2
    assert [e["step_index"] for e in final] == [0, 2]

    # Offset file should reflect the line count we emitted.
    offset_file = env["offset_dir"] / _CONV_A
    assert offset_file.exists()
    assert offset_file.read_text().strip() == "2"


def test_streamer_picks_up_late_appearing_conversations(env: dict[str, Any]) -> None:
    """A conversation created between two streamer passes is picked up on the second pass."""
    _write_transcript(env, _CONV_A, [_make_event(0, "USER_EXPLICIT", "USER_INPUT", content="first")])
    _record_conversation(env, _CONV_A)
    _run_streamer(env)
    assert {e["_mngr_conv_id"] for e in _read_raw_events(env)} == {_CONV_A}

    _write_transcript(env, _CONV_B, [_make_event(0, "USER_EXPLICIT", "USER_INPUT", content="from B")])
    _record_conversation(env, _CONV_B)
    _run_streamer(env)

    events = _read_raw_events(env)
    assert {e["_mngr_conv_id"] for e in events} == {_CONV_A, _CONV_B}


def test_streamer_resets_offset_when_transcript_shrinks(env: dict[str, Any]) -> None:
    """Defensive: if agy rewrites the transcript shorter than the stored offset, we reset to 0."""
    transcript_path = _write_transcript(
        env,
        _CONV_A,
        [
            _make_event(0, "USER_EXPLICIT", "USER_INPUT", content="first"),
            _make_event(2, "MODEL", "PLANNER_RESPONSE", content="response"),
        ],
    )
    _record_conversation(env, _CONV_A)
    _run_streamer(env)
    assert (env["offset_dir"] / _CONV_A).read_text().strip() == "2"

    # Replace the transcript with a shorter file -- simulates a forked/restarted
    # conversation with the same UUID (rare but defensively handled).
    transcript_path.write_text(_make_event(0, "USER_EXPLICIT", "USER_INPUT", content="restart") + "\n")
    _run_streamer(env)

    # The defensive reset should pull the offset back to 0 and then re-emit
    # the single line. We tolerate the duplication that occurs because the
    # raw transcript now contains both the original 2 lines and the new line.
    assert (env["offset_dir"] / _CONV_A).read_text().strip() == "1"


def test_streamer_handles_missing_transcript_file_gracefully(env: dict[str, Any]) -> None:
    """A recorded conversation id without an on-disk transcript yet is tolerated."""
    _record_conversation(env, _CONV_MISSING)
    # No transcript file staged.
    _run_streamer(env)
    assert _read_raw_events(env) == []


def test_streamer_dedupes_conversation_ids_in_the_ids_file(env: dict[str, Any]) -> None:
    """If a conversation id appears twice in the ids file, the streamer treats it as one."""
    _write_transcript(env, _CONV_A, [_make_event(0, "USER_EXPLICIT", "USER_INPUT", content="hi")])
    _record_conversation(env, _CONV_A)
    _record_conversation(env, _CONV_A)
    _run_streamer(env)
    assert len(_read_raw_events(env)) == 1


def test_streamer_skips_malformed_transcript_lines(env: dict[str, Any]) -> None:
    """A truncated or malformed JSON line is skipped, not fatal."""
    transcript_dir = env["app_data_dir"] / "brain" / _CONV_A / ".system_generated" / "logs"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.jsonl").write_text(
        "{ not really json\n" + _make_event(0, "USER_EXPLICIT", "USER_INPUT", content="good") + "\n"
    )
    _record_conversation(env, _CONV_A)

    _run_streamer(env)

    events = _read_raw_events(env)
    assert len(events) == 1
    assert events[0]["content"] == "good"
