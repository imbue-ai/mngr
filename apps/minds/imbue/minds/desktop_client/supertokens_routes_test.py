"""Unit tests for the minds desktop client's supertokens_routes helpers.

The OAuth flow now lives entirely inside ``mngr imbue_cloud auth oauth``;
the desktop server only spawns that subprocess and tracks per-flow status
so the frontend can show "waiting" / "done" without blocking on the
subprocess. These tests cover that small status registry.
"""

import time
from pathlib import Path
from uuid import uuid4

import pytest
from flask.testing import FlaskClient
from pydantic import AnyUrl

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthSession
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudVerificationStatus
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.supertokens_routes import _OAuthFlowStatus
from imbue.minds.desktop_client.supertokens_routes import _read_oauth_status
from imbue.minds.desktop_client.supertokens_routes import _record_oauth_status
from imbue.minds.desktop_client.supertokens_routes import _run_oauth_subprocess
from imbue.minds.desktop_client.supertokens_routes import bounce_latchkey_forward_supervisor
from imbue.minds.primitives import OutputFormat
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor


def test_record_then_read_returns_same_status() -> None:
    status = _OAuthFlowStatus(state="running", deadline=time.monotonic() + 60)
    _record_oauth_status("flow-aaa", status)
    fetched = _read_oauth_status("flow-aaa")
    assert fetched is not None
    assert fetched.state == "running"


def test_read_unknown_flow_returns_none() -> None:
    assert _read_oauth_status("never-recorded") is None


def test_record_overwrites_previous_status_for_same_flow() -> None:
    deadline = time.monotonic() + 60
    _record_oauth_status("flow-bbb", _OAuthFlowStatus(state="running", deadline=deadline))
    _record_oauth_status(
        "flow-bbb",
        _OAuthFlowStatus(
            state="done",
            user_id="user-xyz",
            email="alice@example.com",
            deadline=deadline,
        ),
    )
    fetched = _read_oauth_status("flow-bbb")
    assert fetched is not None
    assert fetched.state == "done"
    assert fetched.email == "alice@example.com"


def test_expired_flows_are_pruned_on_next_read() -> None:
    """A flow whose deadline has passed is dropped on the next access."""
    expired_deadline = time.monotonic() - 1
    _record_oauth_status("flow-ccc", _OAuthFlowStatus(state="done", deadline=expired_deadline))
    # Recording another flow triggers pruning of the expired one.
    _record_oauth_status("flow-ddd", _OAuthFlowStatus(state="running", deadline=time.monotonic() + 60))
    assert _read_oauth_status("flow-ccc") is None
    assert _read_oauth_status("flow-ddd") is not None


def test_bounce_latchkey_forward_supervisor_swallows_latchkey_error(tmp_path: Path) -> None:
    """A failing supervisor.bounce() (LatchkeyError) must be logged, not propagated.

    bounce() falls back to ensure_running() when no live supervisor is found, and
    ensure_running() raises LatchkeyError when the mngr binary cannot be spawned.
    That error must not escape into the request handlers that call this helper.
    """
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(tmp_path / "does-not-exist-mngr"),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    # Sanity: a direct bounce() does raise the uncaught-by-(OSError, RuntimeError) error.
    try:
        supervisor.bounce()
        raised = False
    except LatchkeyError:
        raised = True
    assert raised, "expected bounce() to raise LatchkeyError when the mngr binary is missing"

    # The helper must swallow it (no exception propagates).
    bounce_latchkey_forward_supervisor(supervisor)


class _ExplodingSessionStore(MultiAccountSessionStore):
    """Session store whose identity-cache invalidation always fails, to exercise the mirroring error path."""

    def invalidate_identity_cache(self) -> None:
        raise ImbueCloudCliError("identity cache invalidation exploded (test)")


def test_run_oauth_subprocess_marks_flow_done_without_flask_app_context(tmp_path: Path) -> None:
    """Regression: the OAuth thread runs outside any Flask app context.

    It used to call ``get_state()`` (a ``current_app``-bound proxy) via
    ``_kick_sync_scheduler``, which raised ``RuntimeError: Working outside of
    application context`` and left the flow status stuck on "running" -- the
    frontend then showed "Waiting for you to finish signing in..." forever.
    This calls the thread target directly (no app context, like the real
    thread) and asserts the flow resolves to "done".
    """
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.oauth_session_to_return = ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=email,
        display_name="Test User",
    )
    flow_id = f"flow-{uuid4().hex}"
    _record_oauth_status(flow_id, _OAuthFlowStatus(state="running", deadline=time.monotonic() + 60))

    _run_oauth_subprocess(
        provider_id="google",
        flow_id=flow_id,
        imbue_cloud_cli=cli,
        session_store=make_session_store_for_test(tmp_path, cli),
        sync_scheduler=None,
        minds_config=None,
        output_format=OutputFormat.JSON,
        latchkey_forward_supervisor=None,
        connector_url="https://test--rsc-api.modal.run",
    )

    status = _read_oauth_status(flow_id)
    assert status is not None
    assert status.state == "done"
    assert status.email == email


