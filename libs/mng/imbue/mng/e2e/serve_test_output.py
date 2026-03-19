"""Simple web server for viewing e2e test outputs.

Serves transcript files and asciinema cast recordings from .test_output/.

Usage:
    uv run python -m imbue.mng.e2e.serve_test_output [--port PORT]
"""

import argparse
import html
import json
import re
from http.server import HTTPServer
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

_TEST_OUTPUT_DIR = Path(__file__).resolve().parent / ".test_output"

_ASCIINEMA_PLAYER_CSS = "https://cdn.jsdelivr.net/npm/asciinema-player@3.9.0/dist/bundle/asciinema-player.css"
_ASCIINEMA_PLAYER_JS = "https://cdn.jsdelivr.net/npm/asciinema-player@3.9.0/dist/bundle/asciinema-player.min.js"


def _html_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<link rel="stylesheet" type="text/css" href="{_ASCIINEMA_PLAYER_CSS}">
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2em; background: #fafafa; color: #222; }}
  h1 {{ font-size: 1.4em; }}
  h2 {{ font-size: 1.1em; margin-top: 2em; }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  nav {{ margin-bottom: 1.5em; font-size: 0.9em; color: #666; }}
  nav a {{ margin-right: 0.3em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: 0.3em 0; }}
  .transcript {{ background: #1e1e1e; color: #d4d4d4; padding: 1em; border-radius: 6px; overflow-x: auto; font-family: 'SF Mono', 'Menlo', 'Consolas', monospace; font-size: 0.85em; line-height: 1.6; }}
  .transcript .cmd-block {{ border-top: 1px solid #444; padding-top: 0.6em; margin-top: 0.6em; }}
  .transcript .cmd-block:first-child {{ border-top: none; padding-top: 0; margin-top: 0; }}
  .transcript .comment {{ color: #6a9955; }}
  .transcript .prompt {{ color: #569cd6; }}
  .transcript .stderr {{ color: #f44747; }}
  .transcript .exit-code {{ color: #888; font-style: italic; }}
  .cast-player {{ margin: 1em 0; }}
  .cast-label {{ font-family: 'SF Mono', 'Menlo', 'Consolas', monospace; font-size: 0.85em; color: #666; margin-bottom: 0.3em; }}
</style>
</head>
<body>
{body}
<script src="{_ASCIINEMA_PLAYER_JS}"></script>
</body>
</html>"""


def _render_transcript(text: str) -> str:
    """Render a transcript into styled HTML blocks."""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    current_block: list[str] = []

    for line in lines:
        if line.startswith("# ") or line.startswith("$ "):
            if current_block and (line.startswith("# ") or line.startswith("$ ")):
                # Check if this is the start of a new command (comment or prompt following an exit code)
                if current_block and any(l.startswith("$ ") for l in current_block):
                    blocks.append(current_block)
                    current_block = []
        current_block.append(line)

    if current_block:
        blocks.append(current_block)

    # Re-split: a block starts at the first "# " or "$ " that follows a "? " line
    blocks = []
    current_block = []
    for line in lines:
        is_new_block_start = (
            (line.startswith("# ") or line.startswith("$ "))
            and current_block
            and any(l.startswith("? ") for l in current_block)
        )
        if is_new_block_start:
            blocks.append(current_block)
            current_block = []
        current_block.append(line)
    if current_block:
        blocks.append(current_block)

    html_parts: list[str] = []
    for block in blocks:
        rendered_lines: list[str] = []
        for line in block:
            escaped = html.escape(line)
            if line.startswith("# "):
                rendered_lines.append(f'<span class="comment">{escaped}</span>')
            elif line.startswith("$ "):
                rendered_lines.append(f'<span class="prompt">{escaped}</span>')
            elif line.startswith("! "):
                rendered_lines.append(f'<span class="stderr">{escaped}</span>')
            elif re.match(r"^\? \d+$", line):
                code = line[2:]
                rendered_lines.append(f'<span class="exit-code">exit code: {html.escape(code)}</span>')
            else:
                rendered_lines.append(escaped)
        html_parts.append('<div class="cmd-block">' + "\n".join(rendered_lines) + "</div>")

    return '<div class="transcript">' + "\n".join(html_parts) + "</div>"


def _index_page() -> str:
    """List all test runs."""
    runs = sorted(
        [d for d in _TEST_OUTPUT_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )
    items = "\n".join(f'<li><a href="/run/{r.name}">{r.name}</a></li>' for r in runs)
    return _html_page("E2E Test Runs", f"<h1>Test Runs</h1>\n<ul>\n{items}\n</ul>")


def _run_page(run_name: str) -> str | None:
    """List all tests in a run."""
    run_dir = _TEST_OUTPUT_DIR / run_name
    if not run_dir.is_dir():
        return None
    tests = sorted(d for d in run_dir.iterdir() if d.is_dir())
    items = "\n".join(f'<li><a href="/run/{run_name}/{t.name}">{t.name}</a></li>' for t in tests)
    nav = f'<nav><a href="/">&larr; all runs</a></nav>'
    return _html_page(f"Run {run_name}", f"{nav}<h1>Run {html.escape(run_name)}</h1>\n<ul>\n{items}\n</ul>")


def _test_page(run_name: str, test_name: str) -> str | None:
    """Show transcript and cast players for a single test."""
    test_dir = _TEST_OUTPUT_DIR / run_name / test_name
    if not test_dir.is_dir():
        return None

    nav = f'<nav><a href="/">&larr; all runs</a> / <a href="/run/{html.escape(run_name)}">{html.escape(run_name)}</a></nav>'
    parts = [f"{nav}<h1>{html.escape(test_name)}</h1>"]

    # Transcript
    transcript_path = test_dir / "transcript.txt"
    if transcript_path.exists():
        parts.append("<h2>Transcript</h2>")
        parts.append(_render_transcript(transcript_path.read_text()))

    # Cast files
    cast_files = sorted(test_dir.glob("*.cast"))
    for i, cast_file in enumerate(cast_files):
        cast_url = f"/cast/{run_name}/{test_name}/{cast_file.name}"
        parts.append(f"<h2>Recording: {html.escape(cast_file.stem)}</h2>")
        parts.append(f'<div class="cast-label">{html.escape(cast_file.name)}</div>')
        div_id = f"player-{i}"
        parts.append(f'<div id="{div_id}" class="cast-player"></div>')
        parts.append(
            f"<script>AsciinemaPlayer.create({json.dumps(cast_url)}, "
            f"document.getElementById({json.dumps(div_id)}), "
            f"{{fit: 'width', theme: 'asciinema'}});</script>"
        )

    return _html_page(f"{test_name} - {run_name}", "\n".join(parts))


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
        # Quieter logging -- just method and path
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve e2e test output for viewing")
    parser.add_argument("--port", type=int, default=8742, help="Port to listen on (default: 8742)")
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"Serving e2e test output at http://127.0.0.1:{args.port}")
    print(f"Test output dir: {_TEST_OUTPUT_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
