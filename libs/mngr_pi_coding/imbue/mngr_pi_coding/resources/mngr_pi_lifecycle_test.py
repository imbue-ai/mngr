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

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
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
  const ctx = {
    sessionManager: { getSessionId: () => ev.sessionId, getSessionFile: () => ev.sessionFile },
    model: ev.model,
    thinkingLevel: ev.thinkingLevel,
  };
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


def _run_extension(
    tmp_path: Path, events: list[dict[str, Any]], *, emit_common: bool = True, emit_usage: bool = False
) -> Path:
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
    if emit_usage:
        # The usage writer is gated on this marker (provisioned by mngr_pi_coding_usage).
        (state_dir / "pi_emit_usage").write_text("1")
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


_MODEL_STATE = Path("model_state.json")


def test_model_state_written_on_session_start(tmp_path: Path) -> None:
    """Pre-turn-1: session_start fires before the first prompt, so the launch model +
    thinking level land on disk immediately for the chat model bar."""
    state = _run_extension(
        tmp_path,
        [
            {
                "event": "session_start",
                "sessionId": "s1",
                "sessionFile": "/s/s1.jsonl",
                "model": {"provider": "anthropic", "id": "claude-opus-4-8"},
                "thinkingLevel": "high",
            }
        ],
    )
    assert json.loads((state / _MODEL_STATE).read_text()) == {
        "model": "anthropic/claude-opus-4-8",
        "effort": "high",
        "fast": False,
    }


def test_sentinel_not_written_when_model_state_write_fails(tmp_path: Path) -> None:
    """Readiness ordering: the model state (and session file) land BEFORE the
    sentinel mngr's create wait reports on, so everything the chat needs at first
    paint is on disk by the time readiness fires. When the model-state write
    fails, readiness must therefore not be signaled."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # A directory at the model-state path makes its writeFileSync fail (EISDIR).
    (state_dir / "model_state.json").mkdir()
    state = _run_extension(
        tmp_path,
        [
            {
                "event": "session_start",
                "sessionId": "s1",
                "sessionFile": "/s/s1.jsonl",
                "model": {"provider": "anthropic", "id": "claude-opus-4-8"},
                "thinkingLevel": "high",
            }
        ],
    )
    assert not (state / "pi_session_started").exists()
    # The session file was recorded before the failing write.
    assert (state / "pi_session_file").read_text() == "/s/s1.jsonl"


def test_model_state_tracks_model_and_thinking_switches(tmp_path: Path) -> None:
    """model_select / thinking_level_select keep the state live. The changed axis comes
    from the event; the untouched one from ctx (which may lag the event)."""
    state = _run_extension(
        tmp_path,
        [
            {
                "event": "session_start",
                "sessionId": "s1",
                "sessionFile": "/s/s1.jsonl",
                "model": {"provider": "anthropic", "id": "claude-opus-4-8"},
                "thinkingLevel": "high",
            },
            # /model switch: event carries the new model, ctx still reports the thinking level.
            {
                "event": "model_select",
                "payload": {"model": {"provider": "openai", "id": "gpt-5.2"}},
                "thinkingLevel": "high",
            },
            # thinking change: event carries the new level, ctx still reports the model.
            {
                "event": "thinking_level_select",
                "payload": {"level": "low"},
                "model": {"provider": "openai", "id": "gpt-5.2"},
            },
        ],
    )
    assert json.loads((state / _MODEL_STATE).read_text()) == {
        "model": "openai/gpt-5.2",
        "effort": "low",
        "fast": False,
    }


def test_marker_set_on_agent_start_and_cleared_on_agent_end(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "agent_start", "sessionId": "root"},
            {"event": "agent_end", "sessionId": "root"},
        ],
    )
    assert not (state / "active").exists()


def test_marker_present_after_agent_start(tmp_path: Path) -> None:
    state = _run_extension(tmp_path, [{"event": "agent_start", "sessionId": "root"}])
    assert (state / "active").exists()


def test_session_shutdown_clears_marker(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "agent_start", "sessionId": "root"},
            {"event": "session_shutdown", "sessionId": "root"},
        ],
    )
    assert not (state / "active").exists()


_EMITTER = "pi-coding/common_transcript"


def _message_end(**message: Any) -> dict[str, Any]:
    """A ``message_end`` event whose payload carries the given pi message."""
    return {"event": "message_end", "payload": {"message": message}}


# One message of each role the stream represents: a user turn, an assistant
# inference with thinking + a tool call, and that call's result.
_ONE_TURN_EVENTS: list[dict[str, Any]] = [
    _message_end(role="user", content="hi", timestamp=1),
    _message_end(
        role="assistant",
        content=[
            {"type": "thinking", "thinking": "let me look"},
            {"type": "text", "text": "ok"},
            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
        ],
        model="m",
        stopReason="toolUse",
        usage={"input": 7, "output": 3, "cacheRead": 1, "cacheWrite": 2, "cost": {"total": 0.25}},
        timestamp=2,
    ),
    _message_end(
        role="toolResult",
        toolCallId="c1",
        toolName="bash",
        content=[{"type": "text", "text": "out"}],
        isError=False,
        timestamp=3,
    ),
]


def test_common_transcript_records_for_each_role(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        _ONE_TURN_EVENTS
        # Roles the stream does not model are skipped.
        + [_message_end(role="bashExecution", command="x", timestamp=4)],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert [r["type"] for r in records] == ["header", "step", "step", "observation"]
    header, user_step, agent_step, observation = records
    # The extension derives the header id from the state-dir basename (the agent id).
    header_digest = hashlib.sha256(f"{state.name}:{_EMITTER}".encode()).hexdigest()[:32]
    assert header == {
        "type": "header",
        "event_id": f"header-{header_digest}",
        "emitter": _EMITTER,
        "schema_version": "ATIF-v1.7",
    }
    assert user_step["source"] == "user"
    assert user_step["message"] == "hi"
    assert agent_step["source"] == "agent"
    assert agent_step["message"] == "ok"
    assert agent_step["model_name"] == "m"
    assert agent_step["reasoning_content"] == "let me look"
    assert agent_step["extra"] == {"finish_reason": "toolUse"}
    assert agent_step["tool_calls"] == [
        {"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "ls"}}
    ]
    assert observation["results"] == [
        {"source_call_id": "c1", "content": "out", "extra": {"is_error": False, "tool_name": "bash"}}
    ]
    assert all(r["emitter"] == _EMITTER for r in records)
    assert len({r["event_id"] for r in records}) == len(records)


def test_tool_result_without_a_call_id_still_validates(tmp_path: Path) -> None:
    """The schema requires a call id and a tool name on every streamed result, so a
    message missing either degrades to the empty string rather than an invalid line."""
    state = _run_extension(
        tmp_path,
        [_message_end(role="toolResult", content=[{"type": "text", "text": "out"}], timestamp=1)],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (observation,) = [r for r in records if r["type"] == "observation"]
    assert observation["results"][0]["source_call_id"] == ""
    assert observation["results"][0]["extra"]["tool_name"] == ""
    assert validate_common_transcript_record(observation) is None


def test_assistant_usage_maps_to_atif_metric_names(tmp_path: Path) -> None:
    """ATIF's prompt_tokens counts ALL input (cache hits and writes included) and
    cached_tokens only the cache reads; the cache-write count has no ATIF field, so
    it rides under metrics.extra. pi's client-side per-message cost fills cost_usd."""
    state = _run_extension(tmp_path, _ONE_TURN_EVENTS)
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step" and r["source"] == "agent"]
    assert agent_step["metrics"] == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "cached_tokens": 1,
        "cost_usd": 0.25,
        "extra": {"cache_creation_input_tokens": 2},
    }


