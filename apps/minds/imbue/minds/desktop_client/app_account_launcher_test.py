"""Unit tests for the `/ui/ws` ``accounts`` frame.

The home screen's bottom-left account launcher and Manage Accounts both render
from this frame, and both stay put while accounts change underneath them. The
frame is what updates them, so these tests drive the account list directly and
assert what the windows would be told.
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
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_models import UiAccountEntry


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
    """No accounts: the launcher reads "Log in" and its click opens the sign-in modal."""
    app = _make_app(tmp_path, make_fake_imbue_cloud_cli())
    with app.app_context():
        message = _build_ui_accounts_message(_session_store(app))

    assert (message.has_accounts, message.account_email, message.extra_account_count) == (False, "", 0)
    assert message.accounts == ()


def test_accounts_frame_marks_the_only_signed_in_account_default_over_a_departed_default(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-current", email="current@example.com")
    app = _make_app(tmp_path, cli)
    minds_config = get_state(app).minds_config
    assert minds_config is not None
    minds_config.set_default_account_id("user-departed")

    with app.app_context():
        message = _build_ui_accounts_message(_session_store(app))

    assert message.account_email == "current@example.com"
    assert [(entry.user_id, entry.is_default) for entry in message.accounts] == [("user-current", True)]


def test_accounts_frame_names_the_default_account_and_lists_them_all(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-first", email="first@example.com")
    cli.add_account(user_id="user-second", email="second@example.com")
    app = _make_app(tmp_path, cli)
    minds_config = get_state(app).minds_config
    assert minds_config is not None
    minds_config.set_default_account_id("user-second")

    with app.app_context():
        message = _build_ui_accounts_message(_session_store(app))

    assert (message.has_accounts, message.account_email, message.extra_account_count) == (
        True,
        "second@example.com",
        1,
    )
    assert message.accounts == (
        UiAccountEntry(
            user_id="user-first", email="first@example.com", workspace_count=0, is_default=False, is_enabled=True
        ),
        UiAccountEntry(
            user_id="user-second", email="second@example.com", workspace_count=0, is_default=True, is_enabled=True
        ),
    )


def test_accounts_frame_follows_a_sign_out(tmp_path: Path) -> None:
    """After signing the last account out, neither the launcher nor Manage Accounts may still show it.

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

    assert (before.account_email, [entry.email for entry in before.accounts]) == (
        "only@example.com",
        ["only@example.com"],
    )
    assert (after.has_accounts, after.account_email, after.accounts) == (False, "", ())


def test_accounts_frame_follows_a_switch_of_the_default_account(tmp_path: Path) -> None:
    """Switching the default account re-labels the launcher and moves the default mark."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-first", email="first@example.com")
    cli.add_account(user_id="user-second", email="second@example.com")
    app = _make_app(tmp_path, cli)
    minds_config = get_state(app).minds_config
    assert minds_config is not None
    minds_config.set_default_account_id("user-first")

    with app.app_context():
        session_store = _session_store(app)
        before = _build_ui_accounts_message(session_store)
        minds_config.set_default_account_id("user-second")
        after = _build_ui_accounts_message(session_store)

    assert before.account_email == "first@example.com"
    assert after.account_email == "second@example.com"
    assert [entry.is_default for entry in after.accounts] == [False, True]


def test_accounts_frame_is_empty_without_a_session_store(tmp_path: Path) -> None:
    app = _make_app(tmp_path, make_fake_imbue_cloud_cli())
    with app.app_context():
        message = _build_ui_accounts_message(None)

    assert (message.has_accounts, message.accounts) == (False, ())
