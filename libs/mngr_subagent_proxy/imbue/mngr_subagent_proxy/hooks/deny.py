"""PreToolUse:Agent hook for the plugin's DENY mode.

Reads the hook JSON from stdin and emits a PreToolUse decision JSON on
stdout that DENIES the Task tool with a short ``permissionDecisionReason``
of the form::

    Use a mngr subagent instead of Task. Run: bash <wait_script_path>
    (see the `mngr-subagents` skill for context).

The accompanying ``mngr-subagents`` Claude skill (provisioned in deny
mode at ``.claude/skills/mngr-subagents/SKILL.md``) explains the full
protocol. We deliberately keep this deny message short so it does not
crowd the parent's transcript on every Task call -- the verbose
context lives in the skill, loaded on demand.

The wait-script (``$MNGR_AGENT_STATE_DIR/proxy_commands/wait-<tid>.sh``)
spawns a mngr-managed subagent via ``mngr create`` and blocks on
``subagent_wait`` until end_turn, then prints the subagent's reply to
stdout. Claude runs that one Bash command and uses the script's stdout
as if it were the Task tool's tool_result.

No subagent is spawned automatically by this hook. No PostToolUse
cleanup is installed. No SessionStart reaper. No Stop-hook guarding.
This is deliberately a much smaller surface than PROXY mode -- see the
plugin README's "DENY mode" section.

On failure modes (missing env, malformed input, etc.) emits a generic
deny so Claude is still informed; the Task tool never silently passes
through in deny mode.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Final
from typing import TextIO

from loguru import logger

from imbue.mngr_subagent_proxy._hook_io import DEFAULT_MAX_SUBAGENT_DEPTH
from imbue.mngr_subagent_proxy._hook_io import emit_depth_limit_deny
from imbue.mngr_subagent_proxy._hook_io import emit_pre_tool_deny
from imbue.mngr_subagent_proxy._hook_io import parse_int_env
from imbue.mngr_subagent_proxy._hook_io import read_hook_stdin_json
from imbue.mngr_subagent_proxy._hook_io import write_executable_file
from imbue.mngr_subagent_proxy._hook_io import write_secure_file
from imbue.mngr_subagent_proxy._target_name import build_subagent_target_name
from imbue.mngr_subagent_proxy.mngr_binary import get_mngr_command_shell_form

_GENERIC_DENY_REASON: Final[str] = (
    "mngr_subagent_proxy is in deny mode: the Task tool is disabled for this agent. "
    "Use a mngr-managed subagent instead. See the `mngr-subagents` skill for the protocol."
)


def build_deny_reason(wait_script: Path, run_in_background: bool) -> str:
    """Build the short deny reason addressed to Claude.

    Verbose context (when to use, parsing protocol, inspection commands)
    lives in the ``mngr-subagents`` skill, not here. The deny reason is
    a one-liner pointer + the concrete command for this Task call.
    """
    if run_in_background:
        return (
            f"Use a mngr subagent instead of Task. "
            f"Run: bash {shlex.quote(str(wait_script))} --spawn-only "
            f"(see the `mngr-subagents` skill for context). "
            f"The script returns immediately; the subagent runs in the background."
        )
    return (
        f"Use a mngr subagent instead of Task. "
        f"Run: bash {shlex.quote(str(wait_script))} "
        f"(see the `mngr-subagents` skill for context). "
        f"The script's stdout is the subagent's reply -- treat it as the Task tool's tool_result."
    )


def build_deny_wait_script(tool_use_id: str, target_name: str, parent_cwd: str) -> str:
    """Build the per-Task-call wait-script for deny mode.

    Same shape as ``hooks/spawn.build_wait_script`` but simpler: the
    runner is Claude, not Haiku, so we drop the Haiku-specific ceremony
    (``MNGR_PROXY_END_OF_OUTPUT`` sentinel, idempotent re-entry guard
    keyed on PostToolUse cleanup, watermark sidefile for permission
    redo, ``mngr-proxy-child`` agent type). Spawned children are plain
    ``claude`` agents labeled ``mngr_subagent_proxy=child``.

    The script:
    1. Captures the parent's env to a temporary file (under EXIT trap so
       a partial write cannot leave secrets on disk), then runs
       ``mngr create --reuse`` with the prompt sidefile written by the
       deny hook.
    2. With ``--spawn-only``, exits 0 once the subagent is created.
    3. Otherwise, blocks on ``subagent_wait`` and prints the subagent's
       end-turn reply (with the ``END_TURN:`` prefix stripped) to stdout.
    4. On ``PERMISSION_REQUIRED:<name>``, prints
       ``NEED_PERMISSION: <name>`` and exits 1 so Claude (and the user)
       see they need to ``mngr connect <name>`` to resolve.
    """
    q_tid = shlex.quote(tool_use_id)
    q_target = shlex.quote(target_name)
    q_parent_cwd = shlex.quote(parent_cwd)
    mngr_cmd = get_mngr_command_shell_form()
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
        "\n"
        'mkdir -p "$STATE_DIR/proxy_commands"\n'
        "\n"
        'if [ ! -f "$INIT_FLAG" ]; then\n'
        # Trap covers the env-file (parent secrets) for any exit path.
        # Installed BEFORE the redirect so a signal between the redirect
        # and the trap cannot leave secrets on disk.
        '    trap \'shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"\' EXIT\n'
        "    env | grep -Ev "
        "'^(MNGR_AGENT_STATE_DIR|MNGR_AGENT_NAME|MAIN_CLAUDE_SESSION_ID|MNGR_HOST_DIR)=' "
        '> "$ENV_FILE"\n'
        # --reuse: idempotent. If a previous run partially succeeded
        # (host provisioned but message-delivery errored), the next run
        # must adopt the existing same-named agent rather than fail.
        f'    {mngr_cmd} create "$TARGET_NAME:$PARENT_CWD" \\\n'
        "        --type claude \\\n"
        "        --transfer=none \\\n"
        "        --no-ensure-clean \\\n"
        "        --no-connect \\\n"
        "        --reuse \\\n"
        '        --env-file "$ENV_FILE" \\\n'
        '        --message-file "$PROMPT_FILE" \\\n'
        "        --label mngr_subagent_proxy=child \\\n"
        "        --env MNGR_SUBAGENT_DEPTH=$((${MNGR_SUBAGENT_DEPTH:-0}+1))\n"
        '    shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"\n'
        "    trap - EXIT\n"
        '    touch "$INIT_FLAG"\n'
        "fi\n"
        "\n"
        # Background: spawn-only, return without waiting.
        'if [ "${1:-}" = "--spawn-only" ]; then\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "output=$(uv run python -m imbue.mngr_subagent_proxy.subagent_wait "
        '"$TARGET_NAME")\n'
        'case "$output" in\n'
        "    END_TURN:*)\n"
        # Print the subagent's end-turn body verbatim. Claude reads
        # stdout directly -- no sentinel needed (Claude is the runner,
        # not Haiku).
        "        printf '%s\\n' \"${output#END_TURN:}\"\n"
        "        ;;\n"
        "    PERMISSION_REQUIRED:*)\n"
        '        echo "NEED_PERMISSION: $TARGET_NAME" >&2\n'
        "        exit 1\n"
        "        ;;\n"
        "    *)\n"
        '        echo "ERROR: unexpected subagent_wait output: $output" >&2\n'
        "        exit 1\n"
        "        ;;\n"
        "esac\n"
    )


def run(stdin: TextIO, stdout: TextIO) -> None:
    """PreToolUse:Agent deny hook core.

    Pure function in terms of its dependencies: takes stdin/stdout streams
    explicitly. Reads env vars and filesystem state directly.

    The depth-limit check matches PROXY mode's: at or beyond
    ``MNGR_MAX_SUBAGENT_DEPTH`` (default 3) the hook emits a deny
    citing the depth, NOT the usual "use mngr instead" deny. This
    keeps Claude (or any reader of the parent transcript) from being
    pointed at a wait-script that would happily spawn another nested
    subagent and grow the chain unbounded.
    """
    os.umask(0o077)

    depth = parse_int_env("MNGR_SUBAGENT_DEPTH", 0)
    max_depth = parse_int_env("MNGR_MAX_SUBAGENT_DEPTH", DEFAULT_MAX_SUBAGENT_DEPTH)
    if depth >= max_depth:
        logger.warning("deny: depth {}/{} reached; denying Task with depth-limit reason", depth, max_depth)
        emit_depth_limit_deny(stdout, depth, max_depth)
        return

    payload = read_hook_stdin_json(stdin, "deny")
    if payload is None:
        emit_pre_tool_deny(stdout, _GENERIC_DENY_REASON)
        return

    tool_use_id = payload.get("tool_use_id")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    orig_prompt = tool_input.get("prompt") or ""
    orig_desc = tool_input.get("description") or ""
    orig_run_bg = bool(tool_input.get("run_in_background", False))

    if not isinstance(tool_use_id, str) or not tool_use_id or not isinstance(orig_prompt, str) or not orig_prompt:
        logger.warning("deny: missing tool_use_id or prompt in hook input; emitting generic deny")
        emit_pre_tool_deny(stdout, _GENERIC_DENY_REASON)
        return

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    parent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not state_dir_env or not parent_name:
        logger.warning("deny: missing MNGR_AGENT_STATE_DIR or MNGR_AGENT_NAME; emitting generic deny")
        emit_pre_tool_deny(stdout, _GENERIC_DENY_REASON)
        return

    target_name = build_subagent_target_name(parent_name, orig_desc, tool_use_id)
    parent_cwd = str(Path.cwd())
    state_dir = Path(state_dir_env)
    prompt_file = state_dir / "subagent_prompts" / f"{tool_use_id}.md"
    wait_script = state_dir / "proxy_commands" / f"wait-{tool_use_id}.sh"

    try:
        write_secure_file(prompt_file, orig_prompt)
    except OSError as e:
        logger.warning("deny: failed to write prompt file {}: {}", prompt_file, e)
        emit_pre_tool_deny(stdout, _GENERIC_DENY_REASON)
        return

    try:
        write_executable_file(wait_script, build_deny_wait_script(tool_use_id, target_name, parent_cwd))
    except OSError as e:
        logger.warning("deny: failed to write wait script {}: {}", wait_script, e)
        emit_pre_tool_deny(stdout, _GENERIC_DENY_REASON)
        return

    emit_pre_tool_deny(stdout, build_deny_reason(wait_script, orig_run_bg))


def main() -> None:
    """PreToolUse:Agent deny hook entry point. Wires up the real stdin/stdout."""
    run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
