"""Behavioural test for the opencode lifecycle plugin (resources/mngr_opencode_plugin.ts).

The plugin runs in-process on the ``opencode serve`` Node process and owns the
``active`` marker plus the raw and common transcripts. We exercise it the way the
pi extension is exercised: load the real ``.ts`` under Node, hand it a synthetic
stream of OpenCode events, and assert on the files it writes. The conversion logic
moved into TypeScript (so it no longer runs in CI as Python), making this the only
CI-runnable check that opencode's emitter still produces the canonical ATIF-shaped
records -- without it, emitter drift would surface only in the (non-CI) release test.

Skipped automatically when Node (with TypeScript support) is unavailable, e.g. a CI
sandbox without it -- the ``.ts`` is a resource, not Python, so it does not count
toward coverage.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_opencode.opencode_config import PERMISSIONS_WAITING_FILENAME

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
# assistant message (with a reasoning part and a completed bash tool call) stream
# in, and the session goes idle -- which is what triggers the common-transcript
# rebuild.
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
        "properties": {"part": {"id": "p0", "messageID": "m2", "type": "reasoning", "text": "counting is easy"}},
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

_EMITTER = "opencode/common_transcript"

# The id of the bash tool part inside _TURN_EVENTS, so a variant turn can swap in a
# differently-shaped tool state without restating the whole event stream.
_TOOL_PART_ID = "p3"


def _turn_with_tool_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """``_TURN_EVENTS`` with the bash tool part's ``state`` replaced by ``state``."""

    def replaced(event: dict[str, Any]) -> dict[str, Any]:
        part = event["properties"].get("part")
        if part is None or part["id"] != _TOOL_PART_ID:
            return event
        return {"type": event["type"], "properties": {"part": {**part, "state": state}}}

    return [replaced(event) for event in _TURN_EVENTS]


