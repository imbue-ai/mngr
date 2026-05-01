"""PreToolUse:Agent hook for the plugin's DENY mode.

Emits a PreToolUse decision JSON on stdout that DENIES the Task tool
with a short ``permissionDecisionReason`` pointing Claude at the
``mngr-subagents`` Claude skill (provisioned at
``.claude/skills/mngr-subagents/SKILL.md`` by ``plugin.py``). The
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
cleanup. No SessionStart reaper. No Stop-hook guarding. This is
deliberately a much smaller surface than PROXY mode -- see the
plugin README's "DENY mode" section.
"""

from __future__ import annotations

import os
import sys
from typing import Final
from typing import TextIO

from loguru import logger

from imbue.mngr_subagent_proxy.hook_io import DEFAULT_MAX_SUBAGENT_DEPTH
from imbue.mngr_subagent_proxy.hook_io import emit_depth_limit_deny
from imbue.mngr_subagent_proxy.hook_io import emit_pre_tool_deny
from imbue.mngr_subagent_proxy.hook_io import parse_int_env
from imbue.mngr_subagent_proxy.hook_io import read_hook_stdin_json

DENY_REASON: Final[str] = (
    "mngr_subagent_proxy is in deny mode: the Task tool is disabled for this agent. "
    "Use a mngr-managed subagent instead -- see the `mngr-subagents` skill for the "
    "two-command spawn-and-wait protocol."
)


def run(stdin: TextIO, stdout: TextIO) -> None:
    """PreToolUse:Agent deny hook core.

    Pure function in terms of dependencies: takes stdin/stdout streams
    explicitly, reads env vars directly.

    The depth-limit check matches PROXY mode's: at or beyond
    ``MNGR_MAX_SUBAGENT_DEPTH`` (default 3) the hook emits a deny
    citing the depth, NOT the usual "use a mngr subagent" deny.
    """
    os.umask(0o077)

    depth = parse_int_env("MNGR_SUBAGENT_DEPTH", 0)
    max_depth = parse_int_env("MNGR_MAX_SUBAGENT_DEPTH", DEFAULT_MAX_SUBAGENT_DEPTH)
    if depth >= max_depth:
        logger.warning("deny: depth {}/{} reached; denying Task with depth-limit reason", depth, max_depth)
        emit_depth_limit_deny(stdout, depth, max_depth)
        return

    # Drain stdin so the parent runner sees clean closure. We don't use
    # any of the content -- the deny is uniform regardless of what
    # Claude was trying to delegate.
    read_hook_stdin_json(stdin, "deny")
    emit_pre_tool_deny(stdout, DENY_REASON)


def main() -> None:
    """PreToolUse:Agent deny hook entry point. Wires up the real stdin/stdout."""
    run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
