"""Unit tests for the `/ui/ws` ``accounts`` frame.

The titlebar's account switcher and Manage Accounts both render from this
frame, and both stay put while accounts change underneath them. The frame is
what updates them, so these tests drive the account list directly and assert
what the windows would be told.
"""

from pathlib import Path

from flask import Flask

from imbue.minds.desktop_client.app import _build_ui_accounts_message
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_models import UiAccountEntry
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord


def _make_app(tmp_path: Path, cli: FakeImbueCloudCli) -> Flask:
    """A desktop client wired to ``cli``, so the frame builder can be called under its context."""
    return create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        imbue_cloud_cli=cli,
        session_store=make_session_store_for_test(tmp_path, cli=cli),
        minds_config=MindsConfig(data_dir=tmp_path),
    )


def _session_store(app: Flask) -> MultiAccountSessionStore:
    session_store = get_state(app).session_store
    assert session_store is not None
    return session_store


def test_accounts_frame_is_empty_when_signed_out(tmp_path: Path) -> None:
    """No accounts: the switcher lists only the device, and the launcher reads "Log in"."""
    app = _make_app(tmp_path, make_fake_imbue_cloud_cli())
    with app.app_context():
        message = _build_ui_accounts_message(_session_store(app))

    assert (message.has_accounts, message.accounts, message.signed_out_accounts) == (False, (), ())


def test_accounts_frame_lists_every_signed_in_account_with_its_display_name(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-first", email="first@example.com", display_name="First Person")
    cli.add_account(user_id="user-second", email="second@example.com")
    app = _make_app(tmp_path, cli)

    with app.app_context():
        message = _build_ui_accounts_message(_session_store(app))

    assert message.has_accounts is True
    # The window's account is stamped per connection by the publisher; the derive carries the device.
    assert message.account_id == LOCAL_PROFILE_USER_ID
    assert message.accounts == (
        UiAccountEntry(
            user_id="user-first", email="first@example.com", display_name="First Person", workspace_count=0
        ),
        UiAccountEntry(user_id="user-second", email="second@example.com", workspace_count=0),
    )


def test_accounts_frame_lists_a_signed_out_account_whose_workspaces_are_still_here(tmp_path: Path) -> None:
    """A signed-out account with active records on this install is offered to switch to, with its workspace count."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-here", email="here@example.com")
    app = _make_app(tmp_path, cli)
    with app.app_context():
        session_store = _session_store(app)
        record_store = session_store.record_store
        assert record_store is not None
        record_store.upsert_local_record(
            "user-gone",
            "gone@example.com",
            ReplicaRecord(host_id="host-1", agent_id="agent-1", display_name="left behind"),
        )
        message = _build_ui_accounts_message(session_store)

    assert [entry.user_id for entry in message.accounts] == ["user-here"]
    assert message.signed_out_accounts == (
        UiAccountEntry(user_id="user-gone", email="gone@example.com", workspace_count=1),
    )


def test_accounts_frame_follows_a_sign_out(tmp_path: Path) -> None:
    """After signing the last account out, neither the switcher nor Manage Accounts may still show it.

    Sign-out drops the plugin's session and invalidates the identity cache; the
    frame is re-derived from that, so it flips to the signed-out shape.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-only", email="only@example.com")
    app = _make_app(tmp_path, cli)
    with app.app_context():
        session_store = _session_store(app)
        before = _build_ui_accounts_message(session_store)

        cli.remove_account("user-only")
        session_store.invalidate_identity_cache()
        after = _build_ui_accounts_message(session_store)

    assert [entry.email for entry in before.accounts] == ["only@example.com"]
    assert (after.has_accounts, after.accounts) == (False, ())


def test_accounts_frame_is_empty_without_a_session_store(tmp_path: Path) -> None:
    app = _make_app(tmp_path, make_fake_imbue_cloud_cli())
    with app.app_context():
        message = _build_ui_accounts_message(None)

    assert (message.has_accounts, message.accounts) == (False, ())
