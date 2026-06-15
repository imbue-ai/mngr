"""Behavioural test for the Codex usage writer (resources/codex_usage.sh).

Plants a synthetic raw rollout stream, runs the script single-pass, and asserts
on the cost_snapshot events it emits end-to-end (bash wiring + the python3
emitter it invokes, ``codex_usage_emit.py``). Both interpreters are always
available, so no skip is needed. The emitter's own logic is unit-tested directly
in ``codex_usage_emit_test.py``; this test covers the shell integration.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parent / "codex_usage.sh"


def _run(tmp_path: Path, rollout_lines: list[dict[str, Any]]) -> Path:
    state = tmp_path / "state"
    (state / "logs" / "codex_transcript").mkdir(parents=True, exist_ok=True)
    (state / "events" / "logs").mkdir(parents=True, exist_ok=True)
    raw = state / "logs" / "codex_transcript" / "events.jsonl"
    raw.write_text("\n".join(json.dumps(line) for line in rollout_lines) + "\n")
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--single-pass"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state)},
    )
    assert result.returncode == 0, f"writer failed:\n{result.stdout}\n{result.stderr}"
    return state


def _usage_events(state: Path) -> list[dict[str, Any]]:
    path = state / "events" / "codex" / "usage" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _session_meta(session_id: str) -> dict[str, Any]:
    return {"timestamp": "2026-01-01T00:00:00.000Z", "type": "session_meta", "payload": {"id": session_id}}


def _turn_context(model: str) -> dict[str, Any]:
    return {"timestamp": "2026-01-01T00:00:00.000Z", "type": "turn_context", "payload": {"model": model}}


def _token_count(total_usage: dict[str, Any], rate_limits: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "token_count", "info": {"total_token_usage": total_usage}}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {"timestamp": "2026-01-18T02:33:10.756Z", "type": "event_msg", "payload": payload}


def test_writer_emits_snapshot_with_cache_subtracted_and_windows(tmp_path: Path) -> None:
    state = _run(
        tmp_path,
        [
            _session_meta("sess-uuid"),
            _turn_context("gpt-5.2-codex"),
            _token_count(
                {
                    "input_tokens": 8372779,
                    "cached_input_tokens": 7991680,
                    "output_tokens": 175227,
                    "total_tokens": 8548006,
                },
                rate_limits={
                    "primary": {"used_percent": 23.0, "window_minutes": 300, "resets_at": 1768718414},
                    "secondary": {"used_percent": 27.0, "window_minutes": 10080, "resets_at": 1769113493},
                },
            ),
        ],
    )
    events = _usage_events(state)
    assert len(events) == 1
    event = events[0]
    assert event["source"] == "codex/usage"
    assert event["session_id"] == "sess-uuid"
    assert event["model"] == "openai/gpt-5.2-codex"
    assert event["cost"] is None
    # input_tokens is inclusive of cached -> emit non-cached input + cache_read.
    assert event["tokens"] == {
        "input": 8372779 - 7991680,
        "output": 175227,
        "cache_read": 7991680,
        "cache_creation": None,
    }
    assert event["cost_mode"] == "SUBSCRIPTION"
    assert event["rate_limits"]["five_hour"] == {
        "used_percentage": 23.0,
        "resets_at": 1768718414,
        "window_seconds": 18000,
        "label": "5h",
    }
    assert event["rate_limits"]["seven_day"]["window_seconds"] == 10080 * 60


def test_writer_api_key_mode_without_rate_limits(tmp_path: Path) -> None:
    state = _run(
        tmp_path,
        [
            _session_meta("s"),
            _turn_context("gpt-5"),
            _token_count({"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5}),
        ],
    )
    event = _usage_events(state)[0]
    assert event["cost_mode"] == "API_KEY"
    assert "rate_limits" not in event
    assert event["model"] == "openai/gpt-5"


def test_writer_dedups_token_count_lines_across_passes(tmp_path: Path) -> None:
    lines = [
        _session_meta("s"),
        _turn_context("gpt-5"),
        _token_count({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}),
    ]
    state = _run(tmp_path, lines)
    assert len(_usage_events(state)) == 1
    # Re-running over the same input must not re-emit (dedup by line index).
    subprocess.run(
        ["bash", str(_SCRIPT), "--single-pass"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state)},
    )
    assert len(_usage_events(state)) == 1


def _append_and_rerun(state: Path, rollout_lines: list[dict[str, Any]]) -> None:
    """Append more rollout lines and run another single pass over the same state dir."""
    raw = state / "logs" / "codex_transcript" / "events.jsonl"
    with raw.open("a") as handle:
        for line in rollout_lines:
            handle.write(json.dumps(line) + "\n")
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--single-pass"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state)},
    )
    assert result.returncode == 0, f"writer failed:\n{result.stdout}\n{result.stderr}"


def test_writer_processes_only_new_lines_and_carries_session_model_across_passes(tmp_path: Path) -> None:
    # Pass 1: meta + turn_context + one token_count.
    state = _run(
        tmp_path,
        [
            _session_meta("sess-uuid"),
            _turn_context("gpt-5.2-codex"),
            _token_count({"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}),
        ],
    )
    assert len(_usage_events(state)) == 1
    # Pass 2: append ONLY a later token_count (no meta/turn_context). The cursor
    # must carry session_id + model from the persisted state so the new event
    # still resolves them.
    _append_and_rerun(state, [_token_count({"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30})])
    events = _usage_events(state)
    assert len(events) == 2
    latest = events[-1]
    assert latest["session_id"] == "sess-uuid"
    assert latest["model"] == "openai/gpt-5.2-codex"
    assert latest["tokens"] == {"input": 80, "output": 30, "cache_read": 20, "cache_creation": None}
