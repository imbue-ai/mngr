"""End-to-end test for the minds forwarding server using Playwright.

Starts the forwarding server on a random port, authenticates via the
one-time login URL, and verifies the landing page loads.

Run headless (default):
    cd apps/minds && uv run pytest test_forwarding_server_e2e.py -m release --no-cov --cov-fail-under=0

Run headed (for interactive debugging):
    HEADED=1 cd apps/minds && uv run pytest test_forwarding_server_e2e.py -m release --no-cov --cov-fail-under=0

Slow down for debugging:
    HEADED=1 SLOW_MO=500 cd apps/minds && uv run pytest test_forwarding_server_e2e.py -m release --no-cov --cov-fail-under=0
"""

import os
import re
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import MngrCliBackendResolver
from imbue.minds.primitives import OneTimeCode


def _find_free_port() -> int:
    """Find and return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(
    tmp_dir: str,
    host: str,
    port: int,
    one_time_code: OneTimeCode,
) -> threading.Thread:
    """Start the forwarding server in a background thread and return the thread."""
    paths = MindPaths(data_dir=tmp_dir)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    auth_store.add_one_time_code(code=one_time_code)

    backend_resolver = MngrCliBackendResolver()
    agent_creator = AgentCreator(paths=paths)

    app = create_forwarding_server(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        agent_creator=agent_creator,
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    for _ in range(50):
        try:
            with socket.create_connection((host, port), timeout=0.1):
                break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)

    return thread


@pytest.mark.release
def test_forwarding_server_starts_and_shows_login_page(tmp_path: object) -> None:
    """Verify the forwarding server starts and shows the login page."""
    tmp_dir = str(tmp_path)
    host = "127.0.0.1"
    port = _find_free_port()
    code = OneTimeCode("test-code-for-e2e-12345")

    _start_server(tmp_dir, host, port, code)

    headed = os.environ.get("HEADED", "0") == "1"
    slow_mo = int(os.environ.get("SLOW_MO", "0"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, slow_mo=slow_mo)
        page = browser.new_page()

        # Visit the server -- should show login page (not authenticated)
        page.goto(f"http://{host}:{port}/")
        page.wait_for_load_state("domcontentloaded")
        assert "Minds" in page.title() or "minds" in page.content().lower()
        assert "login" in page.content().lower()

        # Authenticate via the one-time code
        page.goto(f"http://{host}:{port}/login?one_time_code={code}")
        page.wait_for_load_state("domcontentloaded")

        # Should redirect to landing page after auth
        # Wait for redirect to complete
        page.wait_for_url(re.compile(r"/$|/create"), timeout=5000)

        # The landing page should show either the create form (no agents)
        # or the agent list
        content = page.content().lower()
        assert "create" in content or "your minds" in content or "agent" in content

        browser.close()
