"""The SuperTokens auth surfaces render client-side (the mithril auth
components in ``frontend/src/views/AuthPages.ts``); these tests assert the
boot island each render function seeds and that the page mounts the right
component. The form/OAuth/verification behavior is covered by the vitest
suite ``frontend/src/views/AuthPages.test.ts``."""

from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.desktop_client.templates_auth import render_signin_modal_page
from imbue.minds.desktop_client.testing import parse_boot_island


def test_render_auth_page_defaults_to_signup() -> None:
    html = render_auth_page(default_to_signup=True)
    island = parse_boot_island(html)
    assert island["auth"]["default_to_signup"] is True
    assert island["auth"]["is_modal"] is False
    assert "MindsUI.mountAuthPage" in html


def test_render_auth_page_defaults_to_signin() -> None:
    html = render_auth_page(default_to_signup=False)
    assert parse_boot_island(html)["auth"]["default_to_signup"] is False


def test_render_auth_page_includes_message() -> None:
    html = render_auth_page(message="Please sign in to share")
    assert parse_boot_island(html)["auth"]["message"] == "Please sign in to share"


def test_render_auth_page_with_return_to_seeds_the_back_link() -> None:
    html = render_auth_page(return_to="/create")
    assert parse_boot_island(html)["auth"]["return_to"] == "/create"


def test_render_auth_page_without_return_to_has_empty_return_to() -> None:
    html = render_auth_page()
    assert parse_boot_island(html)["auth"]["return_to"] == ""


def test_render_signin_modal_page_seeds_the_modal_auth_island() -> None:
    # The sign-in modal mounts the AuthForm's modal variant from its island
    # (backdrop + card + host-adapter dismissal render client-side); the
    # island marks the modal and carries the create-flow intro + return_to.
    html = render_signin_modal_page()
    island = parse_boot_island(html)
    assert island["auth"]["is_modal"] is True
    assert island["auth"]["return_to"] == "/create"
    assert "MindsUI.mountSigninModal" in html


def test_render_signin_modal_page_opts_out_of_scrollbar_gutter() -> None:
    # Regression: the modal is an edge-to-edge overlay surface; without the
    # ``no-scrollbar-gutter`` opt-out on the html element, classic scrollbars
    # reserve a 15px gutter that the dim backdrop never paints.
    html = render_signin_modal_page()
    assert '<html lang="en" class="no-scrollbar-gutter">' in html


def test_render_signin_modal_page_shows_imbue_cloud_intro() -> None:
    # The intro copy explains why signing in is required (Imbue Cloud needs an
    # account) and that closing the modal falls back to running locally.
    island = parse_boot_island(render_signin_modal_page())
    assert "run your workspace on Imbue Cloud" in island["auth"]["intro"]
    assert "run it directly on your computer" in island["auth"]["intro"]


def test_render_signin_modal_page_defaults_to_signup_tab() -> None:
    assert parse_boot_island(render_signin_modal_page())["auth"]["default_to_signup"] is True


def test_render_signin_modal_page_can_lead_with_signin_tab() -> None:
    # Callers labeled "Log In" pass default_to_signup=False so the sign-in tab
    # leads.
    assert parse_boot_island(render_signin_modal_page(default_to_signup=False))["auth"]["default_to_signup"] is False


def test_render_check_email_page() -> None:
    html = render_check_email_page(email="user@example.com")
    assert parse_boot_island(html)["check_email"]["email"] == "user@example.com"
    assert "MindsUI.mountCheckEmail" in html


def test_render_oauth_close_page_with_display_name() -> None:
    html = render_oauth_close_page(email="user@example.com", display_name="Test User")
    island = parse_boot_island(html)
    assert island["oauth_close"] == {"email": "user@example.com", "display_name": "Test User"}


def test_render_oauth_close_page_without_display_name() -> None:
    html = render_oauth_close_page(email="user@example.com")
    assert parse_boot_island(html)["oauth_close"]["email"] == "user@example.com"


def test_render_forgot_password_page() -> None:
    html = render_forgot_password_page()
    assert parse_boot_island(html)["forgot_password"] == {}
    assert "MindsUI.mountForgotPassword" in html


def test_render_settings_page() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name="Test User",
        user_id="abc123",
        provider="google",
        user_id_prefix="a1b2c3d4e5f67890",
    )
    island = parse_boot_island(html)
    assert island["account_settings"] == {
        "email": "user@example.com",
        "display_name": "Test User",
        "provider": "google",
        "user_id_prefix": "a1b2c3d4e5f67890",
    }
    assert "MindsUI.mountAccountSettings" in html


def test_render_settings_page_carries_email_provider() -> None:
    # The email-provider Change password affordance renders client-side off
    # the seeded provider (AuthPages.test.ts covers the conditional link).
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="email",
        user_id_prefix="a1b2c3d4e5f67890",
    )
    assert parse_boot_island(html)["account_settings"]["provider"] == "email"


def test_render_settings_page_carries_oauth_provider() -> None:
    html = render_settings_page(
        email="user@example.com",
        display_name=None,
        user_id="abc123",
        provider="github",
        user_id_prefix="a1b2c3d4e5f67890",
    )
    assert parse_boot_island(html)["account_settings"]["provider"] == "github"
