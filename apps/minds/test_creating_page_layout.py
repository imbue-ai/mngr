"""The creating page fits the window instead of growing a scrollbar.

The loading screen shown while a machine is created is laid out to shrink
rather than scroll: the progress block takes the height it needs (the log
panel, when open, being most of it) and the walkthrough's illustration gives
up the difference. Nothing but a browser can tell whether that budget actually
balances, so this drives a real one at the window sizes the app can be at --
the Electron default and its minimum (``electron/main.js``) -- and at each one
reads the scroll geometry of the shell's scroll container.

Marked ``release`` because it needs a Playwright browser, which only the
release CI job installs (same reason as ``test_sse_redirect.py``).

Run from the repo root. The bundle has to exist (the tests skip otherwise), and
PLAYWRIGHT_BROWSERS_PATH has to be fixed to a real location because the minds
autouse fixture re-points HOME at a per-test tmpdir, hiding the default one:

    (cd apps/minds/frontend && pnpm build)
    PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright" \\
        just test apps/minds/test_creating_page_layout.py

(that path is the macOS one; on Linux it is ``$HOME/.cache/ms-playwright``.)
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import CreateAttemptLogSink
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.ui_api import read_vite_entry_tags
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.utils.testing import find_free_port

# The window the app opens at, a size in between, and the smallest the user can
# drag it to -- all three from ``buildBundleWindowOptions`` in electron/main.js.
DEFAULT_WINDOW_SIZE: Final[tuple[int, int]] = (1200, 800)
SUPPORTED_WINDOW_SIZES: Final[tuple[tuple[int, int], ...]] = (DEFAULT_WINDOW_SIZE, (1000, 700), (800, 600))

# Enough lines that the log panel is at its full height, which is the case that
# leaves the walkthrough least room.
LOG_LINE_COUNT: Final[int] = 60

# Reading the layout back out of the page. The shell scrolls local pages inside
# #local-page-scroll (never the document), so that container's own overflow is
# the whole question. --gfx-scale is what the walkthrough publishes for the
# scene to draw itself at.
MEASURE_LAYOUT_JS: Final[str] = """
() => {
  const scroller = document.querySelector('#local-page-scroll');
  const graphic = document.querySelector('#graphic');
  const scene = document.querySelector('.gfx');
  const height = (element) => (element === null ? 0 : element.getBoundingClientRect().height);
  return {
    has_scroller: scroller !== null,
    overflow_px: scroller === null ? 0 : scroller.scrollHeight - scroller.clientHeight,
    graphic_height_px: height(graphic),
    scene_height_px: height(scene),
    scene_scale: graphic === null
      ? 0
      : Number(getComputedStyle(graphic).getPropertyValue('--gfx-scale') || '1'),
  };
}
"""


@contextmanager
def _creating_page_in_browser(tmp_path: Path) -> Iterator[Page]:
    """The /creating page of a real desktop client, open in a real browser.

    The create attempt is registered directly on the creator (as
    test_sse_redirect.py does) rather than started for real: the page only has
    to stay in flight while the layout is measured, and a real create would
    either need a machine or fail out from under the walkthrough.
    """
    # Editable installs (uv sync, CI) skip the wheel's frontend build hook, so
    # the bundle is only there if someone built it. Without it the SPA cannot
    # mount and there is no layout to measure -- which is a missing artifact,
    # not a failing page.
    if read_vite_entry_tags() is None:
        pytest.skip("no frontend bundle to measure; run `pnpm build` in apps/minds/frontend first")

    host = "127.0.0.1"
    port = find_free_port()
    code = OneTimeCode("test-creating-layout-code")
    paths = InstallationPaths(data_dir=tmp_path)
    auth_store = FileAuthStore(data_directory=paths.auth_dir)
    auth_store.add_one_time_code(code=code)

    with ConcurrencyGroup(name="test-creating-page-layout") as root_concurrency_group:
        creator = AgentCreator(
            paths=paths,
            root_concurrency_group=root_concurrency_group,
            notification_dispatcher=NotificationDispatcher.create(
                is_electron=False, tkinter_module=None, is_macos=False
            ),
            system_interface_health_tracker=SystemInterfaceHealthTracker(),
        )
        create_attempt_id = CreateAttemptId()
        log_sink = CreateAttemptLogSink()
        with creator._lock:
            creator._statuses[str(create_attempt_id)] = AgentCreateAttemptStatus.INITIALIZING
            creator._launch_modes[str(create_attempt_id)] = LaunchMode.DOCKER
            creator._host_names[str(create_attempt_id)] = "layout-test-workspace"
            creator._log_sinks[str(create_attempt_id)] = log_sink
        for line_number in range(LOG_LINE_COUNT):
            log_sink.put(f"[layout-test] create attempt log line {line_number}")

        app = create_desktop_client(
            auth_store=auth_store,
            backend_resolver=MngrCliBackendResolver(),
            http_client=None,
            agent_creator=creator,
            paths=paths,
        )
        # make_server binds and listens before it returns, so a connection made
        # before the serving thread gets going just waits in the backlog: there
        # is nothing to poll for here.
        server = make_server(host, port, app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    width, height = DEFAULT_WINDOW_SIZE
                    page = browser.new_page(viewport={"width": width, "height": height})
                    page.goto(f"http://{host}:{port}/login?one_time_code={code}")
                    page.wait_for_url(f"http://{host}:{port}/", timeout=10000)
                    page.goto(f"http://{host}:{port}/creating/{create_attempt_id}")
                    # The walkthrough is the part that has to fit, so wait for it.
                    page.wait_for_selector("#onboarding", state="visible", timeout=10000)
                    _settle_layout(page)
                    yield page
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server_thread.join(timeout=5)


def _settle_layout(page: Page) -> None:
    """Wait out the frame in which the illustration's scale catches up.

    The scale is published by a ResizeObserver, which the browser delivers
    before the next paint, so two animation frames is the whole wait -- no
    sleeping, and nothing timing-dependent to be flaky about.
    """
    page.evaluate("() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def _measure_layout(page: Page) -> dict[str, Any]:
    measurements: dict[str, Any] = page.evaluate(MEASURE_LAYOUT_JS)
    assert measurements["has_scroller"], "the shell's local-page scroll container is not on the page"
    return measurements


def _set_details_open(page: Page, is_open: bool) -> None:
    page.click("text=Show details" if is_open else "text=Hide details")
    page.wait_for_selector("#logs", state="visible" if is_open else "detached", timeout=5000)
    _settle_layout(page)


@pytest.mark.release
def test_creating_page_never_scrolls_at_any_supported_window_size(tmp_path: Path) -> None:
    """No window the app can be at makes the loading screen scroll, logs open or closed."""
    with _creating_page_in_browser(tmp_path) as page:
        for width, height in SUPPORTED_WINDOW_SIZES:
            page.set_viewport_size({"width": width, "height": height})
            _settle_layout(page)
            closed = _measure_layout(page)
            assert closed["overflow_px"] == 0, f"the creating page scrolls at {width}x{height} with the logs closed"
            _set_details_open(page, is_open=True)
            opened = _measure_layout(page)
            assert opened["overflow_px"] == 0, f"the creating page scrolls at {width}x{height} with the logs open"
            _set_details_open(page, is_open=False)


@pytest.mark.release
def test_creating_page_shrinks_the_illustration_to_make_room_for_the_logs(tmp_path: Path) -> None:
    """Opening the logs takes the room out of the illustration, not out of the page.

    The illustration still has to be drawn: giving up the space by removing the
    picture (or by clipping the page) would keep the scrollbar away too, and is
    not what the layout is supposed to do.
    """
    with _creating_page_in_browser(tmp_path) as page:
        width, height = DEFAULT_WINDOW_SIZE
        page.set_viewport_size({"width": width, "height": height})
        _settle_layout(page)

        closed = _measure_layout(page)
        assert closed["scene_scale"] == pytest.approx(1.0), "the illustration is not full size in a window with room"

        _set_details_open(page, is_open=True)
        opened = _measure_layout(page)
        assert opened["graphic_height_px"] < closed["graphic_height_px"], "the illustration kept its full height"
        assert 0 < opened["scene_scale"] < 1.0, "the illustration is not drawn at the height it was left"
        assert opened["scene_height_px"] > 0, "the illustration is gone rather than smaller"
