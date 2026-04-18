from __future__ import annotations

import platform
import shlex
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.config.agent_class_registry import is_agent_class_registered
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString

LOCAL_CONNECTOR_NAME: Final[str] = "LocalConnector"


def get_ssh_known_hosts_file(host: OnlineHostInterface) -> Path | None:
    """Extract the known_hosts file path from a host's SSH configuration.

    Returns None if no known_hosts file is configured, or if it is set to /dev/null
    (which indicates host key checking was explicitly disabled at provisioning time).
    """
    known_hosts = host.connector.host.data.get("ssh_known_hosts_file")
    if known_hosts and known_hosts != "/dev/null":
        return Path(known_hosts)
    return None


@pure
def build_ssh_transport_command(
    key_path: Path,
    port: int,
    known_hosts_file: Path | None,
) -> str:
    """Build an SSH transport command string for use with rsync -e or GIT_SSH_COMMAND.

    Always uses StrictHostKeyChecking=yes, which refuses connections to hosts not
    present in the known_hosts file. When known_hosts_file is provided, that file is
    used via UserKnownHostsFile. When None, the system default (~/.ssh/known_hosts)
    is used without setting UserKnownHostsFile.
    """
    parts = ["ssh", "-i", shlex.quote(str(key_path)), "-p", str(port)]
    if known_hosts_file is not None:
        parts.extend(
            ["-o", f"UserKnownHostsFile={shlex.quote(str(known_hosts_file))}", "-o", "StrictHostKeyChecking=yes"]
        )
    else:
        parts.extend(["-o", "StrictHostKeyChecking=yes"])
    return " ".join(parts)


def add_safe_directory_on_remote(host: OnlineHostInterface, path: Path) -> None:
    """Add a git safe.directory entry on a remote host.

    On remote hosts (Docker/Modal), file ownership may differ from the SSH user
    (e.g., after rsync from a local machine with a different UID). This tells
    git to trust the given directory regardless of ownership.

    No-op for local hosts, where the current user already owns the directories.
    """
    if host.is_local:
        return
    host.execute_idempotent_command(
        f"git config --global --add safe.directory {shlex.quote(str(path))}",
    )


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
def _resolve_effective_agent_type(agent_type: str, config: MngrConfig) -> str:
    """Resolve through parent_type so custom types inherit their parent's identity.

    For example, a custom type "my-claude" with parent_type "claude" resolves
    to "claude". Types without a parent_type resolve to themselves.
    """
    type_config = config.agent_types.get(AgentTypeName(agent_type))
    if type_config is not None and type_config.parent_type is not None:
        return str(type_config.parent_type)
    return agent_type


@pure
def resolve_expected_process_name(
    agent_type: str,
    command: CommandString,
    config: MngrConfig,
) -> str:
    """Resolve the expected process name for lifecycle state detection.

    For agent types with complex wrapper commands (like claude), returns the
    known process name. For custom types with a parent_type, resolves through
    the parent. Otherwise extracts the basename from the command.
    """
    effective_type = _resolve_effective_agent_type(agent_type, config)

    if effective_type in _EXPECTED_PROCESS_NAME_BY_AGENT_TYPE:
        return _EXPECTED_PROCESS_NAME_BY_AGENT_TYPE[effective_type]

    return command.split()[0].split("/")[-1] if command else ""


def check_agent_type_known(
    agent_type: str,
    config: MngrConfig,
) -> bool:
    """Check whether an agent type is recognized (has a registered agent class).

    Resolves through parent_type in config so that custom types inheriting
    from a known type (e.g., my-claude -> claude) are also considered known.

    Not marked @pure because it reads from the global agent class registry.
    """
    effective_type = _resolve_effective_agent_type(agent_type, config)
    return is_agent_class_registered(effective_type)


def seconds_since(activity_time: datetime | None) -> float | None:
    """Seconds elapsed since a UTC timestamp; None if input is None.

    Not marked @pure because it reads the current wall-clock time, so it
    does not return the same output for the same input.
    """
    if activity_time is None:
        return None
    return (datetime.now(timezone.utc) - activity_time).total_seconds()


def compute_idle_seconds(
    user_activity: datetime | None,
    agent_activity: datetime | None,
    ssh_activity: datetime | None,
) -> float | None:
    """Compute idle seconds from the most recent activity time."""
    latest_activity = max(
        (t for t in (user_activity, agent_activity, ssh_activity) if t is not None),
        default=None,
    )
    return seconds_since(latest_activity)


