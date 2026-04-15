"""SuperTokens authentication routes for the minds desktop client.

Provides routes for email/password sign-up/sign-in, OAuth (Google/GitHub),
email verification, password reset, and session status. All routes use
plain HTML + vanilla JS -- no SuperTokens frontend SDK.
"""

import json
import webbrowser

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from supertokens_python.asyncio import list_users_by_account_info
from supertokens_python.exceptions import SuperTokensError
from supertokens_python.recipe.emailpassword.asyncio import consume_password_reset_token
from supertokens_python.recipe.emailpassword.asyncio import send_reset_password_email
from supertokens_python.recipe.emailpassword.asyncio import sign_in
from supertokens_python.recipe.emailpassword.asyncio import sign_up
from supertokens_python.recipe.emailpassword.asyncio import update_email_or_password
from supertokens_python.recipe.emailpassword.interfaces import ConsumePasswordResetTokenOkResult
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import PasswordPolicyViolationError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import UpdateEmailOrPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailverification.asyncio import is_email_verified
from supertokens_python.recipe.emailverification.asyncio import send_email_verification_email
from supertokens_python.recipe.emailverification.asyncio import verify_email_using_token
from supertokens_python.recipe.emailverification.interfaces import VerifyEmailUsingTokenOkResult
from supertokens_python.recipe.emailverification.syncio import is_email_verified as is_email_verified_sync
from supertokens_python.recipe.emailverification.syncio import (
    send_email_verification_email as send_email_verification_email_sync,
)
from supertokens_python.recipe.session.asyncio import create_new_session_without_request_response
from supertokens_python.recipe.thirdparty.asyncio import get_provider
from supertokens_python.recipe.thirdparty.asyncio import manually_create_or_update_user
from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult
from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
from supertokens_python.syncio import get_user
from supertokens_python.types import RecipeUserId
from supertokens_python.types.base import AccountInfoInput

from pydantic import PrivateAttr

from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.supertokens_auth import SuperTokensSessionStore
from imbue.minds.desktop_client.supertokens_auth import SuperTokensUserId as AuthSuperTokensUserId
from imbue.minds.desktop_client.supertokens_auth import UserIdPrefix as AuthUserIdPrefix
from imbue.minds.desktop_client.supertokens_auth import UserInfo
from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_reset_password_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.desktop_client.templates_auth import render_verify_email_failed_page
from imbue.minds.desktop_client.templates_auth import render_verify_email_success_page
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event

_TENANT_ID = "public"


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _get_session_store(request: Request) -> SuperTokensSessionStore:
    """Return the supertokens session store.

    When the app uses MultiAccountSessionStore, a thin adapter wraps it
    to provide the SuperTokensSessionStore interface expected by these routes.
    """
    store = request.app.state.supertokens_session_store
    if isinstance(store, MultiAccountSessionStore):
        return _MultiAccountSessionStoreAdapter.create(store)
    return store


class _MultiAccountSessionStoreAdapter(SuperTokensSessionStore):
    """Adapter that wraps MultiAccountSessionStore for supertokens routes.

    Provides the SuperTokensSessionStore interface by operating on the
    most recently added account (since the supertokens OAuth flow always
    produces a session for the account that just signed in).
    """

    _multi_store: MultiAccountSessionStore = PrivateAttr()

    @staticmethod
    def create(multi_store: MultiAccountSessionStore) -> "_MultiAccountSessionStoreAdapter":
        """Create an adapter wrapping the given multi-account store."""
        adapter = _MultiAccountSessionStoreAdapter(data_directory=multi_store.data_dir / "supertokens")
        adapter._multi_store = multi_store
        return adapter

    def store_session(
        self,
        access_token: str,
        refresh_token: str | None,
        user_id: str,
        email: str,
        display_name: str | None = None,
    ) -> None:
        self._multi_store.add_or_update_session(
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=user_id,
            email=email,
            display_name=display_name,
        )

    def get_user_info(self) -> UserInfo | None:
        accounts = self._multi_store.list_accounts()
        if not accounts:
            return None
        latest = accounts[-1]
        info = self._multi_store.get_user_info(str(latest.user_id))
        if info is None:
            return None
        return UserInfo(
            user_id=AuthSuperTokensUserId(str(info.user_id)),
            email=info.email,
            display_name=info.display_name,
            user_id_prefix=AuthUserIdPrefix(str(info.user_id_prefix)),
        )

    def get_access_token(self) -> str | None:
        accounts = self._multi_store.list_accounts()
        if not accounts:
            return None
        latest = accounts[-1]
        return self._multi_store.get_access_token(str(latest.user_id))

    def is_signed_in(self) -> bool:
        return self._multi_store.is_any_signed_in()

    def has_signed_in_before(self) -> bool:
        return self._multi_store.has_signed_in_before()

    def clear_session(self) -> None:
        accounts = self._multi_store.list_accounts()
        if accounts:
            self._multi_store.remove_session(str(accounts[-1].user_id))


