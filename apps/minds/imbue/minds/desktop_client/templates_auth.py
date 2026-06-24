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
) -> str:
    """Render the sign-up / sign-in page."""
    title = "Create account" if default_to_signup else "Sign in"
    return CATALOG.render(
        "auth.SignupSignin",
        title=title,
        default_to_signup=default_to_signup,
        message=message,
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
    report_unexpected_errors: bool,
    include_error_logs: bool,
) -> str:
    """Render the account settings page.

    ``report_unexpected_errors`` / ``include_error_logs`` seed the per-machine error-reporting
    toggles (the same settings the first-launch consent screen records).
    """
    return CATALOG.render(
        "auth.Settings",
        email=email,
        display_name=display_name,
        user_id=user_id,
        provider=provider,
        user_id_prefix=user_id_prefix,
        report_unexpected_errors=report_unexpected_errors,
        include_error_logs=include_error_logs,
    )
