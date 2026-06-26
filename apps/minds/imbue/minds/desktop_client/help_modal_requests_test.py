from imbue.minds.desktop_client.help_modal_requests import HelpModalRequestBroker
from imbue.minds.desktop_client.help_modal_requests import OpenHelpRequest


def test_request_open_fans_out_to_every_subscriber() -> None:
    broker = HelpModalRequestBroker()
    first: list[OpenHelpRequest] = []
    second: list[OpenHelpRequest] = []
    broker.add_on_request_callback(first.append)
    broker.add_on_request_callback(second.append)

    request = OpenHelpRequest(description="it broke", workspace_agent_id="agent-1")
    broker.request_open(request)

    assert first == [request]
    assert second == [request]


def test_removed_subscriber_stops_receiving_requests() -> None:
    broker = HelpModalRequestBroker()
    received: list[OpenHelpRequest] = []
    broker.add_on_request_callback(received.append)
    broker.remove_on_request_callback(received.append)

    broker.request_open(OpenHelpRequest(description="ignored"))

    assert received == []


def test_request_open_with_no_subscribers_is_a_noop() -> None:
    # No window is listening, so the request is simply dropped (there is nowhere to show the modal).
    HelpModalRequestBroker().request_open(OpenHelpRequest(description="nobody home"))
