"""Integration tests that drive the FastAPI app end-to-end through ``TestClient``.

These exercise routing, auth, request validation, and the ctx/pool/supertokens
layers together against in-memory fakes. Pure-function and single-layer unit
tests live alongside the code in ``app_test.py``.
"""

from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.app import AdminAuth
from imbue.remote_service_connector.app import _MAX_BUCKETS_PER_ACCOUNT
from imbue.remote_service_connector.app import derive_s3_secret_access_key
from imbue.remote_service_connector.app import web_app
from imbue.remote_service_connector.testing import FakeCloudflareOps
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import InMemoryKeyStore
from imbue.remote_service_connector.testing import make_fake_forwarding_ctx
from imbue.remote_service_connector.testing import make_fake_key_store
from imbue.remote_service_connector.testing import make_fake_pool_backend
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.testing import make_fake_tunnel_token

_ADMIN_STUB_TOKEN = "admin-stub-jwt"
_ADMIN_STUB_USERNAME = "testuser"
_ADMIN_STUB_EMAIL = "testuser@example.com"
_PAID_ADMIN_KEY_TEST_VALUE = "paid-admin-key-secret-9f3a2b"


def _admin_headers() -> dict[str, str]:
    """Return a Bearer header for a fake SuperTokens admin session.

    Paired with ``_make_test_client`` which stubs ``_authenticate_supertokens``
    to recognise ``_ADMIN_STUB_TOKEN`` and return a canned ``AdminAuth``.
    """
    return {"Authorization": f"Bearer {_ADMIN_STUB_TOKEN}"}


def _agent_headers(tunnel_id: str) -> dict[str, str]:
    token = make_fake_tunnel_token(tunnel_id)
    return {"Authorization": f"Bearer {token}"}


