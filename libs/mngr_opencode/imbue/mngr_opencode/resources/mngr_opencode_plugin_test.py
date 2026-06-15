"""Behavioural test for the opencode lifecycle plugin (resources/mngr_opencode_plugin.ts).

The plugin runs in-process on the ``opencode serve`` Node process and owns the
``active`` marker plus the raw and common transcripts. We exercise it the way the
pi extension is exercised: load the real ``.ts`` under Node, hand it a synthetic
stream of OpenCode events, and assert on the files it writes. The conversion logic
moved into TypeScript (so it no longer runs in CI as Python), making this the only
CI-runnable check that opencode's emitter still produces the canonical envelope --
without it, emitter drift would surface only in the (non-CI) release test.

Skipped automatically when Node (with TypeScript support) is unavailable, e.g. a CI
sandbox without it -- the ``.ts`` is a resource, not Python, so it does not count
toward coverage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record

_PLUGIN_NAME = "mngr_opencode_plugin.ts"
_PLUGIN_PATH = Path(__file__).parent / _PLUGIN_NAME

# Driver: load the plugin, invoke it to obtain its hooks (only the server role does
# anything), then replay a JSON list of OpenCode events through the `event` hook.
_DRIVER_MJS = """
import { readFileSync } from "node:fs";
const events = JSON.parse(readFileSync(process.argv[2], "utf8"));
const mod = await import("./mngr_opencode_plugin.ts");
const hooks = await mod.MngrLifecyclePlugin({});
for (const event of events) {
  await hooks.event({ event });
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


def _run_plugin(tmp_path: Path, events: list[dict[str, Any]]) -> Path:
    """Run the plugin over ``events`` under a fresh state dir; return the state dir."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")

    (work_dir / _PLUGIN_NAME).write_text(_PLUGIN_PATH.read_text())
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
            # Only the server role maintains the marker/transcripts (the attach client stays inert).
            "MNGR_OPENCODE_ROLE": "server",
            "MNGR_OPENCODE_EMIT_COMMON": "1",
        },
    )
    assert result.returncode == 0, f"plugin driver failed:\n{result.stdout}\n{result.stderr}"
    return state_dir


_COMMON_TRANSCRIPT = Path("events") / "opencode" / "common_transcript" / "events.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# A realistic single turn: the root session is created, a user message and an
# assistant message (with a completed bash tool call) stream in, and the session
# goes idle -- which is what triggers the common-transcript rebuild.
_TURN_EVENTS: list[dict[str, Any]] = [
    {"type": "session.created", "properties": {"info": {"id": "ses_root"}}},
    {
        "type": "message.updated",
        "properties": {"info": {"id": "m1", "role": "user", "sessionID": "ses_root", "time": {"created": 1000}}},
    },
    {
        "type": "message.part.updated",
        "properties": {"part": {"id": "p1", "messageID": "m1", "type": "text", "text": "Count slowly"}},
    },
    {
        "type": "message.updated",
        "properties": {
            "info": {
                "id": "m2",
                "role": "assistant",
                "sessionID": "ses_root",
                "time": {"created": 2000},
                "providerID": "opencode",
                "modelID": "deepseek-v4-flash-free",
                "finish": "stop",
            }
        },
    },
    {
        "type": "message.part.updated",
        "properties": {"part": {"id": "p2", "messageID": "m2", "type": "text", "text": "one two three"}},
    },
    {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "p3",
                "messageID": "m2",
                "type": "tool",
                "callID": "call_1",
                "tool": "bash",
                "state": {"status": "completed", "input": {"command": "echo hi"}, "output": "hi"},
            }
        },
    },
    {"type": "session.status", "properties": {"sessionID": "ses_root", "status": {"type": "idle"}}},
]


def test_emitted_common_records_conform_to_canonical_schema(tmp_path: Path) -> None:
    """Every record the plugin emits must validate against the shared envelope schema.

    Guards against the opencode emitter (resources/mngr_opencode_plugin.ts) and the
    canonical schema (imbue.mngr.agents.common_transcript_records) drifting apart.
    """
    state = _run_plugin(tmp_path, _TURN_EVENTS)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert {r["type"] for r in records} == {"user_message", "assistant_message", "tool_result"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_user_and_assistant_text_captured(tmp_path: Path) -> None:
    """Sanity check on the conversion itself (the marker/transcript single-writer path)."""
    state = _run_plugin(tmp_path, _TURN_EVENTS)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    by_type = {r["type"]: r for r in records}
    assert by_type["user_message"]["content"] == "Count slowly"
    assert by_type["assistant_message"]["text"] == "one two three"
    assert by_type["assistant_message"]["model"] == "opencode/deepseek-v4-flash-free"
    assert by_type["assistant_message"]["tool_calls"][0]["tool_name"] == "bash"
    assert by_type["tool_result"]["output"] == "hi"
    assert by_type["tool_result"]["is_error"] is False
