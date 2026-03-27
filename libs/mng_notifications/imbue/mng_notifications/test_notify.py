import json
import threading

import pytest

from imbue.mng.api.observe import get_agent_states_events_path
from imbue.mng.api.observe import get_default_events_base_dir
from imbue.mng.config.data_types import MngContext
from imbue.mng.utils.polling import wait_for
from imbue.mng_notifications.config import NotificationsPluginConfig
from imbue.mng_notifications.mock_notifier_test import RecordingNotifier
from imbue.mng_notifications.watcher import watch_for_waiting_agents


@pytest.mark.acceptance
def test_watcher_detects_running_to_waiting_via_observe_events(
    temp_mng_ctx: MngContext,
) -> None:
    events_path = get_agent_states_events_path(get_default_events_base_dir(temp_mng_ctx.config))
    events_path.parent.mkdir(parents=True, exist_ok=True)

    notifier = RecordingNotifier()
    stop_event = threading.Event()

    watcher_thread = threading.Thread(
        target=watch_for_waiting_agents,
        kwargs={
            "mng_ctx": temp_mng_ctx,
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
                "source": "mng/agent_states",
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
