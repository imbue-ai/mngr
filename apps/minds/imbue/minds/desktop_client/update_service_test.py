"""Starting, scheduling, and cancelling updates through the workspace's update command, and the run close-out."""

import json
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import CANCELLED_UPDATE_COMMAND_RESULT
from imbue.minds.desktop_client.testing import CREATED_UPDATE_COMMAND_RESULT
from imbue.minds.desktop_client.testing import CUT_OFF_ON_REACHED_HOST_EXEC_RESULT
from imbue.minds.desktop_client.testing import EXEC_CUT_OFF_REASON
from imbue.minds.desktop_client.testing import NEEDS_SIGN_IN_UPDATE_COMMAND_RESULT
from imbue.minds.desktop_client.testing import SYSTEM_SERVICES_PROVIDER_NAME
from imbue.minds.desktop_client.testing import UPDATE_CHAT_ID
from imbue.minds.desktop_client.testing import UPDATE_CHAT_NAME
from imbue.minds.desktop_client.testing import armed_update_schedule_file
from imbue.minds.desktop_client.testing import begin_dispatched_update_run
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import exec_result_stdout
from imbue.minds.desktop_client.testing import host_offline_exec_result
from imbue.minds.desktop_client.testing import make_update_state_store
from imbue.minds.desktop_client.testing import update_command_answer
from imbue.minds.desktop_client.testing import update_program_invocation
from imbue.minds.desktop_client.testing import update_run_probe_stdout
from imbue.minds.desktop_client.update_apply_window import UpdateApplyWindowManager
from imbue.minds.desktop_client.update_service import DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME
from imbue.minds.desktop_client.update_service import SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME
from imbue.minds.desktop_client.update_service import UpdateDispatchOutcome
from imbue.minds.desktop_client.update_service import UpdateScheduleOutcome
from imbue.minds.desktop_client.update_service import UpdateSettingsSource
from imbue.minds.desktop_client.update_service import WorkspaceUpdateService
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome
from imbue.minds.desktop_client.workspace_update_state import AutoUpdatePolicy
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateDetector
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_CEILING = "minds-v0.9.0"


class _InvalidationRecordingDetector(WorkspaceUpdateDetector):
    """A detector that remembers which workspaces had their cached version dropped."""

    _invalidated: list[AgentId] = PrivateAttr(default_factory=list)

    def invalidate_cached_version(self, agent_id: AgentId) -> None:
        self._invalidated.append(agent_id)
        super().invalidate_cached_version(agent_id)

    @property
    def invalidated(self) -> list[AgentId]:
        return self._invalidated


def _build_service(
    tmp_path: Path,
    concurrency_group: ConcurrencyGroup,
    *,
    caller: MngrCaller,
    store: WorkspaceUpdateStateStore | None = None,
    started: list[AgentId] | None = None,
    start_failure_reason: str | None = None,
    backend_resolver: MngrCliBackendResolver | None = None,
    minds_config: MindsConfig | None = None,
) -> WorkspaceUpdateService:
    """A service whose app values are fixed: ceiling ``_CEILING``, window 11 PM-3 AM, Los Angeles time."""
    store = store if store is not None else make_update_state_store(tmp_path)
    backend_resolver = backend_resolver if backend_resolver is not None else MngrCliBackendResolver()
    apply_window = UpdateApplyWindowManager(
        tracker=SystemInterfaceHealthTracker(),
        store=store,
        mngr_caller=caller,
        backend_resolver=backend_resolver,
        concurrency_group=concurrency_group,
        dispatch_recovery=lambda agent_id: None,
    )
    return WorkspaceUpdateService(
        state_store=store,
        detector=_InvalidationRecordingDetector(
            store=store,
            backend_resolver=backend_resolver,
            mngr_caller=caller,
            concurrency_group=concurrency_group,
            read_supported_version=lambda: _CEILING,
            read_update_facts=lambda agent_id, is_sign_in_checked: None,
            read_auto_update_policy=lambda agent_id: AutoUpdatePolicy(is_enabled=False, skipped_target_ref=""),
            arm_for_auto_update=lambda agent_id: None,
        ),
        apply_window=apply_window,
        mngr_caller=caller,
        backend_resolver=backend_resolver,
        settings_source=UpdateSettingsSource(
            read_ceiling=lambda: _CEILING,
            read_update_window=lambda: (23, 3),
            read_zone=lambda: "America/Los_Angeles",
            paths=None,
        ),
        minds_config=minds_config,
        start_workspace=_record_start(started if started is not None else [], failure_reason=start_failure_reason),
    )


def _record_start(started: list[AgentId], *, failure_reason: str | None) -> Callable[[AgentId], MindHostActionOutcome]:
    """A stand-in host start that records who it was asked for and reports the machine up, or why not."""

    def start(agent_id: AgentId) -> MindHostActionOutcome:
        started.append(agent_id)
        return MindHostActionOutcome(is_successful=failure_reason is None, failure_reason=failure_reason)

    return start


