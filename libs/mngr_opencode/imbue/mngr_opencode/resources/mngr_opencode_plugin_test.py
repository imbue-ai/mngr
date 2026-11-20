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
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr.hosts.background_tasks import TURN_END_FILE_NAME
from imbue.mngr.primitives import TurnEndKind
from imbue.mngr.utils.testing import read_turn_end_in
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


def _run_plugin(tmp_path: Path, events: list[dict[str, Any]], driver: str = _DRIVER_MJS) -> Path:
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
    driver_path.write_text(driver)
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


def _permission_reply(request_id: str, session_id: str = "ses_root", reply: str = "once") -> dict[str, Any]:
    return {
        "type": "permission.replied",
        "properties": {"sessionID": session_id, "requestID": request_id, "reply": reply},
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


# Turn end. Each sequence is what opencode 1.19.5 publishes to plugins, in order.
_ROOT = "ses_root"
_CHILD = "ses_child"
_PROMPT_ID = "msg_prompt"
_REPLY_ID = "msg_reply"
_PROVIDER_ID = "opencode"
_MODEL_ID = "deepseek-v4.1-flash-free"
_EARLIER_TURN_END_LINE = "ended:1792575895 kind:FAILED native_type:APIError\n"
_PERMISSION_REJECTED_MESSAGE = "The user rejected permission to use this specific tool call."
_QUESTION_REJECTED_MESSAGE = "The user dismissed this question"
_ABORTED_ERROR = {"name": "MessageAbortedError", "data": {"message": "Aborted"}}
_API_ERROR = {
    "name": "APIError",
    "data": {"message": "Internal server error", "statusCode": 500, "isRetryable": False},
}
_AUTH_ERROR = {"name": "ProviderAuthError", "data": {"providerID": _PROVIDER_ID, "message": "Missing API key"}}
_MODEL_NOT_FOUND_ERROR = {"name": "UnknownError", "data": {"message": "Model not found: opencode/missing-model."}}
_CONTEXT_OVERFLOW_ERROR = {"name": "ContextOverflowError", "data": {"message": "prompt is too long"}}
_TASK_INPUT = {"prompt": "Find the README", "description": "Find the README", "subagent_type": "general"}


def _session_created(session_id: str, parent_id: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": session_id,
        "slug": session_id,
        "projectID": "prj_work",
        "directory": "/work",
        "title": "New session",
        "version": "1.19.5",
        "time": {"created": 500, "updated": 500},
    }
    if parent_id is not None:
        info["parentID"] = parent_id
    return {"type": "session.created", "properties": {"sessionID": session_id, "info": info}}


def _went_busy(session_id: str = _ROOT) -> dict[str, Any]:
    return {"type": "session.status", "properties": {"sessionID": session_id, "status": {"type": "busy"}}}


def _went_retrying() -> dict[str, Any]:
    status = {"type": "retry", "attempt": 1, "message": "Overloaded", "next": 2000}
    return {"type": "session.status", "properties": {"sessionID": _ROOT, "status": status}}


def _went_idle(session_id: str = _ROOT) -> list[dict[str, Any]]:
    return [
        {"type": "session.status", "properties": {"sessionID": session_id, "status": {"type": "idle"}}},
        {"type": "session.idle", "properties": {"sessionID": session_id}},
    ]


def _message_updated(info: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message.updated", "properties": {"sessionID": info["sessionID"], "info": info}}


def _prompt(message_id: str = _PROMPT_ID, created: int = 1000, session_id: str = _ROOT) -> dict[str, Any]:
    return _message_updated(
        {
            "id": message_id,
            "sessionID": session_id,
            "role": "user",
            "time": {"created": created},
            "agent": "build",
            "model": {"providerID": _PROVIDER_ID, "modelID": _MODEL_ID},
        }
    )


def _reply(
    message_id: str = _REPLY_ID, created: int = 2000, parent_id: str = _PROMPT_ID, **fields: Any
) -> dict[str, Any]:
    return _message_updated(
        {
            "id": message_id,
            "sessionID": _ROOT,
            "role": "assistant",
            "time": {"created": created},
            "parentID": parent_id,
            "modelID": _MODEL_ID,
            "providerID": _PROVIDER_ID,
            "mode": "build",
            "agent": "build",
            "path": {"cwd": "/work", "root": "/work"},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            **fields,
        }
    )


def _completed_at(created: int) -> dict[str, int]:
    return {"created": created, "completed": created + 500}


def _step(
    message_id: str, created: int, finish: str, *events: dict[str, Any], parent_id: str = _PROMPT_ID, **fields: Any
) -> list[dict[str, Any]]:
    """One model call: the reply is created, the call goes busy, ``events`` stream, then the finish and completion land."""
    return [
        _reply(message_id, created, parent_id, **fields),
        _went_busy(),
        *events,
        _reply(message_id, created, parent_id, finish=finish, **fields),
        _reply(message_id, created, parent_id, finish=finish, time=_completed_at(created), **fields),
    ]


def _tool_part(
    part_id: str, state: dict[str, Any], message_id: str = _REPLY_ID, session_id: str = _ROOT, **fields: Any
) -> dict[str, Any]:
    part = {
        "id": part_id,
        "sessionID": session_id,
        "messageID": message_id,
        "type": "tool",
        "callID": f"call_{part_id}",
        "tool": "bash",
        "state": state,
        **fields,
    }
    return {"type": "message.part.updated", "properties": {"sessionID": session_id, "part": part, "time": 2100}}


def _running(**fields: Any) -> dict[str, Any]:
    return {"status": "running", "input": {"command": "ls"}, "time": {"start": 2100}, **fields}


def _completed(**fields: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "input": {"command": "ls"},
        "output": "README.md\n",
        "title": "ls",
        "metadata": {},
        "time": {"start": 2100, "end": 2200},
        **fields,
    }


def _failed(error: str, **fields: Any) -> dict[str, Any]:
    return {
        "status": "error",
        "input": {"command": "ls"},
        "error": error,
        "time": {"start": 2100, "end": 2200},
        **fields,
    }


def _session_error(error: dict[str, Any], session_id: str = _ROOT) -> dict[str, Any]:
    return {"type": "session.error", "properties": {"sessionID": session_id, "error": error}}


def _permission_asked(part_id: str, message_id: str = _REPLY_ID, session_id: str = _ROOT) -> dict[str, Any]:
    request = {
        "id": "per_1",
        "sessionID": session_id,
        "permission": "bash",
        "patterns": ["ls"],
        "metadata": {},
        "always": ["ls *"],
        "tool": {"messageID": message_id, "callID": f"call_{part_id}"},
    }
    return {"type": "permission.asked", "properties": request}


def _permission_rejected(session_id: str = _ROOT) -> dict[str, Any]:
    return _permission_reply("per_1", session_id, reply="reject")


def _question_asked(part_id: str, message_id: str = _REPLY_ID, session_id: str = _ROOT) -> dict[str, Any]:
    question = {
        "question": "Which file?",
        "header": "File",
        "options": [{"label": "a.txt", "description": "The first"}],
    }
    request = {
        "id": "que_1",
        "sessionID": session_id,
        "questions": [question],
        "tool": {"messageID": message_id, "callID": f"call_{part_id}"},
    }
    return {"type": "question.asked", "properties": request}


def _question_rejected(session_id: str = _ROOT) -> dict[str, Any]:
    return {"type": "question.rejected", "properties": {"sessionID": session_id, "requestID": "que_1"}}


def _compaction_prompt(message_id: str, created: int, is_auto: bool) -> list[dict[str, Any]]:
    part = {
        "id": f"prt_{message_id}",
        "sessionID": _ROOT,
        "messageID": message_id,
        "type": "compaction",
        "auto": is_auto,
    }
    return [
        _prompt(message_id, created),
        {"type": "message.part.updated", "properties": {"sessionID": _ROOT, "part": part, "time": created}},
    ]


_COMPACTED: dict[str, Any] = {"type": "session.compacted", "properties": {"sessionID": _ROOT}}


_TURN_OPENING: list[dict[str, Any]] = [_session_created(_ROOT), _prompt(), _went_busy()]
# The loop's next iteration goes busy, finds the last reply final and leaves the loop.
_LOOP_EXIT: list[dict[str, Any]] = [_went_busy(), *_went_idle()]


def _child_reply(**fields: Any) -> dict[str, Any]:
    return _reply("msg_child_reply", 2150, "msg_child_prompt", sessionID=_CHILD, **fields)


def _subtask_stopped(*child_events: dict[str, Any]) -> list[dict[str, Any]]:
    """A stop during a subtask whose child session has emitted ``child_events``; the reply stays `tool-calls`."""
    return [
        *_TURN_OPENING,
        _reply(agent="general", mode="general"),
        _tool_part("prt_task", _running(input=_TASK_INPUT), tool="task"),
        _session_created(_CHILD, _ROOT),
        _prompt("msg_child_prompt", 2100, _CHILD),
        _went_busy(_CHILD),
        _child_reply(),
        _went_busy(_CHILD),
        *child_events,
        _reply(agent="general", mode="general", finish="tool-calls", time=_completed_at(2000)),
        _tool_part("prt_task", _failed("Cancelled", input=_TASK_INPUT), tool="task"),
        *_went_idle(),
    ]


def _kind_and_native_type(state: Path) -> tuple[TurnEndKind, str] | None:
    turn_end = read_turn_end_in(state)
    return None if turn_end is None else (turn_end.kind, turn_end.native_type)


def _seed_earlier_turn_end(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / TURN_END_FILE_NAME).write_text(_EARLIER_TURN_END_LINE)


def test_a_root_turn_ending_on_a_final_reply_records_completed_with_its_finish(tmp_path: Path) -> None:
    started_at = datetime.now(timezone.utc)
    state = _run_plugin(
        tmp_path,
        [
            *_TURN_OPENING,
            *_step(
                "msg_tool_step", 2000, "tool-calls", _tool_part("prt_ls", _completed(), message_id="msg_tool_step")
            ),
            _went_busy(),
            *_step(_REPLY_ID, 3000, "stop"),
            *_LOOP_EXIT,
        ],
    )
    turn_end = read_turn_end_in(state)
    assert turn_end is not None
    assert (turn_end.kind, turn_end.native_type) == (TurnEndKind.COMPLETED, "stop")
    assert started_at - timedelta(seconds=1) <= turn_end.ended_at <= datetime.now(timezone.utc)


@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        pytest.param(_ABORTED_ERROR, TurnEndKind.INTERRUPTED, id="aborted"),
        pytest.param(_API_ERROR, TurnEndKind.FAILED, id="api-error"),
        pytest.param(_AUTH_ERROR, TurnEndKind.FAILED, id="auth-error"),
    ],
)
def test_a_root_session_error_ending_the_turn_records_its_kind_and_name(
    tmp_path: Path, error: dict[str, Any], expected_kind: TurnEndKind
) -> None:
    """opencode publishes the error and goes idle, then saves the error on the reply and goes idle again."""
    state = _run_plugin(
        tmp_path,
        [
            *_TURN_OPENING,
            _reply(),
            _went_busy(),
            _session_error(error),
            *_went_idle(),
            _reply(error=error, time=_completed_at(2000)),
            *_went_idle(),
        ],
    )
    assert _kind_and_native_type(state) == (expected_kind, error["name"])


