"""Visual diff harness for the minds SPA.

``capture-spa`` builds the frontend bundle, renders the SPA index page per
route with a deterministic fixture ``UiBootstrap`` (built from the real wire
models in ``ui_models.py`` so fixtures can never drift from the schema),
serves each route at its REAL path (the SPA router reads
``location.pathname``), and screenshots every route. ``compare`` then diffs
two captures side by side. Intended as a local sanity tool for UI changes --
not wired into CI.

Outputs land at ``apps/minds/.visual-diff/<label>/`` (gitignored).

Typical use:

    # On main:
    git checkout main
    uv run apps/minds/scripts/visual_diff.py capture-spa --label spa-main

    # On the feature branch:
    git checkout your-branch
    uv run apps/minds/scripts/visual_diff.py capture-spa --label spa-branch

    # Compare:
    uv run apps/minds/scripts/visual_diff.py compare spa-main spa-branch
    open apps/minds/.visual-diff/report-spa-main-vs-spa-branch.html

``capture-spa`` writes:
    apps/minds/.visual-diff/<label>/html/spa_<route-slug>.html
    apps/minds/.visual-diff/<label>/png/spa_<route-slug>.png

``compare`` writes a single ``report-<a>-vs-<b>.html`` with a side-by-side
table of screenshots + a per-scenario verdict (HTML structural diff +
pixel diff threshold).
"""

import argparse
import difflib
import html
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import zlib
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from imbue.imbue_common.logging import setup_logging
from imbue.minds.desktop_client.discovery_health import DiscoveryHealth
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.ui_api import read_vite_entry_tags
from imbue.minds.desktop_client.ui_models import ProviderPanelStatus
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiAccountsMessage
from imbue.minds.desktop_client.ui_models import UiBootstrap
from imbue.minds.desktop_client.ui_models import UiBootstrapSeed
from imbue.minds.desktop_client.ui_models import UiDiscoveryHealthMessage
from imbue.minds.desktop_client.ui_models import UiEnvironmentMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiNotificationEntry
from imbue.minds.desktop_client.ui_models import UiNotificationsMessage
from imbue.minds.desktop_client.ui_models import UiProviderEntry
from imbue.minds.desktop_client.ui_models import UiProvidersMessage
from imbue.minds.desktop_client.ui_models import UiRequestsMessage
from imbue.minds.desktop_client.ui_models import UiSnapshot
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage
from imbue.minds.errors import MindError


def _repo_root() -> Path:
    """Locate the repo root.

    We prefer ``git rev-parse --show-toplevel`` over a hardcoded relative
    path so the script keeps working when copied outside the tree (useful
    for capturing both sides of a branch swap from one stable location).
    """
    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path(__file__).resolve().parents[3]


REPO_ROOT: Final[Path] = _repo_root()
STATIC_DIR: Final[Path] = REPO_ROOT / "apps" / "minds" / "imbue" / "minds" / "desktop_client" / "static"
OUTPUT_ROOT: Final[Path] = REPO_ROOT / "apps" / "minds" / ".visual-diff"

VIEWPORT_W: Final[int] = 1440
VIEWPORT_H: Final[int] = 900

# Exceptions that Playwright can raise during navigation/screenshotting.
_PLAYWRIGHT_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    PlaywrightError,
    PlaywrightTimeoutError,
    OSError,
)


