#!/usr/bin/env python3
"""Visual diff harness for desktop_client templates.

Captures rendered HTML + Playwright screenshots for every page under
``imbue.minds.desktop_client.templates`` (and friends), then compares
two captures side by side. Intended as a local sanity tool for
template-layer changes -- not wired into CI.

Outputs land at ``apps/minds/.visual-diff/<label>/`` (gitignored).

Typical use:

    # On main:
    git checkout main
    uv run apps/minds/scripts/visual_diff.py capture --label main

    # On the feature branch:
    git checkout gleb/jinjax
    uv run apps/minds/scripts/visual_diff.py capture --label jinjax

    # Compare:
    uv run apps/minds/scripts/visual_diff.py compare main jinjax
    open apps/minds/.visual-diff/report-main-vs-jinjax.html

``capture`` writes:
    apps/minds/.visual-diff/<label>/html/<scenario>.html
    apps/minds/.visual-diff/<label>/png/<scenario>.png

``compare`` writes a single ``report-<a>-vs-<b>.html`` with a side-by-side
table of screenshots + a per-scenario verdict (HTML structural diff +
pixel diff threshold).
"""

import argparse
import difflib
import html
import http.server
import json
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

import jinja2.exceptions
from loguru import logger
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
from imbue.minds.desktop_client.latchkey.handlers.templates import render_file_sharing_permission_dialog
from imbue.minds.desktop_client.latchkey.handlers.templates import render_predefined_permission_dialog
from imbue.minds.desktop_client.templates import render_accounts_page
from imbue.minds.desktop_client.templates import render_auth_error_page
from imbue.minds.desktop_client.templates import render_chrome_page
from imbue.minds.desktop_client.templates import render_create_form
from imbue.minds.desktop_client.templates import render_creating_page
from imbue.minds.desktop_client.templates import render_destroying_page
from imbue.minds.desktop_client.templates import render_dev_styleguide_page
from imbue.minds.desktop_client.templates import render_inbox_page
from imbue.minds.desktop_client.templates import render_inbox_unavailable_fragment
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.minds.desktop_client.templates import render_login_page
from imbue.minds.desktop_client.templates import render_login_redirect_page
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.templates import render_sidebar_page
from imbue.minds.desktop_client.templates import render_welcome_page
from imbue.minds.desktop_client.templates import render_workspace_settings
from imbue.minds.desktop_client.templates_auth import render_auth_page
from imbue.minds.desktop_client.templates_auth import render_check_email_page
from imbue.minds.desktop_client.templates_auth import render_forgot_password_page
from imbue.minds.desktop_client.templates_auth import render_oauth_close_page
from imbue.minds.desktop_client.templates_auth import render_settings_page
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo


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

# Exceptions that a template builder can plausibly raise during scenario
# rendering. The harness catches these so a single broken page doesn't
# abort the whole capture run; anything outside this set (e.g.
# KeyboardInterrupt, SystemExit, or a real programming defect like an
# AssertionError that wasn't anticipated) should still crash.
_BUILDER_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    TypeError,
    AttributeError,
    KeyError,
    ValueError,
    LookupError,
    RuntimeError,
    OSError,
    ImportError,
    jinja2.exceptions.TemplateError,
)

# Exceptions that Playwright can raise during navigation/screenshotting.
_PLAYWRIGHT_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    PlaywrightError,
    PlaywrightTimeoutError,
    OSError,
)


# -- Scenario catalog -----------------------------------------------------


