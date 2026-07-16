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
) -> str:
    """Render the sign-up / sign-in page.

    ``return_to`` is an optional same-origin path the user came from (e.g.
    ``/create`` when they chose the remote preset without an account). When
    set, the page shows a back link to it; the page's JS also forwards it to
    ``/post-login`` so a successful sign-in lands them back there.
    """
    title = "Create account" if default_to_signup else "Sign in"
    return CATALOG.render(
        "auth.SignupSignin",
        title=title,
        default_to_signup=default_to_signup,
        message=message,
        return_to=return_to or "",
    )


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

    Loaded into the desktop client's shared modal WebContentsView so it covers
    the whole window, including the title bar. Opened from the create screen
    (a signed-out user pressing "Create" with the Imbue Cloud preset), from
    the welcome splash's Sign Up / Log In buttons, from the home screen's
    account launcher when signed out, and from the Manage Accounts modal's
    "Add account".

    ``return_to`` is where a successful sign-in lands the content view; the
    create flow keeps its dedicated intro copy. ``default_to_signup`` picks
    which AuthForm tab leads on first paint (callers that say "Log In" pass
    False so the sign-in tab shows).
    """
    intro = _SIGNIN_MODAL_CREATE_INTRO if return_to == "/create" else _SIGNIN_MODAL_GENERIC_INTRO
    return CATALOG.render("pages.SigninModal", intro=intro, return_to=return_to, default_to_signup=default_to_signup)


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
    """Render the account settings page.

    The per-machine error-reporting toggles live on the app-level Settings page (/settings), not here.
    """
    return CATALOG.render(
        "auth.Settings",
        email=email,
        display_name=display_name,
        user_id=user_id,
        provider=provider,
        user_id_prefix=user_id_prefix,
    )
