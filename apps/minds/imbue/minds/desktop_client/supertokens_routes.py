"""SuperTokens authentication routes for the minds desktop client.

These routes render the sign-in / sign-up / password-reset / settings pages
and provide JSON APIs consumed by those pages' vanilla JS. All actual
SuperTokens operations are delegated to the `remote_service_connector`
backend via `AuthBackendClient`; the desktop client never sees the
SuperTokens API key, OAuth client secrets, or any other server-only
credential.
"""

import html
import json
import threading
import time
import webbrowser
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger

from imbue.minds.desktop_client.auth_backend_client import AuthBackendClient
from imbue.minds.desktop_client.auth_backend_client import AuthBackendError
from imbue.minds.desktop_client.auth_backend_client import AuthResult
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import UserInfo
from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _get_session_store(request: Request) -> MultiAccountSessionStore:
    return request.app.state.session_store


def _get_auth_backend(request: Request) -> AuthBackendClient:
    return request.app.state.auth_backend_client


def _get_latest_user_info(session_store: MultiAccountSessionStore) -> UserInfo | None:
    accounts = session_store.list_accounts()
    if not accounts:
        return None
    return session_store.get_user_info(str(accounts[-1].user_id))


def _get_server_port(request: Request) -> int:
    return request.app.state.auth_server_port


def _get_output_format(request: Request) -> OutputFormat:
    return request.app.state.auth_output_format


def _store_session_from_auth_result(
    session_store: MultiAccountSessionStore,
    result: AuthResult,
) -> None:
    """Persist the session tokens + user info from a successful auth result."""
    assert result.user is not None and result.tokens is not None, "AuthResult missing user/tokens"
    session_store.add_or_update_session(
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
        user_id=result.user.user_id,
        email=result.user.email,
        display_name=result.user.display_name,
    )


def _auth_error_response(exc: AuthBackendError | httpx.HTTPError) -> Response:
    logger.warning("Auth backend unavailable: {}", exc)
    return _json_response(
        {"status": "ERROR", "message": "Authentication service is unavailable"},
        502,
    )


def _handle_auth_page(request: Request, message: str | None = None) -> HTMLResponse:
    """Render the sign-up or sign-in page."""
    session_store = _get_session_store(request)
    default_to_signup = not session_store.has_signed_in_before()
    return HTMLResponse(
        render_auth_page(
            default_to_signup=default_to_signup,
            message=message,
            server_port=_get_server_port(request),
        )
    )


async def _handle_signup_api(request: Request) -> Response:
    """Handle email/password sign-up (JSON API)."""
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    body = await request.json()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        return _json_response({"status": "FIELD_ERROR", "message": "Email and password are required"}, 400)

    try:
        result = backend.signup(email=email, password=password)
    except (httpx.HTTPError, AuthBackendError) as exc:
        return _auth_error_response(exc)

    if result.status != "OK":
        return _json_response({"status": result.status, "message": result.message or ""})

    _store_session_from_auth_result(session_store, result)
    assert result.user is not None
    return _json_response(
        {"status": "OK", "userId": result.user.user_id, "needsEmailVerification": result.needs_email_verification}
    )


async def _handle_signin_api(request: Request) -> Response:
    """Handle email/password sign-in (JSON API)."""
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    body = await request.json()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        return _json_response({"status": "FIELD_ERROR", "message": "Email and password are required"}, 400)

    try:
        result = backend.signin(email=email, password=password)
    except (httpx.HTTPError, AuthBackendError) as exc:
        return _auth_error_response(exc)

    if result.status != "OK":
        return _json_response({"status": result.status, "message": result.message or ""})

    _store_session_from_auth_result(session_store, result)
    assert result.user is not None
    return _json_response(
        {
            "status": "OK",
            "userId": result.user.user_id,
            "needsEmailVerification": result.needs_email_verification,
        }
    )


