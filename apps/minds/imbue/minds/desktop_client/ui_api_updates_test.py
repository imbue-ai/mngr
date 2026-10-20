"""Tests for the /ui/api/updates routes: who may dispatch, and what each of the workspace's answers comes to."""

import json
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import CANCELLED_UPDATE_COMMAND_RESULT
from imbue.minds.desktop_client.testing import CREATED_UPDATE_COMMAND_RESULT
from imbue.minds.desktop_client.testing import CUT_OFF_ON_REACHED_HOST_EXEC_RESULT
from imbue.minds.desktop_client.testing import NEEDS_SIGN_IN_UPDATE_COMMAND_RESULT
from imbue.minds.desktop_client.testing import UPDATE_CHAT_ID
from imbue.minds.desktop_client.testing import UPDATE_CHAT_NAME
from imbue.minds.desktop_client.testing import armed_update_schedule_file
from imbue.minds.desktop_client.testing import begin_dispatched_update_run
from imbue.minds.desktop_client.testing import blocking_release_wait_body
from imbue.minds.desktop_client.testing import exec_error_stdout
from imbue.minds.desktop_client.testing import host_offline_exec_result
from imbue.minds.desktop_client.testing import landed_verdict
from imbue.minds.desktop_client.testing import update_command_answer
from imbue.minds.desktop_client.testing import update_program_invocation
from imbue.minds.desktop_client.testing import update_run_probe_stdout
from imbue.minds.desktop_client.testing import write_stub_mngr
from imbue.minds.desktop_client.ui_api_updates import build_workspace_updates_message
from imbue.minds.desktop_client.ui_api_updates import run_bulk_action
from imbue.minds.desktop_client.ui_models import UiWorkspaceUpdate
from imbue.minds.desktop_client.update_apply_window import AGENTS_BEGIN_SENTINEL
from imbue.minds.desktop_client.update_service import DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME
from imbue.minds.desktop_client.update_service import SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME
from imbue.minds.desktop_client.update_service import UpdateDispatch
from imbue.minds.desktop_client.update_service import UpdateDispatchOutcome
from imbue.minds.desktop_client.update_service import UpdateScheduleOutcome
from imbue.minds.desktop_client.update_service import WorkspaceUpdateService
from imbue.minds.desktop_client.update_service import _SIGN_IN_OPENED_SENTINEL
from imbue.minds.desktop_client.update_status import UpdateActivity
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateUnknownReason
from imbue.minds.desktop_client.update_status import UpdateVerdict
from imbue.minds.desktop_client.update_status import describe_skip_reason
from imbue.minds.desktop_client.update_status import format_update_window
from imbue.minds.desktop_client.update_status import parse_update_schedule_record
from imbue.minds.desktop_client.workspace_defaults import default_workspace_template_ref
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import HookedMngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.api.exec import COMMAND_EXECUTION_FAILURE_PREFIX
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.utils.polling import poll_until

_CEILING = default_workspace_template_ref()
_BULK_TIMEOUT_SECONDS = 10.0


def _armed(override_ref: str = "") -> MngrCallResult:
    return update_command_answer(
        "ARMED", mode="arm", schedule=armed_update_schedule_file(ceiling_ref=_CEILING, override_ref=override_ref)
    )


_TOO_OLD = update_command_answer("TOO_OLD_TO_UPDATE", "This workspace predates the update-self skill.")
# A command that never reached the machine because its host is down: what a stopped machine answers first.
_HOST_OFFLINE = host_offline_exec_result()
_HOST_OFFLINE_DETAIL = "Host 'host-1' is offline and automatic starting is disabled."


def _stopped_machine_caller() -> ScriptedMngrCaller:
    """A machine that is down until the dispatch starts it, and creates the chat once it has."""
    return ScriptedMngrCaller(results=(_HOST_OFFLINE, CREATED_UPDATE_COMMAND_RESULT))


class _SystemServicesResolver(StaticBackendResolver):
    """A resolver on which every workspace agent is its own host's system-services agent.

    True of a real Imbue Studio workspace -- the primary agent *is* ``system-services`` --
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
    minds_config: MindsConfig | None = None,
) -> tuple[FlaskClient, Flask]:
    """An app with the update machinery wired over exactly one of ``mngr_result`` / ``mngr_caller``.

    ``mngr_caller`` covers the in-workspace commands. Host starts do not go through
    it -- they shell out to ``mngr`` -- so those land in the stub binary, readable
    via :func:`_host_actions`.
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
        minds_config=minds_config,
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


def _recording_caller(app: Flask) -> RecordingMngrCaller:
    caller = _service(app).mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    return caller


def _mark(app: Flask, agent_id: AgentId, availability: UpdateAvailability, current_version: str) -> None:
    _service(app).state_store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=availability),
        current_version=current_version,
        supported_version="minds-v0.4.1",
        is_version_from_label=False,
    )


def _mark_out_of_date(app: Flask, agent_id: AgentId) -> None:
    _mark(app, agent_id, UpdateAvailability.OUT_OF_DATE, "minds-v0.3.9")


def _mark_patch_available(app: Flask, agent_id: AgentId) -> None:
    _mark(app, agent_id, UpdateAvailability.PATCH_AVAILABLE, "minds-v0.4.0")


def _mark_up_to_date(app: Flask, agent_id: AgentId) -> None:
    _mark(app, agent_id, UpdateAvailability.UP_TO_DATE, "minds-v0.4.1")


def _arm_on_the_row(app: Flask, agent_id: AgentId, override_ref: str) -> None:
    """The row as an arm naming ``override_ref`` left it."""
    schedule = parse_update_schedule_record(
        armed_update_schedule_file(ceiling_ref=_CEILING, override_ref=override_ref)
    )
    assert schedule is not None
    _service(app).state_store.record_armed_schedule(agent_id, schedule)


