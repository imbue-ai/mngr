"""Unit tests for event_watcher.py."""

import json
import subprocess
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from imbue.mng_claude_changeling.conftest import EventWatcherSubprocessCapture
from imbue.mng_claude_changeling.conftest import create_changeling_conversations_table_in_test_db
from imbue.mng_claude_changeling.conftest import write_changelings_settings_toml
from imbue.mng_claude_changeling.conftest import write_conversation_to_db
from imbue.mng_claude_changeling.data_types import WatcherSettings
from imbue.mng_claude_changeling.resources import event_watcher as event_watcher_module
from imbue.mng_claude_changeling.resources.event_watcher import DEFAULT_CEL_FILTER
from imbue.mng_claude_changeling.resources.event_watcher import _CHAT_PAIR_TIMEOUT_SECONDS
from imbue.mng_claude_changeling.resources.event_watcher import _DEFAULT_BURST_SIZE
from imbue.mng_claude_changeling.resources.event_watcher import _DEFAULT_HIGH_RATE_WARNING_THRESHOLD
from imbue.mng_claude_changeling.resources.event_watcher import _DEFAULT_MAX_DELIVERY_RETRIES
from imbue.mng_claude_changeling.resources.event_watcher import _DEFAULT_MAX_MESSAGES_PER_MINUTE
from imbue.mng_claude_changeling.resources.event_watcher import _DeliveryState
from imbue.mng_claude_changeling.resources.event_watcher import _EventWatcherSettings
from imbue.mng_claude_changeling.resources.event_watcher import _SendRateTracker
from imbue.mng_claude_changeling.resources.event_watcher import _TokenBucket
from imbue.mng_claude_changeling.resources.event_watcher import _compute_backoff_seconds
from imbue.mng_claude_changeling.resources.event_watcher import _compute_rate_warning
from imbue.mng_claude_changeling.resources.event_watcher import _deliver_batch
from imbue.mng_claude_changeling.resources.event_watcher import _filter_catchup_events
from imbue.mng_claude_changeling.resources.event_watcher import _format_delivery_message
from imbue.mng_claude_changeling.resources.event_watcher import _format_time_since_last
from imbue.mng_claude_changeling.resources.event_watcher import _get_system_notifications_conversation_id
from imbue.mng_claude_changeling.resources.event_watcher import _load_delivery_state
from imbue.mng_claude_changeling.resources.event_watcher import _load_watcher_settings
from imbue.mng_claude_changeling.resources.event_watcher import _save_delivery_state
from imbue.mng_claude_changeling.resources.event_watcher import _send_chat_notification
from imbue.mng_claude_changeling.resources.event_watcher import _send_message
from imbue.mng_claude_changeling.resources.event_watcher import _separate_chat_events
from imbue.mng_claude_changeling.resources.event_watcher import _should_skip_for_catchup
from imbue.mng_claude_changeling.resources.event_watcher import _write_notification_event

# -- Controllable clock for deterministic TokenBucket tests --


