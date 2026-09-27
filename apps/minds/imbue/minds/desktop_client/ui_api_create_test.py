import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import build_desktop_client_with_accounts
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import write_dead_destroy_marker
from imbue.minds.desktop_client.workspace_defaults import DEFAULT_WORKSPACE_TEMPLATE_GIT_URL
from imbue.minds.desktop_client.workspace_defaults import FALLBACK_BRANCH
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import LaunchMode
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


def test_create_area_routes_require_a_session_cookie(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)
    for path in (
        "/ui/api/create/form-defaults",
        "/ui/api/create/landing-extras",
        f"/ui/api/create/attempts/{CreateAttemptId.generate()}",
    ):
        response = client.get(path)
        assert response.status_code == 401, path
    seeded = client.post(
        f"/ui/api/create/attempts/{CreateAttemptId.generate()}/welcome-chat", json=_welcome_chat_body()
    )
    assert seeded.status_code == 401


def _welcome_chat_body() -> dict[str, object]:
    return {
        "title": "Welcome",
        "turns": [
            {"role": "user", "text": "Wait.. what is honest software?"},
            {"role": "assistant", "text": "Software that works for you."},
        ],
    }


def test_form_defaults_exclude_byok_only_launch_modes_and_carry_region_context(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/create/form-defaults")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert "IMBUE_CLOUD" in payload["launch_modes"]
    assert "AWS" not in payload["launch_modes"]
    assert "GCP" not in payload["launch_modes"]
    assert "AZURE" not in payload["launch_modes"]
    assert payload["selected_launch_mode"] == "IMBUE_CLOUD"
    assert len(payload["docker_runtimes"]) > 0
    assert payload["selected_docker_runtime"] in payload["docker_runtimes"]
    # The BYOK backends merge into the same region machinery the form JS reads.
    assert set(payload["region_options_by_launch_mode"]) >= {"IMBUE_CLOUD", "VULTR", "AWS", "GCP", "AZURE"}
    assert payload["region_selected_by_launch_mode"]["AWS"] in payload["region_options_by_launch_mode"]["AWS"]
    assert payload["default_instance_type_by_backend"]["AWS"]
    assert payload["color"].startswith("#")
    assert payload["prefill"] is None
    assert payload["accounts"] == []


@pytest.mark.parametrize(
    ("user_ids", "configured_default", "expected_default"),
    [
        pytest.param(("user-a", "user-b"), "user-b", "user-b", id="signed-in-configured-default"),
        pytest.param(("user-current",), "user-departed", "user-current", id="sole-account-over-departed-default"),
        pytest.param(("user-a", "user-b"), "user-gone", "", id="departed-default-among-several"),
        pytest.param((), "user-gone", "", id="every-account-signed-out"),
    ],
)
def test_form_defaults_preselect_the_resolved_default_account(
    tmp_path: Path, user_ids: tuple[str, ...], configured_default: str, expected_default: str
) -> None:
    """A stored default that has signed out is never preselected; a sole signed-in account is instead."""
    client, _minds_config = build_desktop_client_with_accounts(
        tmp_path, user_ids, stored_default_account_id=configured_default
    )

    response = client.get("/ui/api/create/form-defaults")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert [account["user_id"] for account in payload["accounts"]] == list(user_ids)
    assert payload["default_account_id"] == expected_default


def test_form_defaults_report_what_each_local_backend_needs_from_this_machine(tmp_path: Path) -> None:
    """The form annotates the local backends from these, so every one must be described.

    The test app's FakeHostProbe describes a bare Linux machine (nothing on
    PATH, no devices), so every backend is missing and the local preset falls
    back to Linux's first choice, Docker.
    """
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/create/form-defaults")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    by_key = {entry["key"]: entry for entry in payload["local_prerequisites"]}
    assert set(by_key) == {"DOCKER", "RUNSC", "LIMA"}
    for entry in by_key.values():
        assert not entry["is_available"]
        assert entry["summary"]
        assert entry["docs_url"].startswith("https://")
    assert payload["local_launch_mode"] == "DOCKER"
    assert payload["local_launch_mode"] in payload["launch_modes"]


def test_form_defaults_describe_no_local_backend_when_the_app_has_no_probe(tmp_path: Path) -> None:
    """An app built without a probe (no concurrency group to run one under) still answers.

    It reports no prerequisites rather than reaching for docker, and the local
    preset's launch mode still has to be one the form offers.
    """
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True, host_probe=None)

    response = client.get("/ui/api/create/form-defaults")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["local_prerequisites"] == []
    assert payload["local_launch_mode"] in payload["launch_modes"]


