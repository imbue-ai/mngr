import json

import pytest

from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest


def _emitted_events(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.strip().splitlines() if line.strip()]


def test_electron_dispatch_emits_the_slack_style_layout_and_the_click_url(capsys: pytest.CaptureFixture[str]) -> None:
    dispatcher = NotificationDispatcher(is_electron=True)

    dispatcher.dispatch(
        NotificationRequest(
            title="alpha", subtitle="Gmail", body="Needs your inbox.", url="/workspace/agent-1?review=r"
        )
    )

    (event,) = _emitted_events(capsys.readouterr().out)
    assert event["event"] == "notification"
    assert event["title"] == "alpha"
    assert event["subtitle"] == "Gmail"
    assert event["body"] == "Needs your inbox."
    assert event["url"] == "/workspace/agent-1?review=r"


def test_electron_dispatch_omits_the_url_when_there_is_nowhere_to_land(capsys: pytest.CaptureFixture[str]) -> None:
    dispatcher = NotificationDispatcher(is_electron=True)

    dispatcher.dispatch(NotificationRequest(title="Imbue Studio", subtitle="Test notification", body="hello"))

    (event,) = _emitted_events(capsys.readouterr().out)
    assert "url" not in event


def test_dispatch_outside_electron_reaches_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """A bare ``minds run`` has no OS channel: nothing is written, nothing raises."""
    dispatcher = NotificationDispatcher(is_electron=False)

    dispatcher.dispatch(NotificationRequest(title="alpha", subtitle="Gmail", body="x"))
    dispatcher.dispatch_read("agent-chat1")

    assert capsys.readouterr().out == ""


def test_electron_read_event_names_the_chat_whose_banners_main_closes(capsys: pytest.CaptureFixture[str]) -> None:
    """The event electron/main.js's handleNotificationRead reads ``chat_agent_id`` from."""
    dispatcher = NotificationDispatcher(is_electron=True)

    dispatcher.dispatch_read("agent-chat1")

    (event,) = _emitted_events(capsys.readouterr().out)
    assert event["event"] == "notification_read"
    assert event["chat_agent_id"] == "agent-chat1"
