"""PreToolUse:Agent hook for the plugin's DENY mode.

Reads the hook JSON from stdin, writes a per-tool_use_id prompt sidefile
under ``$MNGR_AGENT_STATE_DIR/subagent_prompts/`` (so a long Task prompt
does not have to be embedded inline in the deny message), and emits a
PreToolUse decision JSON on stdout that DENIES the Task tool with a
``permissionDecisionReason`` that gives Claude a copy-pasteable
``mngr create`` / ``subagent_wait`` invocation.

The intent is that Claude (the calling agent) reads the deny reason,
runs the suggested commands itself via Bash, and treats the printed
reply as if it were the Task tool's tool_result -- continuing as it
normally would after a Task call.

No subagent is spawned automatically. No PostToolUse cleanup is
installed. No SessionStart reaper. No Stop-hook guarding. This is
deliberately a much smaller surface than PROXY mode -- see the plugin
README's "Deny mode" section.

On failure modes (missing env, malformed input, etc.) emits a generic
deny so Claude is still informed; the Task tool never silently passes
through in deny mode.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any
from typing import Final
from typing import TextIO

from loguru import logger

from imbue.mngr_subagent_proxy.hooks.spawn import slugify

_GENERIC_DENY_REASON: Final[str] = (
    "mngr_subagent_proxy is in deny mode: the Task tool is disabled for this agent. "
    "Use mngr instead. Run via Bash: "
    "`uv run mngr create <name> --type claude --message <prompt>` to spawn an "
    "mngr-managed subagent, then `uv run python -m imbue.mngr_subagent_proxy.subagent_wait <name>` "
    "to wait for its reply (which is printed as `END_TURN:<reply>`)."
)


def _emit(stdout: TextIO, response: dict[str, Any]) -> None:
    """Write a JSON response to stdout and flush."""
    stdout.write(json.dumps(response) + "\n")
    stdout.flush()


def _emit_deny(stdout: TextIO, reason: str) -> None:
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
    try:
        raw = stdin.read()
    except OSError as e:
        logger.warning("deny: failed to read stdin: {}", e)
        return None
    if not raw:
        logger.warning("deny: empty stdin")
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("deny: malformed stdin JSON: {}", e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("deny: stdin JSON is not an object")
        return None
    return parsed


def build_deny_reason(
    target_name: str,
    prompt_file: Path,
    parent_cwd: str,
    run_in_background: bool,
) -> str:
    """Build the permissionDecisionReason string addressed to the calling agent.

    Mirrors the structure of the PROXY-mode Haiku prompt: states the
    rule, gives explicit commands, and labels the expected output. The
    instructions are addressed to Claude (the calling agent), so Claude
    can continue acting "almost the way it normally uses subagents" --
    spawn via Bash, capture the reply, continue.
    """
    address = shlex.quote(f"{target_name}:{parent_cwd}")
    quoted_prompt_file = shlex.quote(str(prompt_file))
    quoted_target = shlex.quote(target_name)

    if run_in_background:
        return (
            f"mngr_subagent_proxy is in deny mode: the Task tool is disabled for this "
            f"agent. The original Task call asked for run_in_background=true, so spawn "
            f"the equivalent mngr-managed subagent in the background and continue your "
            f"turn without waiting for it.\n"
            f"\n"
            f"Run this in Bash exactly:\n"
            f"\n"
            f"    uv run mngr create {address} \\\n"
            f"        --type claude --transfer=none --no-ensure-clean --no-connect \\\n"
            f"        --label mngr_subagent_proxy=child \\\n"
            f"        --message-file {quoted_prompt_file}\n"
            f"\n"
            f"That command returns immediately once the subagent has been spawned. "
            f"Continue your turn; the subagent runs to completion in the background.\n"
            f"\n"
            f"To inspect or interact with the running subagent from another terminal:\n"
            f"    mngr connect {target_name}\n"
            f"To follow its transcript:\n"
            f"    mngr transcript {target_name}\n"
            f"\n"
            f"The full prompt for this Task has been written to: {prompt_file}\n"
        )

    return (
        f"mngr_subagent_proxy is in deny mode: the Task tool is disabled for this "
        f"agent. To run this work as a synchronous mngr subagent and use its reply "
        f"as if it were the Task tool's tool_result, run two commands in Bash.\n"
        f"\n"
        f"Step 1 -- spawn the subagent (returns once it is running):\n"
        f"\n"
        f"    uv run mngr create {address} \\\n"
        f"        --type claude --transfer=none --no-ensure-clean --no-connect \\\n"
        f"        --label mngr_subagent_proxy=child \\\n"
        f"        --message-file {quoted_prompt_file}\n"
        f"\n"
        f"Step 2 -- block until it ends its turn and capture its reply:\n"
        f"\n"
        f"    uv run python -m imbue.mngr_subagent_proxy.subagent_wait {quoted_target}\n"
        f"\n"
        f"The wait command prints a single line of the form `END_TURN:<reply>` when "
        f"the subagent ends its turn. Strip the literal `END_TURN:` prefix; the rest "
        f"is the subagent's final reply. Treat it as the result of this Task call and "
        f"continue your own turn.\n"
        f"\n"
        f"While the subagent runs you can also inspect or intervene from another "
        f"terminal: `mngr connect {target_name}`. To follow its transcript: "
        f"`mngr transcript {target_name}`.\n"
        f"\n"
        f"The full prompt for this Task has been written to: {prompt_file}\n"
    )


def _write_secure_file(path: Path, content: str) -> None:
    """Write content to path with 0600 perms, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)


