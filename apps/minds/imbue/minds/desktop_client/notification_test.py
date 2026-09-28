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

    dispatcher.dispatch(NotificationRequest(title="Mind", subtitle="Test notification", body="hello"))

    (event,) = _emitted_events(capsys.readouterr().out)
    assert "url" not in event


def test_dispatch_outside_electron_reaches_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """A bare ``minds run`` has no OS channel: nothing is written, nothing raises."""
    dispatcher = NotificationDispatcher(is_electron=False)

    dispatcher.dispatch(NotificationRequest(title="alpha", subtitle="Gmail", body="x"))

    assert capsys.readouterr().out == ""