async def _handle_signout_api(request: Request) -> Response:
    """Handle sign-out for a specific account.

    Expects a JSON body with a ``user_id`` field identifying which account to
    sign out. If no user_id is provided, returns an error.

    Revokes every SuperTokens session for the user on the backend before
    deleting the local record -- otherwise the access/refresh tokens stored
    on disk remain valid until their natural expiry even after the user
    clicks "Sign out". A backend revoke failure is logged but does not block
    the local deletion: the user's intent to sign out is honored locally
    regardless.
    """
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    try:
        body = await request.json()
        user_id = body.get("user_id")
    except (json.JSONDecodeError, ValueError):
        user_id = None

    if not user_id:
        return _json_response({"status": "ERROR", "message": "user_id is required"}, 400)

    # The backend derives the user_id from the access token we send, so an
    # attacker cannot revoke a different user's sessions by POSTing here.
    # If the token cannot be refreshed (e.g. offline), skip the backend call
    # -- we still honor the local sign-out intent, and any stolen token from
    # this machine will expire naturally.
    access_token = session_store.get_access_token(str(user_id))
    if access_token is not None:
        backend.revoke_all_sessions(access_token)
    else:
        logger.warning("No usable access token for user {}; skipping backend revoke", str(user_id)[:8])
    session_store.remove_session(str(user_id))
    return _json_response({"status": "OK"})


def _handle_status_api(request: Request) -> Response:
    """Return current auth status and user info."""
    session_store = _get_session_store(request)
    user_info = _get_latest_user_info(session_store)
    if user_info is None:
        return _json_response({"signedIn": False})
    return _json_response(
        {
            "signedIn": True,
            "userId": str(user_info.user_id),
            "email": user_info.email,
            "displayName": user_info.display_name,
            "userIdPrefix": str(user_info.user_id_prefix),
        }
    )


def _handle_email_verified_api(request: Request) -> Response:
    """Check if the current user's email is verified."""
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    user_info = _get_latest_user_info(session_store)
    if user_info is None:
        return _json_response({"verified": False, "signedIn": False})
    try:
        verified = backend.is_email_verified(str(user_info.user_id), user_info.email)
    except httpx.HTTPError as exc:
        logger.warning("Auth backend unreachable during is-email-verified: {}", exc)
        return _json_response({"verified": False, "signedIn": True, "error": "backend_unavailable"}, 502)
    return _json_response({"verified": verified, "signedIn": True})


def _handle_resend_verification_api(request: Request) -> Response:
    """Resend the email verification email."""
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    user_info = _get_latest_user_info(session_store)
    if user_info is None:
        return _json_response({"status": "ERROR", "message": "Not signed in"}, 401)
    try:
        ok = backend.send_verification_email(str(user_info.user_id), user_info.email)
    except httpx.HTTPError as exc:
        logger.warning("Auth backend unreachable during resend-verification: {}", exc)
        return _json_response({"status": "ERROR", "message": "Authentication service is unavailable"}, 502)
    if not ok:
        return _json_response({"status": "ERROR", "message": "Failed to send verification email"}, 502)
    return _json_response({"status": "OK"})


def _handle_check_email_page(request: Request) -> HTMLResponse:
    """Render the 'check your email' page."""
    session_store = _get_session_store(request)
    user_info = _get_latest_user_info(session_store)
    email = user_info.email if user_info else "your email"
    return HTMLResponse(render_check_email_page(email=email))


# In-memory map of ``provider_id -> (state, expiry_timestamp)`` protecting the
# local OAuth callback against forged requests from other processes on the
# same loopback interface. The state value is whatever the upstream provider
# (via the SuperTokens SDK) embedded in the authorize URL -- we record it at
# redirect time and require it to echo back unchanged on the callback. An
# attacker process hitting ``http://127.0.0.1:{port}/auth/callback/{provider}``
# with its own stolen authorization code would not know this state, so the
# callback is rejected before we forward anything to the backend.
_OAUTH_STATE_TTL_SECONDS = 10 * 60
_oauth_pending_states: dict[str, tuple[str, float]] = {}
_oauth_pending_states_lock = threading.Lock()


def _extract_state_from_auth_url(auth_url: str) -> str | None:
    """Return the ``state`` query parameter from an OAuth authorize URL, if any."""
    parsed = urlparse(auth_url)
    states = parse_qs(parsed.query).get("state")
    if states:
        return states[0]
    return None


