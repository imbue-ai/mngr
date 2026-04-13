"""SuperTokens authentication routes for the minds desktop client.

Provides routes for email/password sign-up/sign-in, OAuth (Google/GitHub),
email verification, password reset, and session status. All routes use
plain HTML + vanilla JS -- no SuperTokens frontend SDK.
"""

import webbrowser

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from supertokens_python.recipe.emailpassword.interfaces import ConsumePasswordResetTokenOkResult
from supertokens_python.recipe.emailpassword.interfaces import CreateResetPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailpassword.syncio import consume_password_reset_token
from supertokens_python.recipe.emailpassword.syncio import create_reset_password_token
from supertokens_python.recipe.emailpassword.syncio import sign_in
from supertokens_python.recipe.emailpassword.syncio import sign_up
from supertokens_python.recipe.emailpassword.syncio import update_email_or_password
from supertokens_python.recipe.emailverification.syncio import is_email_verified
from supertokens_python.recipe.emailverification.syncio import send_email_verification_email
from supertokens_python.recipe.session.syncio import create_new_session_without_request_response
from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult
from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
from supertokens_python.recipe.thirdparty.syncio import get_provider
from supertokens_python.recipe.thirdparty.syncio import manually_create_or_update_user
from supertokens_python.syncio import get_user
from supertokens_python.types import RecipeUserId

from imbue.minds.desktop_client.supertokens_auth import SuperTokensSessionStore
from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_reset_password_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event

_TENANT_ID = "public"


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    import json

    return Response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _get_session_store(request: Request) -> SuperTokensSessionStore:
    return request.app.state.supertokens_session_store


def _get_server_port(request: Request) -> int:
    return request.app.state.supertokens_server_port


def _get_output_format(request: Request) -> OutputFormat:
    return request.app.state.supertokens_output_format


def _store_session_from_user(
    session_store: SuperTokensSessionStore,
    user_id: str,
    email: str,
    display_name: str | None = None,
) -> None:
    """Create a SuperTokens session and store the tokens on disk."""
    session = create_new_session_without_request_response(
        tenant_id=_TENANT_ID,
        recipe_user_id=RecipeUserId(user_id),
    )
    tokens = session.get_all_session_tokens_dangerously()
    session_store.store_session(
        access_token=tokens["accessToken"],
        refresh_token=tokens["refreshToken"] or "",
        user_id=user_id,
        email=email,
        display_name=display_name,
    )


# -- Route handlers (module-level, accessed via request.app.state) --


def _handle_auth_page(request: Request, message: str | None = None) -> HTMLResponse:
    """Render the sign-up or sign-in page."""
    session_store = _get_session_store(request)
    default_to_signup = not session_store.has_signed_in_before()
    return HTMLResponse(render_auth_page(
        default_to_signup=default_to_signup,
        message=message,
        server_port=_get_server_port(request),
    ))


