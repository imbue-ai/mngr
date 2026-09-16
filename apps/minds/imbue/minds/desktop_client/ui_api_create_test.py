import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import write_dead_destroy_marker
from imbue.minds.desktop_client.workspace_defaults import DEFAULT_WORKSPACE_TEMPLATE_GIT_URL
from imbue.minds.desktop_client.workspace_defaults import FALLBACK_BRANCH
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import LaunchMode
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


# -- Record-backed retry prefill and attempt detail (mirrors the deleted
# -- create_attempt_rows_pages_test.py coverage) --


def _record(
    create_attempt_id: str,
    state: PendingCreateAttemptState,
    *,
    launch_mode: LaunchMode = LaunchMode.LIMA,
    error: str | None = None,
    log_tail: tuple[str, ...] = (),
    cloud_account: str = "",
    instance_type: str = "",
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
        ),
    )


def _make_client_with_store(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> tuple[FlaskClient, PendingCreateAttemptStore, AgentCreator]:
    """A desktop-client test app whose agent creator carries a pending-create-attempt store."""
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    creator = AgentCreator(
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        pending_create_attempt_store=store,
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        agent_creator=creator,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
    )
    return client, store, creator


def test_form_defaults_prefill_the_form_from_a_known_retry_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A ?retry naming a pending record restores the stored request into the prefill.

    The record names a BYOK cloud account that no longer exists (this test env
    has none configured), so the prefill drops it while still threading the
    stored machine size through.
    """
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = str(CreateAttemptId.generate())
    store.write_record(
        _record(
            create_attempt_id,
            PendingCreateAttemptState.IN_FLIGHT,
            cloud_account="byok-gcp-ghost",
            instance_type="e2-standard-4",
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
    # The ghost account is not offered, so it must not be pre-selected either.
    assert prefill["cloud_account"] == ""


def test_create_attempt_detail_carries_error_and_log_tail_for_a_failed_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A live (in-flight) attempt's detail restates the settings it was submitted with.

    Pointing at a nonexistent local path (the same pattern agent_creator_test.py
    uses) fails fast in the background thread, but the attempt is genuinely
    live -- tracked by get_create_attempt_info -- for the brief window this
    test reads it in, same as the creation page's own polling would.
    """
    client, _store, creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
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
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
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