def test_a_failure_before_the_first_reply_records_failed(tmp_path: Path) -> None:
    """A model opencode cannot find fails the turn with an UnknownError before any reply exists."""
    state = _run_plugin(tmp_path, [*_TURN_OPENING, _session_error(_MODEL_NOT_FOUND_ERROR), *_went_idle()])
    assert _kind_and_native_type(state) == (TurnEndKind.FAILED, "UnknownError")


def test_a_reply_saved_with_an_abort_error_records_interrupted(tmp_path: Path) -> None:
    """A stop before the model call saves MessageAbortedError on the reply without publishing a session error."""
    state = _run_plugin(
        tmp_path,
        [*_TURN_OPENING, _reply(), _reply(error=_ABORTED_ERROR, time=_completed_at(2000)), *_went_idle()],
    )
    assert _kind_and_native_type(state) == (TurnEndKind.INTERRUPTED, "MessageAbortedError")


def test_a_final_reply_with_finish_error_records_failed(tmp_path: Path) -> None:
    """A provider can end a reply with finish `error` and no error object, e.g. gemini's MALFORMED_FUNCTION_CALL."""
    state = _run_plugin(tmp_path, [*_TURN_OPENING, *_step(_REPLY_ID, 2000, "error"), *_LOOP_EXIT])
    assert _kind_and_native_type(state) == (TurnEndKind.FAILED, "error")


