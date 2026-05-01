"""Shared I/O helpers for the plugin's PreToolUse:Agent hooks.

Both ``hooks/spawn.py`` (PROXY mode) and ``hooks/deny.py`` (DENY mode)
need the same handful of side-effecting helpers: read hook JSON from
stdin, read an int env var, write a secure 0600 sidefile, write an
executable 0755 wait-script, and emit a hook-protocol JSON response on
stdout. Centralized here so the two hooks cannot drift on file modes
or response framing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from typing import TextIO

from loguru import logger


def parse_int_env(name: str, default: int) -> int:
    """Parse an int-valued env var; return ``default`` on missing/empty/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def read_hook_stdin_json(stdin: TextIO, log_prefix: str) -> dict[str, Any] | None:
    """Read Claude Code hook JSON from stdin; return None on empty or malformed input.

    ``log_prefix`` distinguishes call sites in log output (e.g. ``"spawn"`` /
    ``"deny"``) so a single concatenated log can still attribute warnings to
    the right hook.

    Returns ``None`` (and logs a warning) on:
    - read errors (OSError),
    - empty input,
    - JSON that doesn't decode,
    - JSON that decodes to anything other than a dict.

    Centralized here so PROXY and DENY hooks cannot drift on input
    validation; both must treat malformed hook input identically.
    """
    try:
        raw = stdin.read()
    except OSError as e:
        logger.warning("{}: failed to read stdin: {}", log_prefix, e)
        return None
    if not raw:
        logger.warning("{}: empty stdin", log_prefix)
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("{}: malformed stdin JSON: {}", log_prefix, e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("{}: stdin JSON is not an object", log_prefix)
        return None
    return parsed


def write_secure_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` with 0600 perms, creating parents as needed.

    Used for prompt sidefiles which may carry user-supplied text and
    should not be world- or group-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)


def write_executable_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` with 0755 perms, creating parents as needed.

    Used for the per-Task-call wait-scripts that the runner (Haiku in
    PROXY mode, Claude in DENY mode) invokes via Bash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def emit_json_response(stdout: TextIO, response: dict[str, Any]) -> None:
    """Write a JSON response to stdout, append a newline, and flush.

    Matches the framing Claude Code's hook protocol expects: one JSON
    object per line on stdout, immediately flushed so the parent hook
    runner sees it before the script exits.
    """
    stdout.write(json.dumps(response) + "\n")
    stdout.flush()


def emit_pre_tool_deny(stdout: TextIO, reason: str) -> None:
    """Emit a PreToolUse permission deny with the given reason.

    Single source of truth for the deny-shape JSON envelope so the
    plugin's PROXY and DENY hooks (and any other deny path) cannot
    drift on hook-protocol framing.
    """
    emit_json_response(
        stdout,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
    )


def emit_depth_limit_deny(stdout: TextIO, depth: int, max_depth: int) -> None:
    """Emit a PreToolUse deny decision citing the subagent-depth limit.

    Same shape (and reason text) as PROXY mode's depth-limit emit, so
    Claude sees identical framing regardless of which mode the parent
    is provisioned with.
    """
    reason = (
        f"mngr_subagent_proxy: subagent depth limit ({depth}/{max_depth}) reached. "
        "Cannot spawn nested Task tools beyond this depth."
    )
    emit_pre_tool_deny(stdout, reason)
