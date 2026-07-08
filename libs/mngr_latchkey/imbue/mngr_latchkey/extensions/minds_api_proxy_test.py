"""End-to-end tests for the ``minds_api_proxy`` gateway extension.

The extension is a Node ESM module. We can't import it from Python, so
this file drives it from the outside:

1. Spin up a tiny in-process Python HTTP server that plays the role of
   the upstream Minds API and records the requests it receives.
2. Spawn a Node child process that loads the extension's default
   export and mounts it on a Node HTTP server (the script we feed Node
   over stdin handles the wiring).
3. Hit the Node server with ``urllib`` and assert on both the response
   the proxy returned and the request shape the upstream Minds API
   observed.

Node is a hard runtime requirement for the ``latchkey gateway``
subprocess we ship alongside the extension, and is installed in the
shared mngr image, so these tests run on offload like any other test.
Each Node spawn asserts the binary is present, so a missing Node fails
loudly rather than skipping silently.
"""

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Final

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# Resolved once at import time; the spawning fixtures assert it is not None
# (Node ships in the shared mngr image, so absence is a hard failure).
_NODE_BINARY: Final[str | None] = shutil.which("node")

_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "minds_api_proxy.mjs"

# Ample window for Node's first-time module load + socket bind on slow CI;
# the actual handshake completes in milliseconds.
_NODE_READY_TIMEOUT_SECONDS: Final[float] = 15.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02


class _RecordedRequest(FrozenModel):
    """Snapshot of one HTTP request that reached the fake upstream."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    # Bytes are not pydantic-native, but they validate cleanly as
    # ``bytes`` -- no need to relax the model config.


class _StagedResponse(MutableModel):
    """Response the fake Minds API hands back on its next request."""

    status: int = Field(default=200, description="HTTP status code to return")
    headers: dict[str, str] = Field(default_factory=dict, description="Extra headers to include on the response")
    body: bytes = Field(default=b"", description="Response body bytes")


class _FakeMindsApiState(MutableModel):
    """Shared mutable state between the fake-server handler and the test.

    The handler hands every request it sees over to ``received`` and
    reads the response shape from ``next_response``. Tests can stage a
    different response per call by reassigning ``next_response`` before
    making the request.
    """

    received: list[_RecordedRequest]
    next_response: _StagedResponse


def _make_fake_minds_api(state: _FakeMindsApiState) -> ThreadingHTTPServer:
    """Build a ``ThreadingHTTPServer`` that records requests and replies from ``state``."""

    class _Handler(BaseHTTPRequestHandler):
        # We deliberately do not override ``log_message`` to silence
        # per-request stderr noise: the project type checker rejects
        # any signature whose first param name is not the base's
        # literal ``format`` (since that name is keyword-callable),
        # and matching ``format`` would shadow a builtin. Pytest
        # captures stderr per test anyway, so the noise is invisible
        # unless the test fails.

        def _handle(self) -> None:
            content_length_raw = self.headers.get("Content-Length")
            body = b""
            if content_length_raw is not None:
                try:
                    body = self.rfile.read(int(content_length_raw))
                except (OSError, ValueError):
                    body = b""
            state.received.append(
                _RecordedRequest(
                    method=self.command,
                    path=self.path,
                    headers={k: v for k, v in self.headers.items()},
                    body=body,
                ),
            )
            staged = state.next_response
            self.send_response(staged.status)
            for header_name, header_value in staged.headers.items():
                self.send_header(header_name, header_value)
            if "Content-Length" not in staged.headers:
                self.send_header("Content-Length", str(len(staged.body)))
            self.end_headers()
            self.wfile.write(staged.body)

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def do_PUT(self) -> None:
            self._handle()

        def do_DELETE(self) -> None:
            self._handle()

        def do_PATCH(self) -> None:
            self._handle()

    return ThreadingHTTPServer(("127.0.0.1", 0), _Handler)


_NODE_DRIVER_SCRIPT_TEMPLATE: Final[str] = r"""
import http from 'node:http';
import handler from {EXTENSION_PATH_LITERAL};

