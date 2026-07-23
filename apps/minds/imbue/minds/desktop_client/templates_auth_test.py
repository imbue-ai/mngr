from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.desktop_client.templates_auth import render_signin_modal_page


def test_render_auth_page_defaults_to_signup() -> None:
    html = render_auth_page(default_to_signup=True)
    assert "Create account" in html
    assert "signup-form" in html
    # Sign-in tab should be hidden
    assert 'id="signin-tab"' in html


def test_render_auth_page_defaults_to_signin() -> None:
    html = render_auth_page(default_to_signup=False)
    assert "Sign in" in html
    assert 'id="signin-tab"' in html


def test_render_auth_page_includes_message() -> None:
    html = render_auth_page(message="Please sign in to share")
    assert "Please sign in to share" in html


def test_render_auth_page_with_return_to_shows_back_link() -> None:
    html = render_auth_page(return_to="/create")
    assert "Back to workspace setup" in html
    assert 'href="/create"' in html


def test_render_auth_page_without_return_to_has_no_back_link() -> None:
    html = render_auth_page()
    assert "Back to workspace setup" not in html


def test_render_auth_page_includes_oauth_buttons() -> None:
    html = render_auth_page()
    assert "Continue with Google" in html
    # GitHub OAuth is not enabled in production, so its login button was removed
    # from the auth page (the underlying provider support is left intact).
    assert "Continue with GitHub" not in html


def test_render_auth_page_oauth_buttons_carry_click_spinner() -> None:
    # On click auth.js hides ``.oauth-btn-icon`` and reveals ``.oauth-btn-spinner``
    # in its place, so both wrappers must be present. ``hidden`` must sit on a
    # plain wrapper span (not on ``<Spinner>``, whose own ``inline-block`` would
    # override ``display:none`` and leave the spinner showing at rest), so assert
    # the exact wrapper class.
    html = render_auth_page()
    assert 'class="oauth-btn-spinner hidden"' in html
    assert 'class="oauth-btn-icon"' in html


def test_render_auth_page_includes_toggle_links() -> None:
    html = render_auth_page()
    assert "Already have an account?" in html
    assert "Don&#39;t have an account?" in html or "Don't have an account?" in html


def test_render_signin_modal_page_embeds_auth_form_in_overlay() -> None:
    # The sign-in modal page is loaded into the shared modal WebContentsView (the
    # overlay layer that also hosts the inbox), so it is a full transparent-body
    # page with a dim backdrop wrapping the shared auth form. It loads auth.js and
    # routes post-auth navigations to the create screen via the auth-nav hooks.
    html = render_signin_modal_page()
    assert 'id="signin-modal-backdrop"' in html
    assert "bg-transparent" in html
    assert 'id="signin-form"' in html
    assert 'id="signup-form"' in html
    assert "/_static/auth.js" in html
    assert "MINDS_AUTH_NAV" in html
    assert "/create" in html
    # Close affordance (the shared DialogCloseButton wired to the dismiss hook).
    assert "dismissSigninModal()" in html


def test_render_signin_modal_page_opts_out_of_scrollbar_gutter() -> None:
    # Regression: the modal is an edge-to-edge overlay surface; without the
    # ``no-scrollbar-gutter`` opt-out on the html element, classic scrollbars
    # reserve a 15px gutter that the dim backdrop never paints, leaving an
    # un-dimmed strip at the window's right edge.
    html = render_signin_modal_page()
    assert '<html lang="en" class="no-scrollbar-gutter">' in html


def test_render_signin_modal_page_shows_imbue_cloud_intro() -> None:
    # The intro copy explains why signing in is required (Imbue Cloud needs an
    # account) and that closing the modal falls back to running locally.
    html = render_signin_modal_page()
    assert "run your workspace on Imbue Cloud" in html
    assert "run it directly on your computer" in html


def test_render_signin_modal_page_defaults_to_signup_tab() -> None:
    # Without an explicit mode the sign-up tab leads (the modal's historical
    # default, kept for the create flow and "Add account").
    html = render_signin_modal_page()
    assert 'id="signin-tab" class="hidden"' in html
    assert 'id="signup-tab" class="hidden"' not in html


def test_render_signin_modal_page_can_lead_with_signin_tab() -> None:
    # Callers labeled "Log In" (the welcome splash, the home screen's account
    # launcher) pass default_to_signup=False so the sign-in tab shows first.
    html = render_signin_modal_page(default_to_signup=False)
    assert 'id="signup-tab" class="hidden"' in html
    assert 'id="signin-tab" class="hidden"' not in html


def test_render_signin_modal_page_routes_forgot_password_out_of_the_overlay() -> None:
    # The "Forgot password?" link must not navigate the overlay iframe (the
    # full-page auth flow would render inside the modal and a sign-in
    # completed there would strand the app in the overlay); the modal's
    # inline script intercepts it and routes through MINDS_AUTH_NAV, which
    # lands the page in the content view and dismisses the modal.
    html = render_signin_modal_page()
    assert 'a[href="/auth/forgot-password"]' in html
    assert 'MINDS_AUTH_NAV("/auth/forgot-password")' in html


def test_render_check_email_page() -> None:
    html = render_check_email_page(email="user@example.com")
    assert "user@example.com" in html
    assert "Check your email" in html
    assert "Resend verification email" in html


def test_render_oauth_close_page_with_display_name() -> None:
    html = render_oauth_close_page(email="user@example.com", display_name="Test User")
    assert "Test User" in html
    assert "close this tab" in html


def test_render_oauth_close_page_without_display_name() -> None:
    html = render_oauth_close_page(email="user@example.com")
    assert "user@example.com" in html


def test_render_forgot_password_page() -> None:
    html = render_forgot_password_page()
    assert "Reset password" in html
    assert "Send reset link" in html


def test_render_settings_page() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name="Test User",
        user_id="abc123",
        provider="google",
        user_id_prefix="a1b2c3d4e5f67890",
    )
    assert "user@example.com" in html
    assert "Test User" in html
    assert "google" in html
    assert "a1b2c3d4e5f67890" in html
    assert "Sign out" in html
    # The error-reporting toggles live on the app-level Settings page, not on account settings.
    assert "Report unexpected errors" not in html


def test_render_settings_page_email_provider_shows_password_link() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="email",
        user_id_prefix="a1b2c3d4e5f67890",
    )
    assert "Change password" in html


def test_render_settings_page_oauth_provider_hides_password_link() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="github",
        user_id_prefix="a1b2c3d4e5f67890",
    )
    assert "Change password" not in html
