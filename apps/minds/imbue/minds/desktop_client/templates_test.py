import pytest

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")


def test_render_landing_page_with_agents_lists_them_as_links() -> None:
    ids = (_AGENT_A, _AGENT_B)
    html = render_landing_page(accessible_agent_ids=ids)
    assert f"/goto/{_AGENT_A}/" in html
    assert f"/goto/{_AGENT_B}/" in html
    assert str(_AGENT_A) in html
    assert str(_AGENT_B) in html


def test_render_landing_page_with_no_agents_shows_empty_state() -> None:
    html = render_landing_page(accessible_agent_ids=())
    assert "No projects yet" in html


def test_render_landing_page_discovering_shows_auto_refresh() -> None:
    html = render_landing_page(accessible_agent_ids=(), is_discovering=True)
    assert "Discovering agents" in html
    assert "reload" in html
    assert "No projects yet" not in html
    assert "/goto/" not in html


def test_render_login_redirect_page_contains_redirect_script() -> None:
    html = render_login_redirect_page(
        one_time_code=OneTimeCode("abc123-secret-82341"),
    )
    assert "window.location.href" in html
    assert "one_time_code=abc123-secret-82341" in html


def test_render_auth_error_page_shows_error_message() -> None:
    html = render_auth_error_page(message="This code has already been used.")
    assert "This code has already been used." in html
    assert "Authentication Failed" in html
    assert "restart the server" in html


def test_agent_id_rejects_invalid_format() -> None:
    with pytest.raises(InvalidRandomIdError):
        AgentId("not-a-valid-agent-id")


def test_agent_id_accepts_valid_format() -> None:
    agent_id = AgentId("agent-00000000000000000000000000000001")
    assert agent_id == "agent-00000000000000000000000000000001"


def test_render_create_form_has_default_values() -> None:
    html = render_create_form()
    assert "selene" in html
    assert "forever-claude-template" in html
    assert "agent_name" in html
    assert "main" in html
    assert "launch_mode" in html


def test_render_create_form_prefills_values() -> None:
    html = render_create_form(git_url="https://custom/repo", agent_name="my-bot", branch="feature/test")
    assert "https://custom/repo" in html
    assert "my-bot" in html
    assert "feature/test" in html


def test_render_create_form_contains_all_launch_modes() -> None:
    html = render_create_form()
    for mode in LaunchMode:
        assert mode.value.lower() in html


def test_render_create_form_selects_local_by_default() -> None:
    html = render_create_form()
    assert 'value="LOCAL" selected' in html


def test_render_create_form_selects_specified_launch_mode() -> None:
    html = render_create_form(launch_mode=LaunchMode.DEV)
    assert 'value="DEV" selected' in html
    assert 'value="LOCAL" selected' not in html


def test_render_login_page_shows_prompt() -> None:
    html = render_login_page()
    assert "login URL" in html.lower() or "Login" in html


def test_render_chrome_page_contains_titlebar() -> None:
    html = render_chrome_page()
    assert "minds-titlebar" in html
    assert "sidebar-toggle" in html
    assert "home-btn" in html
    assert "back-btn" in html
    assert "content-frame" in html


def test_render_chrome_page_hides_window_controls_on_mac() -> None:
    """On macOS, the .minds-wc section is hidden (native traffic lights used instead)."""
    html = render_chrome_page(is_mac=True)
    assert "display: none" in html


def test_render_chrome_page_shows_window_controls_on_non_mac() -> None:
    html = render_chrome_page(is_mac=False)
    assert "min-btn" in html
    assert "max-btn" in html
    assert "close-btn" in html


def test_render_sidebar_page_contains_workspace_list() -> None:
    html = render_sidebar_page()
    assert "sidebar-workspaces" in html
    assert "EventSource" in html
