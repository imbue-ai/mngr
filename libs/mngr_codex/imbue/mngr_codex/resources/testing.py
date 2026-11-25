"""Shared helpers for the codex resource-script tests.

A test that exercises a provisioned command script (a hook, a background-task helper) copies it
into a temp ``commands/`` dir and points ``MNGR_AGENT_STATE_DIR`` at the temp state root before
running. ``provision_commands_dir`` does that provisioning.

The ``rollout_*`` builders mint codex rollout lines as plain dicts, the wire shape
common_transcript_convert.py reads (``{"timestamp":..,"type":<t>,"payload":<p>}``). They are
shared by the converter's unit tests and the shell-level tests, which JSON-encode them.

``FakeCodexAppServer`` answers the app-server requests record_background_tasks.py makes, over a real
WebSocket on a unix socket (``serving_fake_codex_app_server``).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field
from websockets.sync.server import ServerConnection
from websockets.sync.server import unix_serve

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path
from imbue.mngr_codex.codex_config import get_codex_home

_RESOURCES_DIR = Path(__file__).parent

DEFAULT_ROLLOUT_TIMESTAMP = "2026-06-09T07:00:00.000Z"


def provision_commands_dir(state_dir: Path, script_names: Sequence[str]) -> Path:
    """Copy the named scripts into ``state_dir/commands/`` and return the commands dir."""
    commands_dir = state_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    for script_name in script_names:
        shutil.copy(_RESOURCES_DIR / script_name, commands_dir / script_name)
    return commands_dir


def rollout_line(type_: str, payload: dict[str, Any], timestamp: str = DEFAULT_ROLLOUT_TIMESTAMP) -> dict[str, Any]:
    """One raw codex rollout line."""
    return {"timestamp": timestamp, "type": type_, "payload": payload}


def rollout_user_message(text: str) -> dict[str, Any]:
    return rollout_line(
        "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    )


def rollout_assistant_message(text: str) -> dict[str, Any]:
    return rollout_line(
        "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    )


def rollout_reasoning(*summary_texts: str) -> dict[str, Any]:
    """A reasoning item whose summary carries visible thinking text.

    Captured rollouts only ever have an encrypted payload, so a reasoning item with
    extractable text has to be synthesized.
    """
    return rollout_line(
        "response_item",
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": text} for text in summary_texts],
            "encrypted_content": "gAAAAA-opaque",
        },
    )


def rollout_function_call(
    name: str, arguments: str, call_id: str, timestamp: str = DEFAULT_ROLLOUT_TIMESTAMP
) -> dict[str, Any]:
    return rollout_line(
        "response_item",
        {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id},
        timestamp=timestamp,
    )


def rollout_function_call_output(
    call_id: str, output: Any, timestamp: str = DEFAULT_ROLLOUT_TIMESTAMP
) -> dict[str, Any]:
    return rollout_line(
        "response_item", {"type": "function_call_output", "call_id": call_id, "output": output}, timestamp=timestamp
    )


def rollout_custom_tool_call(name: str, program: str, call_id: str, timestamp: str) -> dict[str, Any]:
    """A free-form tool call, the shape codex's code-mode ``exec`` program arrives in."""
    return rollout_line(
        "response_item",
        {"type": "custom_tool_call", "name": name, "input": program, "call_id": call_id, "status": "completed"},
        timestamp=timestamp,
    )


def rollout_custom_tool_call_output(call_id: str, output: str, timestamp: str) -> dict[str, Any]:
    return rollout_line(
        "response_item", {"type": "custom_tool_call_output", "call_id": call_id, "output": output}, timestamp=timestamp
    )


def rollout_timestamp_ms(timestamp: str) -> int:
    """Milliseconds since the epoch for a rollout line's ISO 8601 timestamp."""
    return round(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)


