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

from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthSession
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
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
