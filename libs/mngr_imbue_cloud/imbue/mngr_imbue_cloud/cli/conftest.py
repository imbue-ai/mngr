"""Shared pytest fixtures for ``mngr imbue_cloud`` CLI tests."""

import http.server
import threading
from collections.abc import Iterator

import pytest

from imbue.mngr_imbue_cloud.cli.auth import _OAuthCaptureBox
from imbue.mngr_imbue_cloud.cli.auth import _make_callback_handler_class


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