def _get_server_port(request: Request) -> int:
    return request.app.state.supertokens_server_port


def _get_output_format(request: Request) -> OutputFormat:
    return request.app.state.supertokens_output_format


async def _store_session_from_user(
    session_store: SuperTokensSessionStore,
    user_id: str,
    email: str,
    display_name: str | None = None,
) -> None:
    """Create a SuperTokens session and store the tokens on disk."""
    session = await create_new_session_without_request_response(
        tenant_id=_TENANT_ID,
        recipe_user_id=RecipeUserId(user_id),
    )
    tokens = session.get_all_session_tokens_dangerously()
    session_store.store_session(
        access_token=tokens["accessToken"],
        refresh_token=tokens["refreshToken"] or None,
        user_id=user_id,
        email=email,
        display_name=display_name,
    )


# -- Route handlers (module-level, accessed via request.app.state) --


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
    body = await request.json()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        return _json_response({"status": "FIELD_ERROR", "message": "Email and password are required"}, 400)

    result = await sign_up(
        tenant_id=_TENANT_ID,
        email=email,
        password=password,
    )

    if isinstance(result, EmailAlreadyExistsError):
        return _json_response(
            {"status": "EMAIL_ALREADY_EXISTS", "message": "An account with this email already exists"}
        )

    if isinstance(result, EPSignUpOkResult):
        user = result.user
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
        await _store_session_from_user(session_store, user.id, email)
        await send_email_verification_email(
            tenant_id=_TENANT_ID,
            user_id=user.id,
            recipe_user_id=recipe_user_id,
            email=email,
        )
        return _json_response({"status": "OK", "userId": user.id, "needsEmailVerification": True})

    return _json_response({"status": "ERROR", "message": "Sign-up failed"}, 500)


async def _handle_signin_api(request: Request) -> Response:
    """Handle email/password sign-in (JSON API)."""
    session_store = _get_session_store(request)
    body = await request.json()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        return _json_response({"status": "FIELD_ERROR", "message": "Email and password are required"}, 400)

    result = await sign_in(
        tenant_id=_TENANT_ID,
        email=email,
        password=password,
    )

    if isinstance(result, WrongCredentialsError):
        return _json_response({"status": "WRONG_CREDENTIALS", "message": "Incorrect email or password"})

    if isinstance(result, EPSignInOkResult):
        user = result.user
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
        verified = await is_email_verified(recipe_user_id=recipe_user_id, email=email)
        await _store_session_from_user(session_store, user.id, email)
        needs_verification = not verified
        if needs_verification:
            await send_email_verification_email(
                tenant_id=_TENANT_ID,
                user_id=user.id,
                recipe_user_id=recipe_user_id,
                email=email,
            )
        return _json_response(
            {
                "status": "OK",
                "userId": user.id,
                "needsEmailVerification": needs_verification,
            }
        )

    return _json_response({"status": "ERROR", "message": "Sign-in failed"}, 500)


async def _handle_signout_api(request: Request) -> Response:
    """Handle sign-out."""
    session_store = _get_session_store(request)
    session_store.clear_session()
    return _json_response({"status": "OK"})


def _handle_status_api(request: Request) -> Response:
    """Return current auth status and user info."""
    session_store = _get_session_store(request)
    user_info = session_store.get_user_info()
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
    user_info = session_store.get_user_info()
    if user_info is None:
        return _json_response({"verified": False, "signedIn": False})
    user = get_user(str(user_info.user_id))
    if user is None:
        return _json_response({"verified": False, "signedIn": False})
    recipe_user_id = (
        user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(str(user_info.user_id))
    )
    verified = is_email_verified_sync(recipe_user_id=recipe_user_id, email=user_info.email)
    return _json_response({"verified": verified, "signedIn": True})


