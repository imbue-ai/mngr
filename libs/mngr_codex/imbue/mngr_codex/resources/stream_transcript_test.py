"""Tests for the codex stream_transcript.sh raw streamer.

Exercises the streamer's core behaviors by running it with --single-pass in a
controlled filesystem layout. The streamer reads the single active rollout path
from ``$MNGR_AGENT_STATE_DIR/codex_transcript_path`` (the file
record_session_pointers.sh records), tails that rollout JSONL, and appends every new
line verbatim to ``$MNGR_AGENT_STATE_DIR/logs/codex_transcript/events.jsonl``.

Each test stages:
  - A fake $MNGR_AGENT_STATE_DIR with stub mngr_log.sh and the real
    mngr_common_transcript_lib.sh in commands/
  - A rollout-*.jsonl file somewhere under the state dir's tmp tree
  - A codex_transcript_path file pointing at that rollout

The streamer's contract:
  - Read the rollout path from codex_transcript_path (re-read each cycle).
  - Append every new line of that rollout, verbatim, to the output.
  - Persist a per-rollout offset under
    plugin/codex/.transcript_offsets/<sanitized-basename> so the next pass picks
    up only new lines, with a defensive reset if the rollout shrinks.
  - Re-read the persisted offset on every pass and advance it under the shared
    per-agent transcript lock, so the 1s daemon and the turn-end --single-pass
    flush never re-emit lines the other already streamed.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.utils.polling import poll_until

_SCRIPT_PATH = Path(__file__).parent / "stream_transcript.sh"
# A real rollout captured from the patched codex-cli 0.146.0 build (one full
# exec turn); the streamer copies lines verbatim, so the exactly-once tests
# drive it with the real wire shapes rather than hand-written ones.
_REAL_0146_ROLLOUT = Path(__file__).parent / "test_fixtures" / "codex_0146_rollout_exec_turn.jsonl"


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
    # The real lock library (not a stub): the exactly-once contract depends on
    # its mkdir-lock semantics, mirroring the production commands/ layout.
    (commands / "mngr_common_transcript_lib.sh").write_text(
        importlib.resources.files(mngr_resources).joinpath("mngr_common_transcript_lib.sh").read_text()
    )
    return state_dir


def _lock_dir(state_dir: Path) -> Path:
    """The per-agent transcript lock dir, as laid out by mngr_common_transcript_lib.sh."""
    return state_dir / ".common_transcript_convert.lock"


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


def _wait_for_output_lines(state_dir: Path, expected_count: int, daemon: subprocess.Popen[bytes]) -> None:
    """Block until the raw output has at least expected_count lines (30s cap)."""

    def _has_expected_lines() -> bool:
        if daemon.poll() is not None:
            pytest.fail(f"streamer daemon exited unexpectedly with code {daemon.returncode}")
        output = _output_file(state_dir)
        return output.exists() and len(output.read_text().splitlines()) >= expected_count

    if not poll_until(_has_expected_lines, timeout=30.0, poll_interval=0.05):
        pytest.fail(f"raw output never reached {expected_count} lines")


def _suspend_daemon_outside_lock(daemon: subprocess.Popen[bytes], state_dir: Path) -> None:
    """SIGSTOP the daemon, retrying if it was caught holding the transcript lock.

    Stopping it mid-critical-section would leave the lock dir behind and stall
    the interleaved --single-pass on lock acquisition instead of exercising the
    offset re-read; the critical section is a few milliseconds per cycle, so a
    retry almost always lands in the daemon's sleep.
    """

    def _stopped_outside_lock() -> bool:
        daemon.send_signal(signal.SIGSTOP)
        if not _lock_dir(state_dir).exists():
            return True
        daemon.send_signal(signal.SIGCONT)
        return False

    if not poll_until(_stopped_outside_lock, timeout=30.0, poll_interval=0.05):
        pytest.fail("daemon could not be suspended outside the lock's critical section")


def test_daemon_and_turn_end_single_pass_emit_each_line_exactly_once(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """Interleaving the poll daemon with a turn-end --single-pass over one
    rollout yields every rollout line exactly once, in order (invariant U5).

    Regression test for the bidirectional duplicate-emit race: the daemon used
    to cache per-rollout offsets in memory (persisted offsets were read only on
    first pickup), so after a turn-end --single-pass advanced the persisted
    offset, the daemon's next cycle re-emitted from its stale cache every line
    the single-pass had already streamed. Every pass now re-reads the persisted
    offset under the shared lock. Driven with a real codex 0.146 rollout:
    daemon streams the turn prefix, is suspended (SIGSTOP) mid-run, a
    single-pass streams the mid-turn lines, then the resumed daemon streams the
    final line.
    """
    fixture_lines = _REAL_0146_ROLLOUT.read_text().splitlines()
    assert len(fixture_lines) >= 12, "the captured rollout fixture is expected to hold one full exec turn"

    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    rollout = _write_rollout(tmp_path, "rollout-real.jsonl", fixture_lines[:10])
    _set_transcript_path(state_dir, rollout)

    daemon = subprocess.Popen(
        ["bash", str(_SCRIPT_PATH)],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_output_lines(state_dir, 10, daemon)
        _suspend_daemon_outside_lock(daemon, state_dir)

        # Turn-end leg: more rollout lines land while the daemon still holds its
        # (stale) in-process picture, and the --single-pass flush streams them.
        with rollout.open("a") as f:
            f.write("\n".join(fixture_lines[10:-1]) + "\n")
        _run_streamer(state_dir)
        assert _output_file(state_dir).read_text().splitlines() == fixture_lines[:-1]

        # Daemon leg: the resumed daemon must pick up only the final appended
        # line -- re-reading the offset the single-pass persisted -- not re-emit
        # everything past its stale cache.
        daemon.send_signal(signal.SIGCONT)
        with rollout.open("a") as f:
            f.write(fixture_lines[-1] + "\n")
        # A buggy re-emit arrives as one append that overshoots this count, so
        # the exact-equality assertion below catches it as soon as this returns.
        _wait_for_output_lines(state_dir, len(fixture_lines), daemon)
    finally:
        daemon.send_signal(signal.SIGCONT)
        daemon.kill()
        daemon.wait(timeout=10)

    assert _output_file(state_dir).read_text().splitlines() == fixture_lines


def test_single_pass_defers_emit_while_transcript_lock_is_held(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    """While another pass holds the shared transcript lock, a pass emits
    nothing and leaves the offset untouched; once the lock is free the deferred
    lines are emitted normally."""
    state_dir = _state_dir(tmp_path, stub_mngr_log_sh)
    rollout = _write_rollout(tmp_path, "rollout-a.jsonl", [_user_line("hi")])
    _set_transcript_path(state_dir, rollout)

    _lock_dir(state_dir).mkdir()
    subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir), "MNGR_CONVERT_LOCK_TIMEOUT": "0"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert _read_raw_events(state_dir) == []
    assert not (_offset_dir(state_dir) / "rollout-a.jsonl").exists()

    _lock_dir(state_dir).rmdir()
    _run_streamer(state_dir)
    assert len(_read_raw_events(state_dir)) == 1
