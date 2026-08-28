"""Unit tests for SystemInterfaceHealthTracker."""

import threading
import time
from datetime import datetime
from datetime import timezone

import pytest

from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import BackendFailureRecorder
from imbue.minds.desktop_client.system_interface_health import HostRecoveryKind
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.system_interface_health import should_enroll_suspect_for_backend_failure
from imbue.minds.desktop_client.testing import make_sleep_tracker
from imbue.minds.desktop_client.testing import record_sleep_of
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason

# Short STUCK threshold so the probe-failure-run tests don't have to sleep 5s.
_FAST_THRESHOLD: float = 0.05


@pytest.mark.parametrize(
    "reason,status_code,expected",
    [
        # Connection-level failure (no HTTP status) enrolls.
        (SystemInterfaceBackendFailureReason.CONNECT_ERROR, None, True),
        (SystemInterfaceBackendFailureReason.SSE_EOF, None, True),
        # The causes split out of CONNECT_ERROR enroll exactly as it does. Two
        # are provably this device's fault and one says the host answered, but
        # none of them establishes whether the workspace itself is reachable --
        # only a probe does, so declining to enroll would trade a wrong label
        # for a machine minds never looks at again.
        (SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, None, True),
        (SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, None, True),
        (SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING, None, True),
        # Infrastructure 5xx: the backend is unreachable / not serving.
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 502, True),
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 503, True),
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 504, True),
        # Application errors: the backend is alive and responding, so they
        # don't enroll -- the background probe adjudicates a wedged backend.
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 500, False),
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 404, False),
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 401, False),
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 400, False),
        # UNRESOLVED means the forward has no route for the agent at all (a
        # cold-start warm-up or a genuinely-gone agent); a restart routes through
        # the forward so it cannot help either way. Never enroll on it -- even
        # though it carries a None status code that would otherwise enroll.
        (SystemInterfaceBackendFailureReason.UNRESOLVED, None, False),
        # STALLED reports a backend that has not answered yet -- the request is
        # still in flight. Enrolling is right (a wedged backend looks exactly
        # like this), and costs nothing when the backend was merely slow: the
        # probe answers 200 and clears the suspect flag.
        (SystemInterfaceBackendFailureReason.STALLED, None, True),
    ],
)
def test_should_enroll_suspect_for_backend_failure(
    reason: SystemInterfaceBackendFailureReason,
    status_code: int | None,
    expected: bool,
) -> None:
    assert should_enroll_suspect_for_backend_failure(reason, status_code) is expected


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
    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    assert tracker.get_failure_run_started_wall_at(aid) is None


def test_outage_onset_spans_the_episode_rather_than_the_failure_run() -> None:
    """The outage onset survives the restart attempts made during the outage.

    Recovery compares discovery snapshots against this to decide whether what
    the resolver reports describes the current outage or the world before it.
    The unattended start fires on the stuck edge, within a second of the
    machine wedging, and clears the failure *run* -- so a gate reading the run
    would go unguarded for the rest of the episode, since a new run only ever
    starts from HEALTHY. Only the machine answering again ends it.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    assert tracker.get_outage_started_wall_at(aid) is None

    _drive_to_stuck(tracker, aid)
    onset = tracker.get_outage_started_wall_at(aid)
    assert onset is not None
    assert onset == tracker.get_failure_run_started_wall_at(aid)

    # Neither restart outcome ends the outage: the machine still is not answering.
    tracker.mark_recovering(aid, HostRecoveryKind.START)
    assert tracker.get_outage_started_wall_at(aid) == onset
    tracker.mark_recovery_failed(aid, "the start step failed")
    assert tracker.get_outage_started_wall_at(aid) == onset

    # The machine answering does.
    tracker.record_probe_success(aid)
    assert tracker.get_outage_started_wall_at(aid) is None


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


def test_probe_failure_does_not_disturb_recovering_agent() -> None:
    """A failed probe while a restart is in flight must not flip the agent to STUCK."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    tracker.record_probe_failure(aid)
    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.RECOVERING
    assert seen == [AgentHealth.RECOVERING]