# -- Shared capture helpers -----------------------------------------------


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that silences the per-request access log.

    The serving directory is bound by the lambda factory passed to
    TCPServer in ``_serve_directory`` -- this subclass only overrides
    logging.
    """

    def log_message(self, fmt: str, *args: Any) -> None:  # ty: ignore[invalid-method-override]
        pass


def _serve_directory(root: Path) -> tuple[socketserver.TCPServer, threading.Thread, int]:
    """Spin up a daemon HTTP server rooted at ``root`` on a random free port.

    Playwright loads the rendered HTML over HTTP rather than file:// because
    pages reference ``/_static/...`` as root-absolute paths; file:// breaks
    those references silently. The server runs until process exit.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    httpd = socketserver.TCPServer(
        ("127.0.0.1", port),
        lambda *args, **kwargs: _QuietStaticHandler(*args, directory=str(root), **kwargs),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, port


def _launch_chromium(pw: Any) -> tuple[Any, bool]:
    """Launch Playwright's managed chromium, falling back to a system binary.

    Playwright's downloaded chromium is unavailable on some host OS versions
    (it refuses to install); a system chromium at a well-known path (or named
    via ``MINDS_VISUAL_DIFF_CHROMIUM``) keeps the harness usable there. The
    fallback launches with ``--no-sandbox`` because system chromiums in
    containers/CI often lack the sandbox helper.

    Returns ``(browser, is_full_page_reliable)``: system chromium builds have
    been observed to paint only the first viewport of ``full_page=True``
    screenshots (blank below the fold), so fallback captures must use the
    viewport-resize path in :func:`_capture_full_page_screenshot` instead.
    """
    try:
        return pw.chromium.launch(), True
    except PlaywrightError as exc:
        candidates = [
            p
            for p in (
                os.getenv("MINDS_VISUAL_DIFF_CHROMIUM"),
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/usr/bin/google-chrome",
            )
            if p is not None and Path(p).exists()
        ]
        if not candidates:
            raise
        logger.info("[capture] managed chromium unavailable ({}); using {}", type(exc).__name__, candidates[0])
        return pw.chromium.launch(executable_path=candidates[0], args=["--no-sandbox"]), False


# Upper bound for the viewport-resize screenshot path; pages taller than this
# are clipped rather than ballooning the render surface.
_MAX_SCREENSHOT_HEIGHT: Final[int] = 12000


def _capture_full_page_screenshot(page: Any, path: Path, is_full_page_reliable: bool) -> None:
    """Full-page screenshot that also works on system chromium builds.

    The managed browser stitches ``full_page=True`` correctly; the system
    fallback paints only the first viewport, so there we resize the viewport
    to the document height, shoot without ``full_page``, and restore.
    """
    if is_full_page_reliable:
        page.screenshot(path=str(path), full_page=True)
        return
    document_height = int(page.evaluate("document.body.scrollHeight"))
    clamped_height = max(VIEWPORT_H, min(document_height, _MAX_SCREENSHOT_HEIGHT))
    page.set_viewport_size({"width": VIEWPORT_W, "height": clamped_height})
    try:
        page.screenshot(path=str(path))
    finally:
        page.set_viewport_size({"width": VIEWPORT_W, "height": VIEWPORT_H})


# -- SPA capture subcommand ------------------------------------------------

# Default SPA routes to capture, with a stable slug per route (scenario name
# is ``spa_<slug>``). One place, overridable via --routes. Stub pages are
# legitimate baselines: a screenshot of the placeholder is the current truth.
SPA_ROUTE_SLUGS: Final[tuple[tuple[str, str], ...]] = (
    ("/", "home"),
    ("/create", "create"),
    ("/settings", "settings"),
    ("/accounts", "accounts"),
    ("/workspaces/destroyed", "workspaces_destroyed"),
    ("/help", "help"),
    ("/welcome", "welcome"),
    ("/_dev/styleguide", "dev_styleguide"),
)

FRONTEND_DIR: Final[Path] = REPO_ROOT / "apps" / "minds" / "frontend"


def _slug_for_spa_route(route: str) -> str:
    if route == "/":
        return "home"
    return re.sub(r"[^a-z0-9]+", "_", route.strip("/").lower()).strip("_")


def _build_spa_fixture_bootstrap() -> UiBootstrap:
    """A deterministic bootstrap document exercising the main visual states.

    Built from the real wire models so the fixture can never drift from the
    schema -- a breaking model change fails this builder at capture time.
    """
    workspaces = (
        UiWorkspaceEntry(
            id="agent-00000000000000000000000000000001",
            name="alpha",
            accent="#7c9885",
            host_id="host-00000000000000000000000000000001",
            supports_shutdown=True,
            liveness="RUNNING",
            account="alice@example.com",
        ),
        UiWorkspaceEntry(
            id="agent-00000000000000000000000000000002",
            name="beta",
            accent="#8a7ca8",
            host_id="host-00000000000000000000000000000002",
            supports_shutdown=True,
            liveness="STOPPED",
        ),
        UiWorkspaceEntry(
            id="create-attempt-00000000000000000000000000000001",
            name="gamma",
            accent="#a88a7c",
            create_attempt_state="creating",
        ),
        UiWorkspaceEntry(
            id="agent-00000000000000000000000000000004",
            name="delta-remote",
            accent="#7c88a8",
            is_remote=True,
            location="Alice's laptop",
        ),
    )
    snapshot = UiSnapshot(
        workspaces=UiWorkspacesMessage(
            workspaces=workspaces,
            destroying_agent_ids=(),
            restorable_workspace_ids=(
                "agent-00000000000000000000000000000001",
                "host-00000000000000000000000000000001",
            ),
            remote_workspace_states={"agent-00000000000000000000000000000004": ""},
        ),
        accounts=UiAccountsMessage(has_accounts=True, account_email="alice@example.com", extra_account_count=1),
        providers=UiProvidersMessage(
            providers=(
                UiProviderEntry(name="local", backend="local", status=ProviderPanelStatus.OK, is_enabled=True),
                UiProviderEntry(
                    name="modal",
                    backend=None,
                    status=ProviderPanelStatus.ERROR,
                    is_enabled=True,
                    error_type="AuthError",
                    error_message="Modal token expired.",
                ),
            ),
            last_event_at="2026-01-01T00:00:00+00:00",
            last_full_snapshot_at="2026-01-01T00:00:00+00:00",
        ),
        requests=UiRequestsMessage(
            count=2,
            request_ids=(
                "req-00000000000000000000000000000001",
                "req-00000000000000000000000000000002",
            ),
        ),
        notifications=UiNotificationsMessage(
            entries=(
                UiNotificationEntry(
                    id="req-00000000000000000000000000000001",
                    created_at="2026-01-01T00:00:00+00:00",
                    is_resolved=False,
                    outcome=None,
                    title="Sign in to Slack",
                    body="alpha wants to connect your Slack account.",
                    request_id="req-00000000000000000000000000000001",
                    workspace_agent_id="agent-00000000000000000000000000000001",
                    workspace_name="alpha",
                    workspace_accent="#7c9885",
                    service_name="slack",
                ),
            ),
            unresolved_count=1,
        ),
        health=(UiHealthMessage(agent_id="agent-00000000000000000000000000000002", status=AgentHealth.STUCK),),
        discovery_health=UiDiscoveryHealthMessage(state=DiscoveryHealth.HEALTHY),
        # The baseline every route is captured against, so no device condition:
        # seeding one would put a notice band over every machine page in every
        # capture. Flip it to see the two environment states.
        environment=UiEnvironmentMessage(state=EnvironmentBlock.NONE),
    )
    seed = UiBootstrapSeed(accent="#7c9885", is_mac=True, mngr_forward_origin="https://localhost:8421")
    return UiBootstrap(seed=seed, schema_version=UI_SCHEMA_VERSION, snapshot=snapshot)


def _render_spa_index_html(bootstrap: UiBootstrap) -> str:
    """The SPA index page for a capture, mirroring ``ui_api.serve_spa_index``.

    Keep this shape in sync with the real handler (same bootstrap inline, the
    embed-contract script before the entry tags -- the shell consumes
    ``window.MindsEmbedContract`` at module-evaluation time -- and the same
    entry-tag source via ``read_vite_entry_tags``); the tag builder is
    imported so hashed asset names always come from the live manifest.
    """
    entry_tags = read_vite_entry_tags()
    if entry_tags is None:
        raise SystemExit("frontend bundle missing after build; check pnpm output")
    bootstrap_json = bootstrap.model_dump_json().replace("</", "<\\/")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "  <head>\n"
        '    <meta charset="utf-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "    <title>minds</title>\n"
        f"    <script>window.__MINDS_BOOTSTRAP__ = {bootstrap_json};</script>\n"
        '    <script src="/_static/embed_contract.js"></script>\n'
        f"    {entry_tags}\n"
        "  </head>\n"
        '  <body><div id="app"></div></body>\n'
        "</html>\n"
    )