def test_form_defaults_seed_the_shipped_template_repo_and_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Scrub any operator dev-loop vars left in the shell (`just minds-start`
    # sets them) so this test always sees the end-user defaults.
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
    monkeypatch.delenv("MINDS_WORKSPACE_GIT_URL", raising=False)
    monkeypatch.delenv("MINDS_WORKSPACE_BRANCH", raising=False)
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/create/form-defaults")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["git_url"] == DEFAULT_WORKSPACE_TEMPLATE_GIT_URL
    assert payload["branch"] == FALLBACK_BRANCH


# Observed once hanging for ~33 minutes on a leaked forked child blocked in read,
# with the test itself long finished; killing the child let the run continue, and
# it has not recurred. Retried rather than diagnosed: the leak is in the fork, not
# in what this test asserts, and a hang has no failure to read.
@pytest.mark.flaky
def test_form_defaults_honor_the_operator_worktree_only_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)
    monkeypatch.setenv("MINDS_WORKSPACE_GIT_URL", "/home/operator/default-workspace-template")
    monkeypatch.setenv("MINDS_WORKSPACE_BRANCH", "mngr/dev-branch")

    # Stray MINDS_WORKSPACE_* vars without the explicit opt-in are ignored.
    unopted = json.loads(client.get("/ui/api/create/form-defaults").get_data(as_text=True))
    assert unopted["git_url"] == DEFAULT_WORKSPACE_TEMPLATE_GIT_URL
    assert unopted["branch"] == FALLBACK_BRANCH

    monkeypatch.setenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", "1")
    opted = json.loads(client.get("/ui/api/create/form-defaults").get_data(as_text=True))
    assert opted["git_url"] == "/home/operator/default-workspace-template"
    assert opted["branch"] == "mngr/dev-branch"


def test_form_defaults_ignore_an_unknown_retry_id(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get(f"/ui/api/create/form-defaults?retry={CreateAttemptId.generate()}")

    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True))["prefill"] is None


def test_landing_extras_render_empty_state_for_a_minimal_app(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/create/landing-extras")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["destroying_status_by_agent_id"] == {}
    assert payload["locked_account_emails"] == []
    assert isinstance(payload["is_discovery_complete"], bool)
    assert isinstance(payload["has_restorable_workspaces"], bool)


def _landing_extras_with_failed_destroy(tmp_path: Path, is_workspace_still_active: bool) -> tuple[str, dict]:
    """Landing extras for one workspace whose destroy exited non-zero; returns (agent id, payload)."""
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    write_dead_destroy_marker(paths, agent_id, HostId.generate(), exit_code=137)
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="a@b.com")
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(agent_id),
        host_id=str(HostId.generate()),
        display_name="half-destroyed",
        color="#3c3d06",
        is_cloud_row=False,
    )
    if not is_workspace_still_active:
        # This device's record reconcile tombstones a record once discovery stops
        # listing its host, which can happen before anyone looks at the failure.
        assert session_store.record_store is not None
        session_store.record_store.tombstone_record("user-1", "a@b.com", str(agent_id))
    active_agents: dict[str, dict[str, str]] = {str(agent_id): {}} if is_workspace_still_active else {}
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service=active_agents),
        paths=paths,
        session_store=session_store,
        imbue_cloud_cli=cli,
    )

    response = client.get("/ui/api/create/landing-extras")

    assert response.status_code == 200
    return str(agent_id), json.loads(response.get_data(as_text=True))


def test_landing_extras_surface_a_failed_destroy_whose_host_is_gone(tmp_path: Path) -> None:
    """A destroy that failed after its host went away has no row of its own, so extras supply one."""
    agent_id, payload = _landing_extras_with_failed_destroy(tmp_path, is_workspace_still_active=False)

    assert payload["destroying_status_by_agent_id"] == {agent_id: "failed"}
    assert payload["orphaned_failed_destroys"] == [
        {"agent_id": agent_id, "name": "half-destroyed", "accent": "#3c3d06"}
    ]


def test_landing_extras_leave_a_failed_destroy_with_a_live_row_to_that_row(tmp_path: Path) -> None:
    agent_id, payload = _landing_extras_with_failed_destroy(tmp_path, is_workspace_still_active=True)

    assert payload["destroying_status_by_agent_id"] == {agent_id: "failed"}
    assert payload["orphaned_failed_destroys"] == []


