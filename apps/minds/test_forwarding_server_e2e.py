"""End-to-end test for the minds forwarding server using Playwright.

Starts the forwarding server on a random port, authenticates via the
one-time login URL, and verifies the landing page loads.

Run from the repo root:
    just test apps/minds/test_forwarding_server_e2e.py::test_forwarding_server_login_and_landing

Run headed (browser visible for interactive debugging):
    HEADED=1 just test apps/minds/test_forwarding_server_e2e.py::test_forwarding_server_login_and_landing

Slow motion (500ms between actions, useful with HEADED=1):
    HEADED=1 SLOW_MO=500 just test apps/minds/test_forwarding_server_e2e.py::test_forwarding_server_login_and_landing
"""

import os
import re
import socket
import threading
import time
from pathlib import Path

import dotenv
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import MngrCliBackendResolver
from imbue.minds.primitives import OneTimeCode

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> None:
    """Load environment variables from the repo root .env file."""
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        dotenv.load_dotenv(env_file)


def _find_free_port() -> int:
    """Find and return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ForwardingServerFixture:
    """Manages a forwarding server lifecycle for testing."""

    def __init__(self, tmp_dir: Path) -> None:
        self.host = "127.0.0.1"
        self.port = _find_free_port()
        self.code = OneTimeCode("test-code-for-e2e-12345")
        self.tmp_dir = tmp_dir
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def login_url(self) -> str:
        return f"http://{self.host}:{self.port}/login?one_time_code={self.code}"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        """Start the forwarding server in a background thread."""
        paths = MindPaths(data_dir=self.tmp_dir)
        auth_store = FileAuthStore(data_directory=paths.auth_dir)
        auth_store.add_one_time_code(code=self.code)

        backend_resolver = MngrCliBackendResolver()
        agent_creator = AgentCreator(paths=paths)

        app = create_forwarding_server(
            auth_store=auth_store,
            backend_resolver=backend_resolver,
            http_client=None,
            agent_creator=agent_creator,
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
                time.sleep(0.1)

        raise TimeoutError("Forwarding server did not start within 5 seconds")

    def stop(self) -> None:
        """Signal the server to shut down."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.mark.release
def test_forwarding_server_login_and_landing(tmp_path: Path) -> None:
    """Verify the forwarding server starts, authenticates, and shows the landing page."""
    _load_env()

    server = ForwardingServerFixture(tmp_path)
    server.start()

    headed = os.environ.get("HEADED", "0") == "1"
    slow_mo = int(os.environ.get("SLOW_MO", "0"))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed, slow_mo=slow_mo)
            try:
                page = browser.new_page()

                # Authenticate and land on the main page
                page.goto(server.login_url)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_url(re.compile(r"/$|/create"), timeout=5000)

                # The landing page should show the create form (no agents exist)
                content = page.content().lower()
                assert "create" in content or "your minds" in content
            finally:
                browser.close()
    finally:
        server.stop()
