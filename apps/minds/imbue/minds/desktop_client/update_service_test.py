"""The scheduled-run gate and the run close-out."""

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
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import SYSTEM_SERVICES_PROVIDER_NAME
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import host_offline_exec_result
from imbue.minds.desktop_client.testing import make_update_state_store
from imbue.minds.desktop_client.testing import ready_machine_launch_stdout
from imbue.minds.desktop_client.testing import update_run_probe_stdout
from imbue.minds.desktop_client.update_apply_window import UpdateApplyWindowManager
from imbue.minds.desktop_client.update_chat import UPDATE_SKILL_NAME
from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleStore
from imbue.minds.desktop_client.update_scheduler import UpdateScheduler
from imbue.minds.desktop_client.update_service import UpdateDispatchOutcome
from imbue.minds.desktop_client.update_service import WorkspaceUpdateService
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateDetector
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState


class _InvalidationRecordingDetector(WorkspaceUpdateDetector):
    """A detector that remembers which workspaces had their cached version dropped."""

    _invalidated: list[AgentId] = PrivateAttr(default_factory=list)

    def invalidate_cached_version(self, agent_id: AgentId) -> None:
        self._invalidated.append(agent_id)
        super().invalidate_cached_version(agent_id)

    @property
    def invalidated(self) -> list[AgentId]:
        return self._invalidated


class _FixedHostStateService(WorkspaceUpdateService):
    """A service whose host-state reading is fixed, so the gate can be driven from it."""

    fixed_host_state: HostState | None = Field(default=None, description="What every host-state read answers.")

    def read_host_state(self, agent_id: AgentId) -> HostState | None:
        return self.fixed_host_state


def _build_service(
    tmp_path: Path,
    concurrency_group: ConcurrencyGroup,
    *,
    host_state: HostState | None,
    caller: MngrCaller,
    store: WorkspaceUpdateStateStore | None = None,
    started: list[AgentId] | None = None,
    start_failure_reason: str | None = None,
    backend_resolver: MngrCliBackendResolver | None = None,
) -> _FixedHostStateService:
    store = store if store is not None else make_update_state_store(tmp_path)
    backend_resolver = backend_resolver if backend_resolver is not None else MngrCliBackendResolver()
    apply_window = UpdateApplyWindowManager(
        tracker=SystemInterfaceHealthTracker(),
        store=store,
        mngr_caller=caller,
        backend_resolver=backend_resolver,
        concurrency_group=concurrency_group,
        dispatch_restart=lambda agent_id: None,
    )
    return _FixedHostStateService(
        state_store=store,
        schedule_store=UpdateScheduleStore(records_dir=tmp_path / "update_schedules"),
        detector=_InvalidationRecordingDetector(
            store=store,
            backend_resolver=backend_resolver,
            mngr_caller=caller,
            concurrency_group=concurrency_group,
            read_supported_version=lambda: "minds-v0.4.1",
            read_run_record=lambda agent_id: apply_window.probe_run(agent_id).run_status,
        ),
        apply_window=apply_window,
        mngr_caller=caller,
        backend_resolver=backend_resolver,
        paths=InstallationPaths(data_dir=tmp_path / "data"),
        start_workspace=_record_start(started if started is not None else [], failure_reason=start_failure_reason),
        fixed_host_state=host_state,
    )


def _record_start(started: list[AgentId], *, failure_reason: str | None) -> Callable[[AgentId], MindHostActionOutcome]:
    """A stand-in host start that records who it was asked for and reports the machine up, or why not."""

    def start(agent_id: AgentId) -> MindHostActionOutcome:
        started.append(agent_id)
        return MindHostActionOutcome(is_successful=failure_reason is None, failure_reason=failure_reason)

    return start


