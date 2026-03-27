import json
import threading

import pytest

from imbue.mngr.api.observe import get_agent_states_events_path
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.mock_notifier_test import RecordingNotifier
from imbue.mngr_notifications.watcher import watch_for_waiting_agents


@pytest.mark.acceptance
def test_watcher_detects_running_to_waiting_via_observe_events(
    temp_mngr_ctx: MngrContext,
) -> None:
    events_path = get_agent_states_events_path(get_default_events_base_dir(temp_mngr_ctx.config))
    events_path.parent.mkdir(parents=True, exist_ok=True)

    notifier = RecordingNotifier()
    stop_event = threading.Event()

    watcher_thread = threading.Thread(
        target=watch_for_waiting_agents,
        kwargs={
            "mngr_ctx": temp_mngr_ctx,
            "plugin_config": NotificationsPluginConfig(),
            "notifier": notifier,
            "stop_event": stop_event,
        },
    )
    watcher_thread.start()

    try:
        stop_event.wait(timeout=0.5)

        event = json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "AGENT_STATE_CHANGE",
                "event_id": "evt-test123",
                "source": "mngr/agent_states",
                "agent_id": "agent-test",
                "agent_name": "watch-test",
                "old_state": "RUNNING",
                "new_state": "WAITING",
                "agent": {},
            }
        )
        with events_path.open("a") as f:
            f.write(event + "\n")

        wait_for(
            lambda: len(notifier.calls) > 0,
            timeout=5,
            poll_interval=0.3,
            error_message="Watcher did not send notification for RUNNING -> WAITING event",
        )

        assert notifier.calls[0][0] == "Agent waiting"
        assert "watch-test" in notifier.calls[0][1]
    finally:
        stop_event.set()
        watcher_thread.join(timeout=5)