def _begin_running_run(app: Flask, agent_id: AgentId, chat_agent_name: str) -> None:
    begin_dispatched_update_run(_service(app).state_store, agent_id, chat_agent_name)


def _end_run_needing_decision(app: Flask, agent_id: AgentId) -> None:
    """The row as a run that ended needing the user's decision left it."""
    _service(app).state_store.observe_run_record(
        agent_id,
        UpdateRunStatus(
            chat_agent_name="update-deciding",
            started_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            verdict=UpdateVerdict.NEEDS_DECISION,
            verdict_at=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
        ),
    )
    assert _service(app).state_store.get(agent_id).is_decision_pending


def _post(client: FlaskClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(path, json=body if body is not None else {})


def _published(app: Flask, agent_id: AgentId) -> UiWorkspaceUpdate:
    """The machine's row as the next ``workspace_updates`` frame carries it, which every surface reads."""
    service = _service(app)
    return build_workspace_updates_message(service, (2, 5), service.minds_config).updates[str(agent_id)]


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
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
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


@pytest.mark.witnesses("workspace-updates.patch-available-by-hand")
def test_a_machine_with_only_a_patch_available_is_sent_its_update_agent_when_asked(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_patch_available(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    assert _service(app).state_store.get(agent_id).is_run_in_flight is True


def test_a_machine_with_only_a_patch_available_may_have_its_update_scheduled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=_armed())
    _mark_patch_available(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule")

    assert response.status_code == 200
    assert _published(app, agent_id).is_scheduled is True


@pytest.mark.witnesses("workspace-updates.too-old-to-update-in-place", partial="the dispatch refusal only")
@pytest.mark.parametrize(
    "availability",
    (UpdateAvailability.UP_TO_DATE, UpdateAvailability.APP_BEHIND, UpdateAvailability.NEEDS_RECREATION),
)
def test_a_machine_read_as_having_nothing_to_run_is_refused_without_asking_it(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    availability: UpdateAvailability,
) -> None:
    """The readings that are positive answers rather than missing ones, the too-old machine included."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark(app, agent_id, availability, "minds-v0.4.1")

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 409
    assert _post(client, f"/ui/api/updates/{agent_id}/schedule").status_code == 409

    assert _recording_caller(app).calls == []


@pytest.mark.witnesses("workspace-updates.version-override", partial="the target reaching the workspace")
def test_an_explicit_ref_dispatches_a_machine_the_gate_would_refuse(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_up_to_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now", {"target_ref": "minds-v0.5.0"})

    assert response.status_code == 200
    state = _service(app).state_store.get(agent_id)
    assert state.is_run_in_flight is True
    assert state.target_override == "minds-v0.5.0"
    mode, flags = update_program_invocation(_recording_caller(app).calls[-1])
    assert (mode, flags["--override"]) == ("now", "minds-v0.5.0")


@pytest.mark.parametrize("route", ("now", "schedule"))
def test_a_ref_that_could_read_as_a_flag_or_shell_text_is_refused(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, route: str
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/{route}", {"target_ref": "--force; rm -rf /"})

    assert response.status_code == 400
    assert _recording_caller(app).calls == []
    assert _service(app).state_store.get(agent_id).is_run_in_flight is False


@pytest.mark.witnesses("workspace-updates.template-too-old")
def test_a_machine_too_old_for_the_skill_is_refused_with_an_explanation_and_nothing_more(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """An empty detail key would render as an empty block under the refusal."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=_TOO_OLD)
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 409
    assert response.get_json() == {"error": DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.UNSUPPORTED]}


@pytest.mark.witnesses("workspace-updates.needs-sign-in", partial="the Update now answer and row")
def test_an_update_now_refused_for_sign_in_answers_409_and_marks_the_row_for_sign_in(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The row's sign-in state is the answer; a failure line beside it would say it twice."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=NEEDS_SIGN_IN_UPDATE_COMMAND_RESULT)
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 409
    assert response.get_json() == {"error": DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.NEEDS_SIGN_IN]}
    published = _published(app, agent_id)
    assert published.needs_sign_in is True
    assert published.dispatch_failure == ""
    assert published.is_run_in_flight is False


def test_an_update_now_on_a_machine_already_updating_answers_409_and_shows_that_run(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A run the app did not know of (say, a scheduled one) becomes the row's, with its chat to open."""
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_result=update_command_answer(
            "ALREADY_RUNNING",
            "An update is already running in this workspace.",
            chat_agent_name="update-to-minds-v0-8-9",
            chat_id=UPDATE_CHAT_ID,
        ),
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 409
    published = _published(app, agent_id)
    assert published.activity is UpdateActivity.RUNNING
    assert published.chat_agent_name == "update-to-minds-v0-8-9"
    assert published.chat_agent_id == UPDATE_CHAT_ID
    assert published.dispatch_failure == ""


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
def test_a_dispatched_update_runs_the_row_under_the_chat_the_workspace_created(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    published = _published(app, agent_id)
    # RUNNING, not STARTING: STARTING is what the stalled reconcile refuses to probe.
    assert published.activity is UpdateActivity.RUNNING
    assert published.chat_agent_name == UPDATE_CHAT_NAME
    assert published.chat_agent_id == UPDATE_CHAT_ID


# The run's chat not listed at all: the probe's positive "this run is over".
_GONE_AGENT_STDOUT = update_run_probe_stdout()
_WAITING_AGENT_STDOUT = update_run_probe_stdout(agents="update-abc123\tWAITING\n")
_RUNNING_AGENT_STDOUT = update_run_probe_stdout(agents="update-abc123\tRUNNING\n")


def test_a_poll_landing_mid_spawn_does_not_declare_the_run_stalled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A poll can land while the chat is still being created; STALLED is terminal."""
    _client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_GONE_AGENT_STDOUT)
    )
    service = _service(app)
    service.state_store.set_activity(agent_id, UpdateActivity.STARTING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.STARTING


def test_a_run_positively_gone_with_no_verdict_is_stalled_by_the_poll(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    _client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_GONE_AGENT_STDOUT)
    )
    _begin_running_run(app, agent_id, "update-abc123")

    _service(app).poll_in_flight_runs()

    assert _service(app).state_store.get(agent_id).activity is UpdateActivity.STALLED


@pytest.mark.witnesses("workspace-updates.waiting-run-surfaced")
def test_a_run_reading_waiting_twice_in_a_row_is_surfaced_as_waiting(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """One WAITING sample is a turn boundary; two is an agent waiting on a person. Both are in flight."""
    _client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_WAITING_AGENT_STDOUT)
    )
    service = _service(app)
    _begin_running_run(app, agent_id, "update-abc123")

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
    _client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=0, stdout=_RUNNING_AGENT_STDOUT)
    )
    service = _service(app)
    _begin_running_run(app, agent_id, "update-abc123")
    service.state_store.set_activity(agent_id, UpdateActivity.WAITING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.RUNNING


def test_an_unanswered_probe_does_not_demote_a_waiting_row(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """An unanswered probe is not evidence that the agent moved."""
    _client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=MngrCallResult(returncode=1, stdout=""))
    service = _service(app)
    service.state_store.set_activity(agent_id, UpdateActivity.WAITING)

    service.poll_in_flight_runs()

    assert service.state_store.get(agent_id).activity is UpdateActivity.WAITING


def test_a_verdict_landing_while_the_probe_is_in_flight_is_kept_over_the_poll_s_stalled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The agent goes DONE only after emitting its verdict, so a "gone" probe may be racing one; the verdict wins."""
    caller = HookedMngrCaller(result=MngrCallResult(returncode=0, stdout=_GONE_AGENT_STDOUT))
    _client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    service = _service(app)
    service.state_store.set_activity(agent_id, UpdateActivity.RUNNING)

    def land_verdict_during_probe(argv: Sequence[str]) -> None:
        if any(AGENTS_BEGIN_SENTINEL in arg for arg in argv):
            service.state_store.adopt_run_record(
                agent_id, landed_verdict(UpdateVerdict.UPDATED, resulting_ref="minds-v0.4.1")
            )

    caller.set_on_call(land_verdict_during_probe)

    service.poll_in_flight_runs()

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.verdict is UpdateVerdict.UPDATED


@pytest.mark.witnesses("workspace-updates.one-run-per-workspace")
@pytest.mark.witnesses("workspace-updates.stopped-workspace-started")
def test_a_stopped_machine_is_started_once_before_its_update_runs_and_left_running(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The command that could not reach the machine is what starts it; the command then runs again, and nothing stops it."""
    caller = _stopped_machine_caller()
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    assert _host_actions(tmp_path) == [f"start {agent_id} --quiet"]
    assert [update_program_invocation(call)[0] for call in caller.calls] == ["now", "now"]
    assert _published(app, agent_id).activity is UpdateActivity.RUNNING


@pytest.mark.witnesses("workspace-updates.running-workspace-not-restarted")
def test_a_running_machine_is_updated_without_being_started(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A start against a running machine costs as much as the command and flips its row through STARTING for nothing."""
    caller = RecordingMngrCaller(result=CREATED_UPDATE_COMMAND_RESULT)
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    assert _host_actions(tmp_path) == []
    assert len(caller.calls) == 1


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
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=_stopped_machine_caller())
    with app.app_context():
        tracker = get_state().system_interface_health_tracker
    assert tracker is not None
    tracker.suppress_unattended_recovery(agent_id)
    _mark_out_of_date(app, agent_id)

    _post(client, f"/ui/api/updates/{agent_id}/now")

    assert tracker.is_unattended_recovery_suppressed(agent_id) is False


@pytest.mark.witnesses("workspace-updates.one-run-per-workspace")
def test_a_second_dispatch_loses_to_the_run_already_in_flight_without_asking_the_machine(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_out_of_date(app, agent_id)
    _post(client, f"/ui/api/updates/{agent_id}/now")

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 409
    assert len(_recording_caller(app).calls) == 1


@pytest.mark.witnesses("workspace-updates.no-backup-confirmation", partial="the wire flag only")
def test_the_frame_says_whether_each_machine_has_backups(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A machine without a canonical restic env reads as unbacked."""
    _client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_out_of_date(app, agent_id)

    message = build_workspace_updates_message(_service(app), (2, 5), None)

    assert message.updates[str(agent_id)].is_backup_configured is False


@pytest.mark.witnesses("workspace-updates.no-backup-confirmation", partial="the go-ahead reaching the workspace")
@pytest.mark.parametrize("is_backup_configured", (False, True))
def test_a_dispatch_tells_the_workspace_whether_the_machine_has_backups(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, is_backup_configured: bool
) -> None:
    """The workspace's program words the go-ahead into the run; the app collected the answer at the button."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    service = _service(app)
    _mark_out_of_date(app, agent_id)
    if is_backup_configured:
        paths = service.settings_source.paths
        assert paths is not None
        write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n")

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 200
    _mode, flags = update_program_invocation(_recording_caller(app).calls[0])
    assert flags["--backup-configured"] == ("1" if is_backup_configured else "0")


@pytest.mark.parametrize(
    ("result", "expected_status", "expected_outcome", "expected_detail"),
    (
        (
            NEEDS_SIGN_IN_UPDATE_COMMAND_RESULT,
            409,
            UpdateScheduleOutcome.NEEDS_SIGN_IN,
            "No AI account is signed in to this workspace.",
        ),
        (
            update_command_answer("NO_CRON", "This workspace has no cron program."),
            409,
            UpdateScheduleOutcome.NO_CRON,
            "This workspace has no cron program.",
        ),
        (_TOO_OLD, 409, UpdateScheduleOutcome.UNSUPPORTED, "This workspace predates the update-self skill."),
        (_HOST_OFFLINE, 409, UpdateScheduleOutcome.NOT_RUNNING, _HOST_OFFLINE_DETAIL),
        (
            CUT_OFF_ON_REACHED_HOST_EXEC_RESULT,
            502,
            UpdateScheduleOutcome.UNREACHABLE,
            f"{COMMAND_EXECUTION_FAILURE_PREFIX} workspace: Connection reset by peer",
        ),
    ),
    ids=("needs_sign_in", "no_cron", "too_old", "not_running", "cut_off"),
)
def test_a_schedule_the_workspace_did_not_arm_says_why_and_never_starts_it(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    result: MngrCallResult,
    expected_status: int,
    expected_outcome: UpdateScheduleOutcome,
    expected_detail: str,
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=result)
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule")

    assert response.status_code == expected_status
    assert response.get_json() == {
        "error": SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME[expected_outcome],
        "detail": expected_detail,
    }
    assert _published(app, agent_id).is_scheduled is False
    assert _host_actions(tmp_path) == []


def test_scheduling_arms_the_workspace_and_shows_on_the_row_at_once(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The no-backups confirmation is the SPA's to ask; the route takes a bare press."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=_armed())
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule")

    assert response.status_code == 200
    published = _published(app, agent_id)
    assert published.is_scheduled is True
    assert published.schedule_window == "2:00 AM-5:00 AM"
    mode, flags = update_program_invocation(_recording_caller(app).calls[0])
    assert (mode, flags["--ceiling"], flags["--override"]) == ("arm", _CEILING, "")


def test_cancelling_a_schedule_the_workspace_answered_takes_it_off_the_row(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_armed(), CANCELLED_UPDATE_COMMAND_RESULT))
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)
    _post(client, f"/ui/api/updates/{agent_id}/schedule")

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule/cancel")

    assert response.status_code == 200
    assert _published(app, agent_id).is_scheduled is False
    assert update_program_invocation(caller.calls[1])[0] == "cancel"


@pytest.mark.witnesses("workspace-updates.cancel-needs-the-workspace")
@pytest.mark.parametrize(
    ("route", "message"),
    (
        ("schedule/cancel", "Couldn't reach this workspace to cancel its scheduled update."),
        ("skip", "Couldn't reach this workspace to skip its update."),
    ),
)
def test_a_cancel_or_skip_that_cannot_reach_the_workspace_says_so_and_leaves_the_row_scheduled(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, route: str, message: str
) -> None:
    """The file is still armed in the workspace, so its cron will still start the run."""
    caller = ScriptedMngrCaller(results=(_armed(), _HOST_OFFLINE))
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_caller=caller,
        minds_config=MindsConfig(data_dir=tmp_path / "minds-config"),
    )
    _mark_out_of_date(app, agent_id)
    _post(client, f"/ui/api/updates/{agent_id}/schedule")

    response = _post(client, f"/ui/api/updates/{agent_id}/{route}")

    assert response.status_code == 502
    assert response.get_json() == {
        "error": message,
        "detail": _HOST_OFFLINE_DETAIL,
    }
    published = _published(app, agent_id)
    assert published.is_scheduled is True
    # A skip that did not reach the workspace is not recorded: the workspace will still run it.
    assert published.is_update_skipped is False


@pytest.mark.witnesses("workspace-updates.skip-this-update", partial="the route, the recorded skip, and the row")
def test_skipping_an_update_cancels_its_schedule_and_marks_the_row_skipped_for_this_release(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
    caller = ScriptedMngrCaller(results=(_armed(), CANCELLED_UPDATE_COMMAND_RESULT))
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller, minds_config=minds_config)
    _mark_out_of_date(app, agent_id)
    _post(client, f"/ui/api/updates/{agent_id}/schedule")

    response = _post(client, f"/ui/api/updates/{agent_id}/skip")

    assert response.status_code == 200
    assert minds_config.get_skipped_update_target(str(agent_id)) == _CEILING
    published = _published(app, agent_id)
    assert published.is_scheduled is False
    assert published.is_update_skipped is True
    assert update_program_invocation(caller.calls[1])[0] == "cancel"


@pytest.mark.parametrize(
    ("skipped_target", "is_update_skipped"),
    ((None, False), (_CEILING, True), ("minds-v0.3.0", False)),
    ids=("nothing_skipped", "this_release_skipped", "an_older_release_skipped"),
)
def test_the_frame_says_a_machine_is_skipped_only_for_the_release_the_app_is_on_now(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    skipped_target: str | None,
    is_update_skipped: bool,
) -> None:
    """Once the app moves to a newer release, a skip of the old one no longer holds the machine back."""
    minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
    if skipped_target is not None:
        minds_config.set_skipped_update_target(str(agent_id), skipped_target)
    _client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT, minds_config=minds_config
    )
    _mark_out_of_date(app, agent_id)

    frame = build_workspace_updates_message(_service(app), (2, 5), minds_config)

    assert frame.updates[str(agent_id)].is_update_skipped is is_update_skipped


@pytest.mark.parametrize("is_auto_update_enabled", (True, False, None), ids=("on", "off", "no_settings_storage"))
def test_the_frame_carries_the_auto_update_setting(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, is_auto_update_enabled: bool | None
) -> None:
    minds_config: MindsConfig | None = None
    if is_auto_update_enabled is not None:
        minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
        minds_config.set_auto_update_workspaces(is_auto_update_enabled)
    _client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_out_of_date(app, agent_id)

    frame = build_workspace_updates_message(_service(app), (2, 5), minds_config)

    assert frame.is_auto_update_enabled is bool(is_auto_update_enabled)


@pytest.mark.witnesses("workspace-updates.needs-sign-in", partial="the sign-in button's route")
@pytest.mark.parametrize(
    ("result", "expected_status"),
    (
        (MngrCallResult(returncode=0, stdout=f"{_SIGN_IN_OPENED_SENTINEL}\n"), 200),
        (MngrCallResult(returncode=1, stdout="", stderr="workspace-layout: no client connected"), 502),
    ),
    ids=("opened", "never_opened"),
)
def test_the_sign_in_route_answers_whether_the_workspace_opened_its_chat(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    result: MngrCallResult,
    expected_status: int,
) -> None:
    """A stopped workspace is not booted for it: the press has just navigated into a running one."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=result)

    response = _post(client, f"/ui/api/updates/{agent_id}/sign-in")

    assert response.status_code == expected_status
    calls = _recording_caller(app).calls
    assert len(calls) == 1
    assert calls[0][:3] == ["exec", "--agent", str(agent_id)]
    assert "--no-start" in calls[0]
    assert _host_actions(tmp_path) == []


@pytest.mark.witnesses("workspace-updates.scheduled-version-override", partial="the schedule records the version")
def test_a_scheduled_update_may_name_its_target_and_the_workspace_records_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """Pressing the version field is the confirmation; scheduling only changes when the run happens."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=_armed("minds-v0.5.0"))
    # No availability gate: an up-to-date machine may still be pointed at a named ref.
    _mark_up_to_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/schedule", {"target_ref": "minds-v0.5.0"})

    assert response.status_code == 200
    mode, flags = update_program_invocation(_recording_caller(app).calls[0])
    assert (mode, flags["--override"]) == ("arm", "minds-v0.5.0")
    assert _published(app, agent_id).override_ref == "minds-v0.5.0"


def test_a_press_that_names_no_target_keeps_the_one_the_schedule_is_armed_with(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The plain buttons sit under "Scheduled to update to X"; they must not retarget the machine."""
    caller = ScriptedMngrCaller(results=(_armed("minds-v0.5.0"), CREATED_UPDATE_COMMAND_RESULT))
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)
    _arm_on_the_row(app, agent_id, "minds-v0.5.0")

    assert _post(client, f"/ui/api/updates/{agent_id}/schedule").status_code == 200
    assert update_program_invocation(caller.calls[0])[1]["--override"] == "minds-v0.5.0"

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 200
    assert update_program_invocation(caller.calls[1])[1]["--override"] == "minds-v0.5.0"
    assert _service(app).state_store.get(agent_id).target_override == "minds-v0.5.0"


def test_a_bulk_schedule_leaves_an_armed_machines_named_target_alone(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A blanket schedule-all says nothing about versions, so it must not reset one."""
    caller = RecordingMngrCaller(result=_armed("minds-v0.5.0"))
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)
    _arm_on_the_row(app, agent_id, "minds-v0.5.0")

    response = _post(client, "/ui/api/updates/bulk/schedule", {"agent_ids": [str(agent_id)]})

    assert response.status_code == 200
    assert poll_until(lambda: len(caller.calls) == 1, timeout=_BULK_TIMEOUT_SECONDS)
    mode, flags = update_program_invocation(caller.calls[0])
    assert (mode, flags["--override"]) == ("arm", "minds-v0.5.0")


@pytest.mark.witnesses("workspace-updates.bulk-covers-only-confirmed-workspaces")
@pytest.mark.witnesses("workspace-updates.patch-available-not-prompted", partial="the bulk actions only")
@pytest.mark.parametrize(
    ("bulk_action", "accepted_key", "result"),
    (("now", "dispatching", CREATED_UPDATE_COMMAND_RESULT), ("schedule", "scheduled", _armed())),
)
def test_a_bulk_action_acts_only_on_the_requested_machines_live_state_says_are_behind(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    bulk_action: str,
    accepted_key: str,
    result: MngrCallResult,
) -> None:
    """An unknown machine is passed over here even though the single-machine route would take it, and so is
    one whose last run is waiting on the user's decision: its chat is where that is answered."""
    caller = RecordingMngrCaller(result=result)
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    stale_id = AgentId.generate()
    unknown_id = AgentId.generate()
    running_id = AgentId.generate()
    patch_id = AgentId.generate()
    deciding_id = AgentId.generate()
    _mark_out_of_date(app, stale_id)
    _mark_out_of_date(app, running_id)
    _mark_patch_available(app, patch_id)
    _mark_out_of_date(app, deciding_id)
    _begin_running_run(app, running_id, "update-already")
    _end_run_needing_decision(app, deciding_id)

    response = _post(
        client,
        f"/ui/api/updates/bulk/{bulk_action}",
        {"agent_ids": [str(stale_id), str(unknown_id), str(running_id), str(patch_id), str(deciding_id)]},
    )

    assert response.status_code == 200
    assert response.get_json()[accepted_key] == [str(stale_id)]
    assert poll_until(lambda: len(caller.calls) == 1, timeout=_BULK_TIMEOUT_SECONDS)
    assert caller.calls[0][2] == str(stale_id)


@pytest.mark.parametrize("route", ("now", "schedule"))
def test_a_machine_waiting_on_a_decision_is_not_sent_a_fresh_run(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, route: str
) -> None:
    """As the bulk routes refuse it, so does the machine's own route: the SPA's view can be a moment stale."""
    caller = RecordingMngrCaller(result=_armed())
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)
    _end_run_needing_decision(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/{route}", {"target_ref": "minds-v0.4.1"})

    assert response.status_code == 409
    assert caller.calls == []
    assert _published(app, agent_id).is_decision_pending


def _calls_for(caller: RecordingMngrCaller, agent_id: AgentId) -> list[list[str]]:
    return [call for call in caller.calls if call[2] == str(agent_id)]


@pytest.mark.witnesses(
    "workspace-updates.bulk-unsent-update-is-reported", partial="the other workspaces start, with no gate"
)
def test_bulk_now_starts_every_confirmed_out_of_date_machine_with_no_gate_on_whats_running_there(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Each machine gets the one "now" command and nothing before it: the workspace decides what to pause."""
    caller = RecordingMngrCaller(result=CREATED_UPDATE_COMMAND_RESULT)
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller, is_host_running=True)
    first_id = AgentId.generate()
    second_id = AgentId.generate()
    _mark_out_of_date(app, first_id)
    _mark_out_of_date(app, second_id)

    response = _post(client, "/ui/api/updates/bulk/now", {"agent_ids": [str(first_id), str(second_id)]})

    assert response.status_code == 200
    assert response.get_json()["dispatching"] == [str(first_id), str(second_id)]
    store = _service(app).state_store
    assert poll_until(
        lambda: all(store.get(a).activity is UpdateActivity.RUNNING for a in (first_id, second_id)),
        timeout=_BULK_TIMEOUT_SECONDS,
    )
    for each_id in (first_id, second_id):
        calls = _calls_for(caller, each_id)
        assert [update_program_invocation(call)[0] for call in calls] == ["now"]
        assert store.get(each_id).chat_agent_name == UPDATE_CHAT_NAME


def test_bulk_schedule_arms_every_confirmed_out_of_date_machine(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    caller = RecordingMngrCaller(result=_armed())
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    first_id = AgentId.generate()
    second_id = AgentId.generate()
    _mark_out_of_date(app, first_id)
    _mark_out_of_date(app, second_id)

    response = _post(client, "/ui/api/updates/bulk/schedule", {"agent_ids": [str(first_id), str(second_id)]})

    assert response.status_code == 200
    store = _service(app).state_store
    assert poll_until(
        lambda: all(store.get(a).is_scheduled for a in (first_id, second_id)), timeout=_BULK_TIMEOUT_SECONDS
    )
    assert sorted(update_program_invocation(call)[0] for call in caller.calls) == ["arm", "arm"]


class _OneMachineOfflineMngrCaller(RecordingMngrCaller):
    """Every machine answers ``result`` except ``offline_agent``, whose host stays down."""

    offline_agent: str

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        super().call(argv, timeout, env_overrides, cwd)
        return _HOST_OFFLINE if argv[2] == self.offline_agent else self.result


@pytest.mark.parametrize(
    ("bulk_action", "reachable_answer", "is_reachable_done", "offline_failure"),
    [
        (
            "now",
            CREATED_UPDATE_COMMAND_RESULT,
            lambda state: state.activity is UpdateActivity.RUNNING,
            "Couldn't reach this workspace to start the update.",
        ),
        (
            "schedule",
            _armed(),
            lambda state: state.is_scheduled,
            SCHEDULE_REFUSAL_MESSAGE_BY_OUTCOME[UpdateScheduleOutcome.NOT_RUNNING],
        ),
    ],
)
@pytest.mark.witnesses(
    "workspace-updates.bulk-unsent-update-is-reported", partial="the published row, not its rendering"
)
def test_a_machine_a_bulk_action_could_not_reach_says_why_on_its_row_while_the_rest_go_ahead(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    bulk_action: str,
    reachable_answer: MngrCallResult,
    is_reachable_done: Callable[[UiWorkspaceUpdate], bool],
    offline_failure: str,
) -> None:
    """The bulk route answers before any machine is tried, so the row is the only place the outcome can land."""
    offline_id = AgentId.generate()
    reachable_id = AgentId.generate()
    caller = _OneMachineOfflineMngrCaller(offline_agent=str(offline_id), result=reachable_answer)
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, offline_id)
    _mark_out_of_date(app, reachable_id)

    response = _post(
        client, f"/ui/api/updates/bulk/{bulk_action}", {"agent_ids": [str(offline_id), str(reachable_id)]}
    )

    assert response.status_code == 200
    assert poll_until(lambda: is_reachable_done(_published(app, reachable_id)), timeout=_BULK_TIMEOUT_SECONDS)
    published = _published(app, offline_id)
    assert published.dispatch_failure == offline_failure
    assert published.dispatch_failure_detail == _HOST_OFFLINE_DETAIL
    assert published.is_run_in_flight is False


def test_a_bulk_action_keeps_going_past_a_machine_whose_action_raised() -> None:
    """One failure must not cost the rest of the bulk action; its row reports its own state."""
    first_id = AgentId.generate()
    second_id = AgentId.generate()
    acted: list[AgentId] = []

    def act_on_one(agent_id: AgentId) -> None:
        acted.append(agent_id)
        if agent_id == first_id:
            raise OSError("the warm process's socket went away")

    run_bulk_action(act_on_one, [first_id, second_id])

    assert acted == [first_id, second_id]


class _RaisingMngrCaller(RecordingMngrCaller):
    """Raises ``raised_type`` on every call.

    The warm-process transport is a socket, so a command can fail by raising
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
        super().call(argv, timeout, env_overrides, cwd)
        raise self.raised_type("the command did not come back")


@pytest.mark.parametrize(
    "raised_type",
    [
        OSError,
        # A bug on the way out must unlock the row too.
        AttributeError,
    ],
    ids=["transport-failure", "bug"],
)
def test_a_dispatch_that_raises_leaves_no_run_locked_behind_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, raised_type: type[Exception]
) -> None:
    """The stalled reconcile never probes STARTING, so a row left on it could never be unlocked."""
    _client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_caller=_RaisingMngrCaller(raised_type=raised_type)
    )
    _mark_out_of_date(app, agent_id)
    service = _service(app)

    with pytest.raises(raised_type):
        service.dispatch_update(agent_id)

    state = service.state_store.get(agent_id)
    assert state.activity is UpdateActivity.IDLE
    assert state.is_run_in_flight is False
    # The request that raised carries no sentence of ours, so the row is where the reader learns of it.
    assert state.dispatch_failure == "Something went wrong starting the update in this workspace."


def test_a_second_dispatch_loses_while_the_first_is_still_starting_the_machine(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The run slot is claimed before the slow start, so concurrent dispatchers cannot both land a run."""
    release_path = tmp_path / "release_start"
    caller = _stopped_machine_caller()
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
    outcomes: list[UpdateDispatch] = []

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

    assert second.outcome is UpdateDispatchOutcome.ALREADY_RUNNING
    assert [dispatch.outcome for dispatch in outcomes] == [UpdateDispatchOutcome.DISPATCHED]
    assert _host_actions(tmp_path) == [f"start {agent_id} --quiet"]
    # The first dispatch's command before the start and its command after; the second made none.
    assert len(caller.calls) == 2


def test_a_dispatch_that_cannot_reach_the_machine_hands_the_run_slot_back(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """Every non-dispatch exit must release the claim, or a merely-down machine could never be retried."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=_HOST_OFFLINE)
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
        mngr_result=_HOST_OFFLINE,
        mngr_binary=_write_recording_stub(tmp_path, "failing_stub_mngr", exit_code=1),
    )
    _mark_out_of_date(app, agent_id)

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 502

    assert _service(app).state_store.get(agent_id).activity is UpdateActivity.IDLE


def test_every_update_route_needs_a_session(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = RecordingMngrCaller()
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=False,
        paths=InstallationPaths(data_dir=tmp_path / "data"),
        mngr_caller=caller,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        root_concurrency_group=root_concurrency_group,
    )

    for route in ("now", "schedule", "schedule/cancel", "skip", "sign-in", "dismiss", "note/dismiss"):
        assert client.post(f"/ui/api/updates/{agent_id}/{route}", json={}).status_code == 401, route
    for route in ("now", "schedule"):
        assert client.post(f"/ui/api/updates/bulk/{route}", json={"agent_ids": [str(agent_id)]}).status_code == 401
    assert caller.calls == []


def test_a_build_with_no_update_machinery_answers_503_rather_than_pretending(
    tmp_path: Path, agent_id: AgentId
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    assert client.post(f"/ui/api/updates/{agent_id}/now", json={}).status_code == 503
    assert client.post(f"/ui/api/updates/{agent_id}/skip", json={}).status_code == 503
    assert client.post("/ui/api/updates/bulk/now", json={"agent_ids": []}).status_code == 503


def test_an_unparseable_machine_id_is_refused_with_400(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)

    assert _post(client, "/ui/api/updates/not-an-agent-id/now").status_code == 400
    assert _recording_caller(app).calls == []


def test_the_update_window_reads_as_a_clock() -> None:
    assert format_update_window((2, 5)) == "2:00 AM-5:00 AM"
    assert format_update_window((23, 3)) == "11:00 PM-3:00 AM"
    assert format_update_window((0, 12)) == "12:00 AM-12:00 PM"


def test_the_update_state_rides_the_bootstrap_document_into_first_paint(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch, agent_id: AgentId
) -> None:
    """The badge must show on first paint, not a frame later."""
    monkeypatch.setenv("MINDS_UI_MANIFEST_PATH", str(_write_ui_manifest(tmp_path)))
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
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
    _client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
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

    frame = build_workspace_updates_message(service, (2, 5), None)

    published = frame.updates[str(agent_id)]
    assert published.unknown_reason is UpdateUnknownReason.NO_APP_VERSION
    # Both refs travel: the modal shows the real version beside the branch name.
    assert published.current_version == "minds-v0.3.9"
    assert published.supported_version == "gabriel/some-branch"


@pytest.mark.witnesses("workspace-updates.updated-note", partial="dismissal only")
def test_dismissing_the_updated_note_leaves_a_failure_verdict_standing(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
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


@pytest.mark.witnesses("workspace-updates.workspace-refuses-the-agent")
@pytest.mark.witnesses("workspace-updates.unsent-update-is-reported", partial="the published row, not its rendering")
@pytest.mark.parametrize("program_outcome", ("CREATE_FAILED", "CHAT_APP_UNREACHABLE"))
def test_a_machine_that_refuses_the_agent_has_its_own_words_shown_and_the_run_slot_released(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, program_outcome: str
) -> None:
    """The workspace's refusal is one the user clears themselves, so pressing Update again must be possible."""
    refusal = "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']"
    client, app = _build_client(
        tmp_path, root_concurrency_group, mngr_result=update_command_answer(program_outcome, refusal)
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 502
    assert response.get_json() == {"error": "Couldn't start the update agent in this workspace.", "detail": refusal}
    published = _published(app, agent_id)
    assert published.dispatch_failure == "Couldn't start the update agent in this workspace."
    assert published.dispatch_failure_detail == refusal
    assert published.activity is UpdateActivity.IDLE
    # Refused again on its merits, not as an update already in flight.
    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 502


def _press_update_now_and_fail_to_reach(client: FlaskClient, app: Flask, agent_id: AgentId) -> None:
    """Press Update now on a machine the command cannot reach even once started, and check the row recorded why."""
    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 502
    assert _published(app, agent_id).dispatch_failure == "Couldn't reach this workspace to start the update."


@pytest.mark.witnesses("workspace-updates.unsent-update-is-reported", partial="the published row, not its rendering")
def test_an_update_that_could_not_reach_the_machine_is_published_for_a_modal_opened_later(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The 502 answers only the request that pressed the button; a modal closed while it waited never reads it."""
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_result=MngrCallResult(
            returncode=1,
            stdout=exec_error_stdout("Could not reach host host-1234: connection refused"),
            stderr="WARNING: stale key dir for host-other\n",
            is_mngr_output=True,
        ),
    )
    _mark_out_of_date(app, agent_id)

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 502

    published = _published(app, agent_id)
    assert published.dispatch_failure == "Couldn't reach this workspace to start the update."
    assert published.dispatch_failure_detail == "Could not reach host host-1234: connection refused"
    assert published.activity is UpdateActivity.IDLE


def test_a_command_that_timed_out_is_published_as_unconfirmed_without_a_detail_it_never_had(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A timeout's stderr is the caller's own account of the argv, not anything the machine said."""
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_result=MngrCallResult(
            returncode=-1, is_timed_out=True, stderr="mngr exec --agent x timed out after 420s"
        ),
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 502
    assert "detail" not in response.get_json()
    published = _published(app, agent_id)
    assert published.dispatch_failure == DISPATCH_REFUSAL_MESSAGE_BY_OUTCOME[UpdateDispatchOutcome.UNCONFIRMED]
    assert published.dispatch_failure_detail == ""
    assert _host_actions(tmp_path) == []


@pytest.mark.witnesses("workspace-updates.unsent-update-is-reported", partial="the published row, not its rendering")
def test_an_update_whose_machine_would_not_start_publishes_the_starts_own_diagnosis(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A start that failed and a command that got no answer are different problems, so the row names which."""
    client, app = _build_client(
        tmp_path,
        root_concurrency_group,
        mngr_result=_HOST_OFFLINE,
        mngr_binary=_write_recording_stub(
            tmp_path,
            "failing_start_stub_mngr",
            extra_body='echo "ERROR: The box behind host-5821 is gone" >&2',
            exit_code=1,
        ),
    )
    _mark_out_of_date(app, agent_id)

    response = _post(client, f"/ui/api/updates/{agent_id}/now")

    assert response.status_code == 502
    assert response.get_json()["error"] == "Couldn't start this workspace to run the update."
    published = _published(app, agent_id)
    assert published.dispatch_failure == "Couldn't start this workspace to run the update."
    assert "ERROR: The box behind host-5821 is gone" in published.dispatch_failure_detail
    assert published.activity is UpdateActivity.IDLE


def test_an_update_that_goes_out_after_one_that_did_not_clears_the_failure(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """A row still saying the update could not start, over a run that did, would send the reader after a fixed fault."""
    caller = RecordingMngrCaller(result=_HOST_OFFLINE)
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)
    _press_update_now_and_fail_to_reach(client, app, agent_id)

    caller.result = CREATED_UPDATE_COMMAND_RESULT
    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 200

    published = _published(app, agent_id)
    assert published.activity is UpdateActivity.RUNNING
    assert published.dispatch_failure == ""
    assert published.dispatch_failure_detail == ""


@pytest.mark.witnesses("workspace-updates.unsent-update-is-reported", partial="dismissal only")
def test_dismissing_an_update_that_never_went_out_clears_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """There is no verdict or stall behind it, and the dismiss route must still take it off the row."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=_HOST_OFFLINE)
    _mark_out_of_date(app, agent_id)
    _press_update_now_and_fail_to_reach(client, app, agent_id)

    assert _post(client, f"/ui/api/updates/{agent_id}/dismiss").status_code == 200

    published = _published(app, agent_id)
    assert published.dispatch_failure == ""
    assert published.dispatch_failure_detail == ""


@pytest.mark.witnesses("workspace-updates.unsent-update-is-reported", partial="scheduling only")
@pytest.mark.parametrize("is_bulk", (False, True), ids=["single", "bulk"])
def test_scheduling_an_update_that_never_went_out_clears_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId, is_bulk: bool
) -> None:
    """The schedule is the newer request; a row still saying the last one didn't start would report a superseded press."""
    caller = RecordingMngrCaller(result=_HOST_OFFLINE)
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_caller=caller)
    _mark_out_of_date(app, agent_id)
    _press_update_now_and_fail_to_reach(client, app, agent_id)

    caller.result = _armed()
    response = (
        _post(client, "/ui/api/updates/bulk/schedule", {"agent_ids": [str(agent_id)]})
        if is_bulk
        else _post(client, f"/ui/api/updates/{agent_id}/schedule")
    )
    assert response.status_code == 200

    assert poll_until(lambda: _published(app, agent_id).is_scheduled, timeout=_BULK_TIMEOUT_SECONDS)
    published = _published(app, agent_id)
    assert published.dispatch_failure == ""
    assert published.dispatch_failure_detail == ""


def test_a_dispatch_that_loses_to_a_live_run_records_no_failure_over_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The run in flight is the answer to the press; a refusal recorded over it would read as a failure."""
    client, app = _build_client(tmp_path, root_concurrency_group, mngr_result=CREATED_UPDATE_COMMAND_RESULT)
    _mark_out_of_date(app, agent_id)
    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 200

    assert _post(client, f"/ui/api/updates/{agent_id}/now").status_code == 409

    published = _published(app, agent_id)
    assert published.activity is UpdateActivity.RUNNING
    assert published.dispatch_failure == ""