def test_create_attempt_detail_reports_gone_for_unknown_and_malformed_ids(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    unknown = client.get(f"/ui/api/create/attempts/{CreateAttemptId.generate()}")
    malformed = client.get("/ui/api/create/attempts/not-a-real-id")

    assert unknown.status_code == 200
    assert json.loads(unknown.get_data(as_text=True))["kind"] == "gone"
    assert malformed.status_code == 200
    assert json.loads(malformed.get_data(as_text=True))["kind"] == "gone"


def _record(
    create_attempt_id: str,
    state: PendingCreateAttemptState,
    *,
    launch_mode: LaunchMode = LaunchMode.LIMA,
    error: str | None = None,
    log_tail: tuple[str, ...] = (),
    cloud_account: str = "",
    instance_type: str = "",
    account_id: str = "",
) -> PendingCreateAttemptRecord:
    now = datetime.now(timezone.utc)
    return PendingCreateAttemptRecord(
        create_attempt_id=create_attempt_id,
        state=state,
        provider_instance_name="lima",
        created_at=now,
        updated_at=now,
        error=error,
        log_tail=log_tail,
        request=PendingCreateAttemptRequest(
            repo_source="https://example.com/some-repo.git",
            host_name="row-test-name",
            display_name="Row Test Name",
            branch="feature-branch-7",
            launch_mode=launch_mode,
            account_email="owner@example.com",
            color="#a1b2c3",
            backup_api_key_env="",
            cloud_account=cloud_account,
            instance_type=instance_type,
            account_id=account_id,
        ),
    )


def _make_client_with_store(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    mngr_caller: MngrCaller | None = None,
    session_store: MultiAccountSessionStore | None = None,
) -> tuple[FlaskClient, PendingCreateAttemptStore, AgentCreator]:
    """A desktop-client test app whose agent creator carries a pending-create-attempt store.

    ``mngr_caller`` is what the app reaches workspaces through; the default leaves the app on
    the process-wide caller, which the routes here never use unless a test seeds a chat.
    ``session_store`` holds the signed-in accounts; the default has none.
    """
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    creator = AgentCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        pending_create_attempt_store=store,
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        agent_creator=creator,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        mngr_caller=mngr_caller,
        session_store=session_store,
    )
    return client, store, creator


def test_form_defaults_prefill_the_form_from_a_known_retry_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A ?retry naming a pending record restores the stored request into the prefill.

    The record names a BYOK cloud account that no longer exists (this test env
    has none configured) and an Imbue account that is no longer signed in (it
    has none signed in), so the prefill drops both while still threading the
    stored machine size through.
    """
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group)
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(
        _record(
            create_attempt_id,
            PendingCreateAttemptState.IN_FLIGHT,
            cloud_account="byok-gcp-ghost",
            instance_type="e2-standard-4",
            account_id="user-signed-out",
        )
    )

    response = client.get(f"/ui/api/create/form-defaults?retry={create_attempt_id}")

    assert response.status_code == 200
    prefill = json.loads(response.get_data(as_text=True))["prefill"]
    assert prefill is not None
    assert prefill["git_url"] == "https://example.com/some-repo.git"
    assert prefill["branch"] == "feature-branch-7"
    assert prefill["host_name"] == "Row Test Name"
    assert prefill["launch_mode"] == "LIMA"
    assert prefill["color"] == "#a1b2c3"
    assert prefill["instance_type"] == "e2-standard-4"
    # The ghost accounts are not offered, so they must not be pre-selected either.
    assert prefill["cloud_account"] == ""
    assert prefill["account_id"] == ""


def test_form_defaults_prefill_keeps_a_retried_account_that_is_still_signed_in(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-kept", email="kept@example.com")
    client, store, _creator = _make_client_with_store(
        tmp_path, root_concurrency_group, session_store=make_session_store_for_test(tmp_path / "session-store", cli)
    )
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.FAILED, account_id="user-kept"))

    response = client.get(f"/ui/api/create/form-defaults?retry={create_attempt_id}")

    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True))["prefill"]["account_id"] == "user-kept"


def test_create_attempt_detail_carries_error_and_log_tail_for_a_failed_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group)
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(
        _record(
            create_attempt_id,
            PendingCreateAttemptState.FAILED,
            error="clone blew up",
            log_tail=("line one of the tail", "line two of the tail"),
        )
    )

    response = client.get(f"/ui/api/create/attempts/{create_attempt_id}")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["kind"] == "record"
    record = payload["record"]
    assert record["state"] == "failed"
    assert record["workspace_name"] == "Row Test Name"
    assert record["error"] == "clone blew up"
    assert record["log_tail"] == ["line one of the tail", "line two of the tail"]


def test_create_attempt_detail_reports_an_in_flight_record_without_a_live_thread_as_interrupted(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group)
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.IN_FLIGHT))

    response = client.get(f"/ui/api/create/attempts/{create_attempt_id}")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["kind"] == "record"
    assert payload["record"]["state"] == "interrupted"
    assert payload["record"]["error"] is None


# Live-attempt detail: the creation page's facts (is_remote, expected duration,
# and the settings the attempt was submitted with).


def test_create_attempt_detail_carries_the_request_summary_for_a_live_attempt(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A live (in-flight) attempt's detail restates the settings it was submitted with.

    Pointing at a nonexistent local path (the same pattern agent_creator_test.py
    uses) fails fast in the background thread, but the attempt is genuinely
    live -- tracked by get_create_attempt_info -- for the brief window this
    test reads it in, same as the creation page's own polling would.
    """
    client, _store, creator = _make_client_with_store(tmp_path, root_concurrency_group)
    create_attempt_id = creator.start_create_attempt(
        "file:///nonexistent-repo-for-request-summary-test",
        host_name="request-summary-test",
        display_name="Request Summary Test",
        branch="v9.9.9-summary",
        launch_mode=LaunchMode.DOCKER,
    )

    response = client.get(f"/ui/api/create/attempts/{create_attempt_id}")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["kind"] == "live"
    live = payload["live"]
    assert live["is_remote"] is False
    assert live["expected_duration_seconds"] > 0
    assert live["workspace_name"] == "Request Summary Test"
    assert live["request"] == {
        "display_name": "Request Summary Test",
        "launch_mode": "DOCKER",
        "cloud_account": "",
        "backup_provider": "CONFIGURE_LATER",
        "region": "",
        "instance_type": "",
        "repository": "file:///nonexistent-repo-for-request-summary-test",
        "branch": "v9.9.9-summary",
    }