def test_tool_calls_opencode_does_not_answer_leave_the_reply_final(tmp_path: Path) -> None:
    """After a retry, cleanup marks the first attempt's tool call interrupted; a provider-executed call needs no answer."""
    state = _run_plugin(
        tmp_path,
        [
            *_TURN_OPENING,
            *_step(
                _REPLY_ID,
                2000,
                "stop",
                _tool_part("prt_orphan", _running()),
                _went_retrying(),
                _tool_part("prt_provider", _completed(), metadata={"providerExecuted": True}),
                _tool_part("prt_orphan", _failed("Tool execution aborted", metadata={"interrupted": True})),
            ),
            *_LOOP_EXIT,
        ],
    )
    assert _kind_and_native_type(state) == (TurnEndKind.COMPLETED, "stop")


@pytest.mark.parametrize("finish", ["tool-calls", "stop"])
@pytest.mark.parametrize(
    ("asked", "rejected", "tool_error", "error_name"),
    [
        pytest.param(
            _permission_asked("prt_declined"),
            _permission_rejected(),
            _PERMISSION_REJECTED_MESSAGE,
            "PermissionRejectedError",
            id="permission",
        ),
        pytest.param(
            _question_asked("prt_declined"),
            _question_rejected(),
            _QUESTION_REJECTED_MESSAGE,
            "QuestionRejectedError",
            id="question",
        ),
    ],
)
def test_a_declined_root_tool_ending_the_turn_records_interrupted(
    tmp_path: Path,
    asked: dict[str, Any],
    rejected: dict[str, Any],
    tool_error: str,
    error_name: str,
    finish: str,
) -> None:
    """opencode stops the turn on a declined tool; most providers report the reply's finish as `tool-calls`, some `stop`."""
    state = _run_plugin(
        tmp_path,
        [
            *_TURN_OPENING,
            *_step(
                _REPLY_ID,
                2000,
                finish,
                _tool_part("prt_declined", _running()),
                asked,
                rejected,
                _tool_part("prt_declined", _failed(tool_error)),
            ),
            *_went_idle(),
        ],
    )
    assert _kind_and_native_type(state) == (TurnEndKind.INTERRUPTED, error_name)


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(
            _subtask_stopped(
                _session_error(_ABORTED_ERROR, _CHILD),
                *_went_idle(_CHILD),
                _child_reply(error=_ABORTED_ERROR, time=_completed_at(2150)),
                *_went_idle(_CHILD),
            ),
            id="stop-during-a-subtask",
        ),
        pytest.param(
            _subtask_stopped(
                _tool_part("prt_child_bash", _running(), "msg_child_reply", _CHILD),
                _tool_part("prt_child_question", _running(input={}), "msg_child_reply", _CHILD, tool="question"),
                _permission_asked("prt_child_bash", "msg_child_reply", _CHILD),
                _question_asked("prt_child_question", "msg_child_reply", _CHILD),
                _permission_rejected(_CHILD),
                _question_rejected(_CHILD),
                _tool_part("prt_child_bash", _failed(_PERMISSION_REJECTED_MESSAGE), "msg_child_reply", _CHILD),
                _tool_part(
                    "prt_child_question",
                    _failed(_QUESTION_REJECTED_MESSAGE, input={}),
                    "msg_child_reply",
                    _CHILD,
                    tool="question",
                ),
                _child_reply(finish="tool-calls", time=_completed_at(2150)),
                *_went_idle(_CHILD),
            ),
            id="stop-during-a-subtask-after-its-session-declined-tools",
        ),
        pytest.param(
            [
                *_TURN_OPENING,
                *_step(_REPLY_ID, 2000, "tool-calls", _tool_part("prt_ls", _completed())),
                _went_busy(),
                *_went_idle(),
            ],
            id="stop-between-steps",
        ),
        pytest.param(
            [*_TURN_OPENING, *_step(_REPLY_ID, 2000, "unknown"), _went_busy(), *_went_idle()],
            id="stop-after-finish-unknown",
        ),
        pytest.param(
            [
                *_TURN_OPENING,
                *_step(_REPLY_ID, 2000, "stop", _tool_part("prt_ls", _completed())),
                _went_busy(),
                *_went_idle(),
            ],
            id="stop-after-a-final-finish-with-a-tool-call-to-answer",
        ),
        pytest.param(
            [
                _session_created(_ROOT),
                _prompt("msg_earlier_prompt", 100),
                _reply("msg_earlier_reply", 200, "msg_earlier_prompt", finish="stop", time=_completed_at(200)),
                _prompt(),
                _went_busy(),
                *_went_idle(),
            ],
            id="stop-before-the-first-reply",
        ),
        pytest.param(
            [
                *_TURN_OPENING,
                *_step(
                    "msg_tool_step",
                    2000,
                    "tool-calls",
                    _tool_part("prt_declined", _running(), message_id="msg_tool_step"),
                    _permission_asked("prt_declined", "msg_tool_step"),
                    _permission_rejected(),
                    _tool_part(
                        "prt_declined",
                        _failed(f"{_PERMISSION_REJECTED_MESSAGE[:-1]} with the following feedback: use git ls-files"),
                        message_id="msg_tool_step",
                    ),
                ),
                _went_busy(),
                *_step(_REPLY_ID, 3000, "tool-calls"),
                _went_busy(),
                *_went_idle(),
            ],
            id="declined-with-feedback-in-an-earlier-step",
        ),
        pytest.param(
            [
                _session_created(_ROOT),
                _went_busy(),
                _prompt("msg_shell_prompt", 1000),
                _reply("msg_shell_reply", 1100, "msg_shell_prompt"),
                _tool_part("prt_shell", _running(), message_id="msg_shell_reply"),
                _reply("msg_shell_reply", 1100, "msg_shell_prompt", time=_completed_at(1100)),
                _tool_part("prt_shell", _completed(), message_id="msg_shell_reply"),
                *_went_idle(),
            ],
            id="shell-command",
        ),
    ],
)
def test_a_turn_without_evidence_of_how_it_ended_removes_the_last_turn_end_and_records_nothing(
    tmp_path: Path, events: list[dict[str, Any]]
) -> None:
    _seed_earlier_turn_end(tmp_path)
    state = _run_plugin(tmp_path, events)
    assert not (state / TURN_END_FILE_NAME).exists()


