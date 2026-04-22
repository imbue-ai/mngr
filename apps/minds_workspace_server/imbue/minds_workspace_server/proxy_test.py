from inline_snapshot import snapshot

from imbue.minds_workspace_server.primitives import ServiceName
from imbue.minds_workspace_server.proxy import generate_backend_loading_html
from imbue.minds_workspace_server.proxy import generate_bootstrap_html
from imbue.minds_workspace_server.proxy import generate_service_worker_js
from imbue.minds_workspace_server.proxy import generate_websocket_shim_js
from imbue.minds_workspace_server.proxy import rewrite_absolute_paths_in_html
from imbue.minds_workspace_server.proxy import rewrite_cookie_path
from imbue.minds_workspace_server.proxy import rewrite_location_header
from imbue.minds_workspace_server.proxy import rewrite_proxied_html

_TEST_SERVICE: ServiceName = ServiceName("web")
_TEST_SERVICE_2: ServiceName = ServiceName("api")


def test_generate_bootstrap_html_contains_service_worker_registration() -> None:
    html = generate_bootstrap_html(_TEST_SERVICE)
    assert "serviceWorker.register" in html
    assert f"/service/{_TEST_SERVICE}/" in html
    assert "__sw.js" in html


def test_generate_bootstrap_html_sets_sw_cookie() -> None:
    html = generate_bootstrap_html(_TEST_SERVICE)
    assert f"sw_installed_{_TEST_SERVICE}" in html


def test_generate_service_worker_js_contains_prefix() -> None:
    js = generate_service_worker_js(_TEST_SERVICE)
    assert f"const PREFIX = '/service/{_TEST_SERVICE}'" in js
    assert "skipWaiting" in js
    assert "clients.claim" in js


def test_generate_service_worker_js_rewrites_fetch_urls() -> None:
    js = generate_service_worker_js(_TEST_SERVICE_2)
    assert "url.pathname = PREFIX + url.pathname" in js


def test_generate_service_worker_js_does_not_exclude_auth_routes() -> None:
    js = generate_service_worker_js(_TEST_SERVICE)
    assert "/auth/" not in js


def test_generate_websocket_shim_js_contains_prefix() -> None:
    js = generate_websocket_shim_js(_TEST_SERVICE)
    assert f"var PREFIX = '/service/{_TEST_SERVICE}'" in js
    assert "OrigWebSocket" in js


def test_rewrite_cookie_path_with_root_path() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc; Path=/",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("sid=abc; Path=/service/web/")


def test_rewrite_cookie_path_with_subpath() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc; Path=/api",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("sid=abc; Path=/service/web/api")


