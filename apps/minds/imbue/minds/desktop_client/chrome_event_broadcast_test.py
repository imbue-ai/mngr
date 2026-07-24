import queue
import threading

from imbue.minds.desktop_client.chrome_event_broadcast import ChromeEventBroadcaster
from imbue.minds.desktop_client.chrome_event_broadcast import build_workspace_stopped_payload


def test_broadcast_fans_out_a_copy_to_every_subscriber() -> None:
    broadcaster = ChromeEventBroadcaster()
    first_queue: "queue.Queue[dict[str, str]]" = queue.Queue()
    second_queue: "queue.Queue[dict[str, str]]" = queue.Queue()
    first_event = threading.Event()
    second_event = threading.Event()
    broadcaster.subscribe(first_queue, first_event)
    broadcaster.subscribe(second_queue, second_event)

    payload = build_workspace_stopped_payload("agent-1")
    broadcaster.broadcast(payload)

    # Both connections receive the payload and are woken.
    first_delivery = first_queue.get_nowait()
    second_delivery = second_queue.get_nowait()
    assert first_delivery == {"type": "workspace_stopped", "agent_id": "agent-1"}
    assert second_delivery == {"type": "workspace_stopped", "agent_id": "agent-1"}
    assert first_event.is_set()
    assert second_event.is_set()
    # Each subscriber gets an independent copy: mutating one delivery must not
    # leak into another connection's (or the producer's) dict.
    first_delivery["agent_id"] = "mutated"
    assert second_delivery["agent_id"] == "agent-1"
    assert payload["agent_id"] == "agent-1"


def test_unsubscribed_connection_stops_receiving_broadcasts() -> None:
    broadcaster = ChromeEventBroadcaster()
    event_queue: "queue.Queue[dict[str, str]]" = queue.Queue()
    wake_event = threading.Event()
    broadcaster.subscribe(event_queue, wake_event)
    broadcaster.unsubscribe(event_queue, wake_event)

    broadcaster.broadcast(build_workspace_stopped_payload("agent-1"))

    assert event_queue.empty()
    assert not wake_event.is_set()


def test_broadcast_with_no_subscribers_is_a_noop() -> None:
    # No window is subscribed, so the payload is simply dropped (there is nowhere to act on it).
    ChromeEventBroadcaster().broadcast(build_workspace_stopped_payload("agent-1"))