_HOST_OFFLINE = host_offline_exec_result()


def _armed(*, override_ref: str = "", is_auto_armed: bool = False) -> MngrCallResult:
    """An arm that landed, carrying the schedule file it wrote (window and zone as the workspace has them)."""
    schedule = armed_update_schedule_file(
        ceiling_ref=_CEILING,
        override_ref=override_ref,
        window=(23, 3),
        zone="Europe/Berlin",
        is_auto_armed=is_auto_armed,
    )
    return update_command_answer("ARMED", mode="arm", schedule=schedule)


_RUN_CHAT_ID = "agent-" + "c" * 32
_STOPPED_LEAD_LINE = f"update-old\tSTOPPED\t{_RUN_CHAT_ID}\t\n"
_STOPPED_LEADS_RUN = json.dumps({"chat_agent_name": "update-old"}) + "\n"


@pytest.mark.parametrize(
    ("probe_stdout", "known_chat_id", "activity", "is_chat_gone"),
    [
        # Its chat was deleted: the run is over, and there is no chat left to open.
        (update_run_probe_stdout(agents=""), _RUN_CHAT_ID, UpdateActivity.STALLED, True),
        # Unlisted, but the create never named its id: it may still be on its way, so it is not called deleted.
        (update_run_probe_stdout(agents=""), "", UpdateActivity.STALLED, False),
        # Stopped where nothing will restart it: over, but its chat is still there to open and resume.
        (
            update_run_probe_stdout(run=_STOPPED_LEADS_RUN, agents=_STOPPED_LEAD_LINE),
            _RUN_CHAT_ID,
            UpdateActivity.STALLED,
            False,
        ),
        # Stopped where the workspace's watchdog restarts it: still under way.
        (
            update_run_probe_stdout(run=_STOPPED_LEADS_RUN, agents=_STOPPED_LEAD_LINE, is_watchdog_installed=True),
            _RUN_CHAT_ID,
            UpdateActivity.RUNNING,
            False,
        ),
        # Stopped before it recorded its run: the watchdog has no run to restart it for.
        (
            update_run_probe_stdout(agents=_STOPPED_LEAD_LINE, is_watchdog_installed=True),
            _RUN_CHAT_ID,
            UpdateActivity.STALLED,
            False,
        ),
    ],
    ids=["deleted", "unlisted-without-id", "stopped-no-watchdog", "stopped-watchdog", "stopped-watchdog-unrecorded"],
)
@pytest.mark.witnesses("workspace-updates.stopped-agent-revived", partial="the app's report only")
@pytest.mark.witnesses("workspace-updates.gone-agent-closed-out", partial="the app's report only")
def test_a_run_whose_lead_is_not_running_ends_only_when_nothing_will_bring_it_back(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    probe_stdout: str,
    known_chat_id: str,
    activity: UpdateActivity,
    is_chat_gone: bool,
) -> None:
    """An ended run is the run's end as much as a verdict is, but it landed nothing."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=probe_stdout))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    landings_told: list[AgentId] = []
    service.add_on_update_landed_callback(lambda agent_id, run_started_at, ended_at: landings_told.append(agent_id))
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-old", chat_agent_id=known_chat_id)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert (state.activity, state.is_chat_gone) == (activity, is_chat_gone)
    assert state.chat_agent_id == ("" if is_chat_gone else known_chat_id)
    assert landings_told == []


def test_a_verdict_in_the_run_record_ends_the_run_and_re_reads_the_version(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    run = json.dumps(
        {
            "chat_agent_name": "update-x",
            "started_at": time.time() - 60.0,
            "verdict": "UPDATED",
            "resulting_ref": "minds-v0.4.1",
            "detail": "Landed cleanly.",
            "verdict_at": time.time(),
        }
    )
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0, stdout=update_run_probe_stdout(run=run + "\n", agents="update-x\tWAITING\n")
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-x")

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is UpdateVerdict.UPDATED
    assert state.verdict_detail == "Landed cleanly."
    assert state.success_note_version == "minds-v0.4.1"
    assert isinstance(service.detector, _InvalidationRecordingDetector)
    assert service.detector.invalidated == [agent_id]


def _poll_a_run_whose_record_reads(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, record: dict[str, object]
) -> tuple[WorkspaceUpdateService, AgentId, list[tuple[AgentId, datetime | None, datetime]]]:
    """Poll one in-flight run whose ``run.json`` is ``record``: the service, its id, and each (id, start, end) told."""
    chat_agent_name = str(record["chat_agent_name"])
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=update_run_probe_stdout(run=json.dumps(record) + "\n", agents=f"{chat_agent_name}\tWAITING\n"),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    told: list[tuple[AgentId, datetime | None, datetime]] = []
    service.add_on_update_landed_callback(
        lambda agent_id, run_started_at, ended_at: told.append((agent_id, run_started_at, ended_at))
    )
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, chat_agent_name)
    service.poll_in_flight_runs()
    return service, agent_id, told


@pytest.mark.parametrize("is_verdict_time_recorded", [True, False], ids=["recorded", "unreadable"])
def test_a_landed_update_tells_the_listeners_when_the_run_started_and_ended(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, is_verdict_time_recorded: bool
) -> None:
    """The listeners get the record's start, and its own verdict time or the reading's when the record has none."""
    started_at = time.time() - 120.0
    verdict_at = time.time() - 13.0
    record: dict[str, object] = {
        "chat_agent_name": "update-y",
        "started_at": started_at,
        "verdict": "UPDATED",
        "resulting_ref": "minds-v0.7.3",
    }
    if is_verdict_time_recorded:
        record["verdict_at"] = verdict_at

    polled_from = datetime.now(timezone.utc)
    _service, agent_id, told = _poll_a_run_whose_record_reads(tmp_path, root_concurrency_group, record)
    polled_until = datetime.now(timezone.utc)

    assert [(told_agent_id, run_started_at) for told_agent_id, run_started_at, _ in told] == [
        (agent_id, datetime.fromtimestamp(started_at, tz=timezone.utc))
    ]
    ended_at = told[0][2]
    if is_verdict_time_recorded:
        assert ended_at == datetime.fromtimestamp(verdict_at, tz=timezone.utc)
    else:
        assert polled_from <= ended_at <= polled_until


@pytest.mark.parametrize(
    "verdict",
    [
        UpdateVerdict.ALREADY_CURRENT,
        UpdateVerdict.REFUSED,
        UpdateVerdict.NEEDS_RECREATION,
        UpdateVerdict.STUCK,
        UpdateVerdict.NEEDS_DECISION,
    ],
)
def test_a_verdict_that_landed_nothing_tells_no_listener(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, verdict: UpdateVerdict
) -> None:
    """The workspace serves the build its views already loaded, so there is nothing to tell."""
    record: dict[str, object] = {"chat_agent_name": "update-z", "verdict": verdict.value, "verdict_at": time.time()}

    _service, _agent_id, told = _poll_a_run_whose_record_reads(tmp_path, root_concurrency_group, record)

    assert told == []


@pytest.mark.witnesses("workspace-updates.needs-decision", partial="the row state, not the modal")
@pytest.mark.parametrize(
    ("verdict", "is_decision_pending"),
    ((UpdateVerdict.NEEDS_DECISION, True), (UpdateVerdict.REFUSED, False)),
)
def test_a_needs_decision_verdict_ends_the_run_with_the_decision_pending_and_its_detail(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, verdict: UpdateVerdict, is_decision_pending: bool
) -> None:
    """The run is over -- nothing waits holding leases -- and the row says the user has a decision to make."""
    detail = "Your dashboard widget has no place in the new layout."
    record: dict[str, object] = {
        "chat_agent_name": "update-d",
        "started_at": time.time() - 60.0,
        "verdict": verdict.value,
        "detail": detail,
        "verdict_at": time.time(),
    }

    service, agent_id, _told = _poll_a_run_whose_record_reads(tmp_path, root_concurrency_group, record)

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.is_run_in_flight is False
    assert state.is_decision_pending is is_decision_pending
    assert state.verdict_detail == detail
    assert state.success_note_version == ""


def test_another_runs_record_does_not_close_this_run_out(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A verdict is attributed by chat name; an earlier run's record must not idle the new run's row."""
    run = json.dumps({"chat_agent_name": "update-old", "started_at": time.time() - 9000.0, "verdict": "REFUSED"})
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0, stdout=update_run_probe_stdout(run=run + "\n", agents="update-new\tRUNNING\n")
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-new")

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.verdict is None