class Scenario(FrozenModel):
    """One renderable state to capture.

    ``builder`` returns the HTML string. We pass it as a thunk (rather than
    pre-rendering) so import side-effects from a single branch never bleed
    into both sides of a comparison run.

    ``interactions`` is a list of Playwright actions to run BEFORE
    screenshotting, e.g. clicking a button to open a modal. Each action
    receives the active ``page`` object. Keep them small; complex driving
    belongs in dedicated tests.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    builder: Callable[[], str]
    interactions: tuple[Callable[[Any], None], ...] = Field(default=())


class _Account(FrozenModel):
    """Minimal account stub matching the attrs the templates read.

    The real Account model lives in the imbue_cloud client and pulling it
    in here would couple this tool to that whole subsystem.
    """

    user_id: str
    email: str
    workspace_ids: tuple[str, ...]


def _stub_account(user_id: str, email: str, n_workspaces: int = 0) -> _Account:
    return _Account(
        user_id=user_id,
        email=email,
        workspace_ids=tuple(f"agent-{i:032d}" for i in range(n_workspaces)),
    )


def _build_scenarios() -> list[Scenario]:
    agent_a = AgentId("agent-00000000000000000000000000000001")
    agent_b = AgentId("agent-00000000000000000000000000000002")
    agent_c = AgentId("agent-00000000000000000000000000000003")
    account_a = _stub_account("user-aaaaaa", "alice@example.com", n_workspaces=2)
    account_b = _stub_account("user-bbbbbb", "bob@example.com", n_workspaces=0)

    slack_service = ServicePermissionInfo(
        name="slack",
        scope="slack-api",
        display_name="Slack",
        description="Send and read messages in Slack channels.",
        permission_schemas=("any", "slack-read", "slack-write"),
        description_by_permission_name={
            "slack-read": "Read messages and channel listings.",
            "slack-write": "Send messages as the agent's bot user.",
        },
    )

    creation_info_running = AgentCreationInfo(
        creation_id=CreationId("creation-00000000000000000000000000000001"),
        status=AgentCreationStatus.CREATING_WORKSPACE,
        launch_mode=LaunchMode.DOCKER,
    )

    return [
        # -- Landing page ------------------------------------------------
        Scenario(name="landing_empty", builder=lambda: render_landing_page(accessible_agent_ids=())),
        Scenario(
            name="landing_discovering",
            builder=lambda: render_landing_page(accessible_agent_ids=(), is_discovering=True),
        ),
        Scenario(
            name="landing_with_workspaces",
            builder=lambda: render_landing_page(
                accessible_agent_ids=(agent_a, agent_b),
                mngr_forward_origin="http://localhost:8421",
                agent_names={str(agent_a): "alpha", str(agent_b): "beta"},
            ),
        ),
        Scenario(
            name="landing_with_destroying_workspace",
            builder=lambda: render_landing_page(
                accessible_agent_ids=(agent_a, agent_b, agent_c),
                mngr_forward_origin="http://localhost:8421",
                agent_names={str(agent_a): "alpha", str(agent_b): "beta-destroying", str(agent_c): "gamma-failed"},
                destroying_status_by_agent_id={str(agent_b): "running", str(agent_c): "failed"},
            ),
        ),
        # -- Welcome page ------------------------------------------------
        Scenario(name="welcome", builder=render_welcome_page),
        # -- Create form -------------------------------------------------
        Scenario(name="create_no_account", builder=lambda: render_create_form()),
        Scenario(
            name="create_with_account",
            builder=lambda: render_create_form(accounts=(account_a,), default_account_id="user-aaaaaa"),
        ),
        Scenario(
            name="create_with_error",
            builder=lambda: render_create_form(error_message="imbue_cloud requires an account."),
        ),
        Scenario(
            name="create_lima_subscription",
            builder=lambda: render_create_form(launch_mode=LaunchMode.LIMA, ai_provider=AIProvider.SUBSCRIPTION),
        ),
        Scenario(
            name="create_with_master_password",
            builder=lambda: render_create_form(
                backup_provider=BackupProvider.IMBUE_CLOUD,
                backup_encryption_method=BackupEncryptionMethod.MASTER_PASSWORD,
                has_saved_backup_password=False,
                accounts=(account_a,),
                default_account_id="user-aaaaaa",
            ),
        ),
        # -- Creating page (question flow + loading) ---------------------
        Scenario(
            name="creating",
            builder=lambda: render_creating_page(
                creation_id=CreationId("creation-00000000000000000000000000000001"),
                info=creation_info_running,
            ),
        ),
        # -- Destroying detail page --------------------------------------
        Scenario(
            name="destroying_running",
            builder=lambda: render_destroying_page(agent_id=agent_a, agent_name="alpha", pid=12345, status="running"),
        ),
        Scenario(
            name="destroying_failed",
            builder=lambda: render_destroying_page(agent_id=agent_a, agent_name="alpha", pid=12345, status="failed"),
        ),
        Scenario(
            name="destroying_done",
            builder=lambda: render_destroying_page(agent_id=agent_a, agent_name="alpha", pid=12345, status="done"),
        ),
        # -- Accounts page -----------------------------------------------
        Scenario(
            name="accounts_empty",
            builder=lambda: render_accounts_page(accounts=()),
        ),
        Scenario(
            name="accounts_with_default",
            builder=lambda: render_accounts_page(
                accounts=(account_a, account_b),
                default_account_id="user-aaaaaa",
                enabled_by_user_id={"user-aaaaaa": True, "user-bbbbbb": True},
            ),
        ),
        Scenario(
            name="accounts_with_signed_out",
            builder=lambda: render_accounts_page(
                accounts=(account_a, account_b),
                default_account_id="user-aaaaaa",
                enabled_by_user_id={"user-aaaaaa": True, "user-bbbbbb": False},
            ),
        ),
        # -- Workspace settings ------------------------------------------
        Scenario(
            name="workspace_settings_no_account",
            builder=lambda: render_workspace_settings(
                agent_id=str(agent_a),
                ws_name="alpha",
                current_account=None,
                accounts=(account_a,),
                servers=("system_interface",),
                telegram_state=None,
            ),
        ),
        Scenario(
            name="workspace_settings_with_account",
            builder=lambda: render_workspace_settings(
                agent_id=str(agent_a),
                ws_name="alpha",
                current_account=account_a,
                accounts=(account_a,),
                servers=("system_interface", "frontend"),
                telegram_state="active",
            ),
        ),
        Scenario(
            name="workspace_settings_no_servers",
            builder=lambda: render_workspace_settings(
                agent_id=str(agent_a),
                ws_name="alpha",
                current_account=account_a,
                accounts=(account_a,),
                servers=(),
                telegram_state="pending",
            ),
        ),
        # -- Sharing editor ----------------------------------------------
        Scenario(
            name="sharing_no_account",
            builder=lambda: render_sharing_editor(
                agent_id=str(agent_a),
                service_name="frontend",
                title="Share frontend in alpha",
                has_account=False,
                accounts=(account_a,),
                ws_name="alpha",
            ),
        ),
        Scenario(
            name="sharing_with_account",
            builder=lambda: render_sharing_editor(
                agent_id=str(agent_a),
                service_name="frontend",
                title="Share frontend in alpha",
                mngr_forward_origin="http://localhost:8421",
                initial_emails=["bob@example.com"],
                has_account=True,
                accounts=(account_a,),
                ws_name="alpha",
                account_email="alice@example.com",
            ),
        ),
        # -- Chrome (titlebar) -------------------------------------------
        Scenario(name="chrome_mac_unauth", builder=lambda: render_chrome_page(is_mac=True, is_authenticated=False)),
        Scenario(name="chrome_mac_auth", builder=lambda: render_chrome_page(is_mac=True, is_authenticated=True)),
        Scenario(name="chrome_non_mac_auth", builder=lambda: render_chrome_page(is_mac=False, is_authenticated=True)),
        # -- Sidebar -----------------------------------------------------
        Scenario(name="sidebar", builder=lambda: render_sidebar_page(mngr_forward_origin="http://localhost:8421")),
        # -- Login / login_redirect / auth_error / inbox_unavailable / inbox_empty ---
        Scenario(name="login", builder=render_login_page),
        Scenario(
            name="login_redirect",
            builder=lambda: render_login_redirect_page(one_time_code=OneTimeCode("abc123-secret-82341")),
        ),
        Scenario(
            name="auth_error",
            builder=lambda: render_auth_error_page(message="This code has already been used."),
        ),
        Scenario(
            name="inbox_unavailable_fragment",
            builder=lambda: render_inbox_unavailable_fragment(message="This request was already granted."),
        ),
        Scenario(
            name="inbox_empty",
            builder=lambda: render_inbox_page(cards=[], selected_id="", detail_html="", is_empty=True),
        ),
        # -- Dev styleguide ----------------------------------------------
        Scenario(name="dev_styleguide", builder=render_dev_styleguide_page),
        # -- Latchkey permission dialogs ---------------------------------
        Scenario(
            name="latchkey_predefined_some_checked",
            builder=lambda: render_predefined_permission_dialog(
                agent_id=str(agent_a),
                request_id="req-00000000000000000000000000000001",
                ws_name="alpha",
                rationale="I want to summarize today's messages.",
                service=slack_service,
                checked_permissions=("slack-read",),
                will_open_browser=True,
                mngr_forward_origin="http://localhost:8421",
            ),
        ),
        Scenario(
            name="latchkey_predefined_none_checked",
            builder=lambda: render_predefined_permission_dialog(
                agent_id=str(agent_a),
                request_id="req-00000000000000000000000000000001",
                ws_name="alpha",
                rationale="I haven't yet decided what permissions to ask for.",
                service=slack_service,
                checked_permissions=(),
                will_open_browser=False,
                mngr_forward_origin="http://localhost:8421",
            ),
        ),
        Scenario(
            name="latchkey_file_sharing_read",
            builder=lambda: render_file_sharing_permission_dialog(
                agent_id=str(agent_a),
                request_id="req-00000000000000000000000000000001",
                ws_name="alpha",
                rationale="I need to read this file to answer your question.",
                file_path="/Users/alice/Documents/notes.md",
                access="READ",
                access_human_label="read-only",
                mngr_forward_origin="http://localhost:8421",
            ),
        ),
        Scenario(
            name="latchkey_file_sharing_write",
            builder=lambda: render_file_sharing_permission_dialog(
                agent_id=str(agent_a),
                request_id="req-00000000000000000000000000000001",
                ws_name="alpha",
                rationale="I need to update this file in place.",
                file_path="/Users/alice/Documents/notes.md",
                access="WRITE",
                access_human_label="read & write",
                mngr_forward_origin="http://localhost:8421",
            ),
        ),
        # -- Auth pages (SuperTokens) ------------------------------------
        Scenario(name="auth_signup_default", builder=lambda: render_auth_page(default_to_signup=True)),
        Scenario(name="auth_signin_default", builder=lambda: render_auth_page(default_to_signup=False)),
        Scenario(
            name="auth_signup_with_message",
            builder=lambda: render_auth_page(default_to_signup=True, message="Please sign up to continue."),
        ),
        Scenario(name="auth_check_email", builder=lambda: render_check_email_page(email="alice@example.com")),
        Scenario(name="auth_forgot_password", builder=render_forgot_password_page),
        Scenario(
            name="auth_oauth_close_with_name",
            builder=lambda: render_oauth_close_page(email="alice@example.com", display_name="Alice"),
        ),
        Scenario(
            name="auth_oauth_close_without_name",
            builder=lambda: render_oauth_close_page(email="alice@example.com"),
        ),
        Scenario(
            name="auth_settings_email",
            builder=lambda: render_settings_page(
                email="alice@example.com",
                display_name="Alice",
                user_id="user-aaaaaa",
                provider="email",
                user_id_prefix="user-aa",
            ),
        ),
        Scenario(
            name="auth_settings_oauth",
            builder=lambda: render_settings_page(
                email="alice@example.com",
                display_name=None,
                user_id="user-aaaaaa",
                provider="google",
                user_id_prefix="user-aa",
            ),
        ),
    ]


# -- Capture subcommand --------------------------------------------------


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


def _render_all_html(scenarios: list[Scenario], html_dir: Path) -> None:
    """Render every scenario's HTML to disk; log + record failures inline."""
    for sc in scenarios:
        try:
            rendered = sc.builder()
        except _BUILDER_EXCEPTIONS as exc:
            logger.opt(exception=exc).warning("[render fail] {}: {}", sc.name, type(exc).__name__)
            (html_dir / f"{sc.name}.html").write_text(f"<!-- RENDER FAILED: {html.escape(str(exc))} -->")
            continue
        (html_dir / f"{sc.name}.html").write_text(rendered)


