"""Tests for the notification feed's reconcile semantics and OS dispatch gating."""

from datetime import datetime
from datetime import timezone

import pytest
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification_feed import NotificationDispatchPreferences
from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.notification_feed import PendingNotificationCard
from imbue.minds.desktop_client.ui_models import NotificationOutcome
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage


class _RecordingDispatcher(NotificationDispatcher):
    """Dispatcher double that records dispatch calls instead of hitting any OS channel."""

    _dispatched: list[tuple[NotificationRequest, str]] = PrivateAttr(default_factory=list)

    def dispatch(self, request: NotificationRequest, agent_display_name: str) -> None:
        self._dispatched.append((request, agent_display_name))

    @property
    def dispatched(self) -> list[tuple[NotificationRequest, str]]:
        return self._dispatched


def _make_recording_dispatcher(is_electron: bool = True) -> _RecordingDispatcher:
    # Electron by default: the feed only OS-dispatches on the Electron channel
    # (in browser mode the renderer owns OS delivery).
    return _RecordingDispatcher(is_electron=is_electron, is_macos=False)


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
) -> NotificationFeed:
    preferences = NotificationDispatchPreferences(is_enabled=is_enabled, style=style)
    return NotificationFeed(
        notification_dispatcher=dispatcher,
        get_dispatch_preferences=lambda: preferences,
        get_connected_focused_workspace_agent_ids=lambda: connected_workspace_agent_ids,
        constructed_at=constructed_at,
    )


def _card(
    request_id: str,
    requested_at: str = _ts(0),
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

    ((request, _),) = dispatcher.dispatched
    assert request.title == "alpha asks — Slack"


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


def test_a_non_electron_dispatcher_never_receives_feed_dispatches() -> None:
    """In browser mode the renderer owns OS delivery; the server side must stay silent."""
    dispatcher = _make_recording_dispatcher(is_electron=False)
    feed = _make_feed(dispatcher=dispatcher, style=NotificationStyle.BOTH)

    message = feed.reconcile((_card("evt-1"),), {})

    assert message.unresolved_count == 1
    assert dispatcher.dispatched == []


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


def test_dispatched_request_carries_the_em_dash_title_and_review_deep_link() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)
    agent_id = "agent-" + "a" * 32

    feed.reconcile((_card("evt-1"),), {})

    ((request, agent_display_name),) = dispatcher.dispatched
    assert request.title == "alpha asks — Gmail"
    assert request.message == "Needs to read your inbox to triage email."
    assert request.url == f"/workspace/{agent_id}?review=evt-1"
    assert agent_display_name == "alpha"


def test_dispatched_request_falls_back_to_a_stock_body_when_the_rationale_is_empty() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.reconcile((_card("evt-1", body=""),), {})

    ((request, _),) = dispatcher.dispatched
    assert request.message == "Waiting on your review."


def test_dispatched_request_omits_the_deep_link_when_the_workspace_is_unresolvable() -> None:
    dispatcher = _make_recording_dispatcher()
    feed = _make_feed(dispatcher=dispatcher)

    feed.reconcile((_card("evt-1", workspace_agent_id=""),), {})

    ((request, _),) = dispatcher.dispatched
    assert request.url is None


def test_feed_rejects_a_naive_constructed_at() -> None:
    # The NaiveTimestampError raised in model_post_init surfaces wrapped in
    # pydantic's ValidationError (it is a ValueError subclass).
    with pytest.raises(ValidationError, match="timezone-aware constructed_at"):
        _make_feed(constructed_at=datetime(2026, 8, 18, 11, 0, 0))
