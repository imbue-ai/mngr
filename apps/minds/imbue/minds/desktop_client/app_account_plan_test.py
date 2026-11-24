import json
from pathlib import Path

from flask.testing import FlaskClient

from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudEmailNotVerifiedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudPlanRefusedCliError


def _make_client_with_account(tmp_path: Path, cli: FakeImbueCloudCli) -> FlaskClient:
    cli.add_account(user_id="user-1", email="alice@example.com")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        imbue_cloud_cli=cli,
        session_store=make_session_store_for_test(tmp_path, cli=cli),
    )
    return client


def test_set_plan_answers_an_unverified_email_refusal_as_an_ordinary_failure_with_its_text_as_detail(
    tmp_path: Path,
) -> None:
    """A signed-in account is verified, so the connector's refusal is a backstop: no email is sent, no resend offered."""
    cli = make_fake_imbue_cloud_cli()
    verification_error = ImbueCloudEmailNotVerifiedCliError("account set-plan: requires a verified email")
    verification_error.email = "alice@example.com"
    cli.set_plan_error_to_raise = verification_error
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": "ally"})

    assert response.status_code == 502
    assert json.loads(response.data) == {"detail": "account set-plan: requires a verified email"}


def test_set_plan_answers_other_failures_with_the_command_text_as_detail_only(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.set_plan_error_to_raise = ImbueCloudCliError("account set-plan failed (exit 1); see the desktop client logs")
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": "ally"})

    assert response.status_code == 502
    assert json.loads(response.data) == {"detail": "account set-plan failed (exit 1); see the desktop client logs"}


def test_set_plan_answers_a_refusal_imbue_cloud_stated_with_its_reason_and_code(tmp_path: Path) -> None:
    """The page words a stated refusal itself, so it needs the reason, not a status to retry on."""
    cli = make_fake_imbue_cloud_cli()
    refusal = ImbueCloudPlanRefusedCliError("account set-plan: The 'ally' plan requires partner access")
    refusal.reason = "The 'ally' plan requires partner access"
    cli.set_plan_error_to_raise = refusal
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": "ally"})

    assert response.status_code == 403
    assert json.loads(response.data) == {"code": "plan_refused", "reason": "The 'ally' plan requires partner access"}


def test_set_plan_refuses_a_switch_with_no_plan_in_a_sentence_the_page_shows(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": " "})

    assert response.status_code == 422
    assert json.loads(response.data) == {"error": "Pick a plan to switch to, then select Switch plan."}
    assert cli.set_plan_calls == []


def test_set_plan_tells_a_missing_account_from_a_missing_imbue_cloud(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)
    unconfigured_client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path / "unconfigured", is_authenticated=True
    )

    missing_account = client.post("/accounts/nobody/plan", data={"plan": "free"})
    no_imbue_cloud = unconfigured_client.post("/accounts/user-1/plan", data={"plan": "free"})

    assert missing_account.status_code == 409
    assert "error" not in json.loads(missing_account.data)
    assert "account list" in json.loads(missing_account.data)["detail"]
    assert no_imbue_cloud.status_code == 503
    assert json.loads(no_imbue_cloud.data)["error"] == "Imbue Studio can't do that on this computer."
    assert cli.set_plan_calls == []


def test_the_verification_routes_are_gone(tmp_path: Path) -> None:
    client = _make_client_with_account(tmp_path, make_fake_imbue_cloud_cli())

    resend = client.post("/accounts/user-1/resend-verification")
    send = client.post("/accounts/verification?email=alice@example.com")

    assert resend.status_code in (404, 405)
    assert send.status_code in (404, 405)
