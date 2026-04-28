import re
from typing import Final
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from imbue.imbue_common.pure import pure
from imbue.minds_workspace_server.primitives import ServiceName

_COOKIE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"(;\s*[Pp]ath\s*=\s*)([^;]*)")

# Matches HTML attributes containing absolute-path URLs (starting with /)
# Handles href, src, action, formaction with both single and double quotes
_ABSOLUTE_PATH_ATTR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""((?:href|src|action|formaction)\s*=\s*)(["'])(/(?!/))""",
    re.IGNORECASE,
)


@pure
def get_service_prefix(service_name: ServiceName) -> str:
    """Return the URL prefix under which a service is mounted (e.g. ``/service/web``)."""
    return f"/service/{service_name}"


@pure
def generate_bootstrap_html(service_name: ServiceName) -> str:
    """Generate the bootstrap HTML that installs the Service Worker on first visit."""
    prefix = get_service_prefix(service_name)
    return f"""<!DOCTYPE html>
<html><head><title>Loading...</title></head>
<body>
<p>Loading...</p>
<script>
const PREFIX = '{prefix}/';
const SW_URL = PREFIX + '__sw.js';

async function boot() {{
  const reg = await navigator.serviceWorker.register(SW_URL, {{ scope: PREFIX }});
  const sw = reg.installing || reg.waiting || reg.active;

  function onActivated() {{
    document.cookie = 'sw_installed_{service_name}=1; path=' + PREFIX;
    location.reload();
  }}

  if (sw.state === 'activated') {{
    onActivated();
    return;
  }}

  sw.addEventListener('statechange', () => {{
    if (sw.state === 'activated') onActivated();
  }});
}}

boot().catch(err => {{
  document.body.textContent = 'Failed to initialize: ' + err.message;
}});
</script>
</body></html>"""


@pure
def generate_service_worker_js(service_name: ServiceName) -> str:
    """Generate the Service Worker JavaScript for transparent path rewriting."""
    prefix = get_service_prefix(service_name)
    return f"""
const PREFIX = '{prefix}';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {{
  const url = new URL(event.request.url);

  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith(PREFIX + '/') || url.pathname === PREFIX) return;

  if (url.pathname.endsWith('__sw.js')) return;

  url.pathname = PREFIX + url.pathname;

  async function forwardRequest() {{
    const init = {{
      method: event.request.method,
      headers: event.request.headers,
      credentials: event.request.credentials,
      redirect: 'manual',
    }};

    if (!['GET', 'HEAD'].includes(event.request.method)) {{
      init.body = await event.request.arrayBuffer();
    }}

    return fetch(url.toString(), init);
  }}

  event.respondWith(forwardRequest());
}});
"""


@pure
def generate_websocket_shim_js(service_name: ServiceName) -> str:
    """Generate the WebSocket shim script that rewrites WS URLs to include the service prefix."""
    prefix = get_service_prefix(service_name)
    return f"""<script>
(function() {{
  var PREFIX = '{prefix}';
  var OrigWebSocket = window.WebSocket;

  window.WebSocket = function(url, protocols) {{
    try {{
      var parsed = new URL(url, location.origin);
      if (parsed.host === location.host) {{
        if (!parsed.pathname.startsWith(PREFIX + '/') && parsed.pathname !== PREFIX) {{
          parsed.pathname = PREFIX + parsed.pathname;
        }}
        url = parsed.toString();
      }}
    }} catch(e) {{}}
    return protocols !== undefined
      ? new OrigWebSocket(url, protocols)
      : new OrigWebSocket(url);
  }};

  window.WebSocket.prototype = OrigWebSocket.prototype;
  window.WebSocket.CONNECTING = OrigWebSocket.CONNECTING;
  window.WebSocket.OPEN = OrigWebSocket.OPEN;
  window.WebSocket.CLOSING = OrigWebSocket.CLOSING;
  window.WebSocket.CLOSED = OrigWebSocket.CLOSED;
}})();
</script>"""


@pure
def rewrite_cookie_path(
    set_cookie_header: str,
    service_name: ServiceName,
) -> str:
    """Rewrite the Path attribute in a Set-Cookie header to scope under the service prefix."""
    prefix = get_service_prefix(service_name)

    match = _COOKIE_PATH_PATTERN.search(set_cookie_header)

    if match:
        original_path = match.group(2).strip()
        if original_path.startswith(prefix):
            return set_cookie_header
        separator = "" if original_path.startswith("/") else "/"
        new_path = prefix + separator + original_path
        return set_cookie_header[: match.start(2)] + new_path + set_cookie_header[match.end(2) :]
    else:
        return set_cookie_header + f"; Path={prefix}/"


@pure
def rewrite_location_header(
    location_header: str,
    service_name: ServiceName,
) -> str:
    """Rewrite a Location header so site-absolute redirects stay within the proxied service prefix.

    Without this, a backend returning ``Location: /foo`` causes the browser to resolve
    the redirect against the document origin, escaping the service prefix. Relative
    paths are left untouched because the browser resolves a relative ``Location``
    against the request URL, which already lives under the service prefix. Absolute
    URLs (with scheme or protocol-relative ``//``) and already-prefixed paths are
    also left unchanged.
    """
    parts = urlsplit(location_header)
    # Absolute URL (e.g., https://example.com/...) or protocol-relative (//host/...).
    # The browser will navigate to the explicit host regardless of our prefix, so leave it.
    if parts.scheme or parts.netloc:
        return location_header
    # Relative path or fragment-only reference -- the browser resolves a relative
    # Location against the request URL, which already lives under the service prefix.
    if not parts.path.startswith("/"):
        return location_header
    prefix = get_service_prefix(service_name)
    if parts.path == prefix or parts.path.startswith(prefix + "/"):
        return location_header
    new_path = prefix + parts.path
    return urlunsplit(("", "", new_path, parts.query, parts.fragment))


@pure
def rewrite_absolute_paths_in_html(
    html_content: str,
    service_name: ServiceName,
) -> str:
    """Rewrite absolute-path URLs in HTML attributes to include the service prefix.

    Handles href, src, action, formaction attributes. Rewrites /foo to /service/{name}/foo
    but leaves already-prefixed paths and protocol-relative URLs (//...) unchanged.
    """
    prefix = get_service_prefix(service_name)
    result_parts: list[str] = []
    last_end = 0

    for match in _ABSOLUTE_PATH_ATTR_PATTERN.finditer(html_content):
        quote = match.group(2)
        path_start = match.group(3)

        # Check full attribute value to avoid double-prefixing
        remaining = html_content[match.start(3) :]
        end_quote_idx = remaining.find(quote, 1)
        full_path = remaining[:end_quote_idx] if end_quote_idx > 0 else remaining
        if full_path.startswith(prefix + "/") or full_path == prefix:
            result_parts.append(html_content[last_end : match.end()])
        else:
            result_parts.append(html_content[last_end : match.start(3)])
            result_parts.append(f"{prefix}{path_start}")
        last_end = match.end(3)

    result_parts.append(html_content[last_end:])
    return "".join(result_parts)


@pure
def _inject_into_head(html_content: str, injection: str) -> str:
    """Inject content after the opening <head> tag."""
    if "<head>" in html_content:
        return html_content.replace("<head>", "<head>" + injection, 1)
    elif "<head " in html_content:
        idx = html_content.index("<head ")
        close_idx = html_content.index(">", idx)
        return html_content[: close_idx + 1] + injection + html_content[close_idx + 1 :]
    else:
        return injection + html_content


_BACKEND_LOADING_RETRY_INTERVAL_MS: Final[int] = 1000


@pure
def generate_backend_loading_html(
    current_service: ServiceName | None = None,
    other_services: tuple[ServiceName, ...] = (),
) -> str:
    """Generate a lightweight loading page that retries the current URL after a short delay.

    Returned when the backend service is not yet available. The page shows a
    "Loading..." message and uses JavaScript to reload the page after 1 second,
    which will either succeed (backend is now up) or return this page again.

    When ``other_services`` is non-empty, the page includes fallback links to
    those services (scoped under ``/service/<name>/`` on the same origin).
    Clicking one before the target service is ready simply shows that
    service's own auto-retrying loading page.
    """
    links_html = ""
    services_to_show: list[ServiceName] = []
    for s in other_services:
        if s != current_service and s not in services_to_show:
            services_to_show.append(s)

    if services_to_show:
        link_items = "".join(
            '<a href="/service/{service}/" target="_top"'
            ' style="color: rgb(100, 149, 237); text-decoration: none;'
            ' margin: 0 8px;">{service}</a>'.format(service=service)
            for service in services_to_show
        )
        links_html = (
            '<div style="position: fixed; bottom: 40px; text-align: center;'
            ' width: 100%; color: rgb(100, 100, 100); font-size: 14px;">'
            "While waiting, you can open: {links}</div>"
        ).format(links=link_items)

    return """<!DOCTYPE html>
<html>
<head>
<title>Loading...</title>
<style>
body {{
  font-family: system-ui, -apple-system, sans-serif;
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100vh;
  margin: 0;
  color: rgb(136, 136, 136);
  background: rgb(26, 26, 26);
}}
</style>
</head>
<body>
<p>Loading...</p>
{links}
<script>
setTimeout(function() {{ location.reload(); }}, {interval});
</script>
</body>
</html>""".format(interval=_BACKEND_LOADING_RETRY_INTERVAL_MS, links=links_html)


@pure
def rewrite_proxied_html(
    html_content: str,
    service_name: ServiceName,
) -> str:
    """Apply all HTML transformations needed for proxied responses.

    This rewrites absolute-path URLs, injects a <base> tag for relative URL resolution,
    and injects the WebSocket shim script.
    """
    prefix = get_service_prefix(service_name)

    # Rewrite absolute paths in HTML attributes
    rewritten = rewrite_absolute_paths_in_html(
        html_content=html_content,
        service_name=service_name,
    )

    # Build the injection: base tag + WS shim
    base_tag = f'<base href="{prefix}/">'
    shim = generate_websocket_shim_js(service_name)
    injection = base_tag + shim

    return _inject_into_head(html_content=rewritten, injection=injection)
