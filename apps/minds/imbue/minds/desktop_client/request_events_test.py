import json
from pathlib import Path

from imbue.minds.desktop_client.request_events import LatchkeyPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import SharingRequestEvent
from imbue.minds.desktop_client.request_events import SharingStatusSnapshot
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_latchkey_permission_request_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_events import create_sharing_request_event
from imbue.minds.desktop_client.request_events import load_response_events
from imbue.minds.desktop_client.request_events import parse_request_event
from imbue.minds.desktop_client.request_events import write_request_event_to_file


def test_create_sharing_request_event() -> None:
    """Creating a sharing request event populates all fields."""
    event = create_sharing_request_event(
        agent_id="agent-abc",
        service_name="web",
        is_user_requested=True,
        current_status=SharingStatusSnapshot(enabled=True, url="https://example.com"),
        suggested_emails=["test@example.com"],
    )
    assert event.agent_id == "agent-abc"
    assert event.service_name == "web"
    assert event.request_type == str(RequestType.SHARING)
    assert event.is_user_requested is True
    assert event.current_status is not None
    assert event.current_status.enabled is True
    assert event.suggested_emails == ["test@example.com"]
    assert str(event.event_id).startswith("evt-")
    assert str(event.source) == "requests"


def test_parse_request_event_roundtrip() -> None:
    """A sharing request event can be serialized and parsed back."""
    event = create_sharing_request_event(
        agent_id="agent-xyz",
        service_name="api",
    )
    line = json.dumps(event.model_dump(mode="json"))
    parsed = parse_request_event(line)
    assert parsed is not None
    assert isinstance(parsed, SharingRequestEvent)
    assert parsed.agent_id == "agent-xyz"
    assert parsed.service_name == "api"


def test_parse_invalid_json_returns_none() -> None:
    """Invalid JSON returns None instead of raising."""
    assert parse_request_event("not json") is None


def test_inbox_empty_by_default() -> None:
    """A new inbox has no pending requests."""
    inbox = RequestInbox()
    assert inbox.get_pending_requests() == []
    assert inbox.get_pending_count() == 0


def test_inbox_add_request() -> None:
    """Adding a request makes it appear as pending."""
    event = create_sharing_request_event(agent_id="agent-1", service_name="web")
    inbox = RequestInbox().add_request(event)
    pending = inbox.get_pending_requests()
    assert len(pending) == 1
    assert pending[0].agent_id == "agent-1"


def test_inbox_dedup_by_key() -> None:
    """Multiple requests for the same (agent, server, type) are deduplicated."""
    event1 = create_sharing_request_event(agent_id="agent-1", service_name="web")
    event2 = create_sharing_request_event(agent_id="agent-1", service_name="web")
    inbox = RequestInbox().add_request(event1).add_request(event2)
    pending = inbox.get_pending_requests()
    assert len(pending) == 1
    # The latest request should be the one shown
    assert str(pending[0].event_id) == str(event2.event_id)


def test_inbox_different_keys_not_deduped() -> None:
    """Requests with different dedup keys are not merged."""
    event1 = create_sharing_request_event(agent_id="agent-1", service_name="web")
    event2 = create_sharing_request_event(agent_id="agent-1", service_name="api")
    event3 = create_sharing_request_event(agent_id="agent-2", service_name="web")
    inbox = RequestInbox().add_request(event1).add_request(event2).add_request(event3)
    assert inbox.get_pending_count() == 3


def test_inbox_response_removes_from_pending() -> None:
    """A response for a request removes it from the pending list."""
    event = create_sharing_request_event(agent_id="agent-1", service_name="web")
    response = create_request_response_event(
        request_event_id=str(event.event_id),
        status=RequestStatus.GRANTED,
        agent_id="agent-1",
        request_type=str(RequestType.SHARING),
        service_name="web",
    )
    inbox = RequestInbox().add_request(event).add_response(response)
    assert inbox.get_pending_count() == 0


def test_inbox_response_only_affects_matched_request() -> None:
    """A response only removes the request it references, not others."""
    event1 = create_sharing_request_event(agent_id="agent-1", service_name="web")
    event2 = create_sharing_request_event(agent_id="agent-1", service_name="api")
    response = create_request_response_event(
        request_event_id=str(event1.event_id),
        status=RequestStatus.DENIED,
        agent_id="agent-1",
        request_type=str(RequestType.SHARING),
        service_name="web",
    )
    inbox = RequestInbox().add_request(event1).add_request(event2).add_response(response)
    pending = inbox.get_pending_requests()
    assert len(pending) == 1
    assert isinstance(pending[0], SharingRequestEvent)
    assert pending[0].service_name == "api"


