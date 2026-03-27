import threading
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.events import parse_event_line
from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
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
) -> None:
    """Watch the mngr observe event stream for RUNNING -> WAITING transitions.

    Tails the agent_states events file written by `mngr observe` and sends
    desktop notifications when agents transition from RUNNING to WAITING.
    Runs until stop_event is set or interrupted.
    """
    if stop_event is None:
        stop_event = threading.Event()

    events_path = get_agent_states_events_path(get_default_events_base_dir(mngr_ctx.config))
    logger.info("Watching for agent state transitions in {}", events_path)

    last_size = _get_file_size(events_path)

    while not stop_event.is_set():
        current_size = _get_file_size(events_path)
        if current_size > last_size:
            new_content = _read_from_offset(events_path, last_size)
            if new_content:
                _process_events(
                    new_content,
                    plugin_config,
                    notifier,
                    mngr_ctx.concurrency_group,
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
) -> None:
    """Parse JSONL content and send notifications for RUNNING -> WAITING transitions."""
    for line in content.splitlines():
        record = parse_event_line(line, AGENT_STATES_SOURCE)
        if record is None:
            continue

        if record.data.get("type") != "AGENT_STATE_CHANGE":
            continue
        if record.data.get("old_state") != "RUNNING" or record.data.get("new_state") != "WAITING":
            continue

        agent_name = record.data.get("agent_name", "unknown")
        agent_id = record.data.get("agent_id", "unknown")
        logger.info("{} ({}): RUNNING -> WAITING", agent_name, agent_id)

        title = "Agent waiting"
        message = f"{agent_name} is waiting for input"
        execute_command = build_execute_command(agent_name, plugin_config)
        notifier.notify(title, message, execute_command, cg)