def test_assistant_without_usage_claims_no_metrics(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [_message_end(role="assistant", content=[{"type": "text", "text": "ok"}], timestamp=1)],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step"]
    assert "metrics" not in agent_step
    # An absent model is omitted rather than reported as the empty string.
    assert "model_name" not in agent_step
    assert "extra" not in agent_step
    assert validate_common_transcript_record(agent_step) is None


# Fidelity: ATIF streams carry complete tool arguments and untruncated outputs.
# Display truncation is the reader's job, so nothing here may shorten them.
_LONG_COMMAND = "echo " + "a" * 500
_LONG_OUTPUT = "b" * 5000


def test_tool_arguments_and_output_are_never_truncated(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            _message_end(
                role="assistant",
                content=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": _LONG_COMMAND}}],
                model="m",
                timestamp=1,
            ),
            _message_end(
                role="toolResult",
                toolCallId="c1",
                toolName="bash",
                content=[{"type": "text", "text": _LONG_OUTPUT}],
                timestamp=2,
            ),
        ],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step"]
    assert agent_step["tool_calls"][0]["arguments"] == {"command": _LONG_COMMAND}
    (observation,) = [r for r in records if r["type"] == "observation"]
    assert observation["results"][0]["content"] == _LONG_OUTPUT
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_non_object_tool_arguments_are_preserved_under_raw(tmp_path: Path) -> None:
    """ATIF requires an arguments object; a native non-object is wrapped, not dropped."""
    state = _run_extension(
        tmp_path,
        [
            _message_end(
                role="assistant",
                content=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": "ls -la"}],
                model="m",
                timestamp=1,
            )
        ],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step"]
    assert agent_step["tool_calls"][0]["arguments"] == {"_raw": "ls -la"}


def test_empty_tool_arguments_become_an_empty_arguments_object(tmp_path: Path) -> None:
    """An absent or empty native payload means "no arguments", not a raw empty string."""
    state = _run_extension(
        tmp_path,
        [
            _message_end(
                role="assistant",
                content=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": "   "}],
                model="m",
                timestamp=1,
            )
        ],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (agent_step,) = [r for r in records if r["type"] == "step"]
    assert agent_step["tool_calls"][0]["arguments"] == {}


def test_compaction_summary_becomes_a_system_step(tmp_path: Path) -> None:
    """Compaction is a system-initiated operation whose result exists at emission
    time, so it rides inline on the step with ATIF v1.7's context_management mark."""
    state = _run_extension(
        tmp_path,
        [_message_end(role="compactionSummary", summary="we did things", timestamp=1)],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    (step,) = [r for r in records if r["type"] == "step"]
    assert step["source"] == "system"
    assert step["extra"] == {"context_management": {"type": "compaction", "boundary": "replace"}}
    assert step["observation"] == {"results": [{"content": "we did things"}]}
    assert validate_common_transcript_record(step) is None


def test_emitted_common_records_conform_to_canonical_schema(tmp_path: Path) -> None:
    """Every record the extension emits must validate against the shared record schema.

    Guards against the pi emitter (resources/mngr_pi_lifecycle.ts) and the canonical
    schema (imbue.mngr.agents.common_transcript_records) drifting apart -- a divergence
    no other plugin's tests would catch. Drives every record type from real pi
    message_end payloads and asserts each emitted record conforms.
    """
    state = _run_extension(
        tmp_path,
        _ONE_TURN_EVENTS + [_message_end(role="compactionSummary", summary="so far", timestamp=4)],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    record_types = {r["type"] for r in records}
    assert record_types <= {"header", "step", "observation"}
    # The fixture drives all three, so an empty or degenerate stream cannot pass.
    assert record_types == {"header", "step", "observation"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


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


def _rerun_extension_against_state(tmp_path: Path, state: Path, events: list[dict[str, Any]]) -> None:
    """Replay ``events`` through a second extension load against an existing state dir.

    Simulates a resumed restart: a fresh process over a stream the previous one wrote.
    The caller must already have run :func:`_run_extension` (which sets the work dir up,
    and skips the test when node is unavailable).
    """
    node = shutil.which("node")
    assert node is not None
    work_dir = tmp_path / "work"
    (work_dir / "events.json").write_text(json.dumps(events))
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


def test_common_transcript_event_ids_stay_unique_across_restart(tmp_path: Path) -> None:
    """A second process (resume) must not reuse event_ids written by the first.

    event_id hashes the message's own timestamp and content, so ids stay unique
    across a stop/start even though the resumed session reuses its id and only
    new messages fire message_end.
    """
    state = _run_extension(
        tmp_path, [{"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}}]
    )
    _rerun_extension_against_state(
        tmp_path,
        state,
        [{"event": "message_end", "payload": {"message": {"role": "user", "content": "again", "timestamp": 2}}}],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert [r["type"] for r in records] == ["header", "step", "step"]
    assert len({r["event_id"] for r in records}) == 3


def test_header_is_written_once_across_restarts(tmp_path: Path) -> None:
    """The header is the first line of the file, written on creation only: a restart
    appends to the existing stream rather than opening a second header."""
    state = _run_extension(
        tmp_path, [{"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}}]
    )
    _rerun_extension_against_state(
        tmp_path,
        state,
        [{"event": "message_end", "payload": {"message": {"role": "user", "content": "again", "timestamp": 2}}}],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert [r["type"] for r in records].count("header") == 1
    assert records[0]["type"] == "header"
    # The header is not part of the id derivation: each step carries its own hash.
    assert all(r["event_id"].startswith("pi-") for r in records[1:])
    assert len({r["event_id"] for r in records[1:]}) == 2


def test_no_common_transcript_when_disabled(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [{"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}}],
        emit_common=False,
    )
    assert not (state / _COMMON_TRANSCRIPT).exists()
    # Raw is still captured (it is not gated).
    assert (state / _RAW_TRANSCRIPT).exists()


def test_unknown_content_and_roles_degrade_gracefully(tmp_path: Path) -> None:
    """Unknown content blocks/roles and malformed messages must not crash the extension.

    The stream surfaces only what ATIF models; the raw stream preserves everything
    verbatim, so unknown shapes are never lost.
    """
    state = _run_extension(
        tmp_path,
        [
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "assistant",
                        "model": "m",
                        "stopReason": "toolUse",
                        "timestamp": 1,
                        "content": [
                            {"type": "thinking", "thinking": "secret reasoning"},
                            {"type": "text", "text": "hello"},
                            {"type": "image", "data": "BASE64", "mimeType": "image/png"},
                            {"type": "futureBlockType", "blob": {"nested": True}},
                            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                        ],
                    }
                },
            },
            # Roles the common schema does not model -> skipped from common (kept in raw).
            {
                "event": "message_end",
                "payload": {"message": {"role": "branchSummary", "summary": "x", "timestamp": 2}},
            },
            {
                "event": "message_end",
                "payload": {"message": {"role": "someFutureRole", "whatever": 1, "timestamp": 3}},
            },
            # content that is neither a string nor an array -> coerced to "" (no crash).
            {"event": "message_end", "payload": {"message": {"role": "user", "content": 12345, "timestamp": 4}}},
            # Malformed messages -> skipped entirely.
            {"event": "message_end", "payload": {"message": {"timestamp": 5}}},
            {"event": "message_end", "payload": {"message": None}},
        ],
    )

    common = _read_jsonl(state / _COMMON_TRANSCRIPT)
    # Unknown roles are skipped; only modelled roles surface.
    assert [r["type"] for r in common] == ["header", "step", "step"]
    assistant, user = common[1], common[2]
    assert assistant["source"] == "agent"
    # Images become the placeholder; a future block type carries no text and is dropped.
    assert assistant["message"] == "hello[image omitted]"
    assert [c["function_name"] for c in assistant["tool_calls"]] == ["bash"]
    # Thinking is captured as ATIF reasoning_content rather than dropped.
    assert assistant["reasoning_content"] == "secret reasoning"
    assert user["source"] == "user"
    # Non-string/array content coerces to empty rather than crashing.
    assert user["message"] == ""

    # Raw preserves every well-formed message verbatim -- unknown roles AND unknown blocks.
    raw = _read_jsonl(state / _RAW_TRANSCRIPT)
    assert [r["message"]["role"] for r in raw] == ["assistant", "branchSummary", "someFutureRole", "user"]
    raw_text = json.dumps(raw)
    assert "futureBlockType" in raw_text
    assert "BASE64" in raw_text


# --- Native-vs-common transcript diff (invariant U5). -------------------------
#
# A real pi session captured live from the actual pi binary (a probe agent that
# ran one bash tool call and one thinking turn; the opaque thinkingSignature is
# scrubbed, everything else verbatim). Its `message` entries are exactly the
# message_end payloads the extension receives, so replaying them through the
# real extension and diffing the emitted common transcript against an
# independent enumeration of the session proves every user-visible turn
# surfaces exactly once. Schema validity is deliberately NOT the assertion --
# a schema-valid stream that dropped or duplicated a turn passes validation
# but fails this diff.
_REAL_SESSION_FIXTURE = Path(__file__).parent / "test_fixtures" / "pi_session_two_turns.jsonl"

# Native session line types that are bookkeeping, never conversation turns. An
# unlisted line type fails the enumeration, so additions must be deliberate.
_SESSION_BOOKKEEPING_LINE_TYPES = {
    # Session header (version, id, cwd).
    "session",
    # Model/thinking-level change bookkeeping.
    "model_change",
    "thinking_level_change",
}

# pi message roles the stream deliberately does not model (kept verbatim in the raw
# transcript instead).
_EXCLUDED_MESSAGE_ROLES = {"bashExecution", "custom", "branchSummary"}


def _pi_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        pytest.fail(f"native pi message content is neither string nor list: {content!r}")
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _native_session_messages() -> list[dict[str, Any]]:
    """Every `message` entry of the real session, with the bookkeeping line types excluded explicitly."""
    messages: list[dict[str, Any]] = []
    for line in _REAL_SESSION_FIXTURE.read_text().splitlines():
        record = json.loads(line)
        line_type = record.get("type")
        if line_type in _SESSION_BOOKKEEPING_LINE_TYPES:
            continue
        if line_type != "message":
            pytest.fail(
                f"unclassified native session line type {line_type!r}: classify it as a turn or exclude it deliberately"
            )
        messages.append(record["message"])
    return messages


# A turn descriptor: ("user", text), ("agent", (text, reasoning, calls)), or
# ("tool", (call_id, tool_name, output, is_error)). pi preserves the native toolCall
# ids end to end, so descriptor equality covers pairing.
def _enumerate_expected_turns(messages: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """Classify every native message as a user-visible turn or an explicitly excluded shape."""
    turns: list[tuple[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role in _EXCLUDED_MESSAGE_ROLES:
            continue
        if role == "user":
            turns.append(("user", _pi_text(message.get("content"))))
        elif role == "assistant":
            calls: list[tuple[str, str, str]] = []
            thinking: list[str] = []
            for block in message.get("content") or []:
                block_type = block.get("type") if isinstance(block, dict) else None
                if block_type == "text":
                    continue
                elif block_type == "toolCall":
                    calls.append(
                        (block["id"], block["name"], json.dumps(block.get("arguments") or {}, sort_keys=True))
                    )
                elif block_type == "thinking":
                    if block.get("thinking"):
                        thinking.append(block["thinking"])
                else:
                    pytest.fail(
                        f"unclassified assistant content block type {block_type!r}: classify it or exclude it deliberately"
                    )
            turns.append(("agent", (_pi_text(message.get("content")), "\n\n".join(thinking), tuple(calls))))
        elif role == "toolResult":
            turns.append(
                (
                    "tool",
                    (
                        message["toolCallId"],
                        message["toolName"],
                        _pi_text(message.get("content")),
                        message.get("isError") is True,
                    ),
                )
            )
        else:
            pytest.fail(
                f"unclassified native pi message role {role!r}: classify it as a turn or exclude it deliberately"
            )
    return turns


def _normalize_emitted_common(records: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    normalized: list[tuple[str, Any]] = []
    for record in records:
        record_type = record["type"]
        if record_type == "header":
            continue
        if record_type == "step" and record["source"] == "user":
            normalized.append(("user", record["message"]))
        elif record_type == "step" and record["source"] == "agent":
            calls = tuple(
                (call["tool_call_id"], call["function_name"], json.dumps(call["arguments"], sort_keys=True))
                for call in record.get("tool_calls") or []
            )
            normalized.append(("agent", (record["message"], record.get("reasoning_content") or "", calls)))
        elif record_type == "observation":
            (result,) = record["results"]
            normalized.append(
                (
                    "tool",
                    (
                        result["source_call_id"],
                        result["extra"]["tool_name"],
                        result["content"],
                        result["extra"]["is_error"],
                    ),
                )
            )
        else:
            pytest.fail(f"unexpected common-transcript record: {record!r}")
    return normalized


def test_every_native_turn_appears_in_common_transcript_exactly_once(tmp_path: Path) -> None:
    """Native-vs-common diff over the real captured session: replaying its
    messages through the real extension emits every user-visible turn exactly
    once, in order, with tool calls paired to their results (invariant U5)."""
    messages = _native_session_messages()
    state = _run_extension(
        tmp_path, [{"event": "message_end", "payload": {"message": message}} for message in messages]
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)

    expected = _enumerate_expected_turns(messages)
    actual = _normalize_emitted_common(records)

    # Guard against a degenerate enumeration: the captured session ran a tool
    # and had genuine typed turns, so the expected side must contain both.
    assert any(kind == "tool" for kind, _ in expected)
    assert any(kind == "user" for kind, _ in expected)

    # Exactly once, in order, with the same content and pairing (pi preserves
    # native toolCall ids, so equality covers call/result pairing).
    assert actual == expected

    assert len({record["event_id"] for record in records}) == len(records)
    # Schema validity is necessary (but alone would not catch a dropped turn).
    for record in records:
        assert validate_common_transcript_record(record) is None, record


# Drives the inbox watcher: a fake pi captures sendUserMessage calls; the inbox is
# pre-seeded with one already-delivered line BEFORE load (archived to
# pi_inbox_history and truncated at load, so it is never re-injected), then
# new/malformed lines are appended and we wait for the poll. The inbox content
# right after load is captured so the truncation itself is observable.
_INBOX_DRIVER_MJS = """
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const inbox = STATE + "/pi_inbox";
const injected = [];
const pi = { on: () => {}, sendUserMessage: (c) => injected.push(c) };
writeFileSync(inbox, JSON.stringify("OLD: already delivered") + "\\n");
const mod = await import("./mngr_pi_lifecycle.ts");
mod.default(pi);
const inboxAtLoad = readFileSync(inbox, "utf-8");
appendFileSync(inbox, JSON.stringify("first\\nmultiline") + "\\n");
appendFileSync(inbox, "{not json}\\n");
appendFileSync(inbox, JSON.stringify("second") + "\\n");
await new Promise((r) => setTimeout(r, 600));
writeFileSync(STATE + "/injected.json", JSON.stringify({ injected, inboxAtLoad }));
"""


def test_inbox_watcher_injects_only_new_lines(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "inbox_driver.mjs"
    driver.write_text(_INBOX_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, f"inbox driver failed:\n{result.stdout}\n{result.stderr}"
    outcome = json.loads((state_dir / "injected.json").read_text())
    # Pre-existing line not re-injected; new lines injected in order with the
    # embedded newline preserved; the malformed line is skipped.
    assert outcome["injected"] == ["first\nmultiline", "second"]
    # The prior generation's line was archived verbatim to pi_inbox_history and the
    # inbox truncated in place at load, so the durable inbox holds only
    # current-generation lines (the offset seed reads 0 from the empty file).
    assert outcome["inboxAtLoad"] == ""
    assert (state_dir / "pi_inbox_history").read_text() == json.dumps("OLD: already delivered") + "\n"


# pi's real sendUserMessage returns a Promise; this fake rejects it. The watcher
# must not let that become an unhandled rejection (which would crash the Node
# process and take pi down with it). The driver fails its top-level `await` only
# if the rejection escapes -- so a returncode of 0 *is* the assertion that the
# rejection was handled. A second, good line proves the poll loop kept running.
_INBOX_REJECTION_DRIVER_MJS = """
import { appendFileSync, writeFileSync } from "node:fs";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const inbox = STATE + "/pi_inbox";
const injected = [];
const pi = {
  on: () => {},
  sendUserMessage: (c) => {
    injected.push(c);
    return c === "boom" ? Promise.reject(new Error("rejected inject")) : Promise.resolve();
  },
};
const mod = await import("./mngr_pi_lifecycle.ts");
mod.default(pi);
appendFileSync(inbox, JSON.stringify("boom") + "\\n");
appendFileSync(inbox, JSON.stringify("after") + "\\n");
await new Promise((r) => setTimeout(r, 600));
writeFileSync(STATE + "/injected.json", JSON.stringify(injected));
"""


def test_inbox_watcher_swallows_rejected_inject(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "inbox_rejection_driver.mjs"
    driver.write_text(_INBOX_REJECTION_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, "--unhandled-rejections=throw", str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    # returncode 0 under --unhandled-rejections=throw means the rejection was handled.
    assert result.returncode == 0, f"rejection escaped:\n{result.stdout}\n{result.stderr}"
    injected = json.loads((state_dir / "injected.json").read_text())
    # The rejecting line still advanced the offset (no retry), and the watcher kept
    # running to inject the following line.
    assert injected == ["boom", "after"]


# Drives the interrupt/retract sentinels. A fake ctx exposes isIdle/abort/ui: abort
# APPENDS the parked steers into the editor (as pi does in interactive mode) and records
# an ordered "abort" marker; sendUserMessage records an ordered "inject:<text>" marker.
# session_start is fired so the extension captures the ctx (latestCtx). The scenario JSON
# drives the sequence: an optional pre-load inbox seed, initial idle/draft/parked-steer
# values, then a list of timed actions (append a JSON value / raw text, flip idle, sleep).
# The ordered `log`, the abort count, and the final editor text are written out for assertions.
_SENTINEL_DRIVER_MJS = """
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import mngrPiLifecycle from "./mngr_pi_lifecycle.ts";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const inbox = STATE + "/pi_inbox";
const scenario = JSON.parse(readFileSync(process.argv[2], "utf8"));
const log = [];
let editorText = scenario.draft || "";
let idle = scenario.idle === true;
let aborted = 0;
const handlers = {};
const ctx = {
  isIdle: () => idle,
  abort: () => { aborted++; log.push("abort"); editorText += (scenario.parkedSteers || ""); },
  ui: { getEditorText: () => editorText, setEditorText: (t) => { editorText = t; } },
};
const pi = {
  on: (evt, h) => { (handlers[evt] ||= []).push(h); },
  // Default: resolve immediately (the send lands at once). With scenario.parkDelayMs set, the
  // send resolves only after that delay and logs a "parked:" marker on resolution -- so a test
  // can prove a sentinel waits for the steer to actually park before aborting. neverSettle
  // returns a forever-pending promise (a send that never parks); thenOnly returns a bare
  // then-only thenable (has .then but no .catch/.finally, the minimal Promise-like shape).
  sendUserMessage: (c) => {
    log.push("inject:" + c);
    if (scenario.neverSettle) {
      return new Promise(() => {});
    }
    if (scenario.thenOnly) {
      return { then: (resolve) => { log.push("thenable:" + c); resolve(); } };
    }
    if (scenario.parkDelayMs) {
      return new Promise((resolve) => setTimeout(() => { log.push("parked:" + c); resolve(); }, scenario.parkDelayMs));
    }
    return Promise.resolve();
  },
};
if (scenario.preSeed !== undefined) writeFileSync(inbox, JSON.stringify(scenario.preSeed) + "\\n");
mngrPiLifecycle(pi);
for (const h of (handlers["session_start"] || [])) h({}, ctx);
// Drive the timed action list with a recursive setTimeout (a non-unref'd timer keeps the
// process alive so the extension's own inbox poll fires); the driver stays synchronous.
const actions = scenario.actions || [];
let i = 0;
const step = () => {
  if (i >= actions.length) {
    writeFileSync(STATE + "/outcome.json", JSON.stringify({ log, aborted, editorText }));
    return;
  }
  const action = actions[i++];
  if (action.append !== undefined) appendFileSync(inbox, JSON.stringify(action.append) + "\\n");
  if (action.appendRaw !== undefined) appendFileSync(inbox, action.appendRaw);
  if (action.setIdle !== undefined) idle = action.setIdle;
  setTimeout(step, action.sleep || 0);
};
step();
"""

_RETRACT_KEY = {"minds_interrupt_retract": True}
_FLUSH_KEY = {"minds_interrupt": True}


def _run_sentinel_scenario(tmp_path: Path, scenario: dict[str, Any]) -> Path:
    """Run the sentinel driver over ``scenario`` under a fresh state dir; return the state dir."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "sentinel_driver.mjs"
    driver.write_text(_SENTINEL_DRIVER_MJS)
    scenario_path = work_dir / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, str(driver), str(scenario_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, f"sentinel driver failed:\n{result.stdout}\n{result.stderr}"
    return state_dir


def test_retract_during_a_turn_aborts_discards_and_restores_draft(tmp_path: Path) -> None:
    """A retract sentinel while a turn runs interrupts it, restores the pre-existing draft, and
    resubmits nothing -- the parked steers are discarded (Minds hands them back to the composer)."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "draft": "my draft",
            "parkedSteers": "PARKED STEER",
            "actions": [{"append": _RETRACT_KEY}, {"sleep": 600}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 1
    # Nothing was resubmitted: the only recorded op is the abort.
    assert outcome["log"] == ["abort"]
    # The draft is restored after the abort drained the steers into (and out of) the editor.
    assert outcome["editorText"] == "my draft"


def test_retract_while_idle_no_ops_and_later_strings_still_inject(tmp_path: Path) -> None:
    """A retract with no live turn is a no-op (no abort), and the drain keeps running so a later
    message still injects."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": True,
            "actions": [
                {"append": _RETRACT_KEY},
                {"sleep": 400},
                {"append": "later message"},
                {"sleep": 400},
            ],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 0
    assert outcome["log"] == ["inject:later message"]


def test_pre_existing_retract_line_is_never_processed(tmp_path: Path) -> None:
    """A retract sentinel already on disk at load is archived with the prior generation and never
    interrupts the fresh turn (the generation-reset truncates the inbox at load)."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "draft": "d",
            "parkedSteers": "PARKED",
            "preSeed": _RETRACT_KEY,
            "actions": [{"sleep": 600}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 0
    # The pre-existing sentinel was archived verbatim, not processed (driver writes it with
    # JS's compact JSON, so compare against the same separators).
    archived = (state / "pi_inbox_history").read_text()
    assert json.loads(archived) == _RETRACT_KEY
    assert archived == json.dumps(_RETRACT_KEY, separators=(",", ":")) + "\n"


def test_string_then_sentinel_in_one_write_defers_the_sentinel(tmp_path: Path) -> None:
    """A string line and a retract sentinel appended together are handled across two ticks: the
    steer injects first, and only on a later tick (once it has parked) does the abort fire."""
    both = json.dumps("steer msg") + "\n" + json.dumps(_RETRACT_KEY) + "\n"
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "parkedSteers": "PARKED",
            "actions": [{"appendRaw": both}, {"sleep": 800}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    # The steer was injected before the abort ran, and the sentinel was still processed.
    assert outcome["log"] == ["inject:steer msg", "abort"]
    assert outcome["aborted"] == 1


def test_retract_waits_for_a_slow_parking_steer_before_aborting(tmp_path: Path) -> None:
    """A steer whose send parks slowly (later than one poll) still parks BEFORE the retract aborts:
    the sentinel defers until the injection settles, so the steer is captured-and-discarded rather
    than escaping to commit as a stray turn. Under the old single-tick deferral the abort would
    fire before the ~500ms park; here the ordered log proves park-then-abort."""
    both = json.dumps("slow steer") + "\n" + json.dumps(_RETRACT_KEY) + "\n"
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "parkedSteers": "PARKED",
            "parkDelayMs": 500,
            "actions": [{"appendRaw": both}, {"sleep": 1500}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    # The steer parked before the abort ran (abort is last), and the sentinel was still processed.
    assert outcome["log"] == ["inject:slow steer", "parked:slow steer", "abort"]
    assert outcome["aborted"] == 1


def test_retract_proceeds_after_the_bound_when_an_injection_never_settles(tmp_path: Path) -> None:
    """A steer whose send promise NEVER settles must not defer the retract forever (an
    unstoppable turn): after the bounded deferral (SENTINEL_SETTLE_MAX_TICKS ~= 2s at the 200ms
    cadence) the sentinel proceeds anyway and the abort fires. The un-parked steer may escape as
    a visible duplicate -- the accepted class -- but the stop always lands (U2)."""
    both = json.dumps("never parking") + "\n" + json.dumps(_RETRACT_KEY) + "\n"
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "parkedSteers": "PARKED",
            "neverSettle": True,
            # The bound is 10 deferred ticks (~2s); the abort lands around tick 11 (~2.2s), so
            # 3.5s comfortably covers it without racing the deadline.
            "actions": [{"appendRaw": both}, {"sleep": 3500}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    # The send never settled (no "parked:" marker), yet the sentinel was still consumed and the
    # abort fired after the bound.
    assert outcome["log"] == ["inject:never parking", "abort"]
    assert outcome["aborted"] == 1


def test_then_only_thenable_send_does_not_deadlock_the_sentinel(tmp_path: Path) -> None:
    """A send returning a then-only thenable (has ``.then`` but no ``.catch``/``.finally``) must
    not leak a pendingInjections entry: the injection is assimilated via ``Promise.resolve``, so
    the settle gate drains and the retract aborts promptly. The 800ms window is far below the
    ~2.2s deferral bound, so a leaked entry (rescued only by the bound) would fail this test."""
    both = json.dumps("steer msg") + "\n" + json.dumps(_RETRACT_KEY) + "\n"
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "parkedSteers": "PARKED",
            "thenOnly": True,
            "actions": [{"appendRaw": both}, {"sleep": 800}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    # The thenable settled (its then ran under assimilation) and the abort followed within the
    # normal settle-gate window -- no deadlock, no reliance on the deferral bound.
    assert outcome["log"] == ["inject:steer msg", "thenable:steer msg", "abort"]
    assert outcome["aborted"] == 1


def test_flush_sentinel_still_resubmits_the_parked_steers(tmp_path: Path) -> None:
    """The flush sibling is unchanged by the shared-core refactor: it interrupts, then resubmits
    the parked steers as one merged turn once the aborted turn settles."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "draft": "d",
            "parkedSteers": "PARKED",
            "actions": [
                {"append": _FLUSH_KEY},
                {"sleep": 400},
                {"setIdle": True},
                {"sleep": 400},
            ],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 1
    # Abort first, then the captured steers resubmitted once idle; the draft is restored.
    assert outcome["log"] == ["abort", "inject:PARKED"]
    assert outcome["editorText"] == "d"


_USAGE_EVENTS = Path("events") / "pi-coding" / "usage" / "events.jsonl"


def _assistant_message_end(session_file: str, usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "message_end",
        "sessionId": "s1",
        "sessionFile": session_file,
        "payload": {
            "message": {
                "role": "assistant",
                "content": [],
                "model": "claude-opus-4-8",
                "provider": "anthropic",
                "usage": usage,
            }
        },
    }


def test_usage_writer_emits_cost_snapshot_when_gated(tmp_path: Path) -> None:
    session_file = "/sessions/2056-01-01T00-00-00Z_abc-uuid.jsonl"
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": session_file},
            _assistant_message_end(
                session_file,
                {"input": 2, "output": 7, "cacheRead": 9133, "cacheWrite": 21, "cost": {"total": 0.00488275}},
            ),
        ],
        emit_usage=True,
    )
    records = _read_jsonl(state / _USAGE_EVENTS)
    assert len(records) == 1
    record = records[0]
    assert record["source"] == "pi-coding/usage"
    assert record["type"] == "cost_snapshot"
    # session_id is the session file's basename (timestamp + uuid), stripped of .jsonl.
    assert record["session_id"] == "2056-01-01T00-00-00Z_abc-uuid"
    assert record["cost"] == {"total_cost_usd": 0.00488275}
    assert record["tokens"] == {"input": 2, "output": 7, "cache_read": 9133, "cache_creation": 21}
    assert record["model"] == "anthropic/claude-opus-4-8"
    assert record["cost_mode"] == "API_KEY"


def test_usage_writer_is_inert_without_the_gate_marker(tmp_path: Path) -> None:
    session_file = "/sessions/x.jsonl"
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": session_file},
            _assistant_message_end(session_file, {"input": 1, "cost": {"total": 0.5}}),
        ],
        emit_usage=False,
    )
    assert not (state / _USAGE_EVENTS).exists()


# Drives the switch-mailbox consumer: a fake pi captures setModel/setThinkingLevel and
# stores handlers so the driver can fire session_start (which the consumer needs to capture a
# ctx for ctx.modelRegistry). The intent is written BEFORE session_start -- modeling a switch
# parked while the agent was stopped -- and must be applied exactly once and the mailbox
# consumed (deleted) afterwards.
_CONTROL_DRIVER_MJS = """
import { existsSync, writeFileSync } from "node:fs";
import mngrPiLifecycle from "./mngr_pi_lifecycle.ts";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const control = STATE + "/pi_control.json";
const calls = { setModel: [], setThinkingLevel: [] };
const handlers = {};
const targetModel = { provider: "anthropic", id: "claude-opus-4-8", label: "opus" };
const ctx = {
  model: { provider: "anthropic", id: "claude-sonnet-4-5" },
  thinkingLevel: "medium",
  modelRegistry: {
    find: (p, id) => (p === "anthropic" && id === "claude-opus-4-8") ? targetModel : undefined,
    hasConfiguredAuth: () => true,
  },
};
const pi = {
  on: (evt, h) => { handlers[evt] = h; },
  setModel: (m) => { calls.setModel.push({ provider: m.provider, id: m.id }); return Promise.resolve(true); },
  setThinkingLevel: (l) => { calls.setThinkingLevel.push(l); },
};
// Parked intent: written before the extension loads (stopped-agent case). A stale
// pick it replaced is gone by construction -- the mailbox holds one intent.
writeFileSync(control, JSON.stringify({ model_id: "anthropic/claude-opus-4-8", thinking_level: "high" }));
mngrPiLifecycle(pi);
handlers["session_start"]({}, ctx);
setTimeout(() => {
  writeFileSync(STATE + "/switch_calls.json", JSON.stringify({ ...calls, mailboxConsumed: !existsSync(control) }));
}, 600);
"""


def test_control_watcher_applies_model_and_effort_switch(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "control_driver.mjs"
    driver.write_text(_CONTROL_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, f"control driver failed:\n{result.stdout}\n{result.stderr}"
    calls = json.loads((state_dir / "switch_calls.json").read_text())
    # The parked (pre-session_start) intent resolved via the registry and applied exactly once;
    # the effort passed through; the mailbox was consumed so it cannot re-apply.
    assert calls["setModel"] == [{"provider": "anthropic", "id": "claude-opus-4-8"}]
    assert calls["setThinkingLevel"] == ["high"]
    assert calls["mailboxConsumed"] is True


# Driver for the policy-guard tool_call handler: fire one bash tool_call and report
# the handler's return value, the (possibly mutated) command, and the pre-rewrite
# command the handler records for other extensions, as JSON on stdout.
_GUARD_DRIVER_MJS = """
import mngrPiLifecycle from "./mngr_pi_lifecycle.ts";
const command = process.argv[2];
const handlers = {};
mngrPiLifecycle({ on: (name, handler) => { (handlers[name] ||= []).push(handler); } });
const event = { toolName: "bash", input: { command } };
let result;
for (const handler of (handlers["tool_call"] || [])) { result = handler(event, {}); }
process.stdout.write(JSON.stringify({
  result: result ?? null,
  command: event.input.command,
  originalCommand: event.mngrOriginalCommand ?? null,
}));
"""


def _run_tool_call(tmp_path: Path, command: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Fire one bash ``tool_call``; return ``{result, command, originalCommand}``."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver_path = work_dir / "guard_driver.mjs"
    driver_path.write_text(_GUARD_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    full_env = {"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)}
    full_env.update(env or {})
    result = subprocess.run(
        [node, str(driver_path), command], capture_output=True, text=True, timeout=60, env=full_env
    )
    assert result.returncode == 0, f"guard driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_guard_blocks_pipe_to_pager(tmp_path: Path) -> None:
    out = _run_tool_call(tmp_path, "cat f.txt | " + "tail -5")
    assert out["result"] is not None and out["result"]["block"] is True
    assert "tail or head" in out["result"]["reason"]


def test_guard_blocks_git_commit_amend(tmp_path: Path) -> None:
    out = _run_tool_call(tmp_path, "git commit --amend -m x")
    assert out["result"] is not None and out["result"]["block"] is True


def test_guard_blocks_git_rebase(tmp_path: Path) -> None:
    out = _run_tool_call(tmp_path, "git rebase -i HEAD~2")
    assert out["result"] is not None and out["result"]["block"] is True


def test_guard_allows_and_rewrites_a_normal_command(tmp_path: Path) -> None:
    # An allowed command is not blocked, and is rewritten with the oom self-tag.
    out = _run_tool_call(tmp_path, "ls -la")
    assert out["result"] is None
    assert "oom_score_adj" in out["command"]
    assert out["command"].endswith("; ls -la")


def test_guard_records_the_pre_rewrite_command_for_other_extensions(tmp_path: Path) -> None:
    """pi runs every extension's tool_call handler on one event, in an order we do not
    control. A workspace guard reading `input.command` after the rewrite would see the
    prefix as a command chained ahead of the agent's and refuse it, so the handler
    records what the agent actually wrote."""
    out = _run_tool_call(tmp_path, "tk start abc")
    assert out["originalCommand"] == "tk start abc"
    assert out["command"] != out["originalCommand"]
    # A blocked command never reaches the rewrite, so there is nothing to record.
    blocked = _run_tool_call(tmp_path, "git rebase -i HEAD~2")
    assert blocked["originalCommand"] is None


def test_guard_rewrites_with_git_identity_when_resolvable(tmp_path: Path) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text(json.dumps({"host_id": "host-9"}))
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "data.json").write_text(json.dumps({"name": "test-agent"}))
    out = _run_tool_call(
        tmp_path,
        "git commit -m hi",
        env={"MNGR_AGENT_ID": "agent-x", "MNGR_HOST_DIR": str(host_dir)},
    )
    assert out["result"] is None
    assert "GIT_AUTHOR_NAME='test-agent'" in out["command"]
    assert "GIT_AUTHOR_EMAIL='agent-x@host-9'" in out["command"]