def test_a_compaction_records_the_end_of_its_own_turn(tmp_path: Path) -> None:
    """`/compact` adds a compaction prompt and runs the loop, which replies with a summary."""
    _seed_earlier_turn_end(tmp_path)
    state = _run_plugin(
        tmp_path,
        [
            _session_created(_ROOT),
            *_compaction_prompt("msg_compaction", 1000, is_auto=False),
            _went_busy(),
            *_step(
                "msg_summary",
                2000,
                "stop",
                parent_id="msg_compaction",
                mode="compaction",
                agent="compaction",
                summary=True,
            ),
            _COMPACTED,
            *_LOOP_EXIT,
        ],
    )
    assert _kind_and_native_type(state) == (TurnEndKind.COMPLETED, "stop")


# Records, at each removal of the `active` marker, whether `turn_end` exists yet.
_ACTIVE_CLEAR_PROBE_MJS = (
    """
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
const stateDir = process.env.MNGR_AGENT_STATE_DIR;
const rmSync = fs.rmSync;
fs.rmSync = (path, options) => {
  if (path === `${stateDir}/active`) {
    fs.appendFileSync(`${stateDir}/../turn_end_at_active_clear`, `${fs.existsSync(`${stateDir}/turn_end`)}\\n`);
  }
  return rmSync(path, options);
};
syncBuiltinESMExports();
"""
    + _DRIVER_MJS
)


