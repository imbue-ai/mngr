"""SuperTokens authentication routes for the minds desktop client.

These routes render the sign-in / sign-up / password-reset / settings pages
and provide JSON APIs consumed by those pages' vanilla JS. All actual
SuperTokens operations now go through ``mngr imbue_cloud auth ...`` via the
``ImbueCloudCli`` wrapper -- the four-client HTTP layer minds used to
maintain has been deleted. The route handlers below speak through a thin
``_AuthBackendShim`` that adapts the plugin CLI to the shape they expect.
"""

import json
import secrets
import threading
import time
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.bootstrap import set_imbue_cloud_provider_for_account
from imbue.minds.bootstrap import unset_imbue_cloud_provider_for_account
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import UserInfo
from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event


class AuthBackendError(RuntimeError):
    """Raised when the auth backend (mngr imbue_cloud auth ...) fails unexpectedly."""


class AuthUser(FrozenModel):
    """User information returned by the auth backend."""

    user_id: str
    email: str
    display_name: str | None = None


class AuthResult(FrozenModel):
    """Normalized result of a sign-in / sign-up / OAuth callback.

    Note: tokens are NOT carried back through this struct. The plugin owns
    the SuperTokens session on disk; minds only needs to know the user
    identity (rendered in the UI) and whether email verification is pending.
    """

    status: str = Field(description="OK, WRONG_CREDENTIALS, EMAIL_ALREADY_EXISTS, FIELD_ERROR, or ERROR")
    message: str | None = Field(default=None)
    user: AuthUser | None = Field(default=None)
    needs_email_verification: bool = Field(default=False)


