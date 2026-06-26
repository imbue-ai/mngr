import queue
import threading

from imbue.minds.desktop_client.help_modal_requests import HelpModalRequestBroker
from imbue.minds.desktop_client.help_modal_requests import OpenHelpRequest


def test_request_open_fans_out_to_every_subscriber() -> None:
    broker = HelpModalRequestBroker()
    first_queue: "queue.Queue[OpenHelpRequest]" = queue.Queue()
    second_queue: "queue.Queue[OpenHelpRequest]" = queue.Queue()
    first_event = threading.Event()
    second_event = threading.Event()
    broker.subscribe(first_queue, first_event)
    broker.subscribe(second_queue, second_event)

    request = OpenHelpRequest(description="it broke", workspace_agent_id="agent-1")
    broker.request_open(request)

    # Both connections receive the request and are woken.
    assert first_queue.get_nowait() is request
    assert second_queue.get_nowait() is request
    assert first_event.is_set()
    assert second_event.is_set()


def test_unsubscribed_connection_stops_receiving_requests() -> None:
    broker = HelpModalRequestBroker()
    request_queue: "queue.Queue[OpenHelpRequest]" = queue.Queue()
    wake_event = threading.Event()
    broker.subscribe(request_queue, wake_event)
    broker.unsubscribe(request_queue, wake_event)

    broker.request_open(OpenHelpRequest(description="ignored"))

    assert request_queue.empty()
    assert not wake_event.is_set()


def test_request_open_with_no_subscribers_is_a_noop() -> None:
    # No window is subscribed, so the request is simply dropped (there is nowhere to show the modal).
    HelpModalRequestBroker().request_open(OpenHelpRequest(description="nobody home"))
