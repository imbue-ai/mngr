from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_reset_password_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.desktop_client.templates_auth import render_verify_email_failed_page
from imbue.minds.desktop_client.templates_auth import render_verify_email_success_page


def test_render_auth_page_defaults_to_signup() -> None:
    html = render_auth_page(default_to_signup=True, server_port=8080)
    assert "Create account" in html
    assert "signup-form" in html
    # Sign-in tab should be hidden
    assert 'id="signin-tab"' in html


def test_render_auth_page_defaults_to_signin() -> None:
    html = render_auth_page(default_to_signup=False, server_port=8080)
    assert "Sign in" in html
    assert 'id="signin-tab"' in html


def test_render_auth_page_includes_message() -> None:
    html = render_auth_page(message="Please sign in to share", server_port=8080)
    assert "Please sign in to share" in html


def test_render_auth_page_includes_oauth_buttons() -> None:
    html = render_auth_page(server_port=8080)
    assert "Continue with Google" in html
    assert "Continue with GitHub" in html


def test_render_auth_page_includes_toggle_links() -> None:
    html = render_auth_page(server_port=8080)
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


def test_render_reset_password_page_includes_token() -> None:
    html = render_reset_password_page(token="my-reset-token")
    assert "my-reset-token" in html
    assert "New password" in html


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


def test_render_verify_email_success_page() -> None:
    html = render_verify_email_success_page()
    assert "Email verified" in html
    assert "verified successfully" in html
    assert "Go to home" in html


def test_render_verify_email_failed_page() -> None:
    html = render_verify_email_failed_page()
    assert "Verification failed" in html
    assert "invalid or expired" in html
    assert "Go to sign in" in html