const server = http.createServer(async (request, response) => {{
  try {{
    const handled = await handler(request, response);
    if (!handled && !response.headersSent) {{
      response.writeHead(404, {{ 'Content-Type': 'application/json' }});
      response.end(JSON.stringify({{ error: 'not handled by extension' }}));
    }}
  }} catch (error) {{
    if (!response.headersSent) {{
      response.writeHead(500, {{ 'Content-Type': 'application/json' }});
      response.end(JSON.stringify({{ error: String(error && error.message) }}));
    }}
  }}
}});

server.listen(0, '127.0.0.1', () => {{
  const address = server.address();
  // Print the bound port on its own line so the parent can parse it
  // without confusion from any later log output.
  process.stdout.write('PORT=' + address.port + '\n');
}});

process.on('SIGTERM', () => server.close(() => process.exit(0)));
process.on('SIGINT', () => server.close(() => process.exit(0)));
"""


def _build_node_driver_script() -> str:
    """Return the Node ESM driver source with the extension path inlined.

    The path is JSON-encoded so paths containing quotes/backslashes
    (e.g. on Windows-style mounts in CI) survive the substitution.
    """
    return _NODE_DRIVER_SCRIPT_TEMPLATE.format(
        EXTENSION_PATH_LITERAL=json.dumps(_EXTENSION_PATH.as_uri()),
    )


def _wait_for_node_port(process: subprocess.Popen[str]) -> int:
    """Read the ``PORT=<n>`` handshake line off the Node child's stdout."""
    deadline = time.monotonic() + _NODE_READY_TIMEOUT_SECONDS
    assert process.stdout is not None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr_tail = ""
            if process.stderr is not None:
                stderr_tail = process.stderr.read() or ""
            raise AssertionError(
                f"node child exited prematurely with code {process.returncode}; stderr={stderr_tail!r}",
            )
        line = process.stdout.readline()
        if not line:
            threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
            continue
        line = line.strip()
        if line.startswith("PORT="):
            return int(line.removeprefix("PORT="))
    raise AssertionError(f"node child never printed PORT= within {_NODE_READY_TIMEOUT_SECONDS}s")


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll until TCP ``host:port`` accepts connections, or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=_POLL_INTERVAL_SECONDS):
                return True
        except OSError:
            threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


