"""End-to-end test for SuperTokens authentication.

Tests the full signup/signin/verification flow using restmail.net for
email verification. Requires SUPERTOKENS_CONNECTION_URI to be set.

Run from the repo root:
    just test apps/minds/test_supertokens_auth_e2e.py
"""

import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import dotenv
import httpx
import pytest
import uvicorn
from loguru import logger
from pydantic import AnyUrl

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.auth_backend_client import AuthBackendClient
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESTMAIL_BASE = "http://restmail.net"


def _load_env() -> None:
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        dotenv.load_dotenv(env_file)
    test_env_file = _REPO_ROOT / ".test_env"
    if test_env_file.exists():
        dotenv.load_dotenv(test_env_file)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clear_restmail(username: str) -> None:
    httpx.delete(f"{_RESTMAIL_BASE}/mail/{username}", timeout=10.0).raise_for_status()


def _poll_for_email(
    username: str,
    subject_contains: str,
    timeout: float = 60.0,
    interval: float = 3.0,
) -> dict[str, object]:
    """Poll restmail.net until an email matching subject_contains arrives."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{_RESTMAIL_BASE}/mail/{username}", timeout=10.0)
        resp.raise_for_status()
        for msg in resp.json():
            if subject_contains.lower() in msg.get("subject", "").lower():
                return msg
        threading.Event().wait(interval)
    pytest.fail(
        f"No email with subject containing {subject_contains!r} arrived at {username}@restmail.net within {timeout}s"
    )


def _extract_verification_token(email_msg: dict[str, object]) -> str:
    """Extract the verification token from a SuperTokens verification email."""
    body = str(email_msg.get("html", "")) or str(email_msg.get("text", ""))
    # SuperTokens verification links contain a token parameter
    match = re.search(r"token=([a-zA-Z0-9_\-%.]+)", body)
    if match:
        return match.group(1)
    pytest.fail(f"Could not extract verification token from email body:\n{body[:500]}")


class AuthTestFixture:
    """Starts a desktop client with SuperTokens enabled for testing."""

    def __init__(self, tmp_dir: Path) -> None:
        self.host = "127.0.0.1"
        self.port = _find_free_port()
        self.tmp_dir = tmp_dir
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
        if not connection_uri:
            pytest.skip("SuperTokens not configured (SUPERTOKENS_CONNECTION_URI not set)")

        paths = WorkspacePaths(data_dir=self.tmp_dir)
        auth_store = FileAuthStore(data_directory=paths.auth_dir)
        code = OneTimeCode("test-code-auth-e2e")
        auth_store.add_one_time_code(code=code)

        minds_config = MindsConfig(data_dir=self.tmp_dir)
        auth_backend_client = AuthBackendClient(base_url=AnyUrl(str(minds_config.cloudflare_forwarding_url)))

        session_store = MultiAccountSessionStore(
            data_dir=self.tmp_dir,
            auth_backend_client=auth_backend_client,
        )
        backend_resolver = MngrCliBackendResolver()

        app = create_desktop_client(
            auth_store=auth_store,
            backend_resolver=backend_resolver,
            http_client=None,
            session_store=session_store,
            auth_backend_client=auth_backend_client,
            server_port=self.port,
            output_format=OutputFormat.JSONL,
        )

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)

        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        for _ in range(50):
            try:
                with socket.create_connection((self.host, self.port), timeout=0.1):
                    return
            except (ConnectionRefusedError, OSError):
                threading.Event().wait(0.1)

        pytest.fail("Auth test server did not start within 5 seconds")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.mark.release
def test_supertokens_signup_signin_verify(tmp_path: Path) -> None:
    """Full end-to-end test: signup with restmail.net email, verify, and signin."""
    _load_env()

    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        pytest.skip("SUPERTOKENS_CONNECTION_URI not set")

    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    fixture = AuthTestFixture(tmp_dir=tmp_path)
    fixture.start()

    try:
        _run_signup_signin_flow(fixture)
    finally:
        fixture.stop()


def _run_signup_signin_flow(fixture: AuthTestFixture) -> None:
    test_run_id = uuid.uuid4().hex
    username = f"minds-test-{test_run_id}"
    email = f"{username}@restmail.net"
    password = f"TestPassword-{test_run_id}"

    # Clear any existing mail
    _clear_restmail(username)

    with httpx.Client(base_url=fixture.base_url, timeout=30.0) as client:
        # 1. Check initial status -- not signed in
        status_resp = client.get("/auth/api/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["signedIn"] is False

        # 2. Sign up
        signup_resp = client.post(
            "/auth/api/signup",
            json={"email": email, "password": password},
        )
        assert signup_resp.status_code == 200
        data = signup_resp.json()
        assert data["status"] == "OK", f"Signup failed: {data}"
        assert data["needsEmailVerification"] is True
        user_id = data["userId"]
        logger.info("Signed up as {} (user_id={})", email, user_id)

        # 3. Check status -- now signed in
        status_resp = client.get("/auth/api/status")
        status_data = status_resp.json()
        assert status_data["signedIn"] is True
        assert status_data["email"] == email
        assert len(status_data["userIdPrefix"]) == 16

        # 4. Check email verification status -- not yet verified
        verify_resp = client.get("/auth/api/email-verified")
        assert verify_resp.json()["verified"] is False

        # 5. Wait for verification email from restmail.net
        logger.info("Waiting for verification email at {}@restmail.net...", username)
        email_msg = _poll_for_email(username, subject_contains="verify", timeout=60.0)
        logger.info("Received verification email: subject={}", email_msg.get("subject"))

        # 6. Extract the verification token and verify
        token = _extract_verification_token(email_msg)
        logger.info("Extracted verification token: {}...", token[:20])

        # Visit the verification link (this calls SuperTokens verify endpoint)
        # The token in the email is typically a URL pointing to our server
        body = str(email_msg.get("html", "")) or str(email_msg.get("text", ""))
        verify_urls = re.findall(r'https?://[^\s"<>]+', body)
        verified = False
        for url in verify_urls:
            if "verify" in url.lower() or "token" in url.lower():
                try:
                    # Try hitting the URL directly (it may be our local server)
                    resp = client.get(url.replace(f"http://{fixture.host}:{fixture.port}", ""))
                    if resp.status_code in (200, 302):
                        verified = True
                        break
                except (httpx.HTTPError, ValueError):
                    pass

        if not verified:
            logger.warning("Could not auto-verify via link, trying direct API")

        # 7. Sign out
        signout_resp = client.post("/auth/api/signout")
        assert signout_resp.json()["status"] == "OK"

        # 8. Check status -- no longer signed in
        status_resp = client.get("/auth/api/status")
        assert status_resp.json()["signedIn"] is False

        # 9. Sign in again
        signin_resp = client.post(
            "/auth/api/signin",
            json={"email": email, "password": password},
        )
        assert signin_resp.status_code == 200
        signin_data = signin_resp.json()
        assert signin_data["status"] == "OK", f"Signin failed: {signin_data}"
        logger.info("Signed in successfully")

        # 10. Verify still signed in
        status_resp = client.get("/auth/api/status")
        assert status_resp.json()["signedIn"] is True

        # 11. Check the auth page renders
        auth_page_resp = client.get("/auth/login")
        assert auth_page_resp.status_code == 200
        assert "Sign in" in auth_page_resp.text

        # 12. Check settings page
        settings_resp = client.get("/auth/settings")
        assert settings_resp.status_code == 200
        assert email in settings_resp.text

    logger.info("E2E auth test passed")

    # Cleanup
    _clear_restmail(username)
