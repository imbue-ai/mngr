"""Unit tests for ``AuthBackendClient`` response parsing and error handling.

These tests exercise the client against a connection-refused endpoint; real
end-to-end flows are covered by ``test_supertokens_auth_e2e.py`` which talks
to a deployed ``cloudflare_forwarding`` server.
"""

import httpx
import pytest
from pydantic import AnyUrl

from imbue.minds.desktop_client.auth_backend_client import AuthBackendClient
from imbue.minds.desktop_client.auth_backend_client import AuthResult
from imbue.minds.desktop_client.auth_backend_client import SessionTokens


def _make_client() -> AuthBackendClient:
    return AuthBackendClient(base_url=AnyUrl("http://127.0.0.1:1"), timeout_seconds=0.5)


def test_signin_raises_on_connection_error() -> None:
    """signin() surfaces httpx connection errors rather than swallowing them."""
    client = _make_client()
    with pytest.raises(httpx.HTTPError):
        client.signin(email="a@b.com", password="password123")


def test_refresh_session_raises_on_connection_error() -> None:
    """refresh_session() surfaces httpx connection errors to the caller.

    ``MultiAccountSessionStore._try_refresh`` wraps this call in its own
    ``try/except httpx.HTTPError`` so the error is handled one layer up.
    """
    client = _make_client()
    with pytest.raises(httpx.HTTPError):
        client.refresh_session(refresh_token="r")


def test_is_email_verified_raises_on_connection_error() -> None:
    """is_email_verified() surfaces httpx connection errors to the caller."""
    client = _make_client()
    with pytest.raises(httpx.HTTPError):
        client.is_email_verified(user_id="u", email="e@f.com")


def test_auth_result_parses_minimal_response() -> None:
    """AuthResult handles the minimal ``{"status": "..."}`` shape."""
    result = AuthResult.model_validate({"status": "WRONG_CREDENTIALS", "message": "nope"})
    assert result.status == "WRONG_CREDENTIALS"
    assert result.message == "nope"
    assert result.user is None
    assert result.tokens is None
    assert result.needs_email_verification is False


def test_auth_result_parses_success_with_tokens_and_user() -> None:
    """AuthResult hydrates nested ``user`` and ``tokens`` models."""
    result = AuthResult.model_validate(
        {
            "status": "OK",
            "user": {"user_id": "u1", "email": "x@y.com", "display_name": "X"},
            "tokens": {"access_token": "a", "refresh_token": "r"},
            "needs_email_verification": True,
        }
    )
    assert result.status == "OK"
    assert result.user is not None and result.user.user_id == "u1"
    assert result.user.display_name == "X"
    assert result.tokens is not None and result.tokens.access_token == "a"
    assert result.tokens.refresh_token == "r"
    assert result.needs_email_verification is True


def test_session_tokens_allows_null_refresh_token() -> None:
    """SessionTokens accepts a null refresh_token."""
    tokens = SessionTokens.model_validate({"access_token": "a", "refresh_token": None})
    assert tokens.refresh_token is None


def test_revoke_all_sessions_returns_false_on_connection_error() -> None:
    """revoke_all_sessions swallows connection errors and returns False, so a
    network failure on sign-out does not block the local session removal."""
    client = _make_client()
    assert client.revoke_all_sessions(access_token="fake-access-token") is False