class _FakeClock:
    """Controllable time source for deterministic testing of _TokenBucket."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# -- Default sync verification --


def test_defaults_match_between_data_types_and_event_watcher() -> None:
    """Verify that event_watcher.py constants stay in sync with WatcherSettings defaults."""
    model_defaults = WatcherSettings()
    assert model_defaults.event_cel_filter == DEFAULT_CEL_FILTER
    assert model_defaults.event_burst_size == _DEFAULT_BURST_SIZE
    assert model_defaults.max_event_messages_per_minute == _DEFAULT_MAX_MESSAGES_PER_MINUTE
    assert model_defaults.high_rate_warning_threshold_per_minute == _DEFAULT_HIGH_RATE_WARNING_THRESHOLD
    assert model_defaults.max_delivery_retries == _DEFAULT_MAX_DELIVERY_RETRIES


# -- _load_watcher_settings tests --


def test_load_settings_defaults_when_no_file(tmp_path: Path) -> None:
    settings = _load_watcher_settings(tmp_path)
    assert settings.cel_filter == _EventWatcherSettings().cel_filter
    assert settings.burst_size == 5
    assert settings.max_messages_per_minute == 10
    assert settings.high_rate_warning_threshold == 8


def test_load_settings_reads_custom_values(tmp_path: Path) -> None:
    write_changelings_settings_toml(
        tmp_path,
        "[watchers]\n"
        'event_cel_filter = "source == \\"messages\\""\n'
        "event_burst_size = 3\n"
        "max_event_messages_per_minute = 20\n"
        "high_rate_warning_threshold_per_minute = 15\n",
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.cel_filter == 'source == "messages"'
    assert settings.burst_size == 3
    assert settings.max_messages_per_minute == 20
    assert settings.high_rate_warning_threshold == 15


def test_load_settings_handles_partial_config(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "[watchers]\nevent_burst_size = 7\n")
    settings = _load_watcher_settings(tmp_path)
    assert settings.burst_size == 7
    assert settings.max_messages_per_minute == 10
    assert settings.cel_filter == _EventWatcherSettings().cel_filter


def test_load_settings_handles_corrupt_file(tmp_path: Path) -> None:
    write_changelings_settings_toml(tmp_path, "this is not valid toml {{{")
    settings = _load_watcher_settings(tmp_path)
    assert settings.burst_size == 5


# -- _DeliveryState persistence tests --


def test_load_delivery_state_returns_defaults_when_missing(tmp_path: Path) -> None:
    state = _load_delivery_state(tmp_path / "nonexistent.json")
    assert state.last_event_id == ""
    assert state.last_timestamp == ""


def test_load_delivery_state_reads_valid_file(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_event_id": "evt-123", "last_timestamp": "2026-01-01T00:00:00Z"}))
    state = _load_delivery_state(state_file)
    assert state.last_event_id == "evt-123"
    assert state.last_timestamp == "2026-01-01T00:00:00Z"


def test_load_delivery_state_returns_defaults_on_corrupt_json(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("not valid json {{{")
    state = _load_delivery_state(state_file)
    assert state.last_event_id == ""
    assert state.last_timestamp == ""


def test_save_and_load_delivery_state_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    original = _DeliveryState(last_event_id="evt-abc", last_timestamp="2026-03-01T12:00:00Z")
    _save_delivery_state(state_file, original)
    loaded = _load_delivery_state(state_file)
    assert loaded.last_event_id == "evt-abc"
    assert loaded.last_timestamp == "2026-03-01T12:00:00Z"


def test_save_delivery_state_creates_parent_directories(tmp_path: Path) -> None:
    state_file = tmp_path / "nested" / "dir" / "state.json"
    state = _DeliveryState(last_event_id="evt-1", last_timestamp="2026-01-01T00:00:00Z")
    _save_delivery_state(state_file, state)
    assert state_file.exists()


# -- _TokenBucket tests (using injected clock for determinism) --


def test_token_bucket_allows_burst() -> None:
    clock = _FakeClock()
    bucket = _TokenBucket(burst_size=3, rate_per_second=0.0, time_source=clock)
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is False


def test_token_bucket_refills_over_time() -> None:
    clock = _FakeClock()
    bucket = _TokenBucket(burst_size=1, rate_per_second=10.0, time_source=clock)
    assert bucket.consume() is True
    assert bucket.consume() is False

    # Advance clock enough to refill one token (0.1s at 10/s = 1 token)
    clock.advance(0.1)
    assert bucket.consume() is True


def test_token_bucket_time_until_token_when_empty() -> None:
    clock = _FakeClock()
    bucket = _TokenBucket(burst_size=1, rate_per_second=10.0, time_source=clock)
    bucket.consume()
    wait = bucket.time_until_token()
    assert wait > 0
    assert wait <= 0.11


def test_token_bucket_time_until_token_when_available() -> None:
    clock = _FakeClock()
    bucket = _TokenBucket(burst_size=3, rate_per_second=1.0, time_source=clock)
    assert bucket.time_until_token() == 0.0


def test_token_bucket_does_not_exceed_burst_size() -> None:
    clock = _FakeClock()
    bucket = _TokenBucket(burst_size=2, rate_per_second=1000.0, time_source=clock)

    # Advance clock significantly
    clock.advance(10.0)

    # Even after lots of time, should not exceed burst_size
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is False


# -- _SendRateTracker tests --


def test_send_rate_tracker_initially_zero() -> None:
    tracker = _SendRateTracker()
    assert tracker.messages_per_minute() == 0.0


def test_send_rate_tracker_counts_sends() -> None:
    tracker = _SendRateTracker()
    tracker.record_send()
    tracker.record_send()
    tracker.record_send()
    assert tracker.messages_per_minute() == 3.0


def test_send_rate_tracker_prunes_old_entries() -> None:
    tracker = _SendRateTracker()

    # Record a send "in the past" by manipulating the internal list
    old_time = time.monotonic() - 120.0
    tracker._send_times.append(old_time)

    # Record a recent send
    tracker.record_send()

    # The old entry should be pruned
    assert tracker.messages_per_minute() == 1.0


# -- _should_skip_for_catchup tests --


def test_skip_catchup_returns_false_when_no_prior_state() -> None:
    state = _DeliveryState()
    assert _should_skip_for_catchup({"event_id": "evt-1", "timestamp": "2026-01-01T00:00:00Z"}, state) is False


def test_skip_catchup_returns_true_on_event_id_match() -> None:
    state = _DeliveryState(last_event_id="evt-match", last_timestamp="2026-01-01T00:00:00Z")
    assert _should_skip_for_catchup({"event_id": "evt-match", "timestamp": "2026-01-01T00:00:00Z"}, state) is True


def test_skip_catchup_returns_true_when_timestamp_before_last() -> None:
    state = _DeliveryState(last_event_id="evt-old", last_timestamp="2026-01-02T00:00:00Z")
    assert _should_skip_for_catchup({"event_id": "evt-other", "timestamp": "2026-01-01T00:00:00Z"}, state) is True


def test_skip_catchup_returns_false_when_timestamp_equals_last() -> None:
    """Same-timestamp events should NOT be skipped (at-least-once semantics)."""
    state = _DeliveryState(last_event_id="evt-old", last_timestamp="2026-01-01T00:00:00Z")
    assert _should_skip_for_catchup({"event_id": "evt-other", "timestamp": "2026-01-01T00:00:00Z"}, state) is False


def test_skip_catchup_returns_false_when_timestamp_after_last() -> None:
    state = _DeliveryState(last_event_id="evt-old", last_timestamp="2026-01-01T00:00:00Z")
    assert _should_skip_for_catchup({"event_id": "evt-new", "timestamp": "2026-01-02T00:00:00Z"}, state) is False


# -- _filter_catchup_events tests --


def test_filter_catchup_events_skips_old_events() -> None:
    state = _DeliveryState(last_event_id="evt-1", last_timestamp="2026-01-01T00:00:00Z")
    pending = [
        json.dumps({"event_id": "evt-1", "timestamp": "2026-01-01T00:00:00Z"}),
        json.dumps({"event_id": "evt-2", "timestamp": "2026-01-02T00:00:00Z"}),
    ]
    deliverable, last_parsed, is_catching_up = _filter_catchup_events(pending, state, is_catching_up=True)
    assert len(deliverable) == 1
    assert last_parsed["event_id"] == "evt-2"
    assert is_catching_up is False


def test_filter_catchup_events_passes_all_when_not_catching_up() -> None:
    state = _DeliveryState()
    pending = [
        json.dumps({"event_id": "evt-1", "timestamp": "2026-01-01T00:00:00Z"}),
        json.dumps({"event_id": "evt-2", "timestamp": "2026-01-02T00:00:00Z"}),
    ]
    deliverable, last_parsed, is_catching_up = _filter_catchup_events(pending, state, is_catching_up=False)
    assert len(deliverable) == 2
    assert is_catching_up is False


def test_filter_catchup_events_skips_malformed_lines() -> None:
    state = _DeliveryState()
    pending = [
        "not json at all",
        json.dumps({"event_id": "evt-1", "timestamp": "2026-01-01T00:00:00Z"}),
    ]
    deliverable, last_parsed, is_catching_up = _filter_catchup_events(pending, state, is_catching_up=False)
    assert len(deliverable) == 1
    assert last_parsed["event_id"] == "evt-1"


# -- _compute_rate_warning tests --


def test_compute_rate_warning_returns_none_below_threshold() -> None:
    tracker = _SendRateTracker()
    assert _compute_rate_warning(tracker, threshold=10) is None


def test_compute_rate_warning_returns_warning_above_threshold() -> None:
    tracker = _SendRateTracker()
    for _ in range(12):
        tracker.record_send()
    warning = _compute_rate_warning(tracker, threshold=10)
    assert warning is not None
    assert "High event rate" in warning
    assert "12" in warning


# -- _format_time_since_last tests --


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (30.0, "30s"),
        (120.0, "2.0m"),
        (7200.0, "2.0h"),
    ],
)
def test_format_time_since_last(seconds: float, expected: str) -> None:
    assert _format_time_since_last(seconds) == expected


# -- _format_delivery_message tests --


def test_format_delivery_message_basic() -> None:
    result = _format_delivery_message(
        event_lines=['{"event": 1}', '{"event": 2}'],
        time_since_last_message_seconds=None,
        rate_warning=None,
    )
    assert "[Event watcher] 2 new event(s)" in result
    assert '{"event": 1}' in result
    assert '{"event": 2}' in result


def test_format_delivery_message_with_time_since_last() -> None:
    result = _format_delivery_message(
        event_lines=['{"event": 1}'],
        time_since_last_message_seconds=45.0,
        rate_warning=None,
    )
    assert "Time since last message: 45s" in result


def test_format_delivery_message_with_time_since_last_minutes() -> None:
    result = _format_delivery_message(
        event_lines=['{"event": 1}'],
        time_since_last_message_seconds=180.0,
        rate_warning=None,
    )
    assert "Time since last message: 3.0m" in result


def test_format_delivery_message_with_rate_warning() -> None:
    result = _format_delivery_message(
        event_lines=['{"event": 1}'],
        time_since_last_message_seconds=None,
        rate_warning="High event rate: 15 messages/min (threshold: 8/min)",
    )
    assert "WARNING:" in result
    assert "High event rate" in result


# -- _send_message tests --


def test_send_message_returns_true_on_success(mock_subprocess_success: EventWatcherSubprocessCapture) -> None:
    assert _send_message("my-agent", "hello") is True
    assert len(mock_subprocess_success.calls) == 1
    cmd = mock_subprocess_success.calls[0][0]
    assert any("mng" in c for c in cmd)
    assert "message" in cmd
    assert "my-agent" in cmd


def test_send_message_returns_false_on_failure(mock_subprocess_failure: EventWatcherSubprocessCapture) -> None:
    assert _send_message("my-agent", "hello") is False


def test_send_message_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout_run(cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    mock_sp = types.SimpleNamespace(run=timeout_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    assert _send_message("my-agent", "hello") is False


def test_send_message_returns_false_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def os_error_run(cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        raise OSError("subprocess launch failed")

    mock_sp = types.SimpleNamespace(run=os_error_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    assert _send_message("my-agent", "hello") is False


# -- _deliver_batch tests --


def test_deliver_batch_updates_state_on_success(
    tmp_path: Path,
    mock_subprocess_success: EventWatcherSubprocessCapture,
) -> None:
    state_file = tmp_path / "state.json"
    delivery_state = _DeliveryState(last_delivery_monotonic=time.monotonic() - 10.0)
    rate_tracker = _SendRateTracker()
    event_buffer: list[str] = []
    buffer_lock = threading.Lock()

    event_line = json.dumps({"event_id": "evt-42", "timestamp": "2026-03-01T12:00:00Z"})
    last_parsed = json.loads(event_line)

    success = _deliver_batch(
        deliverable_lines=[event_line],
        last_parsed=last_parsed,
        agent_name="test-agent",
        delivery_state=delivery_state,
        state_file=state_file,
        rate_tracker=rate_tracker,
        event_buffer=event_buffer,
        buffer_lock=buffer_lock,
        time_since_last=10.0,
        rate_warning=None,
    )

    assert success is True

    # Verify state was updated
    assert delivery_state.last_event_id == "evt-42"
    assert delivery_state.last_timestamp == "2026-03-01T12:00:00Z"
    assert delivery_state.last_delivery_monotonic > 0

    # Verify state was persisted
    loaded = _load_delivery_state(state_file)
    assert loaded.last_event_id == "evt-42"

    # Verify rate tracker recorded the send
    assert rate_tracker.messages_per_minute() == 1.0

    # Verify mng message was called
    assert len(mock_subprocess_success.calls) == 1


def test_deliver_batch_puts_events_back_on_failure(
    tmp_path: Path,
    mock_subprocess_failure: EventWatcherSubprocessCapture,
) -> None:
    state_file = tmp_path / "state.json"
    delivery_state = _DeliveryState()
    rate_tracker = _SendRateTracker()
    event_buffer: list[str] = []
    buffer_lock = threading.Lock()

    event_lines = ['{"event_id": "evt-1"}', '{"event_id": "evt-2"}']

    success = _deliver_batch(
        deliverable_lines=event_lines,
        last_parsed={"event_id": "evt-2"},
        agent_name="test-agent",
        delivery_state=delivery_state,
        state_file=state_file,
        rate_tracker=rate_tracker,
        event_buffer=event_buffer,
        buffer_lock=buffer_lock,
        time_since_last=None,
        rate_warning=None,
    )

    assert success is False

    # Verify events were put back in buffer (at the front)
    assert event_buffer == event_lines

    # Verify state was NOT updated
    assert delivery_state.last_event_id == ""

    # Verify rate tracker did NOT record a send
    assert rate_tracker.messages_per_minute() == 0.0

    # Verify state file was NOT created
    assert not state_file.exists()


# -- _write_notification_event tests --


def test_write_notification_event_creates_file(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    _write_notification_event(events_dir, "Test notification", level="WARNING")

    events_file = events_dir / "delivery_failures" / "events.jsonl"
    assert events_file.exists()

    lines = events_file.read_text().strip().split("\n")
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "delivery_notification"
    assert event["source"] == "delivery_failures"
    assert event["level"] == "WARNING"
    assert event["message"] == "Test notification"
    assert "event_id" in event
    assert "timestamp" in event


# -- _get_system_notifications_conversation_id tests --


def test_get_system_notifications_conversation_id_returns_tagged_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_data_dir = tmp_path / "llm_data"
    db_path = llm_data_dir / "logs.db"
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    create_changeling_conversations_table_in_test_db(db_path)
    write_conversation_to_db(db_path, "sys-notif-123", model="echo", tags='{"internal":"system_notifications"}')
    write_conversation_to_db(db_path, "other-conv", model="claude-opus-4.6")

    assert _get_system_notifications_conversation_id() == "sys-notif-123"


def test_get_system_notifications_conversation_id_returns_none_when_no_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_USER_PATH", str(tmp_path / "nonexistent_llm"))
    assert _get_system_notifications_conversation_id() is None


def test_get_system_notifications_conversation_id_returns_none_when_no_tagged_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_data_dir = tmp_path / "llm_data"
    db_path = llm_data_dir / "logs.db"
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    create_changeling_conversations_table_in_test_db(db_path)

    assert _get_system_notifications_conversation_id() is None


# -- _send_chat_notification tests --


def _setup_conversations_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conversation_id: str = "sys-notif-test",
) -> Path:
    """Create a llm DB with a system_notifications conversation and return events_dir."""
    llm_data_dir = tmp_path / "llm_data"
    db_path = llm_data_dir / "logs.db"
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    create_changeling_conversations_table_in_test_db(db_path)
    write_conversation_to_db(db_path, conversation_id, model="echo", tags='{"internal":"system_notifications"}')

    events_dir = tmp_path / "events"
    return events_dir


def test_send_chat_notification_returns_true_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_success: EventWatcherSubprocessCapture,
) -> None:
    """_send_chat_notification returns True when llm succeeds."""
    events_dir = _setup_conversations_db(tmp_path, monkeypatch)
    assert _send_chat_notification(events_dir, "test message") is True
    assert len(mock_subprocess_success.calls) == 1
    cmd = mock_subprocess_success.calls[0][0]
    assert "llm" in cmd
    assert "chat" in cmd
    assert "sys-notif-test" in cmd


def test_send_chat_notification_returns_false_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_failure: EventWatcherSubprocessCapture,
) -> None:
    """_send_chat_notification returns False when llm fails."""
    events_dir = _setup_conversations_db(tmp_path, monkeypatch)
    assert _send_chat_notification(events_dir, "test message") is False


def test_send_chat_notification_returns_false_when_no_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_success: EventWatcherSubprocessCapture,
) -> None:
    """_send_chat_notification returns False when no DB exists."""
    monkeypatch.setenv("LLM_USER_PATH", str(tmp_path / "nonexistent_llm"))
    events_dir = tmp_path / "events"
    assert _send_chat_notification(events_dir, "test message") is False
    assert len(mock_subprocess_success.calls) == 0


# -- _compute_backoff_seconds tests --


def test_compute_backoff_seconds_exponential_growth() -> None:
    assert _compute_backoff_seconds(1) == 2.0
    assert _compute_backoff_seconds(2) == 4.0
    assert _compute_backoff_seconds(3) == 8.0
    assert _compute_backoff_seconds(4) == 16.0


def test_compute_backoff_seconds_caps_at_max() -> None:
    # With base=2 and max=60, 2 * 2^(n-1) caps at 60 for n >= 6 (2*32=64 > 60)
    assert _compute_backoff_seconds(10) == 60.0


# -- _separate_chat_events tests --


def _make_message_event(role: str, conversation_id: str = "conv-1", event_id: str = "evt-1") -> str:
    return json.dumps(
        {
            "source": "messages",
            "role": role,
            "conversation_id": conversation_id,
            "event_id": event_id,
            "timestamp": "2026-03-01T12:00:00Z",
        }
    )


def _make_non_message_event(source: str = "scheduled", event_id: str = "evt-s1") -> str:
    return json.dumps(
        {
            "source": source,
            "type": "trigger",
            "event_id": event_id,
            "timestamp": "2026-03-01T12:00:00Z",
        }
    )


def test_separate_chat_events_passes_non_message_events() -> None:
    """Non-message events pass through unchanged."""
    held: dict[str, tuple[list[str], float]] = {}
    lines = [_make_non_message_event()]
    result = _separate_chat_events(lines, held)
    assert len(result) == 1
    assert len(held) == 0


def test_separate_chat_events_holds_user_message_without_assistant() -> None:
    """User messages are held when no assistant response is present."""
    held: dict[str, tuple[list[str], float]] = {}
    lines = [_make_message_event("user")]
    result = _separate_chat_events(lines, held)
    assert len(result) == 0
    assert "conv-1" in held
    assert len(held["conv-1"][0]) == 1


def test_separate_chat_events_releases_pair() -> None:
    """User + assistant for the same conversation are both delivered, user first."""
    held: dict[str, tuple[list[str], float]] = {}
    lines = [
        _make_message_event("user", event_id="evt-u1"),
        _make_message_event("assistant", event_id="evt-a1"),
    ]
    result = _separate_chat_events(lines, held)
    assert len(result) == 2
    assert len(held) == 0
    # User message must come before assistant message
    assert json.loads(result[0])["role"] == "user"
    assert json.loads(result[1])["role"] == "assistant"


def test_separate_chat_events_releases_previously_held_on_assistant() -> None:
    """Previously held user messages are released when assistant arrives, user first."""
    user_line = _make_message_event("user", event_id="evt-u1")
    held: dict[str, tuple[list[str], float]] = {"conv-1": ([user_line], time.monotonic())}
    lines = [_make_message_event("assistant", event_id="evt-a1")]
    result = _separate_chat_events(lines, held)
    # Both the previously held user message and the assistant message should be delivered
    assert len(result) == 2
    assert "conv-1" not in held
    # User message must come before assistant message
    assert json.loads(result[0])["role"] == "user"
    assert json.loads(result[1])["role"] == "assistant"


def test_separate_chat_events_timeout_releases_held() -> None:
    """User messages held past the timeout are released even without assistant."""
    user_line = _make_message_event("user", event_id="evt-u1")
    old_time = time.monotonic() - _CHAT_PAIR_TIMEOUT_SECONDS - 1.0
    held: dict[str, tuple[list[str], float]] = {"conv-1": ([user_line], old_time)}
    # Empty batch, but timeout should release the held message
    result = _separate_chat_events([], held)
    assert len(result) == 1
    assert "conv-1" not in held


def test_separate_chat_events_different_conversations() -> None:
    """Messages from different conversations are handled independently."""
    held: dict[str, tuple[list[str], float]] = {}
    lines = [
        _make_message_event("user", conversation_id="conv-1", event_id="evt-u1"),
        _make_message_event("user", conversation_id="conv-2", event_id="evt-u2"),
        _make_message_event("assistant", conversation_id="conv-1", event_id="evt-a1"),
    ]
    result = _separate_chat_events(lines, held)
    # conv-1 pair should be delivered, conv-2 user should be held
    assert len(result) == 2
    assert "conv-1" not in held
    assert "conv-2" in held


def test_separate_chat_events_mixed_with_non_message() -> None:
    """Non-message events are delivered even when chat messages are held."""
    held: dict[str, tuple[list[str], float]] = {}
    lines = [
        _make_message_event("user", event_id="evt-u1"),
        _make_non_message_event(),
    ]
    result = _separate_chat_events(lines, held)
    assert len(result) == 1  # Only the non-message event
    parsed = json.loads(result[0])
    assert parsed["source"] == "scheduled"
    assert "conv-1" in held


def test_separate_chat_events_assistant_only_passes_through() -> None:
    """Assistant messages without a corresponding user message are delivered immediately."""
    held: dict[str, tuple[list[str], float]] = {}
    lines = [_make_message_event("assistant", event_id="evt-a1")]
    result = _separate_chat_events(lines, held)
    assert len(result) == 1
    assert json.loads(result[0])["role"] == "assistant"
    assert len(held) == 0


def test_separate_chat_events_malformed_json_passes_through() -> None:
    """Malformed JSON lines pass through unchanged."""
    held: dict[str, tuple[list[str], float]] = {}
    lines = ["not json at all"]
    result = _separate_chat_events(lines, held)
    assert len(result) == 1
    assert result[0] == "not json at all"
