"""Unit tests for event_watcher.py."""

import io
import json
import os
import subprocess
import threading
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imbue.mng_llm.conftest import create_mind_conversations_table_in_test_db
from imbue.mng_llm.conftest import write_conversation_to_db
from imbue.mng_llm.conftest import write_minds_settings_toml
from imbue.mng_mind import event_watcher as event_watcher_module
from imbue.mng_mind.conftest import EventWatcherSubprocessCapture
from imbue.mng_mind.conftest import FakeWaitProcess
from imbue.mng_mind.conftest import SyntheticLoopEnv
from imbue.mng_mind.conftest import TrackingIdleWait
from imbue.mng_mind.conftest import _create_fake_wait_process
from imbue.mng_mind.conftest import _create_synthetic_loop_env
from imbue.mng_mind.conftest import create_executable_command
from imbue.mng_mind.conftest import create_tracking_idle_wait
from imbue.mng_mind.conftest import make_pending_idle_wait
from imbue.mng_mind.data_types import WatcherSettings
from imbue.mng_mind.event_watcher import DEFAULT_CEL_FILTER
from imbue.mng_mind.event_watcher import InvalidTimeFormatError
from imbue.mng_mind.event_watcher import _CHAT_PAIR_TIMEOUT_SECONDS
from imbue.mng_mind.event_watcher import _DEFAULT_BURST_SIZE
from imbue.mng_mind.event_watcher import _DEFAULT_MAX_DELIVERY_RETRIES
from imbue.mng_mind.event_watcher import _DEFAULT_MAX_EVENT_LENGTH
from imbue.mng_mind.event_watcher import _DEFAULT_MAX_MESSAGES_PER_MINUTE
from imbue.mng_mind.event_watcher import _DEFAULT_MAX_SAME_SOURCE_EVENTS_PER_BATCH
from imbue.mng_mind.event_watcher import _DeliveryState
from imbue.mng_mind.event_watcher import _EventWatcherSettings
from imbue.mng_mind.event_watcher import _IgnoredSourcesState
from imbue.mng_mind.event_watcher import _ONBOARDING_MARKER_FILENAME
from imbue.mng_mind.event_watcher import _SCHEDULED_STATE_FILENAME
from imbue.mng_mind.event_watcher import _SendRateTracker
from imbue.mng_mind.event_watcher import _TokenBucket
from imbue.mng_mind.event_watcher import _apply_event_batch_filter
from imbue.mng_mind.event_watcher import _apply_special_event_handling
from imbue.mng_mind.event_watcher import _compute_backoff_seconds
from imbue.mng_mind.event_watcher import _deliver_batch
from imbue.mng_mind.event_watcher import _filter_catchup_events
from imbue.mng_mind.event_watcher import _filter_ignored_sources
from imbue.mng_mind.event_watcher import _get_system_notifications_conversation_id
from imbue.mng_mind.event_watcher import _load_delivery_state
from imbue.mng_mind.event_watcher import _load_ignored_sources_if_updated
from imbue.mng_mind.event_watcher import _load_scheduled_events_state
from imbue.mng_mind.event_watcher import _load_watcher_settings
from imbue.mng_mind.event_watcher import _make_synthetic_event_line
from imbue.mng_mind.event_watcher import _parse_time_of_day
from imbue.mng_mind.event_watcher import _per_event_idle_delay_minutes
from imbue.mng_mind.event_watcher import _resolve_user_timezone
from imbue.mng_mind.event_watcher import _run_delivery_loop
from imbue.mng_mind.event_watcher import _run_event_batch_filter_command
from imbue.mng_mind.event_watcher import _run_synthetic_events_loop
from imbue.mng_mind.event_watcher import _save_delivery_state
from imbue.mng_mind.event_watcher import _save_scheduled_events_state
from imbue.mng_mind.event_watcher import _send_chat_notification
from imbue.mng_mind.event_watcher import _send_message
from imbue.mng_mind.event_watcher import _separate_chat_events
from imbue.mng_mind.event_watcher import _should_skip_for_catchup
from imbue.mng_mind.event_watcher import _write_events_file
from imbue.mng_mind.event_watcher import _write_notification_event
from imbue.mng_mind.event_watcher import main

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
    watcher_defaults = _EventWatcherSettings()
    assert model_defaults.event_cel_filter == DEFAULT_CEL_FILTER
    assert model_defaults.event_exclude_sources == watcher_defaults.event_exclude_sources
    assert model_defaults.event_burst_size == _DEFAULT_BURST_SIZE
    assert model_defaults.max_event_messages_per_minute == _DEFAULT_MAX_MESSAGES_PER_MINUTE
    assert model_defaults.max_delivery_retries == _DEFAULT_MAX_DELIVERY_RETRIES
    assert model_defaults.max_event_length == _DEFAULT_MAX_EVENT_LENGTH
    assert model_defaults.max_same_source_events_per_batch == _DEFAULT_MAX_SAME_SOURCE_EVENTS_PER_BATCH
    assert model_defaults.idle_event_delay_minutes_schedule == watcher_defaults.idle_event_delay_minutes_schedule
    assert model_defaults.scheduled_events == dict(watcher_defaults.scheduled_events)
    assert model_defaults.user_timezone == watcher_defaults.user_timezone
    assert model_defaults.event_batch_filter_command == watcher_defaults.event_batch_filter_command


# -- _load_watcher_settings tests --


def test_load_settings_defaults_when_no_file(tmp_path: Path) -> None:
    settings = _load_watcher_settings(tmp_path)
    assert settings.cel_filter == _EventWatcherSettings().cel_filter
    assert settings.burst_size == 5
    assert settings.max_messages_per_minute == 10


