import json
import threading
from uuid import uuid4

from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.mock_notifier_test import RecordingNotifier
from imbue.mngr_notifications.watcher import watch_for_waiting_agents


def _running_to_waiting_event(agent_name: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "AGENT_STATE_CHANGE",
            "event_id": f"evt-{uuid4().hex}",
            "source": "mngr/agent_states",
            "agent_id": f"agent-{uuid4().hex}",
            "agent_name": agent_name,
            "old_state": "RUNNING",
            "new_state": "WAITING",
            "agent": {},
        }
    )


def test_watcher_detects_running_to_waiting_via_observe_events(
    temp_mngr_ctx: MngrContext,
) -> None:
    """End-to-end: the watcher tails the observe events file, ignores events that
    predate its initial offset, and notifies for a RUNNING -> WAITING event appended
    after it starts -- then stops cleanly when stop_event is set."""
    events_path = get_agent_states_events_path(get_default_events_base_dir(temp_mngr_ctx.config))
    events_path.parent.mkdir(parents=True, exist_ok=True)

    # An event written before the watcher starts is below its initial offset and must NOT fire.
    pre_agent_name = f"pre-agent-{uuid4().hex}"
    events_path.write_text(_running_to_waiting_event(pre_agent_name) + "\n")

    notifier = RecordingNotifier()
    stop_event = threading.Event()
    ready_event = threading.Event()

    watcher_thread = threading.Thread(
        target=watch_for_waiting_agents,
        kwargs={
            "mngr_ctx": temp_mngr_ctx,
            "plugin_config": NotificationsPluginConfig(),
            "notifier": notifier,
            "stop_event": stop_event,
            "ready_event": ready_event,
        },
    )
    watcher_thread.start()

    try:
        assert ready_event.wait(timeout=5), "Watcher did not become ready"

        new_agent_name = f"watch-test-{uuid4().hex}"
        with events_path.open("a") as f:
            f.write(_running_to_waiting_event(new_agent_name) + "\n")

        wait_for(
            lambda: len(notifier.calls) > 0,
            timeout=5,
            poll_interval=0.1,
            error_message="Watcher did not send notification for RUNNING -> WAITING event",
        )

        # Exactly the post-start event fired; the pre-existing one was skipped via the initial offset.
        assert len(notifier.calls) == 1
        assert notifier.calls[0][0] == "Agent waiting"
        assert new_agent_name in notifier.calls[0][1]
        assert pre_agent_name not in notifier.calls[0][1]
    finally:
        stop_event.set()
        watcher_thread.join(timeout=5)
