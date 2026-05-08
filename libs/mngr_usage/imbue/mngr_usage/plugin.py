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
    """Return the shell-quoted statusline shim path for settings.local.json's statusLine.command.

    All env vars the shim needs (MNGR_RATE_LIMITS_WRITER, MNGR_RATE_LIMITS_CACHE,
    MNGR_PROFILE_DIR, MNGR_USER_STATUSLINE_CMD) are exported into the agent's
    process environment via mngr_claude's plugin_env_vars.json mechanism, so the
    command itself is just the shim path -- the shim sees the env vars by
    inheritance from Claude Code's process when the statusline subprocess fires.
    """
    state_dir = shlex.quote(str(agent_state_dir))
    return f"{state_dir}/commands/{_STATUSLINE_SHIM_SCRIPT}"


def _extract_user_statusline_command(source_settings: dict[str, Any], own_shim_path: str) -> str | None:
    """Pull the user's existing statusLine.command (if any) so we can chain to it.

    Claude Code's settings.json shape:
        {"statusLine": {"type": "command", "command": "..."}}

    Skips ``own_shim_path`` (a previously-installed copy of *this* plugin's shim)
    so re-provisioning doesn't capture our own shim as the "user's command" --
    that would form a recursive wrap. In that case the caller should fall back to
    looking at the project-tier (``settings.json``) value, which the merged
    source_settings does not expose if local-tier already had statusLine. A
    follow-up improvement could pre-strip our shim during the merge step in
    ``_read_effective_project_claude_settings``; for now, returning None on a
    self-match is good enough -- a re-provision without a separate user
    statusline just leaves MNGR_USER_STATUSLINE_CMD unset, which the shim
    handles gracefully.
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
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the usage command with mngr."""
    return [usage]


@hookimpl
def claude_extra_per_agent_settings(
    mngr_ctx: MngrContext,
    source_settings: dict[str, Any],
    agent_state_dir: Path,
    work_dir: Path,
    is_local: bool,
) -> ClaudeExtraSettingsContribution | None:
    """Install the rate-limit statusline shim, wrapping any pre-existing statusLine.

    ``source_settings`` is the parsed ``<work_dir>/.claude/settings.json``
    (the project tier of Claude Code's settings stack), so any existing
    project-level ``statusLine.command`` is captured into
    ``MNGR_USER_STATUSLINE_CMD`` for the shim to chain to. mngr_claude
    installs our wrapper into ``<work_dir>/.claude/settings.local.json``
    (the local tier, higher precedence than project), so we wrap rather
    than replace.

    Skips remote hosts: the cache lives under the local user's profile_dir and
    is not reachable from a remote agent's filesystem, so installing the shim
    there would silently write to a remote-only path that `mngr usage` (run
    locally) never reads.

    Env vars exported into the agent's process environment (via
    ClaudeAgent.modify_env_vars / plugin_env_vars.json):
        MNGR_RATE_LIMITS_WRITER  Path to claude_rate_limits_writer.sh
        MNGR_RATE_LIMITS_CACHE   Path to the shared cache (under profile_dir)
        MNGR_PROFILE_DIR         Profile dir (used as fallback inside the writer)
        MNGR_USER_STATUSLINE_CMD The pre-existing statusLine.command (optional)
    """
    if not is_local:
        return None

    statusline_command = _format_statusline_command(agent_state_dir)
    env: dict[str, str] = {
        "MNGR_RATE_LIMITS_WRITER": str(agent_state_dir / "commands" / _RATE_LIMITS_WRITER_SCRIPT),
        "MNGR_RATE_LIMITS_CACHE": str(cache_path(mngr_ctx)),
        "MNGR_PROFILE_DIR": str(mngr_ctx.profile_dir),
    }
    user_cmd = _extract_user_statusline_command(source_settings, own_shim_path=statusline_command)
    if user_cmd is not None:
        env["MNGR_USER_STATUSLINE_CMD"] = user_cmd

    return ClaudeExtraSettingsContribution(
        statusline_command=statusline_command,
        env=env,
        resource_scripts=(_STATUSLINE_SHIM_SCRIPT, _RATE_LIMITS_WRITER_SCRIPT),
        resource_module=_usage_resources,
    )
