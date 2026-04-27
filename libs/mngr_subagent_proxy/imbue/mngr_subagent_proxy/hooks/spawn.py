"""PreToolUse:Agent hook. Routes a Claude Task tool invocation through an
mngr-managed proxy subagent instead of Claude's native nested Agent loop.

Reads the hook JSON from stdin, writes per-tool_use_id side files (prompt,
map, wait-script) under $MNGR_AGENT_STATE_DIR, and emits a PreToolUse
decision JSON on stdout. On failure modes (missing env, malformed input,
etc.) passes through so the native Task tool runs unchanged. At or beyond
the configured depth limit, denies the Task tool with an explanatory
reason so Claude does not spawn nested subagents beyond that depth.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any
from typing import Final
from typing import TextIO

from loguru import logger

_DEFAULT_MAX_DEPTH: Final[int] = 3
_PASS_THROUGH_RESPONSE: Final[dict[str, Any]] = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
}


def _emit(stdout: TextIO, response: dict[str, Any]) -> None:
    """Write a JSON response to stdout and flush."""
    stdout.write(json.dumps(response) + "\n")
    stdout.flush()


def _emit_pass_through(stdout: TextIO) -> None:
    _emit(stdout, _PASS_THROUGH_RESPONSE)


def _emit_depth_limit_deny(stdout: TextIO, depth: int, max_depth: int) -> None:
    """Emit a deny decision with an explanatory reason (depth limit reached)."""
    reason = (
        f"mngr_subagent_proxy: subagent depth limit ({depth}/{max_depth}) reached. "
        "Cannot spawn nested Task tools beyond this depth."
    )
    _emit(
        stdout,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
    )


def _read_stdin_json(stdin: TextIO) -> dict[str, Any] | None:
    """Read hook JSON from stdin; return None on empty or malformed input."""
    try:
        raw = stdin.read()
    except OSError as e:
        logger.warning("spawn: failed to read stdin: {}", e)
        return None
    if not raw:
        logger.warning("spawn: empty stdin")
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("spawn: malformed stdin JSON: {}", e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("spawn: stdin JSON is not an object")
        return None
    return parsed


def _parse_int_env(name: str, default: int) -> int:
    """Parse an int-valued env var; return default on missing/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def slugify(text: str) -> str:
    """Lowercase, replace non-alnum with '-', collapse repeats, trim, cap at 30."""
    lowered = text.lower()
    converted_chars = [ch if ch.isalnum() else "-" for ch in lowered]
    converted = "".join(converted_chars)
    # collapse runs of '-'
    collapsed_parts: list[str] = []
    prev_dash = False
    for ch in converted:
        if ch == "-":
            if prev_dash:
                continue
            prev_dash = True
        else:
            prev_dash = False
        collapsed_parts.append(ch)
    collapsed = "".join(collapsed_parts).strip("-")
    capped = collapsed[:30]
    return capped.rstrip("-")