def test_load_settings_reads_custom_values(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        "[watchers]\n"
        'event_cel_filter = "source == \\"messages\\""\n'
        "event_burst_size = 3\n"
        "max_event_messages_per_minute = 20\n",
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.cel_filter == 'source == "messages"'
    assert settings.burst_size == 3
    assert settings.max_messages_per_minute == 20


def test_load_settings_handles_partial_config(tmp_path: Path) -> None:
    write_minds_settings_toml(tmp_path, "[watchers]\nevent_burst_size = 7\n")
    settings = _load_watcher_settings(tmp_path)
    assert settings.burst_size == 7
    assert settings.max_messages_per_minute == 10
    assert settings.cel_filter == _EventWatcherSettings().cel_filter


def test_load_settings_handles_corrupt_file(tmp_path: Path) -> None:
    write_minds_settings_toml(tmp_path, "this is not valid toml {{{")
    settings = _load_watcher_settings(tmp_path)
    assert settings.burst_size == 5


def test_load_settings_reads_aggregation_settings(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        "[watchers]\nmax_event_length = 10000\nmax_same_source_events_per_batch = 5\n",
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.max_event_length == 10000
    assert settings.max_same_source_events_per_batch == 5


def test_load_settings_reads_event_exclude_sources(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        '[watchers]\nevent_exclude_sources = ["claude/common_transcript", "other/source"]\n',
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.event_exclude_sources == ("claude/common_transcript", "other/source")


def test_load_settings_defaults_event_exclude_sources_to_empty(tmp_path: Path) -> None:
    settings = _load_watcher_settings(tmp_path)
    assert settings.event_exclude_sources == ()


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


def test_save_delivery_state_handles_write_error(tmp_path: Path) -> None:
    """_save_delivery_state logs error but does not raise on write failure."""
    # Use /dev/null as parent (not writable as a directory)
    state_file = Path("/dev/null/state.json")
    state = _DeliveryState(last_event_id="evt-1", last_timestamp="2026-01-01T00:00:00Z")
    # Should not raise
    _save_delivery_state(state_file, state)


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


def test_token_bucket_time_until_token_infinite_when_zero_rate() -> None:
    clock = _FakeClock()
    bucket = _TokenBucket(burst_size=1, rate_per_second=0.0, time_source=clock)
    bucket.consume()
    assert bucket.time_until_token() == float("inf")


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


# -- _send_message tests --


def test_send_message_returns_true_on_success(mock_subprocess_success: EventWatcherSubprocessCapture) -> None:
    assert _send_message("agent-00000000000000000000000000000001", "hello") is True
    assert len(mock_subprocess_success.calls) == 1
    cmd = mock_subprocess_success.calls[0][0]
    assert any("mng" in c for c in cmd)
    assert "message" in cmd
    assert "agent-00000000000000000000000000000001" in cmd


def test_send_message_returns_false_on_failure(mock_subprocess_failure: EventWatcherSubprocessCapture) -> None:
    assert _send_message("agent-00000000000000000000000000000001", "hello") is False


def test_send_message_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout_run(cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    mock_sp = types.SimpleNamespace(run=timeout_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    assert _send_message("agent-00000000000000000000000000000001", "hello") is False


def test_send_message_returns_false_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def os_error_run(cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        raise OSError("subprocess launch failed")

    mock_sp = types.SimpleNamespace(run=os_error_run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    assert _send_message("agent-00000000000000000000000000000001", "hello") is False


# -- _write_events_file tests --


def test_write_events_file_creates_file_with_jsonl_content(tmp_path: Path) -> None:
    event_lines = [
        '{"event_id":"evt-1","timestamp":"2026-03-01T00:00:00Z"}',
        '{"event_id":"evt-2","timestamp":"2026-03-01T00:01:00Z"}',
    ]
    file_path = _write_events_file(event_lines, directory=tmp_path)
    assert file_path is not None
    assert str(file_path).startswith(str(tmp_path))
    assert str(file_path).endswith(".jsonl")

    content = file_path.read_text()
    lines = content.strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == "evt-1"
    assert json.loads(lines[1])["event_id"] == "evt-2"


def test_write_events_file_returns_none_on_write_failure() -> None:
    result = _write_events_file(['{"event_id":"evt-1"}'], directory=Path("/nonexistent_dir_xyz"))
    assert result is None


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
    event_batches_dir = tmp_path / "mind" / "event_batches"
    event_batches_dir.mkdir(parents=True)

    event_line = json.dumps({"event_id": "evt-42", "timestamp": "2026-03-01T12:00:00Z"})
    last_parsed = json.loads(event_line)

    success = _deliver_batch(
        deliverable_lines=[event_line],
        last_parsed=last_parsed,
        agent_id="test-agent",
        delivery_state=delivery_state,
        state_file=state_file,
        rate_tracker=rate_tracker,
        event_buffer=event_buffer,
        buffer_lock=buffer_lock,
        event_batches_dir=event_batches_dir,
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

    # Verify mng message was called with a file path reference
    assert len(mock_subprocess_success.calls) == 1
    cmd = mock_subprocess_success.calls[0][0]
    message_arg = cmd[cmd.index("-m") + 1]
    assert "Please process all events in " in message_arg
    assert str(event_batches_dir) in message_arg
    assert message_arg.endswith(".jsonl")


def test_deliver_batch_puts_events_back_on_failure(
    tmp_path: Path,
    mock_subprocess_failure: EventWatcherSubprocessCapture,
) -> None:
    state_file = tmp_path / "state.json"
    delivery_state = _DeliveryState()
    rate_tracker = _SendRateTracker()
    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    event_batches_dir = tmp_path / "mind" / "event_batches"
    event_batches_dir.mkdir(parents=True)

    event_lines = ['{"event_id": "evt-1"}', '{"event_id": "evt-2"}']

    success = _deliver_batch(
        deliverable_lines=event_lines,
        last_parsed={"event_id": "evt-2"},
        agent_id="test-agent",
        delivery_state=delivery_state,
        state_file=state_file,
        rate_tracker=rate_tracker,
        event_buffer=event_buffer,
        buffer_lock=buffer_lock,
        event_batches_dir=event_batches_dir,
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

    # Verify the orphaned events file was cleaned up
    cmd = mock_subprocess_failure.calls[0][0]
    message_arg = cmd[cmd.index("-m") + 1]
    assert "Please process all events in " in message_arg
    events_file = Path(message_arg.replace("Please process all events in ", ""))
    assert not events_file.exists(), "Orphaned events file should be cleaned up on delivery failure"


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


def test_write_notification_event_handles_write_error(tmp_path: Path) -> None:
    """_write_notification_event should not raise when the events file open fails."""
    events_dir = tmp_path / "events"
    # Create the directory but make events.jsonl a directory so open() fails
    delivery_dir = events_dir / "delivery_failures"
    delivery_dir.mkdir(parents=True)
    # Make events.jsonl a directory so that open() raises IsADirectoryError
    (delivery_dir / "events.jsonl").mkdir()
    _write_notification_event(events_dir, "Test notification")


# -- _get_system_notifications_conversation_id tests --


def test_get_system_notifications_conversation_id_returns_tagged_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_data_dir = tmp_path / "llm_data"
    db_path = llm_data_dir / "logs.db"
    monkeypatch.setenv("LLM_USER_PATH", str(llm_data_dir))

    create_mind_conversations_table_in_test_db(db_path)
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

    create_mind_conversations_table_in_test_db(db_path)

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

    create_mind_conversations_table_in_test_db(db_path)
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
    assert "prompt" in cmd
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


# -- _apply_special_event_handling tests --


def test_apply_special_event_handling_passes_through_when_no_limits_exceeded(tmp_path: Path) -> None:
    """Events pass through unchanged when no aggregation thresholds are exceeded."""
    event_lists_dir = tmp_path / "event_lists"
    event_lists_dir.mkdir()
    lines = [
        json.dumps({"event_id": "evt-1", "timestamp": "2026-03-01T12:00:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-2", "timestamp": "2026-03-01T12:01:00Z", "source": "messages"}),
    ]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=100_000, max_same_source_events_per_batch=100
    )
    assert result == lines


def test_apply_special_event_handling_aggregates_when_source_exceeds_count(tmp_path: Path) -> None:
    """Events from a source are aggregated when count exceeds max_same_source_events_per_batch."""
    event_lists_dir = tmp_path / "event_lists"
    event_lists_dir.mkdir()
    lines = [
        json.dumps({"event_id": "evt-1", "timestamp": "2026-03-01T12:00:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-2", "timestamp": "2026-03-01T12:01:00Z", "source": "messages"}),
        json.dumps({"event_id": "evt-3", "timestamp": "2026-03-01T12:02:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-4", "timestamp": "2026-03-01T12:03:00Z", "source": "scheduled"}),
    ]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=100_000, max_same_source_events_per_batch=2
    )

    assert len(result) == 2

    # First event should be the aggregate (replacing the first scheduled event position)
    aggregate = json.loads(result[0])
    assert aggregate["type"] == "aggregate"
    assert aggregate["source"] == "scheduled"
    assert len(aggregate["aggregate_events"]) == 1

    # Second event should be the messages event (passed through)
    messages_event = json.loads(result[1])
    assert messages_event["event_id"] == "evt-2"

    # Verify the aggregate file contains all 3 scheduled events
    aggregate_file = Path(aggregate["aggregate_events"][0])
    assert aggregate_file.exists()
    file_lines = aggregate_file.read_text().strip().split("\n")
    assert len(file_lines) == 3
    assert json.loads(file_lines[0])["event_id"] == "evt-1"
    assert json.loads(file_lines[1])["event_id"] == "evt-3"
    assert json.loads(file_lines[2])["event_id"] == "evt-4"


def test_apply_special_event_handling_aggregates_when_event_exceeds_length(tmp_path: Path) -> None:
    """All events from a source are aggregated when any single event exceeds max_event_length."""
    event_lists_dir = tmp_path / "event_lists"
    event_lists_dir.mkdir()

    short_event = json.dumps({"event_id": "evt-1", "timestamp": "2026-03-01T12:00:00Z", "source": "mng/agents"})
    long_event = json.dumps(
        {"event_id": "evt-2", "timestamp": "2026-03-01T12:01:00Z", "source": "mng/agents", "data": "x" * 1000}
    )
    other_event = json.dumps({"event_id": "evt-3", "timestamp": "2026-03-01T12:02:00Z", "source": "messages"})

    lines = [short_event, long_event, other_event]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=500, max_same_source_events_per_batch=100
    )

    assert len(result) == 2

    aggregate = json.loads(result[0])
    assert aggregate["type"] == "aggregate"
    assert aggregate["source"] == "mng/agents"

    messages_event = json.loads(result[1])
    assert messages_event["event_id"] == "evt-3"

    # Aggregate file should contain both mng/agents events
    aggregate_file = Path(aggregate["aggregate_events"][0])
    file_lines = aggregate_file.read_text().strip().split("\n")
    assert len(file_lines) == 2


def test_apply_special_event_handling_uses_max_timestamp_from_aggregated_events(tmp_path: Path) -> None:
    """The aggregate event's timestamp is the max timestamp of the aggregated events."""
    event_lists_dir = tmp_path / "event_lists"
    event_lists_dir.mkdir()

    lines = [
        json.dumps({"event_id": "evt-1", "timestamp": "2026-03-01T12:00:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-2", "timestamp": "2026-03-01T14:00:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-3", "timestamp": "2026-03-01T13:00:00Z", "source": "scheduled"}),
    ]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=100_000, max_same_source_events_per_batch=2
    )

    aggregate = json.loads(result[0])
    assert aggregate["timestamp"] == "2026-03-01T14:00:00Z"


def test_apply_special_event_handling_preserves_malformed_lines(tmp_path: Path) -> None:
    """Malformed JSON lines pass through unchanged."""
    event_lists_dir = tmp_path / "event_lists"
    event_lists_dir.mkdir()

    lines = [
        "not json",
        json.dumps({"event_id": "evt-1", "timestamp": "2026-03-01T12:00:00Z", "source": "scheduled"}),
    ]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=100_000, max_same_source_events_per_batch=100
    )
    assert result == lines


def test_apply_special_event_handling_falls_back_on_write_failure(tmp_path: Path) -> None:
    """If aggregate file cannot be written, events are included inline."""
    event_lists_dir = Path("/nonexistent_dir_xyz/event_lists")

    lines = [
        json.dumps({"event_id": "evt-1", "timestamp": "2026-03-01T12:00:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-2", "timestamp": "2026-03-01T12:01:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-3", "timestamp": "2026-03-01T12:02:00Z", "source": "scheduled"}),
    ]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=100_000, max_same_source_events_per_batch=2
    )
    assert result == lines


def test_apply_special_event_handling_multiple_sources_aggregated(tmp_path: Path) -> None:
    """Multiple sources can be aggregated independently in the same batch."""
    event_lists_dir = tmp_path / "event_lists"
    event_lists_dir.mkdir()

    lines = [
        json.dumps({"event_id": "evt-s1", "timestamp": "2026-03-01T12:00:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-m1", "timestamp": "2026-03-01T12:01:00Z", "source": "monitor"}),
        json.dumps({"event_id": "evt-s2", "timestamp": "2026-03-01T12:02:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-s3", "timestamp": "2026-03-01T12:03:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-m2", "timestamp": "2026-03-01T12:04:00Z", "source": "monitor"}),
        json.dumps({"event_id": "evt-m3", "timestamp": "2026-03-01T12:05:00Z", "source": "monitor"}),
    ]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=100_000, max_same_source_events_per_batch=2
    )

    assert len(result) == 2
    agg_scheduled = json.loads(result[0])
    agg_monitor = json.loads(result[1])
    assert agg_scheduled["type"] == "aggregate"
    assert agg_scheduled["source"] == "scheduled"
    assert agg_monitor["type"] == "aggregate"
    assert agg_monitor["source"] == "monitor"

    # Verify both aggregate files exist and have correct event counts
    sched_file = Path(agg_scheduled["aggregate_events"][0])
    monitor_file = Path(agg_monitor["aggregate_events"][0])
    assert len(sched_file.read_text().strip().split("\n")) == 3
    assert len(monitor_file.read_text().strip().split("\n")) == 3


def test_apply_special_event_handling_aggregate_replaces_at_first_occurrence(tmp_path: Path) -> None:
    """The aggregate event replaces the position of the first event from that source."""
    event_lists_dir = tmp_path / "event_lists"
    event_lists_dir.mkdir()

    lines = [
        json.dumps({"event_id": "evt-other", "timestamp": "2026-03-01T12:00:00Z", "source": "messages"}),
        json.dumps({"event_id": "evt-s1", "timestamp": "2026-03-01T12:01:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-s2", "timestamp": "2026-03-01T12:02:00Z", "source": "scheduled"}),
        json.dumps({"event_id": "evt-s3", "timestamp": "2026-03-01T12:03:00Z", "source": "scheduled"}),
    ]
    result = _apply_special_event_handling(
        lines, event_lists_dir, max_event_length=100_000, max_same_source_events_per_batch=2
    )

    assert len(result) == 2
    # First: messages event (non-aggregated, original position)
    assert json.loads(result[0])["event_id"] == "evt-other"
    # Second: aggregate event (replaces first scheduled position)
    assert json.loads(result[1])["type"] == "aggregate"


# -- Test helpers for _run_delivery_loop and main --


def _make_event_line(event_id: str, timestamp: str = "2026-03-01T12:00:00Z", source: str = "scheduled") -> str:
    return json.dumps({"event_id": event_id, "timestamp": timestamp, "source": source})


class _MessageCapture:
    """Records send_message calls and returns a configurable result.

    Use ``wait_for_call()`` to block until at least one call has been made,
    avoiding time.sleep polling in tests.
    """

    def __init__(self, *, succeed: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._succeed = succeed
        self._called = threading.Event()

    def __call__(self, agent_id: str, message: str) -> bool:
        self.calls.append((agent_id, message))
        self._called.set()
        return self._succeed

    def wait_for_call(self, timeout: float = 5.0) -> bool:
        """Block until at least one call has been recorded. Returns True if a call arrived."""
        return self._called.wait(timeout=timeout)


class _FakeEventsProcess:
    """A fake subprocess.Popen that emits JSONL event lines on stdout then exits.

    stdout yields lines immediately (like a real process writing to a pipe).
    wait() blocks until stdout has been consumed, then returns.
    """

    def __init__(self, event_lines: list[str]) -> None:
        stdout_data = "\n".join(event_lines) + "\n" if event_lines else ""
        self.stdout = io.StringIO(stdout_data)
        self.stderr = io.StringIO("")
        self.returncode: int = 0
        self._waited = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        # Simulate "process exits after stdout is consumed":
        # give a brief moment for the reader thread to consume stdout,
        # then return.
        self._waited.wait(timeout=0.2)
        self.returncode = 0
        self._waited.set()
        return 0

    def poll(self) -> int | None:
        if self._waited.is_set():
            return 0
        return None

    def terminate(self) -> None:
        self._waited.set()
        self.returncode = -15

    def kill(self) -> None:
        self._waited.set()
        self.returncode = -9


def _setup_delivery_loop_dirs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, _IgnoredSourcesState]:
    """Create state_file, events_dir, event_batches_dir, event_lists_dir for delivery loop tests."""
    state_file = tmp_path / "events" / ".event_delivery_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    events_dir = tmp_path / "events"
    event_batches_dir = tmp_path / "mind" / "event_batches"
    event_batches_dir.mkdir(parents=True)
    event_lists_dir = tmp_path / "mind" / "event_lists"
    event_lists_dir.mkdir(parents=True)
    ignored_sources_state = _IgnoredSourcesState(file_path=tmp_path / "ignored_sources.txt")
    return state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state


# -- _run_delivery_loop tests --


def test_delivery_loop_delivers_buffered_events(tmp_path: Path) -> None:
    """Events placed in the buffer are written to event_batches_dir and sent via send_message."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(burst_size=5, max_messages_per_minute=60)

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    capture = _MessageCapture()

    # Pre-load buffer with events
    event_buffer.append(_make_event_line("evt-1"))
    event_buffer.append(_make_event_line("evt-2", timestamp="2026-03-01T12:01:00Z"))

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": capture},
        daemon=True,
    )
    thread.start()

    capture.wait_for_call(timeout=5.0)

    stop_event.set()
    thread.join(timeout=2.0)

    assert len(capture.calls) == 1
    agent_id, message = capture.calls[0]
    assert agent_id == "test-agent"
    assert "Please process all events in " in message
    assert message.endswith(".jsonl")

    # Verify events file was written
    batch_files = list(event_batches_dir.glob("*.jsonl"))
    assert len(batch_files) == 1
    lines = batch_files[0].read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == "evt-1"
    assert json.loads(lines[1])["event_id"] == "evt-2"

    # Verify delivery state was persisted
    loaded = _load_delivery_state(state_file)
    assert loaded.last_event_id == "evt-2"
    assert loaded.last_timestamp == "2026-03-01T12:01:00Z"


def test_delivery_loop_retries_on_failure(tmp_path: Path) -> None:
    """When send_message fails, events stay in the buffer for retry."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(burst_size=5, max_messages_per_minute=600, max_delivery_retries=2)

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()

    # Use a send_message that fails the first 2 times then succeeds
    call_count = 0
    succeeded = threading.Event()

    def failing_then_succeeding(agent_id: str, message: str) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            succeeded.set()
            return True
        return False

    event_buffer.append(_make_event_line("evt-retry"))

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": failing_then_succeeding, "backoff_base_seconds": 0.01},
        daemon=True,
    )
    thread.start()

    succeeded.wait(timeout=10.0)

    stop_event.set()
    thread.join(timeout=2.0)

    assert call_count >= 3, f"Expected at least 3 send attempts, got {call_count}"

    # Verify delivery eventually succeeded: state file should be written
    loaded = _load_delivery_state(state_file)
    assert loaded.last_event_id == "evt-retry"


@pytest.mark.timeout(15)
def test_delivery_loop_writes_notification_on_repeated_failure(tmp_path: Path) -> None:
    """After max_delivery_retries consecutive failures, a notification event is written."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(burst_size=5, max_messages_per_minute=600, max_delivery_retries=2)

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()

    event_buffer.append(_make_event_line("evt-fail"))

    notification_file = events_dir / "delivery_failures" / "events.jsonl"
    notification_written = threading.Event()

    def failing_and_tracking(agent_id: str, message: str) -> bool:
        if notification_file.exists():
            notification_written.set()
        return False

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": failing_and_tracking, "backoff_base_seconds": 0.01},
        daemon=True,
    )
    thread.start()

    # Wait for enough failures to trigger notification
    notification_written.wait(timeout=10.0)

    stop_event.set()
    thread.join(timeout=2.0)

    assert notification_file.exists(), "Expected notification event file to be created"
    notification = json.loads(notification_file.read_text().strip().split("\n")[0])
    assert "failed" in notification["message"].lower()
    assert notification["level"] == "ERROR"


def test_delivery_loop_catches_up_from_saved_state(tmp_path: Path) -> None:
    """When resuming with saved state, events before the last delivered timestamp are skipped."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(burst_size=5, max_messages_per_minute=60)

    # Save state indicating evt-1 was already delivered
    _save_delivery_state(state_file, _DeliveryState(last_event_id="evt-1", last_timestamp="2026-03-01T12:00:00Z"))

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    capture = _MessageCapture()

    # Buffer has an old event (should be skipped) and a new event
    event_buffer.append(_make_event_line("evt-1", timestamp="2026-03-01T12:00:00Z"))
    event_buffer.append(_make_event_line("evt-old", timestamp="2026-03-01T11:00:00Z"))
    event_buffer.append(_make_event_line("evt-new", timestamp="2026-03-01T13:00:00Z"))

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": capture},
        daemon=True,
    )
    thread.start()

    capture.wait_for_call(timeout=5.0)

    stop_event.set()
    thread.join(timeout=2.0)

    assert len(capture.calls) == 1
    # The delivered batch should only contain the new event
    batch_files = list(event_batches_dir.glob("*.jsonl"))
    assert len(batch_files) == 1
    lines = batch_files[0].read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "evt-new"


