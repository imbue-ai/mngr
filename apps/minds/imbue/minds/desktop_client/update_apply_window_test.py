"""The apply window: what it silences, what opens and closes it, and what it hands back."""

import json
import subprocess
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import landed_verdict
from imbue.minds.desktop_client.testing import make_update_state_store
from imbue.minds.desktop_client.testing import update_run_probe_stdout
from imbue.minds.desktop_client.update_apply_window import APPLY_GRACE_SECONDS
from imbue.minds.desktop_client.update_apply_window import RUN_STATUS_PATH
from imbue.minds.desktop_client.update_apply_window import UpdateAgentLiveness
from imbue.minds.desktop_client.update_apply_window import UpdateApplyWindowManager
from imbue.minds.desktop_client.update_apply_window import build_update_run_probe_args
from imbue.minds.desktop_client.update_apply_window import parse_update_run_probe
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId

_CHAT = "update-abc123"


def applying_record_json(apply_updated_at: datetime | None = None, **fields: object) -> str:
    """A run record mid-apply, restamped at ``apply_updated_at``."""
    payload: dict[str, object] = {"chat_agent_name": _CHAT, "apply_phase": "merged", **fields}
    if apply_updated_at is not None:
        payload["apply_updated_at"] = apply_updated_at.timestamp()
    return json.dumps(payload) + "\n"


_NO_RUN_STDOUT = update_run_probe_stdout()
_APPLYING_STDOUT = update_run_probe_stdout(run=applying_record_json(), agents=f"{_CHAT}\tRUNNING\n")


class _RestartRecorder:
    """Stands in for ``dispatch_host_restart`` so the hand-off is observable."""

    def __init__(self) -> None:
        self.dispatched: list[AgentId] = []

    def __call__(self, agent_id: AgentId) -> None:
        self.dispatched.append(agent_id)


def _make_manager(
    root_concurrency_group: ConcurrencyGroup,
    tmp_path: Path,
    *,
    probe_stdout: str = _NO_RUN_STDOUT,
    fallback_window_seconds: float = 300.0,
) -> tuple[UpdateApplyWindowManager, SystemInterfaceHealthTracker, WorkspaceUpdateStateStore, _RestartRecorder]:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    store = make_update_state_store(tmp_path)
    restarts = _RestartRecorder()
    manager = UpdateApplyWindowManager(
        tracker=tracker,
        store=store,
        mngr_caller=RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=probe_stdout)),
        concurrency_group=root_concurrency_group,
        dispatch_restart=restarts,
        fallback_window_seconds=fallback_window_seconds,
    )
    return manager, tracker, store, restarts


def _begin_run(store: WorkspaceUpdateStateStore, agent_id: AgentId) -> None:
    """Put a row in flight with the chat name the probe fixtures use."""
    assert store.try_begin_run(agent_id, chat_agent_name=_CHAT)
    store.set_activity(agent_id, UpdateActivity.RUNNING)


def _is_failure_suppressed(tracker: SystemInterfaceHealthTracker, agent_id: AgentId) -> bool:
    """With the zero stuck threshold, one failure plus one probe failure is STUCK unless a grace holds."""
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    return tracker.get_health(agent_id) is AgentHealth.HEALTHY


# -- The probe -------------------------------------------------------------


@pytest.mark.witnesses("workspace-updates.unreachable-workspace-stays-updating")
def test_a_probe_that_never_ran_is_not_read_as_no_run() -> None:
    probe = parse_update_run_probe("", _CHAT)

    assert probe.is_probe_answered is False
    assert probe.is_apply_in_progress is False
    assert probe.is_run_alive is True


def test_a_broken_in_container_mngr_leaves_the_agent_liveness_unknown() -> None:
    """An apply replaces the vendored mngr under itself, so its failure is expected mid-apply."""
    probe = parse_update_run_probe(update_run_probe_stdout(run=applying_record_json(), agents=None), _CHAT)

    assert probe.is_apply_in_progress is True
    assert probe.agent_liveness is UpdateAgentLiveness.UNKNOWN
    assert probe.is_run_alive is True


