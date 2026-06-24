from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_settings_page


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
        report_unexpected_errors=False,
        include_error_logs=False,
    )
    assert "user@example.com" in html
    assert "Test User" in html
    assert "google" in html
    assert "a1b2c3d4e5f67890" in html
    assert "Sign out" in html
    assert "Report unexpected errors" in html


def test_render_settings_page_reflects_error_reporting_toggles() -> None:
    # When reporting is on, the report toggle is checked and the include-logs row is visible (not
    # ``hidden``); the include-logs box reflects its own state.
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="email",
        user_id_prefix="a1b2c3d4e5f67890",
        report_unexpected_errors=True,
        include_error_logs=True,
    )
    report_input = html.split('id="report-errors-toggle"')[1].split(">")[0]
    assert "checked" in report_input
    logs_row = html.split('id="include-logs-row"')[1].split(">")[0]
    assert "hidden" not in logs_row
    logs_input = html.split('id="include-logs-toggle"')[1].split(">")[0]
    assert "checked" in logs_input


def test_render_settings_page_hides_logs_row_when_reporting_off() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="email",
        user_id_prefix="a1b2c3d4e5f67890",
        report_unexpected_errors=False,
        include_error_logs=False,
    )
    report_input = html.split('id="report-errors-toggle"')[1].split(">")[0]
    assert "checked" not in report_input
    logs_row = html.split('id="include-logs-row"')[1].split(">")[0]
    assert "hidden" in logs_row


def test_render_settings_page_email_provider_shows_password_link() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="email",
        user_id_prefix="a1b2c3d4e5f67890",
        report_unexpected_errors=False,
        include_error_logs=False,
    )
    assert "Change password" in html


def test_render_settings_page_oauth_provider_hides_password_link() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="github",
        user_id_prefix="a1b2c3d4e5f67890",
        report_unexpected_errors=False,
        include_error_logs=False,
    )
    assert "Change password" not in html
