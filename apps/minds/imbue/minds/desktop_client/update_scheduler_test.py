"""When a scheduled update runs, when it declines, and what it does when it ends."""

from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from imbue.minds.desktop_client.update_schedule_store import UpdateScheduleStore
from imbue.minds.desktop_client.update_scheduler import ScheduledRunConditions
from imbue.minds.desktop_client.update_scheduler import UpdateScheduler
from imbue.minds.desktop_client.update_scheduler import decide_skip_reason
from imbue.minds.desktop_client.update_scheduler import is_within_update_window
from imbue.minds.desktop_client.update_status import UpdateSkipReason
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 19, hour, 30)


def test_the_window_covers_the_hours_between_its_bounds() -> None:
    assert is_within_update_window(_at(3), (2, 5)) is True
    assert is_within_update_window(_at(1), (2, 5)) is False
    assert is_within_update_window(_at(5), (2, 5)) is False


def test_a_window_that_crosses_midnight_still_works() -> None:
    assert is_within_update_window(_at(23), (23, 3)) is True
    assert is_within_update_window(_at(1), (23, 3)) is True
    assert is_within_update_window(_at(12), (23, 3)) is False


def _conditions(**overrides: bool) -> ScheduledRunConditions:
    base = {
        "is_offered": True,
        "is_run_in_flight": False,
        "is_reachable": True,
        "is_quiet": True,
    }
    return ScheduledRunConditions.model_validate({**base, **overrides})


def test_everything_in_order_runs() -> None:
    assert decide_skip_reason(_conditions(), is_target_named=False) is None


def test_a_run_already_in_flight_is_skipped() -> None:
    assert (
        decide_skip_reason(_conditions(is_run_in_flight=True), is_target_named=False)
        is UpdateSkipReason.UPDATE_IN_FLIGHT
    )


def test_a_machine_that_no_longer_needs_the_update_is_skipped_quietly() -> None:
    assert (
        decide_skip_reason(_conditions(is_offered=False), is_target_named=False) is UpdateSkipReason.ALREADY_UP_TO_DATE
    )


def test_an_unreachable_machine_is_skipped() -> None:
    assert (
        decide_skip_reason(_conditions(is_reachable=False), is_target_named=False)
        is UpdateSkipReason.WORKSPACE_UNREACHABLE
    )


def test_running_chats_stop_the_run() -> None:
    assert decide_skip_reason(_conditions(is_quiet=False), is_target_named=False) is UpdateSkipReason.CHATS_RUNNING


def test_a_named_target_is_not_measured_against_the_app_s_release_ceiling() -> None:
    """The user named the ref, and only the run itself can tell whether the workspace is already on it."""
    assert decide_skip_reason(_conditions(is_offered=False), is_target_named=True) is None


def test_a_named_target_does_not_excuse_the_other_gates() -> None:
    assert decide_skip_reason(_conditions(is_quiet=False), is_target_named=True) is UpdateSkipReason.CHATS_RUNNING


class _SchedulerHarness:
    """A scheduler whose every outside dependency is a recorder."""

    def __init__(
        self,
        records_dir: Path,
        *,
        now: datetime,
        host_state: HostState | None,
        window: tuple[int, int] = (2, 5),
    ) -> None:
        self.dispatched: list[AgentId] = []
        self.dispatched_targets: list[str] = []
        self.stopped: list[AgentId] = []
        self.is_dispatch_successful = True
        self.conditions = _conditions()
        self.now = now
        self.schedule_store = UpdateScheduleStore(records_dir=records_dir)
        self.state_store = WorkspaceUpdateStateStore(schedule_store=self.schedule_store)
        self.scheduler = UpdateScheduler(
            schedule_store=self.schedule_store,
            read_update_window=lambda: window,
            read_conditions=lambda _agent_id: self.conditions,
            read_host_state=lambda _agent_id: host_state,
            dispatch=self._dispatch,
            stop_workspace=self.stopped.append,
            now=lambda: self.now,
        )

    def _dispatch(self, agent_id: AgentId, target_ref: str) -> bool:
        self.dispatched.append(agent_id)
        self.dispatched_targets.append(target_ref)
        return self.is_dispatch_successful


def test_an_armed_intent_s_target_reaches_the_dispatch(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id, target_ref="minds-v0.5.0")

    harness.scheduler.run_window_pass()

    assert harness.dispatched == [agent_id]
    assert harness.dispatched_targets == ["minds-v0.5.0"]


def test_a_pass_outside_the_window_does_nothing(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(12), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)

    harness.scheduler.run_window_pass()

    assert harness.dispatched == []


def test_a_pass_inside_the_window_dispatches_the_armed_intent(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)

    harness.scheduler.run_window_pass()

    assert harness.dispatched == [agent_id]


def test_a_multi_hour_window_is_one_attempt_not_one_per_tick(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)

    harness.scheduler.run_window_pass()
    harness.scheduler.run_window_pass()

    assert len(harness.dispatched) == 1


def test_the_next_window_gets_its_own_attempt(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)
    harness.scheduler.run_window_pass()

    harness.now = _at(3) + timedelta(days=1)
    harness.scheduler.run_window_pass()

    assert len(harness.dispatched) == 2


def test_a_window_that_crosses_midnight_is_still_one_attempt(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(23), host_state=HostState.RUNNING, window=(23, 3))
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)
    harness.scheduler.run_window_pass()

    harness.now = _at(1) + timedelta(days=1)
    harness.scheduler.run_window_pass()

    assert len(harness.dispatched) == 1