def test_no_apply_and_no_update_agent_is_the_one_reading_that_means_stalled() -> None:
    probe = parse_update_run_probe(_NO_RUN_STDOUT, _CHAT)

    assert probe.agent_liveness is UpdateAgentLiveness.GONE
    assert probe.is_run_alive is False


def test_a_live_update_agent_keeps_the_run_alive_without_an_apply() -> None:
    probe = parse_update_run_probe(update_run_probe_stdout(agents=f"{_CHAT}\tRUNNING\n"), _CHAT)

    assert probe.agent_liveness is UpdateAgentLiveness.ALIVE
    assert probe.is_run_alive is True


def test_a_run_with_no_chat_name_reads_unknown_rather_than_gone() -> None:
    probe = parse_update_run_probe(update_run_probe_stdout(agents="some-chat\tRUNNING\n"), "")

    assert probe.agent_liveness is UpdateAgentLiveness.UNKNOWN
    assert probe.is_run_alive is True


@pytest.mark.parametrize("ended_state", ("DONE", "STOPPED"))
def test_an_agent_positively_ended_reads_as_gone(ended_state: str) -> None:
    """STOPPED is what an agent with no tmux session reports, i.e. what a container restart leaves behind."""
    probe = parse_update_run_probe(update_run_probe_stdout(agents=f"{_CHAT}\t{ended_state}\n"), _CHAT)

    assert probe.agent_liveness is UpdateAgentLiveness.GONE
    assert probe.is_run_alive is False


@pytest.mark.parametrize(
    ("agent_lines", "is_waiting"),
    (
        (f"{_CHAT}\tWAITING\n", True),
        (f"other-chat\tRUNNING\n{_CHAT}\tWAITING\n", True),
        (f"{_CHAT}\tRUNNING\n", False),
        # An unparsed state reads as running, not waiting.
        (f"{_CHAT}\n", False),
    ),
)
def test_waiting_is_read_from_the_runs_own_chat_agent(agent_lines: str, is_waiting: bool) -> None:
    probe = parse_update_run_probe(update_run_probe_stdout(agents=agent_lines), _CHAT)

    assert probe.agent_liveness is UpdateAgentLiveness.ALIVE
    assert probe.is_agent_waiting is is_waiting


def _delegated_record_json(chat_agent_name: str = _CHAT, worker: str = "update-self") -> str:
    return json.dumps({"chat_agent_name": chat_agent_name, "worker_agent_name": worker}) + "\n"


@pytest.mark.witnesses("workspace-updates.idle-lead-with-a-working-worker-is-not-waiting")
@pytest.mark.parametrize(
    ("worker_line", "is_waiting"),
    (
        ("update-self\tRUNNING\n", False),
        ("update-self\n", False),
        ("update-self\tWAITING\n", True),
        ("update-self\tDONE\n", True),
        ("update-self\tSTOPPED\n", True),
        ("", True),
    ),
)
def test_an_idle_lead_reads_as_waiting_only_when_its_named_worker_is_not_moving(
    worker_line: str, is_waiting: bool
) -> None:
    stdout = update_run_probe_stdout(run=_delegated_record_json(), agents=f"{_CHAT}\tWAITING\n{worker_line}")
    probe = parse_update_run_probe(stdout, _CHAT)

    assert probe.agent_liveness is UpdateAgentLiveness.ALIVE
    assert probe.is_agent_waiting is is_waiting


def test_a_worker_named_by_another_runs_record_does_not_vouch_for_this_one() -> None:
    stdout = update_run_probe_stdout(
        run=_delegated_record_json(chat_agent_name="update-older"),
        agents=f"{_CHAT}\tWAITING\nupdate-self\tRUNNING\n",
    )
    probe = parse_update_run_probe(stdout, _CHAT)

    assert probe.is_agent_waiting is True


