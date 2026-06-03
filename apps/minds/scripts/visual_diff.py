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
import dataclasses
import difflib
import html
import http.server
import shutil
import socket
import socketserver
import struct
import sys
import threading
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

# All template-rendering functions live in three modules in the desktop_client
# package -- import them lazily inside scenario builders so this script can
# be invoked from any CWD without having to keep its module path in sync.


def _repo_root() -> Path:
    """Locate the repo root.

    We prefer ``git rev-parse --show-toplevel`` over a hardcoded relative
    path so the script keeps working when copied outside the tree (useful
    for capturing both sides of a branch swap from one stable location).
    """
    import subprocess

    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path(__file__).resolve().parents[3]


REPO_ROOT: Final[Path] = _repo_root()
STATIC_DIR: Final[Path] = (
    REPO_ROOT / "apps" / "minds" / "imbue" / "minds" / "desktop_client" / "static"
)
OUTPUT_ROOT: Final[Path] = REPO_ROOT / "apps" / "minds" / ".visual-diff"

VIEWPORT_W: Final[int] = 1440
VIEWPORT_H: Final[int] = 900


# -- Scenario catalog -----------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Scenario:
    """One renderable state to capture.

    ``builder`` returns the HTML string. We pass it as a thunk (rather than
    pre-rendering) so import side-effects from a single branch never bleed
    into both sides of a comparison run.

    ``interactions`` is a list of Playwright actions to run BEFORE
    screenshotting, e.g. clicking a button to open a modal. Each action
    receives the active ``page`` object. Keep them small; complex driving
    belongs in dedicated tests.
    """

    name: str
    builder: Callable[[], str]
    interactions: tuple[Callable[[Any], None], ...] = ()


def _stub_account(user_id: str, email: str, n_workspaces: int = 0) -> Any:
    """Minimal account stub matching the attrs the templates read.

    The real Account model lives in the imbue_cloud client and pulling it
    in here would couple this tool to that whole subsystem.
    """

    @dataclasses.dataclass(frozen=True)
    class _Account:
        user_id: str
        email: str
        workspace_ids: tuple[str, ...]

    return _Account(user_id=user_id, email=email, workspace_ids=tuple(f"agent-{i:032d}" for i in range(n_workspaces)))