def test_delivery_loop_stops_cleanly_on_stop_event(tmp_path: Path) -> None:
    """The delivery loop exits when stop_event is set, even with no events."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings()

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        daemon=True,
    )
    thread.start()

    stop_event.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive(), "Delivery loop should have exited"


def test_delivery_loop_aggregates_events_exceeding_batch_limit(tmp_path: Path) -> None:
    """Events from a source exceeding max_same_source_events_per_batch are aggregated."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(burst_size=5, max_messages_per_minute=60, max_same_source_events_per_batch=2)

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    capture = _MessageCapture()

    # Pre-load buffer with 3 events from the same source (exceeds limit of 2)
    event_buffer.append(_make_event_line("evt-1", source="scheduled"))
    event_buffer.append(_make_event_line("evt-2", timestamp="2026-03-01T12:01:00Z", source="scheduled"))
    event_buffer.append(_make_event_line("evt-3", timestamp="2026-03-01T12:02:00Z", source="scheduled"))

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": capture},
        daemon=True,
    )
    thread.start()

    capture.wait_for_call(timeout=5.0)

    stop_event.set()
    thread.join(timeout=2.0)

    assert len(capture.calls) == 1

    # The batch file should contain a single aggregate event
    batch_files = list(event_batches_dir.glob("*.jsonl"))
    assert len(batch_files) == 1
    batch_lines = batch_files[0].read_text().strip().split("\n")
    assert len(batch_lines) == 1
    aggregate = json.loads(batch_lines[0])
    assert aggregate["type"] == "aggregate"
    assert aggregate["source"] == "scheduled"

    # The event list file should contain the 3 original events
    event_list_files = list(event_lists_dir.glob("*.jsonl"))
    assert len(event_list_files) == 1
    list_lines = event_list_files[0].read_text().strip().split("\n")
    assert len(list_lines) == 3


