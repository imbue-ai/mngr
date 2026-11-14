"""The plan-switch route's contextual email-verification flow.

The connector refuses an ally switch for an unverified email with a
structured 403; the desktop client must translate that into the SPA's
"we just sent a link" prompt: auto-send the verification email, answer with
the structured JSON body, and serve the resend button's endpoint.
"""

import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import record_web_login_flow_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudEmailNotVerifiedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudPlanRefusedCliError


def _make_client_with_account(tmp_path: Path, cli):
    cli.add_account(user_id="user-1", email="alice@example.com")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        imbue_cloud_cli=cli,
        session_store=make_session_store_for_test(tmp_path, cli=cli),
    )
    return client


def test_set_plan_email_not_verified_auto_sends_and_returns_the_structured_403(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    verification_error = ImbueCloudEmailNotVerifiedCliError("account set-plan: requires a verified email")
    verification_error.email = "alice@example.com"
    cli.set_plan_error_to_raise = verification_error
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": "ally"})

    assert response.status_code == 403
    body = json.loads(response.data)
    assert body == {"code": "email_not_verified", "email": "alice@example.com", "sent": True}
    # The refusal itself triggered the contextual send.
    assert cli.resent_verification_emails == ["alice@example.com"]


def test_set_plan_reports_sent_false_when_the_send_is_suppressed(tmp_path: Path) -> None:
    """A cooldown-suppressed (or failed) send flows through as sent=false, not an error."""
    cli = make_fake_imbue_cloud_cli()
    verification_error = ImbueCloudEmailNotVerifiedCliError("account set-plan: requires a verified email")
    verification_error.email = "alice@example.com"
    cli.set_plan_error_to_raise = verification_error
    cli.is_resend_suppressed = True
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": "ally"})

    assert response.status_code == 403
    assert json.loads(response.data)["sent"] is False


def test_set_plan_answers_other_failures_with_the_command_text_as_detail_only(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.set_plan_error_to_raise = ImbueCloudCliError("account set-plan failed (exit 1); see the desktop client logs")
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": "ally"})

    assert response.status_code == 502
    assert json.loads(response.data) == {"detail": "account set-plan failed (exit 1); see the desktop client logs"}
    assert cli.resent_verification_emails == []


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


def test_resend_verification_route_sends_for_the_named_account(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/resend-verification")

    assert response.status_code == 200
    assert json.loads(response.data) == {"sent": True, "email": "alice@example.com"}
    assert cli.resent_verification_emails == ["alice@example.com"]


def test_resend_verification_route_409s_for_an_unknown_account(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/nobody/resend-verification")

    assert response.status_code == 409
    assert cli.resent_verification_emails == []


def test_verification_check_reports_the_connector_verdict_for_a_signed_in_email(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)

    unverified = client.get("/accounts/verification?email=alice@example.com")
    cli.verified_emails.add("alice@example.com")
    verified = client.get("/accounts/verification?email=Alice@Example.com")

    assert json.loads(unverified.data) == {"verified": False, "email": "alice@example.com"}
    assert json.loads(verified.data) == {"verified": True, "email": "alice@example.com"}
    assert cli.is_verified_checks == ["alice@example.com", "alice@example.com"]


@pytest.mark.witnesses(
    "start-flow.verified-at-sign-in", partial="asserts the backend's verification answer, not the start page's create"
)
def test_verification_check_answers_from_a_sign_in_that_reported_the_email_verified(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)
    record_web_login_flow_for_test("alice@example.com", is_email_verified=True)

    response = client.get("/accounts/verification?email=alice@example.com")

    assert json.loads(response.data) == {"verified": True, "email": "alice@example.com"}
    assert cli.is_verified_checks == []


@pytest.mark.parametrize("is_email_verified", [False, None])
def test_verification_check_asks_the_connector_when_the_sign_in_did_not_report_it_verified(
    tmp_path: Path, is_email_verified: bool | None
) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)
    record_web_login_flow_for_test("alice@example.com", is_email_verified=is_email_verified)
    cli.verified_emails.add("alice@example.com")

    response = client.get("/accounts/verification?email=alice@example.com")

    assert json.loads(response.data) == {"verified": True, "email": "alice@example.com"}
    assert cli.is_verified_checks == ["alice@example.com"]


def test_verification_check_ignores_a_verified_sign_in_whose_account_is_not_signed_in(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)
    record_web_login_flow_for_test("bob@example.com", is_email_verified=True)

    response = client.get("/accounts/verification?email=bob@example.com")

    assert response.status_code == 409


def test_verification_check_409s_for_an_email_this_install_has_not_signed_in(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)

    response = client.get("/accounts/verification?email=stranger@example.com")

    assert response.status_code == 409
    assert cli.is_verified_checks == []


def test_verification_check_502s_when_the_cli_fails(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.is_verified_error_to_raise = ImbueCloudCliError("connector unreachable")
    client = _make_client_with_account(tmp_path, cli)

    response = client.get("/accounts/verification?email=alice@example.com")

    assert response.status_code == 502


def test_verification_resend_sends_for_the_signed_in_email(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/verification?email=alice@example.com")

    assert json.loads(response.data) == {"sent": True, "email": "alice@example.com"}
    assert cli.resent_verification_emails == ["alice@example.com"]