async def _handle_signup_api(request: Request) -> Response:
    """Handle email/password sign-up (JSON API)."""
    session_store = _get_session_store(request)
    body = await request.json()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        return _json_response({"status": "FIELD_ERROR", "message": "Email and password are required"}, 400)

    result = sign_up(
        tenant_id=_TENANT_ID,
        email=email,
        password=password,
    )

    if isinstance(result, EmailAlreadyExistsError):
        return _json_response({"status": "EMAIL_ALREADY_EXISTS", "message": "An account with this email already exists"})

    if isinstance(result, EPSignUpOkResult):
        user = result.user
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
        _store_session_from_user(session_store, user.id, email)
        send_email_verification_email(
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

    result = sign_in(
        tenant_id=_TENANT_ID,
        email=email,
        password=password,
    )

    if isinstance(result, WrongCredentialsError):
        return _json_response({"status": "WRONG_CREDENTIALS", "message": "Incorrect email or password"})

    if isinstance(result, EPSignInOkResult):
        user = result.user
        recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
        verified = is_email_verified(recipe_user_id=recipe_user_id, email=email)
        _store_session_from_user(session_store, user.id, email)
        needs_verification = not verified
        if needs_verification:
            send_email_verification_email(
                tenant_id=_TENANT_ID,
                user_id=user.id,
                recipe_user_id=recipe_user_id,
                email=email,
            )
        return _json_response({
            "status": "OK",
            "userId": user.id,
            "needsEmailVerification": needs_verification,
        })

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
    return _json_response({
        "signedIn": True,
        "userId": str(user_info.user_id),
        "email": user_info.email,
        "displayName": user_info.display_name,
        "userIdPrefix": str(user_info.user_id_prefix),
    })


def _handle_email_verified_api(request: Request) -> Response:
    """Check if the current user's email is verified."""
    session_store = _get_session_store(request)
    user_info = session_store.get_user_info()
    if user_info is None:
        return _json_response({"verified": False, "signedIn": False})
    user = get_user(str(user_info.user_id))
    if user is None:
        return _json_response({"verified": False, "signedIn": False})
    recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(str(user_info.user_id))
    verified = is_email_verified(recipe_user_id=recipe_user_id, email=user_info.email)
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
    recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(str(user_info.user_id))
    send_email_verification_email(
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


def _handle_oauth_redirect(provider_id: str, request: Request) -> Response:
    """Initiate OAuth by opening the system browser."""
    server_port = _get_server_port(request)
    provider = get_provider(tenant_id=_TENANT_ID, third_party_id=provider_id)
    if provider is None:
        return _json_response({"error": f"Unknown provider: {provider_id}"}, 404)

    callback_url = f"http://127.0.0.1:{server_port}/auth/callback/{provider_id}"
    auth_redirect = provider.get_authorisation_redirect_url(
        redirect_uri_on_provider_dashboard=callback_url,
        user_context={},
    )

    webbrowser.open(auth_redirect.url_with_query_params)
    return _json_response({"status": "OK", "message": f"Opened {provider_id} sign-in in your browser"})


def _handle_oauth_callback(provider_id: str, request: Request) -> HTMLResponse:
    """Handle OAuth callback from the provider (opened in system browser)."""
    session_store = _get_session_store(request)
    output_format = _get_output_format(request)
    server_port = _get_server_port(request)
    query_params = dict(request.query_params)
    callback_url = f"http://127.0.0.1:{server_port}/auth/callback/{provider_id}"

    provider = get_provider(tenant_id=_TENANT_ID, third_party_id=provider_id)
    if provider is None:
        return HTMLResponse(f"<html><body><h1>Unknown provider: {provider_id}</h1></body></html>", status_code=404)

    try:
        oauth_tokens = provider.exchange_auth_code_for_oauth_tokens(
            redirect_uri_info=RedirectUriInfo(
                redirect_uri_on_provider_dashboard=callback_url,
                redirect_uri_query_params=query_params,
                pkce_code_verifier=None,
            ),
            user_context={},
        )
        user_info = provider.get_user_info(oauth_tokens=oauth_tokens, user_context={})
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

    result = manually_create_or_update_user(
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

    _store_session_from_user(session_store, user.id, email, display_name=display_name)

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

    # Don't reveal whether the email exists
    create_reset_password_token(
        tenant_id=_TENANT_ID,
        user_id="",
        email=email,
    )
    return _json_response({"status": "OK", "message": "If an account exists, a reset email has been sent"})


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

    result = consume_password_reset_token(
        tenant_id=_TENANT_ID,
        token=token,
    )

    if not isinstance(result, ConsumePasswordResetTokenOkResult):
        return _json_response({"status": "INVALID_TOKEN", "message": "Invalid or expired reset token"})

    update_email_or_password(
        recipe_user_id=RecipeUserId(result.user_id),
        password=new_password,
    )

    return _json_response({"status": "OK", "message": "Password has been reset"})


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

    return HTMLResponse(render_settings_page(
        email=user_info.email,
        display_name=user_info.display_name,
        user_id=str(user_info.user_id),
        provider=provider,
        user_id_prefix=str(user_info.user_id_prefix),
    ))


# -- Router factory --


def create_supertokens_router(
    session_store: SuperTokensSessionStore,
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
    router.get("/settings")(_handle_settings_page)

    return router
