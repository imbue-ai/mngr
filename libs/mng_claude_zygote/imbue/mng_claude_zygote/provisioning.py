from __future__ import annotations

import importlib.resources
import shlex
import time
from pathlib import Path
from typing import Final

import pluggy
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.data_types import CommandResult
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude_zygote import resources as zygote_resources
from imbue.mng_claude_zygote.data_types import ProvisioningSettings

# Scripts to provision to $MNG_HOST_DIR/commands/
_SCRIPT_FILES: Final[tuple[str, ...]] = (
    "chat.sh",
    "conversation_watcher.py",
    "event_watcher.py",
    "transcript_watcher.py",
)

# Python modules provisioned alongside scripts (not executable, mode 0644)
_SCRIPT_MODULES: Final[tuple[str, ...]] = ("watcher_common.py",)

# Python tool files to provision to $MNG_HOST_DIR/commands/llm_tools/
_LLM_TOOL_FILES: Final[tuple[str, ...]] = (
    "context_tool.py",
    "extra_context_tool.py",
)

# Default content files written to the work directory root if missing.
# Tuples of (resource path under defaults/, target path relative to work dir).
_DEFAULT_WORK_DIR_FILES: Final[tuple[tuple[str, str], ...]] = (
    ("GLOBAL.md", "GLOBAL.md"),
    ("settings.json", "settings.json"),
)

# Default content files for the talking agent (user-facing conversation voice).
# Tuples of (resource path under defaults/, target path relative to work dir).
_DEFAULT_TALKING_DIR_FILES: Final[tuple[tuple[str, str], ...]] = (("talking/PROMPT.md", "talking/PROMPT.md"),)

# Default content files for the thinking agent (inner monologue).
# Tuples of (resource path under defaults/, target path relative to work dir).
_DEFAULT_THINKING_DIR_FILES: Final[tuple[tuple[str, str], ...]] = (
    ("thinking/PROMPT.md", "thinking/PROMPT.md"),
    ("thinking/settings.json", "thinking/settings.json"),
)

# Default skill files written to thinking/skills/<name>/SKILL.md if missing.
# Each entry is a skill directory name under defaults/thinking/skills/.
_DEFAULT_SKILL_DIRS: Final[tuple[str, ...]] = (
    "send-message-to-user",
    "list-conversations",
    "delegate-task",
    "list-event-types",
    "get-event-type-info",
)


def _execute_with_timing(
    host: OnlineHostInterface,
    cmd: str,
    *,
    hard_timeout: float,
    warn_threshold: float,
    label: str,
) -> CommandResult:
    """Execute a host command with two-threshold timeout monitoring.

    Uses hard_timeout as the actual timeout. If the command takes longer
    than warn_threshold, emits a warning so we can notice degradation
    before it becomes an outright failure.
    """
    start = time.monotonic()
    result = host.execute_command(cmd, timeout_seconds=hard_timeout)
    elapsed = time.monotonic() - start
    if elapsed > warn_threshold:
        logger.warning("{} took {:.1f}s (expected <{:.0f}s): {}", label, elapsed, warn_threshold, cmd)
    return result


def load_zygote_resource(filename: str) -> str:
    """Load a resource file from the mng_claude_zygote resources package."""
    resource_files = importlib.resources.files(zygote_resources)
    resource_path = resource_files.joinpath(filename)
    return resource_path.read_text()


def _write_default_if_missing(
    host: OnlineHostInterface,
    target_path: Path,
    resource_path: str,
    settings: ProvisioningSettings,
) -> None:
    """Write a default resource file to the host if the target doesn't already exist."""
    check = _execute_with_timing(
        host,
        f"test -f {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="file check",
    )
    if check.success:
        logger.debug("Default file already exists, skipping: {}", target_path)
        return

    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(target_path.parent))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir",
    )

    content = load_zygote_resource(resource_path)
    with log_span("Writing default content: {}", target_path):
        host.write_text_file(target_path, content)


class TalkingRoleConstraintError(Exception):
    """Raised when the talking role directory contains skills or settings.

    The talking role is intentionally restricted to only a PROMPT.md file.
    It cannot have skills or settings because the talking agent runs via the
    ``llm`` tool (not Claude Code), and those files would have no effect.
    """


# Restricted files/dirs that must not exist under the talking/ role directory.
_TALKING_FORBIDDEN: Final[tuple[str, ...]] = ("skills", "settings.json")