def test_mark_recovering_clears_pending_failure_run() -> None:
    """Starting a restart abandons any in-progress probe-failure run.

    After the restart the agent recovers; no leftover run may then re-stick it.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    tracker.record_probe_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY

    # A single fresh probe failure starts a brand-new run -- not enough yet.
    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_success_clears_recovering() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    tracker.record_probe_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_mark_stuck_rolls_back_recovering_and_fires_callback() -> None:
    """mark_stuck transitions RECOVERING -> STUCK and fires the change callback."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    assert tracker.get_health(aid) == AgentHealth.RECOVERING
    tracker.mark_stuck(aid)
    assert tracker.get_health(aid) == AgentHealth.STUCK
    assert seen == [AgentHealth.RECOVERING, AgentHealth.STUCK]


def test_mark_stuck_is_idempotent() -> None:
    """A second mark_stuck after the first does not re-fire the callback."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_stuck(aid)
    tracker.mark_stuck(aid)
    assert seen == [AgentHealth.STUCK]


def test_mark_recovery_failed_sets_state_and_carries_error() -> None:
    """mark_recovery_failed transitions to RECOVERY_FAILED and stores the reason."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    tracker.mark_recovery_failed(aid, "mngr start exited 1")

    assert tracker.get_health(aid) == AgentHealth.RECOVERY_FAILED
    assert tracker.get_last_recovery_error(aid) == "mngr start exited 1"
    assert seen == [AgentHealth.RECOVERING, AgentHealth.RECOVERY_FAILED]


