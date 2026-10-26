"""Tests for the /ui/api onboarding routes (the consent answer, onboarding completion, and the sign-in landing)."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from flask.testing import FlaskClient

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_fresh_device_scheduler
from imbue.minds.desktop_client.conftest import push_remote_workspace_record
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path


def test_consent_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.post("/ui/api/onboarding/consent")

    assert response.status_code == 401


def test_consent_without_an_answer_only_marks_the_screen_answered(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    assert minds_config.get_error_reporting_consent_given() is False
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/consent")

    assert response.status_code == 200
    assert minds_config.get_error_reporting_consent_given() is True
    # The answer also (re)writes the latchkey daemon's live consent file.
    consent_path = latchkey_forward_sentry_consent_path(minds_config.data_dir)
    assert json.loads(consent_path.read_text())["report_unexpected_errors"] is True


@pytest.mark.witnesses(
    "home-page.consent-reporting-choice",
    partial="witnesses that the answer becomes the reporting setting; the checkbox starting checked is the SPA's "
    "(ConsentPage.test.ts), outside the Python witnessing surface",
)
@pytest.mark.parametrize("is_reporting_allowed", [True, False])
def test_the_consent_answer_becomes_the_reporting_setting(tmp_path: Path, is_reporting_allowed: bool) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/consent", json={"report_unexpected_errors": is_reporting_allowed})

    assert response.status_code == 200
    assert minds_config.get_error_reporting_consent_given() is True
    assert minds_config.get_report_unexpected_errors() is is_reporting_allowed
    consent_path = latchkey_forward_sentry_consent_path(minds_config.data_dir)
    assert json.loads(consent_path.read_text())["report_unexpected_errors"] is is_reporting_allowed


@pytest.mark.parametrize(
    "body",
    [
        '{"report_unexpected_errors": "no"}',
        '{"report_unexpected_error": false}',
        '{"report_unexpected_errors": fals',
        "[false]",
    ],
    ids=["not-a-boolean", "unknown-field", "invalid-json", "not-an-object"],
)
def test_a_malformed_consent_answer_is_refused(tmp_path: Path, body: str) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/consent", data=body, content_type="application/json")

    assert response.status_code == 400
    assert minds_config.get_error_reporting_consent_given() is False
    assert minds_config.get_report_unexpected_errors() is True


def test_complete_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.post("/ui/api/onboarding/complete")

    assert response.status_code == 401


def test_complete_persists_the_onboarding_flag(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    assert minds_config.get_is_onboarding_complete() is False
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/onboarding/complete")

    assert response.status_code == 200
    # Persisted, not per-run: a fresh config over the same directory reads it back.
    assert MindsConfig(data_dir=tmp_path / "minds-data").get_is_onboarding_complete() is True


def _client_with_scheduler(tmp_path: Path, scheduler: WorkspaceSyncScheduler) -> FlaskClient:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, sync_scheduler=scheduler
    )
    return client


def _landing_body(client: FlaskClient, account_id: str) -> dict[str, object]:
    response = client.get(f"/ui/api/onboarding/signin-landing?account={account_id}")
    assert response.status_code == 200
    return json.loads(response.get_data(as_text=True))


def test_signin_landing_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.get("/ui/api/onboarding/signin-landing?account=abc")

    assert response.status_code == 401


def test_signin_landing_refuses_an_account_id_that_is_not_one(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    assert client.get("/ui/api/onboarding/signin-landing").status_code == 400
    assert client.get("/ui/api/onboarding/signin-landing?account=not%20an%20id").status_code == 400


def test_signin_landing_without_sync_machinery_answers_done_with_no_workspaces(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    assert _landing_body(client, "abc") == {"state": "done", "workspace_count": 0}


def test_signin_landing_is_pending_while_the_first_record_fetch_is_in_flight(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    client = _client_with_scheduler(tmp_path, scheduler)

    assert _landing_body(client, user_id) == {"state": "pending", "workspace_count": 0}


def test_signin_landing_answers_done_with_the_fetched_workspace_count(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    push_remote_workspace_record(tmp_path, cli, user_id)
    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    scheduler.run_one_pass()
    client = _client_with_scheduler(tmp_path, scheduler)

    assert _landing_body(client, user_id) == {"state": "done", "workspace_count": 1}


def test_signin_landing_answers_done_with_zero_for_a_brand_new_account(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    scheduler.run_one_pass()
    client = _client_with_scheduler(tmp_path, scheduler)

    assert _landing_body(client, user_id) == {"state": "done", "workspace_count": 0}


def test_signin_landing_reports_a_failed_first_fetch(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    cli.is_sync_offline = True
    scheduler.run_one_pass_guarded()
    client = _client_with_scheduler(tmp_path, scheduler)

    assert _landing_body(client, user_id) == {"state": "failed", "workspace_count": 0}


def test_signin_landing_answers_from_local_records_when_the_account_already_synced_here(tmp_path: Path) -> None:
    # The account's records were pulled before this sign-in, so nothing tracks
    # a first fetch; the replica itself is the answer.
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    push_remote_workspace_record(tmp_path, cli, user_id)
    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.run_one_pass()
    assert scheduler.list_initial_sync_statuses() == []
    client = _client_with_scheduler(tmp_path, scheduler)

    assert _landing_body(client, user_id) == {"state": "done", "workspace_count": 1}
