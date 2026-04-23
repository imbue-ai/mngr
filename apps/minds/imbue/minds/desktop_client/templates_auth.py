"""SuperTokens auth page renderers.

Thin wrappers around the Jinja2 templates under ``templates/auth/``. All
interactivity lives in ``static/auth.js`` and the per-page inline script
blocks that remain (check-email polling, forgot-password POST, etc.)
"""

from typing import Final

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape

from imbue.minds.desktop_client.templates import TEMPLATE_DIR

_JINJA_ENV: Final[Environment] = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(default_for_string=True, default=True),
)


def render_auth_page(
    default_to_signup: bool = True,
    message: str | None = None,
    server_port: int = 0,
) -> str:
    """Render the sign-up / sign-in page."""
    title = "Create account" if default_to_signup else "Sign in"
    return _JINJA_ENV.get_template("auth/signup_signin.html").render(
        title=title,
        default_to_signup=default_to_signup,
        message=message,
        server_port=server_port,
    )


def render_check_email_page(email: str) -> str:
    """Render the 'check your email for verification' page."""
    return _JINJA_ENV.get_template("auth/check_email.html").render(email=email)


def render_oauth_close_page(email: str, display_name: str | None = None) -> str:
    """Render the 'you can close this tab' page after OAuth."""
    return _JINJA_ENV.get_template("auth/oauth_close.html").render(email=email, display_name=display_name)


def render_forgot_password_page() -> str:
    """Render the forgot password page."""
    return _JINJA_ENV.get_template("auth/forgot_password.html").render()


def render_settings_page(
    email: str,
    display_name: str | None,
    user_id: str,
    provider: str,
    user_id_prefix: str,
) -> str:
    """Render the account settings page."""
    return _JINJA_ENV.get_template("auth/settings.html").render(
        email=email,
        display_name=display_name,
        user_id=user_id,
        provider=provider,
        user_id_prefix=user_id_prefix,
    )