def test_an_armed_intent_is_on_the_row_the_moment_it_is_written_and_off_it_when_cancelled(tmp_path: Path) -> None:
    """The row composes the schedule store's own record, so there is no publish step to forget."""
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(12), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    changes: list[None] = []
    harness.state_store.add_on_change_callback(lambda: changes.append(None))
    harness.schedule_store.schedule(agent_id, target_ref="minds-v0.5.0")

    state = harness.state_store.get(agent_id)
    assert state.is_scheduled is True
    assert state.scheduled_target_ref == "minds-v0.5.0"
    assert len(changes) == 1

    harness.schedule_store.cancel(agent_id)
    assert harness.state_store.get(agent_id).is_scheduled is False
    assert len(changes) == 2


@pytest.mark.witnesses("workspace-updates.skipped-window")
def test_a_skipped_window_records_why_and_leaves_the_intent_armed(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)
    harness.conditions = _conditions(is_quiet=False)

    harness.scheduler.run_window_pass()

    record = harness.schedule_store.read(agent_id)
    assert record is not None
    assert record.last_skip_reason == UpdateSkipReason.CHATS_RUNNING.value
    assert harness.dispatched == []


def test_a_machine_that_no_longer_needs_the_update_loses_the_intent(tmp_path: Path) -> None:
    """Re-armed, it would skip silently forever with no scheduled badge left to cancel it from."""
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)
    harness.conditions = _conditions(is_offered=False)

    harness.scheduler.run_window_pass()

    assert harness.dispatched == []
    assert harness.schedule_store.read(agent_id) is None


@pytest.mark.witnesses(
    "workspace-updates.scheduled-version-override",
    partial="does not cover the route that records the named version, only the scheduled attempt that carries it",
)
def test_a_named_version_still_runs_on_a_machine_the_app_reads_as_up_to_date(tmp_path: Path) -> None:
    """Naming a ref is how the user reaches one this app's own ceiling would never offer."""
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id, target_ref="my-branch")
    harness.conditions = _conditions(is_offered=False)

    harness.scheduler.run_window_pass()

    assert harness.dispatched == [agent_id]
    assert harness.dispatched_targets == ["my-branch"]
    assert harness.schedule_store.read(agent_id) is not None


@pytest.mark.witnesses("workspace-updates.prior-run-state-restored")
def test_a_machine_that_was_stopped_is_put_back_after_its_run(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.STOPPED)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)
    harness.scheduler.run_window_pass()

    harness.scheduler.note_run_finished(agent_id, is_real_failure=False)

    assert harness.stopped == [agent_id]


def test_a_machine_the_user_had_running_is_left_running(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)
    harness.scheduler.run_window_pass()

    harness.scheduler.note_run_finished(agent_id, is_real_failure=False)

    assert harness.stopped == []


def test_a_dispatch_that_never_went_out_stays_armed_for_the_next_window(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.STOPPED)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)
    harness.is_dispatch_successful = False

    harness.scheduler.run_window_pass()

    assert harness.stopped == []
    record = harness.schedule_store.read(agent_id)
    assert record is not None
    assert record.last_skip_reason == UpdateSkipReason.DISPATCH_FAILED.value


@pytest.mark.witnesses("workspace-updates.failure-cancels-the-schedule")
def test_a_real_failure_cancels_the_schedule(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)

    harness.scheduler.note_run_finished(agent_id, is_real_failure=True)

    assert harness.schedule_store.read(agent_id) is None


def test_a_landed_update_also_disarms_the_intent(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(3), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id)

    harness.scheduler.note_run_finished(agent_id, is_real_failure=False)

    assert harness.schedule_store.read(agent_id) is None


def test_run_now_applies_the_same_gate_as_the_scheduled_path(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(12), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.conditions = _conditions(is_quiet=False)

    assert harness.scheduler.run_now(agent_id) is UpdateSkipReason.CHATS_RUNNING


def test_run_now_keeps_the_version_the_armed_intent_names(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(12), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id, target_ref="minds-v9.9.9")

    assert harness.scheduler.run_now(agent_id) is None

    assert harness.dispatched_targets == ["minds-v9.9.9"]


def test_run_now_runs_a_named_version_on_a_machine_the_app_reads_as_up_to_date(tmp_path: Path) -> None:
    harness = _SchedulerHarness(tmp_path / "schedules", now=_at(12), host_state=HostState.RUNNING)
    agent_id = AgentId.generate()
    harness.schedule_store.schedule(agent_id, target_ref="my-branch")
    harness.conditions = _conditions(is_offered=False)

    assert harness.scheduler.run_now(agent_id) is None

    assert harness.dispatched_targets == ["my-branch"]


@pytest.mark.witnesses("workspace-updates.re-arming-replaces")
def test_re_arming_replaces_the_previous_intent_and_its_skip_reason(tmp_path: Path) -> None:
    store = UpdateScheduleStore(records_dir=tmp_path / "schedules")
    agent_id = AgentId.generate()
    first = store.schedule(agent_id)
    store.record_skip(agent_id, UpdateSkipReason.CHATS_RUNNING.value)

    store.schedule(agent_id)

    record = store.read(agent_id)
    assert record is not None
    assert record.last_skip_reason == ""
    assert record.created_at >= first.created_at


def test_an_intent_survives_the_store_being_reopened(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    UpdateScheduleStore(records_dir=tmp_path / "schedules").schedule(agent_id)

    reopened = UpdateScheduleStore(records_dir=tmp_path / "schedules")

    assert [record.agent_id for record in reopened.list_records()] == [str(agent_id)]
