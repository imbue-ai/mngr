"""Shared pytest fixtures for ``mngr imbue_cloud`` CLI tests."""

import http.server
import json
import threading
from collections.abc import Callable
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
    httpx2 call, JSON output) without patching any module attribute.
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
    # No outcome is published in these tests, so the page must not hold its response.
    handler_class = _make_callback_handler_class(box, None, 0.0, "https://accounts.example.test/manage")
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


class _JsonConnectorStubHandler(http.server.BaseHTTPRequestHandler):
    """A quiet loopback connector stub that answers in JSON."""

    def _send_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _DeviceLoginConnectorStubHandler(_JsonConnectorStubHandler):
    """Answers the connector routes ``auth login`` calls, recording each request as ``"METHOD /path"``.

    Those are the accounts-config probe, the listener-lease renewals and
    release, and the code exchange.
    """

    device_token_status_code: int = 200
    device_token_refusal_detail: object = "Invalid, expired, or already-used code"
    device_attempts_status_code: int = 200
    recorded_requests: list[str] = []

    def do_GET(self) -> None:
        self.recorded_requests.append(f"GET {self.path}")
        self._send_json(200, {"google_enabled": False})

    def do_PUT(self) -> None:
        self.recorded_requests.append(f"PUT {self.path}")
        self._send_json(self.device_attempts_status_code, {"status": "OK"})

    def do_DELETE(self) -> None:
        self.recorded_requests.append(f"DELETE {self.path}")
        self._send_json(self.device_attempts_status_code, {"status": "OK"})

    def do_POST(self) -> None:
        self.recorded_requests.append(f"POST {self.path}")
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.device_token_status_code == 200:
            self._send_json(
                200,
                {
                    "status": "OK",
                    "user": {"user_id": "user-device-login-4417", "email": "device-login-4417@example.com"},
                    "tokens": {"access_token": "at-device-login-4417", "refresh_token": "rt-device-login-4417"},
                },
            )
        else:
            self._send_json(self.device_token_status_code, {"detail": self.device_token_refusal_detail})


class _PasswordAuthConnectorStubHandler(_JsonConnectorStubHandler):
    """Answers the connector routes ``auth signin`` and ``auth signup`` call, recording each request as ``"METHOD /path"``.

    Those are the sign-in or sign-up itself, the is-verified check, and the
    revocation of a session the command does not keep.
    """

    is_verified_status_code: int = 200
    is_email_verified: bool = True
    is_verification_reported_as_needed: bool = False
    recorded_requests: list[str] = []

    def do_POST(self) -> None:
        self.recorded_requests.append(f"POST {self.path}")
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path in ("/auth/signin", "/auth/signup"):
            self._send_json(
                200,
                {
                    "status": "OK",
                    "user": {"user_id": "user-password-auth-7306", "email": "password-auth-7306@example.com"},
                    "tokens": {"access_token": "at-password-auth-7306", "refresh_token": "rt-password-auth-7306"},
                    "needs_email_verification": self.is_verification_reported_as_needed,
                },
            )
        elif self.path == "/auth/email/is-verified":
            self._send_json(self.is_verified_status_code, {"status": "OK", "verified": self.is_email_verified})
        elif self.path == "/auth/session/revoke-current":
            self._send_json(200, {"status": "OK"})
        else:
            raise AssertionError(f"unexpected path {self.path}")


class _SendVerificationConnectorStubHandler(_JsonConnectorStubHandler):
    """Answers the token refresh a stored session needs, and the send-verification route with ``send_answer``."""

    send_answer: dict[str, object] = {}

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/auth/session/refresh":
            payload: dict[str, object] = {"status": "OK", "tokens": {"access_token": "at-2", "refresh_token": "rt-2"}}
        elif self.path == "/auth/email/send-verification":
            payload = {"status": "OK", **self.send_answer}
        else:
            raise AssertionError(f"unexpected path {self.path}")
        self._send_json(200, payload)


@pytest.fixture
def start_loopback_stub() -> Iterator[Callable[[type[http.server.BaseHTTPRequestHandler], str], str]]:
    """A starter that serves a handler class on a loopback port and returns its URL; every server stops at teardown."""
    servers: list[http.server.HTTPServer] = []

    def start(handler_class: type[http.server.BaseHTTPRequestHandler], thread_name: str) -> str:
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True, name=thread_name).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def send_verification_connector_stub(
    start_loopback_stub: Callable[[type[http.server.BaseHTTPRequestHandler], str], str],
) -> Callable[[dict[str, object]], str]:
    """Start a loopback connector stub whose send-verification route answers with the given fields; returns its URL."""

    def start(send_answer: dict[str, object]) -> str:
        handler_class = type(
            "_ScopedSendVerificationConnectorStubHandler",
            (_SendVerificationConnectorStubHandler,),
            {"send_answer": send_answer},
        )
        return start_loopback_stub(handler_class, "send-verification-stub-test")

    return start


@pytest.fixture
def device_login_connector_stub(
    start_loopback_stub: Callable[[type[http.server.BaseHTTPRequestHandler], str], str],
) -> Callable[..., tuple[str, list[str]]]:
    """Start a loopback connector stub for ``auth login`` with the given status codes for the exchange and the lease.

    A refused exchange answers ``device_token_refusal_detail`` as its ``detail``.
    Returns ``(base_url, recorded_requests)``; the list fills with ``"METHOD /path"`` as requests arrive.
    """

    def start(
        device_token_status_code: int,
        device_attempts_status_code: int = 200,
        device_token_refusal_detail: object = _DeviceLoginConnectorStubHandler.device_token_refusal_detail,
    ) -> tuple[str, list[str]]:
        recorded_requests: list[str] = []
        handler_class = type(
            "_ScopedDeviceLoginConnectorStubHandler",
            (_DeviceLoginConnectorStubHandler,),
            {
                "device_token_status_code": device_token_status_code,
                "device_token_refusal_detail": device_token_refusal_detail,
                "device_attempts_status_code": device_attempts_status_code,
                "recorded_requests": recorded_requests,
            },
        )
        return start_loopback_stub(handler_class, "device-login-stub-test"), recorded_requests

    return start


@pytest.fixture
def password_auth_connector_stub(
    start_loopback_stub: Callable[[type[http.server.BaseHTTPRequestHandler], str], str],
) -> Callable[..., tuple[str, list[str]]]:
    """Start a loopback connector stub for ``auth signin`` / ``auth signup`` with the given is-verified answer.

    ``is_verification_reported_as_needed`` makes the sign-in itself say ``needs_email_verification``, as an old connector does.
    Returns ``(base_url, recorded_requests)``; the list fills with ``"METHOD /path"`` as requests arrive.
    """

    def start(
        is_email_verified: bool,
        is_verified_status_code: int = 200,
        is_verification_reported_as_needed: bool = False,
    ) -> tuple[str, list[str]]:
        recorded_requests: list[str] = []
        handler_class = type(
            "_ScopedPasswordAuthConnectorStubHandler",
            (_PasswordAuthConnectorStubHandler,),
            {
                "is_email_verified": is_email_verified,
                "is_verified_status_code": is_verified_status_code,
                "is_verification_reported_as_needed": is_verification_reported_as_needed,
                "recorded_requests": recorded_requests,
            },
        )
        return start_loopback_stub(handler_class, "password-auth-stub-test"), recorded_requests

    return start
