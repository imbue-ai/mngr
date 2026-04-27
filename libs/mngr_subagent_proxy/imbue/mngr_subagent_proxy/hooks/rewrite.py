"""PostToolUse:Agent hook. Tears down the mngr subagent and cleans up
per-tool_use_id state files after the Task tool returns.

The subagent's actual end-turn text reaches the parent via Haiku's own
final reply (the wait-script prints the text to Haiku's stdout and
Haiku is instructed to echo it verbatim) -- not via this hook, because
Claude Code's PostToolUse ``updatedToolOutput`` field is MCP-only and
does not apply to built-in tools like Task.

Exits 0 on any failure so Claude Code keeps running.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from typing import Callable
from typing import TextIO

from loguru import logger

from imbue.mngr_subagent_proxy.hooks.destroy_detached import destroy_agent_detached

# Type alias for the detached-destroy callable, so tests can inject a stub.
DestroyAgentDetachedCallable = Callable[[str, Path], None]


def _read_stdin_json(stdin: TextIO) -> dict[str, Any] | None:
    try:
        raw = stdin.read()
    except OSError as e:
        logger.warning("rewrite: failed to read stdin: {}", e)
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("rewrite: malformed stdin JSON: {}", e)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("rewrite: failed to remove {}: {}", path, e)


def run(
    stdin: TextIO,
    stdout: TextIO,
    destroy_callable: DestroyAgentDetachedCallable = destroy_agent_detached,
) -> None:
    """PostToolUse:Agent hook core.

    Takes I/O streams and the detached-destroy callable explicitly so tests
    can inject StringIO buffers and a stub destroy function without
    monkey-patching module-level names.
    """
    os.umask(0o077)

    payload = _read_stdin_json(stdin)
    if payload is None:
        return

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir_env:
        return
    state_dir = Path(state_dir_env)

    tid = payload.get("tool_use_id")
    if not isinstance(tid, str) or not tid:
        return

    map_file = state_dir / "subagent_map" / f"{tid}.json"
    if not map_file.is_file():
        # Native subagent ran (PreToolUse passed through); nothing to do.
        return

    target_name = ""
    try:
        map_data = json.loads(map_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("rewrite: failed to read map file {}: {}", map_file, e)
        map_data = None
    if isinstance(map_data, dict):
        raw_target = map_data.get("target_name")
        if isinstance(raw_target, str):
            target_name = raw_target

    result_file = state_dir / "subagent_results" / f"{tid}.txt"
    prompt_file = state_dir / "subagent_prompts" / f"{tid}.md"
    script_file = state_dir / "proxy_commands" / f"wait-{tid}.sh"
    env_file = state_dir / "proxy_commands" / f"env-{tid}.env"
    init_flag = state_dir / "proxy_commands" / f"initialized-{tid}"

    # Destroy the actual subagent FIRST -- it's the heavy/slow piece and
    # we want it kicked off before we touch any of the local files. The
    # destroy is fire-and-forget so this returns instantly.
    if target_name:
        destroy_log = state_dir / "subagent_destroy.log"
        destroy_callable(target_name, destroy_log)

    # Clean up state-dir-internal artifacts. Leave the wait-script and
    # init_flag in place so a stray re-invocation by Haiku (which can
    # happen if Haiku ignores the MNGR_PROXY_END_OF_OUTPUT sentinel and
    # loops on its Bash tool) finds an idempotent script that emits just
    # the sentinel and exits 0, rather than a missing-file error that
    # keeps Haiku looping. The SessionStart reaper sweeps stale
    # wait-script + init_flag pairs on the next parent boot.
    for path in (env_file, prompt_file, map_file, result_file):
        _best_effort_unlink(path)
    del script_file, init_flag  # intentionally retained


def main() -> None:
    """PostToolUse:Agent hook entry point. Wires up the real stdin/stdout."""
    run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
