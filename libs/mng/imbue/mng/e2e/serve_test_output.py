"""Simple web server for viewing e2e test outputs.

Serves transcript files and asciinema cast recordings from .test_output/.

Usage:
    uv run python -m imbue.mng.e2e.serve_test_output [--port PORT]
"""

import argparse
import html
import json
import re
import sys
from http.server import HTTPServer
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

from imbue.mng.utils.detail_renderer import ASCIINEMA_PLAYER_CSS
from imbue.mng.utils.detail_renderer import ASCIINEMA_PLAYER_JS
from imbue.mng.utils.detail_renderer import render_transcript
from imbue.mng.utils.detail_renderer import render_tutorial_block

_E2E_DIR = Path(__file__).resolve().parent
_REPO_ROOT = next(p for p in [_E2E_DIR, *_E2E_DIR.parents] if (p / ".git").exists())
_TEST_OUTPUT_DIR = _REPO_ROOT / ".test_output" / "e2e"

_SIDEBAR_CSS = "\n".join(
    [
        ".layout { display: flex; gap: 0; }",
        ".sidebar-panel { display: flex; flex-shrink: 0; border-right: 1px solid rgb(221,221,221); }",
        ".sidebar-toggle { writing-mode: vertical-lr; cursor: pointer; user-select: none;"
        " font-size: 0.8em; color: rgb(102,102,102); padding: 0.5em 0.3em;"
        " background: rgb(240,240,240); border: none; border-right: 1px solid rgb(221,221,221); }",
        ".sidebar-toggle:hover { color: rgb(0,102,204); background: rgb(232,232,232); }",
        ".sidebar { width: 300px; padding: 0.5em 1em; font-size: 0.85em; overflow-y: auto;"
        " max-height: calc(100vh - 6em); position: sticky; top: 0; }",
        ".sidebar.collapsed { width: 0; padding: 0; overflow: hidden; }",
        ".sidebar ul { list-style: none; padding: 0; margin: 0; }",
        ".sidebar li { margin: 0.3em 0; }",
        ".sidebar li.active { font-weight: bold; }",
        ".sidebar a { color: rgb(51,51,51); }",
        ".main-content { flex: 1; min-width: 0; padding-left: 1.5em; }",
    ]
)

_SIDEBAR_JS = """
<script>
(function() {
  var KEY = 'e2e-sidebar-collapsed';
  var sidebar = document.querySelector('.sidebar');
  var toggle = document.querySelector('.sidebar-toggle');
  if (!sidebar || !toggle) return;
  if (localStorage.getItem(KEY) === 'true') {
    sidebar.classList.add('collapsed');
  }
  toggle.addEventListener('click', function() {
    sidebar.classList.toggle('collapsed');
    localStorage.setItem(KEY, sidebar.classList.contains('collapsed'));
  });
})();
</script>"""


