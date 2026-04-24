"""PostToolUse:Agent hook. Replaces the Haiku proxy's tool output with the
real END_TURN content harvested from the mngr subagent, then tears down the
subagent and cleans up per-tool_use_id state files. Exits 0 on any failure
so Claude Code keeps running.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr_subagent_proxy.hooks.mngr_api import destroy_agent_detached


def _emit(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def _read_stdin_json() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
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


def main() -> None:
    """PostToolUse:Agent hook entry point."""
    os.umask(0o077)

    payload = _read_stdin_json()
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

    output_text: str
    try:
        raw_result = result_file.read_text()
    except FileNotFoundError:
        raw_result = ""
    except OSError as e:
        logger.warning("rewrite: failed to read result file {}: {}", result_file, e)
        raw_result = ""

    if raw_result:
        output_text = raw_result
    else:
        display_name = target_name or "<unknown>"
        output_text = (
            f"ERROR: mngr subagent {display_name} produced no result "
            "(crashed or proxy failed). Check the mngr agent log."
        )

    response: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": output_text,
        }
    }
    _emit(response)

    # Best-effort detached teardown of the mngr subagent.
    if target_name:
        destroy_log = state_dir / "subagent_destroy.log"
        destroy_agent_detached(target_name, destroy_log)

    for path in (env_file, prompt_file, map_file, result_file, script_file, init_flag):
        _best_effort_unlink(path)


if __name__ == "__main__":
    main()
