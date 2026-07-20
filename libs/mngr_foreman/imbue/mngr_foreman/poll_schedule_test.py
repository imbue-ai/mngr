"""Tests for the adaptive transcript-poll cadence."""

import threading
import time

from imbue.mngr_foreman.poll_schedule import ActivityTracker
from imbue.mngr_foreman.poll_schedule import FAST_POLL_SECONDS
from imbue.mngr_foreman.poll_schedule import FAST_WINDOW_SECONDS
from imbue.mngr_foreman.poll_schedule import IDLE_POLL_SECONDS
from imbue.mngr_foreman.poll_schedule import STEADY_POLL_SECONDS
from imbue.mngr_foreman.poll_schedule import interval_for


def test_interval_fast_inside_window_regardless_of_state() -> None:
    assert interval_for(now=5.0, fast_until=10.0, state="WAITING") == FAST_POLL_SECONDS
    assert interval_for(now=5.0, fast_until=10.0, state="RUNNING") == FAST_POLL_SECONDS
    assert interval_for(now=5.0, fast_until=10.0, state=None) == FAST_POLL_SECONDS


def test_interval_idle_when_waiting_past_window() -> None:
    assert interval_for(now=20.0, fast_until=10.0, state="WAITING") == IDLE_POLL_SECONDS
    assert interval_for(now=20.0, fast_until=10.0, state="waiting") == IDLE_POLL_SECONDS


def test_interval_steady_otherwise() -> None:
    assert interval_for(now=20.0, fast_until=10.0, state="RUNNING") == STEADY_POLL_SECONDS
    assert interval_for(now=20.0, fast_until=10.0, state=None) == STEADY_POLL_SECONDS


def test_tracker_poke_enters_fast_then_decays() -> None:
    tracker = ActivityTracker()
    tracker.poke("a", now=100.0)
    assert tracker.next_interval("a", "WAITING", now=100.0) == FAST_POLL_SECONDS
    assert tracker.next_interval("a", "WAITING", now=100.0 + FAST_WINDOW_SECONDS - 0.1) == FAST_POLL_SECONDS
    # Past the window a waiting agent decays to idle.
    assert tracker.next_interval("a", "WAITING", now=100.0 + FAST_WINDOW_SECONDS + 0.1) == IDLE_POLL_SECONDS


def test_tracker_repoke_extends_window() -> None:
    tracker = ActivityTracker()
    tracker.poke("a", now=100.0)
    tracker.poke("a", now=110.0)  # extends to 110 + window
    assert tracker.next_interval("a", "WAITING", now=120.0) == FAST_POLL_SECONDS


def test_tracker_unknown_agent_uses_state_default() -> None:
    tracker = ActivityTracker()
    assert tracker.next_interval("never-poked", "RUNNING", now=1.0) == STEADY_POLL_SECONDS
    assert tracker.next_interval("never-poked", "WAITING", now=1.0) == IDLE_POLL_SECONDS


def test_poke_wakes_a_waiting_loop_early() -> None:
    # A WAITING agent would otherwise sleep IDLE_POLL_SECONDS (~4s); a poke must
    # cut that short so a just-sent message is polled promptly.
    tracker = ActivityTracker()
    returned_after: list[float] = []

    def waiter() -> None:
        start = time.monotonic()
        tracker.wait_for_next_poll("a", "WAITING")
        returned_after.append(time.monotonic() - start)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    tracker.poke("a")
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert returned_after and returned_after[0] < IDLE_POLL_SECONDS  # woke well before idle timeout


def test_mark_running_sets_fast_without_waking() -> None:
    tracker = ActivityTracker()
    tracker.mark_running("a", now=100.0)
    # Fast cadence now applies...
    assert tracker.next_interval("a", "WAITING", now=100.0) == FAST_POLL_SECONDS
    # ...but no waker was set (mark_running must not wake the loop about to wait).
    waker = tracker._wakers.get("a")
    assert waker is None or not waker.is_set()
