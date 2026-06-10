import threading
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.mngr.api.events import parse_event_line
from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.notifier import Notifier
from imbue.mngr_notifications.notifier import build_execute_command

AGENT_STATES_SOURCE = "mngr/agent_states"


def watch_for_waiting_agents(
    mngr_ctx: MngrContext,
    plugin_config: NotificationsPluginConfig,
    notifier: Notifier,
    stop_event: threading.Event | None = None,
    observe_process: RunningProcess | None = None,
    ready_event: threading.Event | None = None,
) -> None:
    """Watch the mngr observe event stream for RUNNING -> WAITING transitions.

    Tails the agent_states events file written by `mngr observe` and sends
    desktop notifications when agents transition from RUNNING to WAITING.
    Runs until stop_event is set or interrupted.

    If observe_process is provided, periodically checks that it is still alive
    and exits with a warning if it has died.

    If ready_event is provided, it is set after the initial file offset has
    been captured.  Callers can wait on it to know the watcher is ready to
    detect new events.
    """
    if stop_event is None:
        stop_event = threading.Event()

    events_path = get_agent_states_events_path(get_default_events_base_dir(mngr_ctx.config))
    logger.info("Watching for agent state transitions in {}", events_path)

    last_size = _get_file_size(events_path)
    # Per-agent bit: True iff the agent transitioned from RUNNING to UNKNOWN
    # and has not yet transitioned out. Used to recognize the indirect
    # RUNNING -> UNKNOWN -> WAITING sequence as equivalent to RUNNING ->
    # WAITING. Cleared on any transition out of UNKNOWN that isn't WAITING.
    was_running_before_unknown_by_agent_id: dict[str, bool] = {}

    if ready_event is not None:
        ready_event.set()

    while not stop_event.is_set():
        if observe_process is not None and observe_process.returncode is not None:
            write_human_line("mngr observe exited unexpectedly (exit code {})", observe_process.returncode)
            stderr = observe_process.read_stderr().strip()
            if stderr:
                write_human_line("observe stderr: {}", stderr)
            write_human_line("Stopping -- restart mngr notify to try again.")
            return

        current_size = _get_file_size(events_path)
        if current_size > last_size:
            new_content = _read_from_offset(events_path, last_size)
            if new_content:
                _process_events(
                    new_content,
                    plugin_config,
                    notifier,
                    mngr_ctx.concurrency_group,
                    was_running_before_unknown_by_agent_id,
                )
            last_size = current_size

        stop_event.wait(timeout=1.0)


def _get_file_size(path: Path) -> int:
    """Get the file size, returning 0 if the file doesn't exist."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_from_offset(path: Path, offset: int) -> str:
    """Read content from a file starting at the given byte offset."""
    try:
        with path.open() as f:
            f.seek(offset)
            return f.read()
    except OSError:
        return ""


def _process_events(
    content: str,
    plugin_config: NotificationsPluginConfig,
    notifier: Notifier,
    cg: ConcurrencyGroup,
    was_running_before_unknown_by_agent_id: dict[str, bool],
) -> None:
    """Parse JSONL content and send notifications for agents going to WAITING.

    Recognized transitions:
    - ``RUNNING -> WAITING`` (direct): fire notification.
    - ``RUNNING -> UNKNOWN -> WAITING`` (indirect): fire notification on the
      ``UNKNOWN -> WAITING`` step iff this agent was previously RUNNING before
      going UNKNOWN. UNKNOWN is emitted by ``AgentObserver`` for agents on
      providers that could not be reached during the most recent discovery
      attempt; recovery from UNKNOWN should not suppress the "now waiting"
      notification.

    The per-agent flag is set when ``RUNNING -> UNKNOWN`` is observed, cleared
    on any other transition out of UNKNOWN.
    """
    for line in content.splitlines():
        record = parse_event_line(line, AGENT_STATES_SOURCE)

        if record.data.get("type") != "AGENT_STATE_CHANGE":
            continue

        old_state = record.data.get("old_state")
        new_state = record.data.get("new_state")
        agent_id = record.data.get("agent_id", "unknown")

        # Maintain the "was RUNNING before UNKNOWN" bit BEFORE deciding whether to fire.
        if old_state == "RUNNING" and new_state == "UNKNOWN":
            was_running_before_unknown_by_agent_id[agent_id] = True
        elif old_state == "UNKNOWN" and new_state != "WAITING":
            # Any non-WAITING transition out of UNKNOWN clears the bit
            # (notably UNKNOWN -> RUNNING after the provider recovers).
            was_running_before_unknown_by_agent_id.pop(agent_id, None)
        else:
            # Every other transition leaves the bit alone -- including UNKNOWN -> WAITING,
            # which is consumed by the firing-decision below.
            pass

        is_direct_transition = old_state == "RUNNING" and new_state == "WAITING"
        is_indirect_transition = (
            old_state == "UNKNOWN"
            and new_state == "WAITING"
            and was_running_before_unknown_by_agent_id.pop(agent_id, False)
        )
        if not (is_direct_transition or is_indirect_transition):
            continue

        agent_name = record.data.get("agent_name", "unknown")
        logger.info("{} ({}): {} -> {}", agent_name, agent_id, old_state, new_state)
        write_human_line("{} is now WAITING -- sending notification", agent_name)

        title = "Agent waiting"
        message = f"{agent_name} is waiting for input"
        execute_command = build_execute_command(agent_name, plugin_config)
        notifier.notify(title, message, execute_command, cg)
