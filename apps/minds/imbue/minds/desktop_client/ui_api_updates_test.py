"""Tests for the /ui/api/updates routes: who may dispatch, and what a refusal says."""

import json
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import blocking_release_wait_body
from imbue.minds.desktop_client.testing import landed_verdict
from imbue.minds.desktop_client.testing import update_run_probe_stdout
from imbue.minds.desktop_client.testing import write_stub_mngr
from imbue.minds.desktop_client.ui_api_updates import build_workspace_updates_message
from imbue.minds.desktop_client.ui_api_updates import format_update_window
from imbue.minds.desktop_client.ui_api_updates import run_bulk_dispatch
from imbue.minds.desktop_client.update_chat import build_update_chat_message
from imbue.minds.desktop_client.update_service import UpdateDispatchOutcome
from imbue.minds.desktop_client.update_service import WorkspaceUpdateService
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateUnknownReason
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.update_status import describe_skip_reason
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.utils.polling import poll_until

_SKILL_PRESENT_STDOUT = "MNGR_UPDATE_SELF_SKILL_PRESENT\n"
_SKILL_ABSENT_STDOUT = "MNGR_UPDATE_SELF_SKILL_ABSENT\n"


class _SystemServicesResolver(StaticBackendResolver):
    """A resolver on which every workspace agent is its own host's system-services agent.

    True of a real minds workspace -- the primary agent *is* ``system-services`` --
    and what the host lifecycle action resolves against before it starts anything.
    """

    def get_system_services_agent_id(self, workspace_agent_id: AgentId) -> AgentId | None:
        return workspace_agent_id

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id="host-" + "a" * 32)


class _RunningHostResolver(_SystemServicesResolver):
    """A resolver on which every workspace is on a running host."""

    def get_host_state(self, host_id: HostId) -> HostState | None:
        return HostState.RUNNING


def _host_actions(tmp_path: Path) -> list[str]:
    """The ``mngr`` argv lines the stub binary recorded, one per invocation."""
    record = tmp_path / "host_actions.txt"
    if not record.exists():
        return []
    return [line for line in record.read_text().splitlines() if line]


def _build_client(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    *,
    mngr_result: MngrCallResult | None = None,
    mngr_caller: MngrCaller | None = None,
    is_host_running: bool = False,
    mngr_binary: str | None = None,
) -> tuple[FlaskClient, Flask]:
    """An app with the update machinery wired over exactly one of ``mngr_result`` / ``mngr_caller``.

    ``mngr_caller`` covers the in-workspace commands. Host stops and starts do not
    go through it -- they shell out to ``mngr`` -- so those land in the stub binary,
    readable via :func:`_host_actions`.
    """
    assert (mngr_result is None) != (mngr_caller is None), "pass exactly one of mngr_result / mngr_caller"
    if mngr_caller is not None:
        caller = mngr_caller
    else:
        assert mngr_result is not None
        caller = RecordingMngrCaller(result=mngr_result)
    resolver = _RunningHostResolver if is_host_running else _SystemServicesResolver
    client, app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver(url_by_agent_and_service={}),
        paths=InstallationPaths(data_dir=tmp_path / "data"),
        mngr_caller=caller,
        mngr_binary=mngr_binary if mngr_binary is not None else _write_recording_stub(tmp_path, "stub_mngr"),
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        root_concurrency_group=root_concurrency_group,
    )
    return client, app


def _write_recording_stub(tmp_path: Path, name: str, *, extra_body: str = "", exit_code: int = 0) -> str:
    """A stub ``mngr`` that appends its argv to the file :func:`_host_actions` reads."""
    record = tmp_path / "host_actions.txt"
    return write_stub_mngr(tmp_path, name, f'echo "$@" >> {record}\n{extra_body}\nexit {exit_code}')


def _service(app: Flask) -> WorkspaceUpdateService:
    with app.app_context():
        service = get_state().workspace_update_service
    assert service is not None, "the test app should have wired the update service"
    return service