def _probe_reading(chat_agent_name: str, *, started_at: float, verdict: str) -> MngrCallResult:
    run = json.dumps({"chat_agent_name": chat_agent_name, "started_at": started_at, "verdict": verdict})
    return MngrCallResult(
        returncode=0, stdout=update_run_probe_stdout(run=run + "\n", agents=f"{chat_agent_name}\tRUNNING\n")
    )


def test_an_earlier_runs_record_under_the_same_chat_name_does_not_close_this_run_out(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Update chats are named for their target, so a new run can take the name of an earlier one whose chat was
    deleted; until the new lead records its own start, the earlier record is still on the workspace."""
    caller = RecordingMngrCaller(
        result=_probe_reading(UPDATE_CHAT_NAME, started_at=time.time() - 9000.0, verdict="STUCK")
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, UPDATE_CHAT_NAME)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.verdict is None


def test_a_run_under_way_before_the_press_is_closed_out_by_its_own_verdict(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The run an Update now adopted started before the press, so its record predates the press's claim."""
    caller = ScriptedMngrCaller(
        results=(
            update_command_answer(
                "ALREADY_RUNNING",
                "An update is already running in this workspace.",
                chat_agent_name=UPDATE_CHAT_NAME,
                chat_id=UPDATE_CHAT_ID,
            ),
            _probe_reading(UPDATE_CHAT_NAME, started_at=time.time() - 9000.0, verdict="REFUSED"),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    service.dispatch_update(agent_id)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is UpdateVerdict.REFUSED


def test_a_chat_dispatched_moments_ago_is_not_closed_out_by_an_earlier_runs_record_under_its_name(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """An Update now that meets a chat the dispatcher just made adopts it before its lead records its run, while
    the record under that reused name is still the earlier run's; the dispatch time the answer names tells them
    apart."""
    caller = ScriptedMngrCaller(
        results=(
            update_command_answer(
                "ALREADY_RUNNING",
                "An update is already starting in this workspace.",
                chat_agent_name=UPDATE_CHAT_NAME,
                chat_id=UPDATE_CHAT_ID,
                run_started_at=time.time() - 120.0,
            ),
            _probe_reading(UPDATE_CHAT_NAME, started_at=time.time() - 9000.0, verdict="REFUSED"),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    service.dispatch_update(agent_id)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.verdict is None


def test_an_apply_sighting_windows_the_apply_before_any_probe_fails(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The poll's own sighting windows the apply before the health tracker sees the outage."""
    run = json.dumps({"chat_agent_name": "update-x", "apply_phase": "merged", "apply_updated_at": time.time()})
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0, stdout=update_run_probe_stdout(run=run + "\n", agents="update-x\tRUNNING\n")
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-x")

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.APPLYING
    assert service.apply_window.is_window_open(agent_id) is True


@pytest.mark.witnesses("workspace-updates.hold-is-reported-with-its-detail")
def test_a_recorded_hold_surfaces_at_once_with_its_detail(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A recorded hold is not a turn boundary, so it needs no debounce."""
    run = json.dumps(
        {
            "chat_agent_name": "update-x",
            "is_holding": True,
            "hold_detail": "Your dashboard widget has no place in the new layout.",
        }
    )
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0, stdout=update_run_probe_stdout(run=run + "\n", agents="update-x\tWAITING\n")
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-x")

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.WAITING
    assert state.is_hold_recorded is True
    assert state.hold_detail == "Your dashboard widget has no place in the new layout."


def _waiting_on_a_recorded_hold(service: WorkspaceUpdateService, agent_id: AgentId) -> None:
    begin_dispatched_update_run(service.state_store, agent_id, "update-x")
    service.state_store.set_activity(agent_id, UpdateActivity.WAITING)
    service.state_store.adopt_run_record(
        agent_id, UpdateRunStatus(chat_agent_name="update-x", is_holding=True, hold_detail="A conflict")
    )


def test_a_cleared_hold_returns_the_row_to_running_without_its_detail(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=update_run_probe_stdout(
                run=json.dumps({"chat_agent_name": "update-x"}) + "\n", agents="update-x\tRUNNING\n"
            ),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    _waiting_on_a_recorded_hold(service, agent_id)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.is_hold_recorded is False
    assert state.hold_detail == ""


def test_a_cleared_hold_comes_off_a_row_whose_agent_is_idle_again(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The agent went idle again over something else within one poll: still waiting, but not for that."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=update_run_probe_stdout(
                run=json.dumps({"chat_agent_name": "update-x"}) + "\n", agents="update-x\tWAITING\n"
            ),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    _waiting_on_a_recorded_hold(service, agent_id)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.WAITING
    assert state.is_hold_recorded is False
    assert state.hold_detail == ""


@pytest.mark.witnesses("workspace-updates.idle-lead-on-a-background-task-is-not-waiting")
@pytest.mark.parametrize(
    ("busy_section", "starting_activity", "expected_activity"),
    (
        ("lead-1\tlead-1\tupdate-x\ttrue\t1\n", UpdateActivity.RUNNING, UpdateActivity.RUNNING),
        ("lead-1\tlead-1\tupdate-x\ttrue\t1\n", UpdateActivity.WAITING, UpdateActivity.RUNNING),
        ("", UpdateActivity.RUNNING, UpdateActivity.WAITING),
    ),
    ids=["busy-stays-running", "busy-returns-to-running", "not-busy-surfaces-waiting"],
)
def test_a_lead_parked_on_a_background_task_with_its_worker_idle_is_not_reported_as_waiting(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    busy_section: str,
    starting_activity: UpdateActivity,
    expected_activity: UpdateActivity,
) -> None:
    """Both agents read WAITING, but the lead's chat will resume on its own, so nobody is waiting on the user."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=update_run_probe_stdout(
                run=json.dumps({"chat_agent_name": "update-x", "worker_agent_name": "update-self"}) + "\n",
                agents="update-x\tWAITING\tlead-1\nupdate-self\tWAITING\tworker-1\n",
                busy=busy_section,
            ),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-x")
    service.state_store.set_activity(agent_id, starting_activity)

    service.poll_in_flight_runs()
    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is expected_activity


def test_the_poll_adopts_the_records_own_start_as_the_runs_identity(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The sweep dedups the record by ``started_at``, so the record's clock must replace the claim's."""
    started_at = time.time() - 120.0
    run = json.dumps({"chat_agent_name": "update-x", "started_at": started_at})
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0, stdout=update_run_probe_stdout(run=run + "\n", agents="update-x\tRUNNING\n")
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-x")

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.run_started_at is not None
    assert abs(state.run_started_at.timestamp() - started_at) < 1.0


def test_the_poll_remembers_the_runs_chat_id_from_the_workspaces_listing(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A run the app did not create the chat for still gets its "Open chat" button."""
    listed_id = "agent-" + "e" * 32
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=update_run_probe_stdout(
                run=json.dumps({"chat_agent_name": "update-x"}) + "\n", agents=f"update-x\tRUNNING\t{listed_id}\n"
            ),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    begin_dispatched_update_run(service.state_store, agent_id, "update-x")

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).chat_agent_id == listed_id


@pytest.mark.witnesses("workspace-updates.running-workspace-not-restarted")
def test_an_update_now_that_created_the_chat_runs_the_row_under_that_chat(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The workspace names the chat, so the row learns it from the answer; one exec, and no start for a running host."""
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=CREATED_UPDATE_COMMAND_RESULT)
    service = _build_service(tmp_path, root_concurrency_group, caller=caller, started=started)
    agent_id = AgentId.generate()

    dispatch = service.dispatch_update(agent_id, target_override="minds-v0.9.1")

    assert dispatch.outcome is UpdateDispatchOutcome.DISPATCHED
    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.chat_agent_name == UPDATE_CHAT_NAME
    assert state.chat_agent_id == UPDATE_CHAT_ID
    assert state.target_override == "minds-v0.9.1"
    assert state.dispatch_failure == ""
    assert started == []
    assert len(caller.calls) == 1
    mode, flags = update_program_invocation(caller.calls[0])
    assert (mode, flags["--ceiling"], flags["--override"]) == ("now", _CEILING, "minds-v0.9.1")


def test_an_update_now_that_finds_a_run_under_way_adopts_that_run(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The run under way is the answer to the press: the row reports it, not a failure, and not the version the
    press asked for, which that run is not going to."""
    caller = RecordingMngrCaller(
        result=update_command_answer(
            "ALREADY_RUNNING",
            "An update is already running in this workspace.",
            chat_agent_name="update-to-minds-v0-8-9",
            chat_id=UPDATE_CHAT_ID,
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    dispatch = service.dispatch_update(agent_id, target_override="minds-v0.9.1")

    assert dispatch.outcome is UpdateDispatchOutcome.ALREADY_RUNNING
    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.RUNNING
    assert state.chat_agent_name == "update-to-minds-v0-8-9"
    assert state.chat_agent_id == UPDATE_CHAT_ID
    assert state.target_override == ""
    assert state.dispatch_failure == ""


def test_an_update_now_refused_for_another_flows_lease_unlocks_the_row_and_says_why(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """With no chat named there is no run for the row to report, so it must not stay locked."""
    detail = "Another update flow is working in this workspace."
    caller = RecordingMngrCaller(result=update_command_answer("ALREADY_RUNNING", detail))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.ALREADY_RUNNING
    state = service.state_store.get(agent_id)
    assert state.is_run_in_flight is False
    assert state.dispatch_failure == DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.ALREADY_RUNNING]
    assert state.dispatch_failure_detail == detail


@pytest.mark.witnesses("workspace-updates.needs-sign-in", partial="the row state on Update now")
def test_an_update_now_refused_for_sign_in_shows_the_sign_in_state_rather_than_a_failure_line(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = RecordingMngrCaller(result=NEEDS_SIGN_IN_UPDATE_COMMAND_RESULT)
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.NEEDS_SIGN_IN
    state = service.state_store.get(agent_id)
    assert state.needs_sign_in is True
    assert state.is_run_in_flight is False
    assert state.dispatch_failure == ""
    assert state.dispatch_failure_detail == ""


@pytest.mark.witnesses("workspace-updates.needs-sign-in", partial="the row clearing once a run goes out")
def test_an_update_that_goes_out_after_a_sign_in_refusal_clears_the_sign_in_state(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = ScriptedMngrCaller(results=(NEEDS_SIGN_IN_UPDATE_COMMAND_RESULT, CREATED_UPDATE_COMMAND_RESULT))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    service.dispatch_update(agent_id)
    assert service.state_store.get(agent_id).needs_sign_in is True

    assert service.dispatch_update(agent_id).outcome is UpdateDispatchOutcome.DISPATCHED

    assert service.state_store.get(agent_id).needs_sign_in is False


def test_an_update_now_on_a_workspace_too_old_for_the_skill_is_unsupported(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = RecordingMngrCaller(
        result=update_command_answer("TOO_OLD_TO_UPDATE", "This workspace predates the update-self skill.")
    )
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.UNSUPPORTED
    state = service.state_store.get(agent_id)
    assert state.is_run_in_flight is False
    assert state.dispatch_failure == DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.UNSUPPORTED]


@pytest.mark.witnesses("workspace-updates.workspace-refuses-the-agent", partial="the service outcome and the row")
@pytest.mark.parametrize("program_outcome", ("CREATE_FAILED", "CHAT_APP_UNREACHABLE", "AN_OUTCOME_FROM_A_NEWER_APP"))
def test_an_update_chat_the_workspace_could_not_create_fails_in_the_workspaces_own_words(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, program_outcome: str
) -> None:
    detail = "The chat app said: no harness is installed for this account."
    caller = RecordingMngrCaller(result=update_command_answer(program_outcome, detail))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.SPAWN_FAILED
    assert dispatch.failure_detail == detail
    state = service.state_store.get(agent_id)
    assert state.is_run_in_flight is False
    assert state.dispatch_failure == DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.SPAWN_FAILED]
    assert state.dispatch_failure_detail == detail


@pytest.mark.parametrize(
    ("result", "expected_detail"),
    (
        (CUT_OFF_ON_REACHED_HOST_EXEC_RESULT, EXEC_CUT_OFF_REASON),
        (MngrCallResult(returncode=1, stdout=exec_result_stdout("", "MemoryError\n"), is_mngr_output=True), ""),
        (MngrCallResult(returncode=-1, is_timed_out=True, stderr="mngr exec --agent x timed out after 420s"), ""),
        (MngrCallResult(returncode=1, stderr="mngr warm process exited without returning a result"), ""),
    ),
    ids=("cut_off_on_reached_host", "program_died_without_answering", "timed_out", "warm_process_died"),
)
def test_an_update_now_that_may_have_run_is_unconfirmed_and_never_starts_the_workspace_and_runs_again(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, result: MngrCallResult, expected_detail: str
) -> None:
    """A command cut off in the workspace may have made the chat, and a second run would start a second update."""
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=result)
    service = _build_service(tmp_path, root_concurrency_group, caller=caller, started=started)
    agent_id = AgentId.generate()

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.UNCONFIRMED
    assert dispatch.failure_detail == expected_detail
    assert started == []
    assert len(caller.calls) == 1
    assert (
        service.state_store.get(agent_id).dispatch_failure
        == DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.UNCONFIRMED]
    )


class _RunStartsWhileTheCommandTimesOutCaller(MngrCaller):
    """Moves the row on while the command is in flight, then times the command out."""

    store: WorkspaceUpdateStateStore = Field(description="Where the run lands.")
    agent_id: AgentId = Field(description="The machine being dispatched to.")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        self.store.set_activity(self.agent_id, UpdateActivity.RUNNING)
        return MngrCallResult(returncode=-1, is_timed_out=True)


def test_an_unconfirmed_update_does_not_unlock_a_run_that_has_started(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Releasing the slot unconditionally would let the retry start a second update in the same machine."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()
    caller = _RunStartsWhileTheCommandTimesOutCaller(store=store, agent_id=agent_id)
    service = _build_service(tmp_path, root_concurrency_group, caller=caller, store=store)

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.UNCONFIRMED
    assert store.get(agent_id).activity is UpdateActivity.RUNNING
    assert store.get(agent_id).dispatch_failure == ""


def test_an_update_runs_on_its_machine_by_the_host_discovery_placed_it_on(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A bare id would make mngr list every host of the provider before the command reaches the workspace."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    caller = RecordingMngrCaller(result=CREATED_UPDATE_COMMAND_RESULT)
    service = _build_service(
        tmp_path,
        root_concurrency_group,
        caller=caller,
        backend_resolver=build_resolver_with_system_services(agent_id, AgentId.generate(), host_id=host_id),
    )

    service.dispatch_update(agent_id)

    assert caller.calls[0][:3] == ["exec", "--agent", f"{agent_id}@{host_id}.{SYSTEM_SERVICES_PROVIDER_NAME}"]


@pytest.mark.witnesses("workspace-updates.stopped-workspace-started", partial="the start and the second run")
def test_an_update_the_command_could_not_reach_starts_the_machine_once_and_runs_again(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Discovery can think a stopped machine is up, so the command's own reach decides, not the host state."""
    started: list[AgentId] = []
    agent_id = AgentId.generate()
    caller = ScriptedMngrCaller(results=(_HOST_OFFLINE, CREATED_UPDATE_COMMAND_RESULT))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller, started=started)

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.DISPATCHED
    assert started == [agent_id]
    assert [update_program_invocation(call)[0] for call in caller.calls] == ["now", "now"]
    assert service.state_store.get(agent_id).chat_agent_name == UPDATE_CHAT_NAME


def test_an_update_still_unreachable_after_its_machine_started_says_what_mngr_said(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=_HOST_OFFLINE)
    service = _build_service(tmp_path, root_concurrency_group, caller=caller, started=started)

    dispatch = service.dispatch_update(AgentId.generate())

    assert dispatch.outcome is UpdateDispatchOutcome.UNREACHABLE
    assert dispatch.failure_detail == "Host 'host-1' is offline and automatic starting is disabled."
    assert len(started) == 1
    assert len(caller.calls) == 2


def test_an_update_whose_machine_would_not_start_carries_the_starts_diagnosis_and_runs_no_more(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A start that failed and a command that got no answer are different problems, so the outcome names which."""
    agent_id = AgentId.generate()
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=_HOST_OFFLINE)
    service = _build_service(
        tmp_path,
        root_concurrency_group,
        caller=caller,
        started=started,
        start_failure_reason="ERROR: The box behind host-5821 is gone",
    )

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.START_FAILED
    assert dispatch.failure_detail == "ERROR: The box behind host-5821 is gone"
    assert started == [agent_id]
    assert len(caller.calls) == 1
    assert service.state_store.get(agent_id).dispatch_failure == "Couldn't start this workspace to run the update."


def test_an_armed_schedule_shows_on_the_row_at_once_from_the_workspaces_own_file(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The arm carries the app's values in; the row shows the file as the workspace wrote it, before any sweep."""
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=_armed(override_ref="minds-v0.9.1"))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller, started=started)
    agent_id = AgentId.generate()

    schedule = service.schedule_update(agent_id, override_ref="minds-v0.9.1", is_auto_armed=False)

    assert schedule.outcome is UpdateScheduleOutcome.ARMED
    state = service.state_store.get(agent_id)
    assert state.is_scheduled is True
    assert state.override_ref == "minds-v0.9.1"
    assert state.schedule_window == "11:00 PM-3:00 AM"
    assert state.schedule_zone == "Europe/Berlin"
    assert state.is_schedule_auto_armed is False
    mode, flags = update_program_invocation(caller.calls[0])
    assert mode == "arm"
    assert flags["--ceiling"] == _CEILING
    assert flags["--override"] == "minds-v0.9.1"
    assert flags["--auto-armed"] == "0"
    assert (flags["--window-start"], flags["--window-end"], flags["--zone"]) == ("23", "3", "America/Los_Angeles")
    assert started == []


@pytest.mark.witnesses("workspace-updates.needs-sign-in", partial="the row state on Schedule, and its clearing")
def test_a_schedule_refused_for_sign_in_shows_on_the_row_until_an_arm_lands(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = ScriptedMngrCaller(results=(NEEDS_SIGN_IN_UPDATE_COMMAND_RESULT, _armed()))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    refused = service.schedule_update(agent_id, override_ref="", is_auto_armed=False)

    assert refused.outcome is UpdateScheduleOutcome.NEEDS_SIGN_IN
    assert refused.failure_detail == "No AI account is signed in to this workspace."
    assert service.state_store.get(agent_id).needs_sign_in is True
    assert service.state_store.get(agent_id).is_scheduled is False

    assert (
        service.schedule_update(agent_id, override_ref="", is_auto_armed=False).outcome is UpdateScheduleOutcome.ARMED
    )

    assert service.state_store.get(agent_id).needs_sign_in is False
    assert service.state_store.get(agent_id).is_scheduled is True


@pytest.mark.witnesses("workspace-updates.no-cron", partial="the refusal outcome, not the offer")
@pytest.mark.parametrize(
    ("program_outcome", "expected_outcome"),
    (
        ("NO_CRON", UpdateScheduleOutcome.NO_CRON),
        ("TOO_OLD_TO_UPDATE", UpdateScheduleOutcome.UNSUPPORTED),
        ("CREATED", UpdateScheduleOutcome.FAILED),
    ),
)
def test_a_schedule_the_workspace_refused_arms_nothing_and_carries_its_words(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    program_outcome: str,
    expected_outcome: UpdateScheduleOutcome,
) -> None:
    detail = "This workspace has no cron program, so nothing in it could start a scheduled update."
    caller = RecordingMngrCaller(result=update_command_answer(program_outcome, detail))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    schedule = service.schedule_update(agent_id, override_ref="", is_auto_armed=False)

    assert schedule.outcome is expected_outcome
    assert schedule.failure_detail == detail
    assert service.state_store.get(agent_id).is_scheduled is False


@pytest.mark.witnesses("workspace-updates.schedule-needs-a-running-workspace", partial="the outcome, not its copy")
@pytest.mark.parametrize(
    ("result", "expected_outcome", "expected_detail"),
    (
        (
            _HOST_OFFLINE,
            UpdateScheduleOutcome.NOT_RUNNING,
            "Host 'host-1' is offline and automatic starting is disabled.",
        ),
        (CUT_OFF_ON_REACHED_HOST_EXEC_RESULT, UpdateScheduleOutcome.UNREACHABLE, EXEC_CUT_OFF_REASON),
    ),
    ids=("not_reached", "cut_off"),
)
def test_a_schedule_the_workspace_did_not_answer_never_starts_it(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    result: MngrCallResult,
    expected_outcome: UpdateScheduleOutcome,
    expected_detail: str,
) -> None:
    """The run is started by the workspace's own cron, so a stopped workspace has to be running for the window anyway."""
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=result)
    service = _build_service(tmp_path, root_concurrency_group, caller=caller, started=started)
    agent_id = AgentId.generate()

    schedule = service.schedule_update(agent_id, override_ref="", is_auto_armed=False)

    assert schedule.outcome is expected_outcome
    assert schedule.failure_detail == expected_detail
    assert started == []
    assert len(caller.calls) == 1
    assert service.state_store.get(agent_id).is_scheduled is False


@pytest.mark.witnesses("workspace-updates.schedule-by-hand-under-auto-update", partial="the schedule's provenance")
def test_an_auto_update_arm_targets_the_ceiling_with_no_override_and_marks_itself(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = RecordingMngrCaller(result=_armed(is_auto_armed=True))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    service.arm_for_auto_update(agent_id)

    mode, flags = update_program_invocation(caller.calls[0])
    assert (mode, flags["--ceiling"], flags["--override"], flags["--auto-armed"]) == ("arm", _CEILING, "", "1")
    state = service.state_store.get(agent_id)
    assert state.is_scheduled is True
    assert state.is_schedule_auto_armed is True


def test_an_auto_update_arm_the_workspace_refused_says_so_on_the_row_until_an_arm_lands(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Under auto-update a behind row is quiet, so an arm that cannot land has to say why there."""
    detail = "This workspace has no cron program, so nothing in it could start a scheduled update."
    caller = ScriptedMngrCaller(results=(update_command_answer("NO_CRON", detail), _armed()))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()

    service.arm_for_auto_update(agent_id)

    refused = service.state_store.get(agent_id)
    assert refused.dispatch_failure == SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME[UpdateScheduleOutcome.NO_CRON]
    assert refused.dispatch_failure_detail == detail

    service.arm_for_auto_update(agent_id)

    armed = service.state_store.get(agent_id)
    assert armed.is_scheduled is True
    assert armed.dispatch_failure == ""


def test_a_cancel_the_workspace_answered_takes_the_schedule_off_the_row(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = ScriptedMngrCaller(results=(_armed(), CANCELLED_UPDATE_COMMAND_RESULT))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    service.schedule_update(agent_id, override_ref="", is_auto_armed=False)

    assert service.cancel_schedule(agent_id) is None

    assert service.state_store.get(agent_id).is_scheduled is False
    assert update_program_invocation(caller.calls[1]) == ("cancel", {})


@pytest.mark.witnesses("workspace-updates.cancel-needs-the-workspace", partial="the row, not the copy")
@pytest.mark.parametrize(
    ("result", "expected_outcome"),
    (
        (_HOST_OFFLINE, UpdateScheduleOutcome.NOT_RUNNING),
        (CUT_OFF_ON_REACHED_HOST_EXEC_RESULT, UpdateScheduleOutcome.UNREACHABLE),
    ),
    ids=("not_reached", "cut_off"),
)
def test_a_cancel_that_did_not_reach_the_workspace_leaves_the_row_scheduled(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    result: MngrCallResult,
    expected_outcome: UpdateScheduleOutcome,
) -> None:
    """The file is still armed in the workspace, so its cron still starts the run; the row must say so."""
    caller = ScriptedMngrCaller(results=(_armed(), result))
    service = _build_service(tmp_path, root_concurrency_group, caller=caller)
    agent_id = AgentId.generate()
    service.schedule_update(agent_id, override_ref="", is_auto_armed=False)

    refusal = service.cancel_schedule(agent_id)

    assert refusal is not None
    assert refusal.outcome is expected_outcome
    assert service.state_store.get(agent_id).is_scheduled is True


@pytest.mark.witnesses("workspace-updates.skip-this-update", partial="the recorded skip and the cancel")
def test_skipping_an_update_remembers_the_apps_ceiling_for_that_workspace_and_cancels_its_schedule(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The sweep re-arms nothing for this release; a relaunched app reads the same skip."""
    config_dir = tmp_path / "minds-config"
    caller = ScriptedMngrCaller(results=(_armed(), CANCELLED_UPDATE_COMMAND_RESULT))
    service = _build_service(
        tmp_path, root_concurrency_group, caller=caller, minds_config=MindsConfig(data_dir=config_dir)
    )
    agent_id = AgentId.generate()
    service.schedule_update(agent_id, override_ref="", is_auto_armed=False)

    assert service.skip_update(agent_id) is None

    assert MindsConfig(data_dir=config_dir).get_skipped_update_target(str(agent_id)) == _CEILING
    assert service.state_store.get(agent_id).is_scheduled is False
    assert update_program_invocation(caller.calls[1])[0] == "cancel"
