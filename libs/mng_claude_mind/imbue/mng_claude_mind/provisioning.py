"""Claude-specific provisioning functions for the claude-mind agent type.

Provides Claude Code-specific provisioning: settings.json injection,
.claude/skills symlink, memory directory setup, and hook configuration.

Generic mind provisioning (default content, prompts, skills) is provided
by the mng_mind plugin.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude.claude_config import encode_claude_project_dir_name
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
    role_dir_abs: str,
    settings: ProvisioningSettings,
) -> None:
    """Set up the per-role memory directory and initial sync into Claude project memory.

    Creates:
    - <work_dir>/<active_role>/memory/ (if it doesn't exist)
    - ~/.claude/projects/<project_name>/memory/ (real directory, not symlink)
    - Initial rsync of contents from role memory/ to claude project memory/
    """
    memory_dir = work_dir / active_role / "memory"
    # Use .parent because Claude Code's project dir is named after the git repo
    # root (the mind dir), not the role subdirectory within it. This must
    # match the path used by build_memory_sync_hooks_config.
    project_dir_name = encode_claude_project_dir_name(Path(role_dir_abs).parent)

    quoted_project_dir_name = shlex.quote(project_dir_name)
    project_memory_shell = f'"$HOME/.claude/projects/"{quoted_project_dir_name}/memory'
    cmd = f"mkdir -p {shlex.quote(str(memory_dir))} && rm -f {project_memory_shell} && mkdir -p {project_memory_shell}"
    execute_with_timing(
        host,
        cmd,
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir memory dirs",
    )

    sync_cmd = f"rsync -a --delete {shlex.quote(str(memory_dir))}/ {project_memory_shell}/"
    with log_span("Initial memory sync: {} -> $HOME/.claude/projects/{}/memory", memory_dir, project_dir_name):
        result = execute_with_timing(
            host,
            sync_cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="initial memory sync",
        )
        if not result.success:
            raise RuntimeError(f"Failed to sync memory directory: {result.stderr}")


def build_memory_sync_hooks_config(role_dir_abs: str) -> dict[str, Any]:
    """Build Claude hooks config for syncing per-role memory with Claude project memory.

    Returns a hooks config dict with PreToolUse and PostToolUse entries that
    rsync the memory directory in the appropriate direction.
    """
    project_dir_name = encode_claude_project_dir_name(Path(role_dir_abs).parent)
    quoted_work_memory = shlex.quote(f"{role_dir_abs}/memory")
    quoted_project_dir_name = shlex.quote(project_dir_name)
    project_memory_shell = f'"$HOME/.claude/projects/"{quoted_project_dir_name}/memory'

    pre_cmd = f"rsync -a --delete {quoted_work_memory}/ {project_memory_shell}/"
    post_cmd = f"rsync -a --delete {project_memory_shell}/ {quoted_work_memory}/"

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read",
                    "hooks": [
                        {
                            "type": "command",
                            "command": pre_cmd,
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": post_cmd,
                        }
                    ],
                }
            ],
        }
    }