def test_the_run_record_carries_the_apply_and_its_restamp() -> None:
    restamped_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    run = applying_record_json(
        restamped_at,
        started_at=1700000000.0,
        verdict="UPDATED",
        resulting_ref="minds-v0.4.1",
    )
    probe = parse_update_run_probe(update_run_probe_stdout(run=run, agents=f"{_CHAT}\tRUNNING\n"), _CHAT)

    assert probe.run_status is not None
    assert probe.run_status.chat_agent_name == _CHAT
    assert probe.run_status.verdict is UpdateVerdict.UPDATED
    assert probe.is_apply_in_progress is True
    assert probe.apply_updated_at is not None
    assert abs((probe.apply_updated_at - restamped_at).total_seconds()) < 1.0


def test_an_apply_with_no_readable_restamp_is_still_under_way() -> None:
    run = json.dumps({"chat_agent_name": _CHAT, "apply_phase": "merged", "apply_updated_at": "soon"}) + "\n"
    probe = parse_update_run_probe(update_run_probe_stdout(run=run), _CHAT)

    assert probe.is_apply_in_progress is True
    assert probe.apply_updated_at is None


def test_the_probe_script_frames_a_record_with_no_trailing_newline(tmp_path: Path) -> None:
    """Without a trailing newline the end sentinel would glue onto the closing brace and read as unanswered."""
    record = tmp_path / RUN_STATUS_PATH
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"chat_agent_name": _CHAT, "apply_phase": "merged"}))
    script = build_update_run_probe_args(AgentId.generate())[3]

    # A bare PATH so the listing half cannot find a real `mngr`.
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True
    )
    probe = parse_update_run_probe(result.stdout, _CHAT)

    assert probe.is_probe_answered is True
    assert probe.is_apply_in_progress is True
    assert probe.agent_liveness is UpdateAgentLiveness.UNKNOWN


def test_a_hold_in_the_record_parses_with_its_detail() -> None:
    run = json.dumps({"chat_agent_name": _CHAT, "is_holding": True, "hold_detail": "Your widget"}) + "\n"
    probe = parse_update_run_probe(update_run_probe_stdout(run=run, agents=f"{_CHAT}\tWAITING\n"), _CHAT)

    assert probe.run_status is not None
    assert probe.run_status.is_holding is True
    assert probe.run_status.hold_detail == "Your widget"
    assert probe.is_apply_in_progress is False


# -- The window ------------------------------------------------------------


