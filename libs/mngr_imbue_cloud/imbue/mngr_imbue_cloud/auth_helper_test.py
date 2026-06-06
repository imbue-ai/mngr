"""Tests for the token-refresh glue in auth_helper.

Focus: ``_refresh_locked`` must fail loudly on a malformed refresh response
rather than reuse the old, already-consumed refresh token (which would trip
SuperTokens token-theft detection on the next refresh and revoke the whole
session family).
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.auth_helper import _refresh_locked
from imbue.mngr_imbue_cloud.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore


class _StubRefreshClient(ImbueCloudConnectorClient):
    """Connector client whose refresh endpoint returns a canned response."""

    canned_response: dict[str, Any] = {}

    def auth_refresh_session(self, refresh_token: SecretStr) -> dict[str, Any]:
        return self.canned_response


def _make_session(refresh_token: str | None = "old-refresh") -> AuthSession:
    return AuthSession(
        user_id=SuperTokensUserId("user-abc"),
        email=ImbueCloudAccount("alice@imbue.com"),
        display_name=None,
        access_token=SecretStr("old.acc.tok"),
        refresh_token=SecretStr(refresh_token) if refresh_token else None,
        access_token_expires_at=None,
    )


def _make_client(response: dict[str, Any]) -> _StubRefreshClient:
    return _StubRefreshClient(base_url=AnyUrl("https://example.com"), canned_response=response)


def test_refresh_rotates_access_and_refresh_tokens(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    client = _make_client({"status": "OK", "tokens": {"access_token": "x.e30.y", "refresh_token": "new-refresh"}})
    refreshed = _refresh_locked(store, client, _make_session())
    assert refreshed.access_token.get_secret_value() == "x.e30.y"
    assert refreshed.refresh_token is not None
    assert refreshed.refresh_token.get_secret_value() == "new-refresh"


def test_refresh_keeps_current_token_when_connector_does_not_rotate(tmp_path: Path) -> None:
    """A null refresh_token means SuperTokens did not rotate this cycle; keep the current one."""
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    client = _make_client({"status": "OK", "tokens": {"access_token": "x.e30.y", "refresh_token": None}})
    refreshed = _refresh_locked(store, client, _make_session(refresh_token="old-refresh"))
    assert refreshed.refresh_token is not None
    assert refreshed.refresh_token.get_secret_value() == "old-refresh"


def test_refresh_raises_on_non_string_refresh_token(tmp_path: Path) -> None:
    """A present-but-unusable refresh token must raise, not silently reuse the consumed one."""
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    client = _make_client(
        {"status": "OK", "tokens": {"access_token": "x.e30.y", "refresh_token": {"nested": "wrong"}}}
    )
    with pytest.raises(ImbueCloudAuthError):
        _refresh_locked(store, client, _make_session())


def test_refresh_raises_when_status_not_ok(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    client = _make_client({"status": "ERROR", "message": "bad token"})
    with pytest.raises(ImbueCloudAuthError):
        _refresh_locked(store, client, _make_session())


def test_refresh_raises_when_status_missing(tmp_path: Path) -> None:
    """A missing status is a contract violation, not an implicit success."""
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    client = _make_client({"tokens": {"access_token": "x.e30.y", "refresh_token": "new-refresh"}})
    with pytest.raises(ImbueCloudAuthError):
        _refresh_locked(store, client, _make_session())


def test_refresh_raises_when_no_stored_refresh_token(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    client = _make_client({"status": "OK", "tokens": {"access_token": "x.e30.y", "refresh_token": "x"}})
    with pytest.raises(ImbueCloudAuthError):
        _refresh_locked(store, client, _make_session(refresh_token=None))
