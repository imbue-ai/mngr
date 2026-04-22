from inline_snapshot import snapshot

from imbue.minds.desktop_client.proxy import generate_backend_loading_html
from imbue.minds.desktop_client.proxy import generate_bootstrap_html
from imbue.minds.desktop_client.proxy import generate_service_worker_js
from imbue.minds.desktop_client.proxy import generate_websocket_shim_js
from imbue.minds.desktop_client.proxy import rewrite_absolute_paths_in_html
from imbue.minds.desktop_client.proxy import rewrite_cookie_path
from imbue.minds.desktop_client.proxy import rewrite_location_header
from imbue.minds.desktop_client.proxy import rewrite_proxied_html
from imbue.minds.primitives import ServerName
from imbue.mngr.primitives import AgentId

_TEST_AGENT: AgentId = AgentId("agent-00000000000000000000000000000001")
_TEST_AGENT_2: AgentId = AgentId("agent-00000000000000000000000000000002")
_TEST_SERVER: ServerName = ServerName("web")
_TEST_SERVER_2: ServerName = ServerName("api")


def test_generate_bootstrap_html_contains_service_worker_registration() -> None:
    html = generate_bootstrap_html(_TEST_AGENT, _TEST_SERVER)
    assert "serviceWorker.register" in html
    assert f"/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/" in html
    assert "__sw.js" in html


def test_generate_bootstrap_html_sets_sw_cookie() -> None:
    html = generate_bootstrap_html(_TEST_AGENT, _TEST_SERVER)
    assert f"sw_installed_{_TEST_AGENT}_{_TEST_SERVER}" in html


def test_generate_service_worker_js_contains_prefix() -> None:
    js = generate_service_worker_js(_TEST_AGENT, _TEST_SERVER)
    assert f"const PREFIX = '/forwarding/{_TEST_AGENT}/{_TEST_SERVER}'" in js
    assert "skipWaiting" in js
    assert "clients.claim" in js


def test_generate_service_worker_js_rewrites_fetch_urls() -> None:
    js = generate_service_worker_js(_TEST_AGENT_2, _TEST_SERVER_2)
    assert "url.pathname = PREFIX + url.pathname" in js


def test_generate_service_worker_js_does_not_exclude_auth_routes() -> None:
    js = generate_service_worker_js(_TEST_AGENT, _TEST_SERVER)
    assert "/auth/" not in js


def test_generate_websocket_shim_js_contains_prefix() -> None:
    js = generate_websocket_shim_js(_TEST_AGENT, _TEST_SERVER)
    assert f"var PREFIX = '/forwarding/{_TEST_AGENT}/{_TEST_SERVER}'" in js
    assert "OrigWebSocket" in js


def test_rewrite_cookie_path_with_root_path() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc; Path=/",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/forwarding/agent-00000000000000000000000000000001/web/")


def test_rewrite_cookie_path_with_subpath() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc; Path=/api",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/forwarding/agent-00000000000000000000000000000001/web/api")


def test_rewrite_cookie_path_without_path_attribute() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/forwarding/agent-00000000000000000000000000000001/web/")


def test_rewrite_cookie_path_does_not_double_prefix() -> None:
    result = rewrite_cookie_path(
        set_cookie_header=f"sid=abc; Path=/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/api",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("sid=abc; Path=/forwarding/agent-00000000000000000000000000000001/web/api")


# -- Absolute path rewriting --


def test_rewrite_absolute_paths_rewrites_href() -> None:
    html = '<a href="/hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<a href="/forwarding/agent-00000000000000000000000000000001/web/hello.txt">link</a>')


def test_rewrite_absolute_paths_rewrites_src() -> None:
    html = '<img src="/images/logo.png">'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<img src="/forwarding/agent-00000000000000000000000000000001/web/images/logo.png">')


def test_rewrite_absolute_paths_rewrites_action() -> None:
    html = '<form action="/submit">'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<form action="/forwarding/agent-00000000000000000000000000000001/web/submit">')


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
    html = f'<a href="/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot('<a href="/forwarding/agent-00000000000000000000000000000001/web/hello.txt">link</a>')


def test_rewrite_absolute_paths_handles_single_quotes() -> None:
    html = "<a href='/hello.txt'>link</a>"
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("<a href='/forwarding/agent-00000000000000000000000000000001/web/hello.txt'>link</a>")


# -- Location header rewriting --