def test_rewrite_cookie_path_without_path_attribute() -> None:
    result = rewrite_cookie_path(
        set_cookie_header="sid=abc",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("sid=abc; Path=/service/web/")


def test_rewrite_cookie_path_does_not_double_prefix() -> None:
    result = rewrite_cookie_path(
        set_cookie_header=f"sid=abc; Path=/service/{_TEST_SERVICE}/api",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("sid=abc; Path=/service/web/api")


# -- Absolute path rewriting --


def test_rewrite_absolute_paths_rewrites_href() -> None:
    html = '<a href="/hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot('<a href="/service/web/hello.txt">link</a>')


def test_rewrite_absolute_paths_rewrites_src() -> None:
    html = '<img src="/images/logo.png">'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot('<img src="/service/web/images/logo.png">')


def test_rewrite_absolute_paths_rewrites_action() -> None:
    html = '<form action="/submit">'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot('<form action="/service/web/submit">')


def test_rewrite_absolute_paths_preserves_relative_urls() -> None:
    html = '<a href="hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot('<a href="hello.txt">link</a>')


def test_rewrite_absolute_paths_preserves_protocol_relative_urls() -> None:
    html = '<a href="//example.com/page">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot('<a href="//example.com/page">link</a>')


def test_rewrite_absolute_paths_preserves_full_urls() -> None:
    html = '<a href="https://example.com/page">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot('<a href="https://example.com/page">link</a>')


def test_rewrite_absolute_paths_does_not_double_prefix() -> None:
    html = f'<a href="/service/{_TEST_SERVICE}/hello.txt">link</a>'
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot('<a href="/service/web/hello.txt">link</a>')


def test_rewrite_absolute_paths_handles_single_quotes() -> None:
    html = "<a href='/hello.txt'>link</a>"
    result = rewrite_absolute_paths_in_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("<a href='/service/web/hello.txt'>link</a>")


# -- Location header rewriting --


def test_rewrite_location_header_with_site_absolute_path() -> None:
    result = rewrite_location_header(
        location_header="/foo",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("/service/web/foo")


def test_rewrite_location_header_with_site_absolute_root() -> None:
    result = rewrite_location_header(
        location_header="/",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("/service/web/")


def test_rewrite_location_header_preserves_query_string() -> None:
    result = rewrite_location_header(
        location_header="/foo?bar=1&baz=2",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("/service/web/foo?bar=1&baz=2")


def test_rewrite_location_header_preserves_fragment() -> None:
    result = rewrite_location_header(
        location_header="/foo#section",
        service_name=_TEST_SERVICE,
    )
    assert result == snapshot("/service/web/foo#section")


def test_rewrite_location_header_does_not_double_prefix() -> None:
    already_prefixed = f"/service/{_TEST_SERVICE}/foo"
    result = rewrite_location_header(
        location_header=already_prefixed,
        service_name=_TEST_SERVICE,
    )
    assert result == already_prefixed


def test_rewrite_location_header_does_not_double_prefix_exact_match() -> None:
    prefix = f"/service/{_TEST_SERVICE}"
    result = rewrite_location_header(
        location_header=prefix,
        service_name=_TEST_SERVICE,
    )
    assert result == prefix


def test_rewrite_location_header_preserves_relative_path() -> None:
    result = rewrite_location_header(
        location_header="foo",
        service_name=_TEST_SERVICE,
    )
    assert result == "foo"


def test_rewrite_location_header_preserves_dot_relative_path() -> None:
    result = rewrite_location_header(
        location_header="./foo",
        service_name=_TEST_SERVICE,
    )
    assert result == "./foo"


def test_rewrite_location_header_preserves_protocol_relative_url() -> None:
    result = rewrite_location_header(
        location_header="//example.com/page",
        service_name=_TEST_SERVICE,
    )
    assert result == "//example.com/page"


def test_rewrite_location_header_preserves_absolute_url() -> None:
    result = rewrite_location_header(
        location_header="https://example.com/page",
        service_name=_TEST_SERVICE,
    )
    assert result == "https://example.com/page"


def test_rewrite_location_header_preserves_fragment_only() -> None:
    result = rewrite_location_header(
        location_header="#anchor",
        service_name=_TEST_SERVICE,
    )
    assert result == "#anchor"


# -- Full proxied HTML rewriting --


def test_rewrite_proxied_html_injects_base_tag_and_shim() -> None:
    html = "<html><head><title>Test</title></head><body></body></html>"
    result = rewrite_proxied_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert f'<base href="/service/{_TEST_SERVICE}/">' in result
    assert "OrigWebSocket" in result
    assert "<title>Test</title>" in result


def test_rewrite_proxied_html_rewrites_absolute_paths() -> None:
    html = '<html><head></head><body><a href="/page">link</a></body></html>'
    result = rewrite_proxied_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert f'href="/service/{_TEST_SERVICE}/page"' in result


def test_rewrite_proxied_html_with_head_attributes() -> None:
    html = '<html><head lang="en"><title>Test</title></head><body></body></html>'
    result = rewrite_proxied_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert f'<head lang="en"><base href="/service/{_TEST_SERVICE}/">' in result


def test_rewrite_proxied_html_without_head_tag() -> None:
    html = "<html><body>Hello</body></html>"
    result = rewrite_proxied_html(
        html_content=html,
        service_name=_TEST_SERVICE,
    )
    assert result.startswith(f'<base href="/service/{_TEST_SERVICE}/">')
    assert "<html><body>Hello</body></html>" in result


def test_generate_backend_loading_html_with_no_services_has_no_links() -> None:
    html = generate_backend_loading_html()
    assert "Loading..." in html
    assert "location.reload()" in html
    assert "/service/" not in html


def test_generate_backend_loading_html_includes_other_services() -> None:
    html = generate_backend_loading_html(
        other_services=(ServiceName("terminal"), ServiceName("web")),
    )
    assert "/service/terminal/" in html
    assert "/service/web/" in html


def test_generate_backend_loading_html_excludes_current_service_from_links() -> None:
    html = generate_backend_loading_html(
        current_service=ServiceName("terminal"),
        other_services=(ServiceName("terminal"), ServiceName("web")),
    )
    assert "/service/terminal/" not in html
    assert "/service/web/" in html


def test_generate_backend_loading_html_links_use_target_top() -> None:
    html = generate_backend_loading_html(
        other_services=(ServiceName("terminal"),),
    )
    assert 'target="_top"' in html
