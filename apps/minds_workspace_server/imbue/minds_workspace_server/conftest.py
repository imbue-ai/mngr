import json
import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import BrowserType
from playwright.sync_api import Playwright
from playwright.sync_api import sync_playwright

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster

# --- pytest-playwright fixture-scope overrides -------------------------------
#
# pytest-playwright (installed as a plugin) ships these fixtures at SESSION
# scope: `playwright` (the sync_playwright handle, which spawns the node
# driver subprocess), `browser_type`, `browser_type_launch_args`, `connect_options`,
# `launch_browser`, and `browser` (the actual chromium/firefox process).
# Session-scope means teardown runs at pytest session end -- AFTER mngr's
# autouse `session_cleanup` fixture (libs/mngr/imbue/mngr/conftest.py) has
# already checked for leaked child processes. In offload release batches
# that mix workspace-server e2e tests with other mngr tests, both the
# playwright node driver and chrome-headless-shell are still alive when
# session_cleanup runs, so it asserts "leftover child processes" and
# cascades a teardown error into every sibling test in the batch
# (test_install.py, test_help.py, test_release_vultr, etc.).
#
# The fix is to force the entire fixture chain down to function scope so
# each test's playwright+chrome teardown finishes inside its own pytest
# teardown. Cost: a second or so per test to re-spawn the driver+browser;
# trivial for the tiny e2e suite here.
#
# All five session-scoped fixtures must be overridden together because
# pytest forbids a session-scope fixture from depending on a function-scope
# one ("ScopeMismatch"). Only overriding `browser` while leaving
# `launch_browser` at session scope produced exactly that error in release
# run 15.


@pytest.fixture
def playwright() -> Generator[Playwright, None, None]:
    pw = sync_playwright().start()
    yield pw
    pw.stop()


@pytest.fixture
def browser_type(playwright: Playwright) -> BrowserType:
    return playwright.chromium


@pytest.fixture
def browser_type_launch_args(pytestconfig: Any) -> dict[str, Any]:
    launch_options: dict[str, Any] = {}
    headed = pytestconfig.getoption("--headed", default=False)
    if headed:
        launch_options["headless"] = False
    browser_channel = pytestconfig.getoption("--browser-channel", default=None)
    if browser_channel:
        launch_options["channel"] = browser_channel
    slowmo = pytestconfig.getoption("--slowmo", default=0)
    if slowmo:
        launch_options["slow_mo"] = slowmo
    device = pytestconfig.getoption("--device", default=None)
    if device:
        launch_options["device"] = device
    return launch_options


@pytest.fixture
def connect_options() -> dict[str, Any] | None:
    return None


@pytest.fixture
def launch_browser(
    browser_type_launch_args: dict[str, Any],
    browser_type: BrowserType,
    connect_options: dict[str, Any] | None,
) -> Any:
    def launch(**kwargs: Any) -> Browser:
        launch_options = {**browser_type_launch_args, **kwargs}
        if connect_options:
            # Copied verbatim from pytest-playwright's upstream launch_browser
            # fixture. ty cannot verify the dynamic **connect_options spread
            # against connect's typed parameters (ws_endpoint: str, timeout,
            # headers, expose_network); the dict shape is dictated by
            # pytest-playwright's extension point for remote-browser use and
            # we mirror it exactly so downstream overrides stay compatible.
            return browser_type.connect(
                **{  # ty: ignore[invalid-argument-type]
                    **connect_options,
                    "headers": {
                        "x-playwright-launch-options": json.dumps(launch_options),
                        **(connect_options.get("headers") or {}),
                    },
                }
            )
        return browser_type.launch(**launch_options)

    return launch


@pytest.fixture
def browser(launch_browser: Any) -> Generator[Browser, None, None]:
    browser_instance = launch_browser()
    yield browser_instance
    browser_instance.close()


@pytest.fixture
def broadcaster() -> WebSocketBroadcaster:
    return WebSocketBroadcaster()


@pytest.fixture
def agent_manager(broadcaster: WebSocketBroadcaster, monkeypatch: pytest.MonkeyPatch) -> AgentManager:
    """Create an AgentManager without starting the observe subprocess."""
    monkeypatch.setenv("MNGR_AGENT_ID", "test-agent-id")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", "/tmp/test-work")
    return AgentManager.build(broadcaster)


@pytest.fixture
def git_work_dir(tmp_path: Path) -> Path:
    """Create a minimal git repository for tests that need a real git work directory."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        },
    )
    return tmp_path
