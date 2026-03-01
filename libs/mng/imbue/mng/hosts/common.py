from __future__ import annotations

import platform
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mng.config.data_types import MngConfig
from imbue.mng.interfaces.data_types import ACTIVITY_SOURCES_BY_IDLE_MODE
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import IdleMode

LOCAL_CONNECTOR_NAME: Final[str] = "LocalConnector"


@pure
def is_macos() -> bool:
    """Check if the current system is macOS (Darwin)."""
    return platform.system() == "Darwin"


# Activity sources that are host-level (vs agent-level)
HOST_LEVEL_ACTIVITY_SOURCES: Final[frozenset[ActivitySource]] = frozenset(
    {
        ActivitySource.BOOT,
        ActivitySource.USER,
        ActivitySource.SSH,
    }
)


def get_activity_sources_for_idle_mode(idle_mode: IdleMode) -> tuple[ActivitySource, ...]:
    """Get the activity sources that should be monitored for a given idle mode.

    Delegates to the canonical mapping in interfaces/data_types.py.
    """
    return ACTIVITY_SOURCES_BY_IDLE_MODE[idle_mode]


# =========================================================================
# Shared Listing Helpers
# =========================================================================

# Agent types that use a fixed expected process name instead of computing
# from the stored command. This handles agents like ClaudeAgent where the
# assembled command is a complex shell wrapper but the actual running
# process has a known name.
_EXPECTED_PROCESS_NAME_BY_AGENT_TYPE: Final[dict[str, str]] = {
    "claude": "claude",
}

# Common shell names for lifecycle state detection
SHELL_COMMANDS: Final[frozenset[str]] = frozenset({"bash", "sh", "zsh", "fish", "dash", "ksh", "tcsh", "csh"})


@pure
def resolve_expected_process_name(
    agent_type: str,
    command: CommandString,
    config: MngConfig,
) -> str:
    """Resolve the expected process name for lifecycle state detection.

    For agent types with complex wrapper commands (like claude), returns the
    known process name. For custom types with a parent_type, resolves through
    the parent. Otherwise extracts the basename from the command.
    """
    # Resolve parent type for custom agent types
    effective_type = agent_type
    type_config = config.agent_types.get(AgentTypeName(agent_type))
    if type_config is not None and type_config.parent_type is not None:
        effective_type = str(type_config.parent_type)

    if effective_type in _EXPECTED_PROCESS_NAME_BY_AGENT_TYPE:
        return _EXPECTED_PROCESS_NAME_BY_AGENT_TYPE[effective_type]

    return command.split()[0].split("/")[-1] if command else ""


def compute_idle_seconds(
    user_activity: datetime | None,
    agent_activity: datetime | None,
    ssh_activity: datetime | None,
) -> float | None:
    """Compute idle seconds from the most recent activity time."""
    latest_activity: datetime | None = None
    for activity_time in (user_activity, agent_activity, ssh_activity):
        if activity_time is not None:
            if latest_activity is None or activity_time > latest_activity:
                latest_activity = activity_time
    if latest_activity is None:
        return None
    return (datetime.now(timezone.utc) - latest_activity).total_seconds()


@pure
def timestamp_to_datetime(timestamp: int | None) -> datetime | None:
    """Convert a Unix timestamp to a UTC datetime, or None if the timestamp is None."""
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (ValueError, OSError) as e:
        logger.trace("Failed to convert timestamp {} to datetime: {}", timestamp, e)
        return None


@pure
def get_descendant_process_names(root_pid: str, ps_output: str) -> list[str]:
    """Get names of all descendant processes from ps output."""
    children_by_ppid: dict[str, list[str]] = {}
    comm_by_pid: dict[str, str] = {}

    for line in ps_output.strip().split("\n"):
        line_parts = line.split()
        if len(line_parts) >= 3:
            pid, ppid, comm = line_parts[0], line_parts[1], line_parts[2]
            comm_by_pid[pid] = comm
            if ppid not in children_by_ppid:
                children_by_ppid[ppid] = []
            children_by_ppid[ppid].append(pid)

    descendant_names: list[str] = []
    queue = list(children_by_ppid.get(root_pid, []))
    while queue:
        pid = queue.pop(0)
        if pid in comm_by_pid:
            descendant_names.append(comm_by_pid[pid])
        queue.extend(children_by_ppid.get(pid, []))

    return descendant_names


@pure
def determine_lifecycle_state(
    tmux_info: str | None,
    is_active: bool,
    expected_process_name: str,
    ps_output: str,
) -> AgentLifecycleState:
    """Determine agent lifecycle state from tmux info and ps output.

    This is a pure function that replicates the logic from
    BaseAgent.get_lifecycle_state() using pre-collected data instead of
    making SSH calls.
    """
    if not tmux_info:
        return AgentLifecycleState.STOPPED

    parts = tmux_info.split("|")
    if len(parts) != 3:
        return AgentLifecycleState.STOPPED

    pane_dead, current_command, pane_pid = parts

    if pane_dead == "1":
        return AgentLifecycleState.DONE

    if current_command == expected_process_name:
        return AgentLifecycleState.RUNNING if is_active else AgentLifecycleState.WAITING

    # Check descendant processes
    descendant_names = get_descendant_process_names(pane_pid, ps_output)

    if expected_process_name in descendant_names:
        return AgentLifecycleState.RUNNING if is_active else AgentLifecycleState.WAITING

    # Check for non-shell descendant processes
    non_shell_processes = [p for p in descendant_names if p not in SHELL_COMMANDS]
    if non_shell_processes:
        return AgentLifecycleState.REPLACED

    # Current command is a shell -> agent probably finished
    if current_command in SHELL_COMMANDS:
        return AgentLifecycleState.DONE

    return AgentLifecycleState.REPLACED