def _build_spa_bundle() -> None:
    """Build frontend/ -> static/ui/ so the capture reflects this branch's source."""
    minds_dir = REPO_ROOT / "apps" / "minds"
    logger.info("[capture-spa] building the frontend bundle (pnpm install/generate/build)")
    # install + generate first: node_modules/ and src/generated/ are both
    # gitignored, so a bare pnpm build on a fresh checkout dies inside tsc
    # with an opaque unresolved-module error. Same sequence as hatch_build.py
    # and scripts/snapshot_minds_e2e_state.py.
    subprocess.run(
        [
            "bash",
            "-c",
            ". scripts/select_node_version.sh && cd frontend"
            " && pnpm install --frozen-lockfile && pnpm generate && pnpm build",
        ],
        cwd=str(minds_dir),
        check=True,
    )


class SpaCaptureWiringError(MindError, TypeError):
    """Raised when the SPA route handler is mounted on a non-capture server."""

    ...


class _SpaCaptureServer(socketserver.TCPServer):
    """TCPServer that carries the SPA capture's route map + serving root.

    Attributes are assigned right after construction (no __init__ override);
    the handler reaches them through ``self.server`` with a cast, the typed
    idiom for http.server's handler/server split.
    """

    spa_html_by_route: dict[str, Path]
    spa_root_dir: Path