def _make_test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient with the FastAPI app, injecting a fake context.

    Sets up the SuperTokens Bearer auth path so tests calling admin endpoints
    can authenticate with ``_admin_headers()`` without needing a real JWT.
    Installs an in-memory paid-list backend seeded with the stub admin email
    so paid-feature endpoints (``/hosts/*``, ``/keys/*``, ``/buckets/*``)
    authorize out of the box; gate tests use ``_make_pool_test_client`` to
    get the backend and flip entries. The paid-status cache is disabled
    (``MINDS_PAID_LIST_CACHE_TTL_SECONDS=0``) so the module-level cache never
    bleeds between tests.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    fake_ctx = make_fake_forwarding_ctx()
    monkeypatch.setattr(app_mod, "get_ctx", lambda: fake_ctx)

    def _stub_supertokens(token: str) -> AdminAuth:
        if token != _ADMIN_STUB_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        return AdminAuth(username=_ADMIN_STUB_USERNAME, email=_ADMIN_STUB_EMAIL)

    monkeypatch.setattr(app_mod, "_authenticate_supertokens", _stub_supertokens)
    backend = make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return TestClient(web_app)


def test_route_create_tunnel_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["tunnel_name"] == "testuser--agent1"
    assert data["token"] is not None


def test_route_create_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post("/tunnels", json={"agent_id": "agent2"}, headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_list_tunnels_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_add_service_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["service_name"] == "web"


def test_route_add_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 200


def test_route_add_service_agent_wrong_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post("/tunnels", json={"agent_id": "agent2"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent2/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_remove_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.delete("/tunnels/testuser--agent1/services/web", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200


def test_route_delete_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_set_tunnel_auth_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200


def test_route_get_tunnel_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()["rules"]) == 1


def test_route_set_tunnel_auth_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": []},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_no_auth_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels")
    assert resp.status_code == 401


def test_route_rejects_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """After removing USER_CREDENTIALS, Basic Auth is no longer a supported scheme."""
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels", headers={"Authorization": "Basic dGVzdDp0ZXN0"})
    assert resp.status_code == 401


def test_route_malformed_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels/foo--bar/services", headers={"Authorization": "Bearer not-valid-base64!!!"})
    assert resp.status_code == 401


def test_route_create_tunnel_too_long_username_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a tunnel whose authenticated username is too long returns 400, not 500."""
    long_name = "a_very_long_username_exceeds_max"
    client = _make_test_client(monkeypatch)
    # Override the stub to return an AdminAuth with an overly-long username,
    # simulating a SuperTokens session whose user_id_prefix is longer than the
    # tunnel-naming limit.
    monkeypatch.setattr(
        app_mod,
        "_authenticate_supertokens",
        lambda _token: AdminAuth(username=long_name),
    )
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 400


# -- Auth route tests --
#
# These are smoke tests that verify the auth routes are wired up and reject
# calls when SuperTokens is not configured. Exercising the success paths
# requires a real SuperTokens core and is covered by release-marked E2E tests.


def test_auth_signup_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signup without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_signin_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signin without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_session_refresh_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/refresh without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "r"})
    assert resp.status_code == 503


def test_auth_session_revoke_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke", headers={"Authorization": "Bearer any-token"})
    assert resp.status_code == 503


def test_auth_session_revoke_requires_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without a Bearer access token returns 401.

    This guards against an anonymous caller terminating arbitrary users'
    sessions just by knowing (or guessing) their user_id.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke")
    assert resp.status_code == 401


def test_auth_verify_email_missing_token_shows_failed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify-email endpoint renders an HTML failure page when the token is missing."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email")
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_reset_password_page_renders_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset-password page renders an HTML form embedding the token."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/reset-password", params={"token": "tok-xyz"})
    assert resp.status_code == 200
    assert "tok-xyz" in resp.text
    assert "Reset password" in resp.text


# -- /auth/* happy-path tests (powered by FakeSuperTokensBackend) --


def _install_fake_supertokens(monkeypatch: pytest.MonkeyPatch) -> FakeSuperTokensBackend:
    """Wire the FakeSuperTokensBackend into the app module and return it."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    backend = make_fake_supertokens_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return backend


def test_auth_signup_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup creates an account, issues a session, and sends a verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "new@example.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "new@example.com"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == 1
    assert "new@example.com" in backend.accounts_by_email


def test_auth_signup_field_error_on_empty_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup returns FIELD_ERROR for empty email or password."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "  ", "password": "x"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "FIELD_ERROR"


def test_auth_signup_duplicate_email_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing up with an email that already exists returns EMAIL_ALREADY_EXISTS."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "EMAIL_ALREADY_EXISTS"
    assert len(backend.accounts_by_email) == 1


def test_auth_signup_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signup is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_up", SuperTokensGeneralError("core down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ERROR",
        "message": "Auth backend unavailable",
        "user": None,
        "tokens": None,
        "needs_email_verification": False,
    }


def test_auth_signin_happy_path_with_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin against a verified account returns OK and skips resending verification."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    initial_verify_count = len(backend.sent_verification_emails)
    account = backend.accounts_by_email["a@b.com"]
    backend.mark_email_verified(account.user_id)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["needs_email_verification"] is False
    assert len(backend.sent_verification_emails) == initial_verify_count


def test_auth_signin_wrong_password_returns_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin with an incorrect password returns WRONG_CREDENTIALS without issuing a session."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "wrong"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "WRONG_CREDENTIALS"
    assert body["tokens"] is None


def test_auth_signin_unverified_email_triggers_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing in to an unverified account sends another verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "unv@example.com", "password": "password123"})
    before = len(backend.sent_verification_emails)
    resp = client.post("/auth/signin", json={"email": "unv@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_signin_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signin is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_in", SuperTokensSessionError("session store down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_refresh_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh rotates tokens and invalidates the old refresh token."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "r@e.com", "password": "password123"}).json()
    initial_refresh = signup["tokens"]["refresh_token"]
    resp = client.post("/auth/session/refresh", json={"refresh_token": initial_refresh})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["tokens"]["refresh_token"] != initial_refresh
    assert initial_refresh not in backend.sessions_by_refresh_token


def test_auth_session_refresh_rejects_unknown_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh returns status=ERROR for an unknown refresh token."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_revoke_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/revoke tears down every session for the authenticated user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "rev@e.com", "password": "password123"}).json()
    access = signup["tokens"]["access_token"]
    assert len(backend.sessions_by_access_token) == 1
    resp = client.post(
        "/auth/session/revoke",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert resp.json()["revoked_count"] == 1
    assert len(backend.sessions_by_access_token) == 0


def test_auth_send_verification_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/send-verification resends a verification email for a known user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "v@e.com", "password": "password123"}).json()
    user_id = signup["user"]["user_id"]
    before = len(backend.sent_verification_emails)
    resp = client.post(
        "/auth/email/send-verification",
        json={"user_id": user_id, "email": "v@e.com"},
    )
    assert resp.status_code == 200
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_send_verification_email_unknown_user_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending verification email for a user that doesn't exist returns 404."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/email/send-verification",
        json={"user_id": "does-not-exist", "email": "a@b.com"},
    )
    assert resp.status_code == 404


def test_auth_is_email_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/is-verified reflects the underlying account state."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "iv@e.com", "password": "password123"}).json()
    user_id = signup["user"]["user_id"]
    resp = client.post("/auth/email/is-verified", json={"user_id": user_id, "email": "iv@e.com"})
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}
    backend.mark_email_verified(user_id)
    resp = client.post("/auth/email/is-verified", json={"user_id": user_id, "email": "iv@e.com"})
    assert resp.json() == {"verified": True}


def test_auth_is_email_verified_unknown_user_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/is-verified returns verified=False for a user that doesn't exist."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/email/is-verified", json={"user_id": "nope", "email": "a@b.com"})
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}


def test_auth_verify_email_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verify-email page consumes a valid token and marks the account verified."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "ve@e.com", "password": "password123"})
    token = next(iter(backend.verification_tokens.keys()))
    resp = client.get("/auth/verify-email", params={"token": token})
    assert resp.status_code == 200
    assert "Email verified" in resp.text
    user_id = backend.accounts_by_email["ve@e.com"].user_id
    assert backend.accounts_by_id[user_id].is_verified is True


def test_auth_verify_email_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting an invalid verification token renders the failure page."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email", params={"token": "bogus"})
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_forgot_password_sends_reset_email_for_known_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/forgot enqueues a reset email when the account exists."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "fp@e.com", "password": "password123"})
    resp = client.post("/auth/password/forgot", json={"email": "fp@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert len(backend.sent_reset_emails) == 1


def test_auth_forgot_password_unknown_email_still_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """For unknown emails the endpoint returns the same success shape (anti-enumeration)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/forgot", json={"email": "nobody@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.sent_reset_emails == []


def test_auth_reset_password_consumes_token_and_updates_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid reset token updates the account password; it cannot be reused."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "rp@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["rp@e.com"].user_id
    token = backend.issue_reset_token(user_id)
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "newpass456"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.accounts_by_id[user_id].password == "newpass456"
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "again789"})
    assert resp.json()["status"] == "INVALID_TOKEN"


def test_auth_reset_password_rejects_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/reset returns 400 when the token or password is missing."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/reset", json={"token": "", "new_password": ""})
    assert resp.status_code == 400


def test_auth_oauth_authorize_returns_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize asks the provider for a redirect URL."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oa@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "google", "callback_url": "http://127.0.0.1:9999/cb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["url"].startswith("https://google.example.com/auth")


def test_auth_oauth_authorize_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "unknown", "callback_url": "http://127.0.0.1/cb"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_oauth_callback_creates_user_and_returns_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback links the provider user, creates an account, and returns tokens."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider(
        "google",
        email="cb@e.com",
        third_party_user_id="tp-1",
        display_name="Callback User",
    )
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "google",
            "callback_url": "http://127.0.0.1:9999/cb",
            "query_params": {"code": "abc", "state": "xyz"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "cb@e.com"
    assert body["user"]["display_name"] == "Callback User"
    assert body["tokens"]["access_token"].startswith("at-")
    assert "cb@e.com" in backend.accounts_by_email


def test_auth_oauth_callback_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "missing",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_get_user_returns_provider_email_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports 'email' for password-registered accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "gu@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["gu@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "email"
    assert body["email"] == "gu@e.com"


def test_auth_get_user_reports_third_party_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports the OAuth provider ID for OAuth accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oauth-user@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "google",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {"code": "a"},
        },
    )
    user_id = backend.accounts_by_email["oauth-user@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "google"


def test_auth_get_user_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} returns 404 when the user does not exist."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/users/does-not-exist")
    assert resp.status_code == 404


# -- Uncovered route and ctx-method tests --


def test_route_get_service_auth_returns_empty_rules_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../services/.../auth returns {'rules': []} when no policy is set."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services/web/auth", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


def test_route_set_service_auth_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /tunnels/.../services/.../auth admin path persists the policy."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.put(
        "/tunnels/testuser--agent1/services/web/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "updated"}


def test_route_get_tunnel_auth_returns_empty_rules_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../auth returns an empty rules list when no tunnel-level policy is set."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


def test_route_create_and_list_service_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST/GET /tunnels/.../service-tokens round-trip through ForwardingCtx."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "my-token"
    assert body["client_secret"] is not None
    resp = client.get("/tunnels/testuser--agent1/service-tokens", headers=_admin_headers())
    assert resp.status_code == 200
    listed = resp.json()
    # The created token is surfaced by the listing; the secret is only returned
    # at creation time, never on a later listing.
    assert [t["name"] for t in listed] == ["my-token"]
    assert listed[0]["client_secret"] is None


def test_route_service_tokens_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent Bearer auth can't create service tokens (admin-only)."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../services admin path lists services."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_admin_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_admin_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_delete_tunnel_admin_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admin can delete a tunnel they own."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_admin_headers())
    assert resp.status_code == 200
    resp = client.get("/tunnels", headers=_admin_headers())
    assert resp.json() == []


# -- Host pool endpoint tests --


def _make_pool_test_client(
    monkeypatch: pytest.MonkeyPatch,
    pool_backend: FakePoolBackend | None = None,
) -> tuple[TestClient, FakePoolBackend]:
    """Create a TestClient with both tunnel-auth and pool-backend fakes installed.

    The returned backend is seeded with the stub admin email as paid so
    paid-feature routes authorize by default; gate tests flip entries via
    ``backend.add_paid_email`` / ``add_paid_domain`` / the CRUD endpoints.
    """
    client = _make_test_client(monkeypatch)
    monkeypatch.setenv("POOL_SSH_PRIVATE_KEY", "fake-management-key-pem")
    backend = pool_backend if pool_backend is not None else make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend


def test_lease_host_returns_available_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns a host when one is available with matching version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        vps_address="10.0.0.1",
        agent_id="agent-111",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_db_id"] == "00000000-0000-0000-0000-000000000001"
    assert body["vps_address"] == "10.0.0.1"
    assert body["agent_id"] == "agent-111"
    assert body["host_name"] == "my-workspace"
    assert body["attributes"] == {"version": "v0.1.0"}
    # Verify SSH key was injected on both VPS and container
    assert len(backend.append_key_calls) == 2
    # Verify host was marked as leased and the user-supplied host_name was
    # written to the row.
    assert backend.pool_rows[0].status == "leased"
    assert backend.pool_rows[0].leased_to_user == _ADMIN_STUB_USERNAME
    assert backend.pool_rows[0].host_name == "my-workspace"


def test_lease_host_returns_503_when_pool_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when no hosts are available."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]


def test_lease_host_returns_503_when_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease returns 503 when available hosts have a different version."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.2.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert "No pre-created agents" in resp.json()["detail"]
    # Verify the host was not leased
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_filters_out_other_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` only leases a host in that datacenter; otherwise 503."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-WEST-OR",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 503
    assert backend.pool_rows[0].status == "available"


def test_lease_host_hard_region_leases_matching_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hard ``region`` leases a host whose region matches."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        region="US-EAST-VA",
    )
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
            "region": "US-EAST-VA",
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert backend.pool_rows[0].status == "leased"


def test_lease_host_rejects_invalid_host_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/lease rejects host_name values that fail the SafeName regex."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "bad.name",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 422
    # The available row stays available since validation rejected the request
    # before the SELECT/UPDATE.
    assert backend.pool_rows[0].status == "available"


def test_release_host_succeeds_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release strips OVH tags, cancels the VPS, and drops the row."""
    client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    # Row fully cleaned up (deleted), VPS cancelled, stale tags stripped.
    assert backend.pool_rows == []
    assert backend.ovh_ops.cancelled == [row.vps_instance_id]
    stripped_keys = {key for _urn, key in backend.ovh_ops.deleted_tags}
    assert stripped_keys == {"minds_env", "mngr-host-id"}
    # The provider tag is never stripped.
    assert all(key != "mngr-provider" for _urn, key in backend.ovh_ops.deleted_tags)


def test_release_host_idempotent_when_already_removing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release on a row already in 'removing' re-drives cleanup and returns 200."""
    client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-000000000077"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000077/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "released"
    assert backend.pool_rows == []
    assert backend.ovh_ops.cancelled == [row.vps_instance_id]


def test_release_host_fails_loudly_when_ovh_cancel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed OVH cancel makes release return an error -- never a false success.

    Synchronous release contract: a "released" 200 must mean the VPS is actually
    cancelled. When the cancel fails the endpoint returns 5xx and keeps the row
    as 'removing' so the client (or the sweep backstop) retries -- the opposite
    of the old behavior, which returned 200 and silently stranded the VPS.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.ovh_ops.fail_on_cancel = True
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000099"), version="v0.1.0", leased_to_user=_ADMIN_STUB_USERNAME
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000099/release", headers=_admin_headers())
    assert resp.status_code == 502
    # The row is NOT deleted; it stays 'removing' so the teardown is retryable.
    assert len(backend.pool_rows) == 1
    assert backend.pool_rows[0].status == "removing"


def test_release_host_returns_403_for_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release returns 403 when the caller is not the lease owner."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"), version="v0.1.0", leased_to_user="other-user"
    )
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 403
    assert "do not own" in resp.json()["detail"]
    # Verify the host was not released
    assert backend.pool_rows[0].status == "leased"


def test_release_host_unknown_returns_already_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /hosts/{id}/release on a missing row returns 200 already_released (idempotent)."""
    client, _backend = _make_pool_test_client(monkeypatch)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000999/release", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_released"


def test_list_hosts_returns_leased_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /hosts returns only hosts leased by the authenticated user."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000001"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
        agent_id="agent-aaa",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000002"),
        version="v0.1.0",
        leased_to_user="other-user",
        agent_id="agent-bbb",
    )
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000003"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
        agent_id="agent-ccc",
    )
    resp = client.get("/hosts", headers=_admin_headers())
    assert resp.status_code == 200
    hosts = resp.json()
    assert len(hosts) == 2
    host_ids = {h["host_db_id"] for h in hosts}
    assert host_ids == {"00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000003"}


def test_route_lease_host_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool-lease route denies a caller whose email is not in the paid lists."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-000000000001"), version="v0.1.0")
    # Flip the seeded stub email to not-paid.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post(
        "/hosts/lease",
        json={
            "ssh_public_key": "ssh-ed25519 AAAA testkey",
            "host_name": "my-workspace",
            "attributes": {"version": "v0.1.0"},
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    # Verify the gate fired before any DB / SSH side effects ran.
    assert backend.pool_rows[0].status == "available"
    assert backend.append_key_calls == []


def test_route_release_host_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_ADMIN_STUB_USERNAME,
    )
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/hosts/00000000-0000-0000-0000-000000000042/release", headers=_admin_headers())
    assert resp.status_code == 403
    assert backend.pool_rows[0].status == "leased"


def test_route_list_hosts_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/hosts", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_create_litellm_key_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LiteLLM key-create route enforces the paid-list gate.

    The gate fires before any LiteLLM HTTP call, so this test does not need
    to stub the LiteLLM proxy.
    """
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/keys/create", json={}, headers=_admin_headers())
    assert resp.status_code == 403


def test_route_list_litellm_keys_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/keys", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_get_litellm_key_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.get("/keys/some-key-id", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_update_litellm_key_budget_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.put("/keys/some-key-id/budget", json={}, headers=_admin_headers())
    assert resp.status_code == 403


def test_route_delete_litellm_key_returns_403_when_email_not_paid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.delete("/keys/some-key-id", headers=_admin_headers())
    assert resp.status_code == 403


def test_route_create_tunnel_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloudflare forwarding (`/tunnels/*`) must work even when the user is not paid."""
    client, backend = _make_pool_test_client(monkeypatch)
    # Not-paid: the tunnel route should be unaffected by the paid gate.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["tunnel_name"] == f"{_ADMIN_STUB_USERNAME}--agent1"


def test_route_list_services_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel services routes work for any verified email regardless of paid status."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    create_resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_admin_headers())
    assert create_resp.status_code == 200
    list_resp = client.get(f"/tunnels/{_ADMIN_STUB_USERNAME}--agent1/services", headers=_admin_headers())
    assert list_resp.status_code == 200


# -- Paid-list CRUD endpoint tests (admin-key authenticated) --


def _paid_admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_PAID_ADMIN_KEY_TEST_VALUE}"}


def _make_paid_crud_test_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakePoolBackend]:
    """Test client with the paid-admin key configured and a fresh paid-list backend."""
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_PAID_ADMIN_KEY", _PAID_ADMIN_KEY_TEST_VALUE)
    return client, backend


def test_paid_crud_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # No Authorization header.
    assert client.get("/paid/domains").status_code == 401
    # Wrong key.
    bad = client.get("/paid/domains", headers={"Authorization": "Bearer wrong-key"})
    assert bad.status_code == 401
    # A SuperTokens admin JWT is NOT accepted on the paid CRUD endpoints.
    assert client.get("/paid/domains", headers=_admin_headers()).status_code == 401


def test_paid_crud_rejects_non_ascii_bearer_token_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII bearer credential is a clean 401, not a 500 (compare over bytes)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # HTTP header values are latin-1; pass raw bytes (both key and value, which
    # matches httpx's ``Mapping[bytes, bytes]`` header type) so the non-ASCII
    # octets reach the handler -- httpx would otherwise reject a non-ASCII str.
    resp = client.get("/paid/domains", headers={b"Authorization": "Bearer wröng-kéy".encode("latin-1")})
    assert resp.status_code == 401


def test_paid_crud_returns_403_when_admin_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.delenv("MINDS_PAID_ADMIN_KEY", raising=False)
    resp = client.get("/paid/domains", headers=_paid_admin_headers())
    assert resp.status_code == 403
    assert "not enabled" in resp.json()["detail"]


def test_paid_admin_key_is_rejected_on_user_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin key must not authenticate user-facing routes (e.g. /hosts)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.get("/hosts", headers=_paid_admin_headers())
    assert resp.status_code == 401


def test_add_and_list_paid_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    add_resp = client.post("/paid/domains/add", json={"value": "Imbue.com"}, headers=_paid_admin_headers())
    assert add_resp.status_code == 200
    assert add_resp.json() == {"status": "added", "domain": "imbue.com"}
    list_resp = client.get("/paid/domains", headers=_paid_admin_headers())
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert [r["domain"] for r in rows] == ["imbue.com"]
    assert rows[0]["is_paid"] is True


def test_remove_paid_domain_is_soft_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    remove_resp = client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    assert remove_resp.status_code == 200
    # The row is still present (soft delete), but is_paid is now false.
    all_rows = client.get("/paid/domains", headers=_paid_admin_headers()).json()
    assert [(r["domain"], r["is_paid"]) for r in all_rows] == [("imbue.com", False)]
    # paid_only filter hides it.
    paid_rows = client.get("/paid/domains?paid_only=true", headers=_paid_admin_headers()).json()
    assert paid_rows == []


def test_re_adding_soft_removed_domain_reactivates_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    original = client.get("/paid/domains", headers=_paid_admin_headers()).json()[0]
    client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    reactivated = client.get("/paid/domains", headers=_paid_admin_headers()).json()[0]
    assert reactivated["is_paid"] is True
    # created_at is preserved across the remove/re-add cycle.
    assert reactivated["created_at"] == original["created_at"]


def test_add_paid_domain_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    first = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    second = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_paid_admin_headers())
    assert first.status_code == 200
    assert second.status_code == 200
    rows = client.get("/paid/domains", headers=_paid_admin_headers()).json()
    assert len(rows) == 1


def test_remove_absent_paid_email_is_idempotent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/remove", json={"value": "nobody@nowhere.com"}, headers=_paid_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"status": "removed", "email": "nobody@nowhere.com"}


def test_add_paid_email_then_gate_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: add a paid email via CRUD, then a user with that email passes the gate."""
    client, backend = _make_paid_crud_test_client(monkeypatch)
    # Start from a clean slate where the stub email is not paid.
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    assert client.get("/hosts", headers=_admin_headers()).status_code == 403
    client.post("/paid/emails/add", json={"value": _ADMIN_STUB_EMAIL}, headers=_paid_admin_headers())
    assert client.get("/hosts", headers=_admin_headers()).status_code == 200


@pytest.mark.parametrize("bad_value", ["", "   ", "has space", "foo@bar.com"])
def test_add_paid_domain_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/domains/add", json={"value": bad_value}, headers=_paid_admin_headers())
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_value", ["", "no-at-sign", "@nodomain", "local@", "a b@c.com"])
def test_add_paid_email_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/add", json={"value": bad_value}, headers=_paid_admin_headers())
    assert resp.status_code == 400


# -- R2 bucket endpoint tests --


_ADMIN_STUB_USER_ID = "12345678-1234-5678-1234-567812345678"


def _make_bucket_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeCloudflareOps, InMemoryKeyStore]:
    """Create a TestClient with the R2 fakes installed (Cloudflare ops + key store)."""
    client = _make_test_client(monkeypatch)
    # Build our own fake ctx so the fake is typed as FakeForwardingCtx (which
    # exposes ``.fake``); re-patching get_ctx overrides the one _make_test_client
    # installed.
    fake_ctx = make_fake_forwarding_ctx()
    store = make_fake_key_store()
    # Single-loop patching (same pattern as the Fake*Backend.install_on_app_module
    # helpers) so the monkeypatch ratchet only counts one occurrence.
    bucket_fakes: dict[str, object] = {
        "get_ctx": lambda: fake_ctx,
        "get_key_store": lambda: store,
        "_get_user_id_from_access_token": lambda token: _ADMIN_STUB_USER_ID,
    }
    for name, fake_impl in bucket_fakes.items():
        monkeypatch.setattr(app_mod, name, fake_impl)
    return client, fake_ctx.fake, store


# --- Route tests ---


def test_create_bucket_returns_bucket_and_default_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "my-data"}, headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"]["bucket_name"] == "testuser--my-data"
    assert body["bucket"]["s3_endpoint"] == "https://test-account.r2.cloudflarestorage.com"
    assert body["key"]["access"] == "readwrite"
    assert body["key"]["bucket_name"] == "testuser--my-data"
    access_key_id = body["key"]["access_key_id"]
    assert access_key_id
    # Secret is the sha256 of the fake token value, returned once.
    assert body["key"]["secret_access_key"] == derive_s3_secret_access_key(f"token-value-{access_key_id}")
    # Bucket actually created in the fake.
    assert "testuser--my-data" in fake.buckets
    # Key metadata recorded; the secret/token value is NOT persisted.
    rows = store.list_keys(_ADMIN_STUB_USER_ID, None)
    assert len(rows) == 1
    assert rows[0]["access_key_id"] == access_key_id
    assert "secret_access_key" not in rows[0]
    assert "value" not in rows[0]
    assert rows[0]["owner_user_id"] == _ADMIN_STUB_USER_ID


def test_create_bucket_with_read_access(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "ro", "access": "read"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["key"]["access"] == "read"


def test_create_bucket_invalid_access_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "x", "access": "write"}, headers=_admin_headers())
    assert resp.status_code == 422


def test_create_bucket_invalid_name_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "!!!"}, headers=_admin_headers())
    assert resp.status_code == 400


def test_create_bucket_duplicate_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    assert client.post("/buckets", json={"name": "dup"}, headers=_admin_headers()).status_code == 200
    resp = client.post("/buckets", json={"name": "dup"}, headers=_admin_headers())
    assert resp.status_code == 409


def test_create_bucket_at_cap_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    for i in range(_MAX_BUCKETS_PER_ACCOUNT):
        name = f"testuser--b{i}"
        fake.buckets[name] = {"name": name}
        fake.bucket_objects[name] = []
    resp = client.post("/buckets", json={"name": "one-more"}, headers=_admin_headers())
    assert resp.status_code == 409


def test_list_buckets_returns_only_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "a"}, headers=_admin_headers())
    client.post("/buckets", json={"name": "b"}, headers=_admin_headers())
    # A bucket owned by someone else, plus a crafted name that merely *contains*
    # the prefix -- the in-code startswith re-check must exclude it.
    fake.buckets["otheruser--secret"] = {"name": "otheruser--secret"}
    fake.buckets["evil-testuser--x"] = {"name": "evil-testuser--x"}
    resp = client.get("/buckets", headers=_admin_headers())
    assert resp.status_code == 200
    names = sorted(b["bucket_name"] for b in resp.json())
    assert names == ["testuser--a", "testuser--b"]


def test_get_bucket_info(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    resp = client.get("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["bucket_name"] == "testuser--data"


def test_get_bucket_info_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.get("/buckets/missing", headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_bucket_non_empty_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    fake.bucket_objects["testuser--data"].append("obj1")
    resp = client.delete("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 409
    assert "testuser--data" in fake.buckets


def test_destroy_bucket_empty_cascades_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    client.post("/buckets/data/keys", json={}, headers=_admin_headers())
    assert len(store.list_keys(_ADMIN_STUB_USER_ID, None)) == 2
    assert len(fake.account_tokens) == 2
    resp = client.delete("/buckets/data", headers=_admin_headers())
    assert resp.status_code == 200
    assert "testuser--data" not in fake.buckets
    assert store.list_keys(_ADMIN_STUB_USER_ID, None) == []
    assert fake.account_tokens == {}


def test_create_additional_key_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_admin_headers())
    resp = client.post("/buckets/data/keys", json={"alias": "ro", "access": "read"}, headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["access"] == "read"
    per_bucket = client.get("/buckets/data/keys", headers=_admin_headers()).json()
    assert sorted(k["alias"] for k in per_bucket) == ["default", "ro"]
    account_wide = client.get("/bucket-keys", headers=_admin_headers()).json()
    assert len(account_wide) == 2


def test_create_key_for_missing_bucket_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets/nope/keys", json={}, headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    create = client.post("/buckets", json={"name": "data"}, headers=_admin_headers()).json()
    access_key_id = create["key"]["access_key_id"]
    resp = client.delete(f"/bucket-keys/{access_key_id}", headers=_admin_headers())
    assert resp.status_code == 200
    assert access_key_id not in fake.account_tokens
    assert store.get_key(access_key_id) is None


def test_destroy_key_unknown_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.delete("/bucket-keys/does-not-exist", headers=_admin_headers())
    assert resp.status_code == 404


def test_destroy_key_not_owned_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, store = _make_bucket_test_client(monkeypatch)
    store.add_key("akid-other", "some-other-user", "other--bucket", "readwrite", "x")
    resp = client.delete("/bucket-keys/akid-other", headers=_admin_headers())
    assert resp.status_code == 404
    # The other user's row is untouched.
    assert store.get_key("akid-other") is not None


def test_buckets_require_paid_account(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    # Install a paid-list backend where the stub admin email is NOT paid.
    backend = make_fake_pool_backend()
    backend.add_paid_email(_ADMIN_STUB_EMAIL, is_paid=False)
    backend.install_on_app_module(app_mod, monkeypatch)
    resp = client.post("/buckets", json={"name": "x"}, headers=_admin_headers())
    assert resp.status_code == 403