def test_mark_recovery_failed_refires_with_updated_reason() -> None:
    """A second failure re-fires the callback even though the state is unchanged."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.mark_recovery_failed(aid, "first reason")
    tracker.mark_recovery_failed(aid, "second reason")

    assert tracker.get_last_recovery_error(aid) == "second reason"
    assert seen == [AgentHealth.RECOVERY_FAILED, AgentHealth.RECOVERY_FAILED]


def test_success_clears_recovery_failed_and_error() -> None:
    """A successful probe recovers a RECOVERY_FAILED agent and drops its error."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.mark_recovery_failed(aid, "boom")
    tracker.record_probe_success(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert tracker.get_last_recovery_error(aid) is None


def test_mark_recovering_clears_prior_recovery_error() -> None:
    """Starting a fresh restart attempt drops the previous failure reason."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.mark_recovery_failed(aid, "old failure")
    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)

    assert tracker.get_health(aid) == AgentHealth.RECOVERING
    assert tracker.get_last_recovery_error(aid) is None


def test_get_last_recovery_error_is_none_for_untracked_agent() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    assert tracker.get_last_recovery_error(AgentId.generate()) is None


def test_a_recorded_backend_outage_outlives_the_attempt_but_not_the_episode() -> None:
    """A rejected command's backend outage survives the next attempt and dies with the record.

    The recovery verdict reads this one *without* a freshness gate, and what makes
    that safe is this lifecycle. It must survive a fresh restart attempt, unlike
    the restart error beside it: it describes the backend rather than the attempt,
    and the next attempt is routed through that same backend. And it must not
    survive the machine answering, or a machine that later wedges for reasons of
    its own would be blamed on a backend that is, by then, fine.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    before = datetime.now(timezone.utc)

    tracker.record_backend_outage(aid, "docker", "Docker Desktop is manually paused.")
    tracker.mark_recovery_failed(aid, "This machine's backend is unreachable, so the restart could not run: paused")

    outage = tracker.get_backend_outage(aid)
    assert outage is not None
    assert outage.provider_name == "docker"
    assert outage.reason == "Docker Desktop is manually paused."
    # The stamp is what bounds the record's authority (the reader drops it once
    # the provider has a snapshot newer than this), so it has to be the moment
    # the rejection was observed.
    assert outage.observed_at >= before

    # The user retries: the attempt's own reason is superseded, the backend's is not.
    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    assert tracker.get_last_recovery_error(aid) is None
    assert tracker.get_backend_outage(aid) == outage

    # Dropped with the record itself, which is also the untracked-agent read.
    tracker.record_probe_success(aid)
    assert tracker.get_backend_outage(aid) is None


def test_get_recovery_kind_reports_the_kind_and_is_scoped_to_recovering() -> None:
    """The recovery kind is readable only while RECOVERING and survives a deduped claim.

    The recovery card renders "Restarting <machine>..." vs "Reconnecting to
    <machine>..." off it. A RESTART must not be rewritten by a deduped later
    START, and the kind must not leak out of the episode (a subsequent HEALTHY
    reads None again).
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    # Untracked / non-recovering agents report no kind.
    assert tracker.get_recovery_kind(aid) is None

    # A full manual bounce wins the claim and records itself as a RESTART.
    assert tracker.mark_recovering(aid, HostRecoveryKind.RESTART) is True
    assert tracker.get_recovery_kind(aid) is HostRecoveryKind.RESTART

    # A deduped later request (returns False) must not rewrite the kind of the
    # episode already in flight -- the first winner's worker is the one running.
    assert tracker.mark_recovering(aid, HostRecoveryKind.START) is False
    assert tracker.get_recovery_kind(aid) is HostRecoveryKind.RESTART

    # Scoped to RECOVERING: once recovered the kind reads None again.
    tracker.record_probe_success(aid)
    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert tracker.get_recovery_kind(aid) is None

    # The unattended entry dispatch records a START.
    assert tracker.mark_recovering(aid, HostRecoveryKind.START) is True
    assert tracker.get_recovery_kind(aid) is HostRecoveryKind.START


def test_remove_on_change_callback() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []

    def cb(_a: AgentId, h: AgentHealth) -> None:
        seen.append(h)

    tracker.add_on_change_callback(cb)
    tracker.remove_on_change_callback(cb)
    tracker.remove_on_change_callback(cb)

    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    assert seen == []


def test_snapshot_all_omits_healthy_and_suspect_agents() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    a1 = AgentId.generate()
    a2 = AgentId.generate()

    tracker.mark_recovering(a1, HostRecoveryKind.RESTART)
    # a2 is suspect (enrolled by an envelope) but still HEALTHY.
    tracker.record_failure(a2)

    assert tracker.snapshot_all() == {a1: AgentHealth.RECOVERING}


def test_snapshot_probe_targets_includes_suspect_stuck_and_recovery_failed() -> None:
    """Probe targets are the agents the bg loop is responsible for recovering.

    RECOVERING agents are deliberately excluded -- the recovery worker owns the
    decision for those, via the readiness probe it runs once its commands
    return. A RESTART is where a second opinion does damage: a bg probe during
    the gap between ``mark_recovering`` and the worker's ``mngr stop`` would
    observe the pre-stop system interface as still healthy and prematurely flip
    the agent back to HEALTHY.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    suspect = AgentId.generate()
    stuck = AgentId.generate()
    recovery_failed = AgentId.generate()
    recovering = AgentId.generate()
    recovered = AgentId.generate()

    tracker.record_failure(suspect)
    tracker.mark_stuck(stuck)
    tracker.mark_recovery_failed(recovery_failed, "boom")
    tracker.mark_recovering(recovering, HostRecoveryKind.RESTART)
    tracker.record_failure(recovered)
    tracker.record_probe_success(recovered)

    assert tracker.snapshot_probe_targets() == frozenset({suspect, stuck, recovery_failed})


def test_snapshot_probe_targets_excludes_recovering_agents() -> None:
    """RECOVERING agents are never probed by the background loop.

    Regression for the race where a bg probe between ``mark_recovering`` and
    the restart worker's ``mngr stop`` actually tearing down the backend would
    see the old system interface as healthy and call ``record_probe_success``,
    flipping the agent prematurely to HEALTHY -- which the recovery page then
    302'd back to the about-to-disappear machine.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD)
    aid = AgentId.generate()

    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)

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

    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    assert seen == [AgentHealth.RECOVERING]


# ---------------------------------------------------------------------------
# CreateAttempt grace (initial-create-attempt STUCK suppression)
# ---------------------------------------------------------------------------


def test_create_attempt_grace_suppresses_probe_failures_entirely() -> None:
    """While a create attempt grace is active, probe failures never drive STUCK.

    A zero stuck-threshold would otherwise flip the agent STUCK on the very
    first probe failure; with the grace active, no failure run even starts.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.begin_create_attempt_grace(agent_id, time.monotonic() + 60.0)

    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    tracker.record_probe_failure(agent_id)

    assert tracker.get_health(agent_id) == AgentHealth.HEALTHY
    assert agent_id not in tracker.snapshot_all()


def test_expired_create_attempt_grace_no_longer_suppresses_stuck() -> None:
    """Once the grace deadline passes, the normal stuck accounting applies."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.begin_create_attempt_grace(agent_id, time.monotonic() - 1.0)

    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)

    assert tracker.get_health(agent_id) == AgentHealth.STUCK


def test_end_create_attempt_grace_restores_normal_probe_accounting() -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.begin_create_attempt_grace(agent_id, time.monotonic() + 60.0)
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.HEALTHY

    tracker.end_create_attempt_grace(agent_id)
    tracker.record_probe_failure(agent_id)

    assert tracker.get_health(agent_id) == AgentHealth.STUCK


# -- intentional-stop suppression --


def test_a_machine_that_answers_again_is_no_longer_a_deliberate_stop() -> None:
    """A probe success drops the intentional-stop marker.

    Entering a stopped machine from its row dispatches a start, which never
    goes through the in-app start that clears the marker. Without
    this, such a machine would run for the rest of the process's life with
    unattended recovery silently switched off.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.suppress_unattended_recovery(agent_id)

    tracker.record_probe_success(agent_id)

    assert tracker.is_unattended_recovery_suppressed(agent_id) is False


def test_a_stopped_machine_keeps_its_marker_while_it_stays_unreachable() -> None:
    """Only a machine that actually answers clears the marker.

    A stopped host cannot serve a probe, so the clear above can never fire for
    one -- the suppression has to survive exactly the failures a deliberate
    stop produces.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.suppress_unattended_recovery(agent_id)

    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)

    assert tracker.get_health(agent_id) == AgentHealth.STUCK
    assert tracker.is_unattended_recovery_suppressed(agent_id) is True


def test_a_probe_taken_while_the_stop_runs_does_not_clear_the_marker() -> None:
    """A stop's own command is the one window where a 200 is not the machine coming back.

    ``mngr stop`` blocks for tens of seconds (a cloud host, minutes) while the
    interface answers for the first of them, so a probe target being stopped
    gets polled mid-stop. Clearing on that 200 would hand the machine to the
    unattended dispatch while the stop is still running.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.suppress_unattended_recovery(agent_id, is_stop_in_flight=True)

    tracker.record_probe_success(agent_id)

    assert tracker.is_unattended_recovery_suppressed(agent_id) is True


def test_a_stop_that_has_returned_hands_the_marker_back_to_the_probes() -> None:
    """Closing the in-flight window restores the ordinary probe-cleared lifetime.

    Nothing can answer a probe once the stop has actually taken the host down,
    so a later 200 really is the machine back -- started by a route that never
    goes through the in-app start -- and must drop the marker again.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.suppress_unattended_recovery(agent_id, is_stop_in_flight=True)
    tracker.suppress_unattended_recovery(agent_id)

    tracker.record_probe_success(agent_id)

    assert tracker.is_unattended_recovery_suppressed(agent_id) is False


def test_probe_success_clears_create_attempt_grace() -> None:
    """A reachable machine drops its grace: later failures count normally."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    tracker.begin_create_attempt_grace(agent_id, time.monotonic() + 60.0)
    tracker.record_failure(agent_id)
    tracker.record_probe_success(agent_id)

    # The grace is gone, so a fresh failure run drives STUCK as usual.
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)

    assert tracker.get_health(agent_id) == AgentHealth.STUCK


