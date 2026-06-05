"""Integration tests for claude_usage_writer.sh.

Exercises the bash writer directly via subprocess to ensure it appends
properly-shaped JSONL events to the per-agent usage events file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest


def _has_jq() -> bool:
    return shutil.which("jq") is not None


pytestmark = pytest.mark.skipif(not _has_jq(), reason="jq not installed; required by claude_usage_writer.sh")


def _run_writer(writer_path: Path, stdin: str, events_file: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MNGR_USAGE_EVENTS_PATH": str(events_file)}
    return subprocess.run(
        [str(writer_path)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _read_last_event(events_file: Path) -> dict:
    """Helper: read the last well-formed JSON line."""
    lines = [line for line in events_file.read_text().splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_writer_emits_event_with_rate_limits(writer_path: Path, events_file: Path) -> None:
    """When stdin has a rate_limits field, append one event line in canonical shape."""
    payload = json.dumps(
        {
            "session_id": "abc",
            "cost": {"total_cost_usd": 0.42, "total_duration_ms": 12000},
            "rate_limits": {
                "five_hour": {"used_percentage": 73.4, "resets_at": 1777673400},
                "seven_day": {"used_percentage": 41.0, "resets_at": 1778000000},
            },
        }
    )
    result = _run_writer(writer_path, payload, events_file)
    assert result.returncode == 0, result.stderr
    event = _read_last_event(events_file)
    assert event["source"] == "claude/usage"
    assert event["type"] == "cost_snapshot"
    assert event["event_id"].startswith("evt-")
    # The event timestamp is canonical UTC ISO 8601 with a fixed 9-digit
    # fractional part (the writer emits `<date>T<time>.000000000Z`). The regex
    # pins the full shape, including the trailing `Z` that anchors it to UTC.
    # The regex alone can't tell that the calendar fields name a real instant
    # (it would accept month 13), so also strptime the seconds-resolution `[:19]`
    # prefix -- sliced to 19 chars because strptime's `%f` only parses 1-6
    # fractional digits and would choke on the 9-digit fraction.
    timestamp = event["timestamp"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z", timestamp), timestamp
    datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S")
    # session_id and cost are passed through unchanged. They let downstream
    # consumers correlate a cost reading to the session it accumulated in.
    assert event["session_id"] == "abc"
    assert event["cost"]["total_cost_usd"] == 0.42
    assert event["cost"]["total_duration_ms"] == 12000
    assert event["rate_limits"]["five_hour"]["used_percentage"] == 73.4
    assert event["rate_limits"]["five_hour"]["resets_at"] == 1777673400
    assert event["rate_limits"]["seven_day"]["used_percentage"] == 41.0
    # Writer decorates Claude Code's window keys with short human-display labels
    # (5h / 7d / overage) so mngr_usage's per-line prefix is compact. Without
    # these, the renderer would fall back to the literal key (`five_hour: ...`).
    assert event["rate_limits"]["five_hour"]["label"] == "5h"
    assert event["rate_limits"]["seven_day"]["label"] == "7d"
    # window_seconds gives mngr_usage what it needs to derive elapsed_percentage
    # without hardcoding per-window-class knowledge in the reader.
    assert event["rate_limits"]["five_hour"]["window_seconds"] == 18000
    assert event["rate_limits"]["seven_day"]["window_seconds"] == 604800


def test_writer_emits_cost_only_event_for_api_key_users(writer_path: Path, events_file: Path) -> None:
    """API-key (non-subscription) sessions never include rate_limits in the statusline
    payload (per Claude Code docs: rate_limits only appears for Claude.ai Pro/Max
    subscribers). But cost is always present. The writer must still emit an event
    so cost tracking works for API-key users."""
    payload = json.dumps(
        {
            "session_id": "uuid-xyz",
            "cost": {"total_cost_usd": 1.23, "total_duration_ms": 60000},
            # No rate_limits field at all.
        }
    )
    result = _run_writer(writer_path, payload, events_file)
    assert result.returncode == 0, result.stderr
    event = _read_last_event(events_file)
    assert event["type"] == "cost_snapshot"
    assert event["session_id"] == "uuid-xyz"
    assert event["cost"]["total_cost_usd"] == 1.23
    # rate_limits is explicitly null (not missing) so the reader can distinguish
    # "writer ran but no rate-limit data" from "writer never ran".
    assert event["rate_limits"] is None


def test_writer_omits_window_seconds_for_overage(writer_path: Path, events_file: Path) -> None:
    """Overage has no fixed window length, so the writer omits window_seconds for it.
    The reader treats a missing window_seconds as 'no derived elapsed metrics for this window'."""
    payload = json.dumps(
        {
            "rate_limits": {
                "overage": {"used_percentage": 5.0, "resets_at": 1777673400},
            }
        }
    )
    result = _run_writer(writer_path, payload, events_file)
    assert result.returncode == 0, result.stderr
    event = _read_last_event(events_file)
    assert "window_seconds" not in event["rate_limits"]["overage"]
    # Label is still present though -- only window_seconds is class-specific.
    assert event["rate_limits"]["overage"]["label"] == "overage"


def test_writer_skips_when_no_rate_limits_and_no_cost(writer_path: Path, events_file: Path) -> None:
    """Earliest statusline renders may have neither rate_limits nor cost yet;
    emitting an all-null event would just clutter the log so we skip."""
    payload = json.dumps({"session_id": "abc", "context_window": {"used_percentage": 5.0}})
    result = _run_writer(writer_path, payload, events_file)
    assert result.returncode == 0, result.stderr
    assert not events_file.exists()


def test_writer_skips_when_rate_limits_and_cost_are_both_null(writer_path: Path, events_file: Path) -> None:
    """Explicitly-null both fields is treated as 'no data' -- skip emission."""
    payload = json.dumps({"rate_limits": None, "cost": None})
    result = _run_writer(writer_path, payload, events_file)
    assert result.returncode == 0, result.stderr
    assert not events_file.exists()


def test_writer_skips_on_garbage_input(writer_path: Path, events_file: Path) -> None:
    """Non-JSON input shouldn't crash the writer (and shouldn't emit a malformed event)."""
    result = _run_writer(writer_path, "this is not json", events_file)
    assert result.returncode == 0, result.stderr
    assert not events_file.exists()


def test_writer_passes_through_non_object_rate_limits(writer_path: Path, events_file: Path) -> None:
    """If the statusline schema ever sends a non-object `rate_limits` (string,
    array, etc.), the writer must not crash under `set -euo pipefail`. The
    value is written through unchanged so the CLI reader's isinstance(dict)
    check can filter it downstream."""
    payload = json.dumps({"rate_limits": "unexpected"})
    result = _run_writer(writer_path, payload, events_file)
    assert result.returncode == 0, result.stderr
    event = _read_last_event(events_file)
    assert event["rate_limits"] == "unexpected"


def test_writer_appends_one_event_per_render(writer_path: Path, events_file: Path) -> None:
    """Successive renders accumulate as separate event lines."""
    for pct in (10.0, 20.0, 30.0):
        payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": 1700000000}}})
        result = _run_writer(writer_path, payload, events_file)
        assert result.returncode == 0, result.stderr
    lines = [line for line in events_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 3
    assert json.loads(lines[-1])["rate_limits"]["five_hour"]["used_percentage"] == 30.0


def test_writer_handles_concurrent_appends(writer_path: Path, events_file: Path) -> None:
    """Concurrent renders must end with an events file in which every distinct
    event survives exactly once -- short lines are atomic on append, so we don't
    need flock. We check both that no output is torn and that no event is dropped
    or duplicated."""
    payloads = [
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": float(i), "resets_at": 1700000000 + i}}})
        for i in range(20)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_run_writer, writer_path, payload, events_file) for payload in payloads]
        for f in futures:
            r = f.result()
            assert r.returncode == 0, r.stderr
    lines = [line for line in events_file.read_text().splitlines() if line.strip()]
    assert len(lines) == 20
    # Every distinct submitted event must survive exactly once: reconstructing the
    # set of used_percentage values confirms none was dropped or duplicated (a count
    # check alone would not), and json.loads on each line fails on any torn output.
    seen_percentages = {json.loads(line)["rate_limits"]["five_hour"]["used_percentage"] for line in lines}
    assert seen_percentages == {float(i) for i in range(20)}


def test_writer_errors_when_no_path_resolution_possible(writer_path: Path, tmp_path: Path) -> None:
    """If neither MNGR_USAGE_EVENTS_PATH nor MNGR_AGENT_STATE_DIR is set, exit 64."""
    env = {k: v for k, v in os.environ.items() if k not in ("MNGR_USAGE_EVENTS_PATH", "MNGR_AGENT_STATE_DIR")}
    result = subprocess.run(
        [str(writer_path)],
        input='{"rate_limits": {}}',
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 64
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
