"""Tests for the /ui/api notification-feed action routes (workspace read, clear, clear-all)."""

from pathlib import Path

from flask import Flask

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.notification_feed import AgentMessageCard
from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_models import UiNotificationEntry

_WORKSPACE_ID = "agent-" + "a" * 32


def _feed_of(app: Flask) -> NotificationFeed:
    with app.app_context():
        feed = get_state().notification_feed
    assert feed is not None
    return feed


def _append_message(feed: NotificationFeed, workspace_agent_id: str = _WORKSPACE_ID) -> UiNotificationEntry:
    return feed.append_agent_message(
        AgentMessageCard(
            chat_agent_id="agent-" + "c" * 32,
            chat_name="chat",
            body="done",
            workspace_agent_id=workspace_agent_id,
            workspace_name="alpha",
            workspace_accent="#aabbcc",
        )
    )


def _entry_ids(feed: NotificationFeed) -> list[str]:
    return [entry.id for entry in feed.reconcile((), {}).entries]


def test_reading_a_workspace_removes_its_agent_messages(tmp_path: Path) -> None:
    client, app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)
    feed = _feed_of(app)
    _append_message(feed)
    kept = _append_message(feed, workspace_agent_id="agent-" + "b" * 32)

    response = client.post(f"/ui/api/notifications/workspace/{_WORKSPACE_ID}/read")

    assert response.status_code == 200
    assert _entry_ids(feed) == [kept.id]


def test_clearing_one_entry_and_clearing_all(tmp_path: Path) -> None:
    client, app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)
    feed = _feed_of(app)
    cleared = _append_message(feed)
    _append_message(feed)
    _append_message(feed)

    single = client.post(f"/ui/api/notifications/{cleared.id}/clear")
    assert single.status_code == 200
    assert len(_entry_ids(feed)) == 2

    everything = client.post("/ui/api/notifications/clear-all")
    assert everything.status_code == 200
    assert _entry_ids(feed) == []


def test_feed_actions_require_authentication(tmp_path: Path) -> None:
    client, app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)
    feed = _feed_of(app)
    entry = _append_message(feed)

    responses = [
        client.post(f"/ui/api/notifications/{entry.id}/clear"),
        client.post(f"/ui/api/notifications/workspace/{_WORKSPACE_ID}/read"),
        client.post("/ui/api/notifications/clear-all"),
    ]

    assert [response.status_code for response in responses] == [401, 401, 401]
    assert _entry_ids(feed) == [entry.id]