def _build_wait_script(tool_use_id: str, target_name: str, parent_cwd: str) -> str:
    """Build the small per-tool_use_id shell wait-script that Haiku invokes via Bash.

    The wait-script is a boundary to a shell-only consumer (the Haiku proxy
    agent's Bash tool), so it remains shell. Values are baked in as literals
    via shlex.quote so the script does not depend on the hook's env at run
    time beyond MNGR_AGENT_STATE_DIR / MNGR_SUBAGENT_DEPTH.
    """
    q_tid = shlex.quote(tool_use_id)
    q_target = shlex.quote(target_name)
    q_parent_cwd = shlex.quote(parent_cwd)
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "umask 077\n"
        "\n"
        f"TID={q_tid}\n"
        f"TARGET_NAME={q_target}\n"
        f"PARENT_CWD={q_parent_cwd}\n"
        'STATE_DIR="${MNGR_AGENT_STATE_DIR:?MNGR_AGENT_STATE_DIR not set}"\n'
        'ENV_FILE="$STATE_DIR/proxy_commands/env-$TID.env"\n'
        'INIT_FLAG="$STATE_DIR/proxy_commands/initialized-$TID"\n'
        'PROMPT_FILE="$STATE_DIR/subagent_prompts/$TID.md"\n'
        'RESULT_FILE="$STATE_DIR/subagent_results/$TID.txt"\n'
        "\n"
        'if [ ! -f "$INIT_FLAG" ]; then\n'
        "    env | grep -Ev "
        "'^(MNGR_AGENT_STATE_DIR|MNGR_AGENT_NAME|MAIN_CLAUDE_SESSION_ID|MNGR_HOST_DIR)=' "
        '> "$ENV_FILE"\n'
        '    uv run mngr create "$TARGET_NAME:$PARENT_CWD" \\\n'
        "        --type mngr-proxy-child \\\n"
        "        --transfer=none \\\n"
        "        --no-ensure-clean \\\n"
        "        --no-connect \\\n"
        '        --env-file "$ENV_FILE" \\\n'
        '        --message-file "$PROMPT_FILE" \\\n'
        "        --label mngr_subagent_proxy=child \\\n"
        "        --env MNGR_SUBAGENT_PROXY_CHILD=1 \\\n"
        "        --env MNGR_SUBAGENT_DEPTH=$((${MNGR_SUBAGENT_DEPTH:-0}+1))\n"
        '    shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"\n'
        '    touch "$INIT_FLAG"\n'
        "fi\n"
        "\n"
        'mkdir -p "$(dirname "$RESULT_FILE")"\n'
        'output=$(uv run python -m imbue.mngr_subagent_proxy.subagent_wait "$TARGET_NAME")\n'
        'case "$output" in\n'
        "    END_TURN:*)\n"
        '        printf \'%s\' "${output#END_TURN:}" > "$RESULT_FILE"\n'
        # Print the subagent end-turn text as the wait-script's stdout so
        # Haiku's Bash captures it; Haiku is instructed to echo this verbatim
        # in its own final reply, which becomes the parent's tool_result. The
        # END_OF_OUTPUT sentinel is the one stable signal Haiku uses to
        # decide it's done -- the body content is opaque text from a real
        # subagent and may contain anything (including 'DONE' literally).
        "        printf '%s\\n' \"${output#END_TURN:}\"\n"
        '        echo "MNGR_PROXY_END_OF_OUTPUT"\n'
        "        ;;\n"
        "    PERMISSION_REQUIRED:*)\n"
        '        echo "NEED_PERMISSION: $TARGET_NAME"\n'
        "        ;;\n"
        "    *)\n"
        '        echo "ERROR: unexpected subagent_wait output: $output" >&2\n'
        "        exit 1\n"
        "        ;;\n"
        "esac\n"
    )