@pytest.fixture
def fake_minds_api() -> Generator[tuple[ThreadingHTTPServer, _FakeMindsApiState, str], None, None]:
    """Run a fake Minds API HTTP server in a background thread for one test."""
    state = _FakeMindsApiState(received=[], next_response=_StagedResponse(status=200, headers={}, body=b"ok"))
    server = _make_fake_minds_api(state)
    thread = threading.Thread(target=server.serve_forever, name="fake-minds-api", daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        base_url = f"http://{host}:{port}"
        assert _wait_for_port(str(host), int(port))
        yield server, state, base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


_INJECTED_API_KEY: Final[str] = "central-api-key-fixture-value"


@pytest.fixture
def node_proxy(
    fake_minds_api: tuple[ThreadingHTTPServer, _FakeMindsApiState, str],
) -> Generator[tuple[str, _FakeMindsApiState], None, None]:
    """Spawn the Node proxy driver pointed at the fake Minds API; yield its URL + state."""
    _server, state, upstream_base_url = fake_minds_api
    assert _NODE_BINARY is not None
    script = _build_node_driver_script()
    process = subprocess.Popen(
        [_NODE_BINARY, "--input-type=module", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"LATCHKEY_EXTENSION_MINDS_API_URL": upstream_base_url, "PATH": "/usr/bin:/bin"},
        text=True,
    )
    try:
        port = _wait_for_node_port(process)
        proxy_url = f"http://127.0.0.1:{port}"
        yield proxy_url, state
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


@pytest.fixture
def node_proxy_with_api_key(
    fake_minds_api: tuple[ThreadingHTTPServer, _FakeMindsApiState, str],
) -> Generator[tuple[str, _FakeMindsApiState], None, None]:
    """Like ``node_proxy`` but with ``LATCHKEY_EXTENSION_MINDS_API_KEY`` set.

    Used by the tests that pin the proxy's ``Authorization`` header
    overwrite behaviour. The fixture spins up its own child so the
    base ``node_proxy`` test set keeps running with the env var unset
    (which is its own pinned behaviour: pass-through Authorization).
    """
    _server, state, upstream_base_url = fake_minds_api
    assert _NODE_BINARY is not None
    script = _build_node_driver_script()
    process = subprocess.Popen(
        [_NODE_BINARY, "--input-type=module", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "LATCHKEY_EXTENSION_MINDS_API_URL": upstream_base_url,
            "LATCHKEY_EXTENSION_MINDS_API_KEY": _INJECTED_API_KEY,
            "PATH": "/usr/bin:/bin",
        },
        text=True,
    )
    try:
        port = _wait_for_node_port(process)
        proxy_url = f"http://127.0.0.1:{port}"
        yield proxy_url, state
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Make one HTTP request and return ``(status, response_headers, body)``."""
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return int(resp.status), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return int(e.code), dict(e.headers or {}), e.read()


def test_proxy_forwards_get_request_to_upstream(
    node_proxy: tuple[str, _FakeMindsApiState],
) -> None:
    proxy_url, state = node_proxy
    state.next_response = _StagedResponse(
        status=200,
        headers={"Content-Type": "text/plain"},
        body=b"hello from minds",
    )
    status, headers, body = _http_request(f"{proxy_url}/minds-api-proxy/health?x=1")
    assert status == 200
    assert headers.get("Content-Type") == "text/plain"
    assert body == b"hello from minds"
    assert len(state.received) == 1
    recorded = state.received[0]
    assert recorded.method == "GET"
    assert recorded.path == "/health?x=1"


def test_proxy_strips_extension_prefix_when_no_subpath(
    node_proxy: tuple[str, _FakeMindsApiState],
) -> None:
    """``/minds-api-proxy`` (no trailing slash) maps to upstream ``/``."""
    proxy_url, state = node_proxy
    state.next_response = _StagedResponse(status=204, headers={}, body=b"")
    status, _headers, body = _http_request(f"{proxy_url}/minds-api-proxy")
    assert status == 204
    assert body == b""
    assert len(state.received) == 1
    assert state.received[0].path == "/"


def test_proxy_forwards_post_body_and_method(
    node_proxy: tuple[str, _FakeMindsApiState],
) -> None:
    proxy_url, state = node_proxy
    state.next_response = _StagedResponse(status=201, headers={}, body=b"created")
    payload = b'{"hello":"world"}'
    status, _headers, body = _http_request(
        f"{proxy_url}/minds-api-proxy/items",
        method="POST",
        headers={"Content-Type": "application/json", "X-Custom": "value"},
        body=payload,
    )
    assert status == 201
    assert body == b"created"
    assert len(state.received) == 1
    recorded = state.received[0]
    assert recorded.method == "POST"
    assert recorded.path == "/items"
    assert recorded.body == payload
    assert recorded.headers.get("Content-Type") == "application/json"
    assert recorded.headers.get("X-Custom") == "value"


def test_proxy_injects_authorization_bearer_when_api_key_env_set(
    node_proxy_with_api_key: tuple[str, _FakeMindsApiState],
) -> None:
    """With the env var set, the proxy injects the central key as a Bearer token."""
    proxy_url, state = node_proxy_with_api_key
    state.next_response = _StagedResponse(status=200, headers={}, body=b"ok")
    _http_request(f"{proxy_url}/minds-api-proxy/api/v1/agents/abc/notifications", method="POST")
    assert len(state.received) == 1
    received = state.received[0]
    normalized = {k.lower(): v for k, v in received.headers.items()}
    assert normalized.get("authorization") == f"Bearer {_INJECTED_API_KEY}"


def test_proxy_overwrites_inbound_authorization_when_api_key_env_set(
    node_proxy_with_api_key: tuple[str, _FakeMindsApiState],
) -> None:
    """An agent-supplied ``Authorization`` value must never reach the upstream."""
    proxy_url, state = node_proxy_with_api_key
    state.next_response = _StagedResponse(status=200, headers={}, body=b"ok")
    _http_request(
        f"{proxy_url}/minds-api-proxy/api/v1/agents/abc/notifications",
        method="POST",
        headers={"Authorization": "Bearer attacker-supplied-token"},
    )
    assert len(state.received) == 1
    received = state.received[0]
    normalized = {k.lower(): v for k, v in received.headers.items()}
    assert normalized.get("authorization") == f"Bearer {_INJECTED_API_KEY}"
    assert "attacker-supplied-token" not in normalized.get("authorization", "")


def test_proxy_passes_authorization_through_when_api_key_env_unset(
    node_proxy: tuple[str, _FakeMindsApiState],
) -> None:
    """Without the env var, the proxy must not synthesize an Authorization header.

    Tests / fixtures that don't bother stubbing the central key still
    need pass-through semantics so the upstream sees whatever the
    caller actually sent (it will likely 401, but the proxy must not
    paper over that).
    """
    proxy_url, state = node_proxy
    state.next_response = _StagedResponse(status=401, headers={}, body=b"unauth")
    _http_request(
        f"{proxy_url}/minds-api-proxy/api/v1/agents/abc/notifications",
        method="POST",
        headers={"Authorization": "Bearer original-value"},
    )
    assert len(state.received) == 1
    normalized = {k.lower(): v for k, v in state.received[0].headers.items()}
    assert normalized.get("authorization") == "Bearer original-value"


def test_proxy_strips_gateway_internal_headers_before_forwarding(
    node_proxy: tuple[str, _FakeMindsApiState],
) -> None:
    """Gateway-auth headers must never leak through to the Minds API."""
    proxy_url, state = node_proxy
    state.next_response = _StagedResponse(status=200, headers={}, body=b"ok")
    _http_request(
        f"{proxy_url}/minds-api-proxy/echo",
        headers={
            "X-Latchkey-Gateway-Password": "secret-password",
            "X-Latchkey-Gateway-Permissions-Override": "secret-jwt",
            "X-Pass-Through": "yes",
        },
    )
    assert len(state.received) == 1
    received_headers = state.received[0].headers
    # Header case is normalized by Python's http.server, but the value
    # presence is what matters.
    normalized = {k.lower(): v for k, v in received_headers.items()}
    assert "x-latchkey-gateway-password" not in normalized
    assert "x-latchkey-gateway-permissions-override" not in normalized
    assert normalized.get("x-pass-through") == "yes"


def test_proxy_relays_upstream_status_and_headers(
    node_proxy: tuple[str, _FakeMindsApiState],
) -> None:
    proxy_url, state = node_proxy
    state.next_response = _StagedResponse(
        status=418,
        headers={"Content-Type": "application/json", "X-Custom-Response": "from-upstream"},
        body=b'{"teapot":true}',
    )
    status, headers, body = _http_request(f"{proxy_url}/minds-api-proxy/teapot")
    assert status == 418
    assert headers.get("Content-Type") == "application/json"
    assert headers.get("X-Custom-Response") == "from-upstream"
    assert body == b'{"teapot":true}'


def test_non_proxy_paths_return_404(
    node_proxy: tuple[str, _FakeMindsApiState],
) -> None:
    """The extension must defer (return ``false``) for unrelated paths.

    The driver script handles a ``false`` return by writing a 404. So
    any path outside ``/minds-api-proxy`` exercises that
    'not handled' branch end-to-end.
    """
    proxy_url, state = node_proxy
    status, _headers, body = _http_request(f"{proxy_url}/permission-requests")
    assert status == 404
    assert json.loads(body)["error"] == "not handled by extension"
    # The upstream Minds API must not have been contacted.
    assert state.received == []


def test_proxy_returns_503_when_env_var_unset(
    fake_minds_api: tuple[ThreadingHTTPServer, _FakeMindsApiState, str],
) -> None:
    """Without ``LATCHKEY_EXTENSION_MINDS_API_URL``, the proxy must 503 deterministically."""
    # The ``fake_minds_api`` fixture is requested only to share the
    # skip-when-node-missing gate; the upstream server it stands up is
    # irrelevant to this test (the proxy must 503 before contacting it).
    del fake_minds_api
    assert _NODE_BINARY is not None
    script = _build_node_driver_script()
    process = subprocess.Popen(
        [_NODE_BINARY, "--input-type=module", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin"},
        text=True,
    )
    try:
        port = _wait_for_node_port(process)
        status, headers, body = _http_request(f"http://127.0.0.1:{port}/minds-api-proxy/foo")
        assert status == 503
        assert "application/json" in headers.get("Content-Type", "")
        assert "LATCHKEY_EXTENSION_MINDS_API_URL" in json.loads(body)["error"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