def _html_page(title: str, nav: str, body: str, sidebar: str | None = None) -> str:
    """Render a full HTML page.

    The nav (breadcrumb) is always rendered at the top of the page, outside any
    sidebar layout. If sidebar is provided, the body is placed inside a flex
    layout with the sidebar on the left.
    """
    extra_css = ""
    extra_js = ""
    if sidebar is not None:
        extra_css = _SIDEBAR_CSS
        extra_js = _SIDEBAR_JS
        sidebar_html = (
            '<div class="sidebar-panel">'
            '<button class="sidebar-toggle">Tests</button>'
            f'<div class="sidebar">{sidebar}</div>'
            "</div>"
        )
        body = '<div class="layout">' + sidebar_html + '<div class="main-content">' + body + "</div></div>"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<link rel="stylesheet" type="text/css" href="{ASCIINEMA_PLAYER_CSS}">
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2em; background: rgb(250,250,250); color: rgb(34,34,34); }}
  h2 {{ font-size: 1.1em; margin-top: 2em; }}
  a {{ color: rgb(0,102,204); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  nav {{ margin-bottom: 1em; font-size: 0.9em; color: rgb(102,102,102); }}
  nav a {{ margin-right: 0.3em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: 0.3em 0; }}
  .transcript {{ background: rgb(30,30,30); color: rgb(212,212,212); padding: 1em; border-radius: 6px; overflow-x: auto; font-family: 'SF Mono', 'Menlo', 'Consolas', monospace; font-size: 0.85em; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; }}
  .transcript .cmd-block {{ border-top: 1px solid rgb(68,68,68); padding-top: 0.6em; margin-top: 0.6em; }}
  .transcript .cmd-block:first-child {{ border-top: none; padding-top: 0; margin-top: 0; }}
  .transcript .comment {{ color: rgb(220,220,170); }}
  .transcript .prompt {{ color: rgb(86,156,214); }}
  .transcript .stderr-prefix {{ color: rgb(244,71,71); }}
  .transcript .exit-code {{ color: rgb(136,136,136); font-style: italic; }}
  .cast-player {{ margin: 1em 0; display: flex; justify-content: flex-start; }}
  {extra_css}
</style>
</head>
<body>
{nav}
{body}
<script src="{ASCIINEMA_PLAYER_JS}"></script>
{extra_js}
</body>
</html>"""


def _index_page() -> str:
    """List all test runs."""
    runs = sorted(
        [d for d in _TEST_OUTPUT_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )
    items = "\n".join(f'<li><a href="/run/{r.name}">{r.name}</a></li>' for r in runs)
    return _html_page("E2E Test Runs", "<nav><b>Test Runs</b></nav>", "<ul>\n" + items + "\n</ul>")


def _run_page(run_name: str) -> str | None:
    """List all tests in a run -- full listing, no sidebar."""
    run_dir = _TEST_OUTPUT_DIR / run_name
    if not run_dir.is_dir():
        return None
    tests = sorted(d for d in run_dir.iterdir() if d.is_dir())
    items = "\n".join(f'<li><a href="/run/{run_name}/{t.name}">{t.name}</a></li>' for t in tests)
    nav = f'<nav><a href="/">Test Runs</a> / <b>{html.escape(run_name)}</b></nav>'
    return _html_page(f"Run {run_name}", nav, "<ul>\n" + items + "\n</ul>")


def _build_test_sidebar(run_name: str, run_dir: Path, active_test: str) -> str:
    """Build sidebar HTML listing all tests in a run."""
    all_tests = sorted(d.name for d in run_dir.iterdir() if d.is_dir())
    items: list[str] = []
    for t in all_tests:
        cls = ' class="active"' if t == active_test else ""
        items.append(f'<li{cls}><a href="/run/{run_name}/{t}">{t}</a></li>')
    return "<ul>" + "\n".join(items) + "</ul>"


def _test_page(run_name: str, test_name: str) -> str | None:
    """Show transcript and cast players for a single test."""
    run_dir = _TEST_OUTPUT_DIR / run_name
    test_dir = run_dir / test_name
    if not test_dir.is_dir():
        return None

    sidebar = _build_test_sidebar(run_name, run_dir, active_test=test_name)

    nav = (
        f'<nav><a href="/">Test Runs</a> / '
        f'<a href="/run/{html.escape(run_name)}">{html.escape(run_name)}</a> / '
        f"<b>{html.escape(test_name)}</b></nav>"
    )

    parts: list[str] = []

    # Tutorial block (the original script block this test covers)
    tutorial_block_path = test_dir / "tutorial_block.txt"
    if tutorial_block_path.exists():
        parts.append("<h2>Tutorial block</h2>")
        parts.append(render_tutorial_block(tutorial_block_path.read_text()))

    # Collect cast files first so we can link agent names in the transcript
    cast_files = sorted(test_dir.glob("*.cast"))
    cast_stems = [f.stem for f in cast_files]

    # Transcript
    transcript_path = test_dir / "transcript.txt"
    if transcript_path.exists():
        parts.append("<h2>CLI transcript</h2>")
        parts.append(render_transcript(transcript_path.read_text(), cast_stems=cast_stems))

    # Cast players
    player_inits: list[str] = []
    for i, cast_file in enumerate(cast_files):
        cast_url = f"/cast/{run_name}/{test_name}/{cast_file.name}"
        anchor_id = f"cast-{html.escape(cast_file.stem)}"
        parts.append(f'<h2 id="{anchor_id}">TUI recording: {html.escape(cast_file.stem)}</h2>')
        div_id = f"player-{i}"
        parts.append(f'<div id="{div_id}" class="cast-player"></div>')
        player_inits.append(
            f"AsciinemaPlayer.create({json.dumps(cast_url)}, "
            f"document.getElementById({json.dumps(div_id)}), "
            f"{{fit: 'none', terminalFontSize: '0.85em', theme: 'asciinema'}});"
        )

    if player_inits:
        init_code = "\n  ".join(player_inits)
        parts.append(
            f"<script>\n"
            f"document.addEventListener('DOMContentLoaded', function() {{\n"
            f"  var check = setInterval(function() {{\n"
            f"    if (typeof AsciinemaPlayer !== 'undefined') {{\n"
            f"      clearInterval(check);\n"
            f"      {init_code}\n"
            f"    }}\n"
            f"  }}, 50);\n"
            f"}});\n"
            f"</script>"
        )

    return _html_page(f"{test_name} - {run_name}", nav, "\n".join(parts), sidebar=sidebar)


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path == "/" or path == "":
            self._respond_html(_index_page())
            return

        # /run/<run_name>
        m = re.fullmatch(r"/run/([^/]+)", path)
        if m:
            page = _run_page(m.group(1))
            if page:
                self._respond_html(page)
            else:
                self._respond_404()
            return

        # /run/<run_name>/<test_name>
        m = re.fullmatch(r"/run/([^/]+)/([^/]+)", path)
        if m:
            page = _test_page(m.group(1), m.group(2))
            if page:
                self._respond_html(page)
            else:
                self._respond_404()
            return

        # /cast/<run_name>/<test_name>/<file.cast>
        m = re.fullmatch(r"/cast/([^/]+)/([^/]+)/([^/]+\.cast)", path)
        if m:
            cast_path = _TEST_OUTPUT_DIR / m.group(1) / m.group(2) / m.group(3)
            if cast_path.is_file():
                self._respond_file(cast_path, "application/json")
            else:
                self._respond_404()
            return

        self._respond_404()

    def _respond_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_404(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not found")

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve e2e test output for viewing")
    parser.add_argument("--port", type=int, default=8742, help="Port to listen on (default: 8742)")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    sys.stdout.write(f"Serving e2e test output at http://127.0.0.1:{args.port}\n")
    sys.stdout.write(f"Test output dir: {_TEST_OUTPUT_DIR}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
