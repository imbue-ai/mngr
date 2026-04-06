"""Provisioning functions for llm-based agents.

Provides installation, configuration, and conversation management for agents
that use the llm CLI tool. Used directly by LlmAgent and also imported by
mngr_claude_mind for its llm-related provisioning steps.
"""

from __future__ import annotations

import importlib.resources
import json
import shlex
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_llm import resources as llm_resources
from imbue.mngr_llm.data_types import ProvisioningSettings

# Supporting service shell scripts to provision to $MNGR_AGENT_STATE_DIR/commands/.
_SERVICE_SCRIPT_FILES: Final[tuple[str, ...]] = ("chat.sh",)

# Scripts provisioned to $MNGR_AGENT_STATE_DIR/commands/ttyd/ for URL-arg dispatch.
# Tuples of (resource filename, target filename under commands/ttyd/).
_TTYD_DISPATCH_SCRIPTS: Final[tuple[tuple[str, str], ...]] = (("ttyd_chat.sh", "chat.sh"),)

# Python tool files to provision to $MNGR_AGENT_STATE_DIR/commands/llm_tools/
_LLM_TOOL_FILES: Final[tuple[str, ...]] = (
    "context_tool.py",
    "extra_context_tool.py",
)

# SQL schema for the mind_conversations table that stores conversation
# metadata (tags, created_at) alongside the llm tool's own tables.
MIND_CONVERSATIONS_TABLE_SQL: Final[str] = (
    "CREATE TABLE IF NOT EXISTS mind_conversations ("
    "conversation_id TEXT PRIMARY KEY, "
    "tags TEXT NOT NULL DEFAULT '{}', "
    "created_at TEXT NOT NULL"
    ")"
)