def _handle_resend_verification_api(request: Request) -> Response:
    """Resend the email verification email."""
    session_store = _get_session_store(request)
    user_info = session_store.get_user_info()
    if user_info is None:
        return _json_response({"status": "ERROR", "message": "Not signed in"}, 401)
    user = get_user(str(user_info.user_id))
    if user is None:
        return _json_response({"status": "ERROR", "message": "User not found"}, 404)
    recipe_user_id = (
        user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(str(user_info.user_id))
    )
    send_email_verification_email_sync(
        tenant_id=_TENANT_ID,
        user_id=str(user_info.user_id),
        recipe_user_id=recipe_user_id,
        email=user_info.email,
    )
    return _json_response({"status": "OK"})


def _handle_check_email_page(request: Request) -> HTMLResponse:
    """Render the 'check your email' page."""
    session_store = _get_session_store(request)
    user_info = session_store.get_user_info()
    email = user_info.email if user_info else "your email"
    return HTMLResponse(render_check_email_page(email=email))


async def _handle_oauth_redirect(provider_id: str, request: Request) -> Response:
    """Initiate OAuth by opening the system browser."""
    server_port = _get_server_port(request)
    provider = await get_provider(tenant_id=_TENANT_ID, third_party_id=provider_id)
    if provider is None:
        return _json_response({"error": f"Unknown provider: {provider_id}"}, 404)

    callback_url = f"http://127.0.0.1:{server_port}/auth/callback/{provider_id}"
    auth_redirect = await provider.get_authorisation_redirect_url(
        redirect_uri_on_provider_dashboard=callback_url,
        user_context={},
    )

    webbrowser.open(auth_redirect.url_with_query_params)
    return _json_response({"status": "OK", "message": f"Opened {provider_id} sign-in in your browser"})


async def _handle_oauth_callback(provider_id: str, request: Request) -> HTMLResponse:
    """Handle OAuth callback from the provider (opened in system browser)."""
    session_store = _get_session_store(request)
    output_format = _get_output_format(request)
    server_port = _get_server_port(request)
    query_params = dict(request.query_params)
    callback_url = f"http://127.0.0.1:{server_port}/auth/callback/{provider_id}"

    provider = await get_provider(tenant_id=_TENANT_ID, third_party_id=provider_id)
    if provider is None:
        return HTMLResponse(f"<html><body><h1>Unknown provider: {provider_id}</h1></body></html>", status_code=404)

    try:
        oauth_tokens = await provider.exchange_auth_code_for_oauth_tokens(
            redirect_uri_info=RedirectUriInfo(
                redirect_uri_on_provider_dashboard=callback_url,
                redirect_uri_query_params=query_params,
                pkce_code_verifier=None,
            ),
            user_context={},
        )
        user_info = await provider.get_user_info(oauth_tokens=oauth_tokens, user_context={})
    except (ValueError, KeyError, OSError) as e:
        logger.error("OAuth callback failed for {}: {}", provider_id, e)
        return HTMLResponse(
            f"<html><body><h1>Authentication failed</h1><p>{e}</p></body></html>",
            status_code=400,
        )

    if user_info.email is None or user_info.email.id is None:
        return HTMLResponse(
            "<html><body><h1>No email provided by the OAuth provider</h1></body></html>",
            status_code=400,
        )

    email = user_info.email.id
    is_verified = user_info.email.is_verified

    result = await manually_create_or_update_user(
        tenant_id=_TENANT_ID,
        third_party_id=provider_id,
        third_party_user_id=user_info.third_party_user_id,
        email=email,
        is_verified=is_verified,
    )

    if not isinstance(result, ManuallyCreateOrUpdateUserOkResult):
        return HTMLResponse(
            "<html><body><h1>Sign-in failed</h1><p>Could not create account</p></body></html>",
            status_code=400,
        )

    user = result.user
    display_name: str | None = None
    if user_info.raw_user_info_from_provider and user_info.raw_user_info_from_provider.from_user_info_api:
        raw = user_info.raw_user_info_from_provider.from_user_info_api
        display_name = raw.get("name") or raw.get("login") or raw.get("displayName")

    await _store_session_from_user(session_store, user.id, email, display_name=display_name)

    emit_event(
        "auth_success",
        {"message": f"Signed in as {display_name or email}", "email": email},
        output_format,
    )

    return HTMLResponse(render_oauth_close_page(email=email, display_name=display_name))


