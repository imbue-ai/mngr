"""Tests for ``mngr imbue_cloud auth`` helpers.

Covers the OAuth localhost callback listener's handler. The handler must:
- Capture query params from a real ``GET /oauth/callback?...`` hit.
- NOT overwrite a previously-captured callback when secondary browser GETs
  (favicon, prefetches, service-worker pings) arrive at the same listener
  with no query params. Before the fix, those secondary GETs erased the
  captured params and the CLI then hung until the 300s OAuth timeout.
"""

import http.server
import threading
import urllib.request
from collections.abc import Iterator

import pytest

from imbue.mngr_imbue_cloud.cli.auth import _make_callback_handler_class
from imbue.mngr_imbue_cloud.cli.auth import _OAuthCaptureBox


@pytest.fixture
def running_callback_server() -> Iterator[tuple[_OAuthCaptureBox, int]]:
    box = _OAuthCaptureBox()
    handler_class = _make_callback_handler_class(box)
    # Bind to port 0 and read back the kernel-assigned port from the live
    # server. Picking a port via a separate socket and rebinding leaves a
    # TOCTOU window where a parallel xdist worker can steal the port.
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="oauth-cb-test")
    thread.start()
    try:
        yield box, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _get(port: int, path: str) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5.0) as resp:
        return resp.status


def test_callback_handler_captures_oauth_query_params(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    box, port = running_callback_server
    status = _get(port, "/oauth/callback?code=abc123&state=xyz")
    assert status == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_followup_favicon_get(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    """Browsers fire a secondary GET /favicon.ico after the callback page renders.

    Before the fix this overwrote the captured params with ``{}``, causing the
    CLI's polling loop to never observe a truthy box and hang until timeout.
    """
    box, port = running_callback_server
    assert _get(port, "/oauth/callback?code=abc123&state=xyz") == 200
    assert _get(port, "/favicon.ico") == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_paramless_root_get(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    """A bare GET / (e.g. from a manual probe or prefetch) must not clobber the box."""
    box, port = running_callback_server
    assert _get(port, "/oauth/callback?code=abc123&state=xyz") == 200
    assert _get(port, "/") == 200
    assert box.get() == {"code": "abc123", "state": "xyz"}


def test_callback_handler_ignores_query_params_on_wrong_path(
    running_callback_server: tuple[_OAuthCaptureBox, int],
) -> None:
    """Even if some other path carries query params, only /oauth/callback should be captured."""
    box, port = running_callback_server
    assert _get(port, "/some-other-path?code=should_be_ignored") == 200
    assert box.get() is None