def _mark_out_of_date(app: Flask, agent_id: AgentId) -> None:
    _service(app).state_store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.OUT_OF_DATE),
        current_version="minds-v0.3.9",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )


def _post(client: FlaskClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(path, json=body if body is not None else {})


@pytest.mark.witnesses("workspace-updates.unknown-workspace-is-dispatchable")
@pytest.mark.parametrize(
    "unknown_reason", (UpdateUnknownReason.NO_MACHINE_VERSION, UpdateUnknownReason.NO_APP_VERSION)
)
def test_a_machine_with_no_readable_version_is_still_sent_its_update_agent(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    unknown_reason: UpdateUnknownReason,
) -> None:
    """Both unknowns dispatch: the machine's own agent reads its upstream."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _service(app).state_store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.UNKNOWN, unknown_reason=unknown_reason),
        current_version="",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    assert _service(app).state_store.get(agent_id).is_run_in_flight is True


@pytest.mark.witnesses("workspace-updates.too-old-to-update-in-place", partial="the dispatch refusal only")
@pytest.mark.parametrize(
    "availability",
    (UpdateAvailability.UP_TO_DATE, UpdateAvailability.APP_BEHIND, UpdateAvailability.NEEDS_RECREATION),
)
def test_a_machine_read_as_having_nothing_to_run_is_refused(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    availability: UpdateAvailability,
) -> None:
    """The readings that are positive answers rather than missing ones, the too-old machine included."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _service(app).state_store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=availability),
        current_version="minds-v0.4.1",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 409


@pytest.mark.witnesses("workspace-updates.version-override")
def test_an_explicit_ref_dispatches_a_machine_the_gate_would_refuse(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The override reaches the spawned chat's seed prompt."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    service = _service(app)
    service.state_store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.UP_TO_DATE),
        current_version="minds-v0.4.1",
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )

    response = _post(client, f"/ui/api/updates/{agent_id}/now", {"target_ref": "minds-v0.5.0"})

    assert response.status_code == 200
    state = service.state_store.get(agent_id)
    assert state.is_run_in_flight is True
    assert state.target_override == "minds-v0.5.0"
    caller = service.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    spawn_argv = caller.calls[-1]
    assert build_update_chat_message(target_override="minds-v0.5.0", is_backup_configured=False) in spawn_argv[3]


