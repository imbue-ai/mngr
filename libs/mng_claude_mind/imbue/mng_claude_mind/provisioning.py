"""Mind-specific provisioning functions for the claude-mind agent type.

Provides provisioning for mind defaults (GLOBAL.md, role prompts, skills),
symlinks, memory directory setup, and talking role constraint validation.

LLM-related provisioning (toolchain installation, conversation management,
supporting services) is provided by the mng_llm plugin.
"""

from __future__ import annotations

import importlib.resources
import shlex
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.agents.default_plugins.claude_config import encode_claude_project_dir_name
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude_mind import resources as mind_resources
from imbue.mng_llm.data_types import ProvisioningSettings
from imbue.mng_llm.provisioning import execute_with_timing

# Default content files written to the work directory root if missing.
# Tuples of (resource path under defaults/, target path relative to work dir).
_DEFAULT_WORK_DIR_FILES: Final[tuple[tuple[str, str], ...]] = (("GLOBAL.md", "GLOBAL.md"),)

# Default content files for the talking agent (user-facing conversation voice).
# Tuples of (resource path under defaults/, target path relative to work dir).
_DEFAULT_TALKING_DIR_FILES: Final[tuple[tuple[str, str], ...]] = (("talking/PROMPT.md", "talking/PROMPT.md"),)

# Default content files for the thinking agent (inner monologue).
# Tuples of (resource path under defaults/, target path relative to work dir).
_DEFAULT_THINKING_DIR_FILES: Final[tuple[tuple[str, str], ...]] = (
    ("thinking/PROMPT.md", "thinking/PROMPT.md"),
    ("thinking/.claude/settings.json", "thinking/.claude/settings.json"),
)

# Default skill files written to thinking/.claude/skills/<name>/SKILL.md if missing.
# Each entry is a skill directory name under defaults/thinking/.claude/skills/.
_DEFAULT_SKILL_DIRS: Final[tuple[str, ...]] = (
    "send-message-to-user",
    "list-conversations",
    "delegate-task",
    "list-event-types",
    "get-event-type-info",
)


def load_mind_resource(filename: str) -> str:
    """Load a resource file from the mng_claude_mind resources package."""
    resource_files = importlib.resources.files(mind_resources)
    resource_path = resource_files.joinpath(filename)
    return resource_path.read_text()


def _write_default_if_missing(
    host: OnlineHostInterface,
    target_path: Path,
    resource_path: str,
    settings: ProvisioningSettings,
) -> None:
    """Write a default resource file to the host if the target doesn't already exist."""
    check = execute_with_timing(
        host,
        f"test -f {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="file check",
    )
    if check.success:
        logger.debug("Default file already exists, skipping: {}", target_path)
        return

    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(target_path.parent))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir",
    )

    content = load_mind_resource(resource_path)
    with log_span("Writing default content: {}", target_path):
        host.write_text_file(target_path, content)


class TalkingRoleConstraintError(Exception):
    """Raised when the talking role directory contains skills or settings.

    The talking role is intentionally restricted to only a PROMPT.md file.
    It cannot have skills or settings because the talking agent runs via the
    ``llm`` tool (not Claude Code), and those files would have no effect.
    """


def provision_default_content(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Write default content files to the work directory if they don't already exist.

    Populates sensible defaults for:
    - GLOBAL.md (shared project instructions for all agents)
    - talking/PROMPT.md (talking agent prompt, used as llm system prompt)
    - thinking/PROMPT.md (primary/inner monologue agent prompt)
    - thinking/.claude/settings.json (primary agent Claude settings)
    - thinking/.claude/skills/<name>/SKILL.md (skills for the thinking agent)

    Only writes files that are missing -- existing files are never overwritten.
    This allows fresh deployments to work out of the box while preserving
    any customizations the user has already made.
    """
    for resource_name, relative_path in _DEFAULT_WORK_DIR_FILES:
        target_path = work_dir / relative_path
        _write_default_if_missing(host, target_path, f"defaults/{resource_name}", settings)

    for resource_name, relative_path in _DEFAULT_TALKING_DIR_FILES:
        target_path = work_dir / relative_path
        _write_default_if_missing(host, target_path, f"defaults/{resource_name}", settings)

    for resource_name, relative_path in _DEFAULT_THINKING_DIR_FILES:
        target_path = work_dir / relative_path
        _write_default_if_missing(host, target_path, f"defaults/{resource_name}", settings)

    skills_dir = work_dir / "thinking" / ".claude" / "skills"
    for skill_name in _DEFAULT_SKILL_DIRS:
        target_path = skills_dir / skill_name / "SKILL.md"
        _write_default_if_missing(
            host, target_path, f"defaults/thinking/.claude/skills/{skill_name}/SKILL.md", settings
        )


def create_mind_symlinks(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    settings: ProvisioningSettings,
) -> None:
    """Create symlinks so Claude Code discovers mind files at standard locations.

    Claude Code runs from within the role directory (via ``cd $ROLE`` in
    assemble_command), so ``.claude/`` is found naturally. We only need:

    - ``<work_dir>/CLAUDE.md`` -> ``<work_dir>/GLOBAL.md``
    - ``<work_dir>/<active_role>/CLAUDE.local.md`` -> ``<work_dir>/<active_role>/PROMPT.md``
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


def _create_symlink_if_target_exists(
    host: OnlineHostInterface,
    link_path: Path,
    target_path: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create a symlink at link_path pointing to target_path, if target exists."""
    check = execute_with_timing(
        host,
        f"test -f {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="file check",
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

    cmd = f"ln -sf {shlex.quote(str(target_path))} {shlex.quote(str(link_path))}"
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


def compute_claude_project_dir_name(work_dir_abs: str) -> str:
    """Compute the Claude project directory name from an absolute work_dir path.

    Claude names project directories by replacing '/' and '.' with '-' in the
    absolute path, e.g. /home/user/.minds/my-agent -> -home-user--minds-my-agent
    """
    return work_dir_abs.replace("/", "-").replace(".", "-")


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
