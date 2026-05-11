"""Unit tests for WorkspaceServerHealthTracker."""

import threading
import time

from imbue.minds.desktop_client.workspace_server_health import AgentHealth
from imbue.minds.desktop_client.workspace_server_health import WorkspaceServerHealthTracker
from imbue.mngr.primitives import AgentId

_FAST_THRESHOLD: float = 0.05


def _wait_for(predicate, timeout: float = 1.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_default_health_is_healthy() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_single_failure_transitions_to_stuck_after_threshold() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[tuple[AgentId, AgentHealth]] = []
    tracker.add_on_change_callback(lambda a, h: seen.append((a, h)))

    tracker.record_failure(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY

    assert _wait_for(lambda: tracker.get_health(aid) == AgentHealth.STUCK)
    assert seen == [(aid, AgentHealth.STUCK)]


def test_success_before_threshold_keeps_healthy_and_cancels_timer() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)
    tracker.record_success(aid)

    time.sleep(_FAST_THRESHOLD * 4)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert seen == []


def test_success_after_stuck_transitions_back_to_healthy() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)
    assert _wait_for(lambda: tracker.get_health(aid) == AgentHealth.STUCK)

    tracker.record_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert seen == [AgentHealth.STUCK, AgentHealth.HEALTHY]


def test_mark_restarting_cancels_pending_stuck_timer() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)
    tracker.mark_restarting(aid)

    assert tracker.get_health(aid) == AgentHealth.RESTARTING

    time.sleep(_FAST_THRESHOLD * 4)
    assert tracker.get_health(aid) == AgentHealth.RESTARTING
    assert seen == [AgentHealth.RESTARTING]


def test_success_clears_restarting() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    tracker.mark_restarting(aid)
    tracker.record_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_repeated_failures_during_window_do_not_double_fire() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    for _ in range(5):
        tracker.record_failure(aid)

    assert _wait_for(lambda: tracker.get_health(aid) == AgentHealth.STUCK)
    assert seen == [AgentHealth.STUCK]


def test_remove_on_change_callback() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []

    def cb(_a: AgentId, h: AgentHealth) -> None:
        seen.append(h)

    tracker.add_on_change_callback(cb)
    tracker.remove_on_change_callback(cb)
    tracker.remove_on_change_callback(cb)

    tracker.mark_restarting(aid)
    assert seen == []


def test_snapshot_all_omits_healthy_agents() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    a1 = AgentId.generate()
    a2 = AgentId.generate()

    tracker.mark_restarting(a1)
    tracker.record_failure(a2)
    tracker.record_success(a2)

    snapshot = tracker.snapshot_all()
    assert snapshot == {a1: AgentHealth.RESTARTING}


def test_concurrent_failures_only_one_stuck_event() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    seen_lock = threading.Lock()

    def cb(_a: AgentId, h: AgentHealth) -> None:
        with seen_lock:
            seen.append(h)

    tracker.add_on_change_callback(cb)

    threads = [threading.Thread(target=tracker.record_failure, args=(aid,)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _wait_for(lambda: tracker.get_health(aid) == AgentHealth.STUCK)
    with seen_lock:
        assert seen == [AgentHealth.STUCK]


def test_callback_exception_does_not_break_subsequent_callbacks() -> None:
    tracker = WorkspaceServerHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []

    def bad_cb(_a: AgentId, _h: AgentHealth) -> None:
        raise ValueError("boom")

    def good_cb(_a: AgentId, h: AgentHealth) -> None:
        seen.append(h)

    tracker.add_on_change_callback(bad_cb)
    tracker.add_on_change_callback(good_cb)

    tracker.mark_restarting(aid)
    assert seen == [AgentHealth.RESTARTING]