def test_the_turn_end_is_written_before_active_is_cleared(tmp_path: Path) -> None:
    _run_plugin(
        tmp_path,
        [*_TURN_OPENING, *_step(_REPLY_ID, 2000, "stop"), *_LOOP_EXIT],
        driver=_ACTIVE_CLEAR_PROBE_MJS,
    )
    assert (tmp_path / "turn_end_at_active_clear").read_text().splitlines() == ["true", "true"]


def test_the_turn_end_line_encodes_its_native_type_as_the_record_format_requires(tmp_path: Path) -> None:
    """No opencode 1.19.5 name needs encoding, so this name is made up to pin the record format's encoding."""
    error = {"name": "Error (retry)", "data": {"message": "failed"}}
    state = _run_plugin(tmp_path, [*_TURN_OPENING, _session_error(error), *_went_idle()])
    assert (state / TURN_END_FILE_NAME).read_text().split(" ", 1)[1] == "kind:FAILED native_type:Error%20%28retry%29\n"


def test_a_failed_turn_end_write_leaves_no_staging_file(tmp_path: Path) -> None:
    """A `turn_end` the plugin cannot replace (here a directory) fails the rename, and the staged line is removed."""
    state_dir = tmp_path / "state"
    (state_dir / TURN_END_FILE_NAME / "occupied").mkdir(parents=True)
    _run_plugin(tmp_path, [*_TURN_OPENING, *_step(_REPLY_ID, 2000, "stop"), *_LOOP_EXIT])
    assert [path.name for path in state_dir.iterdir() if path.name.endswith(".tmp")] == []


