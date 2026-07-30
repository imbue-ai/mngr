"""Minimal test for the redirect flow on the creating page.

No Docker, no agent create attempt -- just tests that the creating page redirects
into the workspace once the create attempt completes. Completion is driven by the
creating page's status poll against the v1 operations resource
(``/api/v1/workspaces/operations/create/<create_attempt_id>``); the SSE stream on
that resource carries only the live log lines.

Run from the repo root:
    just test apps/minds/test_sse_redirect.py::test_sse_redirect_on_done
"""

import os
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
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import CreateAttemptLogSink
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreateAttemptId
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

    # Manually set up a fake agent create attempt that completes immediately. The
    # create attempt is keyed by a minds-internal ``CreateAttemptId`` (the handle the
    # ``/creating/<id>`` page and the ``operations/create/<id>`` resource use);
    # the canonical ``AgentId`` is a separate namespace, known only once the
    # inner ``mngr create`` returns, and is what the redirect ultimately targets.
    create_attempt_id = CreateAttemptId()
    agent_id = AgentId()
    log_sink = CreateAttemptLogSink()

    with creator._lock:
        creator._statuses[str(create_attempt_id)] = AgentCreateAttemptStatus.INITIALIZING
        creator._launch_modes[str(create_attempt_id)] = LaunchMode.DOCKER
        creator._host_names[str(create_attempt_id)] = "test-workspace"
        creator._log_sinks[str(create_attempt_id)] = log_sink

    # ``paths`` mounts the ``/api/v1`` blueprint, which the creating page's JS
    # polls for status/logs (``operations/create/<create_attempt_id>``); without it
    # those routes 404 and the page never learns the create attempt finished.
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
                # background and redirects into it once the create attempt completes.
                page.goto(f"http://{host}:{port}/creating/{create_attempt_id}")
                page.wait_for_selector("#creating", state="attached", timeout=5000)
                logger.info("On creating page, waiting for SSE stream to connect...")

                # Give the EventSource time to connect
                threading.Event().wait(1)

                # Now simulate the create attempt completing: put some log lines
                # then the sentinel into the queue
                logger.info("Simulating create attempt completion...")
                log_sink.put("[test] Building something...")
                log_sink.put("[test] Almost done...")
                threading.Event().wait(0.5)

                # Set status to DONE with the resolved agent id + redirect URL,
                # then put the log sentinel. The creating page's status poll
                # (`operations/create/<create_attempt_id>`) is the authoritative
                # completion signal: once it returns DONE + redirect_url the
                # page stamps data-ready + data-redirect-url on the creating
                # root, and the walkthrough enters the workspace from there.
                # The redirect URL is the canonical `/goto/<agent>/` route the
                # real creator populates.
                with creator._lock:
                    creator._statuses[str(create_attempt_id)] = AgentCreateAttemptStatus.DONE
                    creator._canonical_agent_ids[str(create_attempt_id)] = agent_id
                    creator._redirect_urls[str(create_attempt_id)] = f"/goto/{agent_id}/"

                log_sink.put("[test] Agent created successfully.")
                log_sink.put(LOG_SENTINEL)

                logger.info("CreateAttempt done, waiting for the ready state...")
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