def _remember_oauth_state(provider_id: str, state: str) -> None:
    """Persist the expected OAuth state for a provider with a short expiry."""
    with _oauth_pending_states_lock:
        _prune_expired_oauth_states_locked()
        _oauth_pending_states[provider_id] = (state, time.monotonic() + _OAUTH_STATE_TTL_SECONDS)


def _consume_oauth_state(provider_id: str) -> str | None:
    """Pop and return the stored OAuth state for a provider, or None if none/expired."""
    with _oauth_pending_states_lock:
        _prune_expired_oauth_states_locked()
        entry = _oauth_pending_states.pop(provider_id, None)
    return entry[0] if entry is not None else None


def _prune_expired_oauth_states_locked() -> None:
    now = time.monotonic()
    expired = [p for p, (_, exp) in _oauth_pending_states.items() if exp <= now]
    for p in expired:
        _oauth_pending_states.pop(p, None)


def _handle_oauth_redirect(provider_id: str, request: Request) -> Response:
    """Initiate OAuth by opening the system browser at the provider-specific URL."""
    backend = _get_auth_backend(request)
    server_port = _get_server_port(request)
    callback_url = f"http://127.0.0.1:{server_port}/auth/callback/{provider_id}"
    try:
        auth_url = backend.oauth_authorize_url(provider_id=provider_id, callback_url=callback_url)
    except (httpx.HTTPError, AuthBackendError) as exc:
        logger.warning("Auth backend unreachable during oauth_authorize for {}: {}", provider_id, exc)
        return _json_response({"status": "ERROR", "error": "Authentication service is unavailable"}, 502)
    if auth_url is None:
        return _json_response({"status": "ERROR", "error": f"Unknown provider: {provider_id}"}, 404)

    # Extract the SDK's state so we can verify it on callback. If the SDK didn't
    # include one (older versions, or custom providers), we still open the URL
    # but skip the state check -- nothing to compare against.
    state = _extract_state_from_auth_url(auth_url)
    if state is not None:
        _remember_oauth_state(provider_id, state)
    else:
        logger.warning("OAuth authorize URL for {} has no state param; skipping CSRF state check", provider_id)

    webbrowser.open(auth_url)
    return _json_response({"status": "OK", "message": f"Opened {provider_id} sign-in in your browser"})


def _handle_oauth_callback(provider_id: str, request: Request) -> HTMLResponse:
    """Handle OAuth callback from the provider (opened in system browser)."""
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    output_format = _get_output_format(request)
    server_port = _get_server_port(request)
    callback_url = f"http://127.0.0.1:{server_port}/auth/callback/{provider_id}"
    query_params = dict(request.query_params)

    # CSRF: only accept callbacks whose ``state`` matches the one recorded at
    # ``_handle_oauth_redirect``. Rejecting stale/missing/mismatched state is
    # what prevents a local attacker process from forging callbacks with a
    # stolen authorization code for a different account.
    expected_state = _consume_oauth_state(provider_id)
    actual_state = query_params.get("state")
    if expected_state is not None and (actual_state is None or actual_state != expected_state):
        logger.warning("OAuth callback rejected for {}: state mismatch", provider_id)
        return HTMLResponse(
            "<html><body><h1>Authentication failed</h1>"
            "<p>The OAuth callback did not originate from a sign-in you started. "
            "Please try again from the app.</p></body></html>",
            status_code=400,
        )

    try:
        result = backend.oauth_callback(
            provider_id=provider_id,
            callback_url=callback_url,
            query_params=query_params,
        )
    except (httpx.HTTPError, AuthBackendError) as exc:
        logger.error("OAuth callback failed for {}: {}", provider_id, exc)
        safe_exc = html.escape(str(exc), quote=True)
        return HTMLResponse(
            f"<html><body><h1>Authentication failed</h1><p>{safe_exc}</p></body></html>",
            status_code=502,
        )

    if result.status != "OK" or result.user is None or result.tokens is None:
        message = result.message or "Sign-in failed"
        safe_message = html.escape(message, quote=True)
        return HTMLResponse(
            f"<html><body><h1>Authentication failed</h1><p>{safe_message}</p></body></html>",
            status_code=400,
        )

    _store_session_from_auth_result(session_store, result)

    emit_event(
        "auth_success",
        {
            "message": f"Signed in as {result.user.display_name or result.user.email}",
            "email": result.user.email,
        },
        output_format,
    )

    return HTMLResponse(render_oauth_close_page(email=result.user.email, display_name=result.user.display_name))