def test_emitted_common_records_conform_to_canonical_schema(tmp_path: Path) -> None:
    """Every record the plugin emits must validate against the shared record schema.

    Guards against the opencode emitter (resources/mngr_opencode_plugin.ts) and the
    canonical schema (imbue.mngr.agents.common_transcript_records) drifting apart.
    """
    state = _run_plugin(tmp_path, _TURN_EVENTS)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    record_types = {r["type"] for r in records}
    assert record_types <= {"header", "step", "observation"}
    # The fixture drives all three, so an empty or degenerate stream cannot pass.
    assert record_types == {"header", "step", "observation"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_stream_opens_with_the_atif_header(tmp_path: Path) -> None:
    state = _run_plugin(tmp_path, _TURN_EVENTS)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    # The plugin derives the header id from the state-dir basename (the agent id).
    header_digest = hashlib.sha256(f"{state.name}:{_EMITTER}".encode()).hexdigest()[:32]
    assert records[0] == {
        "type": "header",
        "event_id": f"header-{header_digest}",
        "emitter": _EMITTER,
        "schema_version": "ATIF-v1.7",
    }


def test_header_is_written_exactly_once_per_rebuild(tmp_path: Path) -> None:
    """The plugin rewrites the whole stream on every idle, so two idles must not
    leave two headers (nor duplicate any record)."""
    events = _TURN_EVENTS + [{"type": "session.idle", "properties": {"sessionID": "ses_root"}}]
    state = _run_plugin(tmp_path, events)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert [r["type"] for r in records].count("header") == 1
    assert len({r["event_id"] for r in records}) == len(records)


def test_user_and_assistant_turns_become_atif_steps(tmp_path: Path) -> None:
    """Sanity check on the conversion itself (the marker/transcript single-writer path)."""
    state = _run_plugin(tmp_path, _TURN_EVENTS)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    steps = [r for r in records if r["type"] == "step"]
    user_step, agent_step = steps
    assert user_step["source"] == "user"
    assert user_step["message"] == "Count slowly"
    assert user_step["extra"] == {"conversation_id": "ses_root", "message_id": "m1"}
    assert agent_step["source"] == "agent"
    assert agent_step["message"] == "one two three"
    assert agent_step["model_name"] == "opencode/deepseek-v4-flash-free"
    assert agent_step["reasoning_content"] == "counting is easy"
    assert agent_step["extra"]["finish_reason"] == "stop"
    # opencode reports no per-message usage, so no metrics are claimed.
    assert "metrics" not in agent_step
    assert agent_step["tool_calls"] == [
        {"tool_call_id": "call_1", "function_name": "bash", "arguments": {"command": "echo hi"}}
    ]
    assert all(r["emitter"] == _EMITTER for r in records)


def test_tool_results_become_observation_records(tmp_path: Path) -> None:
    state = _run_plugin(tmp_path, _TURN_EVENTS)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (observation,) = [r for r in records if r["type"] == "observation"]
    assert observation["results"] == [
        {
            "source_call_id": "call_1",
            "content": "hi",
            # The result repeats the step's provenance, so it stands on its own.
            "extra": {
                "conversation_id": "ses_root",
                "message_id": "m2",
                "is_error": False,
                "tool_name": "bash",
            },
        }
    ]


def test_errored_tool_result_is_flagged_on_the_result_extra(tmp_path: Path) -> None:
    events = _turn_with_tool_state({"status": "error", "input": {"command": "false"}, "error": "boom"})
    state = _run_plugin(tmp_path, events)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (observation,) = [r for r in records if r["type"] == "observation"]
    assert observation["results"][0]["content"] == "boom"
    assert observation["results"][0]["extra"] == {
        "conversation_id": "ses_root",
        "message_id": "m2",
        "is_error": True,
        "tool_name": "bash",
    }


# Fidelity: ATIF streams carry complete tool arguments and untruncated outputs.
# Display truncation is the reader's job, so nothing here may shorten them.
_LONG_COMMAND = "echo " + "a" * 500
_LONG_OUTPUT = "b" * 5000


def test_tool_arguments_and_output_are_never_truncated(tmp_path: Path) -> None:
    events = _turn_with_tool_state(
        {"status": "completed", "input": {"command": _LONG_COMMAND}, "output": _LONG_OUTPUT}
    )
    state = _run_plugin(tmp_path, events)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step" and r["source"] == "agent"]
    assert agent_step["tool_calls"][0]["arguments"] == {"command": _LONG_COMMAND}
    (observation,) = [r for r in records if r["type"] == "observation"]
    assert observation["results"][0]["content"] == _LONG_OUTPUT
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_non_object_tool_input_is_preserved_under_raw(tmp_path: Path) -> None:
    """ATIF requires an arguments object; a native non-object is wrapped, not dropped."""
    events = _turn_with_tool_state({"status": "completed", "input": "not-json", "output": "hi"})
    state = _run_plugin(tmp_path, events)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step" and r["source"] == "agent"]
    assert agent_step["tool_calls"][0]["arguments"] == {"_raw": "not-json"}


def test_empty_tool_input_becomes_an_empty_arguments_object(tmp_path: Path) -> None:
    """An absent or empty native payload means "no arguments", not a raw empty string."""
    events = _turn_with_tool_state({"status": "completed", "input": "   ", "output": "hi"})
    state = _run_plugin(tmp_path, events)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step" and r["source"] == "agent"]
    assert agent_step["tool_calls"][0]["arguments"] == {}


def test_image_parts_become_the_text_placeholder(tmp_path: Path) -> None:
    events = [
        {"type": "session.created", "properties": {"info": {"id": "ses_root"}}},
        {
            "type": "message.updated",
            "properties": {"info": {"id": "m1", "role": "user", "sessionID": "ses_root", "time": {"created": 1000}}},
        },
        {
            "type": "message.part.updated",
            "properties": {"part": {"id": "p1", "messageID": "m1", "type": "text", "text": "look: "}},
        },
        {
            "type": "message.part.updated",
            "properties": {
                "part": {"id": "p2", "messageID": "m1", "type": "file", "mime": "image/png", "url": "data:..."}
            },
        },
        {"type": "session.idle", "properties": {"sessionID": "ses_root"}},
    ]
    state = _run_plugin(tmp_path, events)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (user_step,) = [r for r in records if r["type"] == "step"]
    assert user_step["message"] == "look: [image omitted]"


def test_message_without_a_creation_time_gets_the_epoch_timestamp(tmp_path: Path) -> None:
    """ATIF requires an ISO 8601 timestamp, so a message with no ``time.created``
    falls back to the epoch -- stable across rebuilds, unlike "now"."""
    events = [
        {"type": "session.created", "properties": {"info": {"id": "ses_root"}}},
        {"type": "message.updated", "properties": {"info": {"id": "m1", "role": "user", "sessionID": "ses_root"}}},
        {
            "type": "message.part.updated",
            "properties": {"part": {"id": "p1", "messageID": "m1", "type": "text", "text": "no clock"}},
        },
        {"type": "session.idle", "properties": {"sessionID": "ses_root"}},
    ]
    state = _run_plugin(tmp_path, events)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (user_step,) = [r for r in records if r["type"] == "step"]
    assert user_step["timestamp"] == "1970-01-01T00:00:00Z"
    assert validate_common_transcript_record(user_step) is None


# Permission events: the running opencode server (verified against the 1.16.2
# binary) emits `permission.asked` (carrying the request `id`) when a tool blocks on
# approval, and `permission.replied` (carrying `requestID`) when it is answered. The
# `@opencode-ai/sdk` type stubs disagree -- naming them `permission.updated` and
# `permissionID` -- so the plugin accepts both; the alias is covered below. The
# marker is present iff some request id is still pending.
def _permission_ask(request_id: str, session_id: str = "ses_root") -> dict[str, Any]:
    return {
        "type": "permission.asked",
        "properties": {
            "id": request_id,
            "type": "edit",
            "sessionID": session_id,
            "messageID": "m1",
            "title": "Edit file",
            "metadata": {},
            "time": {"created": 1000},
        },
    }


def _permission_reply(request_id: str, session_id: str = "ses_root") -> dict[str, Any]:
    return {
        "type": "permission.replied",
        "properties": {"sessionID": session_id, "requestID": request_id, "reply": "once"},
    }


def _marker_present(state: Path) -> bool:
    return (state / PERMISSIONS_WAITING_FILENAME).exists()


def test_permission_ask_creates_waiting_marker(tmp_path: Path) -> None:
    state = _run_plugin(tmp_path, [_permission_ask("perm_1")])
    assert _marker_present(state)


def test_permission_reply_clears_waiting_marker(tmp_path: Path) -> None:
    state = _run_plugin(tmp_path, [_permission_ask("perm_1"), _permission_reply("perm_1")])
    assert not _marker_present(state)


def test_marker_persists_while_any_permission_still_pending(tmp_path: Path) -> None:
    """Two concurrent prompts; replying to one leaves the marker present for the other."""
    state = _run_plugin(
        tmp_path,
        [_permission_ask("perm_1"), _permission_ask("perm_2", "ses_child"), _permission_reply("perm_1")],
    )
    assert _marker_present(state)


def test_sdk_stub_event_names_are_also_handled(tmp_path: Path) -> None:
    """The `@opencode-ai/sdk` stubs name the events `permission.updated` /
    `permissionID`; the plugin accepts those too (opencode self-upgrades and the
    stubs and binary disagree), so a future build using them keeps working."""
    state = _run_plugin(
        tmp_path,
        [
            {"type": "permission.updated", "properties": {"id": "perm_1", "sessionID": "ses_root"}},
            {"type": "permission.replied", "properties": {"sessionID": "ses_root", "permissionID": "perm_1"}},
        ],
    )
    assert not _marker_present(state)


def test_root_idle_clears_stranded_waiting_marker(tmp_path: Path) -> None:
    """Safety net: a prompt stranded without a reply is cleared when the root turn ends."""
    state = _run_plugin(
        tmp_path,
        [
            {"type": "session.created", "properties": {"info": {"id": "ses_root"}}},
            _permission_ask("perm_1"),
            {"type": "session.idle", "properties": {"sessionID": "ses_root"}},
        ],
    )
    assert not _marker_present(state)


def test_startup_clears_stranded_waiting_marker(tmp_path: Path) -> None:
    """A marker left on disk by a prior killed/crashed server is cleared at startup.

    A fresh server has no pending prompts (the in-memory set is the authority), so
    any on-disk marker is stale -- e.g. after `mngr stop`/`start` while blocked. The
    plugin clears it on init, before any event, so the stale marker can't falsely
    read PERMISSIONS once the next turn sets `active`.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / PERMISSIONS_WAITING_FILENAME).write_text("")
    # _run_plugin runs the plugin factory (which clears the stale marker on init)
    # even though we replay no events.
    returned_state = _run_plugin(tmp_path, [])
    assert returned_state == state_dir
    assert not _marker_present(returned_state)
