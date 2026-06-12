"""PreToolUse:Agent hook for the plugin's DENY mode.

Emits a PreToolUse decision JSON on stdout that DENIES the Task tool
with a short ``permissionDecisionReason`` pointing Claude at the
``mngr-proxy`` Claude skill (provisioned at
``.claude/skills/mngr-proxy/SKILL.md`` by ``plugin.py``). The
skill explains the explicit two-command spawn-and-wait protocol
Claude should use instead of Task.

We deliberately do NOT generate per-Task-call wait-scripts or write
prompt sidefiles. The skill is the single source of truth for the
protocol; uniform invocation is cleaner than offering two redundant
ways to do the same thing.

Depth-limit enforcement is still done here: at or beyond
``MNGR_MAX_SUBAGENT_DEPTH`` (default 3) the hook emits a depth-limit
deny instead of the usual skill-pointer deny, so a chain of
subagents that follow the skill's protocol cannot grow unbounded.

No subagent is spawned automatically by this hook. No PostToolUse
cleanup. No Stop-hook guarding. DENY mode does install the same
label-driven ``hooks/reap.py`` SessionStart hook that PROXY uses
(both spawn paths attach the same parent-id label), but that lives
in its own module and is unrelated to the deny-on-Task behavior
this file implements. See the plugin README's "DENY mode" section
for the full surface comparison.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final
from typing import TextIO

from loguru import logger

from imbue.mngr_claude_subagent_proxy.hook_io import DEFAULT_MAX_SUBAGENT_DEPTH
from imbue.mngr_claude_subagent_proxy.hook_io import emit_depth_limit_deny
from imbue.mngr_claude_subagent_proxy.hook_io import emit_pre_tool_deny
from imbue.mngr_claude_subagent_proxy.hook_io import parse_int_env
from imbue.mngr_claude_subagent_proxy.hook_io import read_hook_stdin_json
from imbue.mngr_claude_subagent_proxy.hooks.agent_definitions import resolve_agent_definition

DENY_REASON: Final[str] = (
    "mngr_claude_subagent_proxy is in deny mode: the Task tool is disabled for this agent. "
    "Use a mngr-managed subagent instead -- see the `mngr-proxy` skill for the "
    "two-command spawn-and-wait protocol."
)


def _build_typed_subagent_pointer(subagent_type: str, work_dir: Path) -> str | None:
    """Build the typed-subagent pointer suffix appended to ``DENY_REASON``.

    Returns ``None`` when ``subagent_type`` is empty or resolves to no
    on-disk definition (built-in types like ``general-purpose``); the
    deny reason stays the short uniform skill pointer in that case.

    When the type resolves, the suffix names the resolved ``.md`` path
    so Claude can prepend its body to the prompt file before spawning
    -- preserving Claude Code's typed-subagent system-prompt contract.
    The body itself is intentionally NOT inlined: it can be thousands
    of characters and would dwarf the actual deny reason.
    """
    if not subagent_type:
        return None
    definition = resolve_agent_definition(subagent_type, work_dir)
    if definition is None:
        return None
    return (
        f" For subagent_type {subagent_type!r}, prepend the body of "
        f"{definition.path} to your prompt file (it is the spawned subagent's "
        f"system prompt) before running `mngr create`."
    )


def run(stdin: TextIO, stdout: TextIO) -> None:
    """PreToolUse:Agent deny hook core.

    Pure function in terms of dependencies: takes stdin/stdout streams
    explicitly, reads env vars directly.

    The depth-limit check matches PROXY mode's: at or beyond
    ``MNGR_MAX_SUBAGENT_DEPTH`` (default 3) the hook emits a deny
    citing the depth, NOT the usual "use a mngr subagent" deny.
    """
    depth = parse_int_env("MNGR_SUBAGENT_DEPTH", 0)
    max_depth = parse_int_env("MNGR_MAX_SUBAGENT_DEPTH", DEFAULT_MAX_SUBAGENT_DEPTH)
    if depth >= max_depth:
        logger.warning("deny: depth {}/{} reached; denying Task with depth-limit reason", depth, max_depth)
        emit_depth_limit_deny(stdout, depth, max_depth)
        return

    # Read stdin through the shared parser. The base deny is uniform
    # regardless of tool_input content, but we DO read ``subagent_type``
    # off the parsed payload to optionally append a typed-subagent
    # pointer (path to the agent definition .md file whose body the
    # spawned subagent should receive as its system prompt). Using the
    # same primitive as hooks/spawn.py keeps malformed-input warnings
    # uniform across the two PreToolUse hooks (single source of truth
    # for stdin validation in hook_io.read_hook_stdin_json).
    payload = read_hook_stdin_json(stdin, "deny")
    subagent_type = ""
    if payload is not None:
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict):
            raw = tool_input.get("subagent_type")
            if isinstance(raw, str):
                subagent_type = raw
    typed_suffix = _build_typed_subagent_pointer(subagent_type, Path.cwd())
    reason = DENY_REASON if typed_suffix is None else DENY_REASON + typed_suffix
    emit_pre_tool_deny(stdout, reason)


def main() -> None:
    """PreToolUse:Agent deny hook entry point. Wires up the real stdin/stdout."""
    run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