class _SpaRouteHandler(_QuietStaticHandler):
    """Serves the SPA index at each route's REAL path.

    The SPA router reads ``location.pathname``, so ``/create`` must be served
    at ``/create`` (not ``/html/spa_create.html``); everything else falls back
    to static file serving rooted at the capture dir (``/_static/...``). The
    route map and serving root ride on the :class:`_SpaCaptureServer`
    instance. ``do_GET``'s name is the http.server API.
    """

    def do_GET(self) -> None:
        server = self.server
        if not isinstance(server, _SpaCaptureServer):
            raise SpaCaptureWiringError(f"SPA route handler mounted on a non-capture server: {type(server)!r}")
        capture_server = server
        # SimpleHTTPRequestHandler reads ``self.directory`` at request time;
        # binding it here (rather than a per-request factory) keeps the
        # handler class module-level.
        self.directory = str(capture_server.spa_root_dir)
        html_by_route = capture_server.spa_html_by_route
        route = self.path.split("?", 1)[0]
        html_path = html_by_route.get(route)
        if html_path is None:
            super().do_GET()
            return
        body = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _screenshot_spa_routes(route_slugs: list[tuple[str, str]], png_dir: Path, port: int) -> None:
    """Screenshot every SPA route once the app has mounted."""
    with sync_playwright() as pw:
        browser, is_full_page_reliable = _launch_chromium(pw)
        try:
            context = browser.new_context(
                viewport={"width": VIEWPORT_W, "height": VIEWPORT_H}, reduced_motion="reduce"
            )
            page = context.new_page()
            for route, slug in route_slugs:
                # ``visual-diff=1`` marks the run so the shell can suppress
                # nondeterministic chrome (e.g. a reconnecting indicator once
                # the channel client exists).
                separator = "&" if "?" in route else "?"
                target = f"http://127.0.0.1:{port}{route}{separator}visual-diff=1"
                try:
                    page.goto(target, wait_until="load", timeout=15000)
                    # Mounted = the app root has children. Stub pages count;
                    # an empty app root means the bundle crashed, which times
                    # out here and logs as a shot failure.
                    page.wait_for_function(
                        "() => { const el = document.getElementById('app'); return !!el && el.children.length > 0; }",
                        timeout=10000,
                    )
                    _capture_full_page_screenshot(page, png_dir / f"spa_{slug}.png", is_full_page_reliable)
                    logger.info("[shot] spa_{}", slug)
                except _PLAYWRIGHT_EXCEPTIONS as exc:
                    logger.opt(exception=exc).warning("[shot fail] spa_{}: {}", slug, type(exc).__name__)
        finally:
            browser.close()


def _do_capture_spa(label: str, routes: list[str] | None, is_build_skipped: bool) -> Path:
    output_dir = OUTPUT_ROOT / label
    html_dir = output_dir / "html"
    png_dir = output_dir / "png"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    html_dir.mkdir(parents=True)
    png_dir.mkdir(parents=True)

    if not is_build_skipped:
        _build_spa_bundle()

    # Same serving convention as the legacy mode: /_static resolves through a
    # symlink into the live static dir (which now also contains ui/).
    (output_dir / "_static").symlink_to(STATIC_DIR)

    route_slugs = [(r, _slug_for_spa_route(r)) for r in routes] if routes else [(r, s) for r, s in SPA_ROUTE_SLUGS]
    bootstrap = _build_spa_fixture_bootstrap()
    index_html = _render_spa_index_html(bootstrap)

    # One HTML artifact per route (identical bodies today, but per-route files
    # keep the compare machinery's scenario model untouched and leave room for
    # per-route bootstrap variations later).
    html_by_route: dict[str, Path] = {}
    for route, slug in route_slugs:
        html_path = html_dir / f"spa_{slug}.html"
        html_path.write_text(index_html)
        html_by_route[route] = html_path

    logger.info("[capture-spa] capturing {} routes -> {}", len(route_slugs), output_dir)
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    httpd = _SpaCaptureServer(("127.0.0.1", port), _SpaRouteHandler)
    httpd.spa_html_by_route = html_by_route
    httpd.spa_root_dir = output_dir
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        _screenshot_spa_routes(route_slugs, png_dir, port)
    finally:
        httpd.shutdown()
        httpd.server_close()

    logger.info("[capture-spa] done: {}", output_dir)
    return output_dir


# -- Compare subcommand --------------------------------------------------


def _read_png_dimensions(path: Path) -> tuple[int, int] | None:
    """Read width/height from a PNG header without a full image library."""
    try:
        with path.open("rb") as fh:
            sig = fh.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return None
            # Skip the IHDR length field.
            fh.read(4)
            if fh.read(4) != b"IHDR":
                return None
            w, h = struct.unpack(">II", fh.read(8))
            return w, h
    except OSError:
        return None