def test_end_create_attempt_grace_is_idempotent_for_unknown_agent() -> None:
    tracker = SystemInterfaceHealthTracker()
    tracker.end_create_attempt_grace(AgentId.generate())


@pytest.mark.witnesses(
    "no-verdict-on-unobserved-time",
    partial="witnesses the stuck conviction only; the same rule binds every other verdict",
)
def test_failure_run_that_straddles_a_sleep_re_accumulates_from_the_wake() -> None:
    """The stuck threshold must be reached entirely while the process was running.

    Closing the lid mid-outage-check freezes the probe loop, so the seconds it
    slept were backed by no probe at all and cannot convict the workspace.
    """
    sleep_tracker, clock = make_sleep_tracker()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD, sleep_tracker=sleep_tracker)
    aid = AgentId.generate()
    seen: list[AgentHealth] = []
    tracker.add_on_change_callback(lambda _a, h: seen.append(h))

    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    outage_onset = tracker.get_outage_started_wall_at(aid)
    record_sleep_of(sleep_tracker, clock, seconds=900.0)

    # The first probe after the wake: the run looks old enough to convict, and
    # would have without the sleep signal.
    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.HEALTHY
    assert seen == []
    # The episode onset is not rewound with the run: the machine really did stop
    # answering when it did, and the freshness gate that reads it is only made
    # stricter by the older mark.
    assert tracker.get_outage_started_wall_at(aid) == outage_onset

    # The re-accumulated run convicts on its own, with no further sleep behind it.
    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.STUCK
    assert seen == [AgentHealth.STUCK]