def run(stdin: TextIO, stdout: TextIO) -> None:
    """PreToolUse:Agent deny hook core.

    Pure function in terms of its dependencies: takes stdin/stdout streams
    explicitly. Reads env vars and filesystem state directly.
    """
    os.umask(0o077)

    payload = _read_stdin_json(stdin)
    if payload is None:
        _emit_deny(stdout, _GENERIC_DENY_REASON)
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
        _emit_deny(stdout, _GENERIC_DENY_REASON)
        return

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    parent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not state_dir_env or not parent_name:
        # Without a state dir we can't write a prompt sidefile, and without a
        # parent name we can't synthesize a unique target name. Embed the
        # prompt inline so Claude still has something to copy-paste.
        logger.warning("deny: missing MNGR_AGENT_STATE_DIR or MNGR_AGENT_NAME; emitting inline-prompt deny")
        inline_reason = (
            "mngr_subagent_proxy is in deny mode: the Task tool is disabled for this "
            "agent. Spawn an mngr-managed subagent via Bash:\n"
            "\n"
            "    uv run mngr create <name> --type claude --transfer=none "
            "--no-ensure-clean --no-connect --label mngr_subagent_proxy=child --message <prompt>\n"
            "\n"
            "Then wait for it with:\n"
            "\n"
            "    uv run python -m imbue.mngr_subagent_proxy.subagent_wait <name>\n"
            "\n"
            "The wait command prints `END_TURN:<reply>`. Strip the prefix; the rest is "
            "the subagent's final reply. The original Task prompt was:\n"
            "\n"
            f"{orig_prompt}\n"
        )
        _emit_deny(stdout, inline_reason)
        return

    slug = slugify(orig_desc or "subagent") or "subagent"
    tid_suffix = tool_use_id[-8:]
    target_name = f"{parent_name}--subagent-{slug}-{tid_suffix}"

    parent_cwd = str(Path.cwd())
    state_dir = Path(state_dir_env)
    prompts_dir = state_dir / "subagent_prompts"
    prompt_file = prompts_dir / f"{tool_use_id}.md"

    try:
        _write_secure_file(prompt_file, orig_prompt)
    except OSError as e:
        logger.warning("deny: failed to write prompt file {}: {}", prompt_file, e)
        # Fall back to embedding the prompt inline.
        inline_reason = (
            f"mngr_subagent_proxy is in deny mode: the Task tool is disabled for this "
            f"agent. Spawn an mngr-managed subagent via Bash:\n"
            f"\n"
            f"    uv run mngr create {shlex.quote(f'{target_name}:{parent_cwd}')} "
            f"--type claude --transfer=none --no-ensure-clean --no-connect "
            f"--label mngr_subagent_proxy=child --message <prompt>\n"
            f"\n"
            f"Then wait for it with:\n"
            f"\n"
            f"    uv run python -m imbue.mngr_subagent_proxy.subagent_wait {shlex.quote(target_name)}\n"
            f"\n"
            f"The wait command prints `END_TURN:<reply>`. Strip the prefix; the rest is "
            f"the subagent's final reply. The original Task prompt was:\n"
            f"\n"
            f"{orig_prompt}\n"
        )
        _emit_deny(stdout, inline_reason)
        return

    reason = build_deny_reason(target_name, prompt_file, parent_cwd, orig_run_bg)
    _emit_deny(stdout, reason)


def main() -> None:
    """PreToolUse:Agent deny hook entry point. Wires up the real stdin/stdout."""
    run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
