import threading
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.jsonl_warn import split_complete_lines
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.errors import MalformedAgentStateEventError
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

    # One warner for the whole tailing session: it buffers a malformed trailing
    # line (treated as an in-progress partial write) and only warns once a later
    # line proves the buffered line was real mid-file corruption.
    warner = MalformedJsonLineWarner(source_description=str(events_path))

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
                # Advance only by the bytes of complete (newline-terminated)
                # lines so a torn trailing line is retried on the next read
                # rather than skipped.
                bytes_consumed = _process_events(
                    new_content,
                    plugin_config,
                    notifier,
                    mngr_ctx.concurrency_group,
                    warner,
                    was_running_before_unknown_by_agent_id,
                )
                last_size += bytes_consumed

        stop_event.wait(timeout=1.0)


def _get_file_size(path: Path) -> int:
    """Get the file size, returning 0 if the file doesn't exist yet.

    A missing file is the legitimate startup case (mngr observe may not have
    created the events file yet). Any other OSError (permission denied, I/O
    error) is unexpected and re-raised rather than masked as "no events".
    """
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _read_from_offset(path: Path, offset: int) -> str:
    """Read content from a file starting at the given byte offset.

    Returns "" if the file does not exist yet (the legitimate startup case).
    Any other OSError is unexpected and propagates rather than being silently
    swallowed as "no new content".
    """
    try:
        with path.open() as f:
            f.seek(offset)
            return f.read()
    except FileNotFoundError:
        return ""


def _process_events(
    content: str,
    plugin_config: NotificationsPluginConfig,
    notifier: Notifier,
    cg: ConcurrencyGroup,
    warner: MalformedJsonLineWarner,
    was_running_before_unknown_by_agent_id: dict[str, bool],
) -> int:
    """Parse JSONL content and send notifications for agents going to WAITING.

    Only whole, newline-terminated lines are consumed; a trailing partial line
    (an in-progress write at the tail of a concurrently-appended file) is held
    back and its bytes are not counted, so it is retried on the next read.
    Returns the number of UTF-8 bytes consumed (the complete-line prefix).

    Malformed JSON lines are routed through ``warner``: an end-of-stream partial
    is buffered (and dropped if never completed), while a genuinely corrupt
    mid-file line is warned-and-skipped on the next line rather than crashing
    the watcher loop.

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
    lines, bytes_consumed = split_complete_lines(content)
    for line in lines:
        parsed = warner.parse(line)
        if parsed is None:
            # Empty line, or a malformed line buffered by the warner.
            continue
        data, _raw_line = parsed

        if data.get("type") != "AGENT_STATE_CHANGE":
            continue

        old_state = data.get("old_state")
        new_state = data.get("new_state")
        agent_id = _require_field(data, "agent_id")

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

        agent_name = _require_field(data, "agent_name")
        logger.info("{} ({}): {} -> {}", agent_name, agent_id, old_state, new_state)
        write_human_line("{} is now WAITING -- sending notification", agent_name)

        title = "Agent waiting"
        message = f"{agent_name} is waiting for input"
        execute_command = build_execute_command(agent_name, plugin_config)
        notifier.notify(title, message, execute_command, cg)

    return bytes_consumed


def _require_field(data: dict[str, object], field_name: str) -> str:
    """Return a required string field from an AGENT_STATE_CHANGE record.

    Raises MalformedAgentStateEventError if the field is absent or not a string,
    so a malformed record fails loudly instead of being collapsed into a
    fabricated "unknown" identity.
    """
    value = data.get(field_name)
    if not isinstance(value, str):
        raise MalformedAgentStateEventError(
            f"AGENT_STATE_CHANGE event missing required string field {field_name!r}: {data!r}"
        )
    return value
