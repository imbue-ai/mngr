from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr_claude.hookspecs import ClaudeExtraSettingsContribution
from imbue.mngr_usage import resources as _usage_resources
from imbue.mngr_usage.cli import cache_path
from imbue.mngr_usage.cli import usage
from imbue.mngr_usage.data_types import UsagePluginConfig

register_plugin_config("usage", UsagePluginConfig)

_RATE_LIMITS_WRITER_SCRIPT = "claude_rate_limits_writer.sh"
_STATUSLINE_SHIM_SCRIPT = "claude_statusline.sh"


def _format_statusline_command(agent_state_dir: Path) -> str:
    """Build the shell snippet that Claude Code's statusLine.command runs.

    Returns the absolute path to the statusline shim. All env vars the shim
    needs (MNGR_RATE_LIMITS_WRITER, MNGR_RATE_LIMITS_CACHE, MNGR_PROFILE_DIR,
    MNGR_USER_STATUSLINE_CMD) are populated through settings.json's `env`
    block, so the command itself does not need an inline env prefix.
    """
    state_dir = shlex.quote(str(agent_state_dir))
    return f"{state_dir}/commands/{_STATUSLINE_SHIM_SCRIPT}"


def _extract_user_statusline_command(source_settings: dict[str, Any]) -> str | None:
    """Pull the user's existing statusLine.command (if any) so we can chain to it.

    Claude Code's settings.json shape:
        {"statusLine": {"type": "command", "command": "..."}}
    """
    statusline = source_settings.get("statusLine")
    if not isinstance(statusline, dict):
        return None
    command = statusline.get("command")
    if isinstance(command, str) and command.strip():
        return command
    return None


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the usage command with mngr."""
    return [usage]


@hookimpl
def claude_extra_per_agent_settings(
    mngr_ctx: MngrContext,
    source_settings: dict[str, Any],
    agent_state_dir: Path,
    is_local: bool,
) -> ClaudeExtraSettingsContribution | None:
    """Install the rate-limit statusline shim into per-agent Claude settings.

    The shim wraps any existing statusLine.command the user already has, so
    composability is preserved. Resource scripts are provisioned to
    $MNGR_AGENT_STATE_DIR/commands/ via the standard mngr_claude path.

    Skips remote hosts: the cache lives under the local user's profile_dir and
    is not reachable from a remote agent's filesystem, so installing the shim
    there would silently write to a remote-only path that `mngr usage` (run
    locally) never reads.

    Env vars steered into the per-agent settings.json:
        MNGR_RATE_LIMITS_WRITER  Path to claude_rate_limits_writer.sh
        MNGR_RATE_LIMITS_CACHE   Path to the shared cache (under profile_dir)
        MNGR_PROFILE_DIR         Profile dir (used as fallback inside the writer)
        MNGR_USER_STATUSLINE_CMD The user's pre-existing statusLine.command (optional)
    """
    if not is_local:
        return None

    env: dict[str, str] = {
        "MNGR_RATE_LIMITS_WRITER": str(agent_state_dir / "commands" / _RATE_LIMITS_WRITER_SCRIPT),
        "MNGR_RATE_LIMITS_CACHE": str(cache_path(mngr_ctx)),
        "MNGR_PROFILE_DIR": str(mngr_ctx.profile_dir),
    }
    user_cmd = _extract_user_statusline_command(source_settings)
    if user_cmd is not None:
        env["MNGR_USER_STATUSLINE_CMD"] = user_cmd

    return ClaudeExtraSettingsContribution(
        statusline_command=_format_statusline_command(agent_state_dir),
        env=env,
        resource_scripts=(_STATUSLINE_SHIM_SCRIPT, _RATE_LIMITS_WRITER_SCRIPT),
        resource_module=_usage_resources,
    )