@pytest.mark.timeout(15)
def test_delivery_loop_filters_ignored_sources(tmp_path: Path) -> None:
    """Events from sources listed in ignored_sources.txt are excluded from delivery."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(burst_size=5, max_messages_per_minute=60)

    # Write an ignored_sources.txt that filters out "scheduled"
    ignored_sources_state.file_path.write_text("scheduled\n")

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    capture = _MessageCapture()

    # Buffer has events from two sources: one ignored, one not
    event_buffer.append(_make_event_line("evt-keep", source="messages"))
    event_buffer.append(_make_event_line("evt-drop", timestamp="2026-03-01T12:01:00Z", source="scheduled"))

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": capture},
        daemon=True,
    )
    thread.start()

    capture.wait_for_call(timeout=5.0)

    stop_event.set()
    thread.join(timeout=2.0)

    assert len(capture.calls) == 1

    # Only the non-ignored event should be in the batch
    batch_files = list(event_batches_dir.glob("*.jsonl"))
    assert len(batch_files) == 1
    lines = batch_files[0].read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "evt-keep"


@pytest.mark.timeout(15)
def test_delivery_loop_skips_empty_batch_after_ignored_sources(tmp_path: Path) -> None:
    """When all events in a batch are from ignored sources, no delivery happens."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(burst_size=5, max_messages_per_minute=60)

    # Write ignored_sources.txt that filters out everything we'll send
    ignored_sources_state.file_path.write_text("scheduled\n")

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    capture = _MessageCapture()

    # All events are from the ignored source
    event_buffer.append(_make_event_line("evt-1", source="scheduled"))
    event_buffer.append(_make_event_line("evt-2", timestamp="2026-03-01T12:01:00Z", source="scheduled"))

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "test-agent",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": capture},
        daemon=True,
    )
    thread.start()

    # Wait for the delivery loop to drain the buffer (events are ignored, so no delivery)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with buffer_lock:
            if not event_buffer:
                break
        stop_event.wait(timeout=0.05)

    stop_event.set()
    thread.join(timeout=2.0)

    # No messages should have been sent since all events were ignored
    assert len(capture.calls) == 0
    assert len(list(event_batches_dir.glob("*.jsonl"))) == 0


