"""The scheduled-run gate and the run close-out."""

import json
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import make_update_state_store
from imbue.minds.desktop_client.testing import update_run_probe_stdout
from imbue.minds.desktop_client.update_apply_window import UpdateApplyWindowManager
from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleStore
from imbue.minds.desktop_client.update_scheduler import UpdateScheduler
from imbue.minds.desktop_client.update_service import UpdateDispatchOutcome
from imbue.minds.desktop_client.update_service import WorkspaceUpdateService
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateDetector
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
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
) -> _FixedHostStateService:
    store = store if store is not None else make_update_state_store(tmp_path)
    backend_resolver = MngrCliBackendResolver()
    apply_window = UpdateApplyWindowManager(
        tracker=SystemInterfaceHealthTracker(),
        store=store,
        mngr_caller=caller,
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
        start_workspace=_record_and_succeed(started if started is not None else []),
        fixed_host_state=host_state,
    )


def _record_and_succeed(started: list[AgentId]) -> Callable[[AgentId], bool]:
    """A stand-in host start that records who it was asked for and always reports the machine up."""

    def start(agent_id: AgentId) -> bool:
        started.append(agent_id)
        return True

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
    agent_id = AgentId.generate()
    service.schedule_store.schedule(agent_id)
    assert scheduler.run_now(agent_id) is None
    service.state_store.try_begin_run(agent_id, chat_agent_name="update-old")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.STALLED
    assert stopped == [agent_id]
    assert service.schedule_store.read(agent_id) is None


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
    """Answers each step of a dispatch; the spawn times out after the chat was really created."""

    store: WorkspaceUpdateStateStore = Field(description="Where the discovered run lands.")
    agent_id: AgentId = Field(description="The machine being dispatched to.")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        if argv[0] == "start":
            return MngrCallResult(returncode=0)
        if any("MNGR_UPDATE_SELF_SKILL_PRESENT" in arg for arg in argv):
            return MngrCallResult(returncode=0, stdout="MNGR_UPDATE_SELF_SKILL_PRESENT\n")
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

    outcome = service.dispatch_update(agent_id)

    assert outcome is UpdateDispatchOutcome.SPAWN_FAILED
    assert store.get(agent_id).activity is UpdateActivity.RUNNING
