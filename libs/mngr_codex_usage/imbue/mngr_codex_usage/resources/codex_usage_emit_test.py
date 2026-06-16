"""Unit tests for the Codex usage emitter (resources/codex_usage_emit.py).

Exercises the emitter's pure helpers and its end-to-end ``emit`` against
synthetic rollout streams on disk, without the surrounding shell script. The
shell integration (codex_usage.sh invoking this module) is covered separately by
``codex_usage_test.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.mngr_codex_usage.resources import codex_usage_emit


def _session_meta(session_id: str) -> dict[str, Any]:
    return {"timestamp": "2026-01-01T00:00:00.000Z", "type": "session_meta", "payload": {"id": session_id}}


def _turn_context(model: str) -> dict[str, Any]:
    return {"timestamp": "2026-01-01T00:00:00.000Z", "type": "turn_context", "payload": {"model": model}}


def _token_count(total_usage: dict[str, Any], rate_limits: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "token_count", "info": {"total_token_usage": total_usage}}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {"timestamp": "2026-01-18T02:33:10.756Z", "type": "event_msg", "payload": payload}


def _run(tmp_path: Path, rollout_lines: list[dict[str, Any]]) -> tuple[Path, Path]:
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out" / "events.jsonl"
    state_file = tmp_path / "state" / ".usage_cursor"
    input_file.write_text("\n".join(json.dumps(line) for line in rollout_lines) + "\n")
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    return output_file, state_file


def _events(output_file: Path) -> list[dict[str, Any]]:
    if not output_file.exists():
        return []
    return [json.loads(line) for line in output_file.read_text().splitlines() if line.strip()]


def test_tokens_subtract_cache_from_input() -> None:
    tokens = codex_usage_emit._tokens_from_total_usage(
        {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 50}
    )
    assert tokens == {"input": 70, "output": 50, "cache_read": 30, "cache_creation": None}


def test_tokens_none_for_non_dict() -> None:
    assert codex_usage_emit._tokens_from_total_usage(None) is None


def test_tokens_none_when_all_buckets_absent() -> None:
    # An empty (or unrecognized-key) usage dict has no usable buckets, so it maps
    # to None -- the emit guard then drops the content-free token block rather than
    # writing an all-None snapshot the reader would price as a spurious $0.00 session.
    assert codex_usage_emit._tokens_from_total_usage({}) is None


def test_emit_skips_token_count_with_empty_usage_and_no_rate_limits(tmp_path: Path) -> None:
    output_file, _ = _run(
        tmp_path,
        [
            _session_meta("sess-1"),
            _turn_context("gpt-5-codex"),
            _token_count({}),
        ],
    )
    assert _events(output_file) == []


def test_rate_limits_map_primary_and_secondary_to_windows() -> None:
    windows = codex_usage_emit._rate_limits(
        {
            "primary": {"used_percent": 12, "resets_at": "t1", "window_minutes": 300},
            "secondary": {"used_percent": 5, "resets_at": "t2", "window_minutes": 10080},
        }
    )
    assert windows is not None
    assert windows["five_hour"] == {"used_percentage": 12, "resets_at": "t1", "window_seconds": 18000, "label": "5h"}
    assert windows["seven_day"] == {"used_percentage": 5, "resets_at": "t2", "window_seconds": 604800, "label": "7d"}


def test_rate_limits_none_for_non_dict() -> None:
    assert codex_usage_emit._rate_limits([]) is None


def test_emit_writes_snapshot_with_cache_subtracted_and_windows(tmp_path: Path) -> None:
    output_file, _ = _run(
        tmp_path,
        [
            _session_meta("sess-1"),
            _turn_context("gpt-5-codex"),
            _token_count(
                {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 50},
                rate_limits={"primary": {"used_percent": 12, "resets_at": "t1", "window_minutes": 300}},
            ),
        ],
    )
    events = _events(output_file)
    assert len(events) == 1
    event = events[0]
    assert event["source"] == "codex/usage"
    assert event["type"] == "cost_snapshot"
    assert event["session_id"] == "sess-1"
    assert event["model"] == "openai/gpt-5-codex"
    assert event["cost"] is None
    assert event["tokens"] == {"input": 70, "output": 50, "cache_read": 30, "cache_creation": None}
    # rate_limits present => imputed subscription spend.
    assert event["cost_mode"] == "SUBSCRIPTION"
    assert event["rate_limits"]["five_hour"]["label"] == "5h"


def test_emit_without_rate_limits_is_api_key_mode(tmp_path: Path) -> None:
    output_file, _ = _run(
        tmp_path,
        [
            _session_meta("sess-1"),
            _token_count({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}),
        ],
    )
    events = _events(output_file)
    assert len(events) == 1
    assert events[0]["cost_mode"] == "API_KEY"
    assert "rate_limits" not in events[0]


def test_emit_skips_token_count_before_session_meta(tmp_path: Path) -> None:
    # No session_id yet => the token_count is skipped (session_id is contractual).
    output_file, _ = _run(tmp_path, [_token_count({"input_tokens": 10, "output_tokens": 5})])
    assert _events(output_file) == []


def test_emit_advances_cursor_so_reprocessing_emits_nothing(tmp_path: Path) -> None:
    lines = [
        _session_meta("sess-1"),
        _turn_context("gpt-5-codex"),
        _token_count({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}),
    ]
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out" / "events.jsonl"
    state_file = tmp_path / "state" / ".usage_cursor"
    input_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    assert len(_events(output_file)) == 1

    # Second pass over the same (unchanged) file: cursor is at EOF, so no re-emit.
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    assert len(_events(output_file)) == 1


def test_emit_resumes_session_and_model_across_calls(tmp_path: Path) -> None:
    # session_meta / turn_context arrive in the first poll; a token_count in a
    # later poll must still resolve session/model from the persisted cursor state.
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out" / "events.jsonl"
    state_file = tmp_path / "state" / ".usage_cursor"

    input_file.write_text(json.dumps(_session_meta("sess-1")) + "\n" + json.dumps(_turn_context("gpt-5-codex")) + "\n")
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    assert _events(output_file) == []

    with input_file.open("a") as handle:
        handle.write(
            json.dumps(_token_count({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5})) + "\n"
        )
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    events = _events(output_file)
    assert len(events) == 1
    assert events[0]["session_id"] == "sess-1"
    assert events[0]["model"] == "openai/gpt-5-codex"


def test_emit_reprocesses_from_top_on_truncation(tmp_path: Path) -> None:
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out" / "events.jsonl"
    state_file = tmp_path / "state" / ".usage_cursor"

    first = [
        _session_meta("sess-1"),
        _turn_context("gpt-5-codex"),
        _token_count({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}),
    ]
    input_file.write_text("\n".join(json.dumps(line) for line in first) + "\n")
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    assert len(_events(output_file)) == 1

    # A fresh, shorter rollout (rotation/truncation) is reprocessed from the top.
    second = [
        _session_meta("sess-2"),
        _turn_context("gpt-5-codex"),
        _token_count({"input_tokens": 7, "cached_input_tokens": 0, "output_tokens": 3}),
    ]
    input_file.write_text("\n".join(json.dumps(line) for line in second) + "\n")
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    events = _events(output_file)
    assert len(events) == 2
    assert events[1]["session_id"] == "sess-2"


def test_emit_skips_malformed_line_but_keeps_processing(tmp_path: Path) -> None:
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out" / "events.jsonl"
    state_file = tmp_path / "state" / ".usage_cursor"
    good = [
        json.dumps(_session_meta("sess-1")),
        json.dumps(_turn_context("gpt-5-codex")),
        "{not valid json",
        json.dumps(_token_count({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5})),
    ]
    input_file.write_text("\n".join(good) + "\n")
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    # The malformed line is skipped; the valid token_count still produces an event.
    assert len(_events(output_file)) == 1


def test_emit_resets_on_corrupt_cursor(tmp_path: Path) -> None:
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out" / "events.jsonl"
    state_file = tmp_path / "state" / ".usage_cursor"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{corrupt cursor")
    lines = [
        _session_meta("sess-1"),
        _turn_context("gpt-5-codex"),
        _token_count({"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}),
    ]
    input_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    codex_usage_emit.emit(str(input_file), str(output_file), str(state_file))
    # A corrupt cursor resets to the top, so the stream is processed from line 1.
    assert len(_events(output_file)) == 1


def test_emit_no_input_file_is_noop(tmp_path: Path) -> None:
    output_file = tmp_path / "out" / "events.jsonl"
    state_file = tmp_path / "state" / ".usage_cursor"
    codex_usage_emit.emit(str(tmp_path / "missing.jsonl"), str(output_file), str(state_file))
    assert not output_file.exists()
