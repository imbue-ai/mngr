from __future__ import annotations

import importlib.resources
import json
import shlex
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import pluggy
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.interfaces.data_types import CommandResult
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.providers.ssh_host_setup import load_resource_script
from imbue.mng_claude_changeling import resources as changeling_resources
from imbue.mng_claude_changeling.data_types import ProvisioningSettings

# Supporting service scripts to provision to $MNG_HOST_DIR/commands/
_SERVICE_SCRIPT_FILES: Final[tuple[str, ...]] = (
    "chat.sh",
    "chat_ttyd_handler.sh",
    "web_server.py",
    "conversation_watcher.py",
    "event_watcher.py",
    "transcript_watcher.py",
)

# Python modules provisioned alongside supporting service scripts (not executable, mode 0644)
_SERVICE_MODULES: Final[tuple[str, ...]] = ("watcher_common.py",)

# Python tool files to provision to $MNG_HOST_DIR/commands/llm_tools/
_LLM_TOOL_FILES: Final[tuple[str, ...]] = (
    "context_tool.py",
    "extra_context_tool.py",
)

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


def load_changeling_resource(filename: str) -> str:
    """Load a resource file from the mng_claude_changeling resources package."""
    resource_files = importlib.resources.files(changeling_resources)
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

    content = load_changeling_resource(resource_path)
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

    Supporting service scripts (event_watcher.py, etc.) use 'uv run mng message' to
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
            "Supporting service scripts (event_watcher.py, etc.) use 'uv run mng message' to communicate "
            "with the primary agent and will fail without mng installed. "
            "Enable the mng_recursive plugin or install mng on the remote host manually."
        )


