import pytest

from imbue.changelings.forwarding_server.templates import render_agent_servers_page
from imbue.changelings.forwarding_server.templates import render_auth_error_page
from imbue.changelings.forwarding_server.templates import render_landing_page
from imbue.changelings.forwarding_server.templates import render_login_redirect_page
from imbue.changelings.primitives import OneTimeCode
from imbue.changelings.primitives import ServerName
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.mng.primitives import AgentId

_AGENT_A: AgentId = AgentId("agent-00000000000000000000000000000001")
_AGENT_B: AgentId = AgentId("agent-00000000000000000000000000000002")


def test_render_landing_page_with_agents_lists_them_as_links() -> None:
    ids = (_AGENT_A, _AGENT_B)
    html = render_landing_page(accessible_agent_ids=ids)
    assert f"/agents/{_AGENT_A}/" in html
    assert f"/agents/{_AGENT_B}/" in html
    assert str(_AGENT_A) in html
    assert str(_AGENT_B) in html


def test_render_landing_page_with_no_agents_shows_empty_state() -> None:
    html = render_landing_page(accessible_agent_ids=())
    assert "No changelings are accessible" in html
    assert "/agents/" not in html


def test_render_login_redirect_page_contains_redirect_script() -> None:
    html = render_login_redirect_page(
        agent_id=_AGENT_A,
        one_time_code=OneTimeCode("abc123-secret-82341"),
    )
    assert "window.location.href" in html
    assert f"agent_id={_AGENT_A}" in html
    assert "one_time_code=abc123-secret-82341" in html


def test_render_auth_error_page_shows_error_message() -> None:
    html = render_auth_error_page(message="This code has already been used.")
    assert "This code has already been used." in html
    assert "Authentication Failed" in html
    assert "generate a new login URL" in html


def test_agent_id_rejects_invalid_format() -> None:
    with pytest.raises(InvalidRandomIdError):
        AgentId("not-a-valid-agent-id")


def test_agent_id_accepts_valid_format() -> None:
    agent_id = AgentId("agent-00000000000000000000000000000001")
    assert agent_id == "agent-00000000000000000000000000000001"


# -- Agent servers page tests --


def test_render_agent_servers_page_with_servers_lists_them_as_links() -> None:
    server_names = (ServerName("api"), ServerName("web"))
    html = render_agent_servers_page(agent_id=_AGENT_A, server_names=server_names)
    assert f"/agents/{_AGENT_A}/api/" in html
    assert f"/agents/{_AGENT_A}/web/" in html
    assert "api" in html
    assert "web" in html
    assert str(_AGENT_A) in html


def test_render_agent_servers_page_with_no_servers_shows_empty_state() -> None:
    html = render_agent_servers_page(agent_id=_AGENT_A, server_names=())
    assert "No servers are currently running" in html
    assert str(_AGENT_A) in html


def test_render_agent_servers_page_has_back_link() -> None:
    html = render_agent_servers_page(agent_id=_AGENT_A, server_names=())
    assert 'href="/"' in html
    assert "Back to all changelings" in html
