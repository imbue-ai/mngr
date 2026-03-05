import time
from concurrent.futures import Future
from unittest.mock import MagicMock
from unittest.mock import patch

from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.tui import REFRESH_INTERVAL_SECONDS
from imbue.mng_kanpan.tui import _KanpanState
from imbue.mng_kanpan.tui import _finish_refresh
from imbue.mng_kanpan.tui import _request_refresh


def _make_state(**overrides: object) -> _KanpanState:
    """Build a _KanpanState with mocked urwid widgets and sensible defaults."""
    defaults = {
        "mng_ctx": MagicMock(),
        "frame": MagicMock(),
        "footer_left_text": MagicMock(),
        "footer_left_attr": MagicMock(),
        "footer_right": MagicMock(),
    }
    defaults.update(overrides)
    return _KanpanState.model_construct(**defaults)


def test_request_refresh_starts_immediately_when_cooldown_expired() -> None:
    loop = MagicMock()
    state = _make_state(last_refresh_time=time.monotonic() - 100)

    with patch("imbue.mng_kanpan.tui._start_refresh") as mock_start:
        _request_refresh(loop, state, cooldown_seconds=5.0)

    mock_start.assert_called_once_with(loop, state)


def test_request_refresh_defers_when_within_cooldown() -> None:
    loop = MagicMock()
    loop.set_alarm_in.return_value = "alarm_handle"
    state = _make_state(last_refresh_time=time.monotonic())

    with patch("imbue.mng_kanpan.tui._start_refresh") as mock_start:
        _request_refresh(loop, state, cooldown_seconds=60.0)

    mock_start.assert_not_called()
    loop.set_alarm_in.assert_called_once()
    delay = loop.set_alarm_in.call_args[0][0]
    assert 59.0 < delay <= 60.0
    assert state.deferred_refresh_alarm == "alarm_handle"


def test_request_refresh_replaces_deferred_with_sooner_alarm() -> None:
    """A manual refresh (short cooldown) should replace a pending auto refresh (long cooldown)."""
    loop = MagicMock()
    loop.set_alarm_in.return_value = "new_alarm"
    now = time.monotonic()
    state = _make_state(
        last_refresh_time=now - 2,
        deferred_refresh_alarm="old_alarm",
        deferred_refresh_fire_at=now + 58,
    )

    with patch("imbue.mng_kanpan.tui._start_refresh"):
        _request_refresh(loop, state, cooldown_seconds=5.0)

    loop.remove_alarm.assert_called_once_with("old_alarm")
    loop.set_alarm_in.assert_called_once()
    delay = loop.set_alarm_in.call_args[0][0]
    assert 2.0 < delay <= 3.0
    assert state.deferred_refresh_alarm == "new_alarm"


def test_request_refresh_keeps_existing_if_sooner() -> None:
    """An auto refresh request should not replace a sooner pending manual refresh."""
    loop = MagicMock()
    now = time.monotonic()
    state = _make_state(
        last_refresh_time=now - 2,
        deferred_refresh_alarm="existing_alarm",
        deferred_refresh_fire_at=now + 3,
    )

    with patch("imbue.mng_kanpan.tui._start_refresh"):
        _request_refresh(loop, state, cooldown_seconds=60.0)

    loop.remove_alarm.assert_not_called()
    loop.set_alarm_in.assert_not_called()
    assert state.deferred_refresh_alarm == "existing_alarm"


def test_request_refresh_noop_when_already_refreshing() -> None:
    loop = MagicMock()
    future: Future[BoardSnapshot] = Future()
    state = _make_state(refresh_future=future)

    with patch("imbue.mng_kanpan.tui._start_refresh") as mock_start:
        _request_refresh(loop, state, cooldown_seconds=0.0)

    mock_start.assert_not_called()
    loop.set_alarm_in.assert_not_called()


def test_finish_refresh_schedules_normal_interval_on_success() -> None:
    loop = MagicMock()
    snapshot = BoardSnapshot(entries=(), fetch_time_seconds=1.0)
    future: Future[BoardSnapshot] = Future()
    future.set_result(snapshot)
    state = _make_state(refresh_future=future)

    _finish_refresh(loop, state)

    assert state.snapshot == snapshot
    assert state.refresh_future is None
    loop.set_alarm_in.assert_called_once()
    delay = loop.set_alarm_in.call_args[0][0]
    assert delay == REFRESH_INTERVAL_SECONDS


def test_finish_refresh_uses_auto_cooldown_on_failure() -> None:
    """After a failed refresh, the next refresh should be deferred by auto_refresh_cooldown_seconds."""
    loop = MagicMock()
    loop.set_alarm_in.return_value = "deferred_alarm"
    future: Future[BoardSnapshot] = Future()
    future.set_exception(RuntimeError("GitHub API error"))
    state = _make_state(
        refresh_future=future,
        auto_refresh_cooldown_seconds=30.0,
    )

    _finish_refresh(loop, state)

    assert state.refresh_future is None
    assert state.deferred_refresh_alarm == "deferred_alarm"
    loop.set_alarm_in.assert_called_once()
    delay = loop.set_alarm_in.call_args[0][0]
    assert 29.0 < delay <= 30.0