def rollout_command_execution(
    script: str,
    started_at: str,
    completed_at: str,
    output: str = "",
    exit_code: int = 0,
    status: str = "completed",
    item_id: str = "exec-1",
    turn_id: str = "turn-1",
) -> dict[str, Any]:
    """The item codex writes when one shell command a tool call started has finished.

    Written at ``completed_at``; codex runs the command string through a login shell, which is the
    argv it records.
    """
    started_at_ms = rollout_timestamp_ms(started_at)
    completed_at_ms = rollout_timestamp_ms(completed_at)
    return rollout_line(
        "event_msg",
        {
            "type": "item_completed",
            "thread_id": "thread-1",
            "turn_id": turn_id,
            "item": {
                "type": "CommandExecution",
                "id": item_id,
                "process_id": "4242",
                "command": ["/bin/zsh", "-lc", script],
                "cwd": "file:///home/user/workspace",
                "parsed_cmd": [{"type": "unknown", "cmd": script}],
                "source": "unified_exec_startup",
                "status": status,
                "stdout": output,
                "stderr": "",
                "aggregated_output": output,
                "exit_code": exit_code,
                "duration": {"secs": (completed_at_ms - started_at_ms) // 1000, "nanos": 0},
                "formatted_output": output,
            },
            "started_at_ms": started_at_ms,
            "completed_at_ms": completed_at_ms,
        },
        timestamp=completed_at,
    )


def rollout_event_msg_user(text: str) -> dict[str, Any]:
    """The display-duplicate event_msg codex also writes for each user message."""
    return rollout_line("event_msg", {"type": "user_message", "message": text, "images": []})


def rollout_session_meta(session_id: str, *, thread_id: str | None = None) -> dict[str, Any]:
    """The item codex writes at the top of each rollout, naming its session and thread.

    A main thread's ``id`` (its thread id) equals its ``session_id``; a spawned subagent's
    ``id`` is its own thread while ``session_id`` stays the root session's.
    """
    return rollout_line("session_meta", {"session_id": session_id, "id": thread_id or session_id})


def rollout_turn_context(model: str, effort: str | None = "medium", turn_id: str = "turn-1") -> dict[str, Any]:
    """The per-turn config item codex writes before each turn's response items.

    Only the fields the converter reads are filled in; the real item also carries
    cwd, approval/sandbox policy and the rest of the turn's configuration.
    """
    payload: dict[str, Any] = {"turn_id": turn_id, "model": model, "summary": "auto"}
    if effort is not None:
        payload["effort"] = effort
    return rollout_line("turn_context", payload)


def rollout_compacted(message: str = "", timestamp: str = DEFAULT_ROLLOUT_TIMESTAMP) -> dict[str, Any]:
    """The line codex writes where it replaced the conversation with a compacted history.

    ``message`` is codex's summary, which a remote compaction leaves empty; the history it kept
    rides opaquely in ``replacement_history``.
    """
    return rollout_line(
        "compacted",
        {"message": message, "replacement_history": [{"type": "compaction", "encrypted_content": "gAAAAA-opaque"}]},
        timestamp=timestamp,
    )


def rollout_token_count(
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    total_input_tokens: int | None = None,
) -> dict[str, Any]:
    """The event_msg codex writes to close one model response, carrying its usage.

    ``last_token_usage`` is the response's own usage and ``total_token_usage`` the
    session running total; ``total_input_tokens`` fills the latter's input bucket so
    a test can tell the two apart (it defaults to the same response's own).
    """
    last_usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    total_usage = {**last_usage, "input_tokens": input_tokens if total_input_tokens is None else total_input_tokens}
    return rollout_line(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "total_token_usage": total_usage,
                "last_token_usage": last_usage,
                "model_context_window": 272000,
            },
            "rate_limits": None,
        },
    )


def codex_thread(
    thread_id: str,
    status_type: str = "idle",
    active_flags: Sequence[str] = (),
    parent_thread_id: str | None = None,
    thread_source: str | None = None,
) -> dict[str, Any]:
    """A loaded thread as codex 0.163.0's ``thread/read`` returns it, trimmed to the fields that vary here."""
    status: dict[str, Any] = {"type": status_type}
    if status_type == "active":
        status["activeFlags"] = list(active_flags)
    return {
        "id": thread_id,
        "status": status,
        "parentThreadId": parent_thread_id,
        "source": "vscode" if parent_thread_id is None else {"subAgent": {"thread_spawn": {"depth": 1}}},
        "threadSource": thread_source,
        "agentNickname": None if parent_thread_id is None else "Planck",
        "createdAt": 1795073526,
        "updatedAt": 1795073531,
        "turns": [],
    }


def codex_hook(key: str, trust_status: str) -> dict[str, Any]:
    """One hook of codex 0.172.0's ``hooks/list``, trimmed to the fields that vary here."""
    return {
        "key": key,
        "eventName": "userPromptSubmit",
        "handlerType": "command",
        "command": 'bash "$MNGR_AGENT_STATE_DIR/commands/codex_turn_hook.sh" UserPromptSubmit',
        "sourcePath": key.split(":", 1)[0],
        "source": "user",
        "enabled": True,
        "isManaged": trust_status == "managed",
        "currentHash": f"sha256:{key.rsplit('/', 1)[-1]}",
        "trustStatus": trust_status,
    }


