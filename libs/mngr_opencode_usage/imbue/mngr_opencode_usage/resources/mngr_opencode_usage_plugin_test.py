"""Behavioural test for the OpenCode usage writer plugin (mngr_opencode_usage_plugin.ts).

Loads the real ``.ts`` under Node, replays a synthetic OpenCode event stream
through its ``event`` hook, and asserts on the cost_snapshot events it appends.
Skipped automatically when Node (with TypeScript support) is unavailable; the
``.ts`` is a resource, not Python, so it does not count toward coverage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_NAME = "mngr_opencode_usage_plugin.ts"
_PLUGIN_PATH = Path(__file__).parent / _PLUGIN_NAME

_DRIVER_MJS = """
import { readFileSync } from "node:fs";
const events = JSON.parse(readFileSync(process.argv[2], "utf8"));
const mod = await import("./mngr_opencode_usage_plugin.ts");
const hooks = await mod.MngrUsagePlugin({});
if (typeof hooks.event === "function") {
  for (const event of events) {
    await hooks.event({ event });
  }
}
"""

_USAGE_EVENTS = Path("events") / "opencode" / "usage" / "events.jsonl"


def _node_supports_typescript(node: str, work_dir: Path) -> bool:
    probe_ts = work_dir / "probe.ts"
    probe_ts.write_text("export const value: number = 1;\n")
    probe_mjs = work_dir / "probe.mjs"
    probe_mjs.write_text("const m = await import('./probe.ts'); process.exit(m.value === 1 ? 0 : 1);\n")
    result = subprocess.run([node, str(probe_mjs)], capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def _run_plugin(tmp_path: Path, events: list[dict[str, Any]], *, role: str = "server") -> Path:
    """Run the writer plugin over ``events`` under a fresh state dir; return the state dir."""
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
            "MNGR_OPENCODE_ROLE": role,
        },
    )
    assert result.returncode == 0, f"plugin driver failed:\n{result.stdout}\n{result.stderr}"
    return state_dir


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _assistant_message_event(
    message_id: str, session_id: str, *, cost: float, tokens: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "message.updated",
        "properties": {
            "info": {
                "id": message_id,
                "role": "assistant",
                "sessionID": session_id,
                "providerID": "anthropic",
                "modelID": "claude-opus-4-8",
                "cost": cost,
                "tokens": tokens,
            }
        },
    }


def test_writer_emits_one_cost_snapshot_per_assistant_message(tmp_path: Path) -> None:
    events = [
        _assistant_message_event(
            "m1",
            "ses_root",
            cost=0.0123,
            tokens={"input": 10, "output": 20, "reasoning": 5, "cache": {"read": 100, "write": 7}},
        ),
    ]
    state_dir = _run_plugin(tmp_path, events)
    records = _read_jsonl(state_dir / _USAGE_EVENTS)
    assert len(records) == 1
    record = records[0]
    assert record["source"] == "opencode/usage"
    assert record["type"] == "cost_snapshot"
    assert record["session_id"] == "ses_root"
    assert record["message_id"] == "m1"
    assert record["cost"] == {"total_cost_usd": 0.0123}
    assert record["model"] == "anthropic/claude-opus-4-8"
    assert record["cost_mode"] == "API_KEY"
    # Reasoning folds into output (20 + 5); cache buckets map straight across.
    assert record["tokens"] == {"input": 10, "output": 25, "cache_read": 100, "cache_creation": 7}
    assert record["event_id"].startswith("evt-")


def test_writer_ignores_user_messages_and_non_message_events(tmp_path: Path) -> None:
    events = [
        {"type": "session.created", "properties": {"info": {"id": "ses_root"}}},
        {
            "type": "message.updated",
            "properties": {"info": {"id": "u1", "role": "user", "sessionID": "ses_root"}},
        },
    ]
    state_dir = _run_plugin(tmp_path, events)
    assert not (state_dir / _USAGE_EVENTS).exists()


def test_writer_is_inert_outside_the_server_role(tmp_path: Path) -> None:
    events = [_assistant_message_event("m1", "ses_root", cost=0.5, tokens={"input": 1, "output": 1})]
    state_dir = _run_plugin(tmp_path, events, role="client")
    assert not (state_dir / _USAGE_EVENTS).exists()
