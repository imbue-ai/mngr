"""SuperTokens auth page renderers.

Thin wrappers around the JinjaX page components under ``templates/auth/``.
All interactivity lives in ``static/auth.js`` and the per-page inline
script blocks that remain (check-email polling, forgot-password POST,
etc.)

Shares the ``Catalog`` from :mod:`templates` so both modules see the same
component cache and autoescape configuration.
"""

from imbue.minds.desktop_client.templates import CATALOG


def render_auth_page(
    default_to_signup: bool = True,
    message: str | None = None,
    return_to: str | None = None,
    back_to: str | None = None,
) -> str:
    """Render the sign-up / sign-in page.

    ``return_to`` is an optional same-origin path the user came from (e.g.
    ``/create/resume`` when they chose the remote preset without an account).
    When set, the page's JS forwards it to ``/post-login`` so a successful
    sign-in lands them there.

    ``back_to`` is the optional same-origin path the page's back link points
    at. It is kept distinct from ``return_to`` so the back link can return the
    user to the picker (``/create``, to switch to the local preset) while a
    successful sign-in resumes creation (``/create/resume``). It defaults to
    ``return_to`` when omitted, so callers that only pass ``return_to`` get a
    back link to that same path.
    """
    title = "Create account" if default_to_signup else "Sign in"
    effective_back_to = back_to if back_to is not None else return_to
    return CATALOG.render(
        "auth.SignupSignin",
        title=title,
        default_to_signup=default_to_signup,
        message=message,
        return_to=return_to or "",
        back_to=effective_back_to or "",
    )


def render_check_email_page(email: str) -> str:
    """Render the 'check your email for verification' page."""
    return CATALOG.render("auth.CheckEmail", email=email)


def render_oauth_close_page(email: str, display_name: str | None = None) -> str:
    """Render the 'you can close this tab' page after OAuth."""
    return CATALOG.render("auth.OauthClose", email=email, display_name=display_name)


def render_forgot_password_page() -> str:
    """Render the forgot password page."""
    return CATALOG.render("auth.ForgotPassword")


def render_settings_page(
    email: str,
    display_name: str | None,
    user_id: str,
    provider: str,
    user_id_prefix: str,
) -> str:
    """Render the account settings page."""
    return CATALOG.render(
        "auth.Settings",
        email=email,
        display_name=display_name,
        user_id=user_id,
        provider=provider,
        user_id_prefix=user_id_prefix,
    )