def create_changeling_symlinks(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    settings: ProvisioningSettings,
) -> None:
    """Create symlinks so Claude Code discovers changeling files at standard locations.

    The active role's .claude/ directory becomes the top-level .claude/ via a
    directory symlink, so Claude Code naturally finds settings.json, skills/, etc.

    Creates:
    - <work_dir>/.claude -> <work_dir>/<active_role>/.claude (directory symlink)
    - <work_dir>/CLAUDE.md -> <work_dir>/GLOBAL.md
    - <work_dir>/CLAUDE.local.md -> <work_dir>/<active_role>/PROMPT.md
    """
    # .claude -> <active_role>/.claude (directory symlink)
    _create_dir_symlink_if_target_exists(
        host,
        link_path=work_dir / ".claude",
        target_path=work_dir / active_role / ".claude",
        settings=settings,
    )

    # CLAUDE.md -> GLOBAL.md (so Claude Code loads global instructions)
    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / "CLAUDE.md",
        target_path=work_dir / "GLOBAL.md",
        settings=settings,
    )

    # CLAUDE.local.md -> <active_role>/PROMPT.md (role-specific prompt)
    _create_symlink_if_target_exists(
        host,
        link_path=work_dir / "CLAUDE.local.md",
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

    # Remove any existing real directory at link_path. ln -sfn on a real
    # directory creates the symlink INSIDE rather than replacing it. The
    # guard "[ -d X ] && [ ! -L X ]" only matches real directories, not
    # existing symlinks (which ln -sfn handles correctly).
    quoted_link = shlex.quote(str(link_path))
    _execute_with_timing(
        host,
        f"[ -d {quoted_link} ] && [ ! -L {quoted_link} ] && rm -rf {quoted_link} || true",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="rm existing dir",
    )

    cmd = f"ln -sfn {shlex.quote(str(target_path))} {quoted_link}"
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


def provision_supporting_services(host: OnlineHostInterface, settings: ProvisioningSettings) -> None:
    """Write supporting service scripts to $MNG_HOST_DIR/commands/.

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

    # Provision the shared logging library (from mng core resources) first,
    # since the supporting service scripts source it.
    mng_log_content = load_resource_script("mng_log.sh")
    mng_log_path = commands_dir / "mng_log.sh"
    with log_span("Writing mng_log.sh to host"):
        host.write_file(mng_log_path, mng_log_content.encode(), mode="0755")

    for script_name in _SERVICE_SCRIPT_FILES:
        script_content = load_changeling_resource(script_name)
        script_path = commands_dir / script_name
        with log_span("Writing {} to host", script_name):
            host.write_file(script_path, script_content.encode(), mode="0755")

    for module_name in _SERVICE_MODULES:
        module_content = load_changeling_resource(module_name)
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
        tool_content = load_changeling_resource(tool_file)
        tool_path = tools_dir / tool_file
        with log_span("Writing {} to host", tool_file):
            host.write_file(tool_path, tool_content.encode(), mode="0644")


def create_event_log_directories(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create the event and log directory structure.

    Creates directories for event sources (events/<source>/):
    - events/conversations/      conversation lifecycle events
    - events/messages/           conversation messages
    - events/scheduled/          scheduled trigger events
    - events/mng_agents/         agent state transitions
    - events/stop/               agent stop events
    - events/monitor/            (future) monitor agent events
    - events/delivery_failures/  event delivery failure notifications
    - events/common_transcript/  agent-agnostic transcript (written by transcript watcher)
    - events/servers/            server registration records

    Creates directories for log sources (logs/<source>/):
    - logs/claude_transcript/    inner monologue (written by Claude background tasks, raw format)
    """
    for source in (
        "conversations",
        "messages",
        "scheduled",
        "mng_agents",
        "stop",
        "monitor",
        "delivery_failures",
        "common_transcript",
        "servers",
    ):
        source_dir = agent_state_dir / "events" / source
        _execute_with_timing(
            host,
            f"mkdir -p {shlex.quote(str(source_dir))}",
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label=f"mkdir events/{source}",
        )

    # Create log directories for raw/non-envelope data
    for log_source in ("claude_transcript",):
        log_dir = agent_state_dir / "logs" / log_source
        _execute_with_timing(
            host,
            f"mkdir -p {shlex.quote(str(log_dir))}",
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label=f"mkdir logs/{log_source}",
        )


def configure_llm_user_path(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create the per-agent llm data directory.

    Creates ``<agent_state_dir>/llm_data/`` so that llm commands have a
    unique database directory. The ``LLM_USER_PATH`` env var itself is
    set by the host's ``_collect_agent_env_vars`` (in host.py), which
    runs after ``agent.provision()`` and writes it to the agent env file
    that gets sourced by all shell processes.
    """
    llm_data_dir = agent_state_dir / "llm_data"
    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(llm_data_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir llm_data",
    )
    logger.info("Created LLM data directory: {}", llm_data_dir)


def _record_conversation_event(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
    *,
    conversation_id: str,
    model: str,
    tags: dict[str, str] | None = None,
) -> None:
    """Append a conversation_created event to events/conversations/events.jsonl."""
    now = datetime.now(timezone.utc)
    event: dict[str, object] = {
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z",
        "type": "conversation_created",
        "event_id": f"evt-{uuid4().hex}",
        "source": "conversations",
        "conversation_id": conversation_id,
        "model": model,
    }
    if tags:
        event["tags"] = tags

    conversations_dir = agent_state_dir / "events" / "conversations"
    _execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(conversations_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir conversations",
    )
    events_file = conversations_dir / "events.jsonl"
    event_line = json.dumps(event, separators=(",", ":"))
    with log_span("Recording conversation event for {}", conversation_id):
        host.execute_command(
            f"echo {shlex.quote(event_line)} >> {shlex.quote(str(events_file))}",
            timeout_seconds=settings.fs_hard_timeout_seconds,
        )


def _inject_conversation(
    host: OnlineHostInterface,
    settings: ProvisioningSettings,
    *,
    model: str,
    prompt: str,
    response: str,
    label: str,
    llm_user_path: Path | None = None,
) -> str | None:
    """Run ``llm inject`` to create a new conversation. Returns the conversation ID on success.

    Omits ``--cid`` so that ``llm inject`` creates a new conversation and
    prints the assigned ID to stdout (e.g. "Injected message into conversation <id>").

    When ``llm_user_path`` is provided, the command is prefixed with
    ``LLM_USER_PATH=<path>`` so the conversation is created in the
    per-agent database rather than the system default.
    """
    env_prefix = f"LLM_USER_PATH={shlex.quote(str(llm_user_path))} " if llm_user_path else ""
    inject_cmd = (
        f"{env_prefix}llm inject -m {shlex.quote(model)} --prompt {shlex.quote(prompt)} {shlex.quote(response)}"
    )
    result = _execute_with_timing(
        host,
        inject_cmd,
        hard_timeout=settings.install_hard_timeout_seconds,
        warn_threshold=settings.install_warn_threshold_seconds,
        label=label,
    )
    if not result.success:
        logger.warning("Failed to create {} conversation via llm inject: {}", label, result.stderr)
        return None

    # Parse conversation ID from output like "Injected message into conversation <id>"
    stdout = result.stdout.strip()
    parts = stdout.rsplit(" ", 1)
    if len(parts) == 2:
        return parts[1]

    logger.warning("Could not parse conversation ID from llm inject output: {}", stdout)
    return None


def create_system_notifications_conversation(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create the system_notifications conversation for delivery failure alerts.

    Uses ``llm inject`` to create a new conversation, then records a
    ``conversation_created`` event in ``events/conversations/events.jsonl``.
    Because this is the first conversation created, ``_send_chat_notification``
    can find it by reading the first entry in the conversations event log.
    """
    model = "echo"

    llm_data_dir = agent_state_dir / "llm_data"
    conversation_id = _inject_conversation(
        host,
        settings,
        model=model,
        prompt="This channel is for system notifications, warnings, and errors.",
        response="Confirmed.",
        label="system_notifications",
        llm_user_path=llm_data_dir,
    )
    if conversation_id is None:
        return

    _record_conversation_event(
        host,
        agent_state_dir,
        settings,
        conversation_id=conversation_id,
        model=model,
        tags={"internal": "system_notifications"},
    )
    logger.info("Created system_notifications conversation: conversation_id={}", conversation_id)


def create_daily_conversation(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
    chat_model: str,
) -> None:
    """Create a daily conversation tagged with today's date.

    Uses ``llm inject`` to seed the conversation with an empty user prompt
    and a greeting from the assistant, then records a ``conversation_created``
    event with ``tags={"daily": "<today>"}`` in the conversations event log.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    llm_data_dir = agent_state_dir / "llm_data"
    conversation_id = _inject_conversation(
        host,
        settings,
        model=chat_model,
        prompt="",
        response="Hi, I'm Elena! How can I help?",
        label="daily",
        llm_user_path=llm_data_dir,
    )
    if conversation_id is None:
        return

    _record_conversation_event(
        host,
        agent_state_dir,
        settings,
        conversation_id=conversation_id,
        model=chat_model,
        tags={"daily": today},
    )
    logger.info("Created daily conversation: conversation_id={} date={}", conversation_id, today)


def compute_claude_project_dir_name(work_dir_abs: str) -> str:
    """Compute the Claude project directory name from an absolute work_dir path.

    Claude names project directories by replacing '/' and '.' with '-' in the
    absolute path, e.g. /home/user/.changelings/my-agent -> -home-user--changelings-my-agent
    """
    return work_dir_abs.replace("/", "-").replace(".", "-")


def resolve_work_dir_abs(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> str:
    """Resolve the absolute path of work_dir on the host.

    Returns the absolute path as a string.
    """
    abs_result = _execute_with_timing(
        host,
        f"cd {shlex.quote(str(work_dir))} && pwd",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="resolve work_dir",
    )
    if not abs_result.success:
        raise RuntimeError(f"Failed to resolve absolute path of {work_dir}: {abs_result.stderr}")
    return abs_result.stdout.strip()


def setup_memory_directory(
    host: OnlineHostInterface,
    work_dir: Path,
    active_role: str,
    work_dir_abs: str,
    settings: ProvisioningSettings,
) -> None:
    """Set up the per-role memory directory and initial sync into Claude project memory.

    Creates:
    - <work_dir>/<active_role>/memory/ (if it doesn't exist)
    - ~/.claude/projects/<project_name>/memory/ (real directory, not symlink)
    - Initial rsync of contents from role memory/ to claude project memory/

    Memory sync hooks (added separately via build_memory_sync_hooks_config) keep
    the two directories in sync during agent operation: PreToolUse syncs from the
    version-controlled role memory into Claude's project memory, and PostToolUse
    syncs back so that any memory Claude wrote is captured in version control.
    """
    memory_dir = work_dir / active_role / "memory"
    project_dir_name = compute_claude_project_dir_name(work_dir_abs)

    # Create both memory directories.
    # Remove any existing symlink at the project memory path (from old provisioning)
    # before creating a real directory.
    quoted_project_dir_name = shlex.quote(project_dir_name)
    project_memory_shell = f'"$HOME/.claude/projects/"{quoted_project_dir_name}/memory'
    cmd = f"mkdir -p {shlex.quote(str(memory_dir))} && rm -f {project_memory_shell} && mkdir -p {project_memory_shell}"
    _execute_with_timing(
        host,
        cmd,
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir memory dirs",
    )

    # Initial sync: copy existing version-controlled memory into Claude's project memory.
    # Trailing slashes on both paths ensure rsync copies contents, not the directory itself.
    sync_cmd = f"rsync -a --delete {shlex.quote(str(memory_dir))}/ {project_memory_shell}/"
    with log_span("Initial memory sync: {} -> $HOME/.claude/projects/{}/memory", memory_dir, project_dir_name):
        result = _execute_with_timing(
            host,
            sync_cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="initial memory sync",
        )
        if not result.success:
            raise RuntimeError(f"Failed to sync memory directory: {result.stderr}")


def build_memory_sync_hooks_config(work_dir_abs: str, active_role: str) -> dict[str, Any]:
    """Build Claude hooks config for syncing per-role memory with Claude project memory.

    Returns a hooks config dict with PreToolUse and PostToolUse entries that
    rsync the memory directory in the appropriate direction:
    - PreToolUse: <role>/memory/ -> ~/.claude/projects/<project>/memory/
      (ensures Claude sees the latest version-controlled memory)
    - PostToolUse: ~/.claude/projects/<project>/memory/ -> <role>/memory/
      (captures any memory Claude wrote back into version control)
    """
    project_dir_name = compute_claude_project_dir_name(work_dir_abs)
    quoted_work_memory = shlex.quote(f"{work_dir_abs}/{active_role}/memory")
    quoted_project_dir_name = shlex.quote(project_dir_name)
    project_memory_shell = f'"$HOME/.claude/projects/"{quoted_project_dir_name}/memory'

    # Pre: sync version-controlled memory INTO Claude's project memory
    pre_cmd = f"rsync -a --delete {quoted_work_memory}/ {project_memory_shell}/"
    # Post: sync Claude's project memory BACK to version-controlled memory
    post_cmd = f"rsync -a --delete {project_memory_shell}/ {quoted_work_memory}/"

    return {
        "hooks": {
            "PreToolUse": [
                {
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
