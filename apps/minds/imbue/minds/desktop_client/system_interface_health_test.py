"""Unit tests for SystemInterfaceHealthTracker."""

import threading
from datetime import datetime
from datetime import timezone

import pytest

from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.system_interface_health import should_enroll_suspect_for_backend_failure
from imbue.mngr.primitives import AgentId

# Short STUCK threshold so the probe-failure-run tests don't have to sleep 5s.
_FAST_THRESHOLD: float = 0.05


@pytest.mark.parametrize(
    "status_code,expected",
    [
        # Connection-level failure (no HTTP status) always enrolls.
        (None, True),
        # Infrastructure 5xx: the backend is unreachable / not serving.
        (502, True),
        (503, True),
        (504, True),
        # Application errors: the backend is alive and responding, so they
        # don't enroll -- the background probe adjudicates a wedged backend.
        (500, False),
        (404, False),
        (401, False),
        (400, False),
        # A success that somehow reached the failure path must not enroll.
        (200, False),
    ],
)
def test_should_enroll_suspect_for_backend_failure(status_code: int | None, expected: bool) -> None:
    assert should_enroll_suspect_for_backend_failure(status_code) is expected


def _sleep(seconds: float) -> None:
    threading.Event().wait(timeout=seconds)


def _drive_to_stuck(tracker: SystemInterfaceHealthTracker, aid: AgentId) -> None:
    """Drive ``aid`` to STUCK the way the probe loop would: an envelope, then a
    run of probe failures spanning the stuck threshold."""
    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)


def test_default_health_is_healthy() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_record_failure_enrolls_suspect_without_changing_health() -> None:
    """A failure envelope only enrolls the agent for probing -- it never sticks it."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    # The agent is a probe target (so the loop polls it) but is HEALTHY, so the
    # chrome auto-redirect (which reads snapshot_all) must not see it.
    assert aid in tracker.snapshot_probe_targets()
    assert aid not in tracker.snapshot_all()
    assert seen == []


def test_single_probe_failure_does_not_stick() -> None:
    """One probe failure starts the run but is not enough on its own for STUCK."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert seen == []


def test_sustained_probe_failures_transition_to_stuck() -> None:
    """A run of probe failures spanning the threshold transitions HEALTHY -> STUCK."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[tuple[AgentId, AgentHealth]] = []
    tracker.add_on_change_callback(lambda a, h: seen.append((a, h)))

    _drive_to_stuck(tracker, aid)

    assert tracker.get_health(aid) == AgentHealth.STUCK
    assert seen == [(aid, AgentHealth.STUCK)]


def test_failure_run_wall_onset_is_recorded_then_cleared() -> None:
    """The wall-clock outage onset is captured when the probe-failure run begins
    and cleared when the agent leaves the failing state.

    The recovery redirect compares this onset against discovery snapshot
    timestamps, so it must track the run: set on the first failure, gone once a
    restart supersedes it.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    # No probe-failure run yet -> no onset.
    assert tracker.get_failure_run_started_wall_at(aid) is None

    before = datetime.now(timezone.utc)
    _drive_to_stuck(tracker, aid)
    after = datetime.now(timezone.utc)

    onset = tracker.get_failure_run_started_wall_at(aid)
    assert onset is not None
    # Captured at the first probe failure, so it falls within the driven window.
    assert before <= onset <= after

    # A restart supersedes the run, clearing the onset.
    tracker.mark_restarting(aid)
    assert tracker.get_failure_run_started_wall_at(aid) is None


