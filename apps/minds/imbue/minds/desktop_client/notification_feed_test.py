"""Tests for the notification feed's reconcile semantics and OS dispatch gating."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification_feed import AgentMessageCard
from imbue.minds.desktop_client.notification_feed import NotificationDispatchPreferences
from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.notification_feed import PendingNotificationCard
from imbue.minds.desktop_client.notification_feed import SystemEventCard
from imbue.minds.desktop_client.testing import RecordingNotificationDispatcher
from imbue.minds.desktop_client.ui_models import NotificationKind
from imbue.minds.desktop_client.ui_models import NotificationOutcome
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage


def _make_recording_dispatcher() -> RecordingNotificationDispatcher:
    return RecordingNotificationDispatcher(is_electron=True)


def _at(minute: int) -> datetime:
    return datetime(2026, 8, 18, 12, minute, 0, tzinfo=timezone.utc)


def _ts(minute: int) -> str:
    return _at(minute).isoformat()


# The default feed construction time: an hour before the default card
# timestamps, so cards made by ``_card`` count as filed after launch.
_CONSTRUCTED_AT = datetime(2026, 8, 18, 11, 0, 0, tzinfo=timezone.utc)


def _make_feed(
    dispatcher: NotificationDispatcher | None = None,
    is_enabled: bool = True,
    style: NotificationStyle = NotificationStyle.BOTH,
    constructed_at: datetime = _CONSTRUCTED_AT,
    connected_workspace_agent_ids: tuple[str, ...] = (),
    cleared_request_ids_path: Path | None = None,
) -> NotificationFeed:
    preferences = NotificationDispatchPreferences(is_enabled=is_enabled, style=style)
    return NotificationFeed(
        notification_dispatcher=dispatcher,
        get_dispatch_preferences=lambda: preferences,
        get_connected_focused_workspace_agent_ids=lambda: connected_workspace_agent_ids,
        constructed_at=constructed_at,
        cleared_request_ids_path=cleared_request_ids_path,
    )


def _card(
    request_id: str,
    requested_at: str | None = _ts(0),
    title: str = "Gmail",
    body: str = "Needs to read your inbox to triage email.",
    workspace_agent_id: str = "agent-" + "a" * 32,
    workspace_name: str = "alpha",
    workspace_accent: str = "#aabbcc",
    service_name: str = "gmail",
) -> PendingNotificationCard:
    return PendingNotificationCard(
        request_id=request_id,
        requested_at=requested_at,
        title=title,
        body=body,
        workspace_agent_id=workspace_agent_id,
        workspace_name=workspace_name,
        workspace_accent=workspace_accent,
        service_name=service_name,
    )


def _entry_ids(message: UiNotificationsMessage) -> list[str]:
    return [entry.id for entry in message.entries]


def test_new_pending_request_creates_an_unresolved_entry_with_snapshotted_fields() -> None:
    feed = _make_feed()

    message = feed.reconcile((_card("evt-1"),), {})

    assert message.unresolved_count == 1
    (entry,) = message.entries
    assert entry.id == "evt-1"
    assert entry.kind == "permission_request"
    assert entry.created_at == _ts(0)
    assert entry.is_resolved is False
    assert entry.outcome is None
    assert entry.title == "Gmail"
    assert entry.body == "Needs to read your inbox to triage email."
    assert entry.request_id == "evt-1"
    assert entry.workspace_agent_id == "agent-" + "a" * 32
    assert entry.workspace_name == "alpha"
    assert entry.workspace_accent == "#aabbcc"
    assert entry.service_name == "gmail"


def test_created_at_is_the_request_own_timestamp_not_reconcile_time() -> None:
    """A backfilled day-old request keeps its true age instead of reading "just now"."""
    feed = _make_feed()
    day_old = "2026-08-17T09:30:00.000000Z"

    message = feed.reconcile((_card("evt-1", requested_at=day_old),), {})

    (entry,) = message.entries
    assert entry.created_at == day_old


@pytest.mark.parametrize("outcome", [NotificationOutcome.APPROVED, NotificationOutcome.DENIED])
def test_response_resolves_the_entry_with_the_response_outcome(outcome: NotificationOutcome) -> None:
    feed = _make_feed()
    feed.reconcile((_card("evt-1"),), {})

    message = feed.reconcile((), {"evt-1": outcome})

    (entry,) = message.entries
    assert entry.is_resolved is True
    assert entry.outcome == outcome
    assert message.unresolved_count == 0


def test_vanished_request_without_a_response_closes_the_entry() -> None:
    feed = _make_feed()
    feed.reconcile((_card("evt-1"),), {})

    message = feed.reconcile((), {})

    (entry,) = message.entries
    assert entry.is_resolved is True
    assert entry.outcome == "closed"
    # The display snapshot survives the source request vanishing.
    assert entry.title == "Gmail"
    assert entry.workspace_name == "alpha"


def test_closed_entry_reopens_when_its_request_reappears_without_a_response() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)
    feed.reconcile((_card("evt-1"),), {})
    feed.reconcile((), {})

    message = feed.reconcile((_card("evt-1"),), {})

    (entry,) = message.entries
    assert entry.is_resolved is False
    assert entry.outcome is None
    # Reopening flips state on the existing entry: no new created_at, no second OS nudge.
    assert entry.created_at == _ts(0)
    assert len(dispatcher.dispatched) == 1
    assert message.unresolved_count == 1


def test_reappearing_request_with_a_recorded_response_stays_resolved() -> None:
    feed = _make_feed()
    feed.reconcile((_card("evt-1"),), {})

    message = feed.reconcile((_card("evt-1"),), {"evt-1": NotificationOutcome.APPROVED})

    (entry,) = message.entries
    assert entry.is_resolved is True
    assert entry.outcome == "approved"


def test_approved_entry_never_reopens_even_when_its_id_is_pending_again() -> None:
    feed = _make_feed()
    feed.reconcile((_card("evt-1"),), {})
    feed.reconcile((), {"evt-1": NotificationOutcome.APPROVED})

    message = feed.reconcile((_card("evt-1"),), {})

    (entry,) = message.entries
    assert entry.is_resolved is True
    assert entry.outcome == "approved"


def test_eviction_drops_the_oldest_resolved_entries_beyond_the_cap() -> None:
    feed = _make_feed()
    # 53 requests, one filed per minute; the three oldest resolve, then vanish.
    cards = tuple(_card(f"evt-{n:03d}", requested_at=_ts(n)) for n in range(53))
    feed.reconcile(cards, {})

    message = feed.reconcile(
        cards[3:],
        {
            "evt-000": NotificationOutcome.APPROVED,
            "evt-001": NotificationOutcome.DENIED,
            "evt-002": NotificationOutcome.APPROVED,
        },
    )

    # 50 unresolved + 3 resolved = 53 > 50: exactly the 3 resolved (the oldest) are evicted.
    assert len(message.entries) == 50
    assert message.unresolved_count == 50
    assert not any(entry.id in ("evt-000", "evt-001", "evt-002") for entry in message.entries)


def test_eviction_keeps_newer_resolved_entries_when_older_resolved_ones_cover_the_overflow() -> None:
    feed = _make_feed()
    cards = tuple(_card(f"evt-{n:03d}", requested_at=_ts(n)) for n in range(52))
    feed.reconcile(cards, {})

    message = feed.reconcile(
        cards[3:],
        {
            "evt-000": NotificationOutcome.APPROVED,
            "evt-001": NotificationOutcome.DENIED,
            "evt-002": NotificationOutcome.APPROVED,
        },
    )

    # 49 unresolved + 3 resolved = 52: evict the 2 oldest resolved, keep evt-002.
    assert len(message.entries) == 50
    resolved_ids = [entry.id for entry in message.entries if entry.is_resolved]
    assert resolved_ids == ["evt-002"]


def test_unresolved_entries_are_never_evicted_even_beyond_the_cap() -> None:
    feed = _make_feed()
    cards = tuple(_card(f"evt-{n:03d}") for n in range(55))

    message = feed.reconcile(cards, {})

    assert len(message.entries) == 55
    assert message.unresolved_count == 55


def test_wire_order_is_unresolved_first_then_resolved_each_newest_first() -> None:
    feed = _make_feed()
    cards = tuple(_card(f"evt-{n}", requested_at=_ts(n)) for n in range(1, 5))
    feed.reconcile(cards, {})

    message = feed.reconcile(
        (_card("evt-2", requested_at=_ts(2)), _card("evt-4", requested_at=_ts(4))),
        {"evt-1": NotificationOutcome.APPROVED, "evt-3": NotificationOutcome.DENIED},
    )

    # Unresolved newest-first (evt-4 then evt-2), then resolved newest-first (evt-3 then evt-1).
    assert _entry_ids(message) == ["evt-4", "evt-2", "evt-3", "evt-1"]
    assert message.unresolved_count == 2


def test_entries_order_by_their_request_timestamps_not_arrival_order() -> None:
    """A late-arriving old request files behind the newer ones already in the feed."""
    feed = _make_feed()
    feed.reconcile((_card("evt-new", requested_at=_ts(30)),), {})

    message = feed.reconcile(
        (_card("evt-new", requested_at=_ts(30)), _card("evt-old", requested_at=_ts(5))),
        {},
    )

    assert _entry_ids(message) == ["evt-new", "evt-old"]


def test_created_at_ties_break_by_id_for_a_deterministic_order() -> None:
    feed = _make_feed()

    message = feed.reconcile((_card("evt-b"), _card("evt-a"), _card("evt-c")), {})

    assert _entry_ids(message) == ["evt-c", "evt-b", "evt-a"]


def test_a_new_entry_dispatches_exactly_once_across_repeated_reconciles() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.reconcile((_card("evt-1"),), {})
    feed.reconcile((_card("evt-1"),), {})
    feed.reconcile((_card("evt-1"),), {})

    assert len(dispatcher.dispatched) == 1


def test_an_entry_created_and_resolved_in_the_same_reconcile_does_not_dispatch() -> None:
    """A response landing within one publish interval of the request settles it before any banner."""
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    message = feed.reconcile((_card("evt-1"),), {"evt-1": NotificationOutcome.APPROVED})

    (entry,) = message.entries
    assert entry.is_resolved is True
    assert dispatcher.dispatched == []


def test_a_response_landing_between_the_dispatch_decision_and_the_dispatch_itself_suppresses_the_banner() -> None:
    """Regression guard for the staleness window between reconcile()'s locked dispatchable
    check and _dispatch_new_entry's actual dispatch, which runs outside the lock and reads
    live preferences from disk in between. get_dispatch_preferences is exactly the point
    _dispatch_new_entry reads live and unlocked, so a side effect there stands in for a
    genuinely concurrent reconcile() call (from another WS-connect thread, per the module's
    own thread-safety docstring) resolving this entry in that window."""
    dispatcher = _make_recording_dispatcher()
    preferences = NotificationDispatchPreferences(is_enabled=True, style=NotificationStyle.BOTH)
    feed: NotificationFeed

    def get_dispatch_preferences() -> NotificationDispatchPreferences:
        feed.reconcile((), {"evt-1": NotificationOutcome.APPROVED})
        return preferences

    feed = NotificationFeed(
        notification_dispatcher=dispatcher,
        get_dispatch_preferences=get_dispatch_preferences,
        get_connected_focused_workspace_agent_ids=lambda: (),
        constructed_at=_CONSTRUCTED_AT,
    )

    message = feed.reconcile((_card("evt-1"),), {})

    assert dispatcher.dispatched == []
    # The returned message predates the race (built before dispatch ran), so
    # it still reports the entry unresolved -- the point is that no banner
    # fired for what is, by dispatch time, an already-resolved request.
    (entry,) = message.entries
    assert entry.is_resolved is False


def test_reconcile_without_a_dispatcher_records_entries_and_does_not_raise() -> None:
    feed = _make_feed(dispatcher=None)

    message = feed.reconcile((_card("evt-1"),), {})

    assert message.unresolved_count == 1


@pytest.mark.parametrize(
    ("is_enabled", "style", "is_dispatch_expected"),
    [
        (True, NotificationStyle.BOTH, True),
        (True, NotificationStyle.OS, True),
        (True, NotificationStyle.CARDS, False),
        (False, NotificationStyle.BOTH, False),
        (False, NotificationStyle.OS, False),
        (False, NotificationStyle.CARDS, False),
    ],
)
def test_dispatch_is_gated_by_the_master_toggle_and_style(
    is_enabled: bool, style: NotificationStyle, is_dispatch_expected: bool
) -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, is_enabled=is_enabled, style=style)

    feed.reconcile((_card("evt-1"),), {})

    assert (len(dispatcher.dispatched) == 1) is is_dispatch_expected


def test_startup_backfill_records_entries_without_dispatching() -> None:
    """Requests redelivered at launch become feed entries but never OS banners."""
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, constructed_at=_at(30))

    message = feed.reconcile(
        (_card("evt-1", requested_at=_ts(0)), _card("evt-2", requested_at=_ts(29))),
        {},
    )

    assert message.unresolved_count == 2
    assert dispatcher.dispatched == []


def test_a_request_filed_after_the_feed_came_up_dispatches_amid_backfill() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, constructed_at=_at(30))

    feed.reconcile(
        (_card("evt-old", requested_at=_ts(0)), _card("evt-new", requested_at=_ts(31), title="Slack")),
        {},
    )

    (request,) = dispatcher.dispatched
    assert request.subtitle == "Slack"


def test_a_request_filed_exactly_at_construction_counts_as_backfill() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, constructed_at=_at(30))

    feed.reconcile((_card("evt-1", requested_at=_ts(30)),), {})

    assert dispatcher.dispatched == []


def test_an_unparseable_requested_at_records_the_entry_silently() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    message = feed.reconcile((_card("evt-1", requested_at="not-a-timestamp"),), {})

    assert message.unresolved_count == 1
    assert dispatcher.dispatched == []


def test_a_request_with_no_filing_time_records_the_entry_silently() -> None:
    """A record the gateway wrote before it stamped filing times is one that predates this launch."""
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    message = feed.reconcile((_card("evt-1", requested_at=None),), {})

    assert _entry_ids(message) == ["evt-1"]
    assert datetime.fromisoformat(message.entries[0].created_at) > _CONSTRUCTED_AT
    assert dispatcher.dispatched == []


def test_a_naive_requested_at_is_assumed_utc() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.reconcile((_card("evt-1", requested_at="2026-08-18T12:05:00.000000"),), {})

    assert len(dispatcher.dispatched) == 1


def test_the_gateway_z_suffixed_timestamp_format_parses_and_dispatches() -> None:
    """Request events stamp ``%Y-%m-%dT%H:%M:%S.%fZ``; that exact shape must count as after launch."""
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.reconcile((_card("evt-1", requested_at="2026-08-18T12:05:00.123456Z"),), {})

    assert len(dispatcher.dispatched) == 1


def test_no_dispatch_when_a_focused_connected_window_displays_the_asking_workspace() -> None:
    dispatcher = _make_recording_dispatcher()
    agent_id = "agent-" + "a" * 32
    feed = _make_feed(dispatcher=dispatcher, connected_workspace_agent_ids=(agent_id,))

    message = feed.reconcile((_card("evt-1", workspace_agent_id=agent_id),), {})

    assert message.unresolved_count == 1
    assert dispatcher.dispatched == []


def test_dispatch_proceeds_when_connected_windows_display_other_workspaces() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, connected_workspace_agent_ids=("agent-" + "b" * 32,))

    feed.reconcile((_card("evt-1", workspace_agent_id="agent-" + "a" * 32),), {})

    assert len(dispatcher.dispatched) == 1


def test_an_unresolvable_workspace_entry_still_dispatches_with_windows_connected() -> None:
    """No workspace agent id means the entry can never be "on screen"."""
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, connected_workspace_agent_ids=("agent-" + "b" * 32,))

    feed.reconcile((_card("evt-1", workspace_agent_id=""),), {})

    assert len(dispatcher.dispatched) == 1


def test_a_recreated_entry_after_eviction_does_not_dispatch_again() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)
    feed.reconcile((_card("evt-x"),), {})
    assert len(dispatcher.dispatched) == 1
    # The request vanishes without a response: the entry closes.
    feed.reconcile((), {})
    # 51 unresolved backfilled cards push the feed past the cap, evicting the
    # closed entry (backfilled so the fillers themselves stay silent).
    fillers = tuple(_card(f"evt-fill-{n:02d}", requested_at="2026-08-18T01:00:00+00:00") for n in range(51))
    evicted_message = feed.reconcile(fillers, {})
    assert "evt-x" not in _entry_ids(evicted_message)

    message = feed.reconcile((*fillers, _card("evt-x")), {})

    # The request reappeared and its entry was recreated, but it already
    # nudged once this process: no second banner.
    assert "evt-x" in _entry_ids(message)
    assert len(dispatcher.dispatched) == 1


def test_dispatched_request_lays_out_workspace_title_headline_subtitle_and_review_deep_link() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)
    agent_id = "agent-" + "a" * 32

    feed.reconcile((_card("evt-1"),), {})

    (request,) = dispatcher.dispatched
    assert request.title == "alpha"
    assert request.subtitle == "Gmail"
    assert request.body == "Needs to read your inbox to triage email."
    assert request.url == f"/workspace/{agent_id}?review=evt-1"


def test_dispatched_request_falls_back_to_a_stock_body_when_the_rationale_is_empty() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.reconcile((_card("evt-1", body=""),), {})

    (request,) = dispatcher.dispatched
    assert request.body == "Waiting on your review."


def test_dispatched_request_omits_the_deep_link_when_the_workspace_is_unresolvable() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.reconcile((_card("evt-1", workspace_agent_id=""),), {})

    (request,) = dispatcher.dispatched
    assert request.url is None


# Agent messages and system events

_WORKSPACE_ID = "agent-" + "a" * 32
_CHAT_ID = "agent-" + "c" * 32


def _agent_message(
    body: str = "Finished the migration; 3 tables moved.",
    workspace_agent_id: str = _WORKSPACE_ID,
    chat_agent_id: str = _CHAT_ID,
) -> AgentMessageCard:
    return AgentMessageCard(
        chat_agent_id=chat_agent_id,
        chat_name="Migration chat",
        body=body,
        workspace_agent_id=workspace_agent_id,
        workspace_name="alpha",
        workspace_accent="#aabbcc",
    )


def _system_event(workspace_agent_id: str = _WORKSPACE_ID, title: str = "Backup setup failed") -> SystemEventCard:
    return SystemEventCard(
        title=title,
        body="Couldn't reach the backup host.",
        workspace_agent_id=workspace_agent_id,
        workspace_name="alpha" if workspace_agent_id else "me@example.com",
        workspace_accent="#aabbcc",
    )


def test_an_agent_message_enters_the_feed_unresolved_and_counts_toward_the_badge() -> None:
    feed = _make_feed()
    changes: list[int] = []
    feed.on_change = lambda: changes.append(1)

    entry = feed.append_agent_message(_agent_message(), sent_at=_at(5))
    message = feed.reconcile((), {})

    assert entry.kind == NotificationKind.AGENT_MESSAGE
    assert message.unresolved_count == 1
    (listed,) = message.entries
    assert listed.id == entry.id
    assert listed.title == "Migration chat"
    assert listed.body == "Finished the migration; 3 tables moved."
    assert listed.chat_agent_id == _CHAT_ID
    assert listed.workspace_agent_id == _WORKSPACE_ID
    assert listed.created_at == _ts(5)
    assert listed.outcome is None
    # The append is a change the publisher has to hear about on its own.
    assert changes == [1]


def test_navigating_to_a_workspace_reads_only_its_agent_messages() -> None:
    feed = _make_feed()
    other_workspace = "agent-" + "b" * 32
    feed.append_agent_message(_agent_message())
    kept = feed.append_agent_message(_agent_message(workspace_agent_id=other_workspace))
    kept_event = feed.append_system_event(_system_event())

    assert feed.mark_workspace_read(_WORKSPACE_ID) is True
    message = feed.reconcile((), {})

    assert {entry.id for entry in message.entries} == {kept.id, kept_event.id}
    assert feed.mark_workspace_read(_WORKSPACE_ID) is False
    assert feed.mark_workspace_read("") is False


def test_an_agent_message_whose_workspace_left_the_list_drops_out() -> None:
    feed = _make_feed()
    feed.append_agent_message(_agent_message())
    still_here = feed.append_agent_message(_agent_message(workspace_agent_id="agent-" + "b" * 32))
    unresolved = feed.append_agent_message(_agent_message(workspace_agent_id=""))
    system_event = feed.append_system_event(_system_event())

    message = feed.reconcile((), {}, known_workspace_agent_ids={"agent-" + "b" * 32})

    # Only agent messages follow their workspace out; the system event about
    # the gone workspace stays until cleared, and so does a message whose
    # workspace never resolved (it had none to leave).
    assert {entry.id for entry in message.entries} == {still_here.id, unresolved.id, system_event.id}


def test_a_system_event_enters_the_feed_and_clearing_it_removes_it() -> None:
    feed = _make_feed()

    entry = feed.append_system_event(_system_event(), happened_at=_at(3))
    listed = feed.reconcile((), {}).entries

    assert entry.kind == NotificationKind.SYSTEM_EVENT
    assert [e.id for e in listed] == [entry.id]
    assert listed[0].title == "Backup setup failed"
    assert listed[0].created_at == _ts(3)
    assert feed.clear(entry.id) is True
    assert feed.reconcile((), {}).entries == ()


def test_clearing_a_request_hides_it_for_good_while_the_request_stays_pending() -> None:
    feed = _make_feed()
    feed.reconcile((_card("evt-1"), _card("evt-2")), {})

    assert feed.clear("evt-1") is True
    message = feed.reconcile((_card("evt-1"), _card("evt-2")), {})

    # The reconcile keeps seeing the pending request, but the cleared entry
    # never comes back -- and it no longer counts.
    assert _entry_ids(message) == ["evt-2"]
    assert message.unresolved_count == 1
    assert feed.clear("evt-1") is False


def test_cleared_requests_stay_cleared_in_a_feed_built_after_a_restart(tmp_path: Path) -> None:
    cleared_path = tmp_path / "cleared.json"
    feed = _make_feed(cleared_request_ids_path=cleared_path)
    feed.reconcile((_card("evt-1"), _card("evt-2"), _card("evt-3")), {})
    feed.clear("evt-1")
    feed.clear_all()
    feed.reconcile((_card("evt-4"),), {})

    restarted = _make_feed(cleared_request_ids_path=cleared_path)
    message = restarted.reconcile((_card("evt-1"), _card("evt-2"), _card("evt-3"), _card("evt-4")), {})

    assert _entry_ids(message) == ["evt-4"]


def test_an_unreadable_cleared_requests_file_starts_the_feed_with_nothing_cleared(tmp_path: Path) -> None:
    cleared_path = tmp_path / "cleared.json"
    cleared_path.write_text("not json")

    message = _make_feed(cleared_request_ids_path=cleared_path).reconcile((_card("evt-1"),), {})

    assert _entry_ids(message) == ["evt-1"]


def test_clearing_an_agent_message_or_system_event_removes_it() -> None:
    feed = _make_feed()
    message_entry = feed.append_agent_message(_agent_message())
    event_entry = feed.append_system_event(_system_event())

    assert feed.clear(message_entry.id) is True
    assert feed.clear(event_entry.id) is True

    assert feed.reconcile((), {}).entries == ()


def test_clear_all_empties_the_feed_receipts_included() -> None:
    feed = _make_feed()
    feed.reconcile((_card("evt-1"), _card("evt-2")), {})
    feed.reconcile((_card("evt-2"),), {"evt-1": NotificationOutcome.APPROVED})
    feed.append_agent_message(_agent_message())
    feed.append_system_event(_system_event())

    assert feed.clear_all() is True
    message = feed.reconcile((_card("evt-2"),), {"evt-1": NotificationOutcome.APPROVED})

    assert message.entries == ()
    assert message.unresolved_count == 0
    assert feed.clear_all() is False


def test_unresolved_count_spans_every_kind() -> None:
    feed = _make_feed()
    feed.reconcile((_card("evt-1"),), {})
    feed.append_agent_message(_agent_message())
    feed.append_system_event(_system_event())

    message = feed.reconcile((_card("evt-1"),), {})

    assert message.unresolved_count == 3
    assert [entry.kind for entry in message.entries] == [
        NotificationKind.SYSTEM_EVENT,
        NotificationKind.AGENT_MESSAGE,
        NotificationKind.PERMISSION_REQUEST,
    ]


def test_eviction_across_kinds_drops_only_resolved_requests() -> None:
    """Agent messages and system events are unresolved until read, so the cap never evicts them."""
    feed = _make_feed()
    feed.reconcile((_card("evt-old"),), {})
    feed.reconcile((), {"evt-old": NotificationOutcome.APPROVED})
    for _ in range(50):
        feed.append_agent_message(_agent_message())

    message = feed.reconcile((), {})

    assert "evt-old" not in _entry_ids(message)
    assert len(message.entries) == 50


def test_an_agent_message_dispatches_the_chat_deep_link_with_the_workspace_as_title() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    entry = feed.append_agent_message(_agent_message())

    (request,) = dispatcher.dispatched
    assert request.title == "alpha"
    assert request.subtitle == "Migration chat"
    assert request.body == "Finished the migration; 3 tables moved."
    assert request.url == f"/workspace/{_WORKSPACE_ID}?chat={_CHAT_ID}"
    assert request.entry == entry


def test_native_banner_event_carries_the_feed_entry_for_the_shared_open_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed = _make_feed(dispatcher=NotificationDispatcher(is_electron=True))
    entry = feed.append_system_event(_system_event(workspace_agent_id=""))

    (event,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert event["entry"] == entry.model_dump(mode="json")
    assert event["url"] == "/accounts"


def test_a_workspace_system_event_dispatches_a_link_to_its_backups_page() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.append_system_event(_system_event())

    (request,) = dispatcher.dispatched
    assert request.subtitle == "Backup setup failed"
    assert request.url == f"/workspace/{_WORKSPACE_ID}/backups"


def test_an_account_level_system_event_titles_itself_with_the_account_and_links_to_accounts() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.append_system_event(_system_event(workspace_agent_id="", title="Backup cleanup finished"))

    (request,) = dispatcher.dispatched
    assert request.title == "me@example.com"
    assert request.url == "/accounts"


def test_an_agent_message_is_not_silenced_as_startup_backfill() -> None:
    """The backfill cutoff is a request rule: a message older than the feed is still news."""
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, constructed_at=_at(30))

    feed.append_agent_message(_agent_message(), sent_at=_at(0))

    assert len(dispatcher.dispatched) == 1


def test_an_agent_message_for_the_workspace_on_screen_in_a_focused_window_stays_silent() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, connected_workspace_agent_ids=(_WORKSPACE_ID,))

    feed.append_agent_message(_agent_message())
    feed.append_agent_message(_agent_message(workspace_agent_id="agent-" + "b" * 32))

    assert [request.url for request in dispatcher.dispatched] == [
        f"/workspace/{'agent-' + 'b' * 32}?chat={_CHAT_ID}",
    ]


def test_an_account_level_event_fires_whatever_windows_are_focused() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, connected_workspace_agent_ids=(_WORKSPACE_ID,))

    feed.append_system_event(_system_event(workspace_agent_id=""))

    assert len(dispatcher.dispatched) == 1


@pytest.mark.parametrize(
    ("is_enabled", "style", "is_dispatch_expected"),
    [
        (True, NotificationStyle.BOTH, True),
        (True, NotificationStyle.OS, True),
        (True, NotificationStyle.CARDS, False),
        (False, NotificationStyle.BOTH, False),
    ],
)
def test_appended_entries_obey_the_master_toggle_and_style(
    is_enabled: bool, style: NotificationStyle, is_dispatch_expected: bool
) -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher, is_enabled=is_enabled, style=style)

    feed.append_agent_message(_agent_message())
    feed.append_system_event(_system_event())

    assert (len(dispatcher.dispatched) == 2) is is_dispatch_expected


def test_feed_rejects_a_naive_constructed_at() -> None:
    # The NaiveTimestampError raised in model_post_init surfaces wrapped in
    # pydantic's ValidationError (it is a ValueError subclass).
    with pytest.raises(ValidationError, match="timezone-aware constructed_at"):
        _make_feed(constructed_at=datetime(2026, 8, 18, 11, 0, 0))