def test_each_sleep_inside_one_outage_restarts_the_run_again() -> None:
    """Several naps during one outage each disqualify the run they interrupted."""
    sleep_tracker, clock = make_sleep_tracker()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD, sleep_tracker=sleep_tracker)
    aid = AgentId.generate()

    tracker.record_failure(aid)
    tracker.record_probe_failure(aid)
    for _ in range(3):
        record_sleep_of(sleep_tracker, clock, seconds=600.0)
        _sleep(_FAST_THRESHOLD + 0.02)
        tracker.record_probe_failure(aid)
        assert tracker.get_health(aid) == AgentHealth.HEALTHY

    _sleep(_FAST_THRESHOLD + 0.02)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.STUCK


def test_failure_run_after_a_sleep_convicts_unchanged() -> None:
    """A recorded interval that the run does not overlap suppresses nothing."""
    sleep_tracker, clock = make_sleep_tracker()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD, sleep_tracker=sleep_tracker)
    aid = AgentId.generate()

    record_sleep_of(sleep_tracker, clock, seconds=900.0)
    _drive_to_stuck(tracker, aid)

    assert tracker.get_health(aid) == AgentHealth.STUCK


def test_a_sleep_never_reopens_a_forced_stuck() -> None:
    """A machine that is already STUCK stays STUCK, whatever the sleep signal says.

    Held by the early return for any record that is not HEALTHY, which the sleep
    check sits behind and never gets past -- so the qualifier that a forced
    ``mark_stuck`` also carries no failure run to disqualify is a second reason
    rather than the operative one.
    """
    sleep_tracker, clock = make_sleep_tracker()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=_FAST_THRESHOLD, sleep_tracker=sleep_tracker)
    aid = AgentId.generate()

    tracker.mark_stuck(aid)
    record_sleep_of(sleep_tracker, clock, seconds=900.0)
    tracker.record_probe_failure(aid)

    assert tracker.get_health(aid) == AgentHealth.STUCK


# -- the classified cause of an episode's connection failures --


def test_repeated_envelopes_record_one_cause_per_episode() -> None:
    """The forward re-emits on every retry; the tracker holds one observation per cause.

    Roughly one envelope a second arrives for as long as a page keeps polling.
    What each repeat contributes is only that the cause is still happening, so it
    moves the last-seen mark and nothing else -- the detail the card renders must
    not be rewritten once a second for text that says the same thing.
    """
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()

    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "no known_hosts")
    first = tracker.get_connection_failure(aid)
    assert first is not None
    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "still no file")

    repeated = tracker.get_connection_failure(aid)
    assert repeated is not None
    assert repeated.reason == first.reason
    assert repeated.detail == "no known_hosts"
    # Strictly later, not merely not-earlier: the mark moving is the whole of
    # what a repeat contributes, and it is what decides how long the residual
    # cause defers to this one. A ``>=`` here would pass with the refresh gone.
    assert repeated.last_observed_at > first.last_observed_at