@pytest.mark.parametrize("host_state", (HostState.STOPPED, HostState.CRASHED, HostState.STARTING))
def test_a_machine_that_is_not_up_reads_as_quiet_without_being_asked(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, host_state: HostState
) -> None:
    """The gate execs with ``--no-start`` and reads unanswered as busy, so asking would decline every such run."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stdout=""))
    service = _build_service(tmp_path, root_concurrency_group, host_state=host_state, caller=caller)

    conditions = service.read_conditions(AgentId.generate())

    assert conditions.is_quiet is True
    assert conditions.is_reachable is True
    assert caller.calls == []


def test_a_running_machine_is_still_asked_and_an_unanswered_gate_counts_as_busy(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stdout=""))
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)

    conditions = service.read_conditions(AgentId.generate())

    assert conditions.is_quiet is False
    assert [argv[0] for argv in caller.calls] == ["exec"]


@pytest.mark.parametrize(
    ("gate_stdout", "is_quiet"),
    (
        ('MINDS_BACKUP_GATE_JSON:{"running_chats": []}\n', True),
        ('MINDS_BACKUP_GATE_JSON:{"running_chats": ["chat-a"]}\n', False),
        ('MINDS_BACKUP_GATE_JSON:{"gate_error": "could not list"}\n', False),
        ("some other output\n", False),
    ),
    ids=["no-chats", "a-chat", "gate-error", "no-payload"],
)
def test_the_chat_gate_reads_quiet_only_from_a_positive_empty_listing(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, gate_stdout: str, is_quiet: bool
) -> None:
    """A gate that could not answer counts as busy."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=gate_stdout))
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)

    assert service.is_workspace_quiet(AgentId.generate()) is is_quiet


def test_a_host_discovery_knows_nothing_about_is_unreachable(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=""))
    service = _build_service(tmp_path, root_concurrency_group, host_state=None, caller=caller)

    conditions = service.read_conditions(AgentId.generate())

    assert conditions.is_reachable is False


@pytest.mark.witnesses("workspace-updates.patch-available-schedule-runs")
@pytest.mark.parametrize(
    ("availability", "is_dispatched"),
    ((UpdateAvailability.PATCH_AVAILABLE, True), (UpdateAvailability.UP_TO_DATE, False)),
)
def test_a_schedule_armed_on_a_machine_with_only_a_patch_available_runs_in_the_window(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, availability: UpdateAvailability, is_dispatched: bool
) -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stdout=""))
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.STOPPED, caller=caller)
    agent_id = AgentId.generate()
    service.state_store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=availability),
        current_version="minds-v0.4.0",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )
    service.schedule_store.schedule(agent_id)
    dispatched: list[AgentId] = []

    def record_dispatch(dispatched_id: AgentId, _target_ref: str) -> bool:
        dispatched.append(dispatched_id)
        return True

    scheduler = UpdateScheduler(
        schedule_store=service.schedule_store,
        read_update_window=lambda: (2, 5),
        read_conditions=service.read_conditions,
        read_host_state=lambda _agent_id: HostState.STOPPED,
        dispatch=record_dispatch,
        stop_workspace=lambda _agent_id: None,
        now=lambda: datetime(2026, 10, 5, 3, 17),
    )

    scheduler.run_window_pass()

    assert dispatched == ([agent_id] if is_dispatched else [])


# The run's chat listed with no live process: the probe's positive "this run is over".
_GONE_AGENT_STDOUT = update_run_probe_stdout(agents="update-old\tSTOPPED\n")


def _attach_recording_scheduler(
    service: WorkspaceUpdateService, *, host_state: HostState | None
) -> tuple[UpdateScheduler, list[AgentId]]:
    """Wire ``service`` to a scheduler whose only outside effect is recording stops."""
    stopped: list[AgentId] = []
    scheduler = UpdateScheduler(
        schedule_store=service.schedule_store,
        read_update_window=lambda: (2, 5),
        read_conditions=service.read_conditions,
        read_host_state=lambda _agent_id: host_state,
        dispatch=lambda _agent_id, _target_ref: True,
        stop_workspace=stopped.append,
    )
    service.add_on_run_finished_callback(scheduler.note_run_finished)
    return scheduler, stopped


def test_a_run_that_vanished_puts_its_machine_back_and_disarms_the_intent(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A gone agent is the run's end as much as a verdict is."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=_GONE_AGENT_STDOUT))
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.STOPPED, caller=caller)
    scheduler, stopped = _attach_recording_scheduler(service, host_state=HostState.STOPPED)
    landings_told: list[AgentId] = []
    service.add_on_update_landed_callback(lambda agent_id, run_started_at, ended_at: landings_told.append(agent_id))
    agent_id = AgentId.generate()
    service.schedule_store.schedule(agent_id)
    assert scheduler.run_now(agent_id) is None
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-old")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.STALLED
    assert stopped == [agent_id]
    assert service.schedule_store.read(agent_id) is None
    assert landings_told == []


