"""Unit tests for the minds desktop client's supertokens_routes helpers.

Sign-in lives entirely on the connector's hosted browser page, driven by
``mngr imbue_cloud auth login``; the desktop server only spawns that
subprocess and tracks per-flow status so the frontend can render the
waiting/copy-link modal without blocking on it. These tests cover the status
registry, the subprocess wrapper's state transitions, and the small JSON
surface the Mithril UI polls.
"""

import time
from pathlib import Path
from uuid import uuid4

import pytest
from flask.testing import FlaskClient
from pydantic import AnyUrl

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
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
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.supertokens_routes import _WebLoginFlowStatus
from imbue.minds.desktop_client.supertokens_routes import _read_web_login_status
from imbue.minds.desktop_client.supertokens_routes import _record_web_login_status
from imbue.minds.desktop_client.supertokens_routes import _run_web_login_subprocess
from imbue.minds.desktop_client.supertokens_routes import bounce_latchkey_forward_supervisor
from imbue.minds.primitives import OutputFormat
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor


def test_record_then_read_returns_same_status() -> None:
    status = _WebLoginFlowStatus(state="running", deadline=time.monotonic() + 60)
    _record_web_login_status("flow-aaa", status)
    fetched = _read_web_login_status("flow-aaa")
    assert fetched is not None
    assert fetched.state == "running"


def test_read_unknown_flow_returns_none() -> None:
    assert _read_web_login_status("never-recorded") is None


def test_record_overwrites_previous_status_for_same_flow() -> None:
    flow_id = f"flow-{uuid4().hex}"
    _record_web_login_status(flow_id, _WebLoginFlowStatus(state="running", deadline=time.monotonic() + 60))
    _record_web_login_status(
        flow_id,
        _WebLoginFlowStatus(state="done", email="done@example.com", deadline=time.monotonic() + 60),
    )
    fetched = _read_web_login_status(flow_id)
    assert fetched is not None
    assert fetched.state == "done"
    assert fetched.email == "done@example.com"


def test_expired_flows_are_pruned_on_next_read() -> None:
    flow_id = f"flow-{uuid4().hex}"
    _record_web_login_status(flow_id, _WebLoginFlowStatus(state="done", deadline=time.monotonic() - 1))
    assert _read_web_login_status(flow_id) is None


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


def test_run_web_login_subprocess_marks_flow_done_without_flask_app_context(tmp_path: Path) -> None:
    """The login thread runs outside any Flask app context and must resolve the flow to "done".

    (The OAuth-era regression this guards: calling ``get_state()`` from the
    thread raised ``RuntimeError: Working outside of application context`` and
    left the flow stuck on "running" -- the frontend then showed "Waiting..."
    forever.)
    """
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.login_session_to_return = ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=email,
        display_name="Test User",
    )
    flow_id = f"flow-{uuid4().hex}"
    url_file = tmp_path / "login-url.txt"
    _record_web_login_status(
        flow_id, _WebLoginFlowStatus(state="running", login_url_file=str(url_file), deadline=time.monotonic() + 60)
    )

    _run_web_login_subprocess(
        flow_id=flow_id,
        url_file=url_file,
        imbue_cloud_cli=cli,
        session_store=make_session_store_for_test(tmp_path, cli),
        sync_scheduler=None,
        minds_config=None,
        output_format=OutputFormat.JSON,
        latchkey_forward_supervisor=None,
        connector_url=str(FAKE_CONNECTOR_URL),
    )

    status = _read_web_login_status(flow_id)
    assert status is not None
    assert status.state == "done"
    assert status.email == email
    # The sign-in URL the plugin wrote is carried on the resolved status, and
    # the temp file itself is cleaned up once the subprocess exits.
    assert status.login_url == cli.login_url_to_write
    assert not url_file.exists()


def test_run_web_login_subprocess_records_error_status_when_mirroring_crashes(tmp_path: Path) -> None:
    """A crash while mirroring the signin must resolve the flow to "error", never leave it "running"."""
    cli = make_fake_imbue_cloud_cli()
    cli.login_session_to_return = ImbueCloudAuthSession(
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
    _record_web_login_status(flow_id, _WebLoginFlowStatus(state="running", deadline=time.monotonic() + 60))

    with pytest.raises(ImbueCloudCliError):
        _run_web_login_subprocess(
            flow_id=flow_id,
            url_file=tmp_path / "login-url.txt",
            imbue_cloud_cli=cli,
            session_store=exploding_store,
            sync_scheduler=None,
            minds_config=None,
            output_format=OutputFormat.JSON,
            latchkey_forward_supervisor=None,
            connector_url=str(FAKE_CONNECTOR_URL),
        )

    status = _read_web_login_status(flow_id)
    assert status is not None
    assert status.state == "error"
    assert status.error is not None
    assert "Signed in as" in status.error


def test_run_web_login_subprocess_marks_finishing_before_mirroring(tmp_path: Path) -> None:
    """The flow is marked "finishing" once the signin is on disk, before the
    (slower) mirror runs -- so the frontend can bring the app forward and show
    "Finishing up..." while mirroring completes, then refresh on "done"."""
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.login_session_to_return = ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=email,
        display_name="Test User",
    )
    flow_id = f"flow-{uuid4().hex}"
    seen_states: list[str] = []

    class _StateCapturingSessionStore(MultiAccountSessionStore):
        """Records the flow's state during mirroring (identity-cache invalidation runs mid-mirror)."""

        def invalidate_identity_cache(self) -> None:
            status = _read_web_login_status(flow_id)
            seen_states.append(status.state if status is not None else "MISSING")

    base = make_session_store_for_test(tmp_path, cli)
    store = _StateCapturingSessionStore(data_dir=tmp_path, cli=cli, record_store=base.record_store)
    _record_web_login_status(flow_id, _WebLoginFlowStatus(state="running", deadline=time.monotonic() + 60))

    _run_web_login_subprocess(
        flow_id=flow_id,
        url_file=tmp_path / "login-url.txt",
        imbue_cloud_cli=cli,
        session_store=store,
        sync_scheduler=None,
        minds_config=None,
        output_format=OutputFormat.JSON,
        latchkey_forward_supervisor=None,
        connector_url=str(FAKE_CONNECTOR_URL),
    )

    # The mirror observed the flow already flipped to "finishing"...
    assert seen_states, "expected identity-cache invalidation to run during mirroring"
    assert seen_states[0] == "finishing"
    # ...and the flow resolves to "done" once mirroring completes.
    final = _read_web_login_status(flow_id)
    assert final is not None
    assert final.state == "done"


