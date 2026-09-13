"""The plan-switch route's contextual email-verification flow.

The connector refuses an ally switch for an unverified email with a
structured 403; the desktop client must translate that into the SPA's
"we just sent a link" prompt: auto-send the verification email, answer with
the structured JSON body, and serve the resend button's endpoint.
"""

import json
from pathlib import Path

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudEmailNotVerifiedCliError


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


def test_set_plan_keeps_other_refusals_as_plain_text_errors(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.set_plan_error_to_raise = ImbueCloudCliError("The 'ally' plan requires partner access")
    client = _make_client_with_account(tmp_path, cli)

    response = client.post("/accounts/user-1/plan", data={"plan": "ally"})

    assert response.status_code == 502
    assert b"requires partner access" in response.data
    assert cli.resent_verification_emails == []


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
