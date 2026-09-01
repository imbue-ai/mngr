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


def rollout_function_call(name: str, arguments: str, call_id: str) -> dict[str, Any]:
    return rollout_line(
        "response_item", {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id}
    )


def rollout_function_call_output(call_id: str, output: Any) -> dict[str, Any]:
    return rollout_line("response_item", {"type": "function_call_output", "call_id": call_id, "output": output})


def rollout_event_msg_user(text: str) -> dict[str, Any]:
    """The display-duplicate event_msg codex also writes for each user message."""
    return rollout_line("event_msg", {"type": "user_message", "message": text, "images": []})