def test_a_verdict_in_the_run_record_ends_the_run_and_closes_it_out(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The record's verdict idles the row, re-reads the version, and closes the scheduled run out."""
    started_at = time.time() - 600.0
    run = json.dumps(
        {
            "chat_agent_name": "update-x",
            "started_at": started_at,
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
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.STOPPED, caller=caller)
    scheduler, stopped = _attach_recording_scheduler(service, host_state=HostState.STOPPED)
    agent_id = AgentId.generate()
    service.schedule_store.schedule(agent_id)
    assert scheduler.run_now(agent_id) is None
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-x")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is UpdateVerdict.UPDATED
    assert state.verdict_detail == "Landed cleanly."
    assert isinstance(service.detector, _InvalidationRecordingDetector)
    assert service.detector.invalidated == [agent_id]
    assert stopped == [agent_id]
    assert service.schedule_store.read(agent_id) is None


def _poll_a_run_whose_record_reads(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, record: dict[str, object]
) -> tuple[AgentId, list[tuple[AgentId, datetime | None, datetime]]]:
    """Poll one in-flight run whose ``run.json`` is ``record``: its id, and each (id, run start, end) a listener was told."""
    chat_agent_name = str(record["chat_agent_name"])
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=update_run_probe_stdout(run=json.dumps(record) + "\n", agents=f"{chat_agent_name}\tWAITING\n"),
        )
    )
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)
    told: list[tuple[AgentId, datetime | None, datetime]] = []
    service.add_on_update_landed_callback(
        lambda agent_id, run_started_at, ended_at: told.append((agent_id, run_started_at, ended_at))
    )
    agent_id = AgentId.generate()
    service.state_store.try_begin_run(agent_id, chat_agent_name=chat_agent_name)
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)
    service.poll_in_flight_runs()
    return agent_id, told


@pytest.mark.parametrize("is_verdict_time_recorded", [True, False], ids=["recorded", "unreadable"])
def test_a_landed_update_tells_the_listeners_when_the_run_started_and_ended(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, is_verdict_time_recorded: bool
) -> None:
    """The listeners get the record's start, and its own verdict time or the reading's when the record has none."""
    started_at = time.time() - 2400.0
    verdict_at = time.time() - 713.0
    record: dict[str, object] = {
        "chat_agent_name": "update-y",
        "started_at": started_at,
        "verdict": "UPDATED",
        "resulting_ref": "minds-v0.7.3",
    }
    if is_verdict_time_recorded:
        record["verdict_at"] = verdict_at

    polled_from = datetime.now(timezone.utc)
    agent_id, told = _poll_a_run_whose_record_reads(tmp_path, root_concurrency_group, record)
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
    [UpdateVerdict.ALREADY_CURRENT, UpdateVerdict.REFUSED, UpdateVerdict.NEEDS_RECREATION, UpdateVerdict.STUCK],
)
def test_a_verdict_that_landed_nothing_tells_no_listener(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, verdict: UpdateVerdict
) -> None:
    """The workspace serves the build its views already loaded, so there is nothing to tell."""
    record: dict[str, object] = {"chat_agent_name": "update-z", "verdict": verdict.value, "verdict_at": time.time()}

    _agent_id, told = _poll_a_run_whose_record_reads(tmp_path, root_concurrency_group, record)

    assert told == []


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
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)
    agent_id = AgentId.generate()
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-new")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

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
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)
    agent_id = AgentId.generate()
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-x")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

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
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)
    agent_id = AgentId.generate()
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-x")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.WAITING
    assert state.is_hold_recorded is True
    assert state.hold_detail == "Your dashboard widget has no place in the new layout."


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
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)
    agent_id = AgentId.generate()
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-x")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)
    service.state_store.set_activity(agent_id, UpdateActivity.WAITING)
    service.state_store.adopt_run_record(
        agent_id, UpdateRunStatus(chat_agent_name="update-x", is_holding=True, hold_detail="A conflict")
    )

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
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)
    agent_id = AgentId.generate()
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-x")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)
    service.state_store.set_activity(agent_id, UpdateActivity.WAITING)
    service.state_store.adopt_run_record(
        agent_id, UpdateRunStatus(chat_agent_name="update-x", is_holding=True, hold_detail="Your widget")
    )

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.WAITING
    assert state.is_hold_recorded is False
    assert state.hold_detail == ""


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
    service = _build_service(tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller)
    agent_id = AgentId.generate()
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-x")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.run_started_at is not None
    assert abs(state.run_started_at.timestamp() - started_at) < 1.0


class _SpawnTimesOutAfterCreatingTheChatCaller(MngrCaller):
    """Times the launch out after the chat was really created."""

    store: WorkspaceUpdateStateStore = Field(description="Where the discovered run lands.")
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