@pytest.mark.timeout(15)
def test_delivery_loop_filters_event_exclude_sources(tmp_path: Path) -> None:
    """Events from sources in event_exclude_sources are excluded from delivery."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(
        burst_size=5,
        max_messages_per_minute=60,
        event_exclude_sources=("claude/common_transcript",),
    )

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    capture = _MessageCapture()

    # Pre-load buffer with events from both excluded and non-excluded sources
    event_buffer.append(_make_event_line("evt-excluded", source="claude/common_transcript"))
    event_buffer.append(_make_event_line("evt-keep", source="messages"))

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "agent-test-00000000000000000001",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": capture},
        daemon=True,
    )
    thread.start()

    assert capture.wait_for_call(timeout=5.0), "Expected send_message to be called"
    stop_event.set()
    thread.join(timeout=2.0)

    # Only the non-excluded event should be in the batch
    batch_files = list(event_batches_dir.glob("*.jsonl"))
    assert len(batch_files) == 1
    lines = batch_files[0].read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "evt-keep"


@pytest.mark.timeout(15)
def test_delivery_loop_delivers_user_message_immediately_when_batching_disabled(tmp_path: Path) -> None:
    """When is_message_batching_enabled=False, user messages are delivered without waiting for assistant."""
    state_file, events_dir, event_batches_dir, event_lists_dir, ignored_sources_state = _setup_delivery_loop_dirs(
        tmp_path
    )
    settings = _EventWatcherSettings(
        burst_size=5,
        max_messages_per_minute=60,
        is_message_batching_enabled=False,
    )

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_event = threading.Event()
    capture = _MessageCapture()

    # A user message from the "messages" source -- with batching enabled this would be
    # held until an assistant response arrives, but with batching disabled it should
    # be delivered immediately.
    user_msg = json.dumps(
        {
            "source": "messages",
            "role": "user",
            "conversation_id": "conv-1",
            "event_id": "evt-user-1",
            "timestamp": "2026-03-01T12:00:00Z",
        }
    )
    event_buffer.append(user_msg)

    thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            "agent-test-00000000000000000001",
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
        ),
        kwargs={"send_message": capture},
        daemon=True,
    )
    thread.start()

    assert capture.wait_for_call(timeout=5.0), "Expected send_message to be called"
    stop_event.set()
    thread.join(timeout=2.0)

    # The user message should have been delivered (not held)
    batch_files = list(event_batches_dir.glob("*.jsonl"))
    assert len(batch_files) == 1
    lines = batch_files[0].read_text().strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event_id"] == "evt-user-1"
    assert parsed["role"] == "user"
    assert parsed["source"] == "messages"


# -- main() tests --


def _setup_main_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    suppress_onboarding: bool = True,
) -> Path:
    """Set up the environment variables and directory structure for main() tests.

    Returns the agent_state_dir path. By default, creates the onboarding marker
    to prevent the synthetic events thread from injecting onboarding events.
    """
    agent_state_dir = tmp_path / "agents" / "agent-test"
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True)
    monkeypatch.setenv("MNG_AGENT_STATE_DIR", str(agent_state_dir))
    monkeypatch.setenv("MNG_AGENT_WORK_DIR", str(work_dir))
    monkeypatch.setenv("MNG_AGENT_ID", "agent-test-00000000000000000001")
    if suppress_onboarding:
        mind_dir = agent_state_dir / "mind"
        mind_dir.mkdir(parents=True, exist_ok=True)
        (mind_dir / _ONBOARDING_MARKER_FILENAME).touch()
    return agent_state_dir


@pytest.mark.timeout(15)
def test_main_delivers_events_from_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() reads events from the subprocess, writes them to event_batches, and sends a message."""
    agent_state_dir = _setup_main_env(tmp_path, monkeypatch)
    capture = _MessageCapture()
    stop_event = threading.Event()

    events = [
        _make_event_line("evt-a", timestamp="2026-03-01T12:00:00Z"),
        _make_event_line("evt-b", timestamp="2026-03-01T12:01:00Z"),
    ]

    call_count = 0

    def fake_start_subprocess(agent_id: str, cel_filter: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeEventsProcess(events)
        # On second call (after restart), stop main
        stop_event.set()
        return _FakeEventsProcess([])

    thread = threading.Thread(
        target=main,
        kwargs={
            "start_subprocess": fake_start_subprocess,
            "stop_event": stop_event,
            "send_message": capture,
        },
        daemon=True,
    )
    thread.start()

    capture.wait_for_call(timeout=5.0)

    # Wait for delivery state file to be written. The state file is saved right after
    # send_message returns, but wait_for_call fires at the moment send_message is
    # called, before _save_delivery_state completes.
    state_file = agent_state_dir / "events" / ".event_delivery_state.json"
    deadline = time.monotonic() + 2.0
    while not state_file.exists() and time.monotonic() < deadline:
        stop_event.wait(timeout=0.01)

    stop_event.set()
    thread.join(timeout=3.0)

    assert len(capture.calls) >= 1
    agent_id, message = capture.calls[0]
    assert agent_id == "agent-test-00000000000000000001"
    assert "Please process all events in " in message
    assert message.endswith(".jsonl")

    # Verify event batch files were created
    event_batches_dir = agent_state_dir / "mind" / "event_batches"
    batch_files = list(event_batches_dir.glob("*.jsonl"))
    assert len(batch_files) >= 1

    # Verify the batch contains the events
    all_event_ids = set()
    for f in batch_files:
        for line in f.read_text().strip().split("\n"):
            all_event_ids.add(json.loads(line)["event_id"])
    assert "evt-a" in all_event_ids
    assert "evt-b" in all_event_ids

    # Verify delivery state was persisted
    assert state_file.exists()
    loaded = _load_delivery_state(state_file)
    assert loaded.last_event_id in ("evt-a", "evt-b")


@pytest.mark.timeout(15)
def test_main_restarts_subprocess_on_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() restarts the events subprocess when it exits."""
    _setup_main_env(tmp_path, monkeypatch)
    capture = _MessageCapture()
    stop_event = threading.Event()

    call_count = 0

    def counting_factory(agent_id: str, cel_filter: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            # Stop after the restart to avoid waiting through multiple delays
            stop_event.set()
        return _FakeEventsProcess([])

    thread = threading.Thread(
        target=main,
        kwargs={
            "start_subprocess": counting_factory,
            "stop_event": stop_event,
            "send_message": capture,
            "subprocess_restart_delay_seconds": 0.01,
        },
        daemon=True,
    )
    thread.start()
    thread.join(timeout=10.0)

    # call_count >= 2 proves the subprocess was started, exited, and restarted
    assert call_count >= 2, f"Expected subprocess to be started at least 2 times, got {call_count}"


def test_main_stops_cleanly_via_stop_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() exits cleanly when stop_event is set."""
    _setup_main_env(tmp_path, monkeypatch)
    stop_event = threading.Event()

    started = threading.Event()

    def factory_with_signal(agent_id: str, cel_filter: str) -> Any:
        started.set()
        return _FakeEventsProcess([])

    thread = threading.Thread(
        target=main,
        kwargs={
            "start_subprocess": factory_with_signal,
            "stop_event": stop_event,
            "send_message": _MessageCapture(),
        },
        daemon=True,
    )
    thread.start()

    started.wait(timeout=3.0)
    stop_event.set()
    thread.join(timeout=3.0)

    assert not thread.is_alive(), "main() should have exited after stop_event was set"


def test_main_creates_required_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() creates the events/ and mind/event_batches/ directories."""
    agent_state_dir = _setup_main_env(tmp_path, monkeypatch)
    stop_event = threading.Event()

    def immediate_stop_factory(agent_id: str, cel_filter: str) -> Any:
        stop_event.set()
        return _FakeEventsProcess([])

    main(
        start_subprocess=immediate_stop_factory,
        stop_event=stop_event,
        send_message=_MessageCapture(),
    )

    assert (agent_state_dir / "events").is_dir()
    assert (agent_state_dir / "mind" / "event_batches").is_dir()
    assert (agent_state_dir / "mind" / "event_lists").is_dir()


# -- _filter_ignored_sources tests --


def test_filter_ignored_sources_removes_matching() -> None:
    lines = [
        json.dumps({"source": "messages", "event_id": "e1"}),
        json.dumps({"source": "scheduled", "event_id": "e2"}),
        json.dumps({"source": "mng/agents", "event_id": "e3"}),
    ]
    result = _filter_ignored_sources(lines, frozenset({"scheduled"}))
    assert len(result) == 2
    assert all("scheduled" not in line for line in result)


def test_filter_ignored_sources_empty_set_passes_all() -> None:
    lines = [json.dumps({"source": "messages", "event_id": "e1"})]
    result = _filter_ignored_sources(lines, frozenset())
    assert result == lines


def test_filter_ignored_sources_malformed_json_passes_through() -> None:
    lines = ["not json", json.dumps({"source": "messages"})]
    result = _filter_ignored_sources(lines, frozenset({"messages"}))
    assert len(result) == 1
    assert result[0] == "not json"


def test_filter_ignored_sources_removes_multiple() -> None:
    lines = [
        json.dumps({"source": "a"}),
        json.dumps({"source": "b"}),
        json.dumps({"source": "c"}),
    ]
    result = _filter_ignored_sources(lines, frozenset({"a", "c"}))
    assert len(result) == 1
    assert json.loads(result[0])["source"] == "b"


# -- _IgnoredSourcesState / _load_ignored_sources_if_updated tests --


def test_load_ignored_sources_reads_file(tmp_path: Path) -> None:
    ignored_file = tmp_path / "ignored_sources.txt"
    ignored_file.write_text("messages\nscheduled\n")
    state = _IgnoredSourcesState(file_path=ignored_file)
    result = _load_ignored_sources_if_updated(state)
    assert result == frozenset({"messages", "scheduled"})


def test_load_ignored_sources_skips_comments_and_blanks(tmp_path: Path) -> None:
    ignored_file = tmp_path / "ignored_sources.txt"
    ignored_file.write_text("# comment\nmessages\n\n  \n# another\nscheduled\n")
    state = _IgnoredSourcesState(file_path=ignored_file)
    result = _load_ignored_sources_if_updated(state)
    assert result == frozenset({"messages", "scheduled"})


def test_load_ignored_sources_returns_empty_when_missing(tmp_path: Path) -> None:
    state = _IgnoredSourcesState(file_path=tmp_path / "nonexistent.txt")
    result = _load_ignored_sources_if_updated(state)
    assert result == frozenset()


def test_load_ignored_sources_caches_until_mtime_changes(tmp_path: Path) -> None:
    ignored_file = tmp_path / "ignored_sources.txt"
    ignored_file.write_text("messages\n")
    state = _IgnoredSourcesState(file_path=ignored_file)

    first_result = _load_ignored_sources_if_updated(state)
    assert first_result == frozenset({"messages"})

    # Same mtime, should return cached
    second_result = _load_ignored_sources_if_updated(state)
    assert second_result == frozenset({"messages"})


def test_load_ignored_sources_rereads_on_mtime_change(tmp_path: Path) -> None:
    ignored_file = tmp_path / "ignored_sources.txt"
    ignored_file.write_text("messages\n")
    state = _IgnoredSourcesState(file_path=ignored_file)

    first_result = _load_ignored_sources_if_updated(state)
    assert first_result == frozenset({"messages"})

    # Write new content then bump mtime (some filesystems have 1s resolution)
    ignored_file.write_text("messages\nscheduled\nmng/agents\n")
    stat = ignored_file.stat()
    os.utime(ignored_file, (stat.st_atime + 2, stat.st_mtime + 2))

    updated_result = _load_ignored_sources_if_updated(state)
    assert updated_result == frozenset({"messages", "scheduled", "mng/agents"})


def test_load_ignored_sources_clears_when_file_deleted(tmp_path: Path) -> None:
    ignored_file = tmp_path / "ignored_sources.txt"
    ignored_file.write_text("messages\n")
    state = _IgnoredSourcesState(file_path=ignored_file)

    result = _load_ignored_sources_if_updated(state)
    assert result == frozenset({"messages"})

    ignored_file.unlink()
    result = _load_ignored_sources_if_updated(state)
    assert result == frozenset()


# -- _resolve_user_timezone tests --


def test_resolve_user_timezone_returns_valid_timezone() -> None:
    tz = _resolve_user_timezone("UTC")
    assert str(tz) == "UTC"


def test_resolve_user_timezone_falls_back_on_invalid() -> None:
    tz = _resolve_user_timezone("Not/A/Real/Timezone")
    assert str(tz) == "UTC"


# -- _make_synthetic_event_line tests --


def test_make_synthetic_event_line_has_envelope_fields() -> None:
    line = _make_synthetic_event_line("idle", "mind/idle")
    parsed = json.loads(line)
    assert parsed["type"] == "idle"
    assert parsed["source"] == "mind/idle"
    assert parsed["event_id"].startswith("evt-")
    assert "timestamp" in parsed


def test_make_synthetic_event_line_includes_extra_fields() -> None:
    line = _make_synthetic_event_line("idle", "mind/idle", {"minutes_since_last_event": 5.0})
    parsed = json.loads(line)
    assert parsed["minutes_since_last_event"] == 5.0


# -- _per_event_idle_delay_minutes tests --


def test_per_event_delay_first_event() -> None:
    assert _per_event_idle_delay_minutes((1, 10, 60), 0) == 1


def test_per_event_delay_second_event() -> None:
    assert _per_event_idle_delay_minutes((1, 10, 60), 1) == 10


def test_per_event_delay_third_event() -> None:
    assert _per_event_idle_delay_minutes((1, 10, 60), 2) == 60


def test_per_event_delay_repeats_last_value() -> None:
    assert _per_event_idle_delay_minutes((1, 10, 60), 3) == 60
    assert _per_event_idle_delay_minutes((1, 10, 60), 10) == 60


def test_per_event_delay_single_value_schedule() -> None:
    assert _per_event_idle_delay_minutes((5,), 0) == 5
    assert _per_event_idle_delay_minutes((5,), 1) == 5
    assert _per_event_idle_delay_minutes((5,), 2) == 5


# -- _parse_time_of_day tests --


def test_parse_time_of_day_hh_mm_ss() -> None:
    assert _parse_time_of_day("13:37:30") == (13, 37, 30)


def test_parse_time_of_day_hh_mm() -> None:
    assert _parse_time_of_day("15:00") == (15, 0, 0)


def test_parse_time_of_day_midnight() -> None:
    assert _parse_time_of_day("00:00:00") == (0, 0, 0)


def test_parse_time_of_day_strips_whitespace() -> None:
    assert _parse_time_of_day("  09:30  ") == (9, 30, 0)


def test_parse_time_of_day_rejects_invalid_format() -> None:
    with pytest.raises(InvalidTimeFormatError, match="Invalid time format"):
        _parse_time_of_day("25")


def test_parse_time_of_day_rejects_out_of_range_hour() -> None:
    with pytest.raises(InvalidTimeFormatError, match="out of range"):
        _parse_time_of_day("25:00")


def test_parse_time_of_day_rejects_out_of_range_minute() -> None:
    with pytest.raises(InvalidTimeFormatError, match="out of range"):
        _parse_time_of_day("12:60")


def test_parse_time_of_day_rejects_non_numeric_values() -> None:
    with pytest.raises(InvalidTimeFormatError, match="Non-numeric"):
        _parse_time_of_day("abc:def")


# -- _load_scheduled_events_state / _save_scheduled_events_state tests --


def test_load_scheduled_state_returns_empty_when_missing(tmp_path: Path) -> None:
    date, fired = _load_scheduled_events_state(tmp_path / "nonexistent.json")
    assert date == ""
    assert fired == set()


def test_save_and_load_scheduled_state_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    _save_scheduled_events_state(state_file, "2026-03-15", {"morning", "evening"})
    date, fired = _load_scheduled_events_state(state_file)
    assert date == "2026-03-15"
    assert fired == {"morning", "evening"}


def test_load_scheduled_state_returns_empty_on_corrupt_json(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("not valid json {{{")
    date, fired = _load_scheduled_events_state(state_file)
    assert date == ""
    assert fired == set()


def test_save_scheduled_state_creates_parent_directories(tmp_path: Path) -> None:
    state_file = tmp_path / "nested" / "dir" / "state.json"
    _save_scheduled_events_state(state_file, "2026-03-15", {"test"})
    assert state_file.exists()


# -- _run_synthetic_events_loop tests --


def test_synthetic_loop_sends_onboarding_on_first_run(tmp_path: Path) -> None:
    """On first run (no marker file), the loop injects a mind/onboarding event."""
    mind_state_dir = tmp_path / "mind"
    mind_state_dir.mkdir(parents=True)
    # Deliberately NOT creating the onboarding marker
    env = _create_synthetic_loop_env(mind_state_dir)

    settings = _EventWatcherSettings()
    env.stop_event.set()
    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        # Positive value: simulates that initial delivery has already happened,
        # so the onboarding wait loop exits immediately.
        last_delivery_monotonic=[1.0],
    )

    assert len(env.event_buffer) == 1
    parsed = json.loads(env.event_buffer[0])
    assert parsed["type"] == "onboarding"
    assert parsed["source"] == "mind/onboarding"
    assert (mind_state_dir / _ONBOARDING_MARKER_FILENAME).exists()


def test_synthetic_loop_onboarding_waits_for_delivery(tmp_path: Path) -> None:
    """Onboarding event is deferred until last_delivery_monotonic becomes positive."""
    mind_state_dir = tmp_path / "mind"
    mind_state_dir.mkdir(parents=True)
    env = _create_synthetic_loop_env(mind_state_dir)

    delivery_mono: list[float] = [0.0]
    onboarding_marker = mind_state_dir / _ONBOARDING_MARKER_FILENAME

    settings = _EventWatcherSettings()

    # Run the synthetic loop in a background thread so we can control timing
    loop_thread = threading.Thread(
        target=_run_synthetic_events_loop,
        kwargs={
            "settings": settings,
            "event_buffer": env.event_buffer,
            "buffer_lock": env.buffer_lock,
            "stop_event": env.stop_event,
            "last_real_event_monotonic": env.last_real_event_monotonic,
            "mind_state_dir": env.mind_state_dir,
            "last_delivery_monotonic": delivery_mono,
        },
        daemon=True,
    )
    loop_thread.start()

    # Give the loop time to enter the waiting state, then verify no
    # onboarding has been sent while delivery_mono is still 0.0.
    # Uses a never-set event for the delay to avoid time.sleep.
    delay = threading.Event()
    delay.wait(timeout=0.3)
    assert len(env.event_buffer) == 0, "Onboarding should be blocked while delivery has not occurred"
    assert not onboarding_marker.exists()

    # Simulate delivery completing
    delivery_mono[0] = 1.0

    # Poll for the onboarding marker to appear (proving the loop unblocked)
    deadline = time.monotonic() + 5.0
    while not onboarding_marker.exists():
        assert time.monotonic() < deadline, "Timed out waiting for onboarding after delivery"
        delay.wait(timeout=0.05)

    env.stop_event.set()
    loop_thread.join(timeout=5.0)

    assert len(env.event_buffer) == 1
    parsed = json.loads(env.event_buffer[0])
    assert parsed["type"] == "onboarding"
    assert parsed["source"] == "mind/onboarding"


def test_synthetic_loop_onboarding_exits_on_stop_without_delivery(tmp_path: Path) -> None:
    """If stop_event fires before delivery, onboarding is not sent."""
    mind_state_dir = tmp_path / "mind"
    mind_state_dir.mkdir(parents=True)
    env = _create_synthetic_loop_env(mind_state_dir)

    settings = _EventWatcherSettings()

    # Run the loop in a thread so we can stop it
    loop_done = threading.Event()

    def run_loop() -> None:
        _run_synthetic_events_loop(
            settings,
            env.event_buffer,
            env.buffer_lock,
            env.stop_event,
            env.last_real_event_monotonic,
            env.mind_state_dir,
            last_delivery_monotonic=[0.0],
        )
        loop_done.set()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()

    # Stop the loop without delivery ever happening
    env.stop_event.set()
    loop_done.wait(timeout=5.0)
    loop_thread.join(timeout=5.0)

    assert len(env.event_buffer) == 0
    assert not (mind_state_dir / _ONBOARDING_MARKER_FILENAME).exists()


def test_synthetic_loop_skips_onboarding_when_marker_exists(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """If the marker file exists, no onboarding event is sent."""
    env = synthetic_loop_env
    settings = _EventWatcherSettings()
    env.stop_event.set()
    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
    )
    assert len(env.event_buffer) == 0


def test_synthetic_loop_sends_idle_event_after_delay(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """The loop sends a mind/idle event after the configured delay."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    # The wait process completes immediately (agent is already idle)
    fake_process = _create_fake_wait_process(is_complete=True, returncode=0)

    call_count = 0

    def clock_past_threshold() -> float:
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            env.stop_event.set()
        # First call: agent becomes idle at 1050
        # Second call: 70s after idle -> fires
        return 1050.0 + (call_count - 1) * 70.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock_past_threshold,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=lambda _agent_id: fake_process,
    )

    idle_events = [line for line in env.event_buffer if '"mind/idle"' in line]
    assert len(idle_events) >= 1
    parsed = json.loads(idle_events[0])
    assert parsed["type"] == "idle"
    assert parsed["source"] == "mind/idle"
    assert parsed["idle_event_number"] == 1
    assert parsed["minutes_since_last_event"] >= 1.0