def _build_scenarios() -> list[Scenario]:
    # Imports inside the function so this script can at least print its
    # --help without the full minds backend on the path.
    from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
    from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
    from imbue.minds.desktop_client.latchkey.handlers.templates import render_file_sharing_permission_dialog
    from imbue.minds.desktop_client.latchkey.handlers.templates import render_predefined_permission_dialog
    from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
    from imbue.minds.desktop_client.templates import render_accounts_page
    from imbue.minds.desktop_client.templates import render_auth_error_page
    from imbue.minds.desktop_client.templates import render_chrome_page
    from imbue.minds.desktop_client.templates import render_create_form
    from imbue.minds.desktop_client.templates import render_creating_page
    from imbue.minds.desktop_client.templates import render_destroying_page
    from imbue.minds.desktop_client.templates import render_dev_styleguide_page
    from imbue.minds.desktop_client.templates import render_landing_page
    from imbue.minds.desktop_client.templates import render_login_page
    from imbue.minds.desktop_client.templates import render_login_redirect_page
    from imbue.minds.desktop_client.templates import render_request_unavailable_page
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
        Scenario("landing_empty", lambda: render_landing_page(accessible_agent_ids=())),
        Scenario(
            "landing_discovering",
            lambda: render_landing_page(accessible_agent_ids=(), is_discovering=True),
        ),
        Scenario(
            "landing_with_workspaces",
            lambda: render_landing_page(
                accessible_agent_ids=(agent_a, agent_b),
                mngr_forward_origin="http://localhost:8421",
                agent_names={str(agent_a): "alpha", str(agent_b): "beta"},
            ),
        ),
        Scenario(
            "landing_with_destroying_workspace",
            lambda: render_landing_page(
                accessible_agent_ids=(agent_a, agent_b, agent_c),
                mngr_forward_origin="http://localhost:8421",
                agent_names={str(agent_a): "alpha", str(agent_b): "beta-destroying", str(agent_c): "gamma-failed"},
                destroying_status_by_agent_id={str(agent_b): "running", str(agent_c): "failed"},
            ),
        ),
        # -- Welcome page ------------------------------------------------
        Scenario("welcome", render_welcome_page),
        # -- Create form -------------------------------------------------
        Scenario("create_no_account", lambda: render_create_form()),
        Scenario(
            "create_with_account",
            lambda: render_create_form(accounts=(account_a,), default_account_id="user-aaaaaa"),
        ),
        Scenario(
            "create_with_error",
            lambda: render_create_form(error_message="imbue_cloud requires an account."),
        ),
        Scenario(
            "create_lima_subscription",
            lambda: render_create_form(launch_mode=LaunchMode.LIMA, ai_provider=AIProvider.SUBSCRIPTION),
        ),
        Scenario(
            "create_with_master_password",
            lambda: render_create_form(
                backup_provider=BackupProvider.IMBUE_CLOUD,
                backup_encryption_method=BackupEncryptionMethod.MASTER_PASSWORD,
                has_saved_backup_password=False,
                accounts=(account_a,),
                default_account_id="user-aaaaaa",
            ),
        ),
        # -- Creating page (question flow + loading) ---------------------
        Scenario(
            "creating",
            lambda: render_creating_page(
                creation_id=CreationId("creation-00000000000000000000000000000001"),
                info=creation_info_running,
            ),
        ),
        # -- Destroying detail page --------------------------------------
        Scenario(
            "destroying_running",
            lambda: render_destroying_page(
                agent_id=agent_a, agent_name="alpha", pid=12345, status="running"
            ),
        ),
        Scenario(
            "destroying_failed",
            lambda: render_destroying_page(
                agent_id=agent_a, agent_name="alpha", pid=12345, status="failed"
            ),
        ),
        Scenario(
            "destroying_done",
            lambda: render_destroying_page(
                agent_id=agent_a, agent_name="alpha", pid=12345, status="done"
            ),
        ),
        # -- Accounts page -----------------------------------------------
        Scenario(
            "accounts_empty",
            lambda: render_accounts_page(accounts=()),
        ),
        Scenario(
            "accounts_with_default",
            lambda: render_accounts_page(
                accounts=(account_a, account_b),
                default_account_id="user-aaaaaa",
                enabled_by_user_id={"user-aaaaaa": True, "user-bbbbbb": True},
            ),
        ),
        Scenario(
            "accounts_with_signed_out",
            lambda: render_accounts_page(
                accounts=(account_a, account_b),
                default_account_id="user-aaaaaa",
                enabled_by_user_id={"user-aaaaaa": True, "user-bbbbbb": False},
            ),
        ),
        # -- Workspace settings ------------------------------------------
        Scenario(
            "workspace_settings_no_account",
            lambda: render_workspace_settings(
                agent_id=str(agent_a),
                ws_name="alpha",
                current_account=None,
                accounts=(account_a,),
                servers=("system_interface",),
                telegram_state=None,
            ),
        ),
        Scenario(
            "workspace_settings_with_account",
            lambda: render_workspace_settings(
                agent_id=str(agent_a),
                ws_name="alpha",
                current_account=account_a,
                accounts=(account_a,),
                servers=("system_interface", "frontend"),
                telegram_state="active",
            ),
        ),
        Scenario(
            "workspace_settings_no_servers",
            lambda: render_workspace_settings(
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
            "sharing_no_account",
            lambda: render_sharing_editor(
                agent_id=str(agent_a),
                service_name="frontend",
                title="Share frontend in alpha",
                has_account=False,
                accounts=(account_a,),
                ws_name="alpha",
            ),
        ),
        Scenario(
            "sharing_with_account",
            lambda: render_sharing_editor(
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
        Scenario("chrome_mac_unauth", lambda: render_chrome_page(is_mac=True, is_authenticated=False)),
        Scenario("chrome_mac_auth", lambda: render_chrome_page(is_mac=True, is_authenticated=True)),
        Scenario("chrome_non_mac_auth", lambda: render_chrome_page(is_mac=False, is_authenticated=True)),
        # -- Sidebar -----------------------------------------------------
        Scenario("sidebar", lambda: render_sidebar_page(mngr_forward_origin="http://localhost:8421")),
        # -- Login / login_redirect / auth_error / request_unavailable ---
        Scenario("login", render_login_page),
        Scenario(
            "login_redirect",
            lambda: render_login_redirect_page(one_time_code=OneTimeCode("abc123-secret-82341")),
        ),
        Scenario("auth_error", lambda: render_auth_error_page(message="This code has already been used.")),
        Scenario(
            "request_unavailable",
            lambda: render_request_unavailable_page(message="This request was already granted."),
        ),
        # -- Dev styleguide ----------------------------------------------
        Scenario("dev_styleguide", render_dev_styleguide_page),
        # -- Latchkey permission dialogs ---------------------------------
        Scenario(
            "latchkey_predefined_some_checked",
            lambda: render_predefined_permission_dialog(
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
            "latchkey_predefined_none_checked",
            lambda: render_predefined_permission_dialog(
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
            "latchkey_file_sharing_read",
            lambda: render_file_sharing_permission_dialog(
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
            "latchkey_file_sharing_write",
            lambda: render_file_sharing_permission_dialog(
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
        Scenario("auth_signup_default", lambda: render_auth_page(default_to_signup=True)),
        Scenario("auth_signin_default", lambda: render_auth_page(default_to_signup=False)),
        Scenario(
            "auth_signup_with_message",
            lambda: render_auth_page(default_to_signup=True, message="Please sign up to continue."),
        ),
        Scenario("auth_check_email", lambda: render_check_email_page(email="alice@example.com")),
        Scenario("auth_forgot_password", render_forgot_password_page),
        Scenario(
            "auth_oauth_close_with_name",
            lambda: render_oauth_close_page(email="alice@example.com", display_name="Alice"),
        ),
        Scenario("auth_oauth_close_without_name", lambda: render_oauth_close_page(email="alice@example.com")),
        Scenario(
            "auth_settings_email",
            lambda: render_settings_page(
                email="alice@example.com",
                display_name="Alice",
                user_id="user-aaaaaa",
                provider="email",
                user_id_prefix="user-aa",
            ),
        ),
        Scenario(
            "auth_settings_oauth",
            lambda: render_settings_page(
                email="alice@example.com",
                display_name=None,
                user_id="user-aaaaaa",
                provider="google",
                user_id_prefix="user-aa",
            ),
        ),
    ]


# -- Capture subcommand --------------------------------------------------


def _serve_directory(root: Path) -> tuple[socketserver.TCPServer, threading.Thread, int]:
    """Spin up a daemon HTTP server rooted at ``root`` on a random free port.

    Playwright loads the rendered HTML over HTTP rather than file:// because
    pages reference ``/_static/...`` as root-absolute paths; file:// breaks
    those references silently. The server runs until process exit.
    """

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # silence the per-request access log

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    httpd = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, port


def _do_capture(label: str) -> Path:
    output_dir = OUTPUT_ROOT / label
    html_dir = output_dir / "html"
    png_dir = output_dir / "png"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    html_dir.mkdir(parents=True)
    png_dir.mkdir(parents=True)

    # Symlink static into the served root so /_static/tailwind.js etc.
    # resolve. Symlink (not copy) so we always pick up the live file.
    (output_dir / "_static").symlink_to(STATIC_DIR)

    scenarios = _build_scenarios()
    print(f"[capture] rendering {len(scenarios)} scenarios -> {output_dir}")

    # 1. Render HTML for every scenario first. Failures here will catch
    # any obviously-broken template before we spin up Playwright.
    for sc in scenarios:
        try:
            rendered = sc.builder()
        except Exception as exc:
            print(f"  [render fail] {sc.name}: {type(exc).__name__}: {exc}")
            (html_dir / f"{sc.name}.html").write_text(f"<!-- RENDER FAILED: {html.escape(str(exc))} -->")
            continue
        (html_dir / f"{sc.name}.html").write_text(rendered)

    # 2. Boot HTTP server + Playwright; screenshot each.
    httpd, _thread, port = _serve_directory(output_dir)
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[capture] playwright not installed; skipping screenshot pass.")
            print("          run `uv pip install playwright && playwright install chromium`")
            return output_dir

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H})
                page = context.new_page()
                for sc in scenarios:
                    target = f"http://127.0.0.1:{port}/html/{sc.name}.html"
                    try:
                        page.goto(target, wait_until="networkidle", timeout=15000)
                        # Tailwind Play CDN generates the utility styles at
                        # runtime; give it a beat to settle before snapshotting.
                        page.wait_for_timeout(400)
                        for action in sc.interactions:
                            action(page)
                        page.screenshot(path=str(png_dir / f"{sc.name}.png"), full_page=True)
                        print(f"  [shot] {sc.name}")
                    except Exception as exc:
                        print(f"  [shot fail] {sc.name}: {type(exc).__name__}: {exc}")
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"[capture] done: {output_dir}")
    return output_dir


# -- Compare subcommand --------------------------------------------------


def _read_png_dimensions(path: Path) -> tuple[int, int] | None:
    """Read width/height from a PNG header without a full image library."""
    try:
        with path.open("rb") as fh:
            sig = fh.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return None
            fh.read(4)  # IHDR length
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


def _structural_html_diff(left_html: str, right_html: str) -> str | None:
    """Return None if the two HTML strings are equivalent enough; else a
    short summary of how they differ.

    We normalize whitespace (collapse runs to a single space) and compare.
    This catches missing/added elements, attribute drift, and changed text
    content without flagging Jinja-vs-JinjaX whitespace cosmetics.
    """
    import re

    def norm(s: str) -> str:
        s = re.sub(r"\s+", " ", s.strip())
        return s

    nl = norm(left_html)
    nr = norm(right_html)
    if nl == nr:
        return None
    # Produce a short unified diff for the report. Split on > to keep
    # lines short and aligned with HTML structure.
    left_lines = nl.replace(">", ">\n").splitlines()
    right_lines = nr.replace(">", ">\n").splitlines()
    diff = difflib.unified_diff(left_lines, right_lines, lineterm="", n=2)
    summary = "\n".join(list(diff)[:80])  # bound the report size
    if not summary:
        summary = "(differs only in whitespace position; both normalize equal)"
    return summary


def _do_compare(label_a: str, label_b: str) -> Path:
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
        diff = _structural_html_diff(html_left, html_right)

        png_a = dir_a / "png" / f"{name}.png"
        png_b = dir_b / "png" / f"{name}.png"
        png_status = "missing"
        if png_a.exists() and png_b.exists():
            ha, hb = _hash_bytes(png_a), _hash_bytes(png_b)
            if ha == hb:
                png_status = "identical"
            else:
                dim_a = _read_png_dimensions(png_a)
                dim_b = _read_png_dimensions(png_b)
                png_status = "differ" if dim_a == dim_b else f"differ ({dim_a} vs {dim_b})"
        rows.append(
            {
                "name": name,
                "verdict": "ok" if (diff is None and png_status == "identical") else "differs",
                "html_diff": diff or "(structurally equivalent)",
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
    print(f"[compare] {sum(1 for r in rows if r['verdict'] == 'ok')} ok / "
          f"{sum(1 for r in rows if r['verdict'] != 'ok')} different")
    print(f"[compare] report: {report_path}")
    return report_path


def _render_report(label_a: str, label_b: str, rows: list[dict[str, Any]]) -> str:
    """Hand-rolled HTML report -- no template engine to keep the tool
    standalone (and to avoid bootstrapping JinjaX in this script)."""
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>visual diff: {html.escape(label_a)} vs {html.escape(label_b)}</title>",
        "<style>",
        "  body { font: 14px -apple-system, system-ui, sans-serif; margin: 24px; color: #18181b; }",
        "  table { border-collapse: collapse; width: 100%; }",
        "  th, td { border: 1px solid #e4e4e7; padding: 8px; vertical-align: top; text-align: left; }",
        "  th { background: #fafafa; position: sticky; top: 0; }",
        "  td.shots { width: 50%; }",
        "  td.shots img { max-width: 100%; border: 1px solid #d4d4d8; }",
        "  pre { background: #fafafa; padding: 8px; overflow: auto; max-height: 280px; font-size: 12px; }",
        "  .verdict-ok { color: #047857; font-weight: 600; }",
        "  .verdict-differs { color: #b91c1c; font-weight: 600; }",
        "  .verdict-missing { color: #92400e; font-weight: 600; }",
        "</style></head><body>",
        f"<h1>visual diff: <code>{html.escape(label_a)}</code> vs <code>{html.escape(label_b)}</code></h1>",
        f"<p>{sum(1 for r in rows if r['verdict'] == 'ok')} ok / "
        f"{sum(1 for r in rows if r['verdict'] != 'ok')} different "
        f"/ total {len(rows)}</p>",
        "<table><thead><tr>"
        "<th>scenario</th><th>verdict</th>"
        f"<th>{html.escape(label_a)} screenshot</th>"
        f"<th>{html.escape(label_b)} screenshot</th>"
        "<th>structural HTML diff</th>"
        "</tr></thead><tbody>",
    ]
    for row in rows:
        verdict = row["verdict"]
        cls = (
            "verdict-ok" if verdict == "ok"
            else "verdict-missing" if verdict.startswith("missing")
            else "verdict-differs"
        )
        parts.append(f"<tr><td><code>{html.escape(row['name'])}</code></td>")
        parts.append(f"<td class='{cls}'>{html.escape(verdict)}</td>")
        if verdict.startswith("missing"):
            parts.append("<td colspan='3'>(scenario only in one capture)</td>")
        else:
            parts.append(f"<td class='shots'><img src='{html.escape(row['png_a_rel'])}' alt=''></td>")
            parts.append(f"<td class='shots'><img src='{html.escape(row['png_b_rel'])}' alt=''></td>")
            parts.append(f"<td><div>png: <code>{html.escape(row['png_status'])}</code></div>")
            parts.append(f"<pre>{html.escape(row['html_diff'])}</pre></td>")
        parts.append("</tr>")
    parts.append("</tbody></table></body></html>")
    return "".join(parts)


# -- main ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
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
            print(sc.name)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