def _hash_bytes(path: Path) -> int:
    """Cheap content fingerprint -- adler32 of the file bytes."""
    try:
        return zlib.adler32(path.read_bytes())
    except OSError:
        return 0


def _collapse_whitespace(s: str) -> str:
    """Collapse all whitespace runs to a single space and strip."""
    return re.sub(r"\s+", " ", s.strip())


def _structural_html_diff(left_html: str, right_html: str) -> str | None:
    """Return None if the two HTML strings are equivalent enough; else a
    short summary of how they differ.

    We normalize whitespace (collapse runs to a single space) and compare.
    This catches missing/added elements, attribute drift, and changed text
    content without flagging whitespace-only cosmetic churn between captures.
    """
    nl = _collapse_whitespace(left_html)
    nr = _collapse_whitespace(right_html)
    if nl == nr:
        return None
    # Produce a short unified diff for the report. Split on > to keep
    # lines short and aligned with HTML structure.
    left_lines = nl.replace(">", ">\n").splitlines()
    right_lines = nr.replace(">", ">\n").splitlines()
    diff = difflib.unified_diff(left_lines, right_lines, lineterm="", n=2)
    # Bound the report size at 80 diff lines.
    summary = "\n".join(list(diff)[:80])
    if not summary:
        summary = "(differs only in whitespace position; both normalize equal)"
    return summary


def _classify_verdict(png_present: bool, png_identical: bool, html_diff: str | None) -> str:
    """Decide the per-scenario verdict.

    PNG hash beats HTML diff because markup changes can legitimately
    reshuffle whitespace or swap literal Unicode for HTML entities
    (``--`` vs ``&mdash;``), both of which render identically.
    """
    if png_identical:
        # PNGs are byte-identical, so the rendered pixels match;
        # ignore any HTML cosmetic differences.
        return "ok"
    if png_present:
        # PNGs differ pixel-for-pixel -- a real visual regression.
        return "differs"
    if html_diff is None:
        # No PNGs (browser pass skipped) but the HTML normalizes equal.
        return "cosmetic"
    # No PNGs and the normalized HTML disagrees.
    return "differs"


def _do_compare(label_a: str, label_b: str) -> Path:
    """Compare two captures.

    Verdict priority:

    - ``ok``: PNG bytes identical (truly visually identical). HTML may
      differ in whitespace, entity encoding, attribute ordering -- those
      are cosmetic and shown for reference only.
    - ``cosmetic``: PNGs missing (browser pass skipped) but HTML
      normalizes to the same tree.
    - ``differs``: PNGs differ pixel-for-pixel, OR PNGs missing and HTML
      normalizes differently.
    - ``missing_in_a`` / ``missing_in_b``: scenario only present in one
      capture.
    """
    dir_a = OUTPUT_ROOT / label_a
    dir_b = OUTPUT_ROOT / label_b
    if not dir_a.exists():
        raise SystemExit(f"capture directory missing: {dir_a}")
    if not dir_b.exists():
        raise SystemExit(f"capture directory missing: {dir_b}")

    html_a = sorted((dir_a / "html").glob("*.html"))
    html_b_names = {p.name for p in (dir_b / "html").glob("*.html")}

    rows: list[dict[str, Any]] = []
    for path_a in html_a:
        name = path_a.stem
        path_b = dir_b / "html" / path_a.name
        if path_a.name not in html_b_names:
            rows.append({"name": name, "verdict": "missing_in_b"})
            continue
        html_left = path_a.read_text()
        html_right = path_b.read_text()
        html_diff = _structural_html_diff(html_left, html_right)

        png_a = dir_a / "png" / f"{name}.png"
        png_b = dir_b / "png" / f"{name}.png"
        png_present = png_a.exists() and png_b.exists()
        png_identical = png_present and _hash_bytes(png_a) == _hash_bytes(png_b)
        if not png_present:
            png_status = "missing"
        elif png_identical:
            png_status = "identical"
        else:
            dim_a = _read_png_dimensions(png_a)
            dim_b = _read_png_dimensions(png_b)
            png_status = "differ" if dim_a == dim_b else f"differ ({dim_a} vs {dim_b})"

        verdict = _classify_verdict(png_present, png_identical, html_diff)
        rows.append(
            {
                "name": name,
                "verdict": verdict,
                "html_diff": html_diff or "(structurally equivalent)",
                "png_status": png_status,
                "png_a_rel": f"{label_a}/png/{name}.png",
                "png_b_rel": f"{label_b}/png/{name}.png",
            }
        )

    # Scenarios that only exist in B (added on the feature branch).
    only_in_b = html_b_names - {p.name for p in html_a}
    for name_html in sorted(only_in_b):
        rows.append({"name": name_html.removesuffix(".html"), "verdict": "missing_in_a"})

    report_path = OUTPUT_ROOT / f"report-{label_a}-vs-{label_b}.html"
    report_path.write_text(_render_report(label_a, label_b, rows))
    n_ok = sum(1 for r in rows if r["verdict"] == "ok")
    n_cosmetic = sum(1 for r in rows if r["verdict"] == "cosmetic")
    n_differs = sum(1 for r in rows if r["verdict"] == "differs")
    n_missing = sum(1 for r in rows if r["verdict"].startswith("missing"))
    logger.info(
        "[compare] {} pixel-identical / {} html-cosmetic / {} differ / {} missing",
        n_ok,
        n_cosmetic,
        n_differs,
        n_missing,
    )
    logger.info("[compare] report: {}", report_path)
    return report_path


