"""Shared pytest fixtures for ``mngr imbue_cloud`` CLI tests."""

import http.server
import json
import threading
from collections.abc import Iterator

import pytest

from imbue.mngr_imbue_cloud.cli.auth import _CallbackCaptureBox
from imbue.mngr_imbue_cloud.cli.auth import _make_callback_handler_class


class _PublicProfileStubHandler(http.server.BaseHTTPRequestHandler):
    """Answers ``GET /users/{id}/profile`` the way the connector does, recording every path it served."""

    served_paths: list[str] = []

    def do_GET(self) -> None:
        self.served_paths.append(self.path)
        user_id = self.path.removeprefix("/users/").removesuffix("/profile")
        origin = f"http://{self.headers['Host']}"
        body = json.dumps(
            {
                "user_id": user_id,
                "display_name": "Alice",
                "profile_picture_url": f"{origin}/users/{user_id}/profile-picture/abc",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture
def local_connector_stub() -> Iterator[tuple[str, list[str]]]:
    """A loopback HTTP server standing in for the connector's public profile route; yields ``(base_url, served_paths)``.

    Lets a CLI test run the real command end to end (URL resolution, the
    httpx call, JSON output) without patching any module attribute.
    """
    served_paths: list[str] = []
    handler_class = type("_ScopedProfileStubHandler", (_PublicProfileStubHandler,), {"served_paths": served_paths})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="connector-stub-test")
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", served_paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


@pytest.fixture
def running_callback_server() -> Iterator[tuple[_CallbackCaptureBox, int]]:
    box = _CallbackCaptureBox()
    handler_class = _make_callback_handler_class(box, None)
    # Bind to port 0 and read back the kernel-assigned port from the live
    # server. Picking a port via a separate socket and rebinding leaves a
    # TOCTOU window where a parallel xdist worker can steal the port.
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="login-cb-test")
    thread.start()
    try:
        yield box, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
