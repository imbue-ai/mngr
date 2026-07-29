"""Unit tests for the account identity the chrome-events stream carries.

The home screen's bottom-left account launcher is server-rendered, but the page
stays put while accounts change underneath it (sign-out and "Set default" both
happen in an overlay modal on top of it). This payload is what re-labels it, so
these tests drive the account list directly and assert what the launcher would
be told.
"""

from pathlib import Path

from flask import Flask

from imbue.minds.desktop_client.app import _build_account_launcher_payload
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state


def _make_app(tmp_path: Path, cli: FakeImbueCloudCli) -> Flask:
    """A desktop client wired to ``cli``, so the payload helper can be called under its context."""
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


def test_account_launcher_payload_is_empty_when_signed_out(tmp_path: Path) -> None:
    """No accounts: the launcher reads "Log in" and its click opens the sign-in modal."""
    app = _make_app(tmp_path, make_fake_imbue_cloud_cli())
    with app.app_context():
        payload = _build_account_launcher_payload(_session_store(app))

    assert payload == {"has_accounts": False, "account_email": "", "extra_account_count": 0}


def test_account_launcher_payload_names_the_default_account_and_counts_the_rest(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-first", email="first@example.com")
    cli.add_account(user_id="user-second", email="second@example.com")
    app = _make_app(tmp_path, cli)
    minds_config = get_state(app).minds_config
    assert minds_config is not None
    minds_config.set_default_account_id("user-second")

    with app.app_context():
        payload = _build_account_launcher_payload(_session_store(app))

    assert payload == {"has_accounts": True, "account_email": "second@example.com", "extra_account_count": 1}


def test_account_launcher_payload_follows_a_sign_out(tmp_path: Path) -> None:
    """The bug: after signing the last account out the launcher must stop naming it.

    Sign-out drops the plugin's session and invalidates the identity cache; the
    payload is re-derived from that, so it flips to the signed-out shape instead
    of keeping the departed account's label.
    """
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-only", email="only@example.com")
    app = _make_app(tmp_path, cli)
    with app.app_context():
        session_store = _session_store(app)
        before = _build_account_launcher_payload(session_store)

        cli.remove_account("user-only")
        session_store.invalidate_identity_cache()
        after = _build_account_launcher_payload(session_store)

    assert before == {"has_accounts": True, "account_email": "only@example.com", "extra_account_count": 0}
    assert after == {"has_accounts": False, "account_email": "", "extra_account_count": 0}


def test_account_launcher_payload_follows_a_switch_of_the_default_account(tmp_path: Path) -> None:
    """Switching the default account re-labels the launcher to the newly-default one."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-first", email="first@example.com")
    cli.add_account(user_id="user-second", email="second@example.com")
    app = _make_app(tmp_path, cli)
    minds_config = get_state(app).minds_config
    assert minds_config is not None
    minds_config.set_default_account_id("user-first")

    with app.app_context():
        session_store = _session_store(app)
        before = _build_account_launcher_payload(session_store)
        minds_config.set_default_account_id("user-second")
        after = _build_account_launcher_payload(session_store)

    assert before["account_email"] == "first@example.com"
    assert after["account_email"] == "second@example.com"