def test_a_ref_that_could_read_as_a_flag_or_shell_text_is_refused(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now", {"target_ref": "--force; rm -rf /"})

    assert response.status_code == 400
    assert _service(app).state_store.get(agent_id).is_run_in_flight is False


@pytest.mark.witnesses("workspace-updates.template-too-old")
def test_a_machine_too_old_for_the_skill_is_refused_with_an_explanation(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """Spawning anyway would hang on an unknown slash command."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_ABSENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 409
    assert "too old" in response.get_json()["error"]


def test_an_unreachable_machine_answers_502_rather_than_409(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The same 502/409 split /assist makes."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=1, stderr="connection refused")
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 502


@pytest.mark.witnesses("workspace-updates.attended-dispatch", partial="the dispatch only, not the navigation")
def test_a_dispatched_update_marks_the_run_in_flight(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    assert _service(app).state_store.get(agent_id).is_run_in_flight is True
    # RUNNING, not STARTING: STARTING is what the stalled reconcile refuses to probe.
    assert _service(app).state_store.get(agent_id).activity is UpdateActivity.RUNNING


def test_a_poll_landing_mid_spawn_does_not_declare_the_run_stalled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A poll can land while the chat is still being created; STALLED is terminal."""
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        # No marker, no update agent: the reading that means "stalled".
        mngr_result=MngrCallResult(returncode=0, stdout=_GONE_AGENT_STDOUT),
    )
    service = _service(app)
    service.state_store.set_activity(agent_id, UpdateActivity.STARTING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.STARTING


def test_a_run_positively_gone_with_no_verdict_is_stalled_by_the_poll(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_result=MngrCallResult(returncode=0, stdout=_GONE_AGENT_STDOUT),
    )
    service = _service(app)
    assert service.state_store.try_begin_run(agent_id, chat_agent_name="update-abc123")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.STALLED


_GONE_AGENT_STDOUT = update_run_probe_stdout()
_WAITING_AGENT_STDOUT = update_run_probe_stdout(agents="update-abc123\tWAITING\n")
_RUNNING_AGENT_STDOUT = update_run_probe_stdout(agents="update-abc123\tRUNNING\n")


@pytest.mark.witnesses("workspace-updates.waiting-run-surfaced")
def test_a_run_reading_waiting_twice_in_a_row_is_surfaced_as_waiting(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """One WAITING sample is a turn boundary; two is an agent waiting on a person. Both are in flight."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_WAITING_AGENT_STDOUT)
    )
    service = _service(app)
    assert service.state_store.try_begin_run(agent_id, chat_agent_name="update-abc123")
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    service.poll_in_flight_runs()
    assert service.state_store.get(agent_id).activity is UpdateActivity.RUNNING

    service.poll_in_flight_runs()
    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.WAITING
    assert state.is_run_in_flight is True


def test_a_waiting_row_goes_back_to_running_the_moment_its_agent_moves(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """No debounce on the way back: a turn boundary cannot fake waking up."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_RUNNING_AGENT_STDOUT)
    )
    service = _service(app)
    assert service.state_store.try_begin_run(agent_id, chat_agent_name="update-abc123")
    service.state_store.set_activity(agent_id, UpdateActivity.WAITING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.RUNNING


def test_an_unanswered_probe_does_not_demote_a_waiting_row(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """An unanswered probe is not evidence that the agent moved."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=1, stdout=""))
    service = _service(app)
    service.state_store.set_activity(agent_id, UpdateActivity.WAITING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.WAITING


class _VerdictLandsMidProbeMngrCaller(RecordingMngrCaller):
    """Answers the liveness probe after running the test's hook, which plays a verdict landing mid-probe."""

    _on_probe: Callable[[], None] | None = PrivateAttr(default=None)

    def set_on_probe(self, hook: Callable[[], None]) -> None:
        self._on_probe = hook

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        result = super().call(argv, timeout, env_overrides, cwd)
        if self._on_probe is not None and any("MNGR_UPDATE_AGENTS_BEGIN" in arg for arg in argv):
            self._on_probe()
        return result


def test_a_verdict_landing_while_the_probe_is_in_flight_is_kept_over_the_poll_s_stalled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The agent goes DONE only after emitting its verdict, so a "gone" probe may be racing one; the verdict wins."""
    caller = _VerdictLandsMidProbeMngrCaller(result=MngrCallResult(returncode=0, stdout=_GONE_AGENT_STDOUT))
    _client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    service = _service(app)
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)
    caller.set_on_probe(
        lambda: service.state_store.adopt_run_record(
            agent_id, landed_verdict(UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1")
        )
    )

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is UpdateVerdict.UPDATED


@pytest.mark.witnesses("workspace-updates.one-run-per-workspace")
@pytest.mark.witnesses("workspace-updates.stopped-workspace-started")
def test_a_stopped_machine_is_started_before_its_update_runs(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A start no-ops on a running host, so the dispatch issues it unconditionally."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT))
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)

    _post(client, f"/ui/api/updates/{agent_id}/now")

    assert _host_actions(tmp_path) == [f"start {agent_id} --quiet"]
    assert caller.calls[0][0] == "exec", "and the start is not an in-workspace command"


def test_a_machine_the_app_stopped_may_be_recovered_again_once_an_update_starts_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """An update's start must clear the intentional-stop mark, exactly as a start the user asked for does.

    Nothing else can: the only other clear is a successful probe, and the probe
    loop polls suspect / stuck agents only, so a machine that came back healthy
    is never probed. A start that left the mark behind would leave the machine
    excluded from unattended recovery for the rest of the process's life -- and
    that exclusion only bites once it is already broken.
    """
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    with app.app_context():
        tracker = get_state().system_interface_health_tracker
    assert tracker is not None
    tracker.suppress_unattended_recovery(agent_id)
    _mark_out_of_date(app, agent_id)

    _post(client, f"/ui/api/updates/{agent_id}/now")

    assert tracker.is_unattended_recovery_suppressed(agent_id) is False


@pytest.mark.witnesses("workspace-updates.one-run-per-workspace")
def test_a_second_dispatch_loses_to_the_run_already_in_flight(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)
    _post(client, f"/ui/api/updates/{agent_id}/now")

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 409


def test_scheduling_arms_the_intent_without_any_handshake(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The no-backups confirmation is the SPA's to ask; the route takes a bare press."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule")

    assert response.status_code == 200
    assert _service(app).schedule_store.read(agent_id) is not None


@pytest.mark.witnesses("workspace-updates.no-backup-confirmation", partial="the wire flag only")
def test_the_frame_says_whether_each_machine_has_backups(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A machine without a canonical restic env reads as unbacked."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)

    message = build_workspace_updates_message(_service(app), (2, 5))

    assert message.updates[str(agent_id)].is_backup_configured is False


@pytest.mark.witnesses("workspace-updates.no-backup-confirmation", partial="the seed prompt only")
@pytest.mark.parametrize("is_backup_configured", (False, True))
def test_a_dispatch_carries_the_go_ahead_only_when_the_machine_has_no_backups(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, is_backup_configured: bool
) -> None:
    """The app collected that answer at the button, and a machine still on the older skill stops to ask for it."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    service = _service(app)
    _mark_out_of_date(app, agent_id)
    if is_backup_configured:
        assert service.paths is not None
        write_canonical_env(service.paths, agent_id, "RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n")

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    caller = service.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    spawn = [call for call in caller.calls if any("mngr create" in arg for arg in call)]
    assert len(spawn) == 1
    unbacked_message = build_update_chat_message(target_override=None, is_backup_configured=False)
    assert (unbacked_message in spawn[0][3]) is not is_backup_configured


def test_cancelling_disarms_the_intent(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)
    _post(client, f"/ui/api/updates/{agent_id}/schedule")

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule/cancel")

    assert response.status_code == 200
    assert _service(app).schedule_store.read(agent_id) is None


@pytest.mark.witnesses("workspace-updates.scheduled-version-override")
def test_a_scheduled_update_may_name_its_target_and_the_run_carries_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """Pressing the version field is the confirmation; scheduling only changes when the run happens."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    service = _service(app)
    # No availability gate: an up-to-date machine may still be pointed at a named ref.
    response = _post(client, f"/ui/api/updates/{agent_id}/schedule", {"target_ref": "minds-v0.5.0"})

    assert response.status_code == 200
    record = service.schedule_store.read(agent_id)
    assert record is not None
    assert record.target_ref == "minds-v0.5.0"
    assert service.state_store.get(agent_id).scheduled_target_ref == "minds-v0.5.0"

    assert service.dispatch_for_scheduler(agent_id, "minds-v0.5.0") is True
    caller = service.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    spawn = [call for call in caller.calls if any("mngr create" in arg for arg in call)]
    assert len(spawn) == 1
    assert build_update_chat_message(target_override="minds-v0.5.0", is_backup_configured=False) in spawn[0][3]
    assert service.state_store.get(agent_id).target_override == "minds-v0.5.0"


def test_a_press_that_names_no_target_keeps_the_one_the_schedule_is_armed_with(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The plain buttons sit under "Scheduled to update to X"; they must not retarget the machine."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    service = _service(app)
    _mark_out_of_date(app, agent_id)
    service.schedule_store.schedule(agent_id, target_ref="minds-v0.5.0")

    assert _post(client, f"/ui/api/updates/{agent_id}/schedule").status_code == 200
    rearmed = service.schedule_store.read(agent_id)
    assert rearmed is not None
    assert rearmed.target_ref == "minds-v0.5.0"

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 200
    assert service.state_store.get(agent_id).target_override == "minds-v0.5.0"


def test_a_bulk_schedule_leaves_an_armed_machines_named_target_alone(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A blanket schedule-all says nothing about versions, so it must not reset one."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    service = _service(app)
    _mark_out_of_date(app, agent_id)
    service.schedule_store.schedule(agent_id, target_ref="minds-v0.5.0")

    response = _post(client, "/ui/api/updates/bulk/schedule", {"agent_ids": [str(agent_id)]})

    assert response.status_code == 200
    record = service.schedule_store.read(agent_id)
    assert record is not None
    assert record.target_ref == "minds-v0.5.0"


def test_a_scheduled_target_is_refused_on_the_same_terms_as_a_now_target(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, _app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule", {"target_ref": "--flag"})

    assert response.status_code == 400


@pytest.mark.witnesses("workspace-updates.bulk-covers-only-confirmed-workspaces")
def test_a_bulk_action_filters_the_requested_list_against_live_state(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """An unknown machine is passed over here even though the single-machine route would take it."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    stale_id = AgentId.generate()
    unknown_id = AgentId.generate()
    running_id = AgentId.generate()
    _mark_out_of_date(app, stale_id)
    _mark_out_of_date(app, running_id)
    _service(app).state_store.set_activity(running_id, UpdateActivity.RUNNING)

    response = _post(
        client,
        "/ui/api/updates/bulk/schedule",
        {"agent_ids": [str(stale_id), str(unknown_id), str(running_id)]},
    )

    assert response.status_code == 200
    assert response.get_json()["scheduled"] == [str(stale_id)]


def test_bulk_now_answers_with_the_machines_it_accepted_and_passes_over_the_rest(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The route's own work, as distinct from the thread it starts."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    stale_id = AgentId.generate()
    unknown_id = AgentId.generate()
    running_id = AgentId.generate()
    _mark_out_of_date(app, stale_id)
    _mark_out_of_date(app, running_id)
    _service(app).state_store.set_activity(running_id, UpdateActivity.RUNNING)

    response = _post(
        client,
        "/ui/api/updates/bulk/now",
        {"agent_ids": [str(stale_id), str(unknown_id), str(running_id)]},
    )

    assert response.status_code == 200
    assert response.get_json()["dispatching"] == [str(stale_id)]


@pytest.mark.witnesses("workspace-updates.skipped-window", partial="the bulk-now path only")
def test_a_bulk_now_run_goes_through_the_schedule_gate_rather_than_dispatching_blind(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """An unreadable chat gate counts as agents working in the machine, so bulk-now skips it."""
    _client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT),
        is_host_running=True,
    )
    _mark_out_of_date(app, agent_id)
    service = _service(app)
    caller = service.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)

    with app.app_context():
        scheduler = get_state().update_scheduler

    run_bulk_dispatch(service, scheduler, [agent_id])

    assert service.state_store.get(agent_id).is_run_in_flight is False
    # The gate probe went out and nothing else did.
    assert [call[0] for call in caller.calls] == ["exec"]
    assert not any("mngr create" in argv for call in caller.calls for argv in call)


class _RaisingOnSpawnMngrCaller(RecordingMngrCaller):
    """Answers the start and the skill probe, then raises ``raised_type`` on the ``mngr create``.

    The warm-process transport is a socket, so a spawn can fail by raising
    rather than by exiting non-zero.
    """

    raised_type: type[Exception] = OSError

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        result = super().call(argv, timeout, env_overrides, cwd)
        if any("mngr create" in arg for arg in argv):
            raise self.raised_type("the spawn did not come back")
        return result


@pytest.mark.parametrize(
    "raised_type",
    [
        OSError,
        # A bug on the way out must unlock the row too.
        AttributeError,
    ],
    ids=["transport-failure", "bug"],
)
def test_a_spawn_that_raises_leaves_no_run_locked_behind_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, raised_type: type[Exception]
) -> None:
    """The stalled reconcile never probes STARTING, so a row left on it could never be unlocked."""
    _client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_caller=_RaisingOnSpawnMngrCaller(
            result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT), raised_type=raised_type
        ),
    )
    _mark_out_of_date(app, agent_id)
    service = _service(app)

    with pytest.raises(raised_type):
        service.dispatch_update(agent_id)

    assert service.state_store.get(agent_id).activity is UpdateActivity.IDLE
    assert service.state_store.get(agent_id).is_run_in_flight is False


