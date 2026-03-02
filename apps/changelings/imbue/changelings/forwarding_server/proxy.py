import re
from typing import Final

from imbue.changelings.primitives import ServerName
from imbue.imbue_common.pure import pure
from imbue.mng.primitives import AgentId

_COOKIE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"(;\s*[Pp]ath\s*=\s*)([^;]*)")

# Matches HTML attributes containing absolute-path URLs (starting with /)
# Handles href, src, action, formaction with both single and double quotes
_ABSOLUTE_PATH_ATTR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""((?:href|src|action|formaction)\s*=\s*)(["'])(/(?!/))""",
    re.IGNORECASE,
)


@pure
def _get_server_prefix(agent_id: AgentId, server_name: ServerName) -> str:
    """Return the URL prefix for a specific server of an agent."""
    return f"/agents/{agent_id}/{server_name}"


@pure
def generate_bootstrap_html(agent_id: AgentId, server_name: ServerName) -> str:
    """Generate the bootstrap HTML that installs the Service Worker on first visit."""
    prefix = _get_server_prefix(agent_id, server_name)
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
    document.cookie = 'sw_installed_{agent_id}_{server_name}=1; path=' + PREFIX;
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
def generate_service_worker_js(agent_id: AgentId, server_name: ServerName) -> str:
    """Generate the Service Worker JavaScript for transparent path rewriting."""
    prefix = _get_server_prefix(agent_id, server_name)
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

  const init = {{
    method: event.request.method,
    headers: event.request.headers,
    mode: event.request.mode,
    credentials: event.request.credentials,
    redirect: 'manual',
  }};

  if (!['GET', 'HEAD'].includes(event.request.method)) {{
    init.body = event.request.body;
    init.duplex = 'half';
  }}

  event.respondWith(fetch(new Request(url.toString(), init)));
}});
"""


@pure
def generate_websocket_shim_js(agent_id: AgentId, server_name: ServerName) -> str:
    """Generate the WebSocket shim script that rewrites WS URLs to include the server prefix."""
    prefix = _get_server_prefix(agent_id, server_name)
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
    agent_id: AgentId,
    server_name: ServerName,
) -> str:
    """Rewrite the Path attribute in a Set-Cookie header to scope under the server prefix."""
    prefix = _get_server_prefix(agent_id, server_name)

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
def rewrite_absolute_paths_in_html(
    html_content: str,
    agent_id: AgentId,
    server_name: ServerName,
) -> str:
    """Rewrite absolute-path URLs in HTML attributes to include the server prefix.

    Handles href, src, action, formaction attributes. Rewrites /foo to /agents/{id}/{server}/foo
    but leaves already-prefixed paths and protocol-relative URLs (//...) unchanged.
    """
    prefix = _get_server_prefix(agent_id, server_name)
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


@pure
def rewrite_proxied_html(
    html_content: str,
    agent_id: AgentId,
    server_name: ServerName,
) -> str:
    """Apply all HTML transformations needed for proxied responses.

    This rewrites absolute-path URLs, injects a <base> tag for relative URL resolution,
    and injects the WebSocket shim script.
    """
    prefix = _get_server_prefix(agent_id, server_name)

    # Rewrite absolute paths in HTML attributes
    rewritten = rewrite_absolute_paths_in_html(
        html_content=html_content,
        agent_id=agent_id,
        server_name=server_name,
    )

    # Build the injection: base tag + WS shim
    base_tag = f'<base href="{prefix}/">'
    shim = generate_websocket_shim_js(agent_id, server_name)
    injection = base_tag + shim

    return _inject_into_head(html_content=rewritten, injection=injection)
