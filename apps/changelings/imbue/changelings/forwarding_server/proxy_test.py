from inline_snapshot import snapshot

from imbue.changelings.forwarding_server.proxy import generate_bootstrap_html
from imbue.changelings.forwarding_server.proxy import generate_service_worker_js
from imbue.changelings.forwarding_server.proxy import generate_websocket_shim_js
from imbue.changelings.forwarding_server.proxy import rewrite_absolute_paths_in_html
from imbue.changelings.forwarding_server.proxy import rewrite_cookie_path
from imbue.changelings.forwarding_server.proxy import rewrite_proxied_html
from imbue.changelings.primitives import ServerName
from imbue.mng.primitives import AgentId

_TEST_AGENT: AgentId = AgentId("agent-00000000000000000000000000000001")
_TEST_AGENT_2: AgentId = AgentId("agent-00000000000000000000000000000002")
_TEST_SERVER: ServerName = ServerName("web")
_TEST_SERVER_2: ServerName = ServerName("api")


def test_generate_bootstrap_html_contains_service_worker_registration() -> None:
    html = generate_bootstrap_html(_TEST_AGENT, _TEST_SERVER)
    assert "serviceWorker.register" in html
    assert f"/agents/{_TEST_AGENT}/{_TEST_SERVER}/" in html
    assert "__sw.js" in html


def test_generate_bootstrap_html_sets_sw_cookie() -> None:
    html = generate_bootstrap_html(_TEST_AGENT, _TEST_SERVER)
    assert f"sw_installed_{_TEST_AGENT}_{_TEST_SERVER}" in html


def test_generate_service_worker_js_contains_prefix() -> None:
    js = generate_service_worker_js(_TEST_AGENT, _TEST_SERVER)
    assert f"const PREFIX = '/agents/{_TEST_AGENT}/{_TEST_SERVER}'" in js
    assert "skipWaiting" in js
    assert "clients.claim" in js


def test_generate_service_worker_js_rewrites_fetch_urls() -> None:
    js = generate_service_worker_js(_TEST_AGENT_2, _TEST_SERVER_2)
    assert "url.pathname = PREFIX + url.pathname" in js


def test_generate_websocket_shim_js_contains_prefix() -> None:
    js = generate_websocket_shim_js(_TEST_AGENT, _TEST_SERVER)
    assert f"var PREFIX = '/agents/{_TEST_AGENT}/{_TEST_SERVER}'" in js
    assert "OrigWebSocket" in js


def test_rewrite_cookie_path_with_root_path() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc; Path=/",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/agents/agent-00000000000000000000000000000001/web/")


def test_rewrite_cookie_path_with_subpath() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc; Path=/api",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/agents/agent-00000000000000000000000000000001/web/api")


def test_rewrite_cookie_path_without_path_attribute() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/agents/agent-00000000000000000000000000000001/web/")


def test_rewrite_cookie_path_does_not_double_prefix() -> None:
    result = rewrite_cookie_path(
        set_cookie_header=f"sid=abc; Path=/agents/{_TEST_AGENT}/{_TEST_SERVER}/api",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/agents/agent-00000000000000000000000000000001/web/api")


# -- Absolute path rewriting --


def test_rewrite_absolute_paths_rewrites_href() -> None:
    html = '<a href="/hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<a href="/agents/agent-00000000000000000000000000000001/web/hello.txt">link</a>')


def test_rewrite_absolute_paths_rewrites_src() -> None:
    html = '<img src="/images/logo.png">'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<img src="/agents/agent-00000000000000000000000000000001/web/images/logo.png">')


def test_rewrite_absolute_paths_rewrites_action() -> None:
    html = '<form action="/submit">'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<form action="/agents/agent-00000000000000000000000000000001/web/submit">')


def test_rewrite_absolute_paths_preserves_relative_urls() -> None:
    html = '<a href="hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<a href="hello.txt">link</a>')


def test_rewrite_absolute_paths_preserves_protocol_relative_urls() -> None:
    html = '<a href="//example.com/page">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<a href="//example.com/page">link</a>')


def test_rewrite_absolute_paths_preserves_full_urls() -> None:
    html = '<a href="https://example.com/page">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<a href="https://example.com/page">link</a>')


def test_rewrite_absolute_paths_does_not_double_prefix() -> None:
    html = f'<a href="/agents/{_TEST_AGENT}/{_TEST_SERVER}/hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<a href="/agents/agent-00000000000000000000000000000001/web/hello.txt">link</a>')


def test_rewrite_absolute_paths_handles_single_quotes() -> None:
    html = "<a href='/hello.txt'>link</a>"
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("<a href='/agents/agent-00000000000000000000000000000001/web/hello.txt'>link</a>")


# -- Full proxied HTML rewriting --


def test_rewrite_proxied_html_injects_base_tag_and_shim() -> None:
    html = "<html><head><title>Test</title></head><body></body></html>"
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert f'<base href="/agents/{_TEST_AGENT}/{_TEST_SERVER}/">' in result
    assert "OrigWebSocket" in result
    assert "<title>Test</title>" in result


def test_rewrite_proxied_html_rewrites_absolute_paths() -> None:
    html = '<html><head></head><body><a href="/page">link</a></body></html>'
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert f'href="/agents/{_TEST_AGENT}/{_TEST_SERVER}/page"' in result


def test_rewrite_proxied_html_with_head_attributes() -> None:
    html = '<html><head lang="en"><title>Test</title></head><body></body></html>'
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert f'<head lang="en"><base href="/agents/{_TEST_AGENT}/{_TEST_SERVER}/">' in result


def test_rewrite_proxied_html_without_head_tag() -> None:
    html = "<html><body>Hello</body></html>"
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result.startswith(f'<base href="/agents/{_TEST_AGENT}/{_TEST_SERVER}/">')
    assert "<html><body>Hello</body></html>" in result