def execute_with_timing(
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
    result = host.execute_idempotent_command(cmd, timeout_seconds=hard_timeout)
    elapsed = time.monotonic() - start
    if elapsed > warn_threshold:
        logger.warning("{} took {:.1f}s (expected <{:.0f}s): {}", label, elapsed, warn_threshold, cmd)
    return result


def resolve_work_dir_abs(
    host: OnlineHostInterface,
    work_dir: Path,
    settings: ProvisioningSettings,
) -> str:
    """Resolve the absolute path of work_dir on the host."""
    abs_result = execute_with_timing(
        host,
        f"cd {shlex.quote(str(work_dir))} && pwd",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="resolve work_dir",
    )
    if not abs_result.success:
        raise RuntimeError(f"Failed to resolve absolute path of {work_dir}: {abs_result.stderr}")
    return abs_result.stdout.strip()


def load_llm_resource(filename: str) -> str:
    """Load a resource file from the mngr_llm resources package."""
    resource_files = importlib.resources.files(llm_resources)
    resource_path = resource_files.joinpath(filename)
    return resource_path.read_text()


def _sql_quote(value: str) -> str:
    """Quote a string value for use in a SQL statement.

    Escapes single quotes by doubling them, per SQL standard.

    We use manual quoting here instead of parameterized queries because the
    SQL is passed to the sqlite3 CLI via host.execute_command() over SSH,
    where parameterized queries are not available.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def check_llm_toolchain(host: OnlineHostInterface, settings: ProvisioningSettings) -> None:
    check_result = execute_with_timing(
        host,
        "command -v llm",
        hard_timeout=settings.command_check_hard_timeout_seconds,
        warn_threshold=settings.command_check_warn_threshold_seconds,
        label="llm check",
    )
    if not check_result.success:
        raise Exception("llm command not found on host. Please run install_llm_toolchain() to install it.")


def install_llm_toolchain(host: OnlineHostInterface, settings: ProvisioningSettings) -> None:
    """Install llm, llm-anthropic, and llm-live-chat on the host.

    Uses uv tool install for llm itself, then llm install for plugins.
    Skips installation if llm is already available.
    """
    with log_span("Installing llm toolchain"):
        check_result = execute_with_timing(
            host,
            "command -v llm",
            hard_timeout=settings.command_check_hard_timeout_seconds,
            warn_threshold=settings.command_check_warn_threshold_seconds,
            label="llm check",
        )
        if check_result.success:
            _install_llm_plugins(host, settings)
            return

        result = execute_with_timing(
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
    """Install the required llm plugins for the models and features we use."""
    for plugin_name in ("llm-anthropic", "llm-live-chat", "llm-matched-responses"):
        with log_span("Installing llm plugin: {}", plugin_name):
            result = execute_with_timing(
                host,
                f"llm install {plugin_name}",
                hard_timeout=settings.install_hard_timeout_seconds,
                warn_threshold=settings.install_warn_threshold_seconds,
                label=f"llm plugin install ({plugin_name})",
            )
            if not result.success:
                raise RuntimeError(f"Failed to install {plugin_name}: {result.stderr}")


def provision_supporting_services(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Write supporting service shell scripts to $MNGR_AGENT_STATE_DIR/commands/.

    Provisions:
    - Service scripts to commands/ (e.g. chat.sh)
    - Ttyd dispatch scripts to commands/ttyd/ (e.g. chat.sh)

    Note: mngr_log.sh (shared logging library) is provisioned by
    Host.provision_agent() to both host-level and agent-level commands
    directories, so we do not write it here.
    """
    commands_dir = agent_state_dir / "commands"
    ttyd_dir = commands_dir / "ttyd"
    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(ttyd_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir commands/ttyd",
    )

    for script_name in _SERVICE_SCRIPT_FILES:
        script_content = load_llm_resource(script_name)
        script_path = commands_dir / script_name
        with log_span("Writing {} to host", script_name):
            host.write_file(script_path, script_content.encode(), mode="0755")

    for resource_name, target_name in _TTYD_DISPATCH_SCRIPTS:
        script_content = load_llm_resource(resource_name)
        script_path = ttyd_dir / target_name
        with log_span("Writing ttyd/{} to host", target_name):
            host.write_file(script_path, script_content.encode(), mode="0755")


def provision_llm_tools(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Write LLM tool Python files to $MNGR_AGENT_STATE_DIR/commands/llm_tools/.

    These files are passed to `llm live-chat` via `--functions` to give
    conversation agents access to mind context.
    """
    tools_dir = agent_state_dir / "commands" / "llm_tools"
    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(tools_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir llm_tools",
    )

    for tool_file in _LLM_TOOL_FILES:
        tool_content = load_llm_resource(tool_file)
        tool_path = tools_dir / tool_file
        with log_span("Writing {} to host", tool_file):
            host.write_file(tool_path, tool_content.encode(), mode="0644")


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
    execute_with_timing(
        host,
        f"mkdir -p {shlex.quote(str(llm_data_dir))}",
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="mkdir llm_data",
    )
    logger.info("Created LLM data directory: {}", llm_data_dir)


def _get_llm_db_path(agent_state_dir: Path) -> Path:
    """Return the path to the llm database for the given agent."""
    return agent_state_dir / "llm_data" / "logs.db"


def create_mind_conversations_table(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create the mind_conversations table in the llm database.

    Uses CREATE TABLE IF NOT EXISTS so it is safe to call multiple times.
    The table stores conversation metadata (tags, created_at) that the llm
    tool's built-in tables do not track.
    """
    db_path = _get_llm_db_path(agent_state_dir)
    cmd = f"sqlite3 {shlex.quote(str(db_path))} {shlex.quote(MIND_CONVERSATIONS_TABLE_SQL)}"
    with log_span("Creating mind_conversations table in {}", db_path):
        result = execute_with_timing(
            host,
            cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="create mind_conversations table",
        )
        if not result.success:
            raise RuntimeError(
                f"Failed to create mind_conversations table: {result.stderr}. "
                "Conversation features (chat, system notifications) will not work without this table."
            )


def _insert_conversation_record(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
    *,
    conversation_id: str,
    tags: dict[str, str] | None = None,
) -> None:
    """Insert a conversation record into the mind_conversations table in the llm database."""
    now = datetime.now(timezone.utc)
    created_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z"
    tags_json = json.dumps(tags or {}, separators=(",", ":"))

    db_path = _get_llm_db_path(agent_state_dir)
    sql = (
        f"INSERT OR REPLACE INTO mind_conversations "
        f"(conversation_id, tags, created_at) "
        f"VALUES ("
        f"{_sql_quote(conversation_id)}, "
        f"{_sql_quote(tags_json)}, "
        f"{_sql_quote(created_at)}"
        f")"
    )
    cmd = f"sqlite3 {shlex.quote(str(db_path))} {shlex.quote(sql)}"
    with log_span("Recording conversation in DB for {}", conversation_id):
        result = execute_with_timing(
            host,
            cmd,
            hard_timeout=settings.fs_hard_timeout_seconds,
            warn_threshold=settings.fs_warn_threshold_seconds,
            label="insert conversation record",
        )
        if not result.success:
            logger.warning("Failed to insert conversation record for {}: {}", conversation_id, result.stderr)


def _update_conversation_name(
    host: OnlineHostInterface,
    settings: ProvisioningSettings,
    *,
    db_path: Path,
    conversation_id: str,
    name: str,
) -> None:
    """Update the name column in the llm conversations table.

    The llm library sets conversation names based on the prompt text, which
    produces poor names for conversations created via ``llm inject`` (e.g.
    empty string). This updates the name to something meaningful
    so that llm-webchat displays it properly.
    """
    sql = f"UPDATE conversations SET name = {_sql_quote(name)} WHERE id = {_sql_quote(conversation_id)}"
    cmd = f"sqlite3 {shlex.quote(str(db_path))} {shlex.quote(sql)}"
    result = execute_with_timing(
        host,
        cmd,
        hard_timeout=settings.fs_hard_timeout_seconds,
        warn_threshold=settings.fs_warn_threshold_seconds,
        label="update conversation name",
    )
    if not result.success:
        logger.warning("Failed to update conversation name for {}: {}", conversation_id, result.stderr)


def _inject_conversation(
    host: OnlineHostInterface,
    settings: ProvisioningSettings,
    *,
    model: str,
    prompt: str,
    response: str,
    label: str,
    display_name: str,
    llm_user_path: Path | None = None,
    env_vars: dict[str, str] | None = None,
) -> str | None:
    """Run ``llm inject`` to create a new conversation. Returns the conversation ID on success.

    After creating the conversation, updates the name in the llm
    ``conversations`` table to use ``display_name`` instead of the
    auto-generated name from the prompt text.
    """
    env_prefix = f"LLM_USER_PATH={shlex.quote(str(llm_user_path))} " if llm_user_path else ""
    if env_vars:
        env_prefix += " ".join(f"{key}={shlex.quote(value)}" for key, value in env_vars.items()) + " "
    inject_cmd = (
        f"{env_prefix}llm inject -m {shlex.quote(model)} --prompt {shlex.quote(prompt)} {shlex.quote(response)}"
    )
    result = execute_with_timing(
        host,
        inject_cmd,
        hard_timeout=settings.install_hard_timeout_seconds,
        warn_threshold=settings.install_warn_threshold_seconds,
        label=label,
    )
    if not result.success:
        logger.warning("Failed to create {} conversation via llm inject: {}", label, result.stderr)
        return None

    stdout = result.stdout.strip()
    parts = stdout.rsplit(" ", 1)
    if len(parts) == 2:
        conversation_id = parts[1]
        # Update the conversation name in the llm database
        db_path = llm_user_path / "logs.db" if llm_user_path else Path("logs.db")
        _update_conversation_name(
            host,
            settings,
            db_path=db_path,
            conversation_id=conversation_id,
            name=display_name,
        )
        return conversation_id

    logger.warning("Could not parse conversation ID from llm inject output: {}", stdout)
    return None


def ensure_named_conversation(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
    *,
    internal_tag: str,
    display_name: str,
    prompt: str,
) -> None:
    """Create an internal conversation with the matched-responses model."""
    llm_data_dir = agent_state_dir / "llm_data"
    conversation_id = _inject_conversation(
        host,
        settings,
        model="matched-responses",
        prompt=prompt,
        response="Confirmed.",
        label=internal_tag,
        display_name=display_name,
        llm_user_path=llm_data_dir,
        env_vars=dict(LLM_MATCHED_RESPONSE=""),
    )
    if conversation_id is None:
        return

    _insert_conversation_record(
        host,
        agent_state_dir,
        settings,
        conversation_id=conversation_id,
        tags={"internal": internal_tag, "name": display_name},
    )
    logger.info("Created {} conversation: conversation_id={}", internal_tag, conversation_id)


def create_system_notifications_conversation(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create the system_notifications conversation for delivery failure alerts."""
    ensure_named_conversation(
        host,
        agent_state_dir,
        settings,
        internal_tag="system_notifications",
        display_name="System Notifications",
        prompt="This channel is for system notifications, warnings, and errors.",
    )


def create_slack_notifications_conversation(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
) -> None:
    """Create the slack_notifications conversation for Slack integration alerts."""
    ensure_named_conversation(
        host,
        agent_state_dir,
        settings,
        internal_tag="slack_notifications",
        display_name="Slack Notifications",
        prompt="This channel is for Slack notifications and messages.",
    )


def create_first_daily_conversation(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
    chat_model: str,
    welcome_message: str,
) -> None:
    """Create a daily conversation tagged with today's date."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_display_name = f"Daily Thread ({today})"

    llm_data_dir = agent_state_dir / "llm_data"
    conversation_id = _inject_conversation(
        host,
        settings,
        model=chat_model,
        prompt="",
        response=welcome_message,
        label="daily",
        display_name=daily_display_name,
        llm_user_path=llm_data_dir,
    )
    if conversation_id is None:
        return

    _insert_conversation_record(
        host,
        agent_state_dir,
        settings,
        conversation_id=conversation_id,
        tags={"daily": today, "name": daily_display_name},
    )
    logger.info("Created daily conversation: conversation_id={} date={}", conversation_id, today)


def create_work_log_conversation(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    settings: ProvisioningSettings,
    chat_model: str,
) -> None:
    """Create a work log conversation for tracking current agent activity.

    The work log is always present and serves as a place for agents to
    communicate what they are currently working on. This helps with
    debugging and keeps the user informed of agent activity.
    """
    llm_data_dir = agent_state_dir / "llm_data"
    conversation_id = _inject_conversation(
        host,
        settings,
        model=chat_model,
        prompt="",
        response="Work log initialized. I will use this thread to communicate what I am currently working on.",
        label="work_log",
        display_name="Work Log",
        llm_user_path=llm_data_dir,
    )
    if conversation_id is None:
        return

    _insert_conversation_record(
        host,
        agent_state_dir,
        settings,
        conversation_id=conversation_id,
        tags={"internal": "work_log", "name": "Work Log"},
    )
    logger.info("Created work log conversation: conversation_id={}", conversation_id)