def _write_secure_file(path: Path, content: str) -> None:
    """Write content to path with 0600 perms, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _write_executable_file(path: Path, content: str) -> None:
    """Write content to path with 0755 perms, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def run(stdin: TextIO, stdout: TextIO) -> None:
    """PreToolUse:Agent hook core.

    Pure function in terms of its dependencies: takes stdin/stdout streams
    explicitly. Reads env vars and filesystem state directly.
    """
    os.umask(0o077)

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    parent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not state_dir_env:
        logger.warning("spawn: MNGR_AGENT_STATE_DIR unset; passing through")
        _emit_pass_through(stdout)
        return
    if not parent_name:
        logger.warning("spawn: MNGR_AGENT_NAME unset; passing through")
        _emit_pass_through(stdout)
        return

    depth = _parse_int_env("MNGR_SUBAGENT_DEPTH", 0)
    max_depth = _parse_int_env("MNGR_MAX_SUBAGENT_DEPTH", _DEFAULT_MAX_DEPTH)
    if depth >= max_depth:
        logger.warning("spawn: depth {}/{} reached; denying Task", depth, max_depth)
        _emit_depth_limit_deny(stdout, depth, max_depth)
        return

    payload = _read_stdin_json(stdin)
    if payload is None:
        _emit_pass_through(stdout)
        return

    tool_use_id = payload.get("tool_use_id")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    orig_prompt = tool_input.get("prompt") or ""
    orig_desc = tool_input.get("description") or ""
    orig_subagent_type = tool_input.get("subagent_type") or ""
    orig_run_bg = bool(tool_input.get("run_in_background", False))

    if not isinstance(tool_use_id, str) or not tool_use_id or not isinstance(orig_prompt, str) or not orig_prompt:
        logger.warning("spawn: missing tool_use_id or prompt in hook input")
        _emit_pass_through(stdout)
        return

    slug = slugify(orig_desc or "subagent") or "subagent"
    tid_suffix = tool_use_id[-8:]
    target_name = f"{parent_name}--subagent-{slug}-{tid_suffix}"

    parent_cwd = str(Path.cwd())

    state_dir = Path(state_dir_env)
    prompts_dir = state_dir / "subagent_prompts"
    map_dir = state_dir / "subagent_map"
    cmd_dir = state_dir / "proxy_commands"
    results_dir = state_dir / "subagent_results"

    try:
        for d in (prompts_dir, map_dir, cmd_dir, results_dir):
            d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("spawn: failed to create state subdirs under {}: {}", state_dir, e)
        _emit_pass_through(stdout)
        return

    prompt_file = prompts_dir / f"{tool_use_id}.md"
    map_file = map_dir / f"{tool_use_id}.json"
    script_file = cmd_dir / f"wait-{tool_use_id}.sh"

    try:
        _write_secure_file(prompt_file, orig_prompt)
    except OSError as e:
        logger.warning("spawn: failed to write prompt file {}: {}", prompt_file, e)
        _emit_pass_through(stdout)
        return

    map_payload = {
        "target_name": target_name,
        "subagent_type": orig_subagent_type,
        "parent_cwd": parent_cwd,
        "run_in_background": orig_run_bg,
    }
    try:
        _write_secure_file(map_file, json.dumps(map_payload))
    except OSError as e:
        logger.warning("spawn: failed to write map file {}: {}", map_file, e)
        _emit_pass_through(stdout)
        return

    wait_script_content = _build_wait_script(tool_use_id, target_name, parent_cwd)
    try:
        _write_executable_file(script_file, wait_script_content)
    except OSError as e:
        logger.warning("spawn: failed to write wait-script {}: {}", script_file, e)
        _emit_pass_through(stdout)
        return

    new_prompt = (
        f"You are an mngr-proxy dispatcher for target agent {target_name!r}. "
        f"Run this exact Bash call: "
        f'Bash(command="bash {script_file}", timeout=1800000). '
        f"\n\n"
        f"Examine the Bash stdout:\n"
        f"- If it contains a line 'NEED_PERMISSION: <name>': "
        f"run Bash(command=\"fake_tool 'subagent <name> waiting; run in another terminal: mngr connect <name>'\", "
        f"timeout=60000), then re-run the original bash command above with the same path. "
        f"Repeat until you see the next case.\n"
        f"- If it contains a line 'MNGR_PROXY_END_OF_OUTPUT' as the last line: "
        f"reply with EXACTLY the stdout content with that final 'MNGR_PROXY_END_OF_OUTPUT' line removed, "
        f"and nothing else (no preamble, no commentary, no markdown wrappers). "
        f"Then end your turn.\n\n"
        f"Do NOT use shell variables, ask questions, or take any other action -- "
        f"the path above is your only command. The stdout content (minus the sentinel) "
        f"is the real subagent's output and is what the user is waiting for."
    )

    response: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "description": orig_desc,
                "subagent_type": "mngr-proxy",
                "prompt": new_prompt,
                "run_in_background": orig_run_bg,
            },
        }
    }
    _emit(stdout, response)


def main() -> None:
    """PreToolUse:Agent hook entry point. Wires up the real stdin/stdout."""
    run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