def validate_talking_role_constraints(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Raise if the talking/ role directory contains skills or settings.

    The talking agent runs via the ``llm`` tool, not Claude Code, so it cannot
    use Claude Code skills or settings. If the user has created either of these,
    we raise ``TalkingRoleConstraintError`` to surface the misconfiguration early
    rather than silently ignoring the files.
    """
    talking_dir = work_dir / "talking"
    for name in _TALKING_FORBIDDEN:
        target = talking_dir / name
        # Use -e so we catch both files and directories (including symlinks)
        check = _execute_with_timing(
            host,
            f"test -e {shlex.quote(str(target))}",
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="talking constraint check",
        )
        if check.success:
            raise TalkingRoleConstraintError(
                f"The talking/ role directory must not contain '{name}'. "
                f"Found: {target}. "
                "The talking agent runs via the llm tool and cannot use Claude Code "
                "skills or settings. Remove this path and try again."
            )


def provision_default_content(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Write default content files to the work directory if they don't already exist.

    Populates sensible defaults for:
    - GLOBAL.md (shared project instructions for all agents)
    - settings.json (shared Claude settings for all agents)
    - talking/PROMPT.md (talking agent prompt, used as llm system prompt)
    - thinking/PROMPT.md (primary/inner monologue agent prompt)
    - thinking/settings.json (primary agent Claude settings)
    - thinking/skills/<name>/SKILL.md (skills for the thinking agent)

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

    skills_dir = work_dir / "thinking" / "skills"
    for skill_name in _DEFAULT_SKILL_DIRS:
        target_path = skills_dir / skill_name / "SKILL.md"
        _write_default_if_missing(host, target_path, f"defaults/thinking/skills/{skill_name}/SKILL.md", settings)


def install_llm_toolchain(host: OnlineHostInterface, settings: ProvisioningSettings) -> None:
    """Install llm, llm-anthropic, and llm-live-chat on the host.

    Uses uv tool install for llm itself, then llm install for plugins.
    Skips installation if llm is already available.
    """
    with log_span("Installing llm toolchain"):
        # Check if llm is already installed
        check_result = _execute_with_timing(
            host,
            "command -v llm",
            hard_timeout=settings.command_check_hard_timeout_seconds,
            warn_threshold=settings.command_check_warn_threshold_seconds,
            label="llm check",
        )
        if check_result.success:
            # llm is installed, just ensure plugins are present
            _install_llm_plugins(host, settings)
            return

        # Install llm via uv tool
        result = _execute_with_timing(
            host,
            "uv tool install llm",
            hard_timeout=settings.install_hard_timeout_seconds,
            warn_threshold=settings.install_warn_threshold_seconds,
            label="llm install",
        )
        if not result.success:
            raise RuntimeError(f"Failed to install llm: {result.stderr}")

        _install_llm_plugins(host, settings)


def _install_llm_plugins(host: OnlineHostInterface, settings: ProvisioningSettings) -> None:
    """Install llm-anthropic and llm-live-chat plugins."""
    for plugin_name in ("llm-anthropic", "llm-live-chat"):
        with log_span("Installing llm plugin: {}", plugin_name):
            result = _execute_with_timing(
                host,
                f"llm install {plugin_name}",
                hard_timeout=settings.install_hard_timeout_seconds,
                warn_threshold=settings.install_warn_threshold_seconds,
                label=f"llm plugin install ({plugin_name})",
            )
            if not result.success:
                raise RuntimeError(f"Failed to install {plugin_name}: {result.stderr}")


def _is_recursive_plugin_registered(pm: pluggy.PluginManager) -> bool:
    """Check if the mng_recursive plugin is registered (and thus will install mng on remote hosts)."""
    return any(name == "recursive_mng" for name, _ in pm.list_name_plugin())


def warn_if_mng_unavailable(
    host: OnlineHostInterface,
    pm: pluggy.PluginManager,
    settings: ProvisioningSettings,
) -> None:
    """Warn if mng will not be available on the agent host.

    Changeling scripts (event_watcher.py, etc.) use 'uv run mng message' to
    communicate with the primary agent. If mng is not available on the host,
    these scripts will fail silently.

    Skips the warning if:
    - The host is local (mng is obviously available since it's running locally)
    - The mng_recursive plugin is registered (it will install mng on remote hosts)
    """
    if host.is_local:
        return

    if _is_recursive_plugin_registered(pm):
        logger.debug("Skipping mng availability check: recursive plugin will install mng")
        return

    check_result = _execute_with_timing(
        host,
        "command -v mng",
        hard_timeout=settings.command_check_hard_timeout_seconds,
        warn_threshold=settings.command_check_warn_threshold_seconds,
        label="mng check",
    )
    if not check_result.success:
        logger.warning(
            "mng is not available on the remote host and the mng_recursive plugin is not enabled. "
            "Changeling scripts (event_watcher.py, etc.) use 'uv run mng message' to communicate "
            "with the primary agent and will fail without mng installed. "
            "Enable the mng_recursive plugin or install mng on the remote host manually."
        )


def create_changeling_symlinks(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create symlinks so Claude Code discovers changeling files at standard locations.

    Creates:
    - <work_dir>/CLAUDE.md -> <work_dir>/GLOBAL.md
    - <work_dir>/CLAUDE.local.md -> <work_dir>/thinking/PROMPT.md
    - <work_dir>/.claude/settings.json -> <work_dir>/settings.json
    - <work_dir>/.claude/settings.local.json -> <work_dir>/thinking/settings.json
    - <work_dir>/.claude/skills -> <work_dir>/thinking/skills  (directory symlink)
    """
    # CLAUDE.md -> GLOBAL.md (so Claude Code loads global instructions)
    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / "CLAUDE.md",
        target_path=work_dir / "GLOBAL.md",
        settings=settings,
    )

    # CLAUDE.local.md -> thinking/PROMPT.md (inner monologue prompt)
    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / "CLAUDE.local.md",
        target_path=work_dir / "thinking" / "PROMPT.md",
        settings=settings,
    )

    # .claude/settings.json -> settings.json (global Claude settings)
    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / ".claude" / "settings.json",
        target_path=work_dir / "settings.json",
        settings=settings,
    )

    # .claude/settings.local.json -> thinking/settings.json (thinking agent settings)
    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / ".claude" / "settings.local.json",
        target_path=work_dir / "thinking" / "settings.json",
        settings=settings,
    )

    # .claude/skills -> thinking/skills (directory symlink)
    _create_dir_symlink_if_target_exists(
        host,
        link_path=work_dir / ".claude" / "skills",
        target_path=work_dir / "thinking" / "skills",
        settings=settings,
    )