def test_probe_failure_without_record_is_ignored() -> None:
    """A probe failure for an agent that was never enrolled does nothing.

    The probe loop only polls enrolled agents, but a record can be dropped
    (by a concurrent recovering probe) between the snapshot and the probe.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.record_probe_failure(aid)
    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert aid not in tracker.snapshot_probe_targets()


def test_probe_success_clears_suspect_and_drops_record() -> None:
    """A reachable probe de-enrolls a suspect agent so the loop stops polling it."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)
    tracker.record_probe_success(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert aid not in tracker.snapshot_probe_targets()
    # The agent was HEALTHY throughout, so no transition callback fires.
    assert seen == []


def test_probe_success_resets_the_failure_run() -> None:
    """A reachable probe mid-run resets it, so STUCK requires a fresh full run.

    This is the spurious-recovery-flash guard: an ephemeral blip that briefly
    fails probing cannot accumulate toward STUCK once a later probe succeeds.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    _sleep(_FAST_THRESHOLD + 0.02)
    # A success here clears the run -- the elapsed time so far must not count.
    tracker.record_probe_success(aid)

    # Re-enroll and fail once more: the run restarts from zero, so a single
    # post-reset failure (even after the original window would have elapsed)
    # is not enough for STUCK.
    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert seen == []


def test_probe_success_after_stuck_transitions_back_to_healthy() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    _drive_to_stuck(tracker, aid)
    assert tracker.get_health(aid) == AgentHealth.STUCK

    tracker.record_probe_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert seen == [AgentHealth.STUCK, AgentHealth.HEALTHY]


def test_repeated_failure_envelopes_enroll_once() -> None:
    """Many failure envelopes for one agent are idempotent -- still one suspect."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    for _ in range(5):
        tracker.record_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert tracker.snapshot_probe_targets() == frozenset({aid})
    assert seen == []


def test_probe_failure_does_not_disturb_restarting_agent() -> None:
    """A failed probe while a restart is in flight must not flip the agent to STUCK."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_restarting(aid)
    tracker.record_probe_failure(aid)
    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.RESTARTING
    assert seen == [AgentHealth.RESTARTING]


def test_mark_restarting_clears_pending_failure_run() -> None:
    """Starting a restart abandons any in-progress probe-failure run.

    After the restart the agent recovers; no leftover run may then re-stick it.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    tracker.mark_restarting(aid)
    tracker.record_probe_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY

    # A single fresh probe failure starts a brand-new run -- not enough yet.
    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_success_clears_restarting() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    tracker.mark_restarting(aid)
    tracker.record_probe_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_mark_stuck_rolls_back_restarting_and_fires_callback() -> None:
    """mark_stuck transitions RESTARTING -> STUCK and fires the change callback."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_restarting(aid)
    assert tracker.get_health(aid) == AgentHealth.RESTARTING
    tracker.mark_stuck(aid)
    assert tracker.get_health(aid) == AgentHealth.STUCK
    assert seen == [AgentHealth.RESTARTING, AgentHealth.STUCK]


def test_mark_stuck_is_idempotent() -> None:
    """A second mark_stuck after the first does not re-fire the callback."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_stuck(aid)
    tracker.mark_stuck(aid)
    assert seen == [AgentHealth.STUCK]


def test_mark_restart_failed_sets_state_and_carries_error() -> None:
    """mark_restart_failed transitions to RESTART_FAILED and stores the reason."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_restarting(aid)
    tracker.mark_restart_failed(aid, "mngr start exited 1")

    assert tracker.get_health(aid) == AgentHealth.RESTART_FAILED
    assert tracker.get_last_restart_error(aid) == "mngr start exited 1"
    assert seen == [AgentHealth.RESTARTING, AgentHealth.RESTART_FAILED]


def test_mark_restart_failed_refires_with_updated_reason() -> None:
    """A second failure re-fires the callback even though the state is unchanged."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_restart_failed(aid, "first reason")
    tracker.mark_restart_failed(aid, "second reason")

    assert tracker.get_last_restart_error(aid) == "second reason"
    assert seen == [AgentHealth.RESTART_FAILED, AgentHealth.RESTART_FAILED]


def test_success_clears_restart_failed_and_error() -> None:
    """A successful probe recovers a RESTART_FAILED agent and drops its error."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.mark_restart_failed(aid, "boom")
    tracker.record_probe_success(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert tracker.get_last_restart_error(aid) is None


def test_mark_restarting_clears_prior_restart_error() -> None:
    """Starting a fresh restart attempt drops the previous failure reason."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.mark_restart_failed(aid, "old failure")
    tracker.mark_restarting(aid)

    assert tracker.get_health(aid) == AgentHealth.RESTARTING
    assert tracker.get_last_restart_error(aid) is None


def test_get_last_restart_error_is_none_for_untracked_agent() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    assert tracker.get_last_restart_error(AgentId.generate()) is None


def test_remove_on_change_callback() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []

    def cb(_a: AgentId, h: AgentHealth) -> None:
        seen.append(h)

    tracker.add_on_change_callback(cb)
    tracker.remove_on_change_callback(cb)
    tracker.remove_on_change_callback(cb)

    tracker.mark_restarting(aid)
    assert seen == []


def test_snapshot_all_omits_healthy_and_suspect_agents() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    a1 = AgentId.generate()
    a2 = AgentId.generate()

    tracker.mark_restarting(a1)
    # a2 is suspect (enrolled by an envelope) but still HEALTHY.
    tracker.record_failure(a2)

    assert tracker.snapshot_all() == {a1: AgentHealth.RESTARTING}


def test_snapshot_probe_targets_includes_suspect_stuck_and_restart_failed() -> None:
    """Probe targets are the agents the bg loop is responsible for recovering.

    RESTARTING agents are deliberately excluded -- the restart worker owns the
    recovery decision for those, since a bg probe during the gap between
    ``mark_restarting`` and the worker's ``mngr stop`` would observe the
    pre-restart system interface as still healthy and prematurely flip the
    agent back to HEALTHY.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    suspect = AgentId.generate()
    stuck = AgentId.generate()
    restart_failed = AgentId.generate()
    restarting = AgentId.generate()
    recovered = AgentId.generate()

    tracker.record_failure(suspect)
    tracker.mark_stuck(stuck)
    tracker.mark_restart_failed(restart_failed, "boom")
    tracker.mark_restarting(restarting)
    tracker.record_failure(recovered)
    tracker.record_probe_success(recovered)

    assert tracker.snapshot_probe_targets() == frozenset({suspect, stuck, restart_failed})


def test_snapshot_probe_targets_excludes_restarting_agents() -> None:
    """RESTARTING agents are never probed by the background loop.

    Regression for the race where a bg probe between ``mark_restarting`` and
    the restart worker's ``mngr stop`` actually tearing down the backend would
    see the old system interface as healthy and call ``record_probe_success``,
    flipping the agent prematurely to HEALTHY -- which the recovery page then
    302'd back to the about-to-disappear workspace.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.mark_restarting(aid)

    assert aid not in tracker.snapshot_probe_targets()
    # ...and even a prior failure envelope (which would normally enroll the
    # agent as a suspect probe target) does not pull it back into the loop
    # while the restart is in flight.
    tracker.record_failure(aid)
    assert aid not in tracker.snapshot_probe_targets()


def test_concurrent_failure_envelopes_then_one_stuck_event() -> None:
    """Concurrent failure envelopes enroll the agent once; a probe-failure run
    then produces exactly one STUCK event."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
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

    tracker.record_probe_failure(aid)
    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.STUCK
    with seen_lock:
        assert seen == [AgentHealth.STUCK]


def test_on_recovery_callback_fires_only_on_non_healthy_to_healthy() -> None:
    """The recovery callback fires on the STUCK -> HEALTHY transition, not on
    every HEALTHY observation.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    recovered: list[AgentId] = []
    tracker.add_on_recovery_callback(lambda a: recovered.append(a))

    # A probe-success for an agent the tracker never tracked is a no-op.
    tracker.record_probe_success(aid)
    assert recovered == []

    _drive_to_stuck(tracker, aid)
    assert tracker.get_health(aid) == AgentHealth.STUCK
    tracker.record_probe_success(aid)
    assert recovered == [aid]

    # A second probe-success against the now-HEALTHY agent must not refire.
    tracker.record_probe_success(aid)
    assert recovered == [aid]


def test_on_recovery_callback_exception_does_not_break_subsequent_callbacks() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentId] = []

    def bad_cb(_a: AgentId) -> None:
        raise ValueError("boom")

    def good_cb(a: AgentId) -> None:
        seen.append(a)

    tracker.add_on_recovery_callback(bad_cb)
    tracker.add_on_recovery_callback(good_cb)

    _drive_to_stuck(tracker, aid)
    tracker.record_probe_success(aid)
    assert seen == [aid]


def test_callback_exception_does_not_break_subsequent_callbacks() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
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
