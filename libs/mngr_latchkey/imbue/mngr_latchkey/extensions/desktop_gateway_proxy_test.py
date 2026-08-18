import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Final

_NODE_BINARY: Final[str | None] = shutil.which("node")
_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "desktop_gateway_proxy.mjs"


class _RecordingHandler(BaseHTTPRequestHandler):
    """HTTP handler that records requests on its server and echoes the path."""

    def _handle(self) -> None:
        server = self.server
        assert isinstance(server, _RecordingServer)
        server.received.append(
            (self.command, self.path, {name.lower(): value for name, value in self.headers.items()})
        )
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()


class _RecordingServer(ThreadingHTTPServer):
    """Threading HTTP server carrying requests recorded by its handler."""

    received: list[tuple[str, str, dict[str, str]]]


@contextmanager
def _recording_server() -> Generator[tuple[str, _RecordingServer], None, None]:
    server = _RecordingServer(("127.0.0.1", 0), _RecordingHandler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _node_driver_script() -> str:
    return f"""
import http from 'node:http';
import handler from {json.dumps(_EXTENSION_PATH.as_uri())};
const server = http.createServer((request, response) => {{
  handler(request, response).then((handled) => {{
    if (!handled && !response.headersSent) {{
      response.writeHead(404, {{'Content-Type': 'application/json'}});
      response.end(JSON.stringify({{error: 'not handled'}}));
    }}
  }});
}});
server.listen(0, '127.0.0.1', () => process.stdout.write(`PORT=${{server.address().port}}\\n`));
process.on('SIGTERM', () => server.close(() => process.exit(0)));
"""


@contextmanager
def _node_proxy(
    upstream_url: str | None,
    permissions_override: str | None = "desktop-override-jwt",
) -> Generator[str, None, None]:
    assert _NODE_BINARY is not None
    env = {"PATH": "/usr/bin:/bin"}
    if upstream_url is not None:
        env["LATCHKEY_EXTENSION_DESKTOP_GATEWAY_URL"] = upstream_url
    if permissions_override is not None:
        env["LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PERMISSIONS_OVERRIDE"] = permissions_override
    process = subprocess.Popen(
        [_NODE_BINARY, "--input-type=module", "-e", _node_driver_script()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline().strip()
        assert line.startswith("PORT="), process.stderr.read() if process.stderr is not None else ""
        yield f"http://127.0.0.1:{int(line.removeprefix('PORT='))}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def _request(url: str, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()


def test_proxy_matches_only_desktop_owned_route_families() -> None:
    with _recording_server() as (upstream_url, upstream), _node_proxy(upstream_url) as proxy_url:
        matched_paths = (
            "/permissions",
            "/permissions/self",
            "/permission-requests",
            "/permission-requests/abc",
            "/minds-api-proxy",
            "/minds-api-proxy/api/v1/timezone",
        )
        for path in matched_paths:
            status, _body = _request(f"{proxy_url}{path}")
            assert status == 200
        for path in ("/permission", "/permission-requests-extra", "/gateway/https://example.com"):
            status, _body = _request(f"{proxy_url}{path}")
            assert status == 404
        assert [path for _method, path, _headers in upstream.received] == list(matched_paths)


def test_via_desktop_is_unwrapped_onto_the_desktop_native_outbound_route() -> None:
    """``/via-desktop/<url>`` reaches the desktop as ``/gateway/<url>``.

    The desktop therefore needs no extension of its own: its built-in outbound
    proxy injects the credentials and runs the real permission check.
    """
    with _recording_server() as (upstream_url, upstream), _node_proxy(upstream_url) as proxy_url:
        status, body = _request(f"{proxy_url}/via-desktop/https://a.example.com/v1/x?y=z", method="POST")
    assert status == 200
    assert body == b"/gateway/https://a.example.com/v1/x?y=z"
    method, path, headers = upstream.received[0]
    assert method == "POST"
    assert path == "/gateway/https://a.example.com/v1/x?y=z"
    # The desktop-target JWT still replaces whatever the workspace sent, so the
    # desktop evaluates the host permissions file rather than a caller-chosen one.
    assert headers["x-latchkey-gateway-permissions-override"] == "desktop-override-jwt"


def test_via_desktop_target_url_is_forwarded_byte_for_byte() -> None:
    """The target is sliced off the raw URL, so nothing re-encodes or normalizes it."""
    targets = (
        "https://a.example.com/p%20q?y=a%2Fb",
        "https://a.example.com/x?q=https://b.example.com/y",
        "http://a.example.com/a/../b",
    )
    with _recording_server() as (upstream_url, upstream), _node_proxy(upstream_url) as proxy_url:
        for target in targets:
            status, _body = _request(f"{proxy_url}/via-desktop/{target}")
            assert status == 200
    assert [path for _method, path, _headers in upstream.received] == [f"/gateway/{t}" for t in targets]


def test_via_desktop_rejects_a_target_that_is_not_an_absolute_http_url() -> None:
    """A malformed target fails on the VPS rather than as an opaque 400 from the desktop."""
    with _recording_server() as (upstream_url, upstream), _node_proxy(upstream_url) as proxy_url:
        for path in ("/via-desktop/example.com/x", "/via-desktop//example.com", "/via-desktop/ftp://a.example.com"):
            status, body = _request(f"{proxy_url}{path}")
            assert status == 400
            assert "absolute http:// or https:// URL" in json.loads(body)["error"]
    assert upstream.received == []


def test_bare_via_desktop_path_is_claimed_but_carries_no_target() -> None:
    """``/via-desktop`` with nothing after it is a caller error, not a pass-through."""
    with _recording_server() as (upstream_url, upstream), _node_proxy(upstream_url) as proxy_url:
        status, _body = _request(f"{proxy_url}/via-desktop")
    assert status == 400
    assert upstream.received == []


def test_proxy_preserves_path_query_method_and_gateway_headers() -> None:
    with _recording_server() as (upstream_url, upstream), _node_proxy(upstream_url) as proxy_url:
        status, body = _request(
            f"{proxy_url}/permission-requests/approve/abc?follow=true&x=1",
            method="POST",
            headers={
                "X-Latchkey-Gateway-Password": "shared-password",
                "X-Latchkey-Gateway-Permissions-Override": "override-jwt",
                "Authorization": "Bearer original",
            },
        )
        assert status == 200
        assert body == b"/permission-requests/approve/abc?follow=true&x=1"
        method, path, headers = upstream.received[0]
        assert method == "POST"
        assert path == "/permission-requests/approve/abc?follow=true&x=1"
        assert headers["x-latchkey-gateway-password"] == "shared-password"
        assert headers["x-latchkey-gateway-permissions-override"] == "desktop-override-jwt"
        assert headers["authorization"] == "Bearer original"


def test_proxy_returns_503_when_desktop_gateway_url_is_unconfigured() -> None:
    with _node_proxy(None) as proxy_url:
        status, body = _request(f"{proxy_url}/permissions/self")
    assert status == 503
    assert "LATCHKEY_EXTENSION_DESKTOP_GATEWAY_URL" in json.loads(body)["error"]


def test_proxy_returns_503_when_desktop_permissions_override_is_unconfigured() -> None:
    with (
        _recording_server() as (upstream_url, upstream),
        _node_proxy(
            upstream_url,
            permissions_override=None,
        ) as proxy_url,
    ):
        status, body = _request(f"{proxy_url}/permissions/self")
    assert status == 503
    assert "LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PERMISSIONS_OVERRIDE" in json.loads(body)["error"]
    assert upstream.received == []


def test_proxy_returns_clear_502_when_desktop_gateway_is_unreachable() -> None:
    with _recording_server() as (upstream_url, _upstream):
        pass
    with _node_proxy(upstream_url) as proxy_url:
        status, body = _request(f"{proxy_url}/minds-api-proxy/api/schema")
    assert status == 502
    assert "Desktop latchkey gateway is unreachable" in json.loads(body)["error"]