def get_seconds_since_last_activity(host: OnlineHostInterface) -> float | None:
    """Return seconds since the most recent host-level activity across all sources.

    Aggregates every host-level ActivitySource file in the host's activity
    directory (BOOT, SSH, USER) and returns the elapsed time since the most
    recent one, or None if nothing has been recorded.

    Only host-level sources are checked (not agent-level ones like AGENT or
    START) because each check on a remote host is a separate SSH round-trip,
    and agent-level sources are never written at the host-level activity
    path so checking them would waste SSH calls without ever finding data.
    """
    activity_times = [host.get_reported_activity_time(source) for source in HOST_LEVEL_ACTIVITY_SOURCES]
    latest_activity = max((t for t in activity_times if t is not None), default=None)
    return seconds_since(latest_activity)


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
def _parse_ps_output(ps_output: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse ps output into children-by-ppid and comm-by-pid mappings."""
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

    return children_by_ppid, comm_by_pid


@pure
def _collect_descendant_names(
    root_pid: str,
    children_by_ppid: dict[str, list[str]],
    comm_by_pid: dict[str, str],
) -> list[str]:
    """Collect comm names of all descendant processes via BFS."""
    descendant_names: list[str] = []
    queue = list(children_by_ppid.get(root_pid, []))
    while queue:
        pid = queue.pop(0)
        if pid in comm_by_pid:
            descendant_names.append(comm_by_pid[pid])
        queue.extend(children_by_ppid.get(pid, []))
    return descendant_names


@pure
def get_descendant_process_names(root_pid: str, ps_output: str) -> list[str]:
    """Get names of all descendant processes from ps output."""
    children_by_ppid, comm_by_pid = _parse_ps_output(ps_output)
    return _collect_descendant_names(root_pid, children_by_ppid, comm_by_pid)


@pure
def determine_lifecycle_state(
    tmux_info: str | None,
    is_active: bool,
    expected_process_name: str,
    ps_output: str,
    is_agent_type_known: bool = True,
) -> AgentLifecycleState:
    """Determine agent lifecycle state from tmux info and ps output.

    This is a pure function that replicates the logic from
    BaseAgent.get_lifecycle_state() using pre-collected data instead of
    making SSH calls.

    When is_agent_type_known is False, the expected_process_name cannot be
    trusted (because we don't know what binary the agent type runs). In that
    case, states that would otherwise be REPLACED are reported as
    RUNNING_UNKNOWN_AGENT_TYPE instead.
    """
    if not tmux_info:
        return AgentLifecycleState.STOPPED

    parts = tmux_info.split("|")
    if len(parts) != 3:
        return AgentLifecycleState.STOPPED

    pane_dead, current_command, pane_pid = parts

    if pane_dead == "1":
        return AgentLifecycleState.DONE

    # Parse the ps output once for all subsequent checks. We use ps as the
    # authoritative source for process names because tmux's pane_current_command
    # can disagree with ps -- some programs modify their process title (e.g.,
    # Claude Code sets it to its version string like "2.1.73"), which tmux
    # picks up while ps -o comm= still reports the original executable name.
    children_by_ppid, comm_by_pid = _parse_ps_output(ps_output)

    # Check tmux's report first (fast path for well-behaved processes)
    if current_command == expected_process_name:
        return AgentLifecycleState.RUNNING if is_active else AgentLifecycleState.WAITING

    # Check descendant processes via ps (authoritative for modified titles)
    descendant_names = _collect_descendant_names(pane_pid, children_by_ppid, comm_by_pid)

    if expected_process_name in descendant_names:
        return AgentLifecycleState.RUNNING if is_active else AgentLifecycleState.WAITING

    # When the agent type is unknown, we cannot distinguish between
    # "replaced by a different program" and "running the correct program
    # under a name we don't recognize". Use a distinct state for this.
    replaced_state = (
        AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE if not is_agent_type_known else AgentLifecycleState.REPLACED
    )

    # Check for non-shell descendant processes
    non_shell_processes = [p for p in descendant_names if p not in SHELL_COMMANDS]
    if non_shell_processes:
        return replaced_state

    # Agent is not running. Determine DONE vs REPLACED by checking whether
    # the pane process is a shell (agent exited normally) or something else
    # (agent was replaced by another program). Use ps as authoritative source
    # since tmux may report a stale modified title.
    pane_comm = comm_by_pid.get(pane_pid)
    if current_command in SHELL_COMMANDS or (pane_comm is not None and pane_comm in SHELL_COMMANDS):
        return AgentLifecycleState.DONE

    return replaced_state