@pytest.mark.witnesses("workspace-updates.apply-outage-is-expected")
def test_opening_the_window_stops_probe_failures_reaching_stuck(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, tracker, store, _ = _make_manager(root_concurrency_group, tmp_path)
    agent_id = AgentId.generate()
    store.set_activity(agent_id, UpdateActivity.RUNNING)

    manager.open_window(agent_id)
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    tracker.record_probe_failure(agent_id)

    assert tracker.get_health(agent_id) is AgentHealth.HEALTHY
    assert store.get(agent_id).activity is UpdateActivity.APPLYING


def test_the_terminal_verdict_closes_the_window(root_concurrency_group: ConcurrencyGroup, tmp_path: Path) -> None:
    manager, tracker, _, _ = _make_manager(root_concurrency_group, tmp_path)
    agent_id = AgentId.generate()
    manager.open_window(agent_id)

    manager.close_window(agent_id, reason="terminal verdict")

    assert manager.is_window_open(agent_id) is False
    assert _is_failure_suppressed(tracker, agent_id) is False


def test_a_probe_success_ends_the_grace_so_normal_accounting_resumes(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, tracker, _, _ = _make_manager(root_concurrency_group, tmp_path)
    agent_id = AgentId.generate()
    manager.open_window(agent_id)

    tracker.record_probe_success(agent_id)

    assert _is_failure_suppressed(tracker, agent_id) is False


def test_re_arming_an_open_window_extends_it_rather_than_stacking(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, tracker, _, _ = _make_manager(root_concurrency_group, tmp_path, fallback_window_seconds=-100.0)
    agent_id = AgentId.generate()
    manager.open_window(agent_id)
    assert manager.is_window_open(agent_id) is False

    manager.fallback_window_seconds = 300.0
    manager.open_window(agent_id)

    assert manager.is_window_open(agent_id) is True
    manager.run_expiry_pass()
    assert manager.is_window_open(agent_id) is True
    manager.close_window(agent_id, reason="terminal verdict")
    assert manager.is_window_open(agent_id) is False
    assert _is_failure_suppressed(tracker, agent_id) is False


def test_the_apply_restamp_sizes_the_window_by_the_templates_own_grace(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, _, _ = _make_manager(root_concurrency_group, tmp_path, fallback_window_seconds=-100.0)
    agent_id = AgentId.generate()

    manager.open_window(agent_id, apply_updated_at=datetime.now(timezone.utc))

    assert manager.is_window_open(agent_id) is True


def test_an_apply_already_past_its_grace_still_declines_this_instant(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, _, _ = _make_manager(root_concurrency_group, tmp_path, fallback_window_seconds=-100.0)
    agent_id = AgentId.generate()
    stale = datetime.now(timezone.utc) - timedelta(seconds=APPLY_GRACE_SECONDS * 3)

    manager.open_window(agent_id, apply_updated_at=stale)

    assert manager.is_window_open(agent_id) is True


def test_arming_the_window_after_the_verdict_landed_keeps_the_verdict(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """A verdict landing under the guard's probe is the later reading; APPLYING must not overwrite it."""
    manager, _, store, _ = _make_manager(root_concurrency_group, tmp_path)
    agent_id = AgentId.generate()
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    store.adopt_run_record(agent_id, landed_verdict(UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1"))

    manager.open_window(agent_id)

    assert store.get(agent_id).activity is UpdateActivity.IDLE
    assert store.get(agent_id).verdict is UpdateVerdict.UPDATED


# -- The race guard --------------------------------------------------------


def test_a_stuck_edge_with_no_update_in_flight_dispatches_normally(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, _, _ = _make_manager(root_concurrency_group, tmp_path)

    assert manager.should_decline_recovery_dispatch(AgentId.generate()) is False


@pytest.mark.witnesses("workspace-updates.prepare-outage-is-real")
def test_a_prepare_phase_outage_dispatches_recovery_normally(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, store, _ = _make_manager(root_concurrency_group, tmp_path, probe_stdout=_NO_RUN_STDOUT)
    agent_id = AgentId.generate()
    _begin_run(store, agent_id)

    assert manager.should_decline_recovery_dispatch(agent_id) is False
    assert manager.is_window_open(agent_id) is False


@pytest.mark.witnesses("workspace-updates.record-settles-the-race", partial="the apply-under-way answer only")
def test_an_apply_under_way_at_the_stuck_edge_arms_the_window_and_declines(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """The race path: the outage lands between polls and the stuck edge fires first."""
    manager, tracker, store, _ = _make_manager(root_concurrency_group, tmp_path, probe_stdout=_APPLYING_STDOUT)
    agent_id = AgentId.generate()
    _begin_run(store, agent_id)

    assert manager.should_decline_recovery_dispatch(agent_id) is True
    assert manager.is_window_open(agent_id) is True
    assert _is_failure_suppressed(tracker, agent_id) is True


@pytest.mark.witnesses("workspace-updates.record-settles-the-race", partial="the cannot-answer-either-way case only")
def test_a_stuck_edge_a_machine_cannot_answer_is_declined_rather_than_restarted(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, store, _ = _make_manager(root_concurrency_group, tmp_path, probe_stdout="")
    agent_id = AgentId.generate()
    _begin_run(store, agent_id)

    assert manager.should_decline_recovery_dispatch(agent_id) is True
    assert manager.is_window_open(agent_id) is True


def test_an_already_open_window_declines_without_probing(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, _, _ = _make_manager(root_concurrency_group, tmp_path, probe_stdout=_NO_RUN_STDOUT)
    agent_id = AgentId.generate()
    manager.open_window(agent_id)

    assert manager.should_decline_recovery_dispatch(agent_id) is True


def test_starting_the_expiry_loop_twice_runs_one_strand(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, _, _ = _make_manager(root_concurrency_group, tmp_path)

    try:
        manager.start()
        manager.start()

        assert [thread.name for thread in threading.enumerate() if thread.name == "update-apply-window"] == [
            "update-apply-window"
        ]
    finally:
        # The loop sleeps on the group's shutdown event; signal it so teardown does not wait an interval.
        root_concurrency_group.shutdown()


# -- Expiry ----------------------------------------------------------------


@pytest.mark.witnesses("workspace-updates.wedged-apply-recovered")
def test_expiry_with_the_machine_still_stuck_hands_off_to_a_restart(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, tracker, store, restarts = _make_manager(root_concurrency_group, tmp_path, fallback_window_seconds=-100.0)
    agent_id = AgentId.generate()
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    manager.open_window(agent_id)
    tracker.mark_stuck(agent_id)

    manager.run_expiry_pass()

    assert restarts.dispatched == [agent_id]
    assert manager.is_window_open(agent_id) is False
    assert store.get(agent_id).activity is UpdateActivity.RUNNING


def test_a_verdict_landing_as_the_window_expires_is_not_overwritten(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    """The expiry pass acts outside the lock, so a run can finish in between."""
    manager, _, store, _ = _make_manager(root_concurrency_group, tmp_path, fallback_window_seconds=-100.0)
    agent_id = AgentId.generate()
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    manager.open_window(agent_id)
    store.adopt_run_record(agent_id, landed_verdict(UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1"))

    manager.run_expiry_pass()

    assert store.get(agent_id).activity is UpdateActivity.IDLE
    assert store.get(agent_id).verdict is UpdateVerdict.UPDATED


def test_expiry_with_a_healthy_machine_hands_the_row_back_without_a_restart(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, store, restarts = _make_manager(root_concurrency_group, tmp_path, fallback_window_seconds=-100.0)
    agent_id = AgentId.generate()
    store.set_activity(agent_id, UpdateActivity.RUNNING)
    manager.open_window(agent_id)

    manager.run_expiry_pass()

    assert restarts.dispatched == []
    assert store.get(agent_id).activity is UpdateActivity.RUNNING


@pytest.mark.witnesses("workspace-updates.wedged-apply-recovered", partial="failure accounting restarts from nothing")
def test_an_expired_window_stops_suppressing_failure_accounting(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, tracker, _, _ = _make_manager(root_concurrency_group, tmp_path, fallback_window_seconds=-100.0)
    agent_id = AgentId.generate()
    manager.open_window(agent_id)

    manager.run_expiry_pass()
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)

    assert tracker.get_health(agent_id) is AgentHealth.STUCK


# -- Run liveness ----------------------------------------------------------


@pytest.mark.witnesses("workspace-updates.run-liveness-is-observed", partial="the positively-gone reading only")
def test_a_workspace_reporting_no_run_and_no_agent_is_unlocked(
    root_concurrency_group: ConcurrencyGroup, tmp_path: Path
) -> None:
    manager, _, store, _ = _make_manager(root_concurrency_group, tmp_path, probe_stdout=_NO_RUN_STDOUT)
    agent_id = AgentId.generate()
    store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.OUT_OF_DATE),
        current_version="minds-v0.3.9",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )
    _begin_run(store, agent_id)

    assert manager.probe_run(agent_id).is_run_alive is False