def _handle_forgot_password_page(request: Request) -> HTMLResponse:
    """Render the forgot password page."""
    return HTMLResponse(render_forgot_password_page())


async def _handle_forgot_password_api(request: Request) -> Response:
    """Send a password reset email."""
    body = await request.json()
    email = body.get("email", "").strip()
    if not email:
        return _json_response({"status": "FIELD_ERROR", "message": "Email is required"}, 400)

    # Look up the user by email. Always return a success-looking response to
    # avoid leaking whether an account exists for the given address.
    _success = _json_response({"status": "OK", "message": "If an account exists, a reset email has been sent"})
    users = await list_users_by_account_info(
        tenant_id=_TENANT_ID,
        account_info=AccountInfoInput(email=email),
    )
    if not users:
        return _success

    user_id = users[0].id
    result = await send_reset_password_email(
        tenant_id=_TENANT_ID,
        user_id=user_id,
        email=email,
    )
    if result == "UNKNOWN_USER_ID_ERROR":
        logger.warning("Failed to send password reset email for user {}", user_id)
    return _success


def _handle_reset_password_page(request: Request, token: str = "") -> HTMLResponse:
    """Render the password reset page."""
    return HTMLResponse(render_reset_password_page(token=token))


async def _handle_reset_password_api(request: Request) -> Response:
    """Process a password reset."""
    body = await request.json()
    token = body.get("token", "")
    new_password = body.get("newPassword", "")

    if not token or not new_password:
        return _json_response({"status": "FIELD_ERROR", "message": "Token and new password are required"}, 400)

    result = await consume_password_reset_token(
        tenant_id=_TENANT_ID,
        token=token,
    )

    if not isinstance(result, ConsumePasswordResetTokenOkResult):
        return _json_response({"status": "INVALID_TOKEN", "message": "Invalid or expired reset token"})

    update_result = await update_email_or_password(
        recipe_user_id=RecipeUserId(result.user_id),
        password=new_password,
    )

    if isinstance(update_result, PasswordPolicyViolationError):
        return _json_response({"status": "FIELD_ERROR", "message": update_result.failure_reason}, 400)

    if not isinstance(update_result, UpdateEmailOrPasswordOkResult):
        return _json_response({"status": "ERROR", "message": "Failed to update password"}, 500)

    return _json_response({"status": "OK", "message": "Password has been reset"})


async def _handle_verify_email(request: Request, token: str = "", tenantId: str = "public") -> HTMLResponse:
    """Handle email verification link click. Verifies the token and shows a result page."""
    if not token:
        return HTMLResponse(render_verify_email_failed_page(), status_code=400)

    try:
        result = await verify_email_using_token(tenant_id=tenantId, token=token)
    except SuperTokensError as exc:
        logger.error("Email verification error: {}", exc)
        return HTMLResponse(render_verify_email_failed_page(), status_code=400)

    if isinstance(result, VerifyEmailUsingTokenOkResult):
        return HTMLResponse(render_verify_email_success_page())

    return HTMLResponse(render_verify_email_failed_page(), status_code=400)


def _handle_settings_page(request: Request) -> HTMLResponse:
    """Render the account settings page."""
    session_store = _get_session_store(request)
    user_info = session_store.get_user_info()
    if user_info is None:
        return HTMLResponse(
            status_code=302,
            headers={"Location": "/auth/login"},
        )

    provider = "email"
    user = get_user(str(user_info.user_id))
    if user and user.login_methods:
        for lm in user.login_methods:
            if lm.third_party is not None:
                provider = lm.third_party.id
                break

    return HTMLResponse(
        render_settings_page(
            email=user_info.email,
            display_name=user_info.display_name,
            user_id=str(user_info.user_id),
            provider=provider,
            user_id_prefix=str(user_info.user_id_prefix),
        )
    )


# -- Router factory --


def create_supertokens_router(
    session_store: SuperTokensSessionStore | MultiAccountSessionStore,
    server_port: int,
    output_format: OutputFormat,
) -> APIRouter:
    """Create a FastAPI router with SuperTokens auth routes.

    Stores config in app.state for access by module-level handlers.
    The actual state is set via a startup event registered on the router.
    """
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
    router.get("/reset-password")(_handle_reset_password_page)
    router.post("/api/reset-password")(_handle_reset_password_api)
    router.get("/verify-email")(_handle_verify_email)
    router.get("/settings")(_handle_settings_page)

    return router