# CSS for the report page. Lives at module scope as a triple-quoted
# string rather than a list of per-line ``"..."`` entries inside
# ``_render_report`` so the source-file lines that contain a leading
# CSS id selector (a ``#`` after some whitespace) don't trip the
# ``trailing-comments`` ratchet -- the regex treats a leading ``"``
# as code and a later ``#`` as the start of a trailing comment, which
# is correct for Python but wrong for CSS-inside-a-string.
_REPORT_CSS: Final[str] = """
body { font: 14px -apple-system, system-ui, sans-serif; margin: 24px; color: #18181b; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #e4e4e7; padding: 8px; vertical-align: top; text-align: left; }
th { background: #fafafa; position: sticky; top: 0; }
td.shots { width: 50%; }
td.shots .thumb { display: block; cursor: zoom-in; background: none; border: 1px solid #d4d4d8; padding: 0; width: 100%; }
td.shots .thumb img { display: block; max-width: 100%; }
td.shots .thumb:focus { outline: 2px solid #2563eb; outline-offset: 2px; }
pre { background: #fafafa; padding: 8px; overflow: auto; max-height: 280px; font-size: 12px; }
.verdict-ok { color: #047857; font-weight: 600; }
.verdict-cosmetic { color: #525252; font-weight: 600; }
.verdict-differs { color: #b91c1c; font-weight: 600; }
.verdict-missing { color: #92400e; font-weight: 600; }
#lightbox { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: none;
  flex-direction: column; z-index: 1000; padding: 16px; }
#lightbox.open { display: flex; }
#lightbox-header { display: flex; align-items: center; gap: 16px; color: #fafafa;
  font: 13px -apple-system, system-ui, sans-serif; padding: 4px 8px; }
#lightbox-title { font-weight: 600; flex: 1; }
#lightbox-side { padding: 2px 8px; border-radius: 4px; background: rgba(255,255,255,0.15); font-family: ui-monospace, monospace; }
#lightbox-counter { color: #d4d4d8; }
#lightbox-close { background: none; border: 1px solid rgba(255,255,255,0.3); color: #fafafa;
  cursor: pointer; padding: 4px 10px; border-radius: 4px; font-size: 14px; }
#lightbox-close:hover { background: rgba(255,255,255,0.1); }
#lightbox-stage { flex: 1; display: flex; align-items: center; justify-content: center;
  overflow: auto; cursor: pointer; }
#lightbox-img { max-width: 100%; max-height: 100%; border: 1px solid rgba(255,255,255,0.2); }
#lightbox-hint { color: #a1a1aa; font-size: 12px; text-align: center; padding: 8px;
  font-family: ui-monospace, monospace; }
"""