def test_synthetic_loop_no_idle_events_when_agent_not_waiting(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """No idle events fire when the agent never enters WAITING state."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    env.last_non_messages_event_monotonic[0] = 1030.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    call_count = 0

    def clock_30s_after_real() -> float:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            env.stop_event.set()
        return 1060.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock_30s_after_real,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=make_pending_idle_wait,
        last_non_messages_event_monotonic=env.last_non_messages_event_monotonic,
    )

    # No idle events because the agent never entered WAITING (still pending)
    idle_events = [line for line in env.event_buffer if '"mind/idle"' in line]
    assert len(idle_events) == 0


# -- Idle wait integration tests --


def test_idle_wait_no_event_until_agent_becomes_idle(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """When idle wait is enabled, no idle events fire until the agent enters WAITING."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    call_count = 0

    def clock_past_threshold() -> float:
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            env.stop_event.set()
        # Well past the 1-minute threshold from the last real event, but agent is not idle
        return 1200.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock_past_threshold,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=make_pending_idle_wait,
    )

    # No idle events because the agent never entered WAITING
    idle_events = [line for line in env.event_buffer if '"mind/idle"' in line]
    assert len(idle_events) == 0


def test_idle_wait_fires_after_agent_becomes_idle(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """Idle events fire based on time since the agent entered WAITING, not since the last real event."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    # The wait process completes immediately (agent is already idle)
    fake_process = _create_fake_wait_process(is_complete=True, returncode=0)

    call_count = 0

    def clock_with_idle_at_1050() -> float:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: wait process poll check -- agent becomes idle at 1050
            return 1050.0
        if call_count > 2:
            env.stop_event.set()
        # 70 seconds after agent became idle at 1050 (> 1 minute threshold)
        return 1120.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock_with_idle_at_1050,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=lambda _agent_id: fake_process,
    )

    idle_events = [line for line in env.event_buffer if '"mind/idle"' in line]
    assert len(idle_events) >= 1
    parsed = json.loads(idle_events[0])
    assert parsed["type"] == "idle"
    assert parsed["idle_event_number"] == 1
    # minutes_since_last_event is measured from when the agent became idle (1050),
    # not from the last real event (1000)
    assert parsed["minutes_since_last_event"] >= 1.0


def test_idle_wait_resets_on_real_event(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """When a real event arrives, idle state resets. No new wait starts until delivery."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    wait_calls: list[str] = []

    def tracking_idle_wait(agent_id: str) -> FakeWaitProcess:
        wait_calls.append(agent_id)
        # Return a pending process (agent not yet idle)
        return _create_fake_wait_process(is_complete=False, returncode=None)

    call_count = 0

    def clock_with_new_event() -> float:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a new real event arriving
            env.last_real_event_monotonic[0] = 1100.0
            return 1100.0
        if call_count > 2:
            env.stop_event.set()
        return 1200.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock_with_new_event,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=tracking_idle_wait,
        # No last_delivery_monotonic -- wait won't restart without delivery
    )

    # Only initial wait at startup; on_real_event does NOT start a new wait
    assert len(wait_calls) == 1
    # No idle events because agent never entered WAITING
    idle_events = [line for line in env.event_buffer if '"mind/idle"' in line]
    assert len(idle_events) == 0


def test_idle_wait_restarts_after_delivery_plus_slack(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """After a real event is delivered, the wait restarts only after slack time elapses."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    wait_calls: list[str] = []

    def tracking_idle_wait(agent_id: str) -> FakeWaitProcess:
        wait_calls.append(agent_id)
        return _create_fake_wait_process(is_complete=False, returncode=None)

    # Simulate delivery happening at T=1005 (after the real event at T=1000)
    delivery_mono: list[float] = [1005.0]

    call_count = 0

    def clock_with_delivery() -> float:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Real event arrives
            env.last_real_event_monotonic[0] = 1002.0
            return 1002.0
        if call_count == 2:
            # Not enough slack yet (only 3s after delivery, need 5s)
            return 1008.0
        if call_count == 3:
            # Enough slack (6s after delivery at 1005)
            return 1011.0
        if call_count > 4:
            env.stop_event.set()
        return 1100.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock_with_delivery,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=tracking_idle_wait,
        last_delivery_monotonic=delivery_mono,
    )

    # Initial wait + restart after delivery+slack = 2 calls
    assert len(wait_calls) == 2


def test_idle_events_require_agent_id(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """Without agent_id, no idle events are sent even if the schedule is configured."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    call_count = 0

    def clock_past_threshold() -> float:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            env.stop_event.set()
        return 1061.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock_past_threshold,
        poll_interval_seconds=0.01,
    )

    # No idle events: agent_id is required for idle event generation
    idle_events = [line for line in env.event_buffer if '"mind/idle"' in line]
    assert len(idle_events) == 0