def test_rewrite_location_header_with_site_absolute_path() -> None:
    result = rewrite_location_header(
        location_header="/foo",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("/forwarding/agent-00000000000000000000000000000001/web/foo")


def test_rewrite_location_header_with_site_absolute_root() -> None:
    result = rewrite_location_header(
        location_header="/",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("/forwarding/agent-00000000000000000000000000000001/web/")


def test_rewrite_location_header_preserves_query_string() -> None:
    result = rewrite_location_header(
        location_header="/foo?bar=1&baz=2",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("/forwarding/agent-00000000000000000000000000000001/web/foo?bar=1&baz=2")


def test_rewrite_location_header_preserves_fragment() -> None:
    result = rewrite_location_header(
        location_header="/foo#section",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == snapshot("/forwarding/agent-00000000000000000000000000000001/web/foo#section")


def test_rewrite_location_header_does_not_double_prefix() -> None:
    already_prefixed = f"/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/foo"
    result = rewrite_location_header(
        location_header=already_prefixed,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == already_prefixed


def test_rewrite_location_header_does_not_double_prefix_exact_match() -> None:
    prefix = f"/forwarding/{_TEST_AGENT}/{_TEST_SERVER}"
    result = rewrite_location_header(
        location_header=prefix,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == prefix


def test_rewrite_location_header_preserves_relative_path() -> None:
    result = rewrite_location_header(
        location_header="foo",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == "foo"


def test_rewrite_location_header_preserves_dot_relative_path() -> None:
    result = rewrite_location_header(
        location_header="./foo",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == "./foo"


def test_rewrite_location_header_preserves_protocol_relative_url() -> None:
    result = rewrite_location_header(
        location_header="//example.com/page",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == "//example.com/page"


def test_rewrite_location_header_preserves_absolute_url() -> None:
    result = rewrite_location_header(
        location_header="https://example.com/page",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == "https://example.com/page"


def test_rewrite_location_header_preserves_fragment_only() -> None:
    result = rewrite_location_header(
        location_header="#anchor",
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result == "#anchor"


# -- Full proxied HTML rewriting --


def test_rewrite_proxied_html_injects_base_tag_and_shim() -> None:
    html = "<html><head><title>Test</title></head><body></body></html>"
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert f'<base href="/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/">' in result
    assert "OrigWebSocket" in result
    assert "<title>Test</title>" in result


def test_rewrite_proxied_html_rewrites_absolute_paths() -> None:
    html = '<html><head></head><body><a href="/page">link</a></body></html>'
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert f'href="/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/page"' in result


def test_rewrite_proxied_html_with_head_attributes() -> None:
    html = '<html><head lang="en"><title>Test</title></head><body></body></html>'
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert f'<head lang="en"><base href="/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/">' in result


def test_rewrite_proxied_html_without_head_tag() -> None:
    html = "<html><body>Hello</body></html>"
    result = rewrite_proxied_html(
        html_content=html,
        agent_id=_TEST_AGENT,
        server_name=_TEST_SERVER,
    )
    assert result.startswith(f'<base href="/forwarding/{_TEST_AGENT}/{_TEST_SERVER}/">')
    assert "<html><body>Hello</body></html>" in result


def test_generate_backend_loading_html_no_agent_id_has_no_links() -> None:
    html = generate_backend_loading_html()
    assert "Loading..." in html
    assert "location.reload()" in html
    assert "/forwarding/" not in html


def test_generate_backend_loading_html_with_agent_id_includes_convention_links() -> None:
    html = generate_backend_loading_html(agent_id=_TEST_AGENT)
    assert f"/forwarding/{_TEST_AGENT}/terminal/" in html
    assert f"/forwarding/{_TEST_AGENT}/agent/" in html


def test_generate_backend_loading_html_excludes_current_server_from_links() -> None:
    html = generate_backend_loading_html(
        agent_id=_TEST_AGENT,
        current_server=ServerName("terminal"),
    )
    assert f"/forwarding/{_TEST_AGENT}/terminal/" not in html
    assert f"/forwarding/{_TEST_AGENT}/agent/" in html


def test_generate_backend_loading_html_includes_other_servers() -> None:
    html = generate_backend_loading_html(
        agent_id=_TEST_AGENT,
        other_servers=(ServerName("web"),),
    )
    assert f"/forwarding/{_TEST_AGENT}/web/" in html


def test_generate_backend_loading_html_links_use_target_top() -> None:
    html = generate_backend_loading_html(agent_id=_TEST_AGENT)
    assert 'target="_top"' in html
