"""End-to-end test for the minds forwarding server using Playwright.

Starts the forwarding server, creates an agent from the forever-claude-template
repo, and waits for a signal file before tearing down. This allows interactive
inspection of the running system.

Run from the repo root:
    just test apps/minds/test_forwarding_server_e2e.py::test_create_agent_e2e

Run headed (browser visible for interactive debugging):
    HEADED=1 just test apps/minds/test_forwarding_server_e2e.py::test_create_agent_e2e

The test waits for /tmp/minds-e2e-done to exist before tearing down.
Create this file to signal the test to finish:
    touch /tmp/minds-e2e-done

The test removes /tmp/minds-e2e-done on startup so it always waits fresh.
"""

import os
import re
import shutil
import socket
import subprocess
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
from imbue.minds.forwarding_server.backend_resolver import MngrStreamManager
from imbue.minds.primitives import OneTimeCode

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_REPO = _REPO_ROOT / ".external_worktrees" / "forever-claude-template"
_SIGNAL_FILE = Path("/tmp/minds-e2e-done")
_AGENT_NAME = "forever"


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


def _wait_for_signal_file(path: Path, poll_interval: float = 1.0) -> None:
    """Block until the signal file exists."""
    while not path.exists():
        time.sleep(poll_interval)


def _destroy_agent(agent_name: str) -> None:
    """Destroy an agent by name, ignoring errors if it doesn't exist."""
    try:
        subprocess.run(
            ["uv", "run", "mngr", "destroy", agent_name, "--yes"],
            capture_output=True,
            timeout=30,
            cwd=_REPO_ROOT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


class ForwardingServerFixture:
    """Manages a forwarding server lifecycle for testing."""

    def __init__(self, tmp_dir: Path) -> None:
        self.host = "127.0.0.1"
        self.port = _find_free_port()
        self.code = OneTimeCode("test-code-for-e2e-12345")
        self.tmp_dir = tmp_dir
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._stream_manager: MngrStreamManager | None = None

    @property
    def login_url(self) -> str:
        return f"http://{self.host}:{self.port}/login?one_time_code={self.code}"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        """Start the forwarding server with agent discovery in a background thread."""
        paths = MindPaths(data_dir=self.tmp_dir)
        auth_store = FileAuthStore(data_directory=paths.auth_dir)
        auth_store.add_one_time_code(code=self.code)

        backend_resolver = MngrCliBackendResolver()
        self._stream_manager = MngrStreamManager(resolver=backend_resolver)
        agent_creator = AgentCreator(paths=paths)

        app = create_forwarding_server(
            auth_store=auth_store,
            backend_resolver=backend_resolver,
            http_client=None,
            agent_creator=agent_creator,
        )

        self._stream_manager.start()

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
        if self._stream_manager is not None:
            self._stream_manager.stop()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.mark.release
def test_create_agent_e2e(tmp_path: Path) -> None:
    """Create an agent from the forever-claude-template and verify it runs.

    After creation, waits for /tmp/minds-e2e-done before tearing down,
    allowing interactive inspection of the running agent.
    """
    _load_env()

    # Clean up signal file so we always wait fresh
    _SIGNAL_FILE.unlink(missing_ok=True)

    # Clean up any leftover agent from a previous run
    _destroy_agent(_AGENT_NAME)

    server = ForwardingServerFixture(tmp_path)
    server.start()

    headed = os.environ.get("HEADED", "0") == "1"
    slow_mo = int(os.environ.get("SLOW_MO", "0"))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed, slow_mo=slow_mo)
            try:
                page = browser.new_page()

                # Authenticate
                page.goto(server.login_url)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_url(re.compile(r"/$|/create"), timeout=5000)

                # Navigate to create page
                page.goto(f"{server.base_url}/create")
                page.wait_for_load_state("domcontentloaded")

                # Fill out the create form
                page.fill("#agent_name", _AGENT_NAME)
                page.fill("#git_url", str(_TEMPLATE_REPO))
                page.fill("#branch", "")
                page.select_option("#launch_mode", "LOCAL")

                # Submit the form
                page.click('button[type="submit"]')

                # Should redirect to creating page with logs
                page.wait_for_url(re.compile(r"/creating/"), timeout=10000)
                assert "Creating" in page.title() or "creating" in page.url

                # Wait for creation to complete (redirects to /agents/{id}/)
                # This can take a while for Docker builds
                page.wait_for_url(
                    re.compile(r"/agents/[^/]+/"),
                    timeout=300000,  # 5 minutes for Docker build
                )

                agent_url = page.url
                print(f"\nAgent created successfully!")
                print(f"Agent URL: {agent_url}")
                print(f"Server: {server.base_url}")
                print(f"\nWaiting for signal file: {_SIGNAL_FILE}")
                print(f"Create it to finish the test:  touch {_SIGNAL_FILE}")

                # Wait for the signal file before tearing down
                _wait_for_signal_file(_SIGNAL_FILE)

                print("\nSignal received, tearing down...")

            finally:
                browser.close()
    finally:
        _destroy_agent(_AGENT_NAME)
        server.stop()
        _SIGNAL_FILE.unlink(missing_ok=True)