def _screenshot_all(scenarios: list[Scenario], png_dir: Path, port: int) -> None:
    """Screenshot every scenario via Playwright."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H})
            page = context.new_page()
            for sc in scenarios:
                target = f"http://127.0.0.1:{port}/html/{sc.name}.html"
                try:
                    page.goto(target, wait_until="networkidle", timeout=15000)
                    # The chrome links the compiled Tailwind sheet
                    # (/_static/app.min.css). ``networkidle`` requests it but
                    # doesn't guarantee the browser has parsed it into
                    # cssRules. Wait until that sheet is present and non-empty
                    # before snapping -- otherwise we screenshot the unstyled
                    # "ASCII-art" version of the page.
                    page.wait_for_function(
                        "() => Array.from(document.styleSheets).some(s => {"
                        "  try { return (s.href || '').includes('app.min.css')"
                        "    && s.cssRules.length > 0; }"
                        "  catch (e) { return false; } })",
                        timeout=10000,
                    )
                    for action in sc.interactions:
                        action(page)
                    page.screenshot(path=str(png_dir / f"{sc.name}.png"), full_page=True)
                    logger.info("[shot] {}", sc.name)
                except _PLAYWRIGHT_EXCEPTIONS as exc:
                    logger.opt(exception=exc).warning("[shot fail] {}: {}", sc.name, type(exc).__name__)
        finally:
            browser.close()


def _build_css() -> None:
    """Compile static/app.css -> static/app.min.css for the current branch.

    The chrome no longer ships a runtime Tailwind JIT; styles come from the
    compiled sheet, so a capture is only faithful if the sheet was just built
    from this branch's source. Delegates to the same `build:css` pnpm script
    used by `just minds-css`, with the pinned Node on PATH.
    """
    minds_dir = REPO_ROOT / "apps" / "minds"
    logger.info("[capture] building app.min.css (pnpm run build:css)")
    subprocess.run(
        ["bash", "-c", ". scripts/select_node_version.sh && pnpm run build:css"],
        cwd=str(minds_dir),
        check=True,
    )


def _do_capture(label: str) -> Path:
    output_dir = OUTPUT_ROOT / label
    html_dir = output_dir / "html"
    png_dir = output_dir / "png"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    html_dir.mkdir(parents=True)
    png_dir.mkdir(parents=True)

    # The chrome's styles come from the compiled Tailwind sheet (app.min.css),
    # which is gitignored and only exists after a build -- and must reflect the
    # CURRENT branch's source so the diff is meaningful. Rebuild it here.
    _build_css()

    # Symlink static into the served root so /_static/app.min.css (+ the
    # per-page JS) resolve. Symlink (not copy) so we always pick up the live
    # file.
    (output_dir / "_static").symlink_to(STATIC_DIR)

    scenarios = _build_scenarios()
    logger.info("[capture] rendering {} scenarios -> {}", len(scenarios), output_dir)

    # 1. Render HTML for every scenario first. Failures here will catch
    # any obviously-broken template before we spin up Playwright.
    _render_all_html(scenarios, html_dir)

    # 2. Boot HTTP server + Playwright; screenshot each.
    httpd, _thread, port = _serve_directory(output_dir)
    try:
        _screenshot_all(scenarios, png_dir, port)
    finally:
        httpd.shutdown()
        httpd.server_close()

    logger.info("[capture] done: {}", output_dir)
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
    content without flagging Jinja-vs-JinjaX whitespace cosmetics.
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

    PNG hash beats HTML diff because the Jinja-to-JinjaX migration
    legitimately reshuffles whitespace and prefers literal Unicode over
    HTML entities (``--`` vs ``&mdash;``), both of which render identically.
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
    standalone (and to avoid bootstrapping JinjaX in this script).

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

    p_capture = sub.add_parser("capture", help="render all scenarios + screenshot")
    p_capture.add_argument(
        "--label",
        required=True,
        help="output dir label (e.g. 'main', 'jinjax'); written to .visual-diff/<label>/",
    )

    p_compare = sub.add_parser("compare", help="diff two captures, produce report.html")
    p_compare.add_argument("label_a", help="baseline label")
    p_compare.add_argument("label_b", help="comparison label")

    sub.add_parser("list-scenarios", help="print scenario names without rendering")

    args = parser.parse_args(argv)

    if args.cmd == "capture":
        _do_capture(args.label)
        return 0
    if args.cmd == "compare":
        _do_compare(args.label_a, args.label_b)
        return 0
    if args.cmd == "list-scenarios":
        for sc in _build_scenarios():
            logger.info("{}", sc.name)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
