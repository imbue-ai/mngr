"""SuperTokens auth page renderers.

Thin shells around the mithril auth surfaces (frontend/src/views/AuthPages.ts):
each render function seeds a boot island and mounts the corresponding
component. Shares the ``Catalog`` from :mod:`templates` so both modules see
the same component cache and autoescape configuration.
"""

from imbue.minds.desktop_client.chrome_state import AccountSettingsBootExtras
from imbue.minds.desktop_client.chrome_state import AuthFormBootExtras
from imbue.minds.desktop_client.chrome_state import CheckEmailBootExtras
from imbue.minds.desktop_client.chrome_state import OauthCloseBootExtras
from imbue.minds.desktop_client.templates import CATALOG


def render_auth_page(
    default_to_signup: bool = True,
    message: str | None = None,
    return_to: str | None = None,
) -> str:
    """Render the standalone sign-up / sign-in page.

    ``return_to`` is an optional same-origin path the user came from (e.g.
    ``/create`` when they chose the remote preset without an account). When
    set, the page shows a back link to it, and a successful sign-in lands them
    back there (the component forwards it through ``/post-login``).
    """
    title = "Create account" if default_to_signup else "Sign in"
    extras = AuthFormBootExtras(
        default_to_signup=default_to_signup,
        intro="",
        message=message or "",
        return_to=return_to or "",
        is_modal=False,
    )
    return CATALOG.render("auth.SignupSignin", title=title, boot_state={"auth": extras.to_payload_dict()})


# Copy shown above the sign-in modal's tabs explaining why the user is being
# asked to sign in (the create screen needs an Imbue account for Imbue Cloud).
_SIGNIN_MODAL_CREATE_INTRO: str = (
    "To run your workspace on Imbue Cloud, sign in or create an Imbue account. "
    "You can also close this and run it directly on your computer instead."
)

# Generic copy for sign-ins launched outside the create flow (the home
# screen's account launcher, the Manage Accounts modal's "Add account").
_SIGNIN_MODAL_GENERIC_INTRO: str = "Sign in to enable sharing and run workspaces on Imbue Cloud."


def render_signin_modal_page(return_to: str = "/create", default_to_signup: bool = True) -> str:
    """Render the sign-in modal page served by ``GET /auth/signin-modal``.

    Hosted in the shared modal WebContentsView (or the browser ModalHost
    iframe). Opened from the create screen (a signed-out user pressing
    "Create" with the Imbue Cloud preset), the welcome splash's Sign Up /
    Log In buttons, the home screen's account launcher when signed out, and
    the Manage Accounts modal's "Add account".

    ``return_to`` is where a successful sign-in lands the content view; the
    create flow keeps its dedicated intro copy. ``default_to_signup`` picks
    which AuthForm tab leads on first paint (callers that say "Log In" pass
    False so the sign-in tab shows).
    """
    intro = _SIGNIN_MODAL_CREATE_INTRO if return_to == "/create" else _SIGNIN_MODAL_GENERIC_INTRO
    extras = AuthFormBootExtras(
        default_to_signup=default_to_signup,
        intro=intro,
        message="",
        return_to=return_to,
        is_modal=True,
    )
    return CATALOG.render("pages.SigninModal", boot_state={"auth": extras.to_payload_dict()})


def render_check_email_page(email: str) -> str:
    """Render the 'check your email for verification' page."""
    extras = CheckEmailBootExtras(email=email)
    return CATALOG.render("auth.CheckEmail", boot_state={"check_email": extras.to_payload_dict()})


def render_oauth_close_page(email: str, display_name: str | None = None) -> str:
    """Render the 'you can close this tab' page after OAuth."""
    extras = OauthCloseBootExtras(email=email, display_name=display_name or "")
    return CATALOG.render("auth.OauthClose", boot_state={"oauth_close": extras.to_payload_dict()})


def render_forgot_password_page() -> str:
    """Render the forgot password page (no seeded data)."""
    return CATALOG.render("auth.ForgotPassword", boot_state={"forgot_password": {}})


def render_settings_page(
    email: str,
    display_name: str | None,
    user_id: str,
    provider: str,
    user_id_prefix: str,
) -> str:
    """Render the account settings page.

    The per-machine error-reporting toggles live on the app-level Settings
    page (/settings), not here. ``user_id`` is accepted for call-site
    compatibility but not rendered (only its prefix is shown).
    """
    del user_id
    extras = AccountSettingsBootExtras(
        email=email,
        display_name=display_name or "",
        provider=provider,
        user_id_prefix=user_id_prefix,
    )
    return CATALOG.render("auth.Settings", boot_state={"account_settings": extras.to_payload_dict()})