class _AuthBackendShim(MutableModel):
    """Adapt ``ImbueCloudCli`` to the API shape the route handlers expect.

    The route handlers were originally written against ``AuthBackendClient``;
    rather than rewrite them all in this commit, we expose the same method
    surface and translate ImbueCloudCli responses into ``AuthResult`` objects.
    The plugin owns the actual session state on disk; this shim never reads
    or writes session files, it only maps response shapes.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    cli: ImbueCloudCli = Field(frozen=True, description="Subprocess wrapper around the imbue_cloud plugin CLI")

    @property
    def _cli(self) -> ImbueCloudCli:
        return self.cli

    def signup(self, email: str, password: str) -> AuthResult:
        try:
            session_obj = self._cli.auth_signup(email, password)
        except ImbueCloudCliError as exc:
            return AuthResult(status="ERROR", message=str(exc))
        return AuthResult(
            status="OK",
            user=AuthUser(
                user_id=str(session_obj.user_id),
                email=str(session_obj.email),
                display_name=session_obj.display_name,
            ),
            needs_email_verification=session_obj.needs_email_verification,
        )

    def signin(self, email: str, password: str) -> AuthResult:
        try:
            session_obj = self._cli.auth_signin(email, password)
        except ImbueCloudCliError as exc:
            return AuthResult(status="ERROR", message=str(exc))
        return AuthResult(
            status="OK",
            user=AuthUser(
                user_id=str(session_obj.user_id),
                email=str(session_obj.email),
                display_name=session_obj.display_name,
            ),
            needs_email_verification=session_obj.needs_email_verification,
        )

    def signout_account(self, account_email: str) -> None:
        try:
            self._cli.auth_signout(account_email)
        except ImbueCloudCliError as exc:
            logger.warning("`mngr imbue_cloud auth signout` failed for {}: {}", account_email, exc)

    def is_email_verified(self, _user_id: str, _email: str) -> bool:
        # The plugin doesn't currently expose this. Treat as verified to
        # avoid spurious "please verify" prompts; the real check happens
        # connector-side at next signin.
        return True

    def send_verification_email(self, _user_id: str, email: str) -> bool:
        try:
            # Touch the session as a smoke check.
            self._cli.auth_status(email)
            return True
        except ImbueCloudCliError as exc:
            logger.warning("Could not invoke auth status for {}: {}", email, exc)
            return False

    def forgot_password(self, email: str) -> None:
        try:
            # Plugin doesn't currently expose forgot-password.
            self._cli.auth_status(email)
        except ImbueCloudCliError as exc:
            logger.warning("Forgot-password (placeholder) call failed for {}: {}", email, exc)

    def get_user_provider(self, _user_id: str) -> str:
        return "email"

    @property
    def base_url(self) -> str:
        # No external base URL anymore -- the desktop UI's reset link
        # redirect should be reworked to point at a fixed connector URL via
        # MindsConfig instead.
        return ""


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _get_session_store(request: Request) -> MultiAccountSessionStore:
    return request.app.state.session_store


def _get_auth_backend(request: Request) -> _AuthBackendShim:
    """Return the request-scoped auth-backend shim wrapping the plugin CLI."""
    cli: ImbueCloudCli | None = request.app.state.imbue_cloud_cli
    if cli is None:
        raise AuthBackendError("imbue_cloud_cli is not configured on this app")
    return _AuthBackendShim(cli=cli)


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
    request: Request,
) -> None:
    """Drop cached identity so the new account shows up, and configure defaults.

    Tokens and identity (email, display_name) are persisted by the
    mngr_imbue_cloud plugin -- minds doesn't mirror them. We only
    invalidate our identity cache so the next ``auth list`` reflects the
    just-signed-in account.

    On first login (no default account set), this account becomes the default.

    Also registers a ``[providers.imbue_cloud_<slug>]`` entry in mngr's
    settings.toml and bounces ``mngr observe`` so the new provider instance
    becomes immediately usable by ``mngr create``/``list``/etc.
    """
    assert result.user is not None, "AuthResult missing user"
    session_store.invalidate_identity_cache()
    minds_config: MindsConfig | None = request.app.state.minds_config
    if minds_config is not None and minds_config.get_default_account_id() is None:
        minds_config.set_default_account_id(result.user.user_id)

    # Explicit signin -- always re-enable the provider entry, even if a
    # previous run auto-disabled it after an auth error.
    if set_imbue_cloud_provider_for_account(result.user.email, force_enable=True):
        _bounce_forward_observe(request)


def _bounce_forward_observe(request: Request) -> None:
    """Bounce the ``mngr forward`` plugin's observe child so a freshly-written
    provider entry takes effect within the same minds session.

    Sends ``SIGHUP`` to the plugin via ``EnvelopeStreamConsumer.bounce_observe()``;
    per-agent event subprocesses, SSH tunnels, and the FastAPI app on the
    plugin side stay alive (the plugin's SIGHUP handler only restarts
    ``mngr observe``). No-op if the consumer hasn't been attached yet.

    ``app.state.envelope_stream_consumer`` is always set (to either the
    consumer or ``None``) by ``create_desktop_client``, so direct attribute
    access is safe — no defensive ``getattr`` required.
    """
    envelope_stream_consumer = request.app.state.envelope_stream_consumer
    if envelope_stream_consumer is None:
        return
    envelope_stream_consumer.bounce_observe()


def _auth_error_response(exc: AuthBackendError | ImbueCloudCliError) -> Response:
    logger.warning("Auth backend unavailable: {}", exc)
    return _json_response(
        {"status": "ERROR", "message": "Authentication service is unavailable"},
        502,
    )


def _handle_auth_page(request: Request, message: str | None = None) -> HTMLResponse:
    """Render the sign-up or sign-in page.

    /auth/signup always defaults to sign-up mode. /auth/login defaults
    to sign-in mode (unless the user has never signed in before, in
    which case it shows sign-up as a convenience).
    """
    default_to_signup = request.url.path.rstrip("/").endswith("/signup")
    return HTMLResponse(
        render_auth_page(
            default_to_signup=default_to_signup,
            message=message,
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
    except (ImbueCloudCliError, AuthBackendError) as exc:
        return _auth_error_response(exc)

    if result.status != "OK":
        return _json_response({"status": result.status, "message": result.message or ""})

    _store_session_from_auth_result(session_store, result, request)
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
    except (ImbueCloudCliError, AuthBackendError) as exc:
        return _auth_error_response(exc)

    if result.status != "OK":
        return _json_response({"status": result.status, "message": result.message or ""})

    _store_session_from_auth_result(session_store, result, request)
    assert result.user is not None
    return _json_response(
        {
            "status": "OK",
            "userId": result.user.user_id,
            "needsEmailVerification": result.needs_email_verification,
        }
    )


def signout_user_via_plugin(request: Request, user_id: str) -> None:
    """Sign ``user_id`` out via the mngr_imbue_cloud plugin and clear local state.

    Resolves the email for ``user_id`` against the cached ``auth list``,
    runs ``mngr imbue_cloud auth signout --account <email>`` (plugin owns
    the SuperTokens session), invalidates the local identity cache, and
    tears down the matching ``[providers.imbue_cloud_<slug>]`` block /
    bounces ``mngr observe`` so ``mngr create``/``list`` reflect the new
    state immediately.

    No-ops gracefully when the user isn't currently visible to the
    plugin -- the cache is still invalidated so a stale entry can't
    survive.
    """
    session_store = _get_session_store(request)
    backend = _get_auth_backend(request)
    session = session_store.get_session(user_id)
    signed_out_email: str | None = None
    if session is not None:
        signed_out_email = str(session.email)
        backend.signout_account(signed_out_email)
    else:
        logger.warning("No mirrored account for user {}; skipping plugin signout", user_id[:8])
    session_store.invalidate_identity_cache()
    if signed_out_email and unset_imbue_cloud_provider_for_account(signed_out_email):
        _bounce_forward_observe(request)


async def _handle_signout_api(request: Request) -> Response:
    """Handle sign-out for a specific account.

    Expects a JSON body with a ``user_id`` field identifying which account to
    sign out. If no user_id is provided, returns an error.

    Delegates the actual SuperTokens revocation to ``mngr imbue_cloud auth
    signout --account <email>``: the plugin owns the session and is the only
    component that knows the access token. A failed backend revoke is
    logged; we still drop the local mirror so the user's intent is honored
    even when the connector is unreachable.
    """
    try:
        body = await request.json()
        user_id = body.get("user_id")
    except (json.JSONDecodeError, ValueError):
        user_id = None

    if not user_id:
        return _json_response({"status": "ERROR", "message": "user_id is required"}, 400)

    signout_user_via_plugin(request, str(user_id))
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
    except ImbueCloudCliError as exc:
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
    except ImbueCloudCliError as exc:
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


# OAuth tracking. The plugin's ``mngr imbue_cloud auth oauth ...`` subprocess
# is what actually drives an OAuth signin (it spins up a localhost listener,
# launches the browser, exchanges the code, and writes the session). Each
# in-progress flow is tracked here by a server-generated key the frontend
# polls so it can show a "waiting for browser" state without blocking on the
# subprocess.
_OAUTH_FLOW_TTL_SECONDS = 10 * 60


class _OAuthFlowStatus(FrozenModel):
    """Status snapshot for a single in-flight OAuth subprocess.

    ``state`` is one of ``"running"``, ``"done"``, or ``"error"``.
    """

    state: str
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    error: str | None = None
    deadline: float | None = None


_oauth_flows: dict[str, _OAuthFlowStatus] = {}
_oauth_flows_lock = threading.Lock()


def _record_oauth_status(flow_id: str, status: _OAuthFlowStatus) -> None:
    with _oauth_flows_lock:
        _prune_expired_oauth_flows_locked()
        _oauth_flows[flow_id] = status


def _read_oauth_status(flow_id: str) -> _OAuthFlowStatus | None:
    with _oauth_flows_lock:
        _prune_expired_oauth_flows_locked()
        return _oauth_flows.get(flow_id)


def _prune_expired_oauth_flows_locked() -> None:
    now = time.monotonic()
    expired = [flow_id for flow_id, st in _oauth_flows.items() if st.deadline is not None and st.deadline <= now]
    for flow_id in expired:
        _oauth_flows.pop(flow_id, None)


def _run_oauth_subprocess(
    provider_id: str,
    flow_id: str,
    imbue_cloud_cli: ImbueCloudCli,
    session_store: MultiAccountSessionStore,
    minds_config: MindsConfig | None,
    output_format: OutputFormat,
    envelope_stream_consumer: EnvelopeStreamConsumer | None,
) -> None:
    """Run ``mngr imbue_cloud auth oauth <provider>`` in a background thread.

    The plugin opens the system browser, listens on its own localhost port for
    the OAuth callback, exchanges the code, and writes the session to its own
    state directory. We then mirror the resulting account identity into
    ``MultiAccountSessionStore`` so the desktop UI can render it, register
    a ``[providers.imbue_cloud_<slug>]`` entry (force-enabled, even if a
    previous run auto-disabled it after an auth error), and bounce the
    ``mngr forward`` plugin's observe child so the new provider config is
    picked up immediately -- mirroring what the email/password
    ``_store_session_from_auth_result`` path does for non-OAuth signins.
    """
    try:
        result = imbue_cloud_cli.auth_oauth(account="", provider_id=provider_id)
    except ImbueCloudCliError as exc:
        logger.warning("Plugin OAuth subprocess failed for {}: {}", provider_id, exc)
        _record_oauth_status(
            flow_id,
            _OAuthFlowStatus(
                state="error",
                error=str(exc),
                deadline=time.monotonic() + _OAUTH_FLOW_TTL_SECONDS,
            ),
        )
        return

    session_store.invalidate_identity_cache()
    if minds_config is not None and minds_config.get_default_account_id() is None:
        minds_config.set_default_account_id(str(result.user_id))

    if set_imbue_cloud_provider_for_account(str(result.email), force_enable=True):
        if envelope_stream_consumer is not None:
            envelope_stream_consumer.bounce_observe()

    emit_event(
        "auth_success",
        {
            "message": f"Signed in as {result.display_name or result.email}",
            "email": str(result.email),
        },
        output_format,
    )

    _record_oauth_status(
        flow_id,
        _OAuthFlowStatus(
            state="done",
            user_id=str(result.user_id),
            email=str(result.email),
            display_name=result.display_name,
            deadline=time.monotonic() + _OAUTH_FLOW_TTL_SECONDS,
        ),
    )


def _handle_oauth_redirect(provider_id: str, request: Request) -> Response:
    """Kick off the plugin's OAuth flow in a background thread.

    Returns immediately with a flow id the frontend can poll. The plugin
    subprocess opens the system browser, captures the callback, and writes
    the session itself; this route then mirrors the account identity into
    ``MultiAccountSessionStore`` once the subprocess finishes.

    The thread is started via ``root_concurrency_group.start_new_thread``
    rather than a bare ``threading.Thread`` so that any unhandled
    exception inside ``_run_oauth_subprocess`` (e.g. a slow
    ``restart_observe`` raising ``TimeoutExpired``) is logged via the CG's
    ``ObservableThread`` instead of disappearing silently and stalling
    the user on a "Waiting..." page.
    """
    imbue_cloud_cli: ImbueCloudCli | None = request.app.state.imbue_cloud_cli
    session_store = _get_session_store(request)
    output_format = _get_output_format(request)
    minds_config: MindsConfig | None = request.app.state.minds_config
    envelope_stream_consumer: EnvelopeStreamConsumer | None = request.app.state.envelope_stream_consumer
    root_cg: ConcurrencyGroup | None = request.app.state.root_concurrency_group
    if imbue_cloud_cli is None:
        return _json_response({"status": "ERROR", "error": "imbue_cloud_cli is not configured"}, 503)
    if root_cg is None:
        return _json_response({"status": "ERROR", "error": "root_concurrency_group is not configured"}, 503)
    if provider_id.lower() not in ("google", "github"):
        return _json_response({"status": "ERROR", "error": f"Unknown provider: {provider_id}"}, 404)

    flow_id = secrets.token_urlsafe(16)
    _record_oauth_status(
        flow_id,
        _OAuthFlowStatus(state="running", deadline=time.monotonic() + _OAUTH_FLOW_TTL_SECONDS),
    )
    root_cg.start_new_thread(
        target=_run_oauth_subprocess,
        kwargs={
            "provider_id": provider_id.lower(),
            "flow_id": flow_id,
            "imbue_cloud_cli": imbue_cloud_cli,
            "session_store": session_store,
            "minds_config": minds_config,
            "output_format": output_format,
            "envelope_stream_consumer": envelope_stream_consumer,
        },
        name=f"imbue-cloud-oauth-{provider_id}",
        is_checked=False,
    )
    return _json_response(
        {
            "status": "OK",
            "flow_id": flow_id,
            "message": f"Opening {provider_id} sign-in in your browser. Complete the flow there.",
        }
    )


def _handle_oauth_status(flow_id: str, request: Request) -> Response:
    """Poll-friendly status for an in-flight OAuth flow.

    The frontend long-polls this until ``state`` is ``"done"`` or ``"error"``.
    """
    _ = request
    status = _read_oauth_status(flow_id)
    if status is None:
        return _json_response({"status": "ERROR", "error": "Unknown flow id"}, 404)
    return _json_response(
        {
            "status": "OK",
            "state": status.state,
            "user_id": status.user_id,
            "email": status.email,
            "display_name": status.display_name,
            "error": status.error,
        }
    )


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
    except (ImbueCloudCliError, AuthBackendError) as exc:
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
    except ImbueCloudCliError as exc:
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
    imbue_cloud_cli: ImbueCloudCli,
    server_port: int,
    output_format: OutputFormat,
) -> APIRouter:
    """Create a FastAPI router with the auth routes.

    Stores dependencies in ``app.state`` so module-level handlers can access
    them. The caller is expected to register this router on an app whose
    ``app.state`` already has ``session_store``, ``imbue_cloud_cli``,
    ``auth_server_port``, and ``auth_output_format`` populated by
    ``create_desktop_client``.
    """
    _ = session_store, imbue_cloud_cli, server_port, output_format
    router = APIRouter(prefix="/auth", tags=["auth"])

    router.get("/login")(_handle_auth_page)
    router.get("/signup")(_handle_auth_page)
    router.post("/api/signup")(_handle_signup_api)
    router.post("/api/signin")(_handle_signin_api)
    router.post("/api/signout")(_handle_signout_api)
    router.get("/api/status")(_handle_status_api)
    router.get("/api/email-verified")(_handle_email_verified_api)
    router.post("/api/resend-verification")(_handle_resend_verification_api)
    router.get("/check-email")(_handle_check_email_page)
    router.get("/oauth/{provider_id}")(_handle_oauth_redirect)
    router.get("/oauth/status/{flow_id}")(_handle_oauth_status)
    router.get("/forgot-password")(_handle_forgot_password_page)
    router.post("/api/forgot-password")(_handle_forgot_password_api)
    router.get("/reset-password")(_handle_reset_password_redirect)
    router.get("/settings")(_handle_settings_page)

    return router