def _handle_forgot_password_page(request: Request) -> HTMLResponse:
    """Render the forgot password page."""
    return HTMLResponse(render_forgot_password_page())


async def _handle_forgot_password_api(request: Request) -> Response:
    """Send a password reset email.

    This endpoint always returns a generic success response regardless of
    whether the email exists or whether the backend call succeeds. Leaking
    backend errors would enable email enumeration.
    """
    backend = _get_auth_backend(request)
    body = await request.json()
    email = body.get("email", "").strip()
    if not email:
        return _json_response({"status": "FIELD_ERROR", "message": "Email is required"}, 400)
    try:
        backend.forgot_password(email)
    except (httpx.HTTPError, AuthBackendError) as exc:
        logger.warning("Auth backend unavailable during forgot-password; returning generic success: {}", exc)
    return _json_response({"status": "OK", "message": "If an account exists, a reset email has been sent"})


def _handle_reset_password_redirect(request: Request) -> Response:
    """Redirect legacy in-app reset links to the auth backend's reset page.

    The reset link embedded in the reset email now points at the backend
    directly; this redirect keeps any older links working.
    """
    backend = _get_auth_backend(request)
    token = request.query_params.get("token", "")
    target = str(backend.base_url).rstrip("/") + "/auth/reset-password"
    if token:
        target = f"{target}?{urlencode({'token': token})}"
    return Response(status_code=302, headers={"Location": target})


def _handle_settings_page(request: Request) -> HTMLResponse:
    """Render the account settings page."""
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    user_info = _get_latest_user_info(session_store)
    if user_info is None:
        return HTMLResponse(
            status_code=302,
            headers={"Location": "/auth/login"},
        )

    try:
        provider = backend.get_user_provider(str(user_info.user_id))
    except httpx.HTTPError as exc:
        logger.warning("Auth backend unreachable during settings page load: {}", exc)
        provider = "email"

    return HTMLResponse(
        render_settings_page(
            email=user_info.email,
            display_name=user_info.display_name,
            user_id=str(user_info.user_id),
            provider=provider,
            user_id_prefix=str(user_info.user_id_prefix),
        )
    )


def create_supertokens_router(
    session_store: MultiAccountSessionStore,
    auth_backend_client: AuthBackendClient,
    server_port: int,
    output_format: OutputFormat,
) -> APIRouter:
    """Create a FastAPI router with the auth routes.

    Stores dependencies in ``app.state`` so module-level handlers can access
    them. The caller is expected to register this router on an app whose
    ``app.state`` already has ``session_store``, ``auth_backend_client``,
    ``auth_server_port``, and ``auth_output_format`` populated by
    ``create_desktop_client``.
    """
    _ = session_store, auth_backend_client, server_port, output_format
    router = APIRouter(prefix="/auth", tags=["auth"])

    router.get("/login")(_handle_auth_page)
    router.post("/api/signup")(_handle_signup_api)
    router.post("/api/signin")(_handle_signin_api)
    router.post("/api/signout")(_handle_signout_api)
    router.get("/api/status")(_handle_status_api)
    router.get("/api/email-verified")(_handle_email_verified_api)
    router.post("/api/resend-verification")(_handle_resend_verification_api)
    router.get("/check-email")(_handle_check_email_page)
    router.get("/oauth/{provider_id}")(_handle_oauth_redirect)
    router.get("/callback/{provider_id}")(_handle_oauth_callback)
    router.get("/forgot-password")(_handle_forgot_password_page)
    router.post("/api/forgot-password")(_handle_forgot_password_api)
    router.get("/reset-password")(_handle_reset_password_redirect)
    router.get("/settings")(_handle_settings_page)

    return router
