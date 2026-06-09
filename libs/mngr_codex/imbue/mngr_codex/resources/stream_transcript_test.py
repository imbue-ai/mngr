"""Tests for the codex stream_transcript.sh raw streamer.

Exercises the streamer's core behaviors by running it with --single-pass in a
controlled filesystem layout. The streamer reads the single active rollout path
from ``$MNGR_AGENT_STATE_DIR/codex_transcript_path`` (the file
set_active_marker.sh records), tails that rollout JSONL, and appends every new
line verbatim to ``$MNGR_AGENT_STATE_DIR/logs/codex_transcript/events.jsonl``.

Each test stages:
  - A fake $MNGR_AGENT_STATE_DIR with stub mngr_log.sh in commands/
  - A rollout-*.jsonl file somewhere under the state dir's tmp tree
  - A codex_transcript_path file pointing at that rollout

The streamer's contract:
  - Read the rollout path from codex_transcript_path (re-read each cycle).
  - Append every new line of that rollout, verbatim, to the output.
  - Persist a per-rollout offset under
    plugin/codex/.transcript_offsets/<sanitized-basename> so the next pass picks
    up only new lines, with a defensive reset if the rollout shrinks.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

_SCRIPT_PATH = Path(__file__).parent / "stream_transcript.sh"


def _make_line(type_: str, payload: dict[str, Any], timestamp: str = "2026-06-09T07:00:00.000Z") -> str:
    """Build one codex rollout wire line: {timestamp, type, payload}."""
    return json.dumps({"timestamp": timestamp, "type": type_, "payload": payload})


def _user_line(text: str) -> str:
    return _make_line(
        "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    )


def _assistant_line(text: str) -> str:
    return _make_line(
        "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    )


def _state_dir(tmp_path: Path, stub_mngr_log_sh: str) -> Path:
    state_dir = tmp_path / "agent"
    commands = state_dir / "commands"
    commands.mkdir(parents=True)
    (commands / "mngr_log.sh").write_text(stub_mngr_log_sh)
    return state_dir


def _write_rollout(tmp_path: Path, name: str, lines: list[str]) -> Path:
    rollout_dir = tmp_path / "rollouts"
    rollout_dir.mkdir(exist_ok=True)
    path = rollout_dir / name
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return path


def _set_transcript_path(state_dir: Path, rollout_path: Path) -> None:
    (state_dir / "codex_transcript_path").write_text(str(rollout_path))


def _output_file(state_dir: Path) -> Path:
    return state_dir / "logs" / "codex_transcript" / "events.jsonl"


def _offset_dir(state_dir: Path) -> Path:
    return state_dir / "plugin" / "codex" / ".transcript_offsets"


def _run_streamer(state_dir: Path) -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Traceback" not in result.stderr, result.stderr


def _read_raw_events(state_dir: Path) -> list[dict[str, Any]]:
    output = _output_file(state_dir)
    if not output.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in output.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


# -- Tests --


def test_streamer_with_no_transcript_path_produces_empty_output(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """No recorded rollout path -> nothing to stream."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    _run_streamer(state_dir)
    assert _read_raw_events(state_dir) == []


def test_streamer_copies_rollout_lines_verbatim(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Each rollout line is appended verbatim (no reschematising)."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    rollout = _write_rollout(tmp_path, "rollout-a.jsonl", [_user_line("hi"), _assistant_line("hello")])
    _set_transcript_path(state_dir, rollout)

    _run_streamer(state_dir)

    events = _read_raw_events(state_dir)
    assert len(events) == 2
    assert events[0]["payload"]["role"] == "user"
    assert events[1]["payload"]["content"][0]["text"] == "hello"
    # Verbatim: the raw output bytes equal the rollout's lines.
    assert _output_file(state_dir).read_text() == rollout.read_text()


def test_streamer_persists_offset_so_second_pass_emits_only_new_lines(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Per-rollout offsets are saved; lines appearing later are picked up incrementally."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    rollout = _write_rollout(tmp_path, "rollout-a.jsonl", [_user_line("first")])
    _set_transcript_path(state_dir, rollout)
    _run_streamer(state_dir)
    assert len(_read_raw_events(state_dir)) == 1

    with rollout.open("a") as f:
        f.write(_assistant_line("second") + "\n")
    _run_streamer(state_dir)

    final = _read_raw_events(state_dir)
    assert len(final) == 2
    assert final[1]["payload"]["role"] == "assistant"

    offset_file = _offset_dir(state_dir) / "rollout-a.jsonl"
    assert offset_file.exists()
    assert offset_file.read_text().strip() == "2"


def test_streamer_follows_a_changed_transcript_path(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """On resume codex opens a fresh rollout; re-reading the path file each cycle
    means the streamer follows the new rollout."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    rollout_a = _write_rollout(tmp_path, "rollout-a.jsonl", [_user_line("from A")])
    _set_transcript_path(state_dir, rollout_a)
    _run_streamer(state_dir)
    assert len(_read_raw_events(state_dir)) == 1

    rollout_b = _write_rollout(tmp_path, "rollout-b.jsonl", [_user_line("from B")])
    _set_transcript_path(state_dir, rollout_b)
    _run_streamer(state_dir)

    events = _read_raw_events(state_dir)
    assert [e["payload"]["content"][0]["text"] for e in events] == ["from A", "from B"]
    # Each rollout has its own offset key.
    assert (_offset_dir(state_dir) / "rollout-a.jsonl").read_text().strip() == "1"
    assert (_offset_dir(state_dir) / "rollout-b.jsonl").read_text().strip() == "1"


def test_streamer_resets_offset_when_rollout_shrinks(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Defensive: if codex replaces the rollout with a shorter file, reset to 0."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    rollout = _write_rollout(tmp_path, "rollout-a.jsonl", [_user_line("first"), _assistant_line("response")])
    _set_transcript_path(state_dir, rollout)
    _run_streamer(state_dir)
    assert (_offset_dir(state_dir) / "rollout-a.jsonl").read_text().strip() == "2"

    # Replace with a shorter file (same path) -- the offset must reset and re-emit.
    rollout.write_text(_user_line("restart") + "\n")
    _run_streamer(state_dir)
    assert (_offset_dir(state_dir) / "rollout-a.jsonl").read_text().strip() == "1"


def test_streamer_handles_missing_rollout_file_gracefully(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A recorded rollout path without an on-disk file yet is tolerated."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    (state_dir / "codex_transcript_path").write_text(str(tmp_path / "rollouts" / "not-there.jsonl"))
    _run_streamer(state_dir)
    assert _read_raw_events(state_dir) == []


def test_streamer_appends_lines_with_spaces_in_rollout_path(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """A rollout path containing spaces is read and tailed correctly."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    spaced_dir = tmp_path / "My Rollouts"
    spaced_dir.mkdir()
    rollout = spaced_dir / "rollout a.jsonl"
    rollout.write_text(_user_line("spaced") + "\n")
    _set_transcript_path(state_dir, rollout)

    _run_streamer(state_dir)

    events = _read_raw_events(state_dir)
    assert len(events) == 1
    assert events[0]["payload"]["content"][0]["text"] == "spaced"