def test_a_second_dispatch_loses_while_the_first_is_still_starting_the_machine(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The run slot is claimed before the slow start, so concurrent dispatchers cannot both land a run."""
    release_path = tmp_path / "release_start"
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT))
    _client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_caller=caller,
        mngr_binary=_write_recording_stub(
            tmp_path, "blocking_stub_mngr", extra_body=blocking_release_wait_body(release_path)
        ),
    )
    _mark_out_of_date(app, agent_id)
    service = _service(app)
    outcomes: list[UpdateDispatchOutcome] = []

    first = threading.Thread(
        target=lambda: outcomes.append(service.dispatch_update(agent_id)),
        name="first-dispatch",
        daemon=True,
    )
    first.start()
    try:
        # The stub records its argv before it blocks, so this is "the start is under way".
        assert poll_until(lambda: _host_actions(tmp_path) != [], timeout=10.0), "the dispatch should have started it"
        second = service.dispatch_update(agent_id)
    finally:
        release_path.touch()
        first.join(timeout=10.0)

    assert second is UpdateDispatchOutcome.ALREADY_RUNNING
    assert outcomes == [UpdateDispatchOutcome.DISPATCHED]
    assert _host_actions(tmp_path) == [f"start {agent_id} --quiet"]
    assert len([call for call in caller.calls if any("mngr create" in arg for arg in call)]) == 1


def test_a_dispatch_that_cannot_reach_the_machine_hands_the_run_slot_back(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """Every non-dispatch exit must release the claim, or a merely-down machine could never be retried."""
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=1, stderr="connection refused")
    )
    _mark_out_of_date(app, agent_id)

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 502

    assert _service(app).state_store.get(agent_id).activity is UpdateActivity.IDLE
    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 502


def test_a_machine_whose_start_fails_hands_the_run_slot_back(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The claim is taken before the start, so a start that could not bring the machine up must release it."""
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT),
        mngr_binary=_write_recording_stub(tmp_path, "failing_stub_mngr", exit_code=1),
    )
    _mark_out_of_date(app, agent_id)

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 502

    assert _service(app).state_store.get(agent_id).activity is UpdateActivity.IDLE


