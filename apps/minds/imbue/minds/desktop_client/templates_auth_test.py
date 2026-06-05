import re

from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_settings_page


def _tab_classes(html: str, tab_id: str) -> str:
    """Return the class attribute of the panel div with the given id.

    The signup/signin panels are sibling ``<div id="...-tab" class="...">``
    elements; whichever panel is *not* the default carries the ``hidden``
    class. This lets the tests assert on the active-tab marker rather than
    mere presence (both panels are always present in the DOM).
    """
    match = re.search(rf'<div id="{re.escape(tab_id)}" class="([^"]*)"', html)
    assert match is not None, f"no panel div with id={tab_id!r} in rendered auth page"
    return match.group(1)


def test_render_auth_page_defaults_to_signup() -> None:
    html = render_auth_page(default_to_signup=True)
    assert "Create account" in html
    assert "signup-form" in html
    # Defaulting to signup means the signup panel is visible and the signin
    # panel is the one hidden.
    assert "hidden" not in _tab_classes(html, "signup-tab")
    assert "hidden" in _tab_classes(html, "signin-tab")


def test_render_auth_page_defaults_to_signin() -> None:
    html = render_auth_page(default_to_signup=False)
    assert "Sign in" in html
    # Defaulting to signin flips which panel is hidden.
    assert "hidden" not in _tab_classes(html, "signin-tab")
    assert "hidden" in _tab_classes(html, "signup-tab")


def test_render_auth_page_includes_message() -> None:
    html = render_auth_page(message="Please sign in to share")
    assert "Please sign in to share" in html


def test_render_auth_page_includes_oauth_buttons() -> None:
    html = render_auth_page()
    assert "Continue with Google" in html
    assert "Continue with GitHub" in html


def test_render_auth_page_includes_toggle_links() -> None:
    html = render_auth_page()
    assert "Already have an account?" in html
    assert "Don&#39;t have an account?" in html or "Don't have an account?" in html


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
