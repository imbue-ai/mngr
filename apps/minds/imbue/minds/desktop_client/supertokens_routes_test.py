"""Unit tests for the minds desktop client's supertokens_routes helpers.

The OAuth flow now lives entirely inside ``mngr imbue_cloud auth oauth``;
the desktop server only spawns that subprocess and tracks per-flow status
so the frontend can show "waiting" / "done" without blocking on the
subprocess. These tests cover that small status registry.
"""

import time
from pathlib import Path
from uuid import uuid4

from imbue.minds.desktop_client.supertokens_routes import _OAuthFlowStatus
from imbue.minds.desktop_client.supertokens_routes import _read_oauth_status
from imbue.minds.desktop_client.supertokens_routes import _record_oauth_status
from imbue.minds.desktop_client.supertokens_routes import bounce_latchkey_forward_supervisor
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
