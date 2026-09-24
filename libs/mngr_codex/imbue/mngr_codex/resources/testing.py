"""Shared helpers for the codex resource-script tests.

A test that exercises a provisioned command script (a hook, a background-task helper) copies it
into a temp ``commands/`` dir and points ``MNGR_AGENT_STATE_DIR`` at the temp state root before
running. ``provision_commands_dir`` does that provisioning.

The ``rollout_*`` builders mint codex rollout lines as plain dicts, the wire shape
common_transcript_convert.py reads (``{"timestamp":..,"type":<t>,"payload":<p>}``). They are
shared by the converter's unit tests and the shell-level tests, which JSON-encode them.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

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


def rollout_turn_context(model: str, effort: str | None = "medium", turn_id: str = "turn-1") -> dict[str, Any]:
    """The per-turn config item codex writes before each turn's response items.

    Only the fields the converter reads are filled in; the real item also carries
    cwd, approval/sandbox policy and the rest of the turn's configuration.
    """
    payload: dict[str, Any] = {"turn_id": turn_id, "model": model, "summary": "auto"}
    if effort is not None:
        payload["effort"] = effort
    return rollout_line("turn_context", payload)


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