def test_every_update_route_needs_a_session(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=False,
        paths=InstallationPaths(data_dir=tmp_path / "data"),
        mngr_caller=RecordingMngrCaller(),
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        root_concurrency_group=root_concurrency_group,
    )

    assert client.post(f"/ui/api/updates/{agent_id}/now", json={}).status_code == 401
    assert client.post("/ui/api/updates/bulk/now", json={"agent_ids": []}).status_code == 401


def test_a_build_with_no_update_machinery_answers_503_rather_than_pretending(
    tmp_path: Path, agent_id: AgentId
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    assert client.post(f"/ui/api/updates/{agent_id}/now", json={}).status_code == 503


def test_the_update_window_reads_as_a_clock() -> None:
    assert format_update_window((2, 5)) == "2:00 AM-5:00 AM"
    assert format_update_window((23, 3)) == "11:00 PM-3:00 AM"
    assert format_update_window((0, 12)) == "12:00 AM-12:00 PM"


def test_the_update_state_rides_the_bootstrap_document_into_first_paint(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch, agent_id: AgentId
) -> None:
    """The badge must show on first paint, not a frame later."""
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_ui_manifest(tmp_path)))
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    _mark_out_of_date(app, agent_id)

    html = client.get("/ui/").get_data(as_text=True)

    bootstrap = json.loads(html.split("window.__MINDS_BOOTSTRAP__ = ", 1)[1].split(";</script>", 1)[0])
    updates = bootstrap["snapshot"]["workspace_updates"]
    assert updates["type"] == "workspace_updates"
    assert updates["update_window"] == "2:00 AM-5:00 AM"
    assert updates["updates"][str(agent_id)]["availability"] == "OUT_OF_DATE"
    assert updates["updates"][str(agent_id)]["current_version"] == "minds-v0.3.9"
    assert updates["updates"][str(agent_id)]["supported_version"] == "minds-v0.4.1"


