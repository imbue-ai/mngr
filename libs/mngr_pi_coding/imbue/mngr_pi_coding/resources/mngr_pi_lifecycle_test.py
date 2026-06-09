"""Behavioural tests for the pi lifecycle extension (resources/mngr_pi_lifecycle.ts).

The extension is the crux of the pi port -- it owns the RUNNING/WAITING marker,
the readiness sentinel, conversation-resume bookkeeping, and transcript emission.
It runs inside pi's Node process, so we exercise it the way mngr_antigravity
exercises its shell-script resources: drive the real file with synthetic
lifecycle events (here via Node instead of bash) and assert on the files it
writes. Skipped automatically when Node (with TypeScript support) is unavailable,
e.g. a CI sandbox without it -- the .ts is a resource, not Python, so it does not
count toward coverage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_pi_coding.plugin import _LIFECYCLE_EXTENSION_NAME
from imbue.mngr_pi_coding.plugin import _load_resource

# Node driver: load the extension, register its handlers via a fake `pi`, then
# replay a JSON list of events. Each event is `{event, payload?, sessionId?,
# sessionFile?}`; the fake ctx returns the given session id/file. Assertions
# live in Python (below) against the files the extension writes.
_DRIVER_MJS = """
import { readFileSync } from "node:fs";
const events = JSON.parse(readFileSync(process.argv[2], "utf8"));
const handlers = {};
const mod = await import("./mngr_pi_lifecycle.ts");
mod.default({ on: (name, handler) => { (handlers[name] ||= []).push(handler); } });
for (const ev of events) {
  const ctx = { sessionManager: { getSessionId: () => ev.sessionId, getSessionFile: () => ev.sessionFile } };
  for (const handler of (handlers[ev.event] || [])) {
    await handler(ev.payload || {}, ctx);
  }
}
"""


def _node_supports_typescript(node: str, work_dir: Path) -> bool:
    """Whether this Node can import a `.ts` module (strip-types, Node >= ~22.6)."""
    probe_ts = work_dir / "probe.ts"
    probe_ts.write_text("export const value: number = 1;\n")
    probe_mjs = work_dir / "probe.mjs"
    probe_mjs.write_text("const m = await import('./probe.ts'); process.exit(m.value === 1 ? 0 : 1);\n")
    result = subprocess.run([node, str(probe_mjs)], capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def _run_extension(tmp_path: Path, events: list[dict[str, Any]], *, emit_common: bool = True) -> Path:
    """Run the extension over ``events`` under a fresh state dir; return the state dir."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")

    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver_path = work_dir / "driver.mjs"
    driver_path.write_text(_DRIVER_MJS)
    events_path = work_dir / "events.json"
    events_path.write_text(json.dumps(events))

    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, str(driver_path), str(events_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ.get("PATH", ""),
            "MNGR_AGENT_STATE_DIR": str(state_dir),
            "MNGR_PI_EMIT_COMMON_TRANSCRIPT": "1" if emit_common else "0",
        },
    )
    assert result.returncode == 0, f"extension driver failed:\n{result.stdout}\n{result.stderr}"
    return state_dir


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


_COMMON_TRANSCRIPT = Path("events") / "pi-coding" / "common_transcript" / "events.jsonl"
_RAW_TRANSCRIPT = Path("logs") / "pi-coding_transcript" / "events.jsonl"


def test_session_start_writes_readiness_sentinel(tmp_path: Path) -> None:
    state = _run_extension(tmp_path, [{"event": "session_start", "sessionId": "s1", "sessionFile": "/s/s1.jsonl"}])
    assert (state / "pi_session_started").read_text() == "1"


def test_session_file_recorded_on_start_and_switch(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": "/s/s1.jsonl"},
            {"event": "session_switch", "sessionId": "s2", "sessionFile": "/s/s2.jsonl"},
        ],
    )
    assert (state / "pi_session_file").read_text() == "/s/s2.jsonl"


def test_in_memory_session_does_not_clobber_recorded_file(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": "/s/s1.jsonl"},
            # session_switch with no sessionFile models an in-memory session.
            {"event": "session_switch", "sessionId": "mem"},
        ],
    )
    assert (state / "pi_session_file").read_text() == "/s/s1.jsonl"


def test_marker_set_on_agent_start_and_cleared_on_agent_end(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "agent_start", "sessionId": "root"},
            {"event": "agent_end", "sessionId": "root"},
        ],
    )
    assert not (state / "active").exists()
    assert (state / "pi_root_session").read_text().strip() == "root"


