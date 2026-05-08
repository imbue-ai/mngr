"""Claude rate-limit data writer for `mngr usage`.

Single responsibility: install a per-agent statusline shim into Claude
agents so each render appends a rate-limit event to
``$MNGR_AGENT_STATE_DIR/events/claude/rate_limits/events.jsonl``.

Discovery is by convention -- ``mngr usage`` walks all
``events/<source>/rate_limits/events.jsonl`` files itself, mirroring how
``mngr transcript`` finds ``common_transcript`` events. We don't implement a
reader hookspec; we just write to the conventional path and let the generic
CLI find the data.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_claude.hookspecs import ClaudeExtraSettingsContribution
from imbue.mngr_claude_usage import resources as _resources

_RATE_LIMITS_WRITER_SCRIPT = "claude_rate_limits_writer.sh"
_STATUSLINE_SHIM_SCRIPT = "claude_statusline.sh"


def _format_statusline_command(agent_state_dir: Path) -> str:
    """Return the shell-quoted shim path used as the value of statusLine.command.

    The shim reads MNGR_RATE_LIMITS_WRITER and (optionally) MNGR_USER_STATUSLINE_CMD
    from the agent's process env, so the command itself is just the shim path.
    """
    state_dir = shlex.quote(str(agent_state_dir))
    return f"{state_dir}/commands/{_STATUSLINE_SHIM_SCRIPT}"


def _extract_user_statusline_command(source_settings: dict[str, Any], own_shim_path: str) -> str | None:
    """Pull the user's existing statusLine.command (if any) so we can chain to it.

    Skips ``own_shim_path`` (a previously-installed copy of *this* plugin's shim)
    so re-provisioning doesn't capture our own shim as the "user's command" --
    that would form a recursive wrap.
    """
    statusline = source_settings.get("statusLine")
    if not isinstance(statusline, dict):
        return None
    command = statusline.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if command.strip() == own_shim_path.strip():
        return None
    return command


@hookimpl
def claude_extra_per_agent_settings(
    mngr_ctx: MngrContext,
    source_settings: dict[str, Any],
    agent_state_dir: Path,
    work_dir: Path,
    is_local: bool,
) -> ClaudeExtraSettingsContribution | None:
    """Install the rate-limit statusline shim, wrapping any pre-existing statusLine.

    Skips remote hosts: ``mngr usage`` walks the local host_dir for events files,
    so a remote-only events file would never be visible.
    """
    if not is_local:
        return None

    statusline_command = _format_statusline_command(agent_state_dir)
    env: dict[str, str] = {
        "MNGR_RATE_LIMITS_WRITER": str(agent_state_dir / "commands" / _RATE_LIMITS_WRITER_SCRIPT),
    }
    user_cmd = _extract_user_statusline_command(source_settings, own_shim_path=statusline_command)
    if user_cmd is not None:
        env["MNGR_USER_STATUSLINE_CMD"] = user_cmd

    return ClaudeExtraSettingsContribution(
        statusline_command=statusline_command,
        env=env,
        resource_scripts=(_STATUSLINE_SHIM_SCRIPT, _RATE_LIMITS_WRITER_SCRIPT),
        resource_module=_resources,
    )