def test_run_web_login_subprocess_records_error_when_the_plugin_fails(tmp_path: Path) -> None:
    """A failed plugin subprocess resolves the flow to "error" with user-facing copy."""
    cli = make_fake_imbue_cloud_cli()
    # No login_session_to_return configured -> the fake raises ImbueCloudCliError.
    flow_id = f"flow-{uuid4().hex}"
    _record_web_login_status(flow_id, _WebLoginFlowStatus(state="running", deadline=time.monotonic() + 60))

    _run_web_login_subprocess(
        flow_id=flow_id,
        url_file=tmp_path / "login-url.txt",
        imbue_cloud_cli=cli,
        session_store=make_session_store_for_test(tmp_path, cli),
        sync_scheduler=None,
        minds_config=None,
        output_format=OutputFormat.JSON,
        latchkey_forward_supervisor=None,
        connector_url=str(FAKE_CONNECTOR_URL),
    )

    status = _read_web_login_status(flow_id)
    assert status is not None
    assert status.state == "error"
    assert status.error is not None
    # The raw CLI failure string is unusable UI copy; the generic actionable
    # message is what the modal shows.
    assert "sign-in service" in status.error


# -- Route tests --------------------------------------------------------------


def _build_auth_test_client(
    tmp_path: Path,
    cli: FakeImbueCloudCli,
    root_cg: ConcurrencyGroup | None = None,
) -> tuple[FlaskClient, MindsConfig]:
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
        root_concurrency_group=root_cg,
    )
    return app.test_client(), minds_config


def test_web_login_start_and_status_round_trip(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    """POST start returns a pollable flow that resolves to "done" with the account identity."""
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.login_session_to_return = ImbueCloudAuthSession(
        user_id=f"user-{uuid4().hex}",
        email=email,
        display_name=None,
    )
    client, minds_config = _build_auth_test_client(tmp_path, cli, root_concurrency_group)

    start = client.post("/auth/api/web-login/start")
    assert start.status_code == 200
    flow_id = start.get_json()["flow_id"]

    # The background thread completes quickly with the in-memory fake; poll
    # (bounded) rather than sleeping a fixed interval.
    deadline = time.monotonic() + 10.0
    body: dict[str, object] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/auth/api/web-login/status/{flow_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        if body["state"] in ("done", "error"):
            break
    assert body["state"] == "done"
    assert body["email"] == email
    assert body["login_url"] == cli.login_url_to_write
    # The signin bookkeeping ran: the first account became the default.
    assert minds_config.get_default_account_id() is not None


def test_web_login_status_404s_for_unknown_flow(tmp_path: Path) -> None:
    client, _minds_config = _build_auth_test_client(tmp_path, make_fake_imbue_cloud_cli())
    resp = client.get(f"/auth/api/web-login/status/flow-{uuid4().hex}")
    assert resp.status_code == 404


def test_legacy_auth_page_urls_redirect_into_the_spa(tmp_path: Path) -> None:
    """/auth/login and /auth/signup (retired pages) bounce to the SPA's web-login trigger."""
    client, _minds_config = _build_auth_test_client(tmp_path, make_fake_imbue_cloud_cli())

    login = client.get("/auth/login?message=Sign+in+to+share")
    assert login.status_code == 302
    assert login.headers["Location"] == "/?web-login=1&web-login-message=Sign+in+to+share"

    signup = client.get("/auth/signup")
    assert signup.status_code == 302
    assert signup.headers["Location"] == "/?web-login=1"


def test_reset_password_redirects_to_the_connector(tmp_path: Path) -> None:
    client, _minds_config = _build_auth_test_client(tmp_path, make_fake_imbue_cloud_cli())
    resp = client.get("/auth/reset-password?token=tok-123")
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"{str(FAKE_CONNECTOR_URL).rstrip('/')}/auth/reset-password?token=tok-123"