def test_run_oauth_subprocess_records_error_status_when_mirroring_crashes(tmp_path: Path) -> None:
    """A crash while mirroring the signin must resolve the flow to "error", never leave it "running"."""
    cli = make_fake_imbue_cloud_cli()
    cli.oauth_session_to_return = ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=f"user-{uuid4().hex}@example.com",
        display_name=None,
    )
    exploding_store = _ExplodingSessionStore(
        data_dir=tmp_path,
        cli=cli,
        record_store=make_session_store_for_test(tmp_path, cli).record_store,
    )
    flow_id = f"flow-{uuid4().hex}"
    _record_oauth_status(flow_id, _OAuthFlowStatus(state="running", deadline=time.monotonic() + 60))

    with pytest.raises(ImbueCloudCliError):
        _run_oauth_subprocess(
            provider_id="google",
            flow_id=flow_id,
            imbue_cloud_cli=cli,
            session_store=exploding_store,
            sync_scheduler=None,
            minds_config=None,
            output_format=OutputFormat.JSON,
            latchkey_forward_supervisor=None,
            connector_url="https://test--rsc-api.modal.run",
        )

    status = _read_oauth_status(flow_id)
    assert status is not None
    assert status.state == "error"
    assert status.error is not None
    assert "Signed in as" in status.error


def test_run_oauth_subprocess_marks_finishing_before_mirroring(tmp_path: Path) -> None:
    """The flow is marked "finishing" once the signin is on disk, before the
    (slower) mirror runs -- so the frontend can bring the app forward and show
    "Finishing up..." while mirroring completes, then navigate on "done"."""
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.oauth_session_to_return = ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=email,
        display_name="Test User",
    )
    flow_id = f"flow-{uuid4().hex}"
    seen_states: list[str] = []

    class _StateCapturingSessionStore(MultiAccountSessionStore):
        """Records the flow's state during mirroring (identity-cache invalidation runs mid-mirror)."""

        def invalidate_identity_cache(self) -> None:
            status = _read_oauth_status(flow_id)
            seen_states.append(status.state if status is not None else "MISSING")

    base = make_session_store_for_test(tmp_path, cli)
    store = _StateCapturingSessionStore(data_dir=tmp_path, cli=cli, record_store=base.record_store)
    _record_oauth_status(flow_id, _OAuthFlowStatus(state="running", deadline=time.monotonic() + 60))

    _run_oauth_subprocess(
        provider_id="google",
        flow_id=flow_id,
        imbue_cloud_cli=cli,
        session_store=store,
        sync_scheduler=None,
        minds_config=None,
        output_format=OutputFormat.JSON,
        latchkey_forward_supervisor=None,
        connector_url="https://test--rsc-api.modal.run",
    )

    # The mirror observed the flow already flipped to "finishing"...
    assert seen_states, "expected identity-cache invalidation to run during mirroring"
    assert seen_states[0] == "finishing"
    # ...and the flow resolves to "done" once mirroring completes.
    final = _read_oauth_status(flow_id)
    assert final is not None
    assert final.state == "done"


# -- Verification-flow route tests -------------------------------------------


def _build_auth_test_client(tmp_path: Path, cli: FakeImbueCloudCli) -> tuple[FlaskClient, MindsConfig]:
    """Minimal desktop-client app for exercising the /auth routes."""
    minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        imbue_cloud_cli=cli,
        minds_config=minds_config,
        session_store=make_session_store_for_test(tmp_path / "session-store", cli),
        paths=WorkspacePaths(data_dir=tmp_path / "data"),
        client_env_config=ClientEnvConfig(
            connector_url=FAKE_CONNECTOR_URL,
            litellm_proxy_url=AnyUrl("https://test--llm.modal.run"),
        ),
    )
    return app.test_client(), minds_config


def _pending_session(email: str) -> ImbueCloudAuthSession:
    return ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=email,
        display_name=None,
        needs_email_verification=True,
    )