def test_idle_wait_restarts_on_nonzero_exit(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """When mng wait exits with a non-zero code, it is restarted."""
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1,))

    wait_calls: list[str] = []
    call_idx = 0

    def wait_that_fails_then_succeeds(agent_id: str) -> FakeWaitProcess:
        nonlocal call_idx
        wait_calls.append(agent_id)
        call_idx += 1
        if call_idx == 1:
            # First call: fail with non-zero exit code
            return _create_fake_wait_process(is_complete=True, returncode=1)
        # Second call: succeed
        return _create_fake_wait_process(is_complete=True, returncode=0)

    call_count = 0

    def clock() -> float:
        nonlocal call_count
        call_count += 1
        if call_count > 4:
            env.stop_event.set()
        # Iteration 1: wait fails at T=1050, restart issued
        # Iteration 2: wait succeeds, agent_idle_since=1100
        # Iteration 3: T=1170 → elapsed=70s > 60s → idle event fires
        return 1050.0 + (call_count - 1) * 50.0

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=wait_that_fails_then_succeeds,
    )

    # First call at startup (fails), second call is restart (succeeds)
    assert len(wait_calls) >= 2
    # Should have sent idle events after the restart succeeded
    idle_events = [line for line in env.event_buffer if '"mind/idle"' in line]
    assert len(idle_events) >= 1


def _make_first_idle_cycle_clock(
    env: SyntheticLoopEnv,
    tracker: TrackingIdleWait,
    delivery_mono: list[float],
    post_reentry_steps: Callable[[int], float | None],
) -> Callable[[], float]:
    """Build a clock that fires the first idle event, simulates delivery, then delegates.

    Implements the common 5-step idle cycle used by multiple tests:
      1. Agent becomes idle (first wait process completes) at T=1050
      2. 70s later (T=1120): first idle event fires (> 1 min threshold)
      3. Delivery + conversation events arrive (messages source)
      4. Delivery completes with slack
      5. Agent re-enters WAITING at T=1140

    For call_count >= 6, delegates to ``post_reentry_steps(call_count)``.
    Return None from the callback to stop the loop.
    """
    call_count_box: list[int] = [0]

    def clock() -> float:
        call_count_box[0] += 1
        n = call_count_box[0]

        if n == 1:
            tracker.processes[0].complete(0)
            return 1050.0
        if n == 2:
            return 1120.0
        if n == 3:
            delivery_mono[0] = 1121.0
            env.last_real_event_monotonic[0] = 1130.0
            return 1130.0
        if n == 4:
            delivery_mono[0] = 1131.0
            return 1137.0
        if n == 5:
            if len(tracker.processes) >= 2:
                tracker.processes[-1].complete(0)
            return 1140.0

        result = post_reentry_steps(n)
        if result is None:
            env.stop_event.set()
            return 1900.0
        return result

    return clock


def _run_idle_cycle_loop(
    env: SyntheticLoopEnv,
    settings: _EventWatcherSettings,
    tracker: TrackingIdleWait,
    delivery_mono: list[float],
    clock: Callable[[], float],
) -> list[dict[str, Any]]:
    """Run the synthetic events loop with standard idle-cycle parameters.

    Returns parsed idle events from the buffer.
    """
    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        clock,
        poll_interval_seconds=0.01,
        agent_id="agent-test",
        start_idle_wait=tracker,
        last_delivery_monotonic=delivery_mono,
        last_non_messages_event_monotonic=env.last_non_messages_event_monotonic,
    )
    return [json.loads(line) for line in env.event_buffer if '"mind/idle"' in line]


def test_idle_wait_preserves_counter_across_messages_events(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """After sending an idle event, conversation events (messages source) do NOT
    reset idle_events_sent because last_non_messages_event_monotonic is unchanged.

    This tests the full backoff cycle: send idle event -> agent processes it
    and generates messages events -> agent re-enters WAITING -> next idle event
    uses the incremented counter (longer delay).
    """
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1, 10, 60))
    tracker = create_tracking_idle_wait()
    delivery_mono: list[float] = [1000.0]

    def post_reentry(n: int) -> float | None:
        if n == 6:
            # 5 min after re-entry, not enough for 10 min delay
            return 1440.0
        if n == 7:
            # 11 min after re-entry, second idle event fires
            return 1800.0
        if n > 8:
            return None
        return 1900.0

    clock = _make_first_idle_cycle_clock(env, tracker, delivery_mono, post_reentry)
    idle_events = _run_idle_cycle_loop(env, settings, tracker, delivery_mono, clock)

    assert len(idle_events) >= 2
    assert idle_events[0]["idle_event_number"] == 1
    assert idle_events[1]["idle_event_number"] == 2


def test_idle_wait_resets_counter_on_non_messages_event(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """When a non-messages event arrives, the idle counter resets to 0.

    Events from sources other than "messages" (e.g. mng/agents, monitor)
    are genuinely new external events and should reset the backoff.
    """
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1, 10, 60))
    tracker = create_tracking_idle_wait()
    delivery_mono: list[float] = [1000.0]

    def post_reentry(n: int) -> float | None:
        if n == 6:
            # A genuinely new non-messages event arrives -- resets counter
            env.last_real_event_monotonic[0] = 1200.0
            env.last_non_messages_event_monotonic[0] = 1200.0
            return 1200.0
        if n == 7:
            delivery_mono[0] = 1202.0
            return 1210.0
        if n == 8:
            # New wait process starts and completes (agent idle again)
            if len(tracker.processes) >= 3:
                tracker.processes[-1].complete(0)
            return 1220.0
        if n == 9:
            # 70s from new idle: first event fires again
            return 1290.0
        if n > 10:
            return None
        return 1400.0

    clock = _make_first_idle_cycle_clock(env, tracker, delivery_mono, post_reentry)
    idle_events = _run_idle_cycle_loop(env, settings, tracker, delivery_mono, clock)

    assert len(idle_events) >= 2
    assert idle_events[0]["idle_event_number"] == 1
    # Counter was reset by the genuinely new event, so this is event 1 again
    assert idle_events[1]["idle_event_number"] == 1


def test_idle_wait_uses_per_event_delay(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """Delays are per-event (not cumulative).

    Schedule [1, 10]: first event after 1 min, second after 10 min
    from the new WAITING time, not 11 min cumulative.
    """
    env = synthetic_loop_env
    env.last_real_event_monotonic[0] = 1000.0
    settings = _EventWatcherSettings(idle_event_delay_minutes_schedule=(1, 10))
    tracker = create_tracking_idle_wait()
    delivery_mono: list[float] = [1000.0]

    def post_reentry(n: int) -> float | None:
        if n == 6:
            # T=1140 + 5 min, not enough for 10 min delay
            return 1440.0
        if n == 7:
            # T=1140 + 11 min, second idle event fires
            return 1800.0
        if n > 8:
            return None
        return 1900.0

    clock = _make_first_idle_cycle_clock(env, tracker, delivery_mono, post_reentry)
    idle_events = _run_idle_cycle_loop(env, settings, tracker, delivery_mono, clock)

    assert len(idle_events) >= 2
    assert idle_events[0]["idle_event_number"] == 1
    assert idle_events[1]["idle_event_number"] == 2


def _make_counting_clock(env: SyntheticLoopEnv, max_iterations: int) -> Callable[[], float]:
    """Create a clock function that stops the loop after max_iterations calls."""
    state = {"count": 0}

    def clock() -> float:
        state["count"] += 1
        if state["count"] > max_iterations:
            env.stop_event.set()
        return time.monotonic()

    return clock


def test_synthetic_loop_sends_scheduled_event_when_time_passes(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """The loop fires a scheduled event when the current time passes the configured time."""
    env = synthetic_loop_env
    settings = _EventWatcherSettings(
        scheduled_events=(("test_event", "00:00:00"),),
        user_timezone="UTC",
    )

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        _make_counting_clock(env, 1),
        poll_interval_seconds=0.01,
    )

    schedule_events = [line for line in env.event_buffer if '"mind/schedule"' in line]
    assert len(schedule_events) == 1
    parsed = json.loads(schedule_events[0])
    assert parsed["type"] == "schedule"
    assert parsed["source"] == "mind/schedule"
    assert parsed["event_name"] == "test_event"
    assert parsed["scheduled_time"] == "00:00:00"


def test_synthetic_loop_does_not_refire_scheduled_event_same_day(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """A scheduled event only fires once per day."""
    env = synthetic_loop_env
    settings = _EventWatcherSettings(
        scheduled_events=(("test_event", "00:00:00"),),
        user_timezone="UTC",
    )

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        _make_counting_clock(env, 3),
        poll_interval_seconds=0.01,
    )

    schedule_events = [line for line in env.event_buffer if '"mind/schedule"' in line]
    assert len(schedule_events) == 1


def test_synthetic_loop_persists_scheduled_state(synthetic_loop_env: SyntheticLoopEnv) -> None:
    """Fired scheduled events are persisted and survive restarts."""
    env = synthetic_loop_env
    settings = _EventWatcherSettings(
        scheduled_events=(("test_event", "00:00:00"),),
        user_timezone="UTC",
    )

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        _make_counting_clock(env, 2),
        poll_interval_seconds=0.01,
    )

    schedule_events = [line for line in env.event_buffer if '"mind/schedule"' in line]
    assert len(schedule_events) == 1

    # Second run: event should NOT fire again (persisted state)
    env.stop_event.clear()
    env.event_buffer.clear()

    _run_synthetic_events_loop(
        settings,
        env.event_buffer,
        env.buffer_lock,
        env.stop_event,
        env.last_real_event_monotonic,
        env.mind_state_dir,
        _make_counting_clock(env, 2),
        poll_interval_seconds=0.01,
    )

    schedule_events = [line for line in env.event_buffer if '"mind/schedule"' in line]
    assert len(schedule_events) == 0
    assert (env.mind_state_dir / _SCHEDULED_STATE_FILENAME).exists()


