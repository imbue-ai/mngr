import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.workspace_defaults import DEFAULT_WORKSPACE_TEMPLATE_GIT_URL
from imbue.minds.desktop_client.workspace_defaults import FALLBACK_BRANCH
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import LaunchMode


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
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        pending_create_attempt_store=store,
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        agent_creator=creator,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
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


# -- Live-attempt detail: the onboarding walkthrough's context (is_remote,
# -- expected_duration_seconds, onboarding_services) --


def test_create_attempt_detail_carries_the_onboarding_walkthrough_context_for_a_live_attempt(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A live (in-flight) attempt's detail carries what the walkthrough needs.

    Pointing at a nonexistent local path (the same pattern agent_creator_test.py
    uses) fails fast in the background thread, but the attempt is genuinely
    live -- tracked by get_create_attempt_info -- for the brief window this
    test reads it in, same as the create form's own in-flight polling would.
    """
    client, _store, creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = creator.start_create_attempt(
        "file:///nonexistent-repo-for-onboarding-context-test",
        host_name="onboarding-context-test",
        launch_mode=LaunchMode.DOCKER,
    )

    response = client.get(f"/ui/api/create/attempts/{create_attempt_id}")

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["kind"] == "live"
    live = payload["live"]
    # DOCKER is a local launch mode, so the machine step's copy and graphic
    # should be the local (not cloud) variant.
    assert live["is_remote"] is False
    assert live["expected_duration_seconds"] > 0
    # The bundled latchkey services catalog backs the app-cloud icon wheel;
    # every entry carries an inlined (data: URI) icon and a display name.
    assert len(live["onboarding_services"]) > 0
    for service in live["onboarding_services"]:
        assert service["icon"].startswith("data:image/")
        assert service["name"]