def test_signup_requiring_verification_defers_local_signin(tmp_path: Path) -> None:
    """An unverified signup reports needsEmailVerification and runs none of the signin bookkeeping."""
    email = f"pending-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.signup_session_to_return = _pending_session(email)
    client, minds_config = _build_auth_test_client(tmp_path, cli)

    resp = client.post("/auth/api/signup", json={"email": email, "password": "hunter2hunter2"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "OK"
    assert body["needsEmailVerification"] is True
    assert body["email"] == email
    # The deferred half: no default account was selected for the pending signup.
    assert minds_config.get_default_account_id() is None


def test_signup_with_verified_email_completes_signin_immediately(tmp_path: Path) -> None:
    """A paid-list (auto-verified) signup skips the verification detour entirely."""
    email = f"paid-{uuid4().hex}@example.com"
    user_id = f"user-{uuid4().hex}"
    cli = make_fake_imbue_cloud_cli()
    cli.signup_session_to_return = ImbueCloudAuthSession(
        user_id=user_id,
        email=email,
        display_name=None,
        needs_email_verification=False,
    )
    client, minds_config = _build_auth_test_client(tmp_path, cli)

    resp = client.post("/auth/api/signup", json={"email": email, "password": "hunter2hunter2"})

    assert resp.status_code == 200
    assert resp.get_json()["needsEmailVerification"] is False
    assert minds_config.get_default_account_id() == user_id


def test_email_verified_poll_completes_deferred_signin(tmp_path: Path) -> None:
    """Once the plugin reports verified, the poll endpoint finishes the signin bookkeeping."""
    email = f"verified-{uuid4().hex}@example.com"
    user_id = f"user-{uuid4().hex}"
    cli = make_fake_imbue_cloud_cli()
    cli.verification_status_by_email[email] = ImbueCloudVerificationStatus(
        verified=True, user_id=user_id, email=email, display_name=None
    )
    client, minds_config = _build_auth_test_client(tmp_path, cli)

    resp = client.get(f"/auth/api/email-verified?email={email}")

    assert resp.status_code == 200
    assert resp.get_json() == {"verified": True}
    assert minds_config.get_default_account_id() == user_id


def test_email_verified_poll_reports_unverified_without_side_effects(tmp_path: Path) -> None:
    email = f"waiting-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.verification_status_by_email[email] = ImbueCloudVerificationStatus(
        verified=False, user_id=f"user-{uuid4().hex}", email=email, display_name=None
    )
    client, minds_config = _build_auth_test_client(tmp_path, cli)

    resp = client.get(f"/auth/api/email-verified?email={email}")

    assert resp.status_code == 200
    assert resp.get_json() == {"verified": False}
    assert minds_config.get_default_account_id() is None


def test_email_verified_poll_requires_email_param(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client, _minds_config = _build_auth_test_client(tmp_path, cli)
    resp = client.get("/auth/api/email-verified")
    assert resp.status_code == 400


def test_email_verified_poll_reports_backend_unavailable(tmp_path: Path) -> None:
    """An email without a pending session (subprocess failure) maps to a 502, not a crash."""
    cli = make_fake_imbue_cloud_cli()
    client, _minds_config = _build_auth_test_client(tmp_path, cli)
    resp = client.get(f"/auth/api/email-verified?email=unknown-{uuid4().hex}@example.com")
    assert resp.status_code == 502
    assert resp.get_json()["verified"] is False


def test_resend_verification_reports_server_cooldown(tmp_path: Path) -> None:
    email = f"resend-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.is_resend_suppressed = True
    client, _minds_config = _build_auth_test_client(tmp_path, cli)

    resp = client.post("/auth/api/resend-verification", json={"email": email})

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "OK", "sent": False}
    assert cli.resent_verification_emails == [email]


def test_resend_verification_requires_email(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client, _minds_config = _build_auth_test_client(tmp_path, cli)
    resp = client.post("/auth/api/resend-verification", json={})
    assert resp.status_code == 400
    assert cli.resent_verification_emails == []


def test_check_email_page_renders_pinned_email(tmp_path: Path) -> None:
    email = f"page-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    client, _minds_config = _build_auth_test_client(tmp_path, cli)
    resp = client.get(f"/auth/check-email?email={email}")
    assert resp.status_code == 200
    assert email in resp.get_data(as_text=True)


def test_check_email_page_without_email_offers_way_back(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    client, _minds_config = _build_auth_test_client(tmp_path, cli)
    resp = client.get("/auth/check-email")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "your email" in page
    assert "/auth/signup" in page


def test_run_oauth_subprocess_refuses_unverified_oauth_email(tmp_path: Path) -> None:
    """An OAuth result with an unverified email fails the flow instead of activating the account."""
    email = f"oauth-unverified-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.oauth_session_to_return = ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=email,
        display_name=None,
        needs_email_verification=True,
    )
    flow_id = f"flow-{uuid4().hex}"
    _record_oauth_status(flow_id, _OAuthFlowStatus(state="running", deadline=time.monotonic() + 60))

    _run_oauth_subprocess(
        provider_id="google",
        flow_id=flow_id,
        imbue_cloud_cli=cli,
        session_store=make_session_store_for_test(tmp_path, cli),
        sync_scheduler=None,
        minds_config=None,
        output_format=OutputFormat.JSON,
        latchkey_forward_supervisor=None,
        connector_url=str(FAKE_CONNECTOR_URL),
    )

    status = _read_oauth_status(flow_id)
    assert status is not None
    assert status.state == "error"
    assert status.error is not None
    assert "not verified" in status.error