def test_load_settings_reads_idle_schedule(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        "[watchers]\nidle_event_delay_minutes_schedule = [1, 10, 60]\n",
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.idle_event_delay_minutes_schedule == (1, 10, 60)


def test_load_settings_reads_scheduled_events(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        '[watchers.scheduled_events]\nmorning = "09:00"\nevening = "17:30:00"\n',
    )
    settings = _load_watcher_settings(tmp_path)
    assert dict(settings.scheduled_events) == {"morning": "09:00", "evening": "17:30:00"}


def test_load_settings_reads_user_timezone(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        '[watchers]\nuser_timezone = "America/New_York"\n',
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.user_timezone == "America/New_York"


def test_load_settings_defaults_new_fields(tmp_path: Path) -> None:
    settings = _load_watcher_settings(tmp_path)
    assert settings.idle_event_delay_minutes_schedule == ()
    assert settings.scheduled_events == ()
    assert settings.user_timezone == "UTC"
    assert settings.is_message_batching_enabled is True
    assert settings.event_batch_filter_command is None


def test_load_settings_reads_event_batch_filter_command(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        '[watchers]\nevent_batch_filter_command = "/usr/local/bin/my_filter.sh"\n',
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.event_batch_filter_command == "/usr/local/bin/my_filter.sh"


def test_load_settings_reads_is_message_batching_enabled(tmp_path: Path) -> None:
    write_minds_settings_toml(
        tmp_path,
        "[watchers]\nis_message_batching_enabled = false\n",
    )
    settings = _load_watcher_settings(tmp_path)
    assert settings.is_message_batching_enabled is False


@pytest.mark.timeout(15)
def test_main_starts_synthetic_events_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() starts the synthetic events thread and delivers onboarding on first run.

    Onboarding waits for last_delivery_monotonic to become positive, so we must
    emit a real event that gets delivered first. We poll for the onboarding marker
    rather than relying on capture timing, because last_delivery_monotonic is
    updated after send_message returns (race window).
    """
    agent_state_dir = _setup_main_env(tmp_path, monkeypatch, suppress_onboarding=False)
    capture = _MessageCapture()
    stop_event = threading.Event()
    mind_state_dir = agent_state_dir / "mind"

    events = [_make_event_line("evt-trigger", timestamp="2026-03-01T12:00:00Z")]
    call_count = 0

    def fake_start_subprocess(agent_id: str, cel_filter: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeEventsProcess(events)
        stop_event.set()
        return _FakeEventsProcess([])

    thread = threading.Thread(
        target=main,
        kwargs={
            "start_subprocess": fake_start_subprocess,
            "stop_event": stop_event,
            "send_message": capture,
        },
        daemon=True,
    )
    thread.start()

    # Poll for the onboarding marker (created after delivery unblocks onboarding)
    onboarding_marker = mind_state_dir / _ONBOARDING_MARKER_FILENAME
    deadline = time.monotonic() + 5.0
    while not onboarding_marker.exists():
        assert time.monotonic() < deadline, "Timed out waiting for onboarding marker"
        stop_event.wait(timeout=0.05)

    stop_event.set()
    thread.join(timeout=5.0)

    assert mind_state_dir.is_dir()
    assert onboarding_marker.exists()


@pytest.mark.timeout(15)
def test_main_delivers_subprocess_events_through_reader_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() reads events from the subprocess via the reader thread and delivers them."""
    _setup_main_env(tmp_path, monkeypatch)
    capture = _MessageCapture()
    stop_event = threading.Event()

    events = [_make_event_line("evt-realtime-check", timestamp="2026-03-01T12:00:00Z")]

    call_count = 0

    def fake_start_subprocess(agent_id: str, cel_filter: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeEventsProcess(events)
        stop_event.set()
        return _FakeEventsProcess([])

    thread = threading.Thread(
        target=main,
        kwargs={
            "start_subprocess": fake_start_subprocess,
            "stop_event": stop_event,
            "send_message": capture,
        },
        daemon=True,
    )
    thread.start()

    capture.wait_for_call(timeout=5.0)
    stop_event.set()
    thread.join(timeout=3.0)

    assert len(capture.calls) >= 1
    _, message = capture.calls[0]
    assert "Please process all events in " in message


# -- _run_event_batch_filter_command tests --


def test_run_event_batch_filter_command_passes_lines_through_identity_command(tmp_path: Path) -> None:
    """A filter command that cats stdin should return the same lines."""
    filter_command = create_executable_command(tmp_path, "identity_filter.sh", "#!/bin/bash\ncat\n")

    lines = ['{"source":"a","event_id":"1"}', '{"source":"b","event_id":"2"}']
    result = _run_event_batch_filter_command(lines, filter_command)
    assert result is not None
    assert len(result) == 2
    assert result[0] == '{"source":"a","event_id":"1"}'
    assert result[1] == '{"source":"b","event_id":"2"}'


def test_run_event_batch_filter_command_allows_filtering_to_empty(tmp_path: Path) -> None:
    """A filter command that outputs empty lines for all events."""
    filter_command = create_executable_command(
        tmp_path, "drop_all_filter.sh", '#!/bin/bash\nwhile IFS= read -r line; do echo ""; done\n'
    )

    lines = ['{"source":"a"}', '{"source":"b"}']
    result = _run_event_batch_filter_command(lines, filter_command)
    assert result is not None
    assert len(result) == 2
    assert result[0] == ""
    assert result[1] == ""


def test_run_event_batch_filter_command_replaces_filtered_events_with_empty_dict(tmp_path: Path) -> None:
    """A filter command can output '{}' to indicate a filtered event."""
    filter_command = create_executable_command(
        tmp_path, "replace_with_empty_dict.sh", '#!/bin/bash\nwhile IFS= read -r line; do echo "{}"; done\n'
    )

    lines = ['{"source":"a"}']
    result = _run_event_batch_filter_command(lines, filter_command)
    assert result is not None
    assert len(result) == 1
    assert result[0] == "{}"


def test_run_event_batch_filter_command_returns_none_for_missing_command() -> None:
    """A non-existent command should return None."""
    result = _run_event_batch_filter_command(['{"a":1}'], "/nonexistent/filter_42983.sh")
    assert result is None


def test_run_event_batch_filter_command_returns_none_for_nonzero_exit(tmp_path: Path) -> None:
    """A command that exits non-zero should return None."""
    filter_command = create_executable_command(tmp_path, "failing_filter.sh", "#!/bin/bash\nexit 1\n")

    result = _run_event_batch_filter_command(['{"a":1}'], filter_command)
    assert result is None


def test_run_event_batch_filter_command_returns_none_for_wrong_line_count(tmp_path: Path) -> None:
    """A command that outputs a different number of lines should return None."""
    filter_command = create_executable_command(tmp_path, "bad_count_filter.sh", '#!/bin/bash\necho "only one"\n')

    lines = ['{"a":1}', '{"b":2}', '{"c":3}']
    result = _run_event_batch_filter_command(lines, filter_command)
    assert result is None


# -- _apply_event_batch_filter tests --


def test_apply_event_batch_filter_drops_empty_and_empty_dict_lines(tmp_path: Path) -> None:
    """Lines that are empty or '{}' should be removed from the result."""
    filter_command = create_executable_command(
        tmp_path,
        "selective_filter.sh",
        '#!/bin/bash\nread line1; echo "$line1"\nread line2; echo ""\nread line3; echo "{}"\n',
    )

    lines = ['{"source":"keep"}', '{"source":"drop1"}', '{"source":"drop2"}']
    result = _apply_event_batch_filter(lines, filter_command)
    assert len(result) == 1
    assert result[0] == '{"source":"keep"}'


def test_apply_event_batch_filter_prepends_error_event_on_command_failure(tmp_path: Path) -> None:
    """If the command fails, a filter_error event is prepended to the original lines."""
    filter_command = create_executable_command(tmp_path, "failing_filter.sh", "#!/bin/bash\nexit 1\n")

    lines = ['{"source":"a"}', '{"source":"b"}']
    result = _apply_event_batch_filter(lines, filter_command)
    assert len(result) == 3
    error_event = json.loads(result[0])
    assert error_event["type"] == "filter_error"
    assert error_event["source"] == "mind/filter_error"
    assert error_event["filter_command"] == filter_command
    assert result[1:] == lines


def test_apply_event_batch_filter_returns_empty_when_all_filtered(tmp_path: Path) -> None:
    """When all events are filtered out, the result should be empty."""
    filter_command = create_executable_command(
        tmp_path, "drop_all.sh", '#!/bin/bash\nwhile IFS= read -r line; do echo ""; done\n'
    )

    lines = ['{"source":"a"}', '{"source":"b"}']
    result = _apply_event_batch_filter(lines, filter_command)
    assert result == []


def test_apply_event_batch_filter_can_modify_event_content(tmp_path: Path) -> None:
    """The command can modify event content (e.g. strip fields)."""
    filter_command = create_executable_command(
        tmp_path,
        "strip_fields.sh",
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        '        print("")\n'
        "        continue\n"
        "    obj = json.loads(line)\n"
        '    obj.pop("data", None)\n'
        "    print(json.dumps(obj))\n",
    )

    lines = [
        '{"source":"a","data":"big_payload","event_id":"1"}',
        '{"source":"b","event_id":"2"}',
    ]
    result = _apply_event_batch_filter(lines, filter_command)
    assert len(result) == 2
    parsed_first = json.loads(result[0])
    assert "data" not in parsed_first
    assert parsed_first["source"] == "a"
    parsed_second = json.loads(result[1])
    assert parsed_second["source"] == "b"
