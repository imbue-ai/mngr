import json
from typing import Any

from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event


def test_emit_event_human_writes_message_to_stdout(capfd: Any) -> None:
    emit_event("test_event", {"message": "hello world"}, OutputFormat.HUMAN)

    captured = capfd.readouterr()
    assert "hello world" in captured.out
    assert captured.err == ""


def test_emit_event_jsonl_writes_json_to_stdout(capfd: Any) -> None:
    emit_event("login_url", {"login_url": "http://localhost:1234"}, OutputFormat.JSONL)

    captured = capfd.readouterr()
    event = json.loads(captured.out.strip())
    assert event["event"] == "login_url"
    assert event["login_url"] == "http://localhost:1234"
    assert captured.err == ""


def test_emit_event_json_is_silent(capfd: Any) -> None:
    emit_event("test_event", {"message": "should not appear"}, OutputFormat.JSON)

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_emit_event_human_without_message_key_is_silent(capfd: Any) -> None:
    emit_event("test_event", {"data": 123}, OutputFormat.HUMAN)

    captured = capfd.readouterr()
    assert captured.out == ""