def test_marker_present_after_agent_start(tmp_path: Path) -> None:
    state = _run_extension(tmp_path, [{"event": "agent_start", "sessionId": "root"}])
    assert (state / "active").exists()


def test_nested_session_does_not_clear_root_marker(tmp_path: Path) -> None:
    """A nested pi's agent_end (different session id) must leave the root marker."""
    state = _run_extension(
        tmp_path,
        [
            {"event": "agent_start", "sessionId": "root"},
            # A nested pi starts mid-turn then finishes; its session id differs from the root.
            {"event": "agent_start", "sessionId": "child"},
            {"event": "agent_end", "sessionId": "child"},
        ],
    )
    assert (state / "active").exists()
    # The root id, not the child's, is recorded.
    assert (state / "pi_root_session").read_text().strip() == "root"


def test_liveness_fallback_clears_when_no_root_recorded(tmp_path: Path) -> None:
    """An agent_end with no recorded root (e.g. id unavailable) still clears the marker."""
    state = _run_extension(
        tmp_path,
        [
            # No sessionId, so no root is recorded; agent_end must still clear via the fallback.
            {"event": "agent_start"},
            {"event": "agent_end"},
        ],
    )
    assert not (state / "active").exists()


def test_session_shutdown_clears_marker(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "agent_start", "sessionId": "root"},
            {"event": "session_shutdown", "sessionId": "root"},
        ],
    )
    assert not (state / "active").exists()


def test_common_transcript_records_for_each_role(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}},
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "ok"},
                            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                        ],
                        "model": "m",
                        "stopReason": "toolUse",
                        "usage": {"input": 7, "output": 3, "cacheRead": 1, "cacheWrite": 0},
                        "timestamp": 2,
                    }
                },
            },
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "c1",
                        "toolName": "bash",
                        "content": [{"type": "text", "text": "out"}],
                        "isError": False,
                        "timestamp": 3,
                    }
                },
            },
            # Roles the common schema does not model are skipped.
            {
                "event": "message_end",
                "payload": {"message": {"role": "bashExecution", "command": "x", "timestamp": 4}},
            },
        ],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert [r["type"] for r in records] == ["user_message", "assistant_message", "tool_result"]
    assert records[0]["content"] == "hi"
    assert records[1]["text"] == "ok"
    assert records[1]["tool_calls"] == [
        {"tool_call_id": "c1", "tool_name": "bash", "input_preview": '{"command":"ls"}'}
    ]
    assert records[1]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "cache_read_tokens": 1,
        "cache_write_tokens": 0,
    }
    assert records[2]["tool_call_id"] == "c1"
    assert records[2]["output"] == "out"
    assert records[2]["is_error"] is False
    assert all(r["source"] == "pi-coding/common_transcript" for r in records)
    assert len({r["event_id"] for r in records}) == 3


def test_raw_transcript_captures_every_message(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}},
            {
                "event": "message_end",
                "payload": {"message": {"role": "bashExecution", "command": "x", "timestamp": 2}},
            },
        ],
    )
    raw = _read_jsonl(state / _RAW_TRANSCRIPT)
    assert len(raw) == 2
    assert raw[0]["message"]["role"] == "user"
    assert raw[1]["message"]["role"] == "bashExecution"


def test_common_transcript_event_ids_stay_unique_across_restart(tmp_path: Path) -> None:
    """A second process (resume) must not reuse event_ids written by the first.

    event_id is seeded from the existing line count, so ids keep climbing across
    a stop/start even though the resumed session reuses its id and only new
    messages fire message_end.
    """
    events = [{"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}}]
    # First run writes one record.
    state = _run_extension(tmp_path, events)
    # Second run against the SAME state dir (simulating a resumed restart).
    # The first run would have skipped the whole test if node were unavailable.
    node = shutil.which("node")
    assert node is not None
    work_dir = tmp_path / "work"
    (work_dir / "events.json").write_text(
        json.dumps(
            [{"event": "message_end", "payload": {"message": {"role": "user", "content": "again", "timestamp": 2}}}]
        )
    )
    result = subprocess.run(
        [node, str(work_dir / "driver.mjs"), str(work_dir / "events.json")],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ.get("PATH", ""),
            "MNGR_AGENT_STATE_DIR": str(state),
            "MNGR_PI_EMIT_COMMON_TRANSCRIPT": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert len(records) == 2
    assert len({r["event_id"] for r in records}) == 2


def test_no_common_transcript_when_disabled(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [{"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}}],
        emit_common=False,
    )
    assert not (state / _COMMON_TRANSCRIPT).exists()
    # Raw is still captured (it is not gated).
    assert (state / _RAW_TRANSCRIPT).exists()