def test_a_spawn_reported_as_failed_does_not_unlock_a_run_that_has_started(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Releasing the slot unconditionally would let the retry start a second update in the same machine."""
    store = make_update_state_store(tmp_path)
    agent_id = AgentId.generate()
    caller = _SpawnTimesOutAfterCreatingTheChatCaller(store=store, agent_id=agent_id)
    service = _build_service(
        tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller, store=store
    )

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.SPAWN_FAILED
    assert store.get(agent_id).activity is UpdateActivity.RUNNING
    assert store.get(agent_id).dispatch_failure == ""


def test_an_update_launches_on_its_machine_by_the_host_discovery_placed_it_on(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A bare id would make mngr list every host of the provider before the launch reaches the workspace."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stdout=""))
    service = _build_service(
        tmp_path,
        root_concurrency_group,
        host_state=HostState.RUNNING,
        caller=caller,
        backend_resolver=build_resolver_with_system_services(agent_id, AgentId.generate(), host_id=host_id),
    )

    service.dispatch_update(agent_id)

    assert caller.calls[0][:3] == ["exec", "--agent", f"{agent_id}@{host_id}.{SYSTEM_SERVICES_PROVIDER_NAME}"]


_LAUNCHED_STDOUT = ready_machine_launch_stdout(UPDATE_SKILL_NAME, has_chat_create_script=True)
_HOST_OFFLINE = host_offline_exec_result()


def test_an_update_on_a_running_machine_launches_without_starting_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A start against a running host costs as much as the launch and flips the row through STARTING for nothing."""
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=_LAUNCHED_STDOUT))
    service = _build_service(
        tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller, started=started
    )

    dispatch = service.dispatch_update(AgentId.generate())

    assert dispatch.outcome is UpdateDispatchOutcome.DISPATCHED
    assert started == []
    assert len(caller.calls) == 1


def test_an_update_the_launch_could_not_reach_starts_the_machine_and_launches_again(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Discovery can think a stopped machine is up, so the launch's own reach decides, not the host state."""
    started: list[AgentId] = []
    agent_id = AgentId.generate()
    caller = ScriptedMngrCaller(results=(_HOST_OFFLINE, MngrCallResult(returncode=0, stdout=_LAUNCHED_STDOUT)))
    service = _build_service(
        tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller, started=started
    )

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.DISPATCHED
    assert started == [agent_id]
    assert len(caller.calls) == 2


def test_an_update_still_unreachable_after_its_machine_started_says_what_mngr_said(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=_HOST_OFFLINE)
    service = _build_service(
        tmp_path, root_concurrency_group, host_state=HostState.STOPPED, caller=caller, started=started
    )

    dispatch = service.dispatch_update(AgentId.generate())

    assert dispatch.outcome is UpdateDispatchOutcome.UNREACHABLE
    assert dispatch.failure_detail == "Host 'host-1' is offline and automatic starting is disabled."
    assert len(started) == 1
    assert len(caller.calls) == 2


def test_an_update_whose_machine_would_not_start_carries_the_starts_diagnosis_and_launches_no_more(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A start that failed and a launch that got no answer are different problems, so the outcome names which."""
    agent_id = AgentId.generate()
    store = make_update_state_store(tmp_path)
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=_HOST_OFFLINE)
    service = _build_service(
        tmp_path,
        root_concurrency_group,
        host_state=HostState.STOPPED,
        caller=caller,
        store=store,
        started=started,
        start_failure_reason="ERROR: The box behind host-5821 is gone",
    )

    dispatch = service.dispatch_update(agent_id)

    assert dispatch.outcome is UpdateDispatchOutcome.START_FAILED
    assert dispatch.failure_detail == "ERROR: The box behind host-5821 is gone"
    assert started == [agent_id]
    assert len(caller.calls) == 1
    assert store.get(agent_id).dispatch_failure == "Couldn't start this machine to run the update."


@pytest.mark.parametrize(
    "result",
    (
        MngrCallResult(returncode=-1, is_timed_out=True),
        MngrCallResult(returncode=1, stderr="mngr warm process exited without returning a result"),
    ),
    ids=("timed_out", "warm_process_died"),
)
def test_an_update_whose_launch_mngr_never_answered_does_not_start_the_machine_and_launch_again(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, result: MngrCallResult
) -> None:
    """A launch cut off before mngr answered may have made the chat, and a second launch would start a second update."""
    started: list[AgentId] = []
    caller = RecordingMngrCaller(result=result)
    service = _build_service(
        tmp_path, root_concurrency_group, host_state=HostState.RUNNING, caller=caller, started=started
    )

    dispatch = service.dispatch_update(AgentId.generate())

    assert dispatch.outcome is UpdateDispatchOutcome.SPAWN_FAILED
    assert started == []
    assert len(caller.calls) == 1
