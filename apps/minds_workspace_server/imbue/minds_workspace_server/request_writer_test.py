"""Tests for request_writer module."""

import json
from pathlib import Path

import pytest

from imbue.minds_workspace_server.request_writer import UnknownRequestTypeError
from imbue.minds_workspace_server.request_writer import write_refresh_request
from imbue.minds_workspace_server.request_writer import write_request_event


def test_write_refresh_request_writes_jsonl_to_correct_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    write_refresh_request("web")

    events_file = tmp_path / "events" / "refresh" / "events.jsonl"
    assert events_file.exists()
    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "refresh_service"
    assert event["source"] == "refresh"
    assert event["service_name"] == "web"
    assert event["event_id"].startswith("evt-")
    assert "timestamp" in event


def test_write_refresh_request_appends_multiple_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    write_refresh_request("web")
    write_refresh_request("api")

    events_file = tmp_path / "events" / "refresh" / "events.jsonl"
    lines = events_file.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["service_name"] == "web"
    assert json.loads(lines[1])["service_name"] == "api"


def test_write_refresh_request_without_agent_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="MNGR_AGENT_STATE_DIR"):
        write_refresh_request("web")


def test_write_request_event_writes_latchkey_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-42")

    written = write_request_event(
        request_type="LATCHKEY_PERMISSION",
        payload={"service_name": "slack", "rationale": "need to post status updates"},
    )

    events_file = tmp_path / "events" / "requests" / "events.jsonl"
    assert events_file.exists()
    event = json.loads(events_file.read_text().splitlines()[0])
    assert event == written
    assert event["type"] == "latchkey_permission_request"
    assert event["source"] == "requests"
    assert event["agent_id"] == "agent-42"
    assert event["request_type"] == "LATCHKEY_PERMISSION"
    assert event["is_user_requested"] is True
    assert event["service_name"] == "slack"
    assert event["rationale"] == "need to post status updates"
    assert event["event_id"].startswith("evt-")
    assert "timestamp" in event


def test_write_request_event_strips_reserved_metadata_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller cannot spoof identity fields by including them in the payload."""
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "real-agent")

    write_request_event(
        request_type="LATCHKEY_PERMISSION",
        payload={
            "service_name": "github",
            "rationale": "why",
            "agent_id": "spoofed-agent",
            "event_id": "evt-spoofed",
            "source": "not-requests",
            "type": "fake_type",
            "timestamp": "1970-01-01T00:00:00.000000Z",
            "request_type": "PERMISSIONS",
        },
    )

    events_file = tmp_path / "events" / "requests" / "events.jsonl"
    event = json.loads(events_file.read_text().splitlines()[0])
    assert event["agent_id"] == "real-agent"
    assert event["event_id"] != "evt-spoofed"
    assert event["source"] == "requests"
    assert event["type"] == "latchkey_permission_request"
    assert event["timestamp"] != "1970-01-01T00:00:00.000000Z"
    assert event["request_type"] == "LATCHKEY_PERMISSION"


def test_write_request_event_rejects_unknown_request_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")

    with pytest.raises(UnknownRequestTypeError, match="CUSTOM_THING"):
        write_request_event(request_type="CUSTOM_THING", payload={"foo": "bar"})

    events_file = tmp_path / "events" / "requests" / "events.jsonl"
    assert not events_file.exists()


def test_write_request_event_rejects_empty_request_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")

    with pytest.raises(UnknownRequestTypeError):
        write_request_event(request_type="", payload={})


def test_write_request_event_accepts_all_known_request_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each known request_type maps to a distinct envelope ``type`` and writes successfully."""
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")

    expected_event_types = {
        "PERMISSIONS": "permissions_request",
        "LATCHKEY_PERMISSION": "latchkey_permission_request",
    }
    for request_type, expected_type in expected_event_types.items():
        written = write_request_event(request_type=request_type, payload={})
        assert written["type"] == expected_type
        assert written["request_type"] == request_type


def test_write_request_event_requires_agent_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)

    with pytest.raises(RuntimeError, match="MNGR_AGENT_ID"):
        write_request_event(request_type="LATCHKEY_PERMISSION", payload={})