def test_inbox_get_request_by_id() -> None:
    """Can find a request by its event_id."""
    event = create_sharing_request_event(agent_id="agent-1", service_name="web")
    inbox = RequestInbox().add_request(event)
    found = inbox.get_request_by_id(str(event.event_id))
    assert found is not None
    assert str(found.event_id) == str(event.event_id)

    assert inbox.get_request_by_id("nonexistent") is None


def test_write_and_load_response_events(tmp_path: Path) -> None:
    """Response events can be written and loaded from disk."""
    response = create_request_response_event(
        request_event_id="evt-abc123",
        status=RequestStatus.GRANTED,
        agent_id="agent-1",
        request_type=str(RequestType.SHARING),
        service_name="web",
    )
    append_response_event(tmp_path, response)
    append_response_event(tmp_path, response)

    loaded = load_response_events(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].request_event_id == "evt-abc123"


def test_write_request_event_to_file(tmp_path: Path) -> None:
    """Request events can be written to a file."""
    events_file = tmp_path / "events" / "requests" / "events.jsonl"
    event = create_sharing_request_event(agent_id="agent-1", service_name="web")
    write_request_event_to_file(events_file, event)

    lines = events_file.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = parse_request_event(lines[0])
    assert parsed is not None
    assert parsed.agent_id == "agent-1"


def test_load_response_events_missing_file(tmp_path: Path) -> None:
    """Loading from a nonexistent file returns an empty list."""
    loaded = load_response_events(tmp_path)
    assert loaded == []


def test_create_latchkey_permission_request_event_populates_all_fields() -> None:
    event = create_latchkey_permission_request_event(
        agent_id="agent-abc",
        service_name="slack",
        rationale="I need to read the team channel to summarize today's discussion.",
    )

    assert event.agent_id == "agent-abc"
    assert event.service_name == "slack"
    assert event.rationale.startswith("I need to read")
    assert event.request_type == str(RequestType.LATCHKEY_PERMISSION)
    assert str(event.event_id).startswith("evt-")
    assert str(event.source) == "requests"


def test_parse_request_event_round_trips_latchkey_permission_request() -> None:
    event = create_latchkey_permission_request_event(
        agent_id="agent-xyz",
        service_name="github",
        rationale="Need to open a PR.",
    )

    line = json.dumps(event.model_dump(mode="json"))
    parsed = parse_request_event(line)

    assert isinstance(parsed, LatchkeyPermissionRequestEvent)
    assert parsed.service_name == "github"
    assert parsed.rationale == "Need to open a PR."


def test_inbox_dedup_includes_latchkey_permission_requests() -> None:
    first = create_latchkey_permission_request_event(
        agent_id="agent-1",
        service_name="slack",
        rationale="first",
    )
    second = create_latchkey_permission_request_event(
        agent_id="agent-1",
        service_name="slack",
        rationale="second",
    )

    inbox = RequestInbox().add_request(first).add_request(second)
    pending = inbox.get_pending_requests()

    assert len(pending) == 1
    assert isinstance(pending[0], LatchkeyPermissionRequestEvent)
    assert pending[0].rationale == "second"


def test_inbox_treats_different_services_as_different_requests() -> None:
    slack_request = create_latchkey_permission_request_event(
        agent_id="agent-1",
        service_name="slack",
        rationale="slack",
    )
    github_request = create_latchkey_permission_request_event(
        agent_id="agent-1",
        service_name="github",
        rationale="github",
    )

    inbox = RequestInbox().add_request(slack_request).add_request(github_request)

    assert inbox.get_pending_count() == 2


def test_request_response_round_trips_auth_failed_status(tmp_path: Path) -> None:
    response = create_request_response_event(
        request_event_id="evt-abc",
        status=RequestStatus.AUTH_FAILED,
        agent_id="agent-1",
        request_type=str(RequestType.LATCHKEY_PERMISSION),
        service_name="slack",
    )
    append_response_event(tmp_path, response)

    loaded = load_response_events(tmp_path)

    assert len(loaded) == 1
    assert loaded[0].status == str(RequestStatus.AUTH_FAILED)
    assert loaded[0].service_name == "slack"


def test_inbox_response_for_latchkey_permission_removes_from_pending() -> None:
    request = create_latchkey_permission_request_event(
        agent_id="agent-1",
        service_name="slack",
        rationale="summary",
    )
    response = create_request_response_event(
        request_event_id=str(request.event_id),
        status=RequestStatus.GRANTED,
        agent_id="agent-1",
        request_type=str(RequestType.LATCHKEY_PERMISSION),
        service_name="slack",
    )

    inbox = RequestInbox().add_request(request).add_response(response)

    assert inbox.get_pending_count() == 0