def _create_symlink_if_target_exists(
    host: OnlineHostInterface,
    link_path: Path,
    target_path: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create a symlink at link_path pointing to target_path, if target exists."""
    check = _execute_with_timing(
        host,
        f"test -f {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="file check",
    )
    if not check.success:
        return

    # Ensure parent directory exists
    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(link_path.parent))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir",
    )

    # Create symlink (force to overwrite existing)
    cmd = f"ln -sf {shlex.quote(str(target_path))} {shlex.quote(str(link_path))}"
    with log_span("Creating symlink: {} -> {}", link_path, target_path):
        result = _execute_with_timing(
            host,
            cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="symlink",
        )
        if not result.success:
            raise RuntimeError(f"Failed to create symlink {link_path} -> {target_path}: {result.stderr}")


def _create_dir_symlink_if_target_exists(
    host: OnlineHostInterface,
    link_path: Path,
    target_path: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create a directory symlink at link_path pointing to target_path, if target exists.

    Uses ``ln -sfn`` so that an existing symlink to a directory is replaced
    rather than creating a symlink inside the target directory.
    """
    check = _execute_with_timing(
        host,
        f"test -d {shlex.quote(str(target_path))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="dir check",
    )
    if not check.success:
        return

    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(link_path.parent))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir",
    )

    cmd = f"ln -sfn {shlex.quote(str(target_path))} {shlex.quote(str(link_path))}"
    with log_span("Creating directory symlink: {} -> {}", link_path, target_path):
        result = _execute_with_timing(
            host,
            cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="dir symlink",
        )
        if not result.success:
            raise RuntimeError(f"Failed to create directory symlink {link_path} -> {target_path}: {result.stderr}")


