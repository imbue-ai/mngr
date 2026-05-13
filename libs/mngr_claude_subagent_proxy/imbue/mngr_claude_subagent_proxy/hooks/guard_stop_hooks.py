"""SessionStart hook (PROXY mode): wrap user Stop / SubagentStop hooks
in the agent's per-agent plugin cache with the
``MNGR_CLAUDE_SUBAGENT_PROXY_CHILD`` env-conditional guard.

PROXY-only. Spawned proxy children carry the
``MNGR_CLAUDE_SUBAGENT_PROXY_CHILD=1`` env var, so guarded hooks no-op
inside them while still firing normally on top-level agents.

Why it lives in a separate hook: PROXY mode and DENY mode share the
exact same ``hooks/reap.py`` (label-driven), but DENY-spawned children
are plain claude agents without the env var, so wrapping the Stop
hooks would be harmless but pointless. Keeping this concern in a
PROXY-only SessionStart hook lets the reap code stay literally
identical across modes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from imbue.mngr_claude_subagent_proxy._stop_hook_guard import guard_per_agent_plugin_cache


def run(stdin: TextIO) -> None:
    """SessionStart hook core. Synchronously wraps Stop hooks in this agent's
    plugin cache.

    Claude Code populates the per-agent plugin cache with files fetched
    fresh from GitHub at session start (not by copying the user
    marketplace dir), so the provisioning-time wrap of the user
    marketplace does not reach the cache. Doing the wrap here -- in a
    SessionStart hook that fires before the FIRST Stop hook -- closes
    that gap. The wrap is idempotent on subsequent SessionStarts.
    """
    try:
        stdin.read()
    except OSError:
        pass

    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir_env:
        return
    guard_per_agent_plugin_cache(Path(state_dir_env))


def main() -> None:
    """SessionStart hook entry point."""
    run(sys.stdin)


if __name__ == "__main__":
    main()