def test_a_changed_cause_replaces_the_recorded_one() -> None:
    """What is wrong can change mid-episode, and the surfaces must follow it.

    A tunnel that starts working while the pool stays exhausted is still a
    machine this device cannot reach, but a record pinned to the first cause
    would keep naming a fault that has been fixed.
    """
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()

    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "no known_hosts")
    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, "pool timeout")

    observation = tracker.get_connection_failure(aid)
    assert observation is not None
    assert observation.reason == SystemInterfaceBackendFailureReason.POOL_EXHAUSTED
    assert observation.detail == "pool timeout"


def test_the_residual_cause_does_not_displace_a_cause_that_is_still_happening() -> None:
    """An episode produces envelopes from several request paths at once.

    A pooled HTTP request reporting POOL_EXHAUSTED and a websocket handshake
    reporting the residual CONNECT_ERROR against the same machine arrive
    interleaved at the forward's retry cadence. If the residual one won, the
    device-side card would appear and disappear once a second. It is residual
    precisely because it establishes nothing, so it yields.
    """
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()
    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, "pool timeout")

    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.CONNECT_ERROR, "connection refused")

    observation = tracker.get_connection_failure(aid)
    assert observation is not None
    assert observation.reason == SystemInterfaceBackendFailureReason.POOL_EXHAUSTED
    # A different *established* cause still replaces it: that is a real change
    # in what is wrong, not an absence of information.
    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING, "refused")
    replaced = tracker.get_connection_failure(aid)
    assert replaced is not None
    assert replaced.reason == SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING


def test_the_residual_cause_takes_over_from_a_cause_that_has_fallen_silent() -> None:
    """A device-side fault that stops happening must stop explaining the outage.

    Both device-side causes are momentary: a pool refills, a socket that would
    not bind binds on the next try. If such a cause deferred forever, one blip
    during a machine outage would leave the card telling the user their machine
    is probably fine and withholding the start that would fix it -- the same
    misdiagnosis this decomposition exists to end, only pointed the other way.
    The forward keeps reporting a cause that is still happening, so silence for
    the deference window is what says it has stopped.
    """
    tracker = SystemInterfaceHealthTracker(established_cause_deference_seconds=0.0)
    aid = AgentId()
    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, "pool timeout")

    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.CONNECT_ERROR, "connection refused")

    observation = tracker.get_connection_failure(aid)
    assert observation is not None
    assert observation.reason == SystemInterfaceBackendFailureReason.CONNECT_ERROR
    assert observation.detail == "connection refused"


def test_the_recorded_cause_clears_when_the_machine_answers() -> None:
    """The cause describes one episode, so a probe that ends the episode ends it too."""
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()
    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, "pool timeout")

    tracker.record_probe_success(aid)

    assert tracker.get_connection_failure(aid) is None


def test_a_cause_that_outlives_its_episodes_is_logged_once_per_interval() -> None:
    """A failure that is not the system interface's own repeats across probe successes.

    The forward emits one envelope per *agent*, so any service on a healthy
    machine that stops listening reports a connection failure at the retry
    cadence while the machine answers every probe. Each success drops the
    episode's record, so the per-episode dedup above never engages and, without
    an interval, this logs (and breadcrumbs) once every couple of seconds for as
    long as the tab stays open -- burying the device-side incident the line
    exists to mark.
    """
    tracker = SystemInterfaceHealthTracker(connection_failure_log_interval_seconds=3600.0)
    aid = AgentId()

    with capture_loguru(level="INFO") as log_output:
        for _ in range(5):
            tracker.record_connection_failure(
                aid, SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING, "refused"
            )
            tracker.record_probe_success(aid)

    assert log_output.getvalue().count("classified as BACKEND_NOT_LISTENING") == 1
    # Rationing the log must not ration the evidence: the surfaces read the
    # record, and it is written on every envelope exactly as before.
    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING, "refused")
    observation = tracker.get_connection_failure(aid)
    assert observation is not None
    assert observation.reason == SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING


def test_a_different_cause_is_logged_at_once_however_recently_the_last_one_was() -> None:
    """The interval rations repetition, not news.

    Which of the causes it is decides whether the user's machine is implicated
    at all, so the transition between them is the one thing in this stream worth
    seeing immediately -- and it must not be swallowed by an interval that a
    preceding repeat happened to open.
    """
    tracker = SystemInterfaceHealthTracker(connection_failure_log_interval_seconds=3600.0)
    aid = AgentId()

    with capture_loguru(level="INFO") as log_output:
        tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING, "refused")
        tracker.record_probe_success(aid)
        tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "no file")

    assert "classified as BACKEND_NOT_LISTENING" in log_output.getvalue()
    assert "classified as TUNNEL_SETUP_FAILED" in log_output.getvalue()


def test_recording_a_cause_does_not_move_health() -> None:
    """Same contract as ``record_failure``: the probe loop is the only authority.

    An envelope is a hint. Letting the classified cause change health would
    make a single failed request enough to mark a machine STUCK, which is
    exactly what the probe threshold exists to prevent.
    """
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()

    tracker.record_connection_failure(aid, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "boom")

    assert tracker.get_health(aid) == AgentHealth.HEALTHY


def test_a_no_op_start_is_recorded_and_superseded_by_the_next_attempt() -> None:
    """A start that booted nothing is remembered until something else is tried.

    It is what stops the terminal state from claiming a failed restart. A fresh
    attempt has to clear it, or a real cold boot after a no-op would still be
    reported as one.
    """
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()
    assert tracker.is_recovery_a_no_op(aid) is False

    tracker.record_recovery_started_nothing(aid)
    assert tracker.is_recovery_a_no_op(aid) is True

    tracker.mark_recovering(aid, HostRecoveryKind.RESTART)
    assert tracker.is_recovery_a_no_op(aid) is False


# -- the envelope-to-tracker policy --


@pytest.mark.parametrize(
    "reason",
    [
        SystemInterfaceBackendFailureReason.CONNECT_ERROR,
        SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED,
        SystemInterfaceBackendFailureReason.POOL_EXHAUSTED,
        SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING,
    ],
)
def test_every_connection_class_envelope_both_enrolls_and_names_its_cause(
    reason: SystemInterfaceBackendFailureReason,
) -> None:
    """The four reasons that report a connection which never carried a response.

    They enroll identically -- none of them establishes whether the workspace is
    reachable, and only a probe does -- while each records a distinct cause, so
    what the surfaces claim can differ without what minds *checks* differing.
    ``BACKEND_NOT_LISTENING`` in particular changes no copy at all today; it is
    recorded so a log or a bug report can tell a dead service inside a reachable
    container from a container nothing could reach.
    """
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()

    BackendFailureRecorder(tracker=tracker)(aid, reason, None, "the error")

    assert aid in tracker.snapshot_probe_targets()
    observation = tracker.get_connection_failure(aid)
    assert observation is not None
    assert observation.reason == reason


@pytest.mark.parametrize(
    "reason, status_code",
    [
        # Mid-response: the connection demonstrably worked, so there is no
        # connection failure to attribute.
        (SystemInterfaceBackendFailureReason.SSE_EOF, None),
        # Still in flight -- it has not failed at all.
        (SystemInterfaceBackendFailureReason.STALLED, None),
        # The backend answered, so nothing here is about reaching it.
        (SystemInterfaceBackendFailureReason.ERROR_RESPONSE, 503),
    ],
)
def test_an_envelope_that_reached_the_backend_enrolls_without_naming_a_cause(
    reason: SystemInterfaceBackendFailureReason, status_code: int | None
) -> None:
    """Enrollment and cause-recording are separate questions, and answer separately here."""
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()

    BackendFailureRecorder(tracker=tracker)(aid, reason, status_code, None)

    assert aid in tracker.snapshot_probe_targets()
    assert tracker.get_connection_failure(aid) is None


def test_an_envelope_minds_ignores_never_touches_the_tracker() -> None:
    """``UNRESOLVED`` is a routeless warm-up a restart cannot help; it must change nothing."""
    tracker = SystemInterfaceHealthTracker()
    aid = AgentId()

    BackendFailureRecorder(tracker=tracker)(aid, SystemInterfaceBackendFailureReason.UNRESOLVED, None, None)

    assert tracker.snapshot_probe_targets() == frozenset()
    assert tracker.get_connection_failure(aid) is None