def test_create_attempt_detail_carries_the_request_summary_for_a_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group)
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(
        _record(create_attempt_id, PendingCreateAttemptState.FAILED, error="boom", instance_type="t3.large")
    )

    response = client.get(f"/ui/api/create/attempts/{create_attempt_id}")

    payload = json.loads(response.get_data(as_text=True))
    assert payload["record"]["request"] == {
        "display_name": "Row Test Name",
        "launch_mode": "LIMA",
        "cloud_account": "",
        "backup_provider": "CONFIGURE_LATER",
        "region": "",
        "instance_type": "t3.large",
        "repository": "https://example.com/some-repo.git",
        "branch": "feature-branch-7",
    }


_WORKSPACE_AGENT_ID = AgentId("agent-0123456789abcdef0123456789abcdef")


def _done_record(create_attempt_id: str) -> PendingCreateAttemptRecord:
    """A finished attempt's record: DONE, naming the workspace it made."""
    record = _record(create_attempt_id, PendingCreateAttemptState.DONE)
    return record.model_copy_update(
        to_update(record.field_ref().agent_id, str(_WORKSPACE_AGENT_ID)),
        to_update(record.field_ref().host_id, "host-0123456789abcdef0123456789abcdef"),
    )


def test_the_welcome_chat_is_seeded_in_the_finished_attempts_workspace(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A DONE record names the workspace; the conversation goes to it through the template's script."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout='{"chat_id": "agent-seeded"}\n'))
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, mngr_caller=caller)
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(_done_record(create_attempt_id))

    response = client.post(f"/ui/api/create/attempts/{create_attempt_id}/welcome-chat", json=_welcome_chat_body())

    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True)) == {"chat_id": "agent-seeded"}
    (argv,) = caller.calls
    assert argv[:3] == ["exec", "--agent", str(_WORKSPACE_AGENT_ID)]
    assert "system/scripts/seed_welcome_chat.py" in argv[3]


def test_the_welcome_chat_is_refused_while_the_attempt_has_no_workspace(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    caller = RecordingMngrCaller()
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, mngr_caller=caller)
    in_flight = str(CreateAttemptId.generate())
    store.write_record(_record(in_flight, PendingCreateAttemptState.IN_FLIGHT))

    assert (
        client.post(f"/ui/api/create/attempts/{in_flight}/welcome-chat", json=_welcome_chat_body()).status_code == 409
    )
    unknown = client.post(
        f"/ui/api/create/attempts/{CreateAttemptId.generate()}/welcome-chat", json=_welcome_chat_body()
    )
    assert unknown.status_code == 409
    assert client.post("/ui/api/create/attempts/not-an-id/welcome-chat", json=_welcome_chat_body()).status_code == 409
    assert caller.calls == []


def test_the_welcome_chat_refuses_a_body_without_turns_and_reports_a_workspace_that_would_not_take_it(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=1,
            stderr=(
                "Could not seed the welcome chat: could not connect\n"
                f"ERROR: Command failed on agent {_WORKSPACE_AGENT_ID}\n"
            ),
            is_mngr_output=True,
        )
    )
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, mngr_caller=caller)
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(_done_record(create_attempt_id))
    path = f"/ui/api/create/attempts/{create_attempt_id}/welcome-chat"

    assert client.post(path, json={"title": "Welcome", "turns": []}).status_code == 400
    assert client.post(path, data="not json", content_type="application/json").status_code == 400
    assert caller.calls == []

    refused = client.post(path, json=_welcome_chat_body())

    assert refused.status_code == 502
    assert json.loads(refused.get_data(as_text=True)) == {
        "error": "Couldn't open the welcome chat in the workspace.",
        "detail": "Could not seed the welcome chat: could not connect",
    }