def _write_ui_manifest(tmp_path: Path) -> Path:
    """A minimal Vite manifest, so the index route serves rather than 503ing."""
    manifest_path = tmp_path / "ui-manifest.json"
    manifest_path.write_text(
        json.dumps({"src/index.ts": {"file": "assets/boot.js", "isEntry": True, "css": ["assets/boot.css"]}})
    )
    return manifest_path


def test_a_skip_reason_from_a_newer_build_is_dropped_rather_than_shown_raw() -> None:
    assert describe_skip_reason("SOME_REASON_THIS_BUILD_HAS_NEVER_HEARD_OF") == ""
    assert describe_skip_reason("") == ""


@pytest.mark.witnesses(
    "workspace-updates.unknown-names-the-missing-side", partial="the channel frame only, not the modal copy"
)
def test_the_updates_frame_carries_which_side_had_no_version(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The modal cannot tell the two unknowns apart if the frame flattens them."""
    _client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    service = _service(app)
    service.state_store.record_detection(
        agent_id,
        detection=UpdateDetection(
            availability=UpdateAvailability.UNKNOWN, unknown_reason=UpdateUnknownReason.NO_APP_VERSION
        ),
        current_version="minds-v0.3.9",
        supported_version="gabriel/some-branch",
        is_version_from_label=False,
    )

    frame = build_workspace_updates_message(service, update_window=(2, 5))

    published = frame.updates[str(agent_id)]
    assert published.unknown_reason is UpdateUnknownReason.NO_APP_VERSION
    # Both refs travel: the modal shows the real version beside the branch name.
    assert published.current_version == "minds-v0.3.9"
    assert published.supported_version == "gabriel/some-branch"


@pytest.mark.witnesses("workspace-updates.updated-note", partial="dismissal only")
def test_dismissing_the_updated_note_leaves_a_failure_verdict_standing(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_SKILL_PRESENT_STDOUT)
    )
    state_store = _service(app).state_store
    state_store.set_activity(agent_id, UpdateActivity.RUNNING)
    state_store.adopt_run_record(agent_id, landed_verdict(UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1"))
    state_store.set_activity(agent_id, UpdateActivity.RUNNING)
    state_store.adopt_run_record(agent_id, landed_verdict(UpdateVerdict.STUCK, detail="it wedged"))

    assert _post(client, f"/ui/api/updates/{agent_id}/note/dismiss").status_code == 200

    after_note = state_store.get(agent_id)
    assert after_note.success_note_version == ""
    assert after_note.verdict is UpdateVerdict.STUCK

    assert _post(client, f"/ui/api/updates/{agent_id}/dismiss").status_code == 200

    assert state_store.get(agent_id).verdict is None