def provision_changeling_scripts(host: OnlineHostInterface, settings: ProvisioningSettings) -> None:
    """Write changeling scripts to $MNG_HOST_DIR/commands/.

    Scripts are loaded from the resources package and written with execute permission.
    """
    commands_dir = host.host_dir / "commands"
    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(commands_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir commands",
    )

    for script_name in _SCRIPT_FILES:
        script_content = load_zygote_resource(script_name)
        script_path = commands_dir / script_name
        with log_span("Writing {} to host", script_name):
            host.write_file(script_path, script_content.encode(), mode="0755")

    for module_name in _SCRIPT_MODULES:
        module_content = load_zygote_resource(module_name)
        module_path = commands_dir / module_name
        with log_span("Writing {} to host", module_name):
            host.write_file(module_path, module_content.encode(), mode="0644")


def provision_llm_tools(host: OnlineHostInterface, settings: ProvisioningSettings) -> None:
    """Write LLM tool Python files to $MNG_HOST_DIR/commands/llm_tools/.

    These files are passed to `llm live-chat` via `--functions` to give
    conversation agents access to changeling context.
    """
    tools_dir = host.host_dir / "commands" / "llm_tools"
    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(tools_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir llm_tools",
    )

    for tool_file in _LLM_TOOL_FILES:
        tool_content = load_zygote_resource(tool_file)
        tool_path = tools_dir / tool_file
        with log_span("Writing {} to host", tool_file):
            host.write_file(tool_path, tool_content.encode(), mode="0644")


def create_event_log_directories(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create the event log directory structure.

    Creates directories for each event source:
    - logs/conversations/     conversation lifecycle events
    - logs/messages/          conversation messages
    - logs/scheduled/         scheduled trigger events
    - logs/mng_agents/        agent state transitions
    - logs/stop/              agent stop events
    - logs/monitor/           (future) monitor agent events
    - logs/claude_transcript/ inner monologue (written by Claude background tasks)
    - logs/common_transcript/ agent-agnostic transcript (written by transcript watcher)
    """
    for source in (
        "conversations",
        "messages",
        "scheduled",
        "mng_agents",
        "stop",
        "monitor",
        "claude_transcript",
        "common_transcript",
    ):
        source_dir = agent_state_dir / "logs" / source
        _execute_with_timing(
            host,
            f"mkdir -p {shlex.quote(str(source_dir))}",
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label=f"mkdir logs/{source}",
        )


def compute_claude_project_dir_name(work_dir_abs: str) -> str:
    """Compute the Claude project directory name from an absolute work_dir path.

    Claude names project directories by replacing '/' and '.' with '-' in the
    absolute path, e.g. /home/user/.changelings/my-agent -> -home-user--changelings-my-agent
    """
    return work_dir_abs.replace("/", "-").replace(".", "-")


def link_memory_directory(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Symlink the memory directory into the Claude project memory path.

    Creates:
    - <work_dir>/memory/ (if it doesn't exist)
    - ~/.claude/projects/<project_name>/memory/ -> <work_dir>/memory/

    This ensures all Claude agents share the same project memory, and that
    memories are version-controlled in the agent's git repo.
    """
    memory_dir = work_dir / "memory"

    # Get the absolute path of work_dir on the host
    abs_result = _execute_with_timing(
        host,
        f"cd {shlex.quote(str(work_dir))} && pwd",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="resolve work_dir",
    )
    if not abs_result.success:
        raise RuntimeError(f"Failed to resolve absolute path of {work_dir}: {abs_result.stderr}")
    abs_work_dir = abs_result.stdout.strip()
    project_dir_name = compute_claude_project_dir_name(abs_work_dir)

    # Create the memory directory
    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(memory_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir memory",
    )

    # Create the Claude project directory and symlink memory into it.
    # Use $HOME instead of ~ because ~ is not expanded inside single quotes
    # (which shlex.quote produces), but $HOME expands in double quotes.
    quoted_project_dir_name = shlex.quote(project_dir_name)
    project_dir_shell = f'"$HOME/.claude/projects/"{quoted_project_dir_name}'
    memory_link_shell = f'"$HOME/.claude/projects/"{quoted_project_dir_name}/memory'

    cmd = f"mkdir -p {project_dir_shell} && ln -sfn {shlex.quote(str(memory_dir))} {memory_link_shell}"
    with log_span("Linking memory: $HOME/.claude/projects/{}/memory -> {}", project_dir_name, memory_dir):
        result = _execute_with_timing(
            host,
            cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="link memory",
        )
        if not result.success:
            raise RuntimeError(f"Failed to link memory directory: {result.stderr}")
