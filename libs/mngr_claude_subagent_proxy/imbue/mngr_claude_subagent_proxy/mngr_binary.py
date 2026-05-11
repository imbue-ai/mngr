"""Resolve the right command for invoking ``mngr`` from inside this plugin.

Copied (with a fallback) from
``imbue.mngr_recursive.watcher_common.get_mngr_command`` so we don't have to
introduce an inter-plugin dependency for one small util. If mngr_recursive
adds another caller / changes shape, sync that change here.

Behavior:
- If ``UV_TOOL_BIN_DIR`` is set (mngr_recursive has provisioned a per-agent
  isolated mngr install), prefer that bin's ``mngr`` so recursive invocations
  honor the per-agent install.
- Otherwise fall back to ``uv run mngr``, which works for the common
  local-host case where the worktree's pyproject resolves the mngr CLI.
"""

from __future__ import annotations

import os
from typing import Final

_FALLBACK_MNGR_COMMAND: Final[tuple[str, ...]] = ("uv", "run", "mngr")


def get_mngr_command() -> list[str]:
    """Return the argv prefix for invoking ``mngr``.

    See module docstring for resolution rules.
    """
    bin_dir = os.environ.get("UV_TOOL_BIN_DIR", "")
    if bin_dir:
        candidate = os.path.join(bin_dir, "mngr")
        if os.path.isfile(candidate):
            return [candidate]
    return list(_FALLBACK_MNGR_COMMAND)


def get_mngr_command_shell_form() -> str:
    """Return ``get_mngr_command()`` formatted for direct embedding into a shell script.

    Used by the wait-script template generator (``hooks/spawn.py``) so the
    generated bash invokes the same per-agent binary the Python helpers would.
    """
    parts = get_mngr_command()
    # Per-agent absolute path or `uv run mngr` -- both are safe to embed
    # without extra quoting; no shell metacharacters are produced by either
    # branch on the supported platforms.
    return " ".join(parts)