def _render_report(label_a: str, label_b: str, rows: list[dict[str, Any]]) -> str:
    """Hand-rolled HTML report -- no template engine to keep the tool
    standalone (free of any template-engine dependency).

    Each thumbnail in the table opens a click-through lightbox: the
    lightbox shows one side at full size; clicking the image swaps to
    the other side; left/right arrow keys step between scenarios that
    actually differ (verdict ``differs``); Esc closes.
    """
    # Lightbox-eligible rows: only scenarios where both captures
    # exist (excludes ``missing_in_*``). The lightbox is most useful
    # for the ``differs`` rows, but we let ``cosmetic`` and ``ok`` in
    # too so users can spot-check anything that draws their eye.
    lightbox_rows = [r for r in rows if not r["verdict"].startswith("missing")]
    differs_indices = [i for i, r in enumerate(lightbox_rows) if r["verdict"] == "differs"]
    lightbox_payload = [
        {
            "name": r["name"],
            "verdict": r["verdict"],
            "src_a": r["png_a_rel"],
            "src_b": r["png_b_rel"],
        }
        for r in lightbox_rows
    ]

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>visual diff: {html.escape(label_a)} vs {html.escape(label_b)}</title>",
        "<style>",
        _REPORT_CSS,
        "</style></head><body>",
        f"<h1>visual diff: <code>{html.escape(label_a)}</code> vs <code>{html.escape(label_b)}</code></h1>",
        f"<p>"
        f"{sum(1 for r in rows if r['verdict'] == 'ok')} pixel-identical &middot; "
        f"{sum(1 for r in rows if r['verdict'] == 'cosmetic')} html-cosmetic &middot; "
        f"{sum(1 for r in rows if r['verdict'] == 'differs')} differ &middot; "
        f"{sum(1 for r in rows if r['verdict'].startswith('missing'))} missing &middot; "
        f"total {len(rows)}"
        f"</p>"
        f"<p style='font-size:13px;color:#525252'>"
        f"<strong>ok</strong> = PNG byte-identical. "
        f"<strong>cosmetic</strong> = no PNGs, HTML normalizes equal. "
        f"<strong>differs</strong> = real visual or structural difference. "
        f"Click a thumbnail to open the lightbox; click the lightbox image to swap "
        f"between A &amp; B; &larr; / &rarr; step between the {len(differs_indices)} "
        f"differing scenario(s); Esc closes."
        f"</p>",
        "<table><thead><tr>"
        "<th>scenario</th><th>verdict</th>"
        f"<th>{html.escape(label_a)} screenshot</th>"
        f"<th>{html.escape(label_b)} screenshot</th>"
        "<th>structural HTML diff</th>"
        "</tr></thead><tbody>",
    ]
    # Track lightbox index alongside table iteration so the data-* hook
    # on each thumbnail points to the right entry in the JS payload.
    lightbox_index = 0
    for row in rows:
        verdict = row["verdict"]
        if verdict == "ok":
            cls = "verdict-ok"
        elif verdict == "cosmetic":
            cls = "verdict-cosmetic"
        elif verdict.startswith("missing"):
            cls = "verdict-missing"
        else:
            cls = "verdict-differs"
        parts.append(f"<tr><td><code>{html.escape(row['name'])}</code></td>")
        parts.append(f"<td class='{cls}'>{html.escape(verdict)}</td>")
        if verdict.startswith("missing"):
            parts.append("<td colspan='3'>(scenario only in one capture)</td>")
        else:
            # Each thumbnail is a <button> so it picks up keyboard focus
            # and Enter activates it; data-lightbox-* tells the JS which
            # entry / side to open.
            parts.append(
                f"<td class='shots'>"
                f"<button class='thumb' type='button' data-lightbox-index='{lightbox_index}' data-lightbox-side='a'>"
                f"<img src='{html.escape(row['png_a_rel'])}' alt=''></button></td>"
            )
            parts.append(
                f"<td class='shots'>"
                f"<button class='thumb' type='button' data-lightbox-index='{lightbox_index}' data-lightbox-side='b'>"
                f"<img src='{html.escape(row['png_b_rel'])}' alt=''></button></td>"
            )
            parts.append(f"<td><div>png: <code>{html.escape(row['png_status'])}</code></div>")
            parts.append(f"<pre>{html.escape(row['html_diff'])}</pre></td>")
            lightbox_index += 1
        parts.append("</tr>")
    parts.append("</tbody></table>")

    # Lightbox overlay markup.
    parts.append(
        "<div id='lightbox' role='dialog' aria-hidden='true'>"
        "  <div id='lightbox-header'>"
        "    <span id='lightbox-title'></span>"
        "    <span id='lightbox-side'></span>"
        "    <span id='lightbox-counter'></span>"
        "    <button id='lightbox-close' type='button' aria-label='Close'>Close (Esc)</button>"
        "  </div>"
        "  <div id='lightbox-stage'><img id='lightbox-img' alt=''></div>"
        "  <div id='lightbox-hint'>click image to swap A &harr; B &middot; "
        "&larr; / &rarr; for next/previous differing scenario &middot; Esc to close</div>"
        "</div>"
    )

    # Lightbox JS. Payload is a data island so we can index into it
    # without escaping HTML attributes character-by-character.
    parts.append("<script id='lightbox-data' type='application/json'>")
    parts.append(json.dumps({"rows": lightbox_payload, "differs": differs_indices}))
    parts.append("</script>")
    parts.append(
        "<script>(function(){"
        "  var data = JSON.parse(document.getElementById('lightbox-data').textContent);"
        "  var rows = data.rows, differs = data.differs;"
        "  var lb = document.getElementById('lightbox');"
        "  var img = document.getElementById('lightbox-img');"
        "  var title = document.getElementById('lightbox-title');"
        "  var sideEl = document.getElementById('lightbox-side');"
        "  var counter = document.getElementById('lightbox-counter');"
        "  var labels = {a: " + json.dumps(label_a) + ", b: " + json.dumps(label_b) + "};"
        "  var idx = -1, side = 'a';"
        "  function show(i, s){"
        "    if (i < 0 || i >= rows.length) return;"
        "    idx = i; side = s;"
        "    var r = rows[i];"
        "    img.src = (s === 'a') ? r.src_a : r.src_b;"
        "    title.textContent = r.name + '  [' + r.verdict + ']';"
        "    sideEl.textContent = (s === 'a' ? 'A: ' : 'B: ') + labels[s];"
        "    var dpos = differs.indexOf(i);"
        "    counter.textContent = dpos >= 0"
        "      ? ('differs ' + (dpos + 1) + ' / ' + differs.length)"
        "      : ('scenario ' + (i + 1) + ' / ' + rows.length);"
        "    lb.classList.add('open');"
        "    lb.setAttribute('aria-hidden', 'false');"
        "  }"
        "  function close(){ lb.classList.remove('open'); lb.setAttribute('aria-hidden', 'true'); idx = -1; }"
        "  function swap(){ if (idx < 0) return; show(idx, side === 'a' ? 'b' : 'a'); }"
        # Step through differs first; if no differs, step through all rows.
        "  function step(delta){"
        "    if (idx < 0) return;"
        "    if (differs.length === 0){ show((idx + delta + rows.length) % rows.length, side); return; }"
        "    var dpos = differs.indexOf(idx);"
        "    if (dpos === -1){"
        # Currently on a non-differ row: jump to the nearest differ in the requested direction.
        "      var i = idx + delta;"
        "      while (i >= 0 && i < rows.length && differs.indexOf(i) === -1) i += delta;"
        "      if (i < 0 || i >= rows.length) i = delta > 0 ? differs[0] : differs[differs.length - 1];"
        "      show(i, side);"
        "      return;"
        "    }"
        "    var next = (dpos + delta + differs.length) % differs.length;"
        "    show(differs[next], side);"
        "  }"
        # Wire thumbnail clicks.
        "  document.querySelectorAll('.thumb').forEach(function(btn){"
        "    btn.addEventListener('click', function(){"
        "      show(parseInt(btn.dataset.lightboxIndex, 10), btn.dataset.lightboxSide);"
        "    });"
        "  });"
        "  document.getElementById('lightbox-close').addEventListener('click', close);"
        # Click on the image (or its stage) toggles side; click outside both closes.
        "  document.getElementById('lightbox-stage').addEventListener('click', function(){ swap(); });"
        # Background click on the lightbox itself (outside the stage) closes.
        "  lb.addEventListener('click', function(e){ if (e.target === lb) close(); });"
        "  document.addEventListener('keydown', function(e){"
        "    if (!lb.classList.contains('open')) return;"
        "    if (e.key === 'Escape'){ close(); return; }"
        "    if (e.key === 'ArrowLeft'){ step(-1); e.preventDefault(); return; }"
        "    if (e.key === 'ArrowRight'){ step(1); e.preventDefault(); return; }"
        # Up/down toggles A<->B as an alternative to clicking.
        "    if (e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === ' '){ swap(); e.preventDefault(); return; }"
        "  });"
        "})();</script>"
    )
    parts.append("</body></html>")
    return "".join(parts)


# -- main ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    setup_logging(level="INFO")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_capture_spa = sub.add_parser(
        "capture-spa", help="build the SPA bundle + screenshot every route with fixture data"
    )
    p_capture_spa.add_argument(
        "--label",
        required=True,
        help="output dir label (e.g. 'spa-main'); written to .visual-diff/<label>/",
    )
    p_capture_spa.add_argument(
        "--routes",
        default=None,
        help="comma-separated route paths to capture (default: the built-in SPA route list)",
    )
    p_capture_spa.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse the existing static/ui bundle instead of running pnpm build",
    )

    p_compare = sub.add_parser("compare", help="diff two captures, produce report.html")
    p_compare.add_argument("label_a", help="baseline label")
    p_compare.add_argument("label_b", help="comparison label")

    args = parser.parse_args(argv)

    if args.cmd == "capture-spa":
        routes = [r.strip() for r in args.routes.split(",") if r.strip()] if args.routes else None
        _do_capture_spa(args.label, routes, args.skip_build)
        return 0
    if args.cmd == "compare":
        _do_compare(args.label_a, args.label_b)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
