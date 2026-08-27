import json
from pathlib import Path

from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.latchkey.response_events import append_response_event
from imbue.minds.desktop_client.latchkey.response_events import create_request_response_event
from imbue.minds.desktop_client.latchkey.response_events import load_response_events


def test_write_and_load_response_events(tmp_path: Path) -> None:
    """Response events can be written and loaded from disk."""
    response = create_request_response_event(
        request_event_id="evt-abc123",
        status=RequestStatus.GRANTED,
        agent_id="agent-1",
    )
    append_response_event(tmp_path, response)
    append_response_event(tmp_path, response)

    loaded = load_response_events(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].request_event_id == "evt-abc123"


def test_load_response_events_missing_file(tmp_path: Path) -> None:
    """Loading from a nonexistent file returns an empty list."""
    loaded = load_response_events(tmp_path)
    assert loaded == []


def test_load_response_events_tolerates_unknown_legacy_fields(tmp_path: Path) -> None:
    """Historical events.jsonl entries with retired fields must still load."""
    events_dir = tmp_path / "events" / "requests"
    events_dir.mkdir(parents=True)
    event = create_request_response_event(
        request_event_id="evt-1",
        status=RequestStatus.GRANTED,
        agent_id="agent-abc",
    )
    line = dict(
        json.loads(event.model_dump_json()),
        type="request_response",
        event_id="evt-1234",
        source="requests",
        request_type="LATCHKEY_PERMISSION",
        scope="slack-api",
        service_name="slack",
        is_user_requested=True,
    )
    (events_dir / "events.jsonl").write_text(json.dumps(line) + "\n")

    loaded = load_response_events(tmp_path)

    assert [e.request_event_id for e in loaded] == ["evt-1"]


def test_append_writes_the_legacy_envelope_keys_for_older_readers(tmp_path: Path) -> None:
    """A pre-slim desktop client hard-requires the envelope fields; the writer
    keeps emitting them (CLEANUP-marked) so a rollback can still read verdicts."""
    append_response_event(
        tmp_path,
        create_request_response_event(request_event_id="evt-b", status=RequestStatus.DENIED, agent_id="agent-1"),
    )
    line = json.loads((tmp_path / "events" / "requests" / "events.jsonl").read_text())
    assert line["type"] == "request_response"
    assert line["source"] == "requests"
    assert line["event_id"].startswith("evt-")
    assert line["request_type"] == "LATCHKEY_PERMISSION"
    assert line["status"] == "DENIED"
