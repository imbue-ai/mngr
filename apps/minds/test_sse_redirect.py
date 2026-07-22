"""Minimal test for the redirect flow on the creating page.

No Docker, no agent creation -- just tests that the creating page redirects
into the workspace once creation completes. Completion is driven by the
creating page's status poll against the v1 operations resource
(``/api/v1/workspaces/operations/create/<creation_id>``); the SSE stream on
that resource carries only the live log lines.

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
from loguru import logger
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.release
def test_sse_redirect_on_done(tmp_path: Path) -> None:
    """Test that the creating page detects completion (via the v1 status poll) and the browser redirects."""
    logger.remove()
    logger.add(
        sys.stderr, level="DEBUG", format="{time:HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} - {message}"
    )

    host = "127.0.0.1"
    port = _find_free_port()
    code = OneTimeCode("test-sse-code-abc123")

    paths = WorkspacePaths(data_dir=tmp_path)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    auth_store.add_one_time_code(code=code)
    resolver = MngrCliBackendResolver()
    root_cg = ConcurrencyGroup(name="test-root")
    root_cg.__enter__()
    creator = AgentCreator(
        paths=paths,
        root_concurrency_group=root_cg,
        notification_dispatcher=NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False),
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )

    # Manually set up a fake agent creation that completes immediately. The
    # creation is keyed by a minds-internal ``CreationId`` (the handle the
    # ``/creating/<id>`` page and the ``operations/create/<id>`` resource use);
    # the canonical ``AgentId`` is a separate namespace, known only once the
    # inner ``mngr create`` returns, and is what the redirect ultimately targets.
    creation_id = CreationId()
    agent_id = AgentId()
    log_queue: queue.Queue[str] = queue.Queue()

    with creator._lock:
        creator._statuses[str(creation_id)] = AgentCreationStatus.INITIALIZING
        creator._launch_modes[str(creation_id)] = LaunchMode.DOCKER
        creator._host_names[str(creation_id)] = "test-workspace"
        creator._log_queues[str(creation_id)] = log_queue

    # ``paths`` mounts the ``/api/v1`` blueprint, which the creating page's JS
    # polls for status/logs (``operations/create/<creation_id>``); without it
    # those routes 404 and the page never learns the creation finished.
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        agent_creator=creator,
        paths=paths,
    )

    server = make_server(host, port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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

                # Go directly to the creating page, which shows the loading /
                # progress screen while the workspace is created in the
                # background and redirects into it once creation completes.
                page.goto(f"http://{host}:{port}/creating/{creation_id}")
                page.wait_for_selector("#creating", state="attached", timeout=5000)
                logger.info("On creating page, waiting for SSE stream to connect...")

                # Give the EventSource time to connect
                threading.Event().wait(1)

                # Now simulate the creation completing: put some log lines
                # then the sentinel into the queue
                logger.info("Simulating creation completion...")
                log_queue.put("[test] Building something...")
                log_queue.put("[test] Almost done...")
                threading.Event().wait(0.5)

                # Set status to DONE with the resolved agent id + redirect URL,
                # then put the log sentinel. The creating page's status poll
                # (`operations/create/<creation_id>`) is the authoritative
                # completion signal: once it returns DONE + redirect_url the
                # page stamps data-ready + data-redirect-url on the creating root and
                # waits for the user to finish the onboarding walkthrough --
                # there is no automatic redirect anymore. The redirect URL is
                # the canonical `/goto/<agent>/` route the real creator
                # populates.
                with creator._lock:
                    creator._statuses[str(creation_id)] = AgentCreationStatus.DONE
                    creator._canonical_agent_ids[str(creation_id)] = agent_id
                    creator._redirect_urls[str(creation_id)] = f"/goto/{agent_id}/"

                log_queue.put("[test] Agent created successfully.")
                log_queue.put(LOG_SENTINEL)

                logger.info("Creation done, waiting for the ready state...")
                page.wait_for_selector("#creating[data-ready='true']", state="attached", timeout=10000)

                # Click through the onboarding walkthrough to the last step,
                # where the Begin button appears once the workspace is ready;
                # clicking it performs the actual navigation.
                for _ in range(20):
                    if page.locator("#onboarding-begin").is_visible():
                        break
                    page.click("#onboarding-next")
                page.click("#onboarding-begin")

                logger.info("Begin clicked, waiting for browser redirect...")
                page.wait_for_url(re.compile(r"/goto/"), timeout=10000)
                logger.info("Redirect happened! URL: {}", page.url)
                assert f"/goto/{agent_id}" in page.url

            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