def codex_background_terminal(item_id: str, command: str = "sleep 47213") -> dict[str, Any]:
    """One entry of codex 0.163.0's ``thread/backgroundTerminals/list``."""
    return {
        "itemId": item_id,
        "processId": "38373",
        "command": command,
        "cwd": "/home/user/work",
        "osPid": None,
        "cpuPercent": None,
        "rssKb": None,
    }


class FakeCodexAppServer(FrozenModel):
    """Answers the requests of one codex app-server client: each loaded thread, and each thread's background terminals.

    Each reply follows an ``account/updated`` notification. A reply in ``replies_by_method`` replaces the one built
    from the listing; a request whose params differ from its entry in ``expected_params_by_method`` gets an error;
    ``page_size`` splits each list into pages; ``is_framing_varied`` sends each reply after a ping, in three fragments;
    ``is_silent`` never replies.
    """

    threads: tuple[Mapping[str, Any], ...] = ()
    background_terminals: Mapping[str, tuple[Mapping[str, Any], ...]] = Field(default_factory=dict)
    replies_by_method: Mapping[str, Mapping[str, Any]] = Field(default_factory=dict)
    expected_params_by_method: Mapping[str, Mapping[str, Any]] = Field(default_factory=dict)
    page_size: int | None = None
    is_framing_varied: bool = False
    is_silent: bool = False

    def answer(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        expected_params = self.expected_params_by_method.get(method)
        if expected_params is not None and params != expected_params:
            return {"error": {"code": -32602, "message": f"unexpected {method} params {json.dumps(params)}"}}
        if method in self.replies_by_method:
            return self.replies_by_method[method]
        if method == "initialize":
            return {"result": {"userAgent": "mngr/0.163.0", "codexHome": "/home/user/.codex"}}
        if method == "thread/loaded/list":
            return {"result": self._page([thread["id"] for thread in self.threads], params)}
        if method == "thread/read":
            (thread,) = [thread for thread in self.threads if thread["id"] == params["threadId"]]
            return {"result": {"thread": thread, "readState": None}}
        if method == "thread/backgroundTerminals/list":
            return {"result": self._page(self.background_terminals.get(params["threadId"], ()), params)}
        return {"error": {"code": -32601, "message": f"unknown method {method}"}}

    def _page(self, items: Sequence[Any], params: Mapping[str, Any]) -> dict[str, Any]:
        if self.page_size is None:
            return {"data": list(items), "nextCursor": None}
        start = int(params.get("cursor") or 0)
        end = start + self.page_size
        return {"data": list(items[start:end]), "nextCursor": str(end) if end < len(items) else None}

    def handle(self, connection: ServerConnection) -> None:
        for message in connection:
            request = json.loads(message)
            if "id" not in request or self.is_silent:
                continue
            connection.send(json.dumps({"method": "account/updated", "params": {}}))
            reply = json.dumps({"id": request["id"], **self.answer(request["method"], request.get("params") or {})})
            if self.is_framing_varied:
                connection.ping()
                third = len(reply) // 3
                connection.send([reply[:third], reply[third : 2 * third], reply[2 * third :]])
            else:
                connection.send(reply)


@contextmanager
def serving_fake_codex_app_server(socket_path: Path, server: FakeCodexAppServer) -> Generator[None, None, None]:
    """Serve ``server`` on ``socket_path`` the way codex's app-server serves its clients: WebSocket over a unix socket."""
    with unix_serve(server.handle, path=str(socket_path), compression=None) as websocket_server:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(websocket_server.serve_forever)
            try:
                yield
            finally:
                websocket_server.shutdown()


@contextmanager
def serving_fake_codex_app_server_for_agent(
    state_dir: Path, server: FakeCodexAppServer
) -> Generator[None, None, None]:
    """Serve ``server`` on the socket the agent at ``state_dir`` launches its app-server on, removing it afterwards."""
    socket_path = get_codex_app_server_socket_path(get_codex_home(state_dir))
    socket_path.parent.mkdir(mode=0o700)
    try:
        with serving_fake_codex_app_server(socket_path, server):
            yield
    finally:
        shutil.rmtree(socket_path.parent, ignore_errors=True)