def test_a_session_error_the_turn_recovers_from_does_not_end_it(tmp_path: Path) -> None:
    """opencode compacts after a ContextOverflowError and goes busy again rather than idle."""
    state = _run_plugin(
        tmp_path,
        [
            *_TURN_OPENING,
            _reply("msg_overflowed", 2000),
            _went_busy(),
            _session_error(_CONTEXT_OVERFLOW_ERROR),
            _reply("msg_overflowed", 2000, time=_completed_at(2000)),
            *_compaction_prompt("msg_compaction", 2600, is_auto=True),
            _went_busy(),
            *_step(
                "msg_summary",
                2700,
                "stop",
                parent_id="msg_compaction",
                mode="compaction",
                agent="compaction",
                summary=True,
            ),
            _prompt("msg_continue", 3300),
            _COMPACTED,
            _went_busy(),
            *_step(_REPLY_ID, 3400, "stop", parent_id="msg_continue"),
            *_LOOP_EXIT,
        ],
    )
    assert _kind_and_native_type(state) == (TurnEndKind.COMPLETED, "stop")


def test_a_child_session_neither_ends_nor_labels_the_root_turn(tmp_path: Path) -> None:
    state = _run_plugin(
        tmp_path,
        [
            *_TURN_OPENING,
            *_step(
                "msg_task_step",
                2000,
                "tool-calls",
                _tool_part("prt_task", _running(input=_TASK_INPUT), message_id="msg_task_step", tool="task"),
                _session_created(_CHILD, _ROOT),
                _prompt("msg_child_prompt", 2100, _CHILD),
                _went_busy(_CHILD),
                _child_reply(),
                _went_busy(_CHILD),
                _session_error(_API_ERROR, _CHILD),
                *_went_idle(_CHILD),
                _child_reply(error=_API_ERROR, time=_completed_at(2150)),
                *_went_idle(_CHILD),
                _tool_part("prt_task", _completed(input=_TASK_INPUT), message_id="msg_task_step", tool="task"),
            ),
            _went_busy(),
            *_step(_REPLY_ID, 3000, "stop"),
            *_LOOP_EXIT,
        ],
    )
    assert _kind_and_native_type(state) == (TurnEndKind.COMPLETED, "stop")


def test_a_root_turn_opening_removes_the_last_turn_end(tmp_path: Path) -> None:
    _seed_earlier_turn_end(tmp_path)
    state = _run_plugin(tmp_path, [_session_created(_ROOT), _prompt(), _went_busy()])
    assert not (state / TURN_END_FILE_NAME).exists()


def test_an_idle_without_an_open_turn_keeps_the_last_turn_end(tmp_path: Path) -> None:
    """A restarted server, or a stop while idle, goes idle without opening a turn."""
    _seed_earlier_turn_end(tmp_path)
    state = _run_plugin(
        tmp_path,
        [
            _session_created(_ROOT),
            _prompt(),
            _reply(finish="stop", time=_completed_at(2000)),
            *_went_idle(),
        ],
    )
    assert (state / TURN_END_FILE_NAME).read_text() == _EARLIER_TURN_END_LINE
