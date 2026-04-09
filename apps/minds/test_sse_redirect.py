"""Minimal test for the SSE-based redirect flow on the creating page.

No Docker, no agent creation -- just tests that the SSE stream delivers
the done event and the browser JS redirects.

Run from the repo root:
    just test apps/minds/test_sse_redirect.py::test_sse_redirect_on_done
"""

import os
import queue
import re
import socket
import sys
import threading
from pathlib import Path

import pytest
import uvicorn
from loguru import logger
from playwright.sync_api import sync_playwright

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreationStatus
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.agent_creator import LOG_SENTINEL
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import MngrCliBackendResolver
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.release
def test_sse_redirect_on_done(tmp_path: Path) -> None:
    """Test that the creating page SSE stream delivers the done event and the browser redirects."""
    logger.remove()
    logger.add(
        sys.stderr, level="DEBUG", format="{time:HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} - {message}"
    )

    host = "127.0.0.1"
    port = _find_free_port()
    code = OneTimeCode("test-sse-code-abc123")

    paths = MindPaths(data_dir=tmp_path)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    auth_store.add_one_time_code(code=code)
    resolver = MngrCliBackendResolver()
    creator = AgentCreator(paths=paths)

    # Manually set up a fake agent creation that completes immediately
    agent_id = AgentId()
    log_queue: queue.Queue[str] = queue.Queue()

    with creator._lock:
        creator._statuses[str(agent_id)] = AgentCreationStatus.CLONING
        creator._log_queues[str(agent_id)] = log_queue

    app = create_forwarding_server(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        agent_creator=creator,
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(50):
        try:
            with socket.create_connection((host, port), timeout=0.1):
                break
        except (ConnectionRefusedError, OSError):
            threading.Event().wait(0.1)

    headed = os.environ.get("HEADED", "0") == "1"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            try:
                page = browser.new_page()
                page.on("console", lambda msg: logger.info("[browser] {}", msg))

                # Authenticate
                page.goto(f"http://{host}:{port}/login?one_time_code={code}")
                page.wait_for_url(re.compile(r"/$|/create"), timeout=5000)

                # Go directly to the creating page
                page.goto(f"http://{host}:{port}/creating/{agent_id}")
                page.wait_for_load_state("domcontentloaded")
                logger.info("On creating page, waiting for SSE stream to connect...")

                # Give the EventSource time to connect
                threading.Event().wait(1)

                # Now simulate the creation completing: put some log lines
                # then the sentinel into the queue
                logger.info("Simulating creation completion...")
                log_queue.put("[test] Building something...")
                log_queue.put("[test] Almost done...")
                threading.Event().wait(0.5)

                # Set status to DONE and put sentinel
                with creator._lock:
                    creator._statuses[str(agent_id)] = AgentCreationStatus.DONE
                    creator._redirect_urls[str(agent_id)] = f"/agents/{agent_id}/"

                log_queue.put("[test] Agent created successfully.")
                log_queue.put(LOG_SENTINEL)
                logger.info("Sentinel sent, waiting for browser redirect...")

                # Wait for the redirect
                page.wait_for_url(re.compile(r"/agents/"), timeout=10000)
                logger.info("Redirect happened! URL: {}", page.url)
                assert f"/agents/{agent_id}" in page.url

            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
