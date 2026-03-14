from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_llm.data_types import ProvisioningSettings
from imbue.mng_llm.provisioning import execute_with_timing

# Claude Code settings.json content, inlined because it is Claude-specific
# and does not belong in the generic mng_mind plugin.
_CLAUDE_SETTINGS_JSON: Final[str] = (
    json.dumps(
        {
            "permissions": {
                "allow": [
                    "Bash(command:mng *)",
                    "Bash(command:$MNG_HOST_DIR/commands/*)",
                ]
            }
        },
        indent=2,
    )
    + "\n"
)


def provision_claude_settings(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    settings: ProvisioningSettings,
) -> None:
    """Write the Claude Code settings.json for the active role if it doesn't exist.

    This creates <work_dir>/<active_role>/.claude/settings.json with default
    permissions for mng commands.
    """
    target_path = work_dir / active_role / ".claude" / "settings.json"
    check = execute_with_timing(
        host,
        f"test -f {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="file check",
    )
    if check.success:
        logger.debug("Claude settings already exists, skipping: {}", target_path)
        return

    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(target_path.parent))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir",
    )

    with log_span("Writing Claude settings: {}", target_path):
        host.write_text_file(target_path, _CLAUDE_SETTINGS_JSON)


def create_mind_symlinks(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    settings: ProvisioningSettings,
) -> None:
    """Create symlinks so Claude Code discovers mind files at standard locations.

    Claude Code runs from within the role directory (via ``cd $ROLE`` in
    assemble_command), so ``.claude/`` is found naturally. We create:

    - ``<work_dir>/CLAUDE.md`` -> ``<work_dir>/GLOBAL.md``
    - ``<work_dir>/<active_role>/CLAUDE.local.md`` -> ``<work_dir>/<active_role>/PROMPT.md``
    - ``<work_dir>/<active_role>/.claude/skills`` -> ``<work_dir>/<active_role>/skills``
    """
    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / "CLAUDE.md",
        target_path=work_dir / "GLOBAL.md",
        settings=settings,
    )

    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / active_role / "CLAUDE.local.md",
        target_path=work_dir / active_role / "PROMPT.md",
        settings=settings,
    )

    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / active_role / ".claude" / "skills",
        target_path=work_dir / active_role / "skills",
        settings=settings,
    )


def _create_symlink_if_target_exists(
    host: OnlineHostInterface,
    link_path: Path,
    target_path: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create a symlink at link_path pointing to target_path, if target exists.

    For directory targets, uses ``test -d`` instead of ``test -f``.
    """
    test_flag = "-d" if target_path.suffix == "" else "-f"
    check = execute_with_timing(
        host,
        f"test {test_flag} {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="target check",
    )
    if not check.success:
        return

    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(link_path.parent))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir",
    )

    # Use -n so ln treats an existing directory destination as a file
    # (otherwise ln -sf creates a symlink inside the directory rather than replacing it)
    cmd = f"ln -sfn {shlex.quote(str(target_path))} {shlex.quote(str(link_path))}"
    with log_span("Creating symlink: {} -> {}", link_path, target_path):
        result = execute_with_timing(
            host,
            cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="symlink",
        )
        if not result.success:
            raise RuntimeError(f"Failed to create symlink {link_path} -> {target_path}: {result.stderr}")


def setup_memory_directory(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    settings: ProvisioningSettings,
) -> None:
    """Create the per-role memory directory if it doesn't exist.

    Creates <work_dir>/<active_role>/memory/. Claude Code reads from and
    writes to this directory directly via the autoMemoryDirectory setting
    (configured in settings.local.json during provisioning).
    """
    memory_dir = work_dir / active_role / "memory"
    with log_span("Creating memory directory: {}", memory_dir):
        result = execute_with_timing(
            host,
            f"mkdir -p {shlex.quote(str(memory_dir))}",
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="mkdir memory dir",
        )
        if not result.success:
            raise RuntimeError(f"Failed to sync memory directory: {result.stderr}")


def run_link_skills_script(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    settings: ProvisioningSettings,
) -> None:
    """Make link_skills.sh executable and run it for the active role.

    The script symlinks each top-level skill into the role's skills
    directory. If a skill already exists in the role folder, the script
    emits a warning and skips it.
    """
    script_path = work_dir / "link_skills.sh"
    check = execute_with_timing(
        host,
        f"test -f {shlex.quote(str(script_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="link_skills check",
    )
    if not check.success:
        logger.debug("link_skills.sh not found at {}, skipping", script_path)
        return

    with log_span("Running link_skills.sh for role '{}'", active_role):
        chmod_result = execute_with_timing(
            host,
            f"chmod +x {shlex.quote(str(script_path))}",
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="chmod link_skills",
        )
        if not chmod_result.success:
            raise RuntimeError(f"Failed to chmod link_skills.sh: {chmod_result.stderr}")

        result = execute_with_timing(
            host,
            f"{shlex.quote(str(script_path))} {shlex.quote(active_role)}",
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="run link_skills",
        )
        if not result.success:
            raise RuntimeError(f"link_skills.sh failed: {result.stderr}")
        elif result.stdout:
            logger.info("link_skills.sh output: {}", result.stdout.strip())


_STOP_HOOK_SCRIPT: Final[str] = """\
#!/usr/bin/env bash
# Prevent Claude from stopping if there are unhandled events.
#
# Reads all event IDs from /tmp/*.events files, compares them against
# /tmp/handled_event_ids, and exits with code 2 if any are unhandled
# (which tells Claude Code to block the stop).

set -euo pipefail

if ! ls /tmp/*.events >/dev/null 2>&1; then
    exit 0
fi

all_ids=$(cat /tmp/*.events | jq -r '.event_id // empty' | sort -u)

if [ -f /tmp/handled_event_ids ]; then
    handled_ids=$(sort -u /tmp/handled_event_ids)
else
    handled_ids=""
fi

unhandled=$(comm -23 <(echo "$all_ids") <(echo "$handled_ids"))

if [ -n "$unhandled" ]; then
    exit 2
fi
"""

_STOP_HOOK_SCRIPT_NAME: Final[str] = "on_stop_prevent_unhandled_events.sh"


def provision_stop_hook_script(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    settings: ProvisioningSettings,
) -> Path:
    """Write the stop hook script to <work_dir>/<role>/.claude/hooks/ and make it executable.

    Returns the path to the script file.
    """
    hooks_dir = work_dir / active_role / ".claude" / "hooks"
    script_path = hooks_dir / _STOP_HOOK_SCRIPT_NAME

    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(hooks_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir hooks dir",
    )

    with log_span("Writing stop hook script: {}", script_path):
        host.write_text_file(script_path, _STOP_HOOK_SCRIPT)

    chmod_result = execute_with_timing(
        host,
        f"chmod +x {shlex.quote(str(script_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="chmod stop hook",
    )
    if not chmod_result.success:
        raise RuntimeError(f"Failed to chmod stop hook script: {chmod_result.stderr}")

    return script_path


def build_stop_hook_config(script_path: Path) -> dict[str, Any]:
    """Build Claude hooks config for checking unhandled events on stop.

    Returns a hooks config dict with a Stop entry that runs the given
    script. The script prevents Claude from stopping if there are event
    IDs in /tmp/*.events files that have not been written to
    /tmp/handled_event_ids.

    Exit code 2 from a Stop hook tells Claude Code to block the stop.
    """
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(script_path),
                        }
                    ],
                }
            ],
        }
    }
