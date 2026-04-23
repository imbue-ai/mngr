import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger as loguru_logger
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
# one ("ScopeMismatch"). Overriding `browser` alone would leave
# `launch_browser` at session scope and trip that check.


@pytest.fixture
def playwright() -> Generator[Playwright, None, None]:
    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        # try/finally guards the node-driver subprocess against teardown-path
        # errors. The whole point of overriding this fixture to function scope
        # is to keep the driver out of session_cleanup's leaked-child check;
        # a mid-teardown error reaching the yield line without try/finally
        # would re-introduce that leak.
        pw.stop()


@pytest.fixture
def browser_type(playwright: Playwright) -> BrowserType:
    return playwright.chromium


@pytest.fixture
def browser_type_launch_args(pytestconfig: pytest.Config) -> dict[str, Any]:
    # Mirrors pytest-playwright's upstream browser_type_launch_args body
    # (see .venv/.../pytest_playwright/pytest_playwright.py `browser_type_launch_args`).
    # Do not add `device` here -- that's a context-level option consumed by
    # browser_context_args in upstream, not a valid kwarg for
    # browser_type.launch(), which would raise TypeError.
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
    return launch_options


@pytest.fixture
def connect_options() -> dict[str, Any] | None:
    return None


def _launch_playwright_browser(
    *,
    browser_type_launch_args: dict[str, Any],
    browser_type: BrowserType,
    connect_options: dict[str, Any] | None,
    **kwargs: Any,
) -> Browser:
    """Launch or connect to a playwright browser.

    Extracted as a top-level helper (rather than an inline closure inside
    :func:`launch_browser`) so the file satisfies the PREVENT_INLINE_FUNCTIONS
    ratchet. The fixture below wraps this with a lambda to bake in the
    fixture-provided arguments while still exposing a ``(**kwargs)`` entry
    point to callers, matching pytest-playwright's upstream API.
    """
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


@pytest.fixture
def launch_browser(
    browser_type_launch_args: dict[str, Any],
    browser_type: BrowserType,
    connect_options: dict[str, Any] | None,
) -> Callable[..., Browser]:
    # A lambda is the idiomatic way to bind the fixture values into a
    # callable here without tripping either the inline-functions ratchet
    # (which flags nested def statements) or the partial-function ratchet.
    return lambda **kwargs: _launch_playwright_browser(
        browser_type_launch_args=browser_type_launch_args,
        browser_type=browser_type,
        connect_options=connect_options,
        **kwargs,
    )


@pytest.fixture
def browser_context_args(
    pytestconfig: pytest.Config,
    playwright: Playwright,
    device: str | None,
    base_url: str | None,
    _pw_artifacts_folder: tempfile.TemporaryDirectory,
) -> dict[str, Any]:
    # Mirrors pytest-playwright's upstream browser_context_args, overridden
    # to function scope because it transitively depends on `playwright`,
    # which we've pinned to function scope above. Without this override
    # pytest raises ScopeMismatch at setup time for every test that uses
    # `page` / `context` (i.e. the entire minds_workspace_server e2e suite).
    context_args: dict[str, Any] = {}
    if device:
        context_args.update(playwright.devices[device])
    if base_url:
        context_args["base_url"] = base_url
    video_option = pytestconfig.getoption("--video", default="off")
    if video_option in ("on", "retain-on-failure"):
        context_args["record_video_dir"] = _pw_artifacts_folder.name
    return context_args


@pytest.fixture
def browser(launch_browser: Callable[..., Browser]) -> Generator[Browser, None, None]:
    browser_instance = launch_browser()
    try:
        yield browser_instance
    finally:
        # try/finally guards chrome-headless-shell against teardown-path
        # errors. Matches the rationale on the `playwright` fixture above:
        # without it a mid-teardown error can leak the browser subprocess
        # into session_cleanup's leaked-child check.
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
def false_binary() -> str:
    """Cross-platform path to a binary that exits immediately with failure.

    Used by tests that exercise the observe watchdog's error path without
    relying on a real mngr installation.
    """
    path = shutil.which("false")
    assert path is not None, "Could not find 'false' binary on this system"
    return path


@pytest.fixture
def loguru_records() -> Iterator[list[str]]:
    """Capture loguru log messages as plain strings for test assertions.

    Each entry in the yielded list is a ``"<LEVEL> <message>"`` line, so tests
    can filter on both level and text without wiring up loguru into pytest's
    stdlib-oriented ``caplog``.
    """
    messages: list[str] = []
    handler_id = loguru_logger.add(
        lambda msg: messages.append(f"{msg.record['level'].name} {msg.record['message']}"),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


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
